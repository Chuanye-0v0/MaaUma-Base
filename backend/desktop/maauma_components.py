"""Authorized Pacer overlays and coordinated base/module transactions."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import hashlib
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
import traceback
import subprocess
import sys
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import Request, build_opener
import uuid
from zipfile import ZipFile,BadZipFile

from desktop import maauma_service as service
from desktop import product_updates as updates
from desktop.update_connectivity import observed_phase, transport_failure

MANIFEST='maauma-component-manifest.json'
STATE='config/maauma-modules.json'
TRANSACTION='updates/maauma-suite-state.json'
LAST_RESULT='updates/last-result.json'
MAX_ARCHIVE=updates.MAX_ARCHIVE
MUTABLE={'local_backend.json','pacer/portable_config.json'}
PUBLIC_DOWNLOAD_HOST='maauma-downloads-1508966578.cos.ap-guangzhou.myqcloud.com'

# Original downloads and immutable prefixes produced by the updater itself.
_STAGED_ARCHIVE_MANIFESTS={
    'base.zip':'public-package-manifest.json',
    'pacer.zip':'maauma-component-manifest.json',
    'base-patch.zip':'maauma-file-patch.json',
    'pacer-patch.zip':'maauma-file-patch.json',
    'pacer-base.zip':'maauma-component-manifest.json',
    'pacer-patch-prefix.zip':'maauma-file-patch.json',
}


def _error(code):raise updates.UpdateError(code)


def _utc_now():return datetime.now(timezone.utc).isoformat()


def _stage_logger(stage):
    log_path=Path(stage)/'update.log'
    def log(message):
        try:
            log_path.parent.mkdir(parents=True,exist_ok=True)
            with log_path.open('a',encoding='utf-8') as stream:
                stream.write(f'[{_utc_now()}] {message}\n')
        except OSError:
            pass
    return log


def _write_last_result(root,payload):
    try:updates.write_json(updates.contained(root,LAST_RESULT),payload)
    except (OSError,updates.UpdateError,ValueError):pass


def _is_transient_lock_error(exc):
    return isinstance(exc,PermissionError) or getattr(exc,'winerror',None) in {5,32}


def _clear_readonly(path):
    try:
        p=Path(path)
        if p.exists():
            os.chmod(p,stat.S_IWRITE|stat.S_IREAD)
    except OSError:
        pass


def _try_in_place_verified_overwrite(src,dst_path,*,log=None):
    if not (dst_path.is_file() and not dst_path.is_symlink()):
        return False
    try:
        payload=Path(src).read_bytes()
        src_sha=hashlib.sha256(payload).hexdigest()
        _clear_readonly(dst_path)
        with dst_path.open('r+b') as stream:
            stream.write(payload)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
        if _digest(dst_path)==src_sha:
            try:Path(src).unlink(missing_ok=True)
            except OSError:pass
            if log:log(f'replace in-place verified overwrite succeeded for {dst_path}')
            return True
    except OSError:
        pass
    return False


def _replace_with_retry(src,dst,*,attempts=8,log=None):
    delays=(0.1,0.25,0.5,1.0,1.5,2.0,2.0)
    dst_path=Path(dst)
    for index in range(max(1,attempts)):
        _clear_readonly(src)
        _clear_readonly(dst_path)
        try:
            os.replace(src,dst)
            return
        except OSError as exc:
            if not _is_transient_lock_error(exc):raise
            if getattr(exc,'winerror',None)==5 and index>=1 and _try_in_place_verified_overwrite(src,dst_path,log=log):
                return
            if index<attempts-1:
                delay=delays[min(index,len(delays)-1)]
                if log:log(f'retryable replace error on {dst} (attempt {index+1}/{attempts}): {exc}; sleeping {delay}s')
                time.sleep(delay)
                continue
            if dst_path.exists():
                aside=dst_path.with_name(f'.{dst_path.name}.{uuid.uuid4().hex[:8]}.old')
                moved_aside=False
                try:
                    os.replace(dst,aside)
                    moved_aside=True
                    os.replace(src,dst)
                    try:aside.unlink(missing_ok=True)
                    except OSError:pass
                    if log:log(f'replace rename-aside succeeded for {dst}')
                    return
                except OSError:
                    if moved_aside and aside.exists() and not dst_path.exists():
                        try:os.replace(aside,dst)
                        except OSError:pass
                if _try_in_place_verified_overwrite(src,dst_path,log=log):
                    return
            raise


def _terminate_installation_pacer_processes(root,log=None):
    """Best-effort termination of lingering pacer/runtime processes inside *root*."""
    if os.name!='nt':return []
    try:
        import ctypes
        from ctypes import wintypes as w
        class Entry(ctypes.Structure):
            _fields_=[('size',w.DWORD),('usage',w.DWORD),('pid',w.DWORD),('heap',ctypes.c_size_t),
                      ('module',w.DWORD),('threads',w.DWORD),('parent',w.DWORD),('priority',w.LONG),
                      ('flags',w.DWORD),('name',w.WCHAR*260)]
        k=ctypes.WinDLL('kernel32',use_last_error=True)
        k.CreateToolhelp32Snapshot.argtypes=[w.DWORD,w.DWORD];k.CreateToolhelp32Snapshot.restype=w.HANDLE
        k.Process32FirstW.argtypes=[w.HANDLE,ctypes.POINTER(Entry)]
        k.Process32NextW.argtypes=[w.HANDLE,ctypes.POINTER(Entry)]
        k.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];k.OpenProcess.restype=w.HANDLE
        k.CloseHandle.argtypes=[w.HANDLE]
        k.QueryFullProcessImageNameW.argtypes=[w.HANDLE,w.DWORD,w.LPWSTR,ctypes.POINTER(w.DWORD)]
        k.TerminateProcess.argtypes=[w.HANDLE,w.UINT];k.TerminateProcess.restype=w.BOOL
        k.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD];k.WaitForSingleObject.restype=w.DWORD
        resolved_root=Path(root).resolve()
        pacer_dir=resolved_root/'pacer'
        runtime_dir=resolved_root/'runtime'
        self_pids={os.getpid(),os.getppid()}
        snapshot=k.CreateToolhelp32Snapshot(2,0)
        if snapshot==ctypes.c_void_p(-1).value:return []
        targets=[];entry=Entry();entry.size=ctypes.sizeof(entry)
        try:
            ok=k.Process32FirstW(snapshot,ctypes.byref(entry))
            while ok:
                pid=int(entry.pid)
                if pid>0 and pid not in self_pids:
                    proc=k.OpenProcess(0x1000,False,pid)
                    if proc:
                        try:
                            buf=ctypes.create_unicode_buffer(32768);size=w.DWORD(len(buf))
                            if k.QueryFullProcessImageNameW(proc,0,buf,ctypes.byref(size)):
                                try:
                                    exe=Path(buf.value).resolve()
                                    if (exe.is_relative_to(pacer_dir) or exe.is_relative_to(runtime_dir)
                                            or (exe.parent==resolved_root and (exe.name.lower()=='mfaavalonia.exe' or exe.name.lower().startswith('maauma')))):
                                        targets.append((pid,exe))
                                except (OSError,RuntimeError):
                                    pass
                        finally:
                            k.CloseHandle(proc)
                ok=k.Process32NextW(snapshot,ctypes.byref(entry))
        finally:
            k.CloseHandle(snapshot)
        terminated=[];handles=[]
        deadline=time.monotonic()+5.0
        for pid,exe in targets:
            proc=k.OpenProcess(0x0001|0x00100000|0x1000,False,pid)
            if not proc:
                if log:log(f'failed opening stale installation process pid={pid} exe={exe} err={ctypes.get_last_error()}')
                continue
            if log:log(f'terminating lingering installation process pid={pid} exe={exe}')
            k.TerminateProcess(proc,1)
            handles.append((pid,proc))
        try:
            for pid,proc in handles:
                rem_ms=max(0,int((deadline-time.monotonic())*1000))
                wait_rc=k.WaitForSingleObject(proc,rem_ms)
                terminated.append(pid)
                if log:log(f'terminated pid={pid} wait_rc={wait_rc}')
        finally:
            for _,proc in handles:
                k.CloseHandle(proc)
        return terminated
    except Exception as exc:
        if log:log(f'process cleanup warning: {type(exc).__name__}: {exc}')
        return []


def _digest(path,*,progress=None,verification_step='archive_integrity'):
    with Path(path).open('rb') as stream:
        if progress is None:return hashlib.file_digest(stream,'sha256').hexdigest()
        total=Path(path).stat().st_size;processed=0;digest=hashlib.sha256()
        progress.verification(verification_step,0,total)
        while block:=stream.read(1024**2):
            digest.update(block);processed+=len(block)
            progress.verification(verification_step,processed,total)
        return digest.hexdigest()


def state(root):
    path=updates.contained(root,STATE)
    value=updates.read_json(path) if path.is_file() else {'schema_version':'maauma.modules.v1','components':{}}
    if value.get('schema_version')!='maauma.modules.v1' or not isinstance(value.get('components'),dict):_error('module_state_invalid')
    return value


def preserved(path):
    if updates.preserved(path):return True
    folded=path.casefold();parts=folded.split('/')
    if folded=='local_backend.json':return True
    if len(parts)>1 and parts[0]=='pacer':
        return parts[1] in {'config','runs','logs','exports','review_exports','temp','updates'} or (
            len(parts)>2 and parts[1:3]==['backend','runs'] and not folded.startswith('pacer/backend/runs/support-card-library/'))
    return False


def _archive_inventory(archive):
    rows=archive.infolist()
    if len(rows)>updates.MAX_FILES or sum(row.file_size for row in rows)>updates.MAX_UNPACKED:_error('package_too_large')
    files={};folded=set()
    for row in rows:
        if row.is_dir():continue
        name=updates.relative_path(row.filename)
        if name.casefold() in folded or stat.S_ISLNK(row.external_attr>>16) or row.flag_bits&1:_error('duplicate_or_linked_archive_path')
        folded.add(name.casefold());files[name]=row
    if any('/'.join(name.split('/')[:i]).casefold() in folded for name in files for i in range(1,len(name.split('/')))):
        _error('file_directory_collision')
    return files


def _metadata(row):
    return (row.filename,row.header_offset,row.CRC,row.file_size,row.compress_size,row.compress_type,
            row.flag_bits,row.external_attr,row.create_system,row.extra,row.comment)


def inspect_component(archive_path,entry,*,trace_id,verify,progress=None):
    try:return _inspect_component(archive_path,entry,trace_id=trace_id,verify=verify,progress=progress)
    except BadZipFile:raise updates.UpdateError('invalid_component_archive') from None


def _inspect_component(archive_path,entry,*,trace_id,verify,progress=None):
    """Validate immutable base ZIP prefix, personalized directory and receipt.

    Signature verification is an authenticated service operation. Format checks
    and the local MU comparison do not substitute for verification.
    """
    if entry.get('component_id')!='pacer' or entry.get('product_id')!='uma-cut':_error('wrong_product')
    updates.version_parts(entry.get('version'));updates.version_parts(entry.get('base_version'))
    if not re.fullmatch('[0-9a-f]{64}',str(entry.get('sha256',''))):_error('invalid_package_digest')
    archive_path=Path(archive_path)
    prefix_size=entry.get('base_size_bytes',entry.get('size_bytes'))
    if type(prefix_size) is not int or not 0<prefix_size<=MAX_ARCHIVE:_error('invalid_package_size')
    if entry.get('integrity_scope')!='base_archive_prefix':_error('unsupported_component_integrity')
    if not prefix_size<archive_path.stat().st_size<=MAX_ARCHIVE:_error('package_integrity_failed')
    prefix=archive_path.with_name(archive_path.stem+'-base.zip')
    digest=hashlib.sha256();remaining=prefix_size
    if progress is not None:progress.verification('archive_integrity',0,prefix_size)
    with archive_path.open('rb') as source,prefix.open('wb') as output:
        while remaining:
            block=source.read(min(1024**2,remaining))
            if not block:_error('package_integrity_failed')
            digest.update(block);output.write(block);remaining-=len(block)
            if progress is not None:progress.verification('archive_integrity',prefix_size-remaining,prefix_size)
    if digest.hexdigest()!=entry['sha256']:_error('package_integrity_failed')
    with ZipFile(prefix) as base,ZipFile(archive_path) as final:
        original=_archive_inventory(base);personal=_archive_inventory(final)
        if set(personal)!=set(original)|{'distribution-receipt.json'}:_error('component_personalization_invalid')
        for name,row in original.items():
            if _metadata(row)!=_metadata(personal[name]):_error('component_payload_replaced')
        receipt_row=personal['distribution-receipt.json']
        if receipt_row.header_offset<prefix_size or receipt_row.file_size>16384:_error('component_personalization_invalid')
        if MANIFEST not in original:_error('component_manifest_missing')
        manifest=json.loads(base.read(MANIFEST).decode('utf-8-sig'))
        if (manifest.get('schema_version')!='maauma.component.v1'
                or any(manifest.get(key)!=entry.get(key) for key in ('component_id','product_id','version','base_version'))):
            _error('component_manifest_mismatch')
        files=manifest.get('files');hashes=manifest.get('file_hashes')
        if not isinstance(files,list) or not files or not isinstance(hashes,dict):_error('invalid_file_inventory')
        if len({updates.relative_path(name).casefold() for name in files})!=len(files):_error('duplicate_package_path')
        if set(files)|{MANIFEST}!=set(original) or set(hashes)!=set(files):_error('package_inventory_mismatch')
        if manifest.get('receipt_targets',[]) not in ([],['pacer/distribution-receipt.json']):_error('invalid_receipt_target')
        mutable=manifest.get('mutable_files',[])
        if (not isinstance(mutable,list) or len(set(mutable))!=len(mutable)
                or not set(mutable)<=MUTABLE or not set(mutable)<=set(files)):_error('invalid_mutable_file')
        verification_total=sum(original[name].file_size for name in files);verified=0
        if progress is not None:progress.verification('file_integrity',0,verification_total)
        for name in files:
            if name.casefold() in {n.casefold() for n in updates.MANIFESTS}|{STATE.casefold()} or name.casefold().startswith('updates/'):
                _error('reserved_component_path')
            if not re.fullmatch('[0-9a-f]{64}',str(hashes[name])):_error('invalid_package_digest')
            with base.open(name) as stream:
                if progress is None:file_hash=hashlib.file_digest(stream,'sha256').hexdigest()
                else:
                    file_digest=hashlib.sha256()
                    while block:=stream.read(1024**2):
                        file_digest.update(block);verified+=len(block)
                        progress.verification('file_integrity',verified,verification_total)
                    file_hash=file_digest.hexdigest()
                if file_hash!=hashes[name]:_error('package_integrity_failed')
        receipt=service.validate_receipt(json.loads(final.read('distribution-receipt.json').decode('utf8')),product='uma-cut',kind='package')
        if receipt['trace_id']!=trace_id or receipt['version']!=entry['version'] or receipt['base_sha256']!=entry['sha256']:
            _error('distribution_receipt_mismatch')
        if progress is not None:progress.verification('receipt',0,None)
        if verify(receipt) is not True:_error('distribution_receipt_unverified')
    return manifest,receipt


def _entry_url(root,entry):
    base=service.config(root)['base_url'];url=str(entry.get('url',''))
    if hasattr(service,'api_path'):return service.api_path(root,url)
    if url.startswith('/v1/'):return url
    if not url.startswith(base+'/v1/'):_error('service_origin_mismatch')
    return url[len(base):]


def _public_download_url(entry,product):
    if 'public_download_url' not in entry:return None
    if (product!='umaa-public' or entry.get('product_id')!='umaa-public'
            or entry.get('component_id') not in (None,'base')):_error('wrong_product')
    url=entry['public_download_url']
    if not isinstance(url,str):_error('invalid_public_download_url')
    # A literal origin and plain object path prevent credentials, ports, escaped
    # separators and query strings from changing the selected destination.
    prefix='https://'+PUBLIC_DOWNLOAD_HOST
    match=re.fullmatch(re.escape(prefix)+r'/public/([0-9A-Za-z.-]+)/([A-Za-z0-9][A-Za-z0-9._-]*\.zip)',url)
    if not match or match[1]!=entry.get('version'):_error('invalid_public_download_url')
    try:updates.version_parts(match[1])
    except updates.UpdateError:_error('invalid_public_download_url')
    return url


@observed_phase('public_cos_download')
def _anonymous_download(url):
    # Deliberately independent of service configuration, device identity and
    # authorization. No service header or grant crosses to the public bucket.
    request=Request(url,headers={'Accept':'application/octet-stream','User-Agent':'MaaUma-Suite/1'},method='GET')
    try:return build_opener(service.NoRedirect()).open(request,timeout=90)
    except (HTTPError,OSError,ValueError) as error:raise transport_failure(service.ServiceError('service_unavailable'),error) from None


def download(root,entry,target,*,product,public=False,progress=None,item_id=None):
    from desktop.update_progress import content_length,read_chunk
    if public and product!='umaa-public':_error('wrong_product')
    source=service.config(root)['download_source']
    if not public and product=='uma-cut' and source!='origin' and entry.get('direct_delivery_url'):
        from desktop import maauma_direct_delivery
        return maauma_direct_delivery.download(root,entry,target,progress=progress,item_id=item_id)
    public_url=_public_download_url(entry,product) if source!='origin' else None
    if source=='cos' and product=='umaa-public' and public_url is None:
        raise service.ServiceError('source_unavailable')
    # Only public base files can go directly to COS. An authorized extension
    # always stays on the signed service route, including an explicit source.
    attempts=[('cos',public_url)] if public_url is not None else [(source,None)]
    if public_url is not None and source=='auto':attempts.append(('origin',None))
    for index,(selected,url) in enumerate(attempts):
        if progress is not None:progress.start_item(item_id)
        size=0
        try:
            if url is not None:response=_anonymous_download(url)
            else:
                path=_entry_url(root,entry)
                response=(service.public_wire(root,path,binary=True,timeout=90,download_source=selected) if public else
                          service.wire(root,path,product=product,binary=True,timeout=90,download_source=selected))
            with response,Path(target).open('wb') as output:
                expected=content_length(response)
                if progress is not None:progress.connected(expected)
                while True:
                    try:block=read_chunk(response)
                    except IncompleteRead:raise
                    except OSError as error:raise transport_failure(service.ServiceError('service_unavailable'),error,phase='package_download') from None
                    if not block:break
                    size+=len(block)
                    if size>MAX_ARCHIVE:_error('package_too_large')
                    output.write(block)
                    if progress is not None:progress.received(len(block))
                if expected is not None and size!=expected:raise IncompleteRead(b'',max(0,expected-size))
        except (service.ServiceError,IncompleteRead) as error:
            if index+1<len(attempts):continue
            if isinstance(error,IncompleteRead):raise updates.UpdateError('package_download_incomplete') from None
            raise
        if not size:_error('package_integrity_failed')
        if progress is not None:progress.transferred()
        return


def _select_component(catalog,base_version):
    choices=[row for row in catalog.get('components',[]) if row.get('component_id')=='pacer'
             and row.get('product_id')=='uma-cut' and row.get('base_version')==base_version]
    if not choices:_error('compatible_pacer_required')
    chosen=choices[0]
    for row in choices[1:]:
        if updates.newer(row['version'],chosen['version']):chosen=row
    return chosen


def _base_entry(catalog):
    # The service may omit a base entry when the installed version is current.
    value=catalog.get('base_update') or catalog.get('base')
    if value is None:return None
    if value.get('product_id')!='umaa-public':_error('wrong_product')
    return {**value,**value.get('package',{})}



def _live_suite_helpers(root):
    """Read executable paths only; no command lines, tokens or process control."""
    if os.name != 'nt':return set()
    try:
        import ctypes
        from ctypes import wintypes as w
        class Entry(ctypes.Structure):
            _fields_=[('size',w.DWORD),('usage',w.DWORD),('pid',w.DWORD),('heap',ctypes.c_size_t),
                      ('module',w.DWORD),('threads',w.DWORD),('parent',w.DWORD),('priority',w.LONG),
                      ('flags',w.DWORD),('name',w.WCHAR*260)]
        k=ctypes.WinDLL('kernel32',use_last_error=True)
        k.CreateToolhelp32Snapshot.argtypes=[w.DWORD,w.DWORD];k.CreateToolhelp32Snapshot.restype=w.HANDLE
        k.Process32FirstW.argtypes=[w.HANDLE,ctypes.POINTER(Entry)]
        k.Process32NextW.argtypes=[w.HANDLE,ctypes.POINTER(Entry)]
        k.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];k.OpenProcess.restype=w.HANDLE
        k.CloseHandle.argtypes=[w.HANDLE]
        k.QueryFullProcessImageNameW.argtypes=[w.HANDLE,w.DWORD,w.LPWSTR,ctypes.POINTER(w.DWORD)]
        snapshot=k.CreateToolhelp32Snapshot(2,0)
        if snapshot==ctypes.c_void_p(-1).value:return None
        live=set();entry=Entry();entry.size=ctypes.sizeof(entry)
        try:
            ok=k.Process32FirstW(snapshot,ctypes.byref(entry))
            if not ok:return None
            while ok:
                if entry.name.lower() in {'python.exe','pythonw.exe'}:
                    process=k.OpenProcess(0x1000,False,entry.pid)
                    if not process:return None
                    if process:
                        try:
                            name=ctypes.create_unicode_buffer(32768);size=w.DWORD(len(name))
                            if k.QueryFullProcessImageNameW(process,0,name,ctypes.byref(size)):
                                path=Path(name.value).resolve()
                                if path.is_relative_to(root/'updates/suite-staged'):
                                    live.add(path.relative_to(root/'updates/suite-staged').parts[0])
                            else:return None
                        finally:k.CloseHandle(process)
                ok=k.Process32NextW(snapshot,ctypes.byref(entry))
                if not ok and ctypes.get_last_error()!=18:return None
        finally:k.CloseHandle(snapshot)
        return live
    except Exception:return None


def _stage_size(stage,deadline):
    info=stage.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:return None
    total=0;pending=[stage]
    while pending:
        if time.monotonic()>deadline:return None
        directory=pending.pop()
        with os.scandir(updates.transaction_io_path(directory)) as entries:
            for entry in entries:
                info=entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:return None
                if stat.S_ISDIR(info.st_mode):pending.append(Path(entry.path))
                elif stat.S_ISREG(info.st_mode):total+=info.st_size
                else:return None
    return total


def _download_owner_exited(pid):
    """False includes uncertainty; never authorize deletion from access denied."""
    if type(pid)is not int or pid<1:return False
    try:
        if os.name=='nt':
            import ctypes
            from ctypes import wintypes as w
            k=ctypes.WinDLL('kernel32',use_last_error=True)
            k.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];k.OpenProcess.restype=w.HANDLE
            k.CloseHandle.argtypes=[w.HANDLE];k.GetExitCodeProcess.argtypes=[w.HANDLE,ctypes.POINTER(w.DWORD)]
            handle=k.OpenProcess(0x1000,False,pid)
            if not handle:return ctypes.get_last_error()==87
            try:
                result=w.DWORD()
                return bool(k.GetExitCodeProcess(handle,ctypes.byref(result)))and result.value!=259
            finally:k.CloseHandle(handle)
        else:
            try:os.kill(pid,0)
            except ProcessLookupError:return True
            except OSError:return False
    except Exception:pass
    return False


def _unprepared_download_reason(root,stage,names,*,own_call=False):
    allowed={'download.json','download.json.tmp','ticket.json.tmp',
             'materialized','recipe-literals'} | set(_STAGED_ARCHIVE_MANIFESTS)
    if names-allowed:return 'prepared_or_unrecognized_content'
    try:
        info=(stage/'download.json').lstat()
        if stat.S_ISLNK(info.st_mode)or getattr(info,'st_file_attributes',0)&0x400:return 'linked_download_owner'
        marker=updates.read_json(stage/'download.json');pid=marker.get('owner_pid')
        if(marker.get('schema_version')!='maauma.suite_download.v1'or marker.get('stage_id')!=stage.name
                or type(pid)is not int or pid<1):return 'download_owner_unidentified'
        if own_call and pid==os.getpid():return None
        if not _download_owner_exited(pid):return 'download_owner_active_or_unknown'
        return None
    except Exception:return 'download_owner_unreadable'


def _discard_failed_preparation(root,stage):
    """Only this invocation's fresh stage, before ticket/backup/helper exist."""
    try:
        if stage.parent!=root/'updates/suite-staged'or not re.fullmatch('[0-9a-f]{32}',stage.name):return
        for part in(stage,stage.parent,root/'updates'):
            info=part.lstat()
            if stat.S_ISLNK(info.st_mode)or getattr(info,'st_file_attributes',0)&0x400:return
        names={p.name for p in stage.iterdir()}
        if _unprepared_download_reason(root,stage,names,own_call=True)is not None:return
        current=root/TRANSACTION
        if current.exists():
            info=current.lstat()
            if stat.S_ISLNK(info.st_mode)or getattr(info,'st_file_attributes',0)&0x400:return
        if current.is_file()and updates.read_json(current).get('ticket')==str(stage/'ticket.json'):return
        if _stage_size(stage,time.monotonic()+4)is None:return
        shutil.rmtree(updates.transaction_io_path(stage))
    except Exception:pass


def _legacy_download_version(stage,names):
    """Only finished, identifiable old ZIPs are disposable without a ticket."""
    allowed=set(_STAGED_ARCHIVE_MANIFESTS) | {'materialized','recipe-literals'}
    if not names or names-allowed:return None
    versions=[]
    for name in sorted(names & set(_STAGED_ARCHIVE_MANIFESTS)):
        try:
            with ZipFile(updates.transaction_io_path(stage/name)) as archive:
                member=_STAGED_ARCHIVE_MANIFESTS[name]
                item=archive.getinfo(member)
                if item.file_size>8*1024*1024:return None
                value=json.loads(archive.read(item))
                if not isinstance(value,dict):return None
                if not isinstance(value.get('schema_version'),str)or not isinstance(value.get('product_id'),str):return None
                if value.get('schema_version') not in {'umaa.public_package.v1','maauma.component.v1','maauma.file_patch_manifest.v1'}:return None
                if value.get('product_id') not in {'umaa-public','uma-cut'}:return None
                versions.append(updates.version_parts(value.get('base_version',value.get('version'))))
        except Exception:return None
    try:return max(versions) if versions else None
    except (TypeError,ValueError):return None


class _CacheMaintenanceBusy(updates.UpdateError):
    pass


@contextmanager
def _cache_maintenance_lock(root):
    """Serialize housekeeping without taking the GUI's lifetime application lock."""
    path=updates.contained(root,'updates/cache-maintenance.lock')
    path.parent.mkdir(parents=True,exist_ok=True)
    _clear_readonly(path)
    deadline=time.monotonic()+0.6
    stream=None
    while True:
        try:
            try:
                descriptor=os.open(path,os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600)
            except FileExistsError:stream=path.open('r+b')
            else:stream=os.fdopen(descriptor,'r+b')
            stream.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            break
        except OSError as error:
            if stream is not None:
                try:stream.close()
                except OSError:pass
                stream=None
            if error.errno in {errno.EACCES,errno.EAGAIN,errno.EDEADLK}or getattr(error,'winerror',None)in {5,32,33}:
                if time.monotonic()<deadline:
                    _clear_readonly(path)
                    time.sleep(0.05)
                    continue
                busy=_CacheMaintenanceBusy('cache_maintenance_busy')
                busy.errno=error.errno;busy.winerror=getattr(error,'winerror',None)
                raise busy from None
            raise
    with stream:
        try:
            stream.seek(0,os.SEEK_END)
            if stream.tell()==0:stream.write(b'0');stream.flush()
            yield
        finally:
            try:
                stream.seek(0)
                if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
            except OSError as error:
                error.cleanup_phase='release_maintenance_lock'
                raise


def _cleanup_failure(error,phase):
    code=str(error)if isinstance(error,updates.UpdateError)else ''
    if not re.fullmatch('[a-z][a-z0-9_]{0,79}',code):
        if isinstance(error,PermissionError):code='cleanup_access_denied'
        elif isinstance(error,FileNotFoundError):code='cleanup_path_missing'
        elif isinstance(error,json.JSONDecodeError):code='cleanup_invalid_json'
        elif isinstance(error,TypeError):code='cleanup_invalid_metadata'
        else:code='cache_cleanup_failed'
    fields={'error_code':code,'error_type':type(error).__name__,
            'phase':getattr(error,'cleanup_phase',phase)}
    for name in ('errno','winerror'):
        value=getattr(error,name,None)
        if type(value)is int:fields[name]=value
    return fields


def cleanup_staged_updates(root,*,dry_run=False,max_seconds=4.0):
    """Retire obsolete suite caches, preserving current rollback and live work.

    A previous successful stage or a provably obsolete, unapplied download is
    reclaimable. Unknown/corrupt stages remain for explicit review. Failure is
    housekeeping only and never changes authorization or an update outcome.
    """
    from contextlib import nullcontext
    outcome=dict(dry_run=dry_run,removed_stages=0,removed_bytes=0,candidates=[],protected=[],budget_exhausted=False,retry_pending=False,
                 byte_count_basis='logical_file_lengths',physical_bytes_released=None)
    phase='validate_application_root'
    try:
        supplied=Path(root).absolute()
        if(supplied/'.git').exists()or(supplied/'.worktrees').exists():return {**outcome,'status':'source_tree_not_a_client'}
        for path in (supplied,*supplied.parents):
            if path.exists():
                attributes=path.lstat()
                if stat.S_ISLNK(attributes.st_mode) or getattr(attributes,'st_file_attributes',0)&0x400:
                    return {**outcome,'status':'linked_application_root'}
        phase='read_installation_manifest'
        root,_,installed=updates.installation(root)
        staged=root/'updates/suite-staged';state_path=root/TRANSACTION
        if not staged.is_dir():return outcome
        for path in (root/'updates',staged,state_path):
            if not path.exists():continue
            attributes=path.lstat()
            if stat.S_ISLNK(attributes.st_mode) or getattr(attributes,'st_file_attributes',0)&0x400:
                return {**outcome,'status':'linked_update_state'}
        deadline=time.monotonic()+max_seconds
        phase='acquire_maintenance_lock'
        with nullcontext() if dry_run else _cache_maintenance_lock(root):
            keep=None;history_allowed=False
            phase='read_current_state'
            if state_path.is_file():
                current=updates.read_json(state_path)
                if current.get('phase')!='installed' or current.get('base_version')!=installed['version']:return outcome
                phase='read_current_ticket'
                _,keep,ticket=_ticket(root,current.get('ticket'))
                phase='read_current_journal'
                journal=updates.read_json(keep/'journal.json')
                if journal.get('phase')!='installed' or ticket.get('base_version')!=installed['version']:return outcome
                history_allowed=True
            phase='check_live_helpers'
            live=_live_suite_helpers(root)
            if live is None:return {**outcome,'status':'process_check_unavailable'}
            phase='snapshot_transaction_state'
            state_info=state_path.stat()if state_path.is_file()else None
            state_token=(state_info.st_dev,state_info.st_ino,state_info.st_size,state_info.st_mtime_ns)if state_info else None
            owned={'ticket.json','journal.json','helper','backup','incoming','retired',
                   'materialized','recipe-literals','download.json','update.log'} | set(_STAGED_ARCHIVE_MANIFESTS)
            phase='enumerate_stages'
            for path in sorted(staged.iterdir()):
                phase='inspect_stage'
                if time.monotonic()>deadline:outcome['budget_exhausted']=True;break
                if not path.is_dir() or not re.fullmatch('[0-9a-f]{32}',path.name):continue
                reason=None
                if keep is not None and path.name==keep.name:reason='current_rollback'
                elif path.name in live:reason='live_helper'
                if reason:outcome['protected'].append({'stage':path.name,'reason':reason});continue
                try:size=_stage_size(path,deadline)
                except OSError:size=None
                if size is None:
                    outcome['protected'].append({'stage':path.name,'reason':'linked_or_unreadable_stage'});continue
                names={p.name for p in path.iterdir()}
                if names-owned:reason='unrecognized_stage_content'
                elif 'ticket.json' in names:
                    try:
                        if not history_allowed:
                            outcome['protected'].append({'stage':path.name,'reason':'no_installed_history_guard'});continue
                        _,_,old=_ticket(root,path/'ticket.json')
                        target=updates.version_parts(old.get('base_version'))
                        source=updates.version_parts(old.get('from_version'))
                        version=updates.version_parts(installed['version'])
                        saved=updates.read_json(path/'journal.json') if 'journal.json'in names else None
                        if saved is not None and (saved.get('schema_version')!='maauma.suite_transaction.v1' or saved.get('phase') not in {'installed','rolled_back'}):
                            reason='unfinished_or_failed_transaction'
                        elif target>version or saved is None and source>=version:reason='current_or_future_prepared_update'
                        elif saved is None and names & {'backup','incoming','retired'}:reason='uncommitted_program_backup'
                    except (OSError,ValueError,TypeError,KeyError,updates.UpdateError):reason='unreadable_stage_metadata'
                else:
                    if 'download.json'in names:reason=_unprepared_download_reason(root,path,names)
                    elif not history_allowed:reason='download_owner_unidentified'
                    else:
                        try:
                            target=_legacy_download_version(path,names)
                            if target is None or target>=updates.version_parts(installed['version']):reason='unidentified_or_current_download'
                        except (TypeError,ValueError):reason='unidentified_or_current_download'
                if reason:outcome['protected'].append({'stage':path.name,'reason':reason});continue
                outcome['candidates'].append({'stage':path.name,'bytes':size})
                if dry_run:continue
                phase='recheck_transaction_state'
                info=state_path.stat()if state_path.is_file()else None
                current_token=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)if info else None
                if current_token!=state_token:break
                try:
                    phase='remove_stage'
                    shutil.rmtree(updates.transaction_io_path(path))
                    outcome['removed_stages']+=1;outcome['removed_bytes']+=size
                except OSError:outcome['protected'].append({'stage':path.name,'reason':'remove_failed'})
    except _CacheMaintenanceBusy as error:
        outcome.update(status='cleanup_busy',retry_pending=False,skipped_busy=True,**_cleanup_failure(error,phase))
    except Exception as error:
        outcome.update(status='cleanup_unavailable',retry_pending=False,**_cleanup_failure(error,phase))
    return outcome


def prepare(root,*,mode='pacer',progress=None):
    if progress is not None:progress.stage('checking')
    if mode not in {'pacer','update'}:_error('invalid_update_mode')
    root,name,installed=updates.installation(root)
    if installed['product_id']!='umaa-public':_error('wrong_product')
    cleanup_staged_updates(root)
    current=state(root);has_pacer='pacer' in current['components']
    private_required=mode=='pacer' or has_pacer
    auth=None;warning=None
    if private_required:
        auth=service.credentials(root,'uma-cut')
        catalog=service.catalog(root)
    else:
        catalog=service.public_catalog(root)
    base_entry=_base_entry(catalog) if mode=='update' else None
    if base_entry and not updates.newer(base_entry.get('version'),installed['version']):base_entry=None
    target_version=base_entry['version'] if base_entry else installed['version']
    if private_required and base_entry:catalog=service.catalog(root,base_version=target_version)
    # An activation may have succeeded while its automatic first download
    # failed. The ordinary update action is also the visible retry path, so use
    # the already opened grant instead of requiring a previously installed module.
    component_entry=_select_component(catalog,target_version) if private_required else None
    if not private_required and updates.contained(root,service.AUTH).is_file():
        try:
            auth=service.credentials(root)
            if 'uma-cut' in auth.get('products',[]):
                service.validate_receipt(auth.get('distribution_receipt'),kind='activation')
                component_entry=_select_component(service.catalog(root,base_version=target_version),target_version)
        except (service.ServiceError,updates.UpdateError,OSError,ValueError) as error:
            warning={'status':'failed','code':str(error) if isinstance(error,(service.ServiceError,updates.UpdateError))
                     else 'pacer_prepare_failed'}
    context={'public_base':not private_required}
    if warning:context['pacer_warning']=warning
    if mode=='update' and not base_entry and component_entry and has_pacer:
        installed_component=current['components']['pacer']
        if (installed_component.get('base_version')==target_version
                and not updates.newer(component_entry['version'],installed_component.get('version'))):
            return {'ok':True,'status':'current','version':installed['version'],'components':['pacer'],**context}
    if not base_entry and not component_entry:return {'ok':True,'status':'current','version':installed['version'],**context}
    if progress is not None:
        progress.set_items(([('base',base_entry.get('size_bytes'))] if base_entry else [])+
                           ([('authorized_extension',None)] if component_entry else []))
    stage=root/'updates/suite-staged'/uuid.uuid4().hex;stage.mkdir(parents=True)
    updates.write_json(stage/'download.json',{'schema_version':'maauma.suite_download.v1',
        'stage_id':stage.name,'owner_pid':os.getpid()})
    ticket={'schema_version':'maauma.prepared_suite.v1','from_version':installed['version'],'base_version':target_version,
            'base_manifest':name,'trace_id':auth['distribution_receipt']['trace_id'] if component_entry else None,'entries':[]}
    from desktop import maauma_file_patches as patches
    plans=[];prepared=False
    try:
        if base_entry:
            row,plan=patches.prepare_transfer(root,stage,kind='base',full=base_entry,
                source_version=installed['version'],trace_id=None,public=not private_required,progress=progress)
            if plan['manifest_name']!=name:_error('manifest_name_changed')
            ticket['entries'].append(row);plans.append(plan)
            if progress is not None:progress.complete_item()
        if component_entry:
            try:
                previous_component=current['components'].get('pacer',{})
                row,plan=patches.prepare_transfer(root,stage,kind='pacer',full=component_entry,
                    source_version=previous_component.get('version'),trace_id=ticket['trace_id'],public=False,progress=progress)
                ticket['entries'].append(row);plans.append(plan)
                if progress is not None:progress.complete_item()
            except (service.ServiceError,updates.UpdateError,OSError,ValueError) as error:
                if progress is not None:progress.fail_item()
                if private_required:raise
                context['pacer_warning']={'status':'failed','code':str(error)
                    if isinstance(error,(service.ServiceError,updates.UpdateError)) else 'pacer_prepare_failed'}
                component_entry=None;ticket['trace_id']=None
        while True:
            try:
                patches.validate_reuse(root,patches.effective(plans),progress=progress)
                break
            except patches.LocalMismatch as error:
                kind=error.owner
                index=next(i for i,row in enumerate(ticket['entries']) if row['kind']==kind)
                previous_row=ticket['entries'][index]
                if previous_row.get('delivery')!='file_patch':raise
                patches.close([plans[index]])
                row,plan=patches.prepare_transfer(root,stage,kind=kind,full=previous_row['entry'],
                    source_version=previous_row['from_version'],trace_id=ticket['trace_id'],
                    public=kind=='base' and not private_required,progress=progress,
                    force_full=True,fallback_reason='local_state_mismatch')
                ticket['entries'][index]=row;plans[index]=plan
                if progress is not None:progress.complete_item()
        if not ticket['entries']:return {'ok':True,'status':'current','version':installed['version'],**context}
        if progress is not None:progress.stage('preparing')
        updates.write_json(stage/'ticket.json',ticket)
        prepared=True
        return {'ok':True,'status':'prepared','ticket':str(stage/'ticket.json'),'version':target_version,
                'components':['pacer'] if component_entry else [],'requires_exit':True,**context}
    finally:
        try:patches.close(plans)
        finally:
            if not prepared:_discard_failed_preparation(root,stage)


def _ticket(root,path):
    root=Path(root).resolve(strict=True);path=Path(path).resolve(strict=True)
    if (path.name!='ticket.json' or path.parent.parent!=root/'updates/suite-staged'
            or not re.fullmatch('[0-9a-f]{32}',path.parent.name)):_error('invalid_update_ticket_path')
    updates.contained(root,path.relative_to(root).as_posix())
    ticket=updates.read_json(path)
    if ticket.get('schema_version')!='maauma.prepared_suite.v1':_error('invalid_update_ticket')
    return root,path.parent,ticket


def _assert_idle(root):
    updates.assert_device_idle(root)
    # All suite task families use existing one-byte OS lock files. Checking every
    # existing lease covers background jobs after their parent GUI has exited.
    folders=[root/'backend/runs/device-occupancy',root/'pacer/backend/runs/device-occupancy']
    local=os.environ.get('LOCALAPPDATA')
    if local:folders.append(Path(local)/'UmaSuite/device-locks')
    for folder in folders:
        for path in folder.glob('*.lock'):
            try:
                with path.open('r+b') as stream:
                    stream.seek(0)
                    if os.name=='nt':
                        import msvcrt
                        msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1);stream.seek(0)
                        msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB);fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
            except OSError:_error('device_busy')


def last_update_result(root):
    try:
        path=updates.contained(Path(root).resolve(),LAST_RESULT)
        return updates.read_json(path) if path.is_file() else None
    except Exception:
        return None


def retryable_ticket(root):
    try:
        resolved=Path(root).resolve(strict=True)
        tx_path=resolved/TRANSACTION
        if not tx_path.is_file():return None
        tx=updates.read_json(tx_path)
        if tx.get('phase')!='rolled_back' or not tx.get('ticket'):return None
        resolved,stage,ticket=_ticket(resolved,tx['ticket'])
        _,manifest_name,installed=updates.installation(resolved)
        if installed.get('version')!=ticket.get('from_version'):return None
        entries=ticket.get('entries')
        if not isinstance(entries,list) or not entries:return None
        previous=state(resolved)
        if 'pacer' in previous.get('components',{}) and not any(row.get('kind')=='pacer' for row in entries):
            return None
        for row in entries:
            kind=row.get('kind')
            if kind not in {'base','pacer'}:return None
            file_path=updates.contained(stage,updates.relative_path(row['file']))
            if not file_path.is_file():return None
            delivery=row.get('delivery')
            if delivery is None:
                entry=row['entry']
                if kind=='pacer':
                    if file_path.stat().st_size!=row['personal_size_bytes'] or _digest(file_path)!=row['personal_sha256']:
                        return None
                else:
                    if file_path.stat().st_size!=entry['size_bytes'] or _digest(file_path)!=entry['sha256']:
                        return None
            elif delivery=='file_patch':
                patch_entry=row['patch_entry']
                expected_size=patch_entry['size_bytes']
                expected_sha=patch_entry['sha256']
                actual_size=file_path.stat().st_size
                if kind=='pacer':
                    if not (expected_size<actual_size<=MAX_ARCHIVE):return None
                    digest=hashlib.sha256();remaining=expected_size
                    with file_path.open('rb') as stream:
                        while remaining:
                            block=stream.read(min(1024**2,remaining))
                            if not block:return None
                            digest.update(block);remaining-=len(block)
                    if digest.hexdigest()!=expected_sha:return None
                else:
                    if actual_size!=expected_size or _digest(file_path)!=expected_sha:
                        return None
            else:
                return None
        return str(stage/'ticket.json')
    except Exception:
        return None


def record_apply_result(root,request,response):
    root_path=Path(root).resolve()
    ticket_str=str(response.get('ticket') or (request.get('ticket') if isinstance(request,dict) else None) or '') or None
    existing=last_update_result(root_path)
    if (isinstance(existing,dict) and ticket_str and existing.get('ticket')==ticket_str
            and existing.get('ok')==bool(response.get('ok'))
            and existing.get('code')==response.get('code')
            and (existing.get('error') or response.get('ok') is True)):
        return existing
    log_path=response.get('log_path')
    if log_path is None and ticket_str:
        try:
            _,stage,_=_ticket(root_path,ticket_str)
            if (stage/'update.log').is_file():log_path=str(stage/'update.log')
        except Exception:
            pass
    payload={
        'schema_version':'maauma.update_result.v1',
        'ok':bool(response.get('ok')),
        'status':response.get('status') or ('installed' if response.get('ok') else 'failed'),
        'code':response.get('code'),
        'version':response.get('version'),
        'ticket':ticket_str,
        'failed_path':response.get('failed_path') or ((response.get('error') or {}).get('failed_path') if isinstance(response.get('error'),dict) else None),
        'error':response.get('error'),
        'log_path':log_path,
        'timestamp_utc':_utc_now(),
    }
    _write_last_result(root_path,payload)
    return payload


def pending_rollback_ticket(root):
    try:
        resolved=Path(root).resolve(strict=True)
        tx_path=resolved/TRANSACTION
        if not tx_path.is_file():return None
        tx=updates.read_json(tx_path)
        if tx.get('phase') not in {'applying','rollback_failed'} or not tx.get('ticket'):return None
        resolved,stage,_ticket_data=_ticket(resolved,tx['ticket'])
        if not (stage/'journal.json').is_file():return None
        return str(stage/'ticket.json')
    except Exception:
        return None


def _restore(root,stage,journal,*,attempted_count=None,error=None,log=None):
    ops=journal['operations'][:attempted_count] if attempted_count is not None else journal['operations']
    for item in reversed(ops):
        name=updates.relative_path(item['path']);target=updates.contained(root,name)
        if item['existed']:
            backup=updates.transaction_io_path(updates.contained(stage/'backup',name))
            if not backup.is_file():_error('rollback_backup_missing')
            try:
                if target.is_file() and target.stat().st_size==backup.stat().st_size and _digest(target)==_digest(backup):
                    if log:log(f'rollback skipped unchanged file {name}')
                    continue
            except OSError:
                pass
            target.parent.mkdir(parents=True,exist_ok=True)
            tmp=updates.transaction_io_path(target.with_name(f'.{target.name}.{uuid.uuid4().hex[:8]}.restore'))
            shutil.copy2(backup,tmp)
            try:_replace_with_retry(tmp,target,log=log)
            except Exception:
                try:tmp.unlink(missing_ok=True)
                except OSError:pass
                raise
        elif target.is_file():
            retired=updates.transaction_io_path(updates.contained(stage/'retired',name));retired.parent.mkdir(parents=True,exist_ok=True)
            _replace_with_retry(target,retired,log=log)
    journal['phase']='rolled_back'
    if error is not None:journal['error']=error
    updates.write_json(stage/'journal.json',journal)
    tx={'phase':'rolled_back','ticket':str(stage/'ticket.json')}
    if error is not None:tx['error']=error
    updates.write_json(root/TRANSACTION,tx)
    if log:log(f'rollback completed phase=rolled_back failed_path={error.get("failed_path") if isinstance(error,dict) else None}')


def apply_prepared(root,ticket_path,*,parent_pid=None):
    root,stage,ticket=_ticket(root,ticket_path)
    log=_stage_logger(stage)
    log(f'apply_prepared start ticket={stage/"ticket.json"} parent_pid={parent_pid} base_version={ticket.get("base_version")}')
    if parent_pid is not None:
        log(f'waiting for parent_pid={parent_pid} exit')
        try:updates.wait_for_exit(int(parent_pid))
        except Exception as exc:
            log(f'wait_for_exit failed: {type(exc).__name__}: {exc}')
            if isinstance(exc,updates.UpdateError):
                exc.ticket=str(stage/'ticket.json');exc.log_path=str(stage/'update.log')
            raise
    with updates.installation_lock(root):
        log('acquired installation_lock')
        try:
            _assert_idle(root)
            log('device occupancy idle confirmed')
            _terminate_installation_pacer_processes(root,log=log)
            transaction=updates.read_json(root/TRANSACTION) if (root/TRANSACTION).is_file() else {}
            if transaction.get('phase') in {'applying','rollback_failed'}:_error('rollback_required')
            _,manifest_name,installed=updates.installation(root);previous=state(root)
            if installed['version']!=ticket.get('from_version'):_error('stale_update_ticket')
            journal_path=stage/'journal.json'
            if journal_path.exists() and updates.read_json(journal_path).get('phase') in {'applying','rollback_failed'}:_error('rollback_required')
            entries=ticket.get('entries')
            if not isinstance(entries,list) or not entries:_error('invalid_update_ticket')
            if 'pacer' in previous['components'] and not any(row.get('kind')=='pacer' for row in entries):_error('compatible_pacer_required')
        except updates.UpdateError as exc:
            log(f'preflight rejected: {exc}')
            exc.ticket=str(stage/'ticket.json');exc.log_path=str(stage/'update.log')
            raise
        old_managed=set(installed['files'])|{manifest_name}|set(previous.get('managed_files',[]))
        incoming={};mutable_payloads={};base_manifest=installed;component_record=previous['components'].get('pacer')
        from desktop import maauma_file_patches as patches
        archives=[];plans=[]
        try:
            for row in entries:
                path=updates.contained(stage,updates.relative_path(row['file']));entry=row['entry']
                if row['kind'] not in {'base','pacer'}:_error('invalid_update_ticket')
                if row['kind']=='pacer':
                    auth=service.credentials(root,'uma-cut')
                    if auth['distribution_receipt']['trace_id']!=ticket['trace_id']:_error('distribution_receipt_mismatch')
                    if entry['base_version']!=ticket['base_version']:_error('component_base_mismatch')
                    # A file patch checks this same personal ZIP in patch_plan below.
                    # Keep one complete hash at that guard; full components keep this guard.
                    if row.get('delivery')!='file_patch' and (path.stat().st_size!=row['personal_size_bytes'] or _digest(path)!=row['personal_sha256']):_error('package_integrity_failed')
                if row.get('delivery')=='file_patch':
                    if row['kind']=='base' and _digest(root/manifest_name)!=row['patch_entry']['from_manifest_sha256']:
                        patches.mark_full(root,'base',row['from_version'],entry)
                        _error('incremental_source_changed')
                    plan=patches.patch_plan(root,stage,row,verify=lambda value:value==row.get('receipt'),
                        personal_integrity_error='package_integrity_failed')
                elif row.get('delivery') is None:
                    plan=patches.full_plan(path,entry,kind=row['kind'],trace_id=ticket['trace_id'],
                                           verify=lambda value:value==row.get('receipt'))
                else:_error('invalid_update_ticket')
                plans.append(plan);archives.extend(plan['archives'])
                if row['kind']=='base':
                    base_manifest=plan['manifest']
                    if plan['manifest_name']!=manifest_name or base_manifest['version']!=ticket['base_version']:_error('component_base_mismatch')
                elif row['kind']=='pacer':
                    manifest=plan['manifest'];receipt=plan['receipt']
                    mutable=set(manifest.get('mutable_files',[]))
                    mutable_payloads.update(plan['mutable_payloads'])
                    component_record={'product_id':'uma-cut','version':manifest['version'],'base_version':manifest['base_version'],
                                      'receipt':receipt,'managed_files':[n for n in manifest['files'] if not preserved(n) and n not in mutable],
                                      'receipt_targets':manifest.get('receipt_targets',[]),'mutable_files':sorted(mutable),
                                      'manifest_sha256':row['patch_entry']['target_manifest_sha256'] if row.get('delivery')=='file_patch' else None}
            if base_manifest['version']!=ticket['base_version']:_error('component_base_mismatch')
            effective=patches.effective(plans)
            log('validating incremental reuse')
            try:patches.validate_reuse(root,effective)
            except patches.LocalMismatch as error:
                for row in entries:
                    if row.get('delivery')=='file_patch' and row['kind']==error.owner:
                        patches.mark_full(root,row['kind'],row['from_version'],row['entry'])
                _error('incremental_source_changed')
            for name,value in effective.items():
                if value['source'] is None:continue
                target=updates.contained(root,name)
                if (value['sha256'] is None and name not in old_managed and target.is_file() and
                        target.stat().st_size==value['size_bytes']):
                    # Full base manifests do not carry per-file hashes. A bridge
                    # may already have installed a newly managed updater module;
                    # compare only this unowned candidate with its authenticated
                    # full-ZIP member instead of rejecting an identical file.
                    _,source_archive,source_member=value['source']
                    with source_archive.open(source_member) as source:
                        value['sha256']=hashlib.file_digest(source,'sha256').hexdigest()
                if (value['sha256'] and target.is_file() and target.stat().st_size==value['size_bytes'] and
                        _digest(target)==value['sha256']):
                    continue
                incoming[name]=value['source']
            new_managed=set(base_manifest['files'])|{manifest_name}
            if component_record:new_managed.update(component_record['managed_files'])
            changes=sorted((set(incoming)|(old_managed-new_managed))-{STATE})
            changes=[name for name in changes if not preserved(name) and name not in MUTABLE]
            new_state={'schema_version':'maauma.modules.v1','base_version':ticket['base_version'],
                       'components':{'pacer':component_record} if component_record else {},
                       'managed_files':component_record['managed_files'] if component_record else []}
            generated={STATE:(json.dumps(new_state,ensure_ascii=False,indent=2)+'\n').encode('utf8')}
            if component_record:
                for name in component_record.get('receipt_targets',[]):
                    generated[name]=(json.dumps(component_record['receipt'],ensure_ascii=False,indent=2)+'\n').encode('utf8')
                connection_path=root/'config/public-connection.json'
                connection=updates.read_json(connection_path) if connection_path.is_file() else {}
                selected=connection.get('endpoint');adb=connection.get('adb_path','')
                if selected:
                    match=re.fullmatch(r'127\.0\.0\.1:([0-9]{1,5})',str(selected))
                    if not match or not 1<=int(match[1])<=65535:_error('invalid_saved_connection')
                # Mutable defaults are validated payloads but never overwrite
                # an existing user-selected connection, even across updates.
                for name,raw in mutable_payloads.items():
                    if (root/name).exists():continue
                    cfg=json.loads(raw.decode('utf-8-sig'))
                    if not isinstance(cfg,dict):_error('invalid_mutable_defaults')
                    if selected:
                        if name=='local_backend.json':cfg.update(endpoint=selected,adb_path=adb)
                        else:cfg.update(adb_port=int(match[1]),adb_path=adb)
                    generated[name]=(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n').encode('utf8')
                if not (root/'local_backend.json').exists() and 'local_backend.json' not in generated:
                    cfg=dict(portable=True,python='runtime/python.exe',repo='backend',worker='backend/scripts/mfa_worker.py',
                        skill_database='backend/data/skill_database.json',tesseract_dir='ocr',tessdata='ocr/tessdata',
                        pacer_repo='pacer/backend',pacer_python='pacer/runtime/python.exe',endpoint=selected or '127.0.0.1:16416',adb_path=adb)
                    generated['local_backend.json']=(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n').encode('utf8')
                book=root/'backend/runs/mfa-accounts.json'
                if not book.exists():generated['backend/runs/mfa-accounts.json']=b'{"next_number":1,"accounts":[]}\n'
            changes+=list(generated)
            log(f'backing up {len(changes)} operations')
            journal={'schema_version':'maauma.suite_transaction.v1','phase':'preparing','operations':[]}
            for name in changes:
                target=updates.contained(root,name)
                if target.exists() and name not in old_managed and name not in generated:_error('unmanaged_file_collision')
                if target.exists() and not target.is_file():_error('managed_path_not_file')
                item={'path':name,'existed':target.is_file()}
                if item['existed']:
                    backup=updates.transaction_io_path(updates.contained(stage/'backup',name));backup.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copy2(target,backup)
                journal['operations'].append(item)
            journal['phase']='applying';updates.write_json(journal_path,journal)
            updates.write_json(root/TRANSACTION,{'phase':'applying','ticket':str(stage/'ticket.json')})
            log(f'applying {len(journal["operations"])} operations')
            current_path=None
            attempted_count=0
            try:
                for item in journal['operations']:
                    attempted_count+=1
                    name=item['path'];current_path=name;target=updates.contained(root,name)
                    _clear_readonly(target)
                    if name in incoming or name in generated:
                        temporary=updates.transaction_io_path(updates.contained(stage/'incoming',name));temporary.parent.mkdir(parents=True,exist_ok=True)
                        if name in generated:temporary.write_bytes(generated[name])
                        else:
                            incoming_source=incoming[name]
                            if incoming_source[0]=='file':shutil.copy2(incoming_source[1],temporary)
                            else:
                                _,archive,member=incoming_source
                                with archive.open(member) as source,temporary.open('wb') as output:shutil.copyfileobj(source,output)
                        target.parent.mkdir(parents=True,exist_ok=True);_replace_with_retry(temporary,target,log=log)
                    elif target.exists() and name not in new_managed:
                        retired=updates.transaction_io_path(updates.contained(stage/'retired',name));retired.parent.mkdir(parents=True,exist_ok=True);_replace_with_retry(target,retired,log=log)
                journal['phase']='installed';updates.write_json(journal_path,journal)
                updates.write_json(root/TRANSACTION,{'phase':'installed','ticket':str(stage/'ticket.json'),
                    'base_version':ticket['base_version']})
                log(f'apply_prepared installed version={ticket["base_version"]}')
                _write_last_result(root,{
                    'schema_version':'maauma.update_result.v1',
                    'ok':True,
                    'status':'installed',
                    'code':'installed',
                    'version':ticket['base_version'],
                    'components':list(new_state['components']),
                    'ticket':str(stage/'ticket.json'),
                    'failed_path':None,
                    'error':None,
                    'log_path':str(stage/'update.log'),
                    'timestamp_utc':_utc_now(),
                })
            except Exception as exc:
                error_info={
                    'type':type(exc).__name__,
                    'message':str(exc)[:500],
                    'errno':getattr(exc,'errno',None),
                    'winerror':getattr(exc,'winerror',None),
                    'failed_path':current_path,
                    'traceback':traceback.format_exc()[-4000:],
                }
                log(f'apply failed at path={current_path}: {error_info["type"]}: {error_info["message"]} (errno={error_info["errno"]}, winerror={error_info["winerror"]})')
                try:_restore(root,stage,journal,attempted_count=attempted_count,error=error_info,log=log)
                except Exception as rb_exc:
                    log(f'rollback failed: {type(rb_exc).__name__}: {rb_exc}')
                    journal['phase']='rollback_failed';journal['error']=error_info
                    journal['rollback_error']=f'{type(rb_exc).__name__}: {rb_exc}'
                    updates.write_json(journal_path,journal)
                    updates.write_json(root/TRANSACTION,{'phase':'rollback_failed','ticket':str(stage/'ticket.json'),'error':error_info})
                    _write_last_result(root,{
                        'schema_version':'maauma.update_result.v1',
                        'ok':False,
                        'status':'rollback_failed',
                        'code':'rollback_required',
                        'version':ticket.get('base_version'),
                        'ticket':str(stage/'ticket.json'),
                        'failed_path':current_path,
                        'error':error_info,
                        'log_path':str(stage/'update.log'),
                        'timestamp_utc':_utc_now(),
                    })
                    err=updates.UpdateError('rollback_required')
                    err.error_info=error_info;err.failed_path=current_path
                    err.ticket=str(stage/'ticket.json');err.log_path=str(stage/'update.log')
                    raise err from rb_exc
                _write_last_result(root,{
                    'schema_version':'maauma.update_result.v1',
                    'ok':False,
                    'status':'rolled_back',
                    'code':'install_failed_rolled_back',
                    'version':ticket.get('base_version'),
                    'ticket':str(stage/'ticket.json'),
                    'failed_path':current_path,
                    'error':error_info,
                    'log_path':str(stage/'update.log'),
                    'timestamp_utc':_utc_now(),
                })
                err=updates.UpdateError('install_failed_rolled_back')
                err.error_info=error_info;err.failed_path=current_path
                err.ticket=str(stage/'ticket.json');err.log_path=str(stage/'update.log')
                raise err from exc
        finally:
            for archive in archives:archive.close()
    return {'ok':True,'status':'installed','version':ticket['base_version'],'components':list(new_state['components']),
            'cache_cleanup':cleanup_staged_updates(root)}


def rollback(root,ticket_path,*,parent_pid=None):
    root,stage,ticket=_ticket(root,ticket_path)
    if parent_pid is not None:updates.wait_for_exit(int(parent_pid))
    with updates.installation_lock(root):
        _assert_idle(root);journal=updates.read_json(stage/'journal.json')
        transaction=updates.read_json(root/TRANSACTION) if (root/TRANSACTION).is_file() else {}
        if transaction.get('ticket')!=str(stage/'ticket.json'):_error('stale_rollback_ticket')
        if journal.get('phase') not in {'installed','applying','rollback_failed'}:_error('stale_rollback_ticket')
        _restore(root,stage,journal)
    return {'ok':True,'status':'rolled_back'}


def launch_install(root,ticket_path,*,parent_pid=None):
    root,stage,ticket=_ticket(root,ticket_path)
    if type(parent_pid) is not int or parent_pid<=0:_error('application_pid_required')
    # Check before telling the UI to close. apply_prepared checks again after
    # the parent exits, covering a task that starts during that interval.
    _assert_idle(root)
    helper=stage/'helper'
    if helper.exists():
        journal_file=stage/'journal.json'
        journal_phase=updates.read_json(journal_file).get('phase') if journal_file.is_file() else None
        live=_live_suite_helpers(root)
        if journal_phase in {'rolled_back','rollback_failed'} and live is not None and stage.name not in live:
            shutil.rmtree(helper)
        else:
            _error('installer_already_prepared')
    # Installer/service code is standard-library-only. The embedded interpreter
    # keeps its stdlib ZIP and native DLL/PYD files at runtime's top level; game
    # OCR/scientific site-packages are unused here and can exceed Windows path
    # limits after adding a transaction/helper prefix.
    isolated_runtime=helper/'runtime';isolated_runtime.mkdir(parents=True)
    for source in (root/'runtime').iterdir():
        if source.is_file() and source.suffix not in {'.pyc','.pyo'}:
            shutil.copy2(source,isolated_runtime/source.name)
    for module in ('desktop/maauma_service.py','desktop/maauma_components.py','desktop/product_updates.py',
                   'desktop/maauma_file_patches.py','desktop/maauma_direct_delivery.py','desktop/service_transport.py',
                   'desktop/zip_recipe.py','desktop/update_progress.py','desktop/update_connectivity.py',
                   'desktop/__init__.py','scripts/maauma_service.py'):
        source=Path(__file__).resolve().parents[1]/module;target=helper/'backend'/module
        target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
    # The source tree uses a namespace package for scripts; an isolated helper
    # supplies its own empty package initializer instead of copying a missing file.
    (helper/'backend/scripts/__init__.py').write_text('',encoding='utf8')
    (helper/'runtime/python311._pth').write_text('python311.zip\n.\nLib/site-packages\n../backend\nimport site\n',encoding='utf8')
    request=helper/'request.json';updates.write_json(request,{'action':'apply','ticket':str(stage/'ticket.json'),
                                                            'parent_pid':parent_pid,'restart':True})
    arguments=['-B','-X','utf8',str(helper/'backend/scripts/maauma_service.py'),
               '--root',str(root),'--request-file',str(request)]
    return {'ok':True,'status':'installer_ready','helper_executable':str(helper/'runtime/python.exe'),
            'helper_arguments':arguments,'helper_working_directory':str(helper),
            'requires_exit':True,'ticket':str(stage/'ticket.json')}
