"""Small public updater-only bridge for 0.4.10/0.4.11/0.4.12; keeps product version."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from desktop import product_updates as updates
from desktop import maauma_components as components

SCHEMA='maauma.updater_bootstrap.v1'
SUPPORTED=frozenset({'0.4.10','0.4.11','0.4.12'})
ALLOWLIST=frozenset('backend/'+name for name in (
    'desktop/__init__.py','desktop/maauma_service.py','desktop/maauma_components.py',
    'desktop/product_updates.py','desktop/update_progress.py','desktop/maauma_file_patches.py',
    'desktop/zip_recipe.py','desktop/maauma_updater_bootstrap.py',
    'scripts/maauma_service.py','scripts/maauma_updater_bootstrap.py'))


def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def gui_pids(root):
    """Find only this install's GUI. Never terminate any process."""
    if os.name!='nt':return []
    from ctypes import wintypes
    class Entry(ctypes.Structure):
        _fields_=[('dwSize',wintypes.DWORD),('cntUsage',wintypes.DWORD),('th32ProcessID',wintypes.DWORD),
                  ('th32DefaultHeapID',ctypes.c_size_t),('th32ModuleID',wintypes.DWORD),
                  ('cntThreads',wintypes.DWORD),('th32ParentProcessID',wintypes.DWORD),
                  ('pcPriClassBase',wintypes.LONG),('dwFlags',wintypes.DWORD),('szExeFile',wintypes.WCHAR*260)]
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
    api.OpenProcess.restype=wintypes.HANDLE
    api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    api.CloseHandle.argtypes=[wintypes.HANDLE]
    api.Process32FirstW.argtypes=[wintypes.HANDLE,ctypes.POINTER(Entry)]
    api.Process32NextW.argtypes=[wintypes.HANDLE,ctypes.POINTER(Entry)]
    api.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
    snapshot=api.CreateToolhelp32Snapshot(2,0)
    if snapshot==ctypes.c_void_p(-1).value:raise updates.UpdateError('process_status_unreadable')
    wanted=str(Path(root,'MFAAvalonia.exe').resolve()).casefold();result=[]
    try:
        entry=Entry();entry.dwSize=ctypes.sizeof(entry);ok=api.Process32FirstW(snapshot,ctypes.byref(entry))
        while ok:
            if entry.szExeFile.casefold()=='mfaavalonia.exe':
                process=api.OpenProcess(0x1000,False,entry.th32ProcessID)
                if process:
                    try:
                        buffer=ctypes.create_unicode_buffer(32768);length=wintypes.DWORD(len(buffer))
                        if api.QueryFullProcessImageNameW(process,0,buffer,ctypes.byref(length)) and str(Path(buffer.value).resolve()).casefold()==wanted:
                            result.append(entry.th32ProcessID)
                    finally:api.CloseHandle(process)
            ok=api.Process32NextW(snapshot,ctypes.byref(entry))
    finally:api.CloseHandle(snapshot)
    return result


def _ticket(root,path):
    root=Path(root).resolve(strict=True);path=Path(path).resolve(strict=True)
    stage=path.parent;ticket=updates.read_json(path)
    if (stage.parent!=root/'updates/suite-staged' or Path(path).name!='ticket.json' or
            len(stage.name)!=32 or any(char not in '0123456789abcdef' for char in stage.name) or ticket.get('schema_version')!=SCHEMA):
        raise updates.UpdateError('invalid_bootstrap_ticket')
    updates.contained(root,Path(path).resolve().relative_to(root).as_posix())
    files=ticket.get('files')
    if not isinstance(files,dict) or set(files)!=ALLOWLIST:raise updates.UpdateError('invalid_bootstrap_manifest')
    for name,row in files.items():
        updates.contained(root,name)
        if (not isinstance(row,dict) or type(row.get('size_bytes')) is not int or not 0<=row['size_bytes']<=4*1024**2 or
                not isinstance(row.get('sha256'),str) or len(row['sha256'])!=64 or
                any(char not in '0123456789abcdef' for char in row['sha256'])):
            raise updates.UpdateError('invalid_bootstrap_manifest')
    return root,stage,ticket


def prepare(root,bundle):
    root,_,installed=updates.installation(root)
    if installed['product_id']!='umaa-public' or installed['version'] not in SUPPORTED:raise updates.UpdateError('bootstrap_version_unsupported')
    bundle=Path(bundle).resolve(strict=True)
    manifest=updates.read_json(bundle/'bootstrap-manifest.json')
    if manifest.get('schema_version')!=SCHEMA or set(manifest.get('files',{}))!=ALLOWLIST:
        raise updates.UpdateError('invalid_bootstrap_manifest')
    stage=root/'updates/suite-staged'/uuid.uuid4().hex;stage.mkdir(parents=True)
    ticket={**manifest,'from_version':installed['version'],'purpose':'updater_only'}
    updates.write_json(stage/'ticket.json',ticket)
    _ticket(root,stage/'ticket.json')
    for name,row in ticket['files'].items():
        source=updates.contained(bundle,name)
        if not source.is_file() or source.stat().st_size!=row['size_bytes'] or digest(source)!=row['sha256']:
            raise updates.UpdateError('package_integrity_failed')
        target=updates.contained(stage/'payload',name);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
    helper=stage/'bootstrap-helper';runtime=helper/'runtime';runtime.mkdir(parents=True)
    for source in (root/'runtime').iterdir():
        if source.is_file() and source.suffix not in {'.pyc','.pyo'}:shutil.copy2(source,runtime/source.name)
    if not (runtime/'python.exe').is_file():raise updates.UpdateError('embedded_runtime_missing')
    shutil.copytree(stage/'payload/backend',helper/'backend')
    (helper/'backend/scripts/__init__.py').write_text('',encoding='utf8')
    (runtime/'python311._pth').write_text('python311.zip\n.\nLib/site-packages\n../backend\nimport site\n',encoding='utf8')
    return {'ok':True,'status':'bootstrap_ready','ticket':str(stage/'ticket.json'),
            'helper_executable':str(runtime/'python.exe'),'helper_script':str(helper/'backend/scripts/maauma_updater_bootstrap.py')}


def apply(root,ticket_path,*,parent_pids=()):
    root,stage,ticket=_ticket(root,ticket_path)
    for pid in set(parent_pids)|set(gui_pids(root)):updates.wait_for_exit(int(pid))
    with updates.installation_lock(root):
        if gui_pids(root):raise updates.UpdateError('application_running')
        components._assert_idle(root)
        transaction=updates.read_json(root/components.TRANSACTION) if (root/components.TRANSACTION).is_file() else {}
        if transaction.get('phase') in {'applying','rollback_failed'}:raise updates.UpdateError('rollback_required')
        _,_,installed=updates.installation(root)
        if ticket.get('from_version') not in SUPPORTED or installed['version']!=ticket['from_version']:raise updates.UpdateError('stale_update_ticket')
        changes=[]
        for name,row in ticket['files'].items():
            source=updates.contained(stage/'payload',name);target=updates.contained(root,name)
            if not source.is_file() or source.stat().st_size!=row['size_bytes'] or digest(source)!=row['sha256']:
                raise updates.UpdateError('package_integrity_failed')
            if target.is_file() and target.stat().st_size==row['size_bytes'] and digest(target)==row['sha256']:continue
            if target.exists() and not target.is_file():raise updates.UpdateError('managed_path_not_file')
            changes.append(name)
        if not changes:
            # Preserve the first bridge's pre-bootstrap backup and its recovery
            # pointer; a harmless repeat must not replace it with an empty undo.
            recovery=transaction.get('ticket') if transaction.get('purpose')=='updater_only' else None
            return {'ok':True,'status':'updater_bootstrapped','version':ticket['from_version'],
                    'changed_files':0,'ticket':recovery}
        journal={'schema_version':'maauma.suite_transaction.v1','phase':'preparing','operations':[],'purpose':'updater_only'}
        for name in changes:
            target=updates.contained(root,name);existed=target.is_file()
            if existed:
                backup=updates.transaction_io_path(updates.contained(stage/'backup',name));backup.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,backup)
            journal['operations'].append({'path':name,'existed':existed})
        journal['phase']='applying';updates.write_json(stage/'journal.json',journal)
        updates.write_json(root/components.TRANSACTION,{'phase':'applying','ticket':str(stage/'ticket.json')})
        try:
            for name in changes:
                temporary=updates.transaction_io_path(updates.contained(stage/'incoming',name));temporary.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(updates.contained(stage/'payload',name),temporary)
                target=updates.contained(root,name);target.parent.mkdir(parents=True,exist_ok=True);os.replace(temporary,target)
            journal['phase']='installed';updates.write_json(stage/'journal.json',journal)
            updates.write_json(root/components.TRANSACTION,{'phase':'installed','ticket':str(stage/'ticket.json'),'purpose':'updater_only'})
        except Exception:
            try:components._restore(root,stage,journal)
            except Exception:
                journal['phase']='rollback_failed';updates.write_json(stage/'journal.json',journal)
                updates.write_json(root/components.TRANSACTION,{'phase':'rollback_failed','ticket':str(stage/'ticket.json')})
                raise updates.UpdateError('rollback_required') from None
            raise updates.UpdateError('install_failed_rolled_back') from None
    return {'ok':True,'status':'updater_bootstrapped','version':ticket['from_version'],'changed_files':len(changes),'ticket':str(stage/'ticket.json')}


def rollback(root,ticket_path):
    root,stage,ticket=_ticket(root,ticket_path)
    with updates.installation_lock(root):
        if gui_pids(root):raise updates.UpdateError('application_running')
        components._assert_idle(root)
        transaction=updates.read_json(root/components.TRANSACTION)
        if transaction.get('ticket')!=str(stage/'ticket.json') and transaction.get('phase')!='rolled_back':raise updates.UpdateError('stale_rollback_ticket')
        if ticket.get('from_version') not in SUPPORTED or updates.installation(root)[2]['version']!=ticket['from_version']:raise updates.UpdateError('stale_rollback_ticket')
        journal=updates.read_json(stage/'journal.json')
        if journal.get('phase') not in {'installed','applying','rollback_failed'}:raise updates.UpdateError('stale_rollback_ticket')
        components._restore(root,stage,journal)
    return {'ok':True,'status':'updater_bootstrap_rolled_back','version':ticket['from_version']}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--bundle',type=Path,default=Path(__file__).resolve().parents[2])
    parser.add_argument('--apply',type=Path);parser.add_argument('--rollback',type=Path)
    args=parser.parse_args(argv)
    try:
        if args.apply:response=apply(args.root,args.apply)
        elif args.rollback:response=rollback(args.root,args.rollback)
        else:
            prepared=prepare(args.root,args.bundle)
            completed=subprocess.run([prepared['helper_executable'],'-B','-X','utf8',prepared['helper_script'],
                                      '--root',str(args.root),'--apply',prepared['ticket']],capture_output=True,encoding='utf8',
                                     creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            response=json.loads(completed.stdout)
    except (updates.UpdateError,OSError,ValueError,KeyError,TypeError) as error:
        response={'ok':False,'code':str(error) if isinstance(error,updates.UpdateError) else 'bootstrap_failed'}
    print(json.dumps(response,ensure_ascii=False));return 0 if response.get('ok') else 1


if __name__=='__main__':raise SystemExit(main())
