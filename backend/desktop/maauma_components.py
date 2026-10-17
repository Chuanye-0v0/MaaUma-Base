"""Authorized Pacer overlays and coordinated base/module transactions."""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from urllib.parse import urlsplit
import uuid
from zipfile import ZipFile,BadZipFile

from desktop import maauma_service as service
from desktop import product_updates as updates

MANIFEST='maauma-component-manifest.json'
STATE='config/maauma-modules.json'
TRANSACTION='updates/maauma-suite-state.json'
MAX_ARCHIVE=updates.MAX_ARCHIVE
MUTABLE={'local_backend.json','pacer/portable_config.json'}


def _error(code):raise updates.UpdateError(code)


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
    if url.startswith('/v1/'):return url
    if not url.startswith(base+'/v1/'):_error('service_origin_mismatch')
    return url[len(base):]


def download(root,entry,target,*,product,public=False,progress=None,item_id=None):
    from desktop.update_progress import content_length,read_chunk
    if progress is not None:progress.start_item(item_id)
    path=_entry_url(root,entry)
    if public and product!='umaa-public':_error('wrong_product')
    size=0
    try:
        response=(service.public_wire(root,path,binary=True,timeout=90) if public else
                  service.wire(root,path,product=product,binary=True,timeout=90))
        with response,Path(target).open('wb') as output:
            if progress is not None:progress.connected(content_length(response))
            while True:
                block=read_chunk(response)
                if not block:break
                size+=len(block)
                if size>MAX_ARCHIVE:_error('package_too_large')
                output.write(block)
                if progress is not None:progress.received(len(block))
    except IncompleteRead:raise updates.UpdateError('package_download_incomplete') from None
    if not size:_error('package_integrity_failed')
    if progress is not None:progress.transferred()


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


def prepare(root,*,mode='pacer',progress=None):
    if progress is not None:progress.stage('checking')
    if mode not in {'pacer','update'}:_error('invalid_update_mode')
    root,name,installed=updates.installation(root)
    if installed['product_id']!='umaa-public':_error('wrong_product')
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
    ticket={'schema_version':'maauma.prepared_suite.v1','from_version':installed['version'],'base_version':target_version,
            'base_manifest':name,'trace_id':auth['distribution_receipt']['trace_id'] if component_entry else None,'entries':[]}
    from desktop import maauma_file_patches as patches
    plans=[]
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
    finally:patches.close(plans)
    if not ticket['entries']:return {'ok':True,'status':'current','version':installed['version'],**context}
    if progress is not None:progress.stage('preparing')
    updates.write_json(stage/'ticket.json',ticket)
    return {'ok':True,'status':'prepared','ticket':str(stage/'ticket.json'),'version':target_version,
            'components':['pacer'] if component_entry else [],'requires_exit':True,**context}


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


def _restore(root,stage,journal):
    for item in reversed(journal['operations']):
        name=updates.relative_path(item['path']);target=updates.contained(root,name)
        if item['existed']:
            backup=updates.transaction_io_path(updates.contained(stage/'backup',name))
            if not backup.is_file():_error('rollback_backup_missing')
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(backup,target)
        elif target.is_file():
            retired=updates.transaction_io_path(updates.contained(stage/'retired',name));retired.parent.mkdir(parents=True,exist_ok=True)
            os.replace(target,retired)
    journal['phase']='rolled_back';updates.write_json(stage/'journal.json',journal)
    updates.write_json(root/TRANSACTION,{'phase':'rolled_back','ticket':str(stage/'ticket.json')})


def apply_prepared(root,ticket_path,*,parent_pid=None):
    root,stage,ticket=_ticket(root,ticket_path)
    if parent_pid is not None:updates.wait_for_exit(int(parent_pid))
    with updates.installation_lock(root):
        _assert_idle(root)
        transaction=updates.read_json(root/TRANSACTION) if (root/TRANSACTION).is_file() else {}
        if transaction.get('phase') in {'applying','rollback_failed'}:_error('rollback_required')
        _,manifest_name,installed=updates.installation(root);previous=state(root)
        if installed['version']!=ticket.get('from_version'):_error('stale_update_ticket')
        journal_path=stage/'journal.json'
        if journal_path.exists() and updates.read_json(journal_path).get('phase') in {'applying','rollback_failed'}:_error('rollback_required')
        entries=ticket.get('entries')
        if not isinstance(entries,list) or not entries:_error('invalid_update_ticket')
        if 'pacer' in previous['components'] and not any(row.get('kind')=='pacer' for row in entries):_error('compatible_pacer_required')
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
                    if path.stat().st_size!=row['personal_size_bytes'] or _digest(path)!=row['personal_sha256']:_error('package_integrity_failed')
                if row.get('delivery')=='file_patch':
                    if row['kind']=='base' and _digest(root/manifest_name)!=row['patch_entry']['from_manifest_sha256']:
                        patches.mark_full(root,'base',row['from_version'],entry)
                        _error('incremental_source_changed')
                    plan=patches.patch_plan(root,stage,row,verify=lambda value:value==row.get('receipt'))
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
            try:
                for item in journal['operations']:
                    name=item['path'];target=updates.contained(root,name)
                    if name in incoming or name in generated:
                        temporary=updates.transaction_io_path(updates.contained(stage/'incoming',name));temporary.parent.mkdir(parents=True,exist_ok=True)
                        if name in generated:temporary.write_bytes(generated[name])
                        else:
                            incoming_source=incoming[name]
                            if incoming_source[0]=='file':shutil.copy2(incoming_source[1],temporary)
                            else:
                                _,archive,member=incoming_source
                                with archive.open(member) as source,temporary.open('wb') as output:shutil.copyfileobj(source,output)
                        target.parent.mkdir(parents=True,exist_ok=True);os.replace(temporary,target)
                    elif target.exists() and name not in new_managed:
                        retired=updates.transaction_io_path(updates.contained(stage/'retired',name));retired.parent.mkdir(parents=True,exist_ok=True);os.replace(target,retired)
                journal['phase']='installed';updates.write_json(journal_path,journal)
                updates.write_json(root/TRANSACTION,{'phase':'installed','ticket':str(stage/'ticket.json'),
                    'base_version':ticket['base_version']})
            except Exception:
                try:_restore(root,stage,journal)
                except Exception:
                    journal['phase']='rollback_failed';updates.write_json(journal_path,journal)
                    updates.write_json(root/TRANSACTION,{'phase':'rollback_failed','ticket':str(stage/'ticket.json')})
                    _error('rollback_required')
                _error('install_failed_rolled_back')
        finally:
            for archive in archives:archive.close()
    return {'ok':True,'status':'installed','version':ticket['base_version'],'components':list(new_state['components'])}


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
    if helper.exists():_error('installer_already_prepared')
    # Installer/service code is standard-library-only. The embedded interpreter
    # keeps its stdlib ZIP and native DLL/PYD files at runtime's top level; game
    # OCR/scientific site-packages are unused here and can exceed Windows path
    # limits after adding a transaction/helper prefix.
    isolated_runtime=helper/'runtime';isolated_runtime.mkdir(parents=True)
    for source in (root/'runtime').iterdir():
        if source.is_file() and source.suffix not in {'.pyc','.pyo'}:
            shutil.copy2(source,isolated_runtime/source.name)
    for module in ('desktop/maauma_service.py','desktop/maauma_components.py','desktop/product_updates.py',
                   'desktop/maauma_file_patches.py','desktop/zip_recipe.py','desktop/update_progress.py',
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
