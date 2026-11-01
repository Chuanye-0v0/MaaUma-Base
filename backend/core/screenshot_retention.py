"""Daily working captures without a process-image archive.

Keep a rolling observation window and the original owner's unconsumed skill or
resource proof only; release it after acknowledgement/settlement. Named journals
and receipts remain. Direct legacy stores still support archive maintenance.
No Pacer capture, historical-tree sweep or device execution is installed here.
"""
from collections import deque
from contextvars import ContextVar
from functools import wraps
import json
from pathlib import Path
import time
import weakref
RECENT_FRAMES = 12
PASSIVE_PAGES = {'unknown', 'loading', 'animation', 'history_loading'}
EVIDENCE_ACTIONS = {'enter_race', 'enter_multi', 'start_autonomous', 'confirm_autonomous_start', 'finish_training', 'confirm_finish', 'exchange_shop', 'confirm_shop', 'confirm_purchase', 'confirm_upgrade', 'confirm_breakthrough', 'confirm_history', 'forward_harvest', 'next_training_rewards', 'confirm_factor_choice', 'confirm_save_team_formation'}
EVIDENCE_FIELDS = {'harvest_summary', 'skill_result', 'skill_batches', 'finish_result', 'finish', 'last_observation', 'settlement', 'reconciliation', 'factor_selection'}
_session = ContextVar('uma_screenshot_retention', default=None)

def daily_no_archive_active():
    session = _session.get()
    return session is not None and (not getattr(session, 'archive', True))

def retain_transaction_proof(owner, observation):
    """Original daily owner names its one still-unconsumed entry proof.

    An acknowledged navigation tap does not consume later scan-resume proof.
    A new entry by the same owner replaces it; task settlement releases it.
    """
    session = _session.get()
    if session is None or session.archive or (not isinstance(observation, dict)):
        return
    try:
        name = observation.get('screenshot_path')
        if not isinstance(name, str):
            return
        path = Path(name).resolve()
        store = next((store for store in session.stores.values() if path in store.frames), None)
        if store is not None and (not path.is_symlink()):
            store.transaction_proofs[owner] = {path}
    except (OSError, ValueError, TypeError, RuntimeError):
        session.registration_errors += 1

def register_working_file(path, *, device=None, source=None, sha=None, proof_group=True):
    """Register a current skill/TP proof copy in the original task's scope."""
    try:
        _register_working_file(path, device=device, source=source, sha=sha, proof_group=proof_group)
    except (OSError, ValueError, TypeError, RuntimeError):
        session = _session.get()
        if session is not None:
            session.registration_errors += 1

def _register_working_file(path, *, device=None, source=None, sha=None, proof_group=True):
    session = _session.get()
    if session is None or session.archive:
        return
    if Path(path).is_symlink():
        return
    path = Path(path).resolve()
    owner = None
    if device is not None:
        value = getattr(device, '_daily_capture_root', None)
        if value is not None:
            owner = session.stores.get(Path(value))
    elif source is not None:
        source = Path(source).resolve()
        owner = next((store for store in session.stores.values() if source in store.frames), None)
    if owner is None or not path.is_relative_to(owner.root) or path.is_symlink() or (path.suffix.lower() != '.png'):
        return
    store = session.store(path.parent)
    for provider in owner.context_providers:
        if provider not in store.context_providers:
            store.context_providers.append(provider)
    if path in store.frames:
        info = path.stat()
        store.frames[path] = (info.st_size, info.st_mtime_ns, sha)
    if proof_group:
        store.proof_group.add(path)
        if source is not None:
            owner.proof_group.add(source)
    store.record({'screenshot_path': str(path), 'frame_sha256': sha, 'page': 'registered_working_file'})

def _pending(result):
    return any((result.get(key) is True for key in ('pending', 'pending_race', 'pending_exchange', 'pending_spin', 'pending_setting', 'start_submitted', 'entry_pending', 'ticket_entry_pending'))) or (result.get('action_outcome') or {}).get('execution_status') == 'uncertain' or ('performed' in result and result['performed'] is None) or (result.get('status') in {'dispatch_intent', 'uncertain', 'submitted'})

def _settled(result, complete):
    if complete or result.get('game_task_complete') is True or (result.get('ok') is True and result.get('skip_verified') is True):
        final = {key: value for key, value in result.items() if key not in {'start_submitted', 'performed', 'status'}}
        return not _pending(final)
    if _pending(result):
        return False
    return result.get('pending') is False or result.get('intentional_yield') is True

def full_capture_enabled():
    root = Path(__file__).resolve().parents[1]
    if root.name == 'backend':
        root = root.parent
    try:
        return json.loads((root / 'config/maa_option.json').read_text(encoding='utf8')).get('save_draw') is True
    except (OSError, ValueError, AttributeError):
        return False

def references(value):
    """Paths and hashes in durable evidence, not a traversal of the filesystem."""
    if isinstance(value, dict):
        for child in value.values():
            yield from references(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from references(child)
    elif isinstance(value, str):
        yield value

class CaptureStore:

    def __init__(self, directory, full=False, *, archive=True):
        self.root = Path(directory).resolve()
        self.archive = archive
        self.full = full if archive else False
        self.frames = {}
        self.recent = deque(maxlen=RECENT_FRAMES)
        self.passive = set()
        self.pinned = set()
        self.evidence = set()
        self.latest = None
        self.action = None
        self.removed = 0
        self.remove_errors = 0
        self.context_providers = []
        self.proof_group = set()
        self.transaction_proofs = {}
        self.document_refs = {}
        if not archive:
            self.restore_working()

    def restore_working(self):
        """Adopt only this policy's exact pending work, never old PNG archives."""
        try:
            index = self.root / 'screenshot-retention.json'
            if index.is_symlink() or index.stat().st_size > 2 * 1024 ** 2:
                return
            saved = json.loads(index.read_text(encoding='utf8'))
            if saved.get('mode') != 'no_archive' or saved.get('capture_directory') != str(self.root):
                return
            for row in saved.get('working_frames', []):
                path = self.root / row['name']
                if Path(row['name']).name != row['name'] or path.suffix.lower() != '.png' or path.is_symlink() or (path.resolve().parent != self.root):
                    continue
                info = path.stat()
                if (info.st_size, info.st_mtime_ns) != (row['size'], row['mtime_ns']):
                    continue
                self.frames[path] = (info.st_size, info.st_mtime_ns, row.get('sha'))
                self.pinned.add(path)
                if row.get('proof_group') is True:
                    self.proof_group.add(path)
                for owner in row.get('transaction_owners', []):
                    self.transaction_proofs.setdefault(owner, set()).add(path)
        except (OSError, ValueError, TypeError, KeyError):
            pass

    def record(self, observation):
        if not isinstance(observation, dict):
            return
        name = observation.get('screenshot_path') or observation.get('screenshot')
        if not isinstance(name, str):
            return
        path = Path(name)
        if path.suffix.lower() != '.png' or path.is_symlink() or path.resolve().parent != self.root:
            return
        try:
            stat = path.stat()
        except OSError:
            return
        path = path.resolve()
        if path in self.frames:
            self.latest = path
            return path
        self.frames[path] = (stat.st_size, stat.st_mtime_ns, observation.get('frame_sha256'))
        self.latest = path
        self.recent.append(path)
        if (observation.get('carrot_reward') or {}).get('status') == 'recognized':
            self.evidence.add(path)
        if observation.get('page') in PASSIVE_PAGES and (not observation.get('actions')) and (not observation.get('navigation_actions')):
            self.passive.add(path)
        if not self.archive:
            self.trim_working()
        elif not self.full:
            self._trim_passive()
        return path

    def before_dispatch(self, *, source=None, action=None):
        source = self.latest if source is None else source
        action = self.action if action is None else action
        if source in self.frames:
            self.pinned.add(source)
            if action in EVIDENCE_ACTIONS:
                self.evidence.add(source)

    def pin_receipt(self, receipt):
        refs = set(references(receipt))
        self.pinned.update((path for path, (_, _, sha) in self.frames.items() if str(path) in refs or sha in refs))

    def _remove(self, path):
        original = self.frames.get(path)
        try:
            if original is None or self.root.resolve() != self.root or path.is_symlink() or (path.resolve().parent != self.root):
                return
            stat = path.stat()
            if (stat.st_size, stat.st_mtime_ns) != original[:2]:
                return
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            self.remove_errors += 1
            return
        else:
            self.removed += 1
        self.frames.pop(path, None)
        self.passive.discard(path)

    def _trim_passive(self):
        for path in self.passive - set(self.recent) - self.pinned:
            self._remove(path)

    def needed_working(self, result=None):
        """Current transaction proof, not historical receipt/frame archives."""
        documents = []
        for provider in self.context_providers:
            try:
                documents.extend(provider())
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        try:
            for path in self.root.glob('*.json'):
                name = path.name
                if name[0].isdigit() or name.endswith('.receipt.json') or name in {'backend_result.json', 'adapter_result.json', 'screenshot-retention.json'} or ('result' in name) or ('report' in name) or path.is_symlink():
                    continue
                info = path.stat()
                token = (info.st_size, info.st_mtime_ns)
                cached = self.document_refs.get(path)
                if cached is None or cached[0] != token:
                    cached = (token, set(references(json.loads(path.read_text(encoding='utf8')))))
                    self.document_refs[path] = cached
                documents.append(list(cached[1]))
        except (OSError, ValueError, TypeError):
            pass
        if isinstance(result, dict):
            documents.append({key: result[key] for key in EVIDENCE_FIELDS if key in result})
        seen = set()
        for _ in range(4):
            refs = set(references(documents))
            extra = []
            for value in refs:
                if not value.endswith('.json'):
                    continue
                try:
                    path = Path(value)
                    if path in seen or path.is_symlink() or path.resolve().parent != self.root:
                        continue
                    seen.add(path)
                    info = path.stat()
                    token = (info.st_size, info.st_mtime_ns)
                    cached = self.document_refs.get(path)
                    if cached is None or cached[0] != token:
                        cached = (token, set(references(json.loads(path.read_text(encoding='utf8')))))
                        self.document_refs[path] = cached
                    extra.append(list(cached[1]))
                except (OSError, ValueError, TypeError, RuntimeError):
                    continue
            if not extra:
                break
            documents.extend(extra)
        refs = set(references(documents))
        return {path for path, (_, _, sha) in self.frames.items() if str(path) in refs or sha in refs}

    def transaction_frames(self):
        return {path for paths in self.transaction_proofs.values() for path in paths}

    def trim_working(self):
        keep = set(self.recent) | self.pinned | self.proof_group | self.transaction_frames() | self.needed_working()
        for path in set(self.frames) - keep:
            self._remove(path)

    def release_consumed(self, required=None):
        """An original journal owner acknowledged consumption; keep its next frame only."""
        refs = set(references(required or {}))
        keep = {path for path, (_, _, sha) in self.frames.items() if str(path) in refs or sha in refs}
        self.pinned.intersection_update(keep)
        self.evidence.intersection_update(keep)
        self.proof_group.intersection_update(keep)
        self.transaction_proofs = {owner: paths & keep for owner, paths in self.transaction_proofs.items() if paths & keep}
        self.recent.clear()
        self.recent.extend(keep)
        for path in set(self.frames) - keep:
            self._remove(path)

    def finish(self, complete, result):
        if not self.archive:
            keep = set() if _settled(result, complete) else self.needed_working(result) | self.pinned | self.proof_group | self.transaction_frames()
            for path in set(self.frames) - keep:
                self._remove(path)
            self.pinned.intersection_update(keep)
            self.evidence.intersection_update(keep)
            self.proof_group.intersection_update(keep)
            self.transaction_proofs = {owner: paths & keep for owner, paths in self.transaction_proofs.items() if paths & keep}
            self.recent.clear()
            self.recent.extend(keep)
            try:
                (self.root / 'screenshot-retention.json').write_text(json.dumps(dict(mode='no_archive', complete=complete, removed_frames=self.removed, retained_frames=len(self.frames), cleanup_errors=self.remove_errors, retained_reason='current_transaction_proof_only' if keep else None, working_frames=[dict(name=path.name, size=value[0], mtime_ns=value[1], sha=value[2], proof_group=path in self.proof_group, transaction_owners=[owner for owner, paths in self.transaction_proofs.items() if path in paths]) for path, value in self.frames.items()], journals_preserved=True, scope='exact_registered_captures', capture_directory=str(self.root)), indent=2), encoding='utf8')
            except OSError:
                pass
            return
        if self.full:
            return
        keep = set(self.recent if not complete else list(self.recent)[-2:]) | self.evidence
        if not complete:
            keep |= set(self.frames) - self.passive
            keep |= self.pinned
        else:
            required = {key: result[key] for key in EVIDENCE_FIELDS if key in result}
            context = self.root / 'context.json'
            try:
                if context.exists():
                    required['context'] = json.loads(context.read_text(encoding='utf8'))
            except (OSError, ValueError):
                return
            refs = set(references(required))
            keep |= {path for path, (_, _, sha) in self.frames.items() if str(path) in refs or sha in refs}
        for path in set(self.frames) - keep:
            self._remove(path)
        try:
            (self.root / 'screenshot-retention.json').write_text(json.dumps(dict(mode='compact', complete=complete, removed_frames=self.removed, retained_frames=len(self.frames), cleanup_errors=self.remove_errors, journals_preserved=True, scope='exact_capture_directory', capture_directory=str(self.root)), indent=2), encoding='utf8')
        except OSError:
            pass

class ScreenshotSession:

    def __init__(self, *, full=None, archive=False):
        self.archive = archive
        self.full = (full_capture_enabled() if full is None else full) if archive else False
        self.stores = {}
        self.finished = False
        self.last_result = {}
        self.registration_errors = 0

    def store(self, directory):
        root = Path(directory).resolve()
        if root in self.stores:
            return self.stores[root]
        store = self.stores[root] = CaptureStore(root, self.full, archive=self.archive)
        if not self.archive:
            try:
                saved = json.loads((root / 'screenshot-retention.json').read_text(encoding='utf8'))
                if saved.get('mode') != 'no_archive' or saved.get('capture_directory') != str(root):
                    return store
                for name in saved.get('working_substores', []):
                    child = (root / name).resolve()
                    if child != root and child.is_relative_to(root) and (not child.is_symlink()):
                        self.store(child)
            except (OSError, ValueError, TypeError, RuntimeError):
                pass
        return store

    def __enter__(self):
        self.token = _session.set(self)
        return self

    def __exit__(self, *args):
        try:
            if args and args[0] is not None or (not self.archive and (not self.finished)):
                self.finish(self.last_result, complete=False)
        finally:
            _session.reset(self.token)

    def finish(self, result, *, complete):
        self.finished = True
        self.last_result = result
        for store in self.stores.values():
            store.finish(complete, result)
        if not self.archive:
            for root, store in self.stores.items():
                children = [child.relative_to(root).as_posix() for child, saved in self.stores.items() if saved.frames and child != root and child.is_relative_to(root)]
                try:
                    index = root / 'screenshot-retention.json'
                    saved = json.loads(index.read_text(encoding='utf8'))
                    saved['working_substores'] = children
                    index.write_text(json.dumps(saved, indent=2), encoding='utf8')
                except (OSError, ValueError, TypeError):
                    pass
        stores = [dict(capture_directory=str(store.root), removed_frames=store.removed, retained_frames=len(store.frames), cleanup_errors=store.remove_errors) for store in self.stores.values()]
        return dict(scope='registered_stores_only', complete=complete, mode='archive' if self.archive else 'no_archive', registration_errors=self.registration_errors, store_count=len(stores), stores=stores, removed_frames=sum((row['removed_frames'] for row in stores)), retained_frames=sum((row['retained_frames'] for row in stores)), cleanup_errors=sum((row['cleanup_errors'] for row in stores)))

def install_capture_retention(device):
    session = _session.get()
    if session is None:
        return
    directory = Path(device.output_dir).resolve()
    device._daily_capture_root = directory
    store = session.store(directory)
    owned_stores = {directory: store}
    latest_capture = None
    action = None
    capture, command, step = (device.capture, device.command, device.step)

    def current_store():
        try:
            current = Path(device.output_dir).resolve()
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        if not current.is_relative_to(directory):
            return None
        if current not in owned_stores:
            owned_stores[current] = session.store(current)
            owned_stores[current].context_providers.append(current_context)
        return owned_stores[current]
    try:
        device_reference = weakref.ref(device)
    except TypeError:
        device_reference = lambda: device

    def current_context():
        owner = device_reference()
        if owner is None:
            return []
        values = [getattr(owner, 'context', {})]
        for book in (getattr(owner, 'book', {}), getattr(getattr(owner, 'journal', None), 'data', {})):
            if isinstance(book, dict):
                values.append({key: value for key, value in book.items() if key.startswith('pending') or key in {'navigation_pending', 'last_observation'}})
        return values
    store.context_providers.append(current_context)

    @wraps(capture)
    def captured(*args, **kwargs):
        nonlocal latest_capture
        started = time.time_ns()
        observed = None
        try:
            observed = capture(*args, **kwargs)
        finally:
            if observed is None and (not session.archive):
                selected = current_store()
                if selected is not None:
                    ended = time.time_ns()
                    try:
                        for path in selected.root.glob('*.png'):
                            if path.stem.isdigit() and started <= int(path.stem) <= ended:
                                selected.record({'screenshot_path': str(path)})
                    except OSError:
                        pass
        selected = current_store()
        path = selected.record(observed) if selected is not None else None
        latest_capture = (selected, path) if path is not None else None
        return observed

    @wraps(command)
    def dispatched(*args, **kwargs):
        if 'shell' in args and ('input' in args or 'am' in args):
            if latest_capture is not None:
                selected, path = latest_capture
                if not selected.archive:
                    selected.pinned.clear()
                selected.before_dispatch(source=path, action=action)
        return command(*args, **kwargs)

    @wraps(step)
    def stepped(*args, **kwargs):
        nonlocal action
        previous = action
        action = args[0] if args else kwargs.get('action_id', kwargs.get('action'))
        try:
            receipt = step(*args, **kwargs)
            for selected in owned_stores.values():
                if selected.archive:
                    selected.pin_receipt(receipt)
                else:
                    selected.pinned.clear()
                    if isinstance(receipt, dict) and _pending(receipt):
                        selected.pin_receipt(receipt)
                    selected.trim_working()
            return receipt
        finally:
            action = previous
    device.capture, device.command, device.step = (captured, dispatched, stepped)
