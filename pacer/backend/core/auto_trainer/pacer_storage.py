"""Expire generated work images after four hours, stop copies after twelve.

Journals, exports, identities and current recovery frames remain. Recoverable
careers expose only older, confirmed-consumed captures; retention is not a game
input or a declaration that the whole career has ended.
"""
import json
import hashlib
import logging
import os
from contextvars import ContextVar
from contextlib import contextmanager
from pathlib import Path
import re
import stat
import time
import threading
import uuid

ORDINARY_RETENTION_SECONDS = 4 * 3600
FAILURE_RETENTION_SECONDS = 12 * 3600
_IMAGE = {'.png', '.jpg', '.jpeg', '.webp'}
_GENERATED = ('autonomous_', 'turn_', 'training_review', 'cut_restart')
_EXCLUDED = {'exports','review_exports','member_portraits','fixtures','source'}
_READ_VERSIONS = ContextVar('pacer_retention_read_versions', default=None)
_UNSTABLE_READ = object()
LOG_COMPRESSION_VERSION = 1
_RUN_READ_ACCESS = ContextVar('pacer_run_read_access',default=None)
_LARGE_JSON = {'pipeline_report.json','state.json','candidates.json','decision.json',
               'report.json','training_options.generated.json','training_page_observation.json',
               'state_context.json','ss_comparison_snapshot.json','回合记录.json'}

def _io(path):
    """Windows extended spelling at I/O only; ownership stays lexical."""
    path=Path(path)
    if os.name!='nt':return path
    value=str(path.absolute())
    if not value.startswith('\\\\?\\'):
        value='\\\\?\\UNC\\'+value[2:]if value.startswith('\\\\')else'\\\\?\\'+value
    return Path(value)


def _glob(path,pattern):
    path=Path(path);native=_io(path)
    for child in native.glob(pattern):yield path/child.relative_to(native)


def _rglob(path,pattern):
    path=Path(path);native=_io(path)
    for child in native.rglob(pattern):yield path/child.relative_to(native)


def _read(path):
    path=Path(path)
    versions=_READ_VERSIONS.get()
    before=None
    try:
        before=_token(path)
        if versions is not None:
            if path in versions and versions[path]!=before:
                versions[path]=_UNSTABLE_READ
            else:versions.setdefault(path,before)
        if before is None or _linked(path):return None
        value=json.loads(_io(path).read_text(encoding='utf-8-sig'))
        if _token(path)!=before:
            if versions is not None:versions[path]=_UNSTABLE_READ
            return None
        if versions is not None and versions[path] is _UNSTABLE_READ:return None
        return value if isinstance(value,dict) else None
    except (OSError,ValueError):
        if versions is not None:versions[path]=_UNSTABLE_READ
        return None


def _read_token(path):
    """Bind guards to bytes already interpreted, including observed absence."""
    versions=_READ_VERSIONS.get()
    return versions[path]if versions is not None and path in versions else _token(path)

def _linked(path):
    info=_io(path).lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info,'st_file_attributes',0)&0x400)

def _token(path):
    try:
        info=_io(path).stat()
        return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)
    except FileNotFoundError:return None

def _owned_path(path,owner,managed):
    """The lexical path and every managed ancestor must remain ordinary."""
    try:
        supplied=Path(path).absolute();resolved=supplied.resolve()
        if resolved!=supplied or not resolved.is_relative_to(owner)or not resolved.is_relative_to(managed):return False
        chain=[supplied,*supplied.parents]
        return not any(_linked(p)for p in chain[:chain.index(managed)+1])
    except (OSError,ValueError):return False

def _refs(value,base,run):
    if isinstance(value,dict):
        for child in value.values():yield from _refs(child,base,run)
    elif isinstance(value,list):
        for child in value:yield from _refs(child,base,run)
    elif isinstance(value,str) and Path(value).suffix.lower() in _IMAGE:
        path=Path(value)
        path=(path if path.is_absolute() else base/path).resolve()
        if path.is_relative_to(run):yield path

def _closed(run,endpoint):
    if (_io(run/'session')).exists() and _linked(run/'session'):return None
    progress=_read(run/'progress.json')
    terminal=_read(run/'session/larc_terminal_settlement.json')
    if (not progress or not terminal or progress.get('run_session_id')!=run.name
            or progress.get('device')!=endpoint or terminal.get('run_session_id')!=run.name
            or terminal.get('device')!=endpoint or terminal.get('status')!='complete'
            or type(progress.get('next_turn')) is not int
            or terminal.get('turn_index')!=progress['next_turn']):
        return None
    report_path=terminal.get('report_path')
    if not isinstance(report_path,str):return None
    report_path=Path(report_path).absolute()
    if not report_path.is_relative_to(run):return None
    owned=[report_path,*report_path.parents]
    owned=owned[:owned.index(run)+1]
    try:
        if any(_linked(p)for p in reversed(owned)):return None
    except OSError:return None
    report_path=report_path.resolve()
    if not report_path.is_relative_to(run):return None
    report=_read(report_path)
    if (not report or report.get('run_session_id')!=run.name or report.get('endpoint')!=endpoint
            or report.get('ok') is not True or report.get('final_page')!='home'
            or report.get('turn_index')!=progress['next_turn']):
        return None
    # All terminal/result frames remain exportable, even after ordinary pages expire.
    return [run/'progress.json',run/'session/larc_terminal_settlement.json',report_path],set(_refs(report,report_path.parent,run))

def _pending(run):
    session=run/'session'
    if not _io(session).is_dir() or _linked(session):return True
    for path in _glob(session,'*.json'):
        value=_read(path)
        if value is None:return True
        if (value.get('status')=='pending_purchase' or value.get('pending_purchase') is True
                or value.get('pending_action') is not None or value.get('input_in_flight') is True):return True
    # These are the runner's in-flight intentions, not historical receipt fields.
    return any(_io(p).is_file() for p in _glob(run,'autonomous_*/pending.json'))

def _lease_free(workspace,endpoint):
    """Probe existing native locks; never create, alter, or remove lock files."""
    base=os.environ.get('UMA_PUBLIC_LOCK_ROOT') or os.environ.get('LOCALAPPDATA')
    if not base:return False
    folders=(Path(base)/'UmaSuite/device-locks',workspace/'runs/device-occupancy')
    try:
        for folder in folders:
            path=folder/(endpoint.replace(':','_')+'.lock')
            if not _io(path).exists():continue
            if any(_linked(p)for p in (path,*path.parents)if _io(p).exists()):return False
            with _io(path).open('rb')as stream:
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                    stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                    fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
        return True
    except OSError:return False

def _manual_closed(run,endpoint,workspace):
    session=run/'session'
    if not _io(session).is_dir()or _linked(session):return None
    state_path=session/'larc_manual_pause_state.json';schema='larc_manual_pause_state.v1'
    if not _io(state_path).is_file():state_path=session/'larc_manual_handoff_state.json';schema='larc_manual_handoff_state.v1'
    state=_read(state_path);progress=_read(run/'progress.json')
    if (not state or not progress or state.get('schema_version')!=schema
            or state.get('run_session_id')!=run.name or state.get('device')!=endpoint
            or progress.get('run_session_id')!=run.name or progress.get('device')!=endpoint
            or state.get('stage')!='final_arc' or state.get('status')!='handed_off'
            or state.get('reason')!='manual_final_arc_finish_handoff'
            or state.get('input_performed') is not False):return None
    control_dir=workspace/'runs/umaa-panel-control'/endpoint.rsplit(':',1)[1]
    for directory in (control_dir.parent,control_dir):
        if _io(directory).exists()and _linked(directory):return None
    control_path=control_dir/'control_state.json';control=_read(control_path)
    if (not control or control.get('schema_version')!='ug_parent_dashboard.v2.control_state'
            or control.get('state') not in {'idle','stopped'}
            or control.get('tap_in_flight') is not False or(_io(control_dir/'runner.lock')).exists()
            or not _lease_free(workspace,endpoint)):return None
    return [run/'progress.json',state_path,control_path],set(_refs(state,session,run))

def record_retention(run,endpoint,mode):
    """Remember an explicit runner option without changing the user's config."""
    run=Path(run).absolute()
    if mode not in {'keep','discard'}or not _owned_path(run,run,run):return
    progress=_read(run/'progress.json') or {}
    if progress.get('run_session_id')!=run.name or progress.get('device')!=endpoint:return
    session=run/'session'
    if not _io(session).is_dir()or not _owned_path(session,run,run):return
    path=session/'pacer_image_retention.json'
    if _io(path).exists()and _linked(path):return
    value={'schema_version':'pacer.image_retention.v1','run_session_id':run.name,
           'device':endpoint,'training_records':mode}
    _store(path,value,run)

def _store(path,value,owner):
    if not _owned_path(path.parent,owner,owner)or(_io(path).exists()and _linked(path)):return False
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with _io(temporary).open('x',encoding='utf8')as stream:json.dump(value,stream,ensure_ascii=False)
        if not _owned_path(temporary,owner,owner)or not _owned_path(path.parent,owner,owner):return False
        _io(temporary).replace(_io(path));return True
    finally:
        if _io(temporary).exists()and _owned_path(temporary,owner,owner):_io(temporary).unlink()

def _retention_mode(run,endpoint):
    policy=_read(run/'session/pacer_image_retention.json') or {}
    if (policy.get('schema_version')=='pacer.image_retention.v1'
            and policy.get('run_session_id')==run.name and policy.get('device')==endpoint):
        return policy.get('training_records')

def _keep(run,endpoint):
    options=_read(run/'session/umaa_panel_options.json') or {}
    if options.get('retain_training') is True:return True
    return _retention_mode(run,endpoint)=='keep'

def _rotate(items,after,key):
    return [v for v in items if key(v)>after]+[v for v in items if key(v)<=after]


def _history_skip(report,reason):
    counts=report.setdefault('history_skips',{})
    counts[reason]=counts.get(reason,0)+1


def _receipt_digest(receipt):
    if not isinstance(receipt,dict):return None
    return hashlib.sha256(json.dumps(receipt,sort_keys=True,ensure_ascii=False,
        separators=(',',':')).encode('utf8')).hexdigest()


def _review_owned(record_path,run,row,receipt,cache):
    """Remember only the identity read from a possibly large review JSON.

    The cache never substitutes a new token for old interpreted bytes. Even a
    cache hit joins this sweep's read-version guards before it owns any image.
    """
    key=str(record_path)
    if not _owned_path(record_path,run,run):cache.pop(key,None);return False
    before=_token(record_path)
    cached=cache.get(key)
    if (isinstance(cached,dict)and isinstance(cached.get('token'),list)
            and tuple(cached['token'])==before):
        versions=_READ_VERSIONS.get()
        if versions is not None:
            if record_path in versions and versions[record_path]!=before:
                versions[record_path]=_UNSTABLE_READ
            else:versions.setdefault(record_path,before)
    else:
        cache.pop(key,None)
        record=_read(record_path)
        after=_token(record_path)
        if before is None or before!=after:return False
        # A stable unreadable/malformed record is a negative cache entry only.
        # It can avoid repeated expensive reads but can never own review pages.
        cached={'token':list(before),'turn':None,'execution_sha256':None}
        read_version=_read_token(record_path)
        if isinstance(record,dict)and read_version==before:
            cached.update(turn=record.get('回合')if type(record.get('回合'))is int else None,
                execution_sha256=_receipt_digest(record.get('执行记录')))
        cache[key]=cached
    return (cached.get('turn')==row['turn_index']and cached.get('execution_sha256')is not None
            and cached['execution_sha256']==_receipt_digest(receipt))


def _consumed_scopes(run,segment,output,row,receipt,review_cache=None):
    """Dispose a proven older action's work, not another turn's recovery files.

    Older custom output layouts retain their original capture-only handling.
    A native turn directory also owns after/growth/navigation work and its
    separately copied five-page review, whose own receipt must still agree.
    """
    native=(output.parent==segment and re.match(
        rf'^turn_{row["turn_index"]:03d}(?:_|$)',output.name) is not None)
    scope=output if native else output/'capture'
    if not _io(scope).is_dir()or not _owned_path(scope,run,run):return [],[]
    scopes=[str(scope.relative_to(run))];proof=[]
    if native:
        review=run/'training_review'/f'turn_{row["turn_index"]:03d}'
        record_path=review/'回合记录.json'
        if _io(review).is_dir()and _owned_path(review,run,run):
            if _review_owned(record_path,run,row,receipt,review_cache if review_cache is not None else {}):
                scopes.append(str(review.relative_to(run)));proof.append(record_path)
    return scopes,proof


def _consumed_histories(run,endpoint,deadline,report,after):
    """Yield old work images from consumption rows, one segment at a time.

    The latest consumed turn and every unconfirmed/pending turn remain intact.
    Segment rotation prevents a large historical prefix consuming every sweep.
    """
    progress_path=run/'progress.json';progress=_read(progress_path)or{}
    if (progress.get('run_session_id')!=run.name or progress.get('device')!=endpoint
            or type(progress.get('next_turn'))is not int):
        _history_skip(report,'progress_unavailable');return
    pending_path=run/'session/larc_autonomous_pending.json'
    pending=_read(pending_path)
    if pending is None and _read_token(pending_path) is not None:
        _history_skip(report,'pending_unavailable');return
    pending=pending or {}
    # Absence is a token too: a newly created pending action must invalidate
    # this sweep before any of its recovery pages can be removed.
    initial=[progress_path,pending_path]
    segments=sorted(_glob(run,'autonomous_*'))
    for segment in _rotate(segments,after,lambda p:p.name):
        if time.monotonic()>deadline:report['budget_exhausted']=True;return
        report['_after_segments'][str(run)]=segment.name
        if not _io(segment).is_dir()or not _owned_path(segment,run,run):continue
        summary_path=segment/'summary.final.json'
        if not _io(summary_path).is_file():summary_path=segment/'summary.json'
        summary=_read(summary_path)or{}
        if (summary.get('run_session_id')!=run.name or summary.get('device')!=endpoint
                or not isinstance(summary.get('turns'),list)):
            _history_skip(report,'summary_unavailable');continue
        proof=list(initial);scopes=[];seen=set()
        for row in summary['turns']:
            if time.monotonic()>deadline:report['budget_exhausted']=True;break
            if (not isinstance(row,dict)or row.get('turn_consumed')is not True
                    or type(row.get('turn_index'))is not int
                    or row['turn_index']>=progress['next_turn']-1
                    or not isinstance(row.get('output'),str)):continue
            output=Path(row['output']).absolute()
            if not output.is_relative_to(segment)or not _owned_path(output,run,run):
                _history_skip(report,'output_not_owned');continue
            if (pending.get('output')==str(output)
                    or pending.get('turn_index')==row['turn_index']):continue
            receipt_path=output/'action_receipt.json';receipt=_read(receipt_path)
            if not receipt or receipt!=row.get('execution'):
                _history_skip(report,'consumption_receipt_changed');continue
            if output in seen:continue
            seen.add(output)
            owned_scopes,review_proof=_consumed_scopes(run,segment,output,row,receipt,
                report.setdefault('_review_ownership_cache',{}))
            scopes.extend(owned_scopes)
            proof.extend((summary_path,receipt_path,*review_proof))
            if time.monotonic()>deadline:report['budget_exhausted']=True;break
        if scopes:yield list(dict.fromkeys(proof)),scopes


def _failure_copies(run,endpoint):
    """Stop-report copies expire separately from a recoverable source frame."""
    progress_path=run/'progress.json';progress=_read(progress_path)or{}
    folder=run/'session/stop_diagnostic'
    if (progress.get('run_session_id')!=run.name or progress.get('device')!=endpoint
            or not _io(folder).is_dir()or not _owned_path(folder,run,run)):return None
    manifest=folder/'manifest.json'
    return [progress_path,*([manifest]if _io(manifest).is_file()else[])]


def _units(runs,report,deadline,only_run=None,after_root='',attempt_after=None,segment_after=None):
    attempt_after=attempt_after or {}
    segment_after=segment_after or {}
    paths=sorted(_glob(runs,'*'))
    for path in _rotate(paths,after_root,lambda p:p.name):
        if time.monotonic()>deadline:report['budget_exhausted']=True;return
        report['_after_root']=path.name
        if not _io(path).is_dir() or _linked(path):continue
        if only_run is not None and not only_run.is_relative_to(path):continue
        if path.name.startswith('larc_panel_'):
            if (_io(path/'session')).exists() and _linked(path/'session'):
                report['protected_runs']+=1;continue
            marker_path=path/'session/pacer_center_run.json'
            marker=_read(marker_path)if _io(marker_path).exists()else None
            endpoint=(_read(path/'progress.json') or {}).get('device')
            if (not isinstance(endpoint,str)or not re.fullmatch(r'127\.0\.0\.1:[0-9]{1,5}',endpoint)
                    or not 1<=int(endpoint.rsplit(':',1)[1])<=65535):continue
            if _io(marker_path).exists()and (not marker or marker.get('source')!='private_pacer_center'
                    or marker.get('mode')!='career' or marker.get('run_id')!=path.name
                    or marker.get('endpoint')!=endpoint):
                report['protected_runs']+=1
                continue
            failure=_failure_copies(path,endpoint)
            if failure:yield path,failure,set(),'failure_copies',set(),endpoint,['session/stop_diagnostic']
            if _keep(path,endpoint):report['legacy_keep_runs_seen']+=1
            proof=_closed(path,endpoint)
            if proof:yield path,*proof,'normal_settlement',set(),endpoint,None
            else:
                manual=_manual_closed(path,endpoint,runs.parent)
                if manual:yield path,manual[0],set(),'manual_terminal_handoff',manual[1],endpoint,None
                else:
                    report['protected_runs']+=1
                    for proof,scopes in _consumed_histories(path,endpoint,deadline,report,segment_after.get(str(path),'')):
                        yield path,proof,set(),'consumed_history',set(),endpoint,scopes
        elif path.name.startswith('cut_pacer_'):
            batch_path=path/'cut_farming.json';batch=_read(batch_path)
            if (not batch or batch.get('schema_version')!='uma.cut_farming_session.v1'
                    or batch.get('pending_action') is not None):
                report['protected_runs']+=1;continue
            config=batch.get('config') or {}
            if not isinstance(config,dict) or not isinstance(batch.get('attempts'),list):
                report['protected_runs']+=1;continue
            legacy_keep=config.get('retain_screenshots')is True
            if legacy_keep:report['legacy_keep_runs_seen']+=1
            port=config.get('device_port')
            if type(port) is not int or not 1<=port<=65535:continue
            attempts=path/'attempts'
            if not _io(attempts).is_dir()or not _owned_path(attempts,path,runs):
                report['protected_runs']+=1;continue
            listed=[a for a in batch['attempts']if isinstance(a,dict)and isinstance(a.get('run_id'),str)]
            listed=sorted(listed,key=lambda a:a['run_id'])
            for attempt in _rotate(listed,attempt_after.get(path.name,''),lambda a:a['run_id']):
                if time.monotonic()>deadline:report['budget_exhausted']=True;return
                report['_after_attempts'][path.name]=attempt['run_id']
                if not isinstance(attempt,dict):continue
                name=attempt.get('run_id');supplied=attempt.get('run_root')
                if not isinstance(name,str) or Path(name).name!=name or not isinstance(supplied,str):continue
                run=(path/'attempts'/name)
                if only_run is not None and run!=only_run:continue
                if not _io(run).is_dir()or not _owned_path(run,attempts,runs)or Path(supplied).resolve()!=run.resolve():continue
                run=run.resolve();endpoint=f'127.0.0.1:{port}'
                failure=_failure_copies(run,endpoint)
                if failure:yield run,[batch_path,*failure],set(),'failure_copies',set(),endpoint,['session/stop_diagnostic']
                if not legacy_keep and _keep(run,endpoint):report['legacy_keep_runs_seen']+=1
                normal=_closed(run,endpoint)
                if normal:yield run,[batch_path,*normal[0]],normal[1],'normal_settlement',set(),endpoint,None;continue
                manual=_manual_closed(run,endpoint,runs.parent)
                if manual:yield run,[batch_path,*manual[0]],set(),'manual_terminal_handoff',manual[1],endpoint,None;continue
                progress=_read(run/'progress.json') or {}
                restart_path=run/'cut_restart_report.json';restart=_read(restart_path) or {}
                if (attempt.get('status') not in {'abandoned','abandoned_and_restarted'}
                        or progress.get('run_session_id')!=run.name or progress.get('device')!=endpoint
                        or restart.get('ok') is not True or restart.get('reason') not in
                        {'ordinary_abandon_confirmed','ordinary_start_confirmed'}):
                    report['protected_runs']+=1
                    for proof,scopes in _consumed_histories(run,endpoint,deadline,report,segment_after.get(str(run),'')):
                        yield run,[batch_path,*proof],set(),'consumed_history',set(),endpoint,scopes
                    continue
                yield run,[batch_path,run/'progress.json',restart_path],set(),'confirmed_abandon',set(),endpoint,None

def _relative(value):
    if not isinstance(value,str):return None
    path=Path(value)
    return path if not path.is_absolute()and '..'not in path.parts else None

def _scan_directory(value):
    relative=_relative(value)
    if relative is None or any(p in _EXCLUDED for p in relative.parts):return None
    if relative==Path('.'):return relative
    return relative if(relative.parts[0].startswith(_GENERATED)
                       or relative.parts[:2]==('session','stop_diagnostic'))else None

def _valid_walk(walk):
    return (isinstance(walk,dict)and isinstance(walk.get('dirs'),list)
            and isinstance(walk.get('entries'),list)and type(walk.get('index'))is int
            and 0<=walk['index']<=len(walk['entries'])
            and isinstance(walk.get('direct',[]),list))

def _frames(run,walk,report,deadline,extensions=None):
    """Persist one directory's names, then consume it without walking prior pages.

    Taking a single-directory name snapshot may exceed the soft time budget once.
    Saving that snapshot is essential: the next call must not pay that cost again.
    Image contents are never read and irrelevant top-level trees are never opened.
    """
    extensions=_IMAGE if extensions is None else extensions
    while walk.get('dirs')or walk.get('entries'):
        if time.monotonic()>deadline:report['budget_exhausted']=True;return
        if not walk.get('entries'):
            relative=_scan_directory(walk['dirs'].pop(0))
            if relative is None:continue
            directory=run/relative
            if not _io(directory).is_dir()or not _owned_path(directory,run,run):continue
            root=relative==Path('.')
            entries=[]
            with os.scandir(_io(directory))as iterator:
                for entry in iterator:
                    if entry.name in _EXCLUDED or entry.is_symlink():continue
                    is_dir=entry.is_dir(follow_symlinks=False)
                    if root and not entry.name.startswith(_GENERATED):continue
                    if is_dir or Path(entry.name).suffix.lower()in extensions:
                        entries.append([entry.name,is_dir])
            walk.update(directory=str(relative),entries=entries,index=0)
            report['indexed_directories']+=1
        entries=walk.get('entries')or[]
        while walk.get('index',0)<len(entries):
            if time.monotonic()>deadline:report['budget_exhausted']=True;return
            entry=entries[walk['index']];walk['index']+=1
            if not isinstance(entry,list)or len(entry)!=2 or not isinstance(entry[0],str)or type(entry[1])is not bool:continue
            name,is_dir=entry
            relative=_relative(name);directory=_scan_directory(walk.get('directory'))
            if relative is None or len(relative.parts)!=1 or directory is None:continue
            child=run/directory/relative
            if is_dir:
                if _scan_directory(str(child.relative_to(run)))is not None and _io(child).is_dir()and _owned_path(child,run,run):
                    walk['dirs'].append(str(child.relative_to(run)))
            else:
                report['visited_files']+=1
                yield child
        walk.update(entries=[],index=0)

def _lock(path):
    if _io(path).exists()and _linked(path):return None
    stream=_io(path).open('a+b')
    try:
        if os.name=='nt':
            import msvcrt
            if not _io(path).stat().st_size:stream.write(b'0');stream.flush()
            stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        return stream
    except OSError:stream.close();return None

def _maintain_images(workspace,*,now=None,dry_run=False,max_seconds=2.0,max_files=20000,min_interval=3600,_completed_run=None):
    """Bounded housekeeping for known terminal Pacer records; never fails a run."""
    report=dict(dry_run=dry_run,removed=0,removed_bytes=0,candidate_files=0,candidate_bytes=0,
                byte_count_basis='logical_file_lengths',physical_bytes_released=None,
                protected_runs=0,kept_runs=0,errors=0,budget_exhausted=False,runs=[],
                visited_files=0,indexed_directories=0,legacy_keep_runs_seen=0,
                policy={'ordinary_hours':4,'failure_hours':12,'legacy_keep_exempt':False})
    lock=None
    read_scope=_READ_VERSIONS.set({})
    try:
        supplied=Path(workspace).absolute()
        if any(_linked(p) for p in (supplied,*supplied.parents) if _io(p).exists()):
            return {**report,'skipped':'linked_backend_path'}
        workspace=supplied.resolve();runs=workspace/'runs'
        manifest=_read(workspace.parent/'product-package-manifest.json') or {}
        if workspace.name!='backend' or manifest.get('product_id')!='uma-cut':
            return {**report,'skipped':'not_packaged_backend'}
        if any((_io(p/'.git')).exists()or(_io(p/'.worktrees')).exists()for p in (workspace.parent,workspace.parent.parent)):
            return {**report,'skipped':'source_tree_not_a_client'}
        if not _io(runs).is_dir() or _linked(runs):return report
        now=time.time() if now is None else now
        state=runs/'maintenance/pacer-png-retention.json'
        if _io(state.parent).exists() and _linked(state.parent):return {**report,'skipped':'linked_maintenance_state'}
        if not dry_run:
            _io(state.parent).mkdir(parents=True,exist_ok=True)
            lock=_lock(state.with_suffix('.lock'))
            if lock is None:return {**report,'skipped':'maintenance_busy'}
        previous=_read(state) if _io(state).is_file() else {}
        previous=previous or {}
        review_cache=previous.get('_review_ownership_cache',{})
        report['_review_ownership_cache']=review_cache if isinstance(review_cache,dict)else{}
        only_run=None
        if _completed_run is not None:
            supplied_run=Path(_completed_run).absolute()
            if any(_linked(p)for p in (supplied_run,*supplied_run.parents)if _io(p).exists()):
                return {**report,'skipped':'linked_completed_run'}
            only_run=supplied_run.resolve()
        if only_run is not None and not only_run.is_relative_to(runs):return {**report,'skipped':'run_outside_backend'}
        if only_run is None and min_interval>0 and not dry_run and now<(previous or {}).get('retry_after',0):return {**report,'skipped':'recent_sweep'}
        walks=previous.get('_walks',{})if not dry_run else {}
        if not isinstance(walks,dict):walks={}
        after_root=previous.get('_after_root','')if not dry_run else ''
        if not isinstance(after_root,str):after_root=''
        attempts=previous.get('_after_attempts',{})if not dry_run else {}
        if not isinstance(attempts,dict):attempts={}
        attempts={k:v for k,v in attempts.items()if isinstance(k,str)and isinstance(v,str)}
        report['_after_root']=after_root;report['_after_attempts']=dict(attempts)
        segments=previous.get('_after_segments',{})if not dry_run else {}
        if not isinstance(segments,dict):segments={}
        segments={k:v for k,v in segments.items()if isinstance(k,str)and isinstance(v,str)}
        report['_after_segments']=dict(segments)
        deadline=time.monotonic()+max_seconds
        for run,proof,pinned,closed_kind,critical_refs,endpoint,scopes in _units(runs,report,deadline,only_run,after_root,attempts,segments):
            if time.monotonic()>deadline:report['budget_exhausted']=True;break
            if only_run is not None and closed_kind!='normal_settlement':report['protected_runs']+=1;continue
            if closed_kind not in {'consumed_history','failure_copies'}and _pending(run):report['protected_runs']+=1;continue
            roster_path=run/'session/larc_member_roster.json'
            roster=_read(roster_path)
            if _read_token(roster_path) is not None:
                if roster is None:report['protected_runs']+=1;continue
                pinned|=set(_refs(roster,roster_path.parent,run))
            pin_proof=[roster_path]
            if closed_kind in {'consumed_history','failure_copies'}:
                pending_path=run/'session/larc_autonomous_pending.json'
                pinned|=set(_refs(_read(pending_path),pending_path.parent,run))
                pin_proof.append(pending_path)
            guards={p:_read_token(p)for p in [*proof,*pin_proof]}
            candidate_before=report['candidate_files'];bytes_before=report['candidate_bytes']
            key=str(run.relative_to(runs))
            if scopes:key+='::'+Path(scopes[0]).parts[0]
            walk=walks.get(key)
            if (not _valid_walk(walk)or walk.get('scope')!=closed_kind
                    or(closed_kind=='consumed_history'and (walk.get('scope_revision')!=2
                        or walk.get('history_scopes')!=list(scopes)))):
                extra=[str(p.relative_to(run))for p in critical_refs
                       if not p.relative_to(run).parts[0].startswith(_GENERATED)
                       and p.relative_to(run).parts[:2]!=('session','stop_diagnostic')
                       and not any(part in _EXCLUDED for part in p.relative_to(run).parts)]
                walk={'dirs':list(scopes)if scopes is not None else['.','session/stop_diagnostic'],
                      'entries':[], 'index':0,'direct':extra,'scope':closed_kind,'scope_revision':2}
                if closed_kind=='consumed_history':walk['history_scopes']=list(scopes)
                walks[key]=walk
            def frames():
                while walk.get('direct'):
                    if time.monotonic()>deadline:report['budget_exhausted']=True;return
                    relative=_relative(walk['direct'].pop(0))
                    if relative is not None and(run/relative).resolve()in critical_refs:yield run/relative
                yield from _frames(run,walk,report,deadline)
            iterator=frames()
            while report['candidate_files']<max_files:
                try:path=next(iterator)
                except StopIteration:break
                try:
                    if not _io(path).is_file()or path.suffix.lower()not in _IMAGE or not _owned_path(path,run,runs):continue
                    relative=path.relative_to(run)
                    if any(part in _EXCLUDED for part in relative.parts):continue
                    critical=relative.parts[:2]==('session','stop_diagnostic')or path.resolve()in critical_refs
                    generated=relative.parts[0].startswith(_GENERATED)
                    if not(critical or generated)or path.resolve()in pinned:continue
                    info=_io(path).stat()
                    lifetime=FAILURE_RETENTION_SECONDS if critical else ORDINARY_RETENTION_SECONDS
                    if now-info.st_mtime<lifetime:continue
                    report['candidate_files']+=1;report['candidate_bytes']+=info.st_size
                    if dry_run:continue
                    before=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)
                    if any(_token(p)!=t for p,t in guards.items())or _token(path)!=before:continue
                    if not _owned_path(path,run,runs):
                        report['protected_runs']+=1;break
                    _io(path).unlink();report['removed']+=1;report['removed_bytes']+=info.st_size
                except OSError:report['errors']+=1
            else:report['budget_exhausted']=True
            complete=not(walk.get('dirs')or walk.get('entries')or walk.get('direct'))
            if complete:walks.pop(key,None)
            report['runs'].append({'run':str(run.relative_to(workspace)),'closed_kind':closed_kind,
                                  'candidate_files':report['candidate_files']-candidate_before,
                                  'candidate_bytes':report['candidate_bytes']-bytes_before,'scan_complete':complete})
            if report['budget_exhausted']:break
        if not dry_run:
            if _io(state.parent).exists() and _linked(state.parent):return {**report,'skipped':'linked_maintenance_state'}
            _io(state.parent).mkdir(parents=True,exist_ok=True)
            value={**report,'_walks':walks,'checked_at':now,
                   'retry_after':previous.get('retry_after',0)if only_run is not None else
                       now+(60 if report['budget_exhausted']or report['errors']else min_interval)}
            if not _store(state,value,runs):report['errors']+=1
    except Exception as error:
        report['errors']+=1
        logging.getLogger(__name__).warning('截图维护未完成：%s',type(error).__name__)
    finally:
        _READ_VERSIONS.reset(read_scope)
        if lock is not None:lock.close()
    report.pop('_after_attempts',None);report.pop('_after_root',None);report.pop('_after_segments',None)
    report.pop('_review_ownership_cache',None)
    return report


def _storage_leaf(filename):
    """Base loads this file outside Pacer's import namespace."""
    path=Path(__file__).resolve().with_name(filename)
    if not _io(path).is_file()or not _owned_path(path,path.parent,path.parent):return None
    values={'__file__':str(path),'__name__':'maauma_'+path.stem}
    exec(compile(_io(path).read_bytes(),str(path),'exec'),values)
    return values


def _json_idle(workspace,shared_backend,endpoint):
    """Advisory busy probe; compression never changes logical file contents.

    A new career can start after a probe. Unlike deleting an image, that does
    not invalidate its readers; the background worker stays below normal.
    """
    base=os.environ.get('UMA_PUBLIC_LOCK_ROOT')or os.environ.get('LOCALAPPDATA')
    folders=[workspace/'runs/device-occupancy',shared_backend/'runs/device-occupancy']
    if base:folders.append(Path(base)/'UmaSuite/device-locks')
    try:
        for folder in folders:
            path=folder/(endpoint.replace(':','_')+'.lock')
            if not _io(path).exists():continue
            if any(_linked(p)for p in (path,*path.parents)if _io(p).exists()):return False
            with _io(path).open('rb')as stream:
                import msvcrt
                stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
        return True
    except OSError:return False


def _json_runs(runs,report,deadline,after,attempt_after):
    for owner in _rotate(sorted(_glob(runs,'*')),after,lambda p:p.name):
        if time.monotonic()>deadline:report['budget_exhausted']=True;return
        report['_after_root']=owner.name
        if not _io(owner).is_dir()or not _owned_path(owner,runs,runs):continue
        if owner.name.startswith('larc_panel_'):
            possible=[owner]
        elif owner.name.startswith('cut_pacer_'):
            attempts=owner/'attempts'
            possible=sorted(_glob(attempts,'*'))if _io(attempts).is_dir()and _owned_path(attempts,owner,runs)else[]
        else:continue
        for run in _rotate(possible,attempt_after.get(owner.name,''),lambda p:p.name):
            if time.monotonic()>deadline:report['budget_exhausted']=True;return
            report['_after_attempts'][owner.name]=run.name
            if not _io(run).is_dir()or not _owned_path(run,owner,runs):continue
            progress_path=run/'progress.json';progress=_read(progress_path)or{}
            endpoint=progress.get('device')
            if (progress.get('run_session_id')!=run.name or not isinstance(endpoint,str)
                    or not re.fullmatch(r'127\.0\.0\.1:[0-9]{1,5}',endpoint)
                    or not 1<=int(endpoint.rsplit(':',1)[1])<=65535):continue
            # Only physical encoding changes, so missing old summaries and old
            # absolute output paths are irrelevant to JSON read compatibility.
            yield run,progress_path,endpoint


def _maintain_json(workspace,*,shared_backend,now=None,dry_run=False,max_seconds=2.0,max_files=20000,min_interval=3600):
    report=dict(dry_run=dry_run,removed=0,removed_bytes=0,compressed_files=0,
                logical_bytes=0,allocated_bytes_saved=0,budget_exhausted=False,
                errors=0,protected_runs=0,visited_files=0,indexed_directories=0,
                candidate_files=0,compression_skips={},native_error_codes={},_after_root='',_after_attempts={})
    if os.name!='nt':return {**report,'skipped':'unsupported_platform'}
    lock=None;read_scope=_READ_VERSIONS.set({})
    try:
        workspace=Path(workspace).absolute();shared_backend=Path(shared_backend).absolute()
        client=workspace.parent.parent
        if (workspace.name!='backend'or shared_backend!=client/'backend'
                or not _owned_path(workspace,client,client)
                or not _owned_path(shared_backend,client,client)):
            return {**report,'skipped':'invalid_compression_root'}
        manifest=_read(workspace.parent/'product-package-manifest.json')or{}
        if manifest.get('product_id')!='uma-cut':return {**report,'skipped':'not_packaged_backend'}
        if any(_io(p/'.git').exists()or _io(p/'.worktrees').exists()for p in (workspace.parent,client)):
            return {**report,'skipped':'source_tree_not_a_client'}
        runs=workspace/'runs'
        if not _io(runs).is_dir()or not _owned_path(runs,workspace,workspace):return report
        state=runs/'maintenance/pacer-json-retention.json'
        if _io(state.parent).exists()and not _owned_path(state.parent,runs,runs):
            return {**report,'skipped':'linked_maintenance_state'}
        if not dry_run:
            _io(state.parent).mkdir(parents=True,exist_ok=True)
            # Share exclusion with image maintenance, but not its scan cursor.
            lock=_lock(state.parent/'pacer-png-retention.lock')
            if lock is None:return {**report,'skipped':'maintenance_busy'}
        now=time.time()if now is None else now
        previous=_read(state)or{}
        if not dry_run and min_interval>0 and now<previous.get('retry_after',0):
            return {**report,'skipped':'recent_sweep'}
        walks=previous.get('_walks',{})if not dry_run else{}
        if not isinstance(walks,dict):walks={}
        after=previous.get('_after_root','')if not dry_run else''
        if not isinstance(after,str):after=''
        attempts=previous.get('_after_attempts',{})if not dry_run else{}
        if not isinstance(attempts,dict):attempts={}
        attempts={k:v for k,v in attempts.items()if isinstance(k,str)and isinstance(v,str)}
        report['_after_attempts']=dict(attempts)
        leaf=_storage_leaf('pacer_log_compression.py')
        if not leaf:return {**report,'skipped':'compression_module_unavailable'}
        deadline=time.monotonic()+max_seconds
        for run,proof,endpoint in _json_runs(runs,report,deadline,after,attempts):
            if not _json_idle(workspace,shared_backend,endpoint):
                report['protected_runs']+=1;continue
            guard=_read_token(proof);key=str(run.relative_to(runs));walk=walks.get(key)
            if not _valid_walk(walk):
                walk={'dirs':['.'],'entries':[],'index':0,'direct':[]}
                walks[key]=walk
            iterator=iter(_frames(run,walk,report,deadline,extensions={'.json'}))
            while report['candidate_files']<max_files:
                try:path=next(iterator)
                except StopIteration:break
                if path.name not in _LARGE_JSON or not _owned_path(path,run,runs):continue
                relative=path.relative_to(run)
                if (not relative.parts[0].startswith(_GENERATED)
                        or any(part in _EXCLUDED or part=='session'for part in relative.parts)):continue
                info=_io(path).stat()
                if now-info.st_mtime<ORDINARY_RETENTION_SECONDS:continue
                if not leaf['MIN_BYTES']<=info.st_size<=leaf['MAX_BYTES']:continue
                if getattr(info,'st_file_attributes',0)&0x800:
                    counts=report['compression_skips']
                    counts['already_compressed']=counts.get('already_compressed',0)+1
                    continue
                report['candidate_files']+=1
                if dry_run:continue
                if _token(proof)!=guard or not _json_idle(workspace,shared_backend,endpoint):
                    report['protected_runs']+=1;break
                result=leaf['compress_file'](path,expected=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns))
                status=result.get('status','compression_unavailable')
                if status=='compressed':
                    report['compressed_files']+=1;report['logical_bytes']+=result['logical_bytes']
                    report['allocated_bytes_saved']+=result['allocated_bytes_saved']
                else:
                    counts=report['compression_skips'];counts[status]=counts.get(status,0)+1
                    if status in {'compression_unavailable','compression_size_unavailable'}:
                        report['errors']+=1
                        code=result.get('winerror');codes=report['native_error_codes']
                        if type(code)is int and(code in codes or len(codes)<8):codes[code]=codes.get(code,0)+1
            else:report['budget_exhausted']=True
            if not(walk.get('dirs')or walk.get('entries')):walks.pop(key,None)
            if report['budget_exhausted']:break
        if not dry_run:
            for name in ('compressed_files','allocated_bytes_saved','logical_bytes'):
                value=previous.get(name+'_total',0)
                report[name+'_total']=(value if type(value)is int and value>=0 else 0)+report[name]
            stored={**report,'_walks':walks,'checked_at':now,
                    'retry_after':now+(60 if report['budget_exhausted']else min_interval)}
            if not _store(state,stored,runs):report['errors']+=1
    except Exception as error:
        report['errors']+=1
        logging.getLogger(__name__).warning('日志维护未完成：%s',type(error).__name__)
    finally:
        _READ_VERSIONS.reset(read_scope)
        if lock is not None:lock.close()
    return report


@contextmanager
def read_access_for_run(run_root,*,max_wait_seconds=10.0):
    """Serialize installed-run export reads with a retention slice.

    Source trees do not enable installed housekeeping and need no read mutex.
    Nested export/feedback readers on the same thread share one native handle.
    """
    workspace=Path(__file__).absolute().parents[2]
    leaf=_storage_leaf('pacer_run_retention.py')
    run=Path(run_root).absolute()
    if (leaf is None or not leaf['_installed'](workspace,workspace.parent.parent/'backend')
            or not run.is_relative_to(workspace/'runs')):
        yield
        return
    if not leaf['_plain'](run):raise ValueError('linked_run_read_path')
    path=workspace/'runs/maintenance/pacer-run-retention.lock'
    held=_RUN_READ_ACCESS.get()or{}
    key=str(path)
    existing=held.get(key)
    if existing is not None and existing[0]==threading.get_ident()and not existing[1].closed:
        opened=os.fstat(existing[1].fileno());named=leaf['_stat'](path)
        if (opened.st_dev,opened.st_ino)!=(named.st_dev,named.st_ino)or not leaf['_plain'](path):
            raise ValueError('run_read_mutex_changed')
        yield
        return
    deadline=time.monotonic()+max(0,max_wait_seconds)
    while True:
        try:
            stream=leaf['_take_lock'](path)
            break
        except OSError:
            if time.monotonic()>=deadline:raise RuntimeError('pacer_run_maintenance_busy')from None
            time.sleep(min(.02,max(0,deadline-time.monotonic())))
    token=_RUN_READ_ACCESS.set({**held,key:(threading.get_ident(),stream)})
    try:yield
    finally:
        _RUN_READ_ACCESS.reset(token)
        stream.close()


_PRUNE_NORMAL_OUTCOMES = frozenset({
    'completed',
    'graduated',
    'unqualified',
    'screened_out',
    'abandoned',
    'mei_unlock_failed',
    'training_failed',
})
_PRUNE_JSON_RE = re.compile(r'^(?:frame|ocr)_.*\.json$')


def prune_completed_attempt(attempt_root, *, outcome='completed', dry_run=False):
    """Prune intermediate turn PNGs and large JSONs after a normal/screened attempt ends.

    Abnormal stops/failures are intentionally preserved for post-mortem triage.
    """
    report = {
        'schema_version': 'pacer.attempt_prune.v1',
        'status': 'skipped',
        'outcome': str(outcome or ''),
        'removed_files': 0,
        'removed_bytes': 0,
        'kept_protected_images': 0,
        'errors': 0,
    }
    if outcome not in _PRUNE_NORMAL_OUTCOMES:
        report['status'] = 'skipped_abnormal_outcome'
        return report
    try:
        root = Path(attempt_root).absolute()
        if not _io(root).is_dir() or _linked(root):
            report['status'] = 'unavailable'
            return report
        # Ownership check: attempt_root must live under a runs directory with no reparse links.
        runs_ancestor = next((p for p in root.parents if p.name == 'runs'), None)
        if runs_ancestor is None or not _owned_path(root, runs_ancestor, runs_ancestor):
            report['status'] = 'outside_runs'
            return report
        has_marker = (
            _io(root / 'progress.json').is_file()
            or _io(root / 'session/pacer_center_run.json').is_file()
            or any(_io(p).is_dir() for p in _glob(root, 'autonomous_*'))
        )
        if not has_marker:
            report['status'] = 'not_attempt_root'
            return report

        protected_images = set()
        terminal_state = _read(root / 'session/larc_terminal_settlement.json')
        if isinstance(terminal_state, dict):
            rp = terminal_state.get('report_path')
            if isinstance(rp, str) and rp:
                rpath = Path(rp).absolute()
                if rpath.is_relative_to(root) and _io(rpath).is_file():
                    trep = _read(rpath)
                    if isinstance(trep, dict):
                        protected_images.update(_refs(trep, rpath.parent, root))

        prune_roots = []
        for entry in _glob(root, '*'):
            nm = entry.name
            if not _io(entry).is_dir() or _linked(entry):
                continue
            if nm in {'exports', 'review_exports', 'session', 'skill_policy'}:
                continue
            if nm.startswith('autonomous_'):
                for sub in _glob(entry, '*'):
                    if not _io(sub).is_dir() or _linked(sub):
                        continue
                    if sub.name.startswith(('turn_', 'capture', 'training_review')):
                        prune_roots.append(sub)
            elif nm == 's':
                for batch in _glob(entry, 'batch_*'):
                    if not _io(batch).is_dir() or _linked(batch):
                        continue
                    for subname in ('capture', 'transaction'):
                        sub = batch / subname
                        if _io(sub).is_dir() and not _linked(sub):
                            prune_roots.append(sub)
            elif nm == 'z':
                for sub in _glob(entry, 'capture*'):
                    if _io(sub).is_dir() and not _linked(sub):
                        prune_roots.append(sub)
            elif nm.startswith(('cut_restart', 'ss_sieve_abandon')):
                prune_roots.append(entry)

        pruned_dirs = []
        for p_root in prune_roots:
            for item in _rglob(p_root, '*'):
                if not _io(item).is_file():
                    continue
                try:
                    if not _owned_path(item, root, p_root):
                        continue
                    rel = item.relative_to(root)
                    if any(part in _EXCLUDED or part == 'session' for part in rel.parts):
                        continue
                    resolved = item.resolve()
                    if resolved in protected_images:
                        report['kept_protected_images'] += 1
                        continue
                    suf = item.suffix.lower()
                    nm = item.name
                    is_target = (
                        suf in _IMAGE
                        or nm in _LARGE_JSON
                        or nm == 'capture_manifest.json'
                        or bool(_PRUNE_JSON_RE.match(nm))
                    )
                    if not is_target:
                        continue
                    sz = _io(item).stat().st_size
                    if not dry_run:
                        _io(item).unlink()
                    report['removed_files'] += 1
                    report['removed_bytes'] += sz
                except OSError:
                    report['errors'] += 1
            pruned_dirs.append(p_root)

        if not dry_run:
            for p_root in pruned_dirs:
                for sub in sorted(_rglob(p_root, '*'), key=lambda p: len(p.parts), reverse=True):
                    if _io(sub).is_dir() and not _linked(sub):
                        try:
                            if not any(_io(sub).iterdir()):
                                _io(sub).rmdir()
                        except OSError:
                            pass
                if _io(p_root).is_dir() and not _linked(p_root):
                    try:
                        if not any(_io(p_root).iterdir()):
                            _io(p_root).rmdir()
                    except OSError:
                        pass
            session_dir = root / 'session'
            if _io(session_dir).is_dir() and not _linked(session_dir):
                receipt_path = session_dir / 'attempt_prune_receipt.json'
                try:
                    tmp = receipt_path.with_name(f'.attempt_prune.{uuid.uuid4().hex[:8]}.tmp')
                    _io(tmp).write_text(
                        json.dumps({**report, 'status': 'pruned', 'recorded_at': int(time.time())},
                                   ensure_ascii=False, separators=(',', ':')),
                        encoding='utf-8',
                    )
                    os.replace(_io(tmp), _io(receipt_path))
                except OSError:
                    pass
        report['status'] = 'dry_run' if dry_run else 'pruned'
    except Exception:
        report['errors'] += 1
        report['status'] = 'error'
    return report


def maintain(workspace,*,now=None,dry_run=False,max_seconds=2.0,max_files=20000,min_interval=3600,
             _completed_run=None,json_only=False,shared_backend=None,background_json=False,
             exclude_run=None,exclude_batch=None,runtime_owner=None):
    if json_only:
        # Compatibility for an already-started legacy compression worker. The
        # whole-run expiry has a single foreground owner, so the displayed
        # deletion counters cannot lose removals made by a second process.
        return _maintain_json(workspace,shared_backend=shared_backend,now=now,dry_run=dry_run,
            max_seconds=max_seconds,max_files=max_files,min_interval=min_interval)
    deadline=time.monotonic()+max(0,max_seconds)
    report={'removed':0,'removed_bytes':0,'errors':0,'budget_exhausted':False,
            'byte_count_basis':'logical_file_lengths','physical_bytes_released':None}
    try:
        leaf=_storage_leaf('pacer_run_retention.py')
        retention=leaf['maintain'](workspace,shared_backend=shared_backend,now=now,dry_run=dry_run,
            max_seconds=max(0,deadline-time.monotonic()),max_files=max_files,min_interval=min_interval,
            exclude_run=exclude_run,exclude_batch=exclude_batch,_deadline=deadline,runtime_owner=runtime_owner,
            _claim_ui_counts=bool(background_json and shared_backend is not None and runtime_owner is None)) if leaf else {
                'status':'unavailable','errors':1,'budget_exhausted':False}
    except Exception:
        retention={'status':'unavailable','errors':1,'budget_exhausted':False}
    report['run_retention']=retention
    # These legacy fields count image-only deletion. All new whole-run files
    # and bytes are counted exactly once under run_retention.
    report['errors']=retention.get('errors',0)
    report['budget_exhausted']=retention.get('budget_exhausted',False)
    # The old four/twelve-hour image-only deleter is superseded by the run
    # policy. In particular unavailable policy data authorizes no deletion.
    # Four-hour compression is redundant for ordinary evidence that now
    # expires after two hours. Existing foreground lifecycle/watch callbacks
    # keep expiry moving without launching another maintenance process.
    return report
