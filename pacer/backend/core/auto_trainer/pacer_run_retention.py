"""Bounded installed-client expiry of historical Pacer run evidence.

Only this module's durable intent registry owns quarantine directories. Device
locks are deliberately independent native handles, including in a runner that
already holds its reentrant execution lease. Byte counts are logical lengths.
"""
from contextlib import contextmanager
from pathlib import Path
import json
import math
import os
import re
import stat
import time
import uuid

SCHEMA = 'pacer.run_retention.v1'
POLICY = 'maauma.pacer_storage_policy.v1'
_KEEP_DIRS = {'exports', 'review_exports'}
_ACTIVE = {'starting', 'running', 'paused', 'pause_requested', 'resume_requested',
           'stop_requested', 'unresponsive'}


def _io(path):
    value = str(Path(path).absolute())
    if os.name == 'nt' and not value.startswith('\\\\?\\'):
        value = '\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value
    return Path(value)


def _stat(path):
    return _io(path).lstat()


def _token(path):
    info = _stat(path)
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def _plain(path):
    """Reject every existing reparse ancestor, including dangling links."""
    path = Path(path).absolute()
    for part in (path, *path.parents):
        try:
            info = _stat(part)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            return False
    return True


def _read(path, limit=262144, *, single_link=True):
    if not _plain(path):
        raise ValueError('linked_json')
    try:
        before = _token(path)
    except FileNotFoundError:
        return None
    if before[2] > limit or single_link and _stat(path).st_nlink != 1:
        raise ValueError('json_not_small_owned_file')
    value = json.loads(_io(path).read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict) or _token(path) != before:
        raise ValueError('unstable_json')
    return value


def _write(path, value):
    if not _plain(path):
        raise ValueError('linked_state')
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with _io(temporary).open('x', encoding='utf8') as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        if not _plain(path):
            raise ValueError('linked_state')
        os.replace(_io(temporary), _io(path))
    finally:
        if _plain(temporary) and _io(temporary).exists():
            _io(temporary).unlink()


def _take_lock(path):
    if not _plain(path):
        raise ValueError('linked_lock')
    _io(path.parent).mkdir(parents=True, exist_ok=True)
    stream = _io(path).open('a+b')
    try:
        if _stat(path).st_nlink != 1:
            raise ValueError('linked_lock')
        if not _stat(path).st_size:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return stream
    except BaseException:
        stream.close()
        raise


@contextmanager
def _device_locks(workspace, shared, endpoint, owner=None):
    base = os.environ.get('UMA_PUBLIC_LOCK_ROOT') or os.environ.get('LOCALAPPDATA')
    if not base:
        raise OSError('device_lock_root_unavailable')
    folders = [Path(base) / 'UmaSuite/device-locks', workspace / 'runs/device-occupancy',
               shared / 'runs/device-occupancy']
    handles = []
    try:
        for folder in dict.fromkeys(folders):
            path=folder / (endpoint.replace(':', '_') + '.lock')
            if owner is not None:
                if not owner.valid():raise OSError('runtime_owner_expired')
                if endpoint==owner.endpoint and path.absolute()==owner.lease.canonical_path:
                    continue
            handles.append(_take_lock(path))
        if owner is not None and not owner.valid():raise OSError('runtime_owner_expired')
        yield
    finally:
        for handle in reversed(handles):
            handle.close()


def _policy(workspace):
    path = workspace.parent.parent / 'config/pacer-storage-policy.json'
    value = _read(path)
    if value is None:
        return 120
    if value.get('schema_version') != POLICY or type(value.get('retention_minutes')) is not int or value['retention_minutes'] not in (30, 120):
        raise ValueError('storage_policy_unavailable')
    return value['retention_minutes']


def _installed(workspace, shared):
    if workspace.name != 'backend' or workspace.parent.name != 'pacer':
        return False
    if shared != workspace.parent.parent / 'backend' or not _plain(workspace) or not _plain(shared):
        return False
    if Path(__file__).absolute() != workspace / 'core/auto_trainer/pacer_run_retention.py':
        return False
    for part in (workspace, *workspace.parents):
        if (_io(part / '.git')).exists() or (_io(part / '.worktrees')).exists():
            return False
    # Immutable installed metadata may be hardlinked to updater staging or a
    # rollback copy. Reading its identity never deletes or writes those links.
    manifest = _read(workspace.parent / 'product-package-manifest.json', 16777216, single_link=False)
    return bool(manifest and manifest.get('product_id') == 'uma-cut')


def _endpoint(value):
    return isinstance(value, str) and bool(re.fullmatch(r'127\.0\.0\.1:[0-9]{1,5}', value)) and 1 <= int(value.rsplit(':', 1)[1]) <= 65535


def _created_order(run):
    match = re.search(r'(20\d{6}_\d{6})', run.name)
    if match:
        return match.group(1)
    compact = re.match(r'(?:cut_pacer|larc_panel)_(\d{6})(\d{6})_', run.name)
    if compact:
        return f'20{compact.group(1)}_{compact.group(2)}'
    return str(getattr(_stat(run), 'st_birthtime', _stat(run).st_ctime)).zfill(24)


def _batch_identity(run):
    """Production writes schema/config before the potentially enormous attempts.

    Decode complete leading JSON fields only. The rest of the batch journal is
    evidence to expire, not an identity document that housekeeping must load.
    """
    path = run / 'cut_farming.json'
    if not _plain(path):
        raise ValueError('linked_batch_identity')
    try:
        before = _token(path)
    except FileNotFoundError:
        return None
    if _stat(path).st_nlink != 1:
        raise ValueError('linked_batch_identity')
    with _io(path).open('r', encoding='utf-8-sig') as stream:
        prefix = stream.read(262144)
    decoder = json.JSONDecoder()
    fields = {}
    index = len(prefix) - len(prefix.lstrip())
    if prefix[index:index + 1] != '{':
        return None
    index += 1
    while index < len(prefix):
        index += len(prefix[index:]) - len(prefix[index:].lstrip())
        if prefix[index:index + 1] in ('}', ''):
            break
        try:
            key, index = decoder.raw_decode(prefix, index)
            index += len(prefix[index:]) - len(prefix[index:].lstrip())
            if not isinstance(key, str) or prefix[index:index + 1] != ':':
                return None
            index += 1
            index += len(prefix[index:]) - len(prefix[index:].lstrip())
            value, index = decoder.raw_decode(prefix, index)
        except ValueError:
            break
        if key in {'schema_version', 'config'}:
            fields[key] = value
        if {'schema_version', 'config'} <= fields.keys():
            break
        index += len(prefix[index:]) - len(prefix[index:].lstrip())
        if prefix[index:index + 1] != ',':
            break
        index += 1
    if _token(path) != before:
        raise ValueError('batch_identity_changed')
    config = fields.get('config')
    port = config.get('device_port') if isinstance(config, dict) else None
    if fields.get('schema_version') != 'uma.cut_farming_session.v1' or type(port) is not int or not 1 <= port <= 65535:
        return None
    created = _created_order(run)
    return {'endpoint': f'127.0.0.1:{port}', 'guards': {'cut_farming.json': before},
            'inode': _token(run)[:2], 'order': created + '/' + run.name, 'kind': 'batch'}


def _identity(run):
    if run.parent.name == 'runs' and run.name.startswith('cut_pacer_'):
        return _batch_identity(run)
    progress = _read(run / 'progress.json')
    marker = _read(run / 'session/pacer_center_run.json')
    if progress is None and marker is None:
        return None
    endpoint = (progress or {}).get('device', (marker or {}).get('endpoint'))
    if not _endpoint(endpoint):
        return None
    if progress is not None and (progress.get('run_session_id') != run.name or progress.get('device') != endpoint):
        return None
    if marker is not None and (marker.get('source') != 'private_pacer_center' or marker.get('mode') != 'career' or marker.get('run_id') != run.name or marker.get('endpoint') != endpoint):
        return None
    guards = {}
    for name in ('progress.json', 'session/pacer_center_run.json'):
        try:
            guards[name] = _token(run / name)
        except FileNotFoundError:
            guards[name] = None
    # Production names contain their creation timestamp; do not choose the most
    # recently touched old waiting state as the newest career.
    created = _created_order(run)
    return {'endpoint': endpoint, 'guards': guards, 'inode': _token(run)[:2],
            'order': created + '/' + run.name}


def _manual(run, endpoint, workspace):
    terminal = _read(run / 'session/larc_terminal_settlement.json')
    if (terminal and terminal.get('run_session_id') == run.name and terminal.get('device') == endpoint
            and (terminal.get('status') == 'complete' or terminal.get('phase') == 'complete')):
        return False
    for name in ('larc_manual_pause_state.json', 'larc_manual_handoff_state.json'):
        value = _read(run / 'session' / name)
        if value is not None:
            if value.get('run_session_id') != run.name or value.get('device') != endpoint:
                raise ValueError('manual_identity_mismatch')
            if value.get('stage') == 'final_arc' and value.get('status') == 'handed_off':
                return False
            if value.get('status') == 'waiting' and value.get('stage') in {'first_arc', 'date'}:
                return True
            break
    control = _read(workspace / 'runs/umaa-panel-control' / endpoint.rsplit(':', 1)[1] / 'control_state.json')
    result = (control or {}).get('last_result')
    return bool(control and control.get('state') == 'stopped' and isinstance(result, dict)
                and result.get('outcome') == 'stopped' and result.get('reason') == 'user_stop')


def _current(workspace, run, endpoint):
    directory = workspace / 'runs/umaa-panel-control' / endpoint.rsplit(':', 1)[1]
    value = _read(directory / 'control_state.json')
    # A crashed host can leave paused/unresponsive on disk forever. Actual
    # execution (including a paused runner) is also excluded by native locks;
    # this recent heartbeat covers only the startup window before that lease.
    heartbeat=(value or {}).get('heartbeat_at')
    fresh=(type(heartbeat)in(int,float)and math.isfinite(heartbeat)
           and 0<=time.monotonic()-heartbeat<=60)
    return bool(value and value.get('schema_version')=='ug_parent_dashboard.v2.control_state'
                and fresh and(value.get('state') in _ACTIVE or value.get('tap_in_flight') is True))


def _excluded(run, excludes):
    return any(run == item or run.is_relative_to(item) or item.is_relative_to(run) for item in excludes)


class _RuntimeOwner:
    def __init__(self,workspace,value):
        from core.cut_portable_lease import current_native_lease
        self.workspace=workspace
        self.endpoint=value.get('endpoint')
        if not _endpoint(self.endpoint):raise ValueError('runtime_endpoint_invalid')
        self.run=Path(value['run_root']).absolute()
        if (not _plain(self.run)or not self.run.is_relative_to(workspace/'runs')
                or self.run==workspace/'runs'):
            raise ValueError('runtime_run_invalid')
        self.identity=_identity(self.run)
        if not self.identity or self.identity.get('kind')=='batch' or self.identity['endpoint']!=self.endpoint:
            raise ValueError('runtime_run_identity_invalid')
        batch=value.get('batch_root')
        self.batch=Path(batch).absolute()if batch is not None else None
        self.batch_identity=None
        if self.run.parent.name=='attempts':
            if self.batch!=self.run.parent.parent:raise ValueError('runtime_batch_invalid')
            self.batch_identity=_batch_identity(self.batch)
            if not self.batch_identity or self.batch_identity['endpoint']!=self.endpoint:
                raise ValueError('runtime_batch_identity_invalid')
        elif self.batch is not None or self.run.parent!=workspace/'runs':
            raise ValueError('runtime_run_location_invalid')
        pins=value.get('pins',[])
        if not isinstance(pins,(list,tuple))or len(pins)>8:raise ValueError('runtime_pins_invalid')
        self.pins=[]
        for pin in pins:
            path=Path(pin).absolute()
            managed=workspace/'runs'
            # Templates outside managed runs cannot be deleted by this
            # cleaner. Ignore them without reading or granting outside access;
            # backend/collection roots must not pin every historical career.
            if not path.is_relative_to(managed)or path==managed:continue
            if '..'in path.relative_to(managed).parts or not _plain(path):
                raise ValueError('runtime_pin_invalid')
            self.pins.append(path)
        self.lease=current_native_lease(workspace/'runs/device-occupancy',self.endpoint)
        if self.lease is None or not self.valid():raise ValueError('runtime_native_lease_unavailable')

    def valid(self):
        try:
            return (self.lease is not None and self.lease.valid()
                and _identity(self.run)==self.identity
                and(self.batch is None or _batch_identity(self.batch)==self.batch_identity))
        except (OSError,ValueError,TypeError,AttributeError):return False

    def protects(self,run):
        # The current batch is exact: protecting it must not exempt its older
        # attempts. Current run and template subtrees/containing roots are pinned.
        return (run==self.batch or run==self.run or self.run.is_relative_to(run)
            or any(run==pin or pin.is_relative_to(run)or run.is_relative_to(pin)for pin in self.pins))


def _current_for_access(workspace,run,endpoint,owner):
    if owner is not None and owner.endpoint==endpoint and owner.valid():return False
    return _current(workspace,run,endpoint)


def _restore(target,source,owner):
    if owner is not None and not owner.valid():raise OSError('runtime_owner_expired')
    if not _plain(source)or _io(source).exists():raise ValueError('source_recreated')
    os.rename(_io(target),_io(source))


def _rel(value):
    path = Path(value)
    if path.is_absolute() or '..' in path.parts or not value or ':' in value:
        raise ValueError('invalid_relative_path')
    return path


def _step(directory, cursor, deadline, report=None):
    """Persist one directory's unsorted name snapshot and its next index.

    A single directory snapshot is the only soft-budget operation: it never
    stats/sorts descendants. Subsequent calls do not replay the scanned prefix.
    """
    if not _plain(directory):
        raise ValueError('linked_directory')
    token = _token(directory)
    if time.monotonic() >= deadline:
        return None, False
    if cursor.get('token') != token or not isinstance(cursor.get('names'), list):
        with os.scandir(_io(directory)) as entries:
            names = [entry.name for entry in entries]
        cursor.update(token=token, names=names, index=0)
        if time.monotonic() >= deadline:
            cursor['snapshot_budget_overruns'] = cursor.get('snapshot_budget_overruns', 0) + 1
            if report is not None:
                report['directory_snapshot_budget_overruns'] += 1
            return None, False
    index = cursor.get('index', 0)
    if index >= len(cursor['names']):
        return None, True
    cursor['index'] = index + 1
    return cursor['names'][index], False


def _discovery(workspace, state, deadline, report):
    runs = workspace / 'runs'
    discovery = state.setdefault('discovery', {'stack': [{'rel': '.', 'cursor': {}}], 'catalog': {}})
    while discovery['stack'] and time.monotonic() < deadline:
        frame = discovery['stack'][-1]
        directory = runs / frame['rel']
        name, done = _step(directory, frame['cursor'], deadline, report)
        if done:
            discovery['stack'].pop()
            continue
        if name is None:
            break
        child = directory / name
        if not _plain(child) or not stat.S_ISDIR(_stat(child).st_mode):
            continue
        if frame['rel'] == '.':
            if name.startswith('cut_pacer_'):
                try:
                    identity = _batch_identity(child)
                    if identity:
                        discovery['catalog'][str(child.relative_to(runs))] = identity
                except (OSError, ValueError, TypeError):
                    report['errors'] += 1
                attempts = child / 'attempts'
                if _plain(attempts) and _io(attempts).is_dir():
                    discovery['stack'].append({'rel': str(attempts.relative_to(runs)), 'cursor': {}})
                continue
            if not name.startswith('larc_panel_'):
                continue
        try:
            identity = _identity(child)
            if identity:
                discovery['catalog'][str(child.relative_to(runs))] = identity
        except (OSError, ValueError, TypeError):
            report['errors'] += 1
    if discovery['stack']:
        report['budget_exhausted'] = True
        return False
    state['catalog'] = discovery['catalog']
    state.pop('discovery', None)
    return True


def _keep(relative):
    path = Path(relative)
    return any(part.casefold() in _KEEP_DIRS for part in path.parts)


def _new_scan(run):
    return {'inode': _token(run)[:2], 'stack': [{'rel': '.', 'cursor': {}}],
            'max_mtime': 0, 'leaves': [], 'dirs': [], 'complete': False}


def _scan(run, scan, deadline, report, max_files):
    if _token(run)[:2] != scan['inode']:
        raise ValueError('run_replaced')
    while scan['stack'] and time.monotonic() < deadline and report['visited_files'] < max_files:
        frame = scan['stack'][-1]
        directory = run / _rel(frame['rel'])
        before = frame['cursor'].get('token')
        name, done = _step(directory, frame['cursor'], deadline, report)
        # A directory changed while suspended: discard the complete scan, since
        # previously enumerated children may no longer describe this tree.
        if before is not None and before != frame['cursor'].get('token'):
            raise ValueError('directory_changed')
        if done:
            scan['dirs'].append([frame['rel'], _token(directory)])
            scan['stack'].pop()
            continue
        if name is None:
            break
        child = directory / name
        if not _plain(child):
            raise ValueError('linked_run_leaf')
        info = _stat(child)
        relative = str(child.relative_to(run))
        if stat.S_ISDIR(info.st_mode):
            scan['stack'].append({'rel': relative, 'cursor': {}})
        elif stat.S_ISREG(info.st_mode):
            scan['max_mtime'] = max(scan['max_mtime'], info.st_mtime)
            scan['leaves'].append([relative, _token(child)])
            report['visited_files'] += 1
        else:
            raise ValueError('unsupported_run_leaf')
    scan['complete'] = not scan['stack']
    if not scan['complete']:
        report['budget_exhausted'] = True
    return scan['complete']


def _intent_paths(runs, intent):
    source = runs / _rel(intent['source'])
    target = runs / '.pacer-expired' / intent['id']
    if not re.fullmatch(r'[a-f0-9]{32}', intent['id']) or not _plain(target) or not _plain(source):
        raise ValueError('invalid_quarantine')
    return source, target


def _drain(runs, state, save, deadline, report, now, ttl, max_files, excludes, owner=None):
    for intent in list(state['intents']):
        if time.monotonic() >= deadline:
            report['budget_exhausted'] = True
            return
        try:
            if owner is not None and not owner.valid():
                report['protected_busy_runs']+=1
                return
            source, target = _intent_paths(runs, intent)
            if _excluded(source, excludes)or owner is not None and owner.protects(source):
                report['protected_excluded_runs'] += 1
                continue
            if not _io(target).exists():
                # Prepared before rename: forget only the intent. Never delete
                # or rename its original path during crash recovery.
                state['intents'].remove(intent)
                save()
                continue
            if _token(target)[:2] != intent['inode']:
                raise ValueError('quarantine_replaced')
            if intent['phase'] == 'prepared':
                intent['phase'] = 'verify'
                intent['verify'] = _new_scan(target)
                save()
            if intent['phase'] == 'verify':
                if not _scan(target, intent['verify'], deadline, report, max_files):
                    continue
                verify = intent['verify']
                if verify['max_mtime'] > now - ttl:
                    if not _io(source).exists():
                        _restore(target,source,owner)
                        state['intents'].remove(intent)
                        save()
                    else:
                        raise ValueError('source_recreated')
                    continue
                intent['phase'] = 'delete'
                intent['leaves'] = verify['leaves']
                intent['dirs'] = sorted((item[0] for item in verify['dirs']), key=lambda item: len(Path(item).parts), reverse=True)
                intent.pop('verify', None)
                save()
            while intent.get('leaves') and time.monotonic() < deadline and report['visited_files'] < max_files:
                relative, token = intent['leaves'][0]
                child = target / _rel(relative)
                if not _keep(relative):
                    try:
                        if not _plain(child) or _token(child) != token or _stat(child).st_mtime > now - ttl:
                            raise ValueError('quarantine_leaf_changed')
                        _io(child).unlink()
                        report['removed_files'] += 1
                        report['removed_bytes'] += token[2]
                    except FileNotFoundError:
                        pass
                intent['leaves'].pop(0)
                report['visited_files'] += 1
            if intent.get('leaves'):
                report['budget_exhausted'] = True
                continue
            while intent.get('dirs') and time.monotonic() < deadline:
                relative = intent['dirs'].pop(0)
                child = target / _rel(relative)
                if not _plain(child):
                    raise ValueError('quarantine_directory_changed')
                try:
                    _io(child).rmdir()
                except OSError as error:
                    if getattr(error, 'winerror', None) not in (145,) and getattr(error, 'errno', None) not in (39, 66):
                        raise
            if intent.get('dirs'):
                report['budget_exhausted'] = True
                continue
            if _io(target).exists():
                # Only preserved exports/program/config remain. Restore their
                # small root; a new real run at source always wins the name.
                if _io(source).exists():
                    raise ValueError('preserved_source_conflict')
                _restore(target,source,owner)
                report['preserved_export_roots'] += 1
            state['intents'].remove(intent)
            report['removed_runs'] += 1
            save()
        except (OSError, ValueError, TypeError, KeyError):
            report['errors'] += 1
            # Failed entries remain registered; continue other historical runs.
            save()


def maintain(workspace, *, shared_backend=None, now=None, dry_run=False, max_seconds=2,
             max_files=20000, min_interval=3600, exclude_run=None, exclude_batch=None, _deadline=None,
             runtime_owner=None, _claim_ui_counts=False):
    report = dict(status='completed', retention_minutes=None, removed_runs=0,
                  removed_files=0, removed_bytes=0, protected_current_runs=0,
                  protected_manual_runs=0, protected_busy_runs=0, protected_excluded_runs=0,
                  preserved_export_roots=0, visited_files=0, errors=0, budget_exhausted=False,
                  directory_snapshot_budget_overruns=0,
                  byte_count_basis='logical_file_lengths', physical_bytes_released=None,
                  watch_enabled=False)
    deadline = min(_deadline or float('inf'), time.monotonic() + max(0, max_seconds))
    lock = None
    try:
        workspace = Path(workspace).absolute()
        shared = Path(shared_backend).absolute() if shared_backend is not None else workspace.parent.parent / 'backend'
        if not _installed(workspace, shared):
            return {**report, 'status': 'unavailable', 'reason': 'not_installed_backend'}
        minutes = _policy(workspace)
        report['retention_minutes'] = minutes
        report['watch_enabled'] = True
        ttl = minutes * 60
        now = time.time() if now is None else now
        runs = workspace / 'runs'
        if not _plain(runs):
            raise ValueError('linked_runs')
        if not _io(runs).is_dir():
            return {**report, 'status': 'no_runs'}
        if dry_run:
            return {**report, 'status': 'dry_run'}
        owner=None
        if runtime_owner is not None:
            try:
                if not isinstance(runtime_owner,dict):raise ValueError('runtime_owner_invalid')
                owner=_RuntimeOwner(workspace,runtime_owner)
            except (OSError,ValueError,TypeError,KeyError,ImportError):
                return {**report,'status':'busy','reason':'runtime_owner_unavailable','protected_busy_runs':1}
        directory = runs / 'maintenance'
        quarantine = runs / '.pacer-expired'
        if not _plain(directory) or not _plain(quarantine):
            raise ValueError('linked_maintenance')
        _io(directory).mkdir(exist_ok=True)
        path = directory / 'pacer-run-retention.json'
        try:
            lock = _take_lock(directory / 'pacer-run-retention.lock')
        except OSError:
            return {**report, 'status': 'busy'}
        state = _read(path, 67108864) or {'schema_version': SCHEMA, 'intents': [], 'scans': {}}
        if state.get('schema_version') != SCHEMA:
            raise ValueError('maintenance_state_unavailable')
        if state.get('retention_minutes') != minutes:
            state['scans'] = {}
            state['retention_minutes'] = minutes
        accounted={key:0 for key in ('removed_runs','removed_files','removed_bytes')}
        pending=state.setdefault('unclaimed_retention',{})
        if not isinstance(pending,dict)or any(type(pending.get(key,0))is not int or pending.get(key,0)<0 for key in accounted):
            raise ValueError('maintenance_counts_unavailable')
        def save():
            for key in accounted:
                pending[key]=pending.get(key,0)+max(0,report[key]-accounted[key])
                accounted[key]=report[key]
            _write(path,state)
        excludes = [Path(value).absolute() for value in (exclude_run, exclude_batch) if value is not None
                    and not(owner is not None and owner.batch is not None and Path(value).absolute()==owner.batch)]
        # Recovery is registry-only; unknown quarantine names are never visited.
        _drain(runs, state, save, deadline, report, now, ttl, max_files, excludes, owner)
        if time.monotonic() >= deadline:
            report['budget_exhausted'] = True
        elif _discovery(workspace, state, deadline, report):
            catalog = state.get('catalog', {})
            latest = {}
            for relative, identity in catalog.items():
                if identity.get('kind') == 'batch':
                    continue
                endpoint = identity['endpoint']
                if endpoint not in latest or identity['order'] > catalog[latest[endpoint]]['order']:
                    latest[endpoint] = relative
            after = state.get('after', '')
            keys = list(catalog)
            if after in keys:
                index = keys.index(after) + 1
                keys = keys[index:] + keys[:index]
            for relative in keys:
                if time.monotonic() >= deadline or report['visited_files'] >= max_files:
                    report['budget_exhausted'] = True
                    break
                run = runs / _rel(relative)
                identity = catalog[relative]
                endpoint = identity['endpoint']
                try:
                    if _excluded(run, excludes)or owner is not None and owner.protects(run):
                        report['protected_excluded_runs'] += 1
                        state['after'] = relative
                        continue
                    if identity.get('kind') == 'batch' and any(
                            Path(child).is_relative_to(Path(relative) / 'attempts')
                            and _io(runs / child).exists() for child in catalog if child != relative):
                        # Expire attempts independently first. A current/manual
                        # attempt keeps its parent batch intact; exports-only
                        # residual roots have no runnable identity next sweep.
                        state['after'] = relative
                        continue
                    if _current_for_access(workspace, run, endpoint, owner):
                        report['protected_current_runs'] += 1
                        state['after'] = relative
                        continue
                    if latest.get(endpoint) == relative and _manual(run, endpoint, workspace):
                        report['protected_manual_runs'] += 1
                        state['after'] = relative
                        continue
                    scan = state['scans'].get(relative)
                    if scan and now < scan.get('retry_after', 0):
                        state['after'] = relative
                        continue
                    if scan is None or scan.get('complete'):
                        scan = state['scans'][relative] = _new_scan(run)
                    if not _scan(run, scan, deadline, report, max_files):
                        break
                    if scan['max_mtime'] > now - ttl:
                        state['scans'][relative] = {'complete': True,
                            'retry_after': scan['max_mtime'] + ttl}
                        state['after'] = relative
                        continue
                    # Keep native locks only for the final ownership and
                    # freshness recheck plus atomic rename; never for deletion.
                    try:
                        with _device_locks(workspace, shared, endpoint, owner):
                            if _identity(run) != identity or _current_for_access(workspace, run, endpoint, owner):
                                raise ValueError('run_identity_changed')
                            if latest.get(endpoint) == relative and _manual(run, endpoint, workspace):
                                raise ValueError('manual_changed')
                            _io(quarantine).mkdir(exist_ok=True)
                            intent = {'id': uuid.uuid4().hex, 'source': relative, 'inode': identity['inode'], 'phase': 'prepared'}
                            state['intents'].append(intent)
                            save()  # durable unique source inode/target before rename
                            if not _plain(run) or _token(run)[:2] != identity['inode'] or not _plain(quarantine):
                                raise ValueError('rename_race')
                            if owner is not None and not owner.valid():raise OSError('runtime_owner_expired')
                            os.rename(_io(run), _io(quarantine / intent['id']))
                            state['scans'].pop(relative, None)
                    except OSError:
                        report['protected_busy_runs'] += 1
                    state['after'] = relative
                    _drain(runs, state, save, deadline, report, now, ttl, max_files, excludes, owner)
                except FileNotFoundError:
                    state['scans'].pop(relative, None)
                    state['after'] = relative
                except (OSError, ValueError, TypeError):
                    report['errors'] += 1
                    state['scans'].pop(relative, None)
                    state['after'] = relative
        save()
        if _claim_ui_counts:
            claimed={key:pending.get(key,0)for key in accounted}
            # Clear durably before exposing a claim; a failed write returns no
            # already-counted runtime removals to the foreground accumulator.
            for key in accounted:report[key]=0
            state['unclaimed_retention']={}
            _write(path,state)
            report.update(claimed)
        if time.monotonic() >= deadline:
            report['budget_exhausted'] = True
        if report['budget_exhausted']:
            report['status'] = 'partial'
        elif report['errors']:
            report['status'] = 'partial'
    except (OSError, ValueError, TypeError, KeyError):
        report.update(status='unavailable', errors=report['errors'] + 1, watch_enabled=False)
    finally:
        if lock is not None:
            lock.close()
    return report
