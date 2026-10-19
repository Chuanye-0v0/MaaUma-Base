"""Task-scoped screenshot retention; never sweeps historical runs or journals.

Actionable frames remain available while a task can still require reconciliation.
Only unreferenced loading/polling frames roll off during execution. After durable
success, ordinary screenshots are disposable; critical evidence stays lossless.
"""
from collections import deque
from contextvars import ContextVar
from functools import wraps
import json
from pathlib import Path
RECENT_FRAMES = 12
PASSIVE_PAGES = {'unknown', 'loading', 'animation', 'history_loading'}
EVIDENCE_ACTIONS = {'enter_race', 'enter_multi', 'start_autonomous', 'confirm_autonomous_start', 'finish_training', 'confirm_finish', 'exchange_shop', 'confirm_shop', 'confirm_purchase', 'confirm_upgrade', 'confirm_breakthrough', 'confirm_history', 'forward_harvest', 'next_training_rewards', 'confirm_factor_choice', 'confirm_save_team_formation'}
EVIDENCE_FIELDS = {'harvest_summary', 'skill_result', 'skill_batches', 'finish_result', 'finish', 'last_observation', 'settlement', 'reconciliation', 'factor_selection'}
_session = ContextVar('uma_screenshot_retention', default=None)

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

    def __init__(self, directory, full=False):
        self.root = Path(directory).resolve()
        self.full = full
        self.frames = {}
        self.recent = deque(maxlen=RECENT_FRAMES)
        self.passive = set()
        self.pinned = set()
        self.evidence = set()
        self.latest = None
        self.action = None
        self.removed = 0
        self.remove_errors = 0

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
        if not self.full:
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

    def finish(self, complete, result):
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

    def __init__(self, *, full=None):
        self.full = full_capture_enabled() if full is None else full
        self.stores = {}

    def __enter__(self):
        self.token = _session.set(self)
        return self

    def __exit__(self, *args):
        try:
            if args and args[0] is not None:
                self.finish({}, complete=False)
        finally:
            _session.reset(self.token)

    def finish(self, result, *, complete):
        for store in self.stores.values():
            store.finish(complete, result)
        stores = [dict(capture_directory=str(store.root), removed_frames=store.removed, retained_frames=len(store.frames), cleanup_errors=store.remove_errors) for store in self.stores.values()]
        return dict(scope='registered_stores_only', complete=complete, store_count=len(stores), stores=stores, removed_frames=sum((row['removed_frames'] for row in stores)), retained_frames=sum((row['retained_frames'] for row in stores)), cleanup_errors=sum((row['cleanup_errors'] for row in stores)))

def install_capture_retention(device):
    session = _session.get()
    if session is None:
        return
    directory = Path(device.output_dir).resolve()
    store = session.stores.setdefault(directory, CaptureStore(directory, session.full))
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
            owned_stores[current] = session.stores.setdefault(current, CaptureStore(current, session.full))
        return owned_stores[current]

    @wraps(capture)
    def captured(*args, **kwargs):
        nonlocal latest_capture
        observed = capture(*args, **kwargs)
        selected = current_store()
        path = selected.record(observed) if selected is not None else None
        latest_capture = (selected, path) if path is not None else None
        return observed

    @wraps(command)
    def dispatched(*args, **kwargs):
        if 'shell' in args and ('input' in args or 'am' in args):
            if latest_capture is not None:
                selected, path = latest_capture
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
                selected.pin_receipt(receipt)
            return receipt
        finally:
            action = previous
    device.capture, device.command, device.step = (captured, dispatched, stepped)
