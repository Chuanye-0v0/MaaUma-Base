"""Synchronous frame watchdog; recovery only runs after the task has unwound."""
from contextvars import ContextVar
from functools import wraps
STALL_SECONDS = 30
_active = ContextVar('uma_stall_watch', default=None)
WAIT_PAGES = {'auth_blocked', 'startup_terms', 'connection_conflict'}

def screen_identity(observed):
    """Pixel identity for stall checks; never substitute the input receipt hash."""
    pixels = observed.get('screen_pixel_sha256')
    if pixels is not None:
        return ('pixels', pixels) if isinstance(pixels, str) and pixels else None
    raw = observed.get('frame_sha256')
    return ('legacy_png', raw) if isinstance(raw, str) and raw else None

def same_screen(first, second):
    identity = screen_identity(first)
    return identity is not None and identity == screen_identity(second)

def install_screen_identity(device):
    """Bind decoded pixels to this capture's original PNG, for every device owner."""
    import hashlib
    import cv2
    import numpy as np
    capture, command = (device.capture, device.command)
    latest, last_pixels, since = (None, None, None)

    @wraps(command)
    def read(*args, **kwargs):
        nonlocal latest
        raw = command(*args, **kwargs)
        if args == ('-s', device.endpoint, 'exec-out', 'screencap', '-p'):
            latest = None
            image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if image is not None:
                digest = hashlib.sha256(str(image.shape).encode('ascii') + b'\x00')
                digest.update(memoryview(image))
                latest = (hashlib.sha256(raw).hexdigest(), digest.hexdigest())
        return raw

    @wraps(capture)
    def observe(*args, **kwargs):
        nonlocal latest, last_pixels, since
        latest = None
        observed = capture(*args, **kwargs)
        if isinstance(observed, dict) and latest and (observed.get('frame_sha256') == latest[0]):
            observed['screen_pixel_sha256'] = latest[1]
            captured = observed.get('captured_monotonic')
            if type(captured) in (int, float):
                if last_pixels != latest[1] or observed.get('page') in WAIT_PAGES:
                    last_pixels, since = (latest[1], captured)
                observed['unchanged_since_monotonic'] = since
        elif isinstance(observed, dict):
            observed.pop('screen_pixel_sha256', None)
        return observed
    device.command, device.capture = (read, observe)

class StallWatch:

    def __init__(self, endpoint, stop):
        self.endpoint, self.stop = (endpoint, stop)
        self.last = None
        self.frame, self.since = (None, None)
        self.loading_since = None

    def __enter__(self):
        self.token = _active.set(self)
        return self

    def __exit__(self, *args):
        _active.reset(self.token)

    def attach(self, device):
        capture = device.capture

        @wraps(capture)
        def watched(*args, **kwargs):
            observed = capture(*args, **kwargs)
            if _active.get() is not self:
                return observed
            if self.stop.is_set():
                raise RuntimeError('operator_stop')
            if not isinstance(observed, dict) or observed.get('endpoint') != self.endpoint:
                return observed
            frame, captured = (screen_identity(observed), observed.get('captured_monotonic'))
            if frame is None or not observed.get('frame_sha256') or type(captured) not in (int, float):
                return observed
            if frame != self.frame or observed.get('page') in WAIT_PAGES:
                self.frame, self.since = (frame, captured)
            observed['unchanged_since_monotonic'] = self.since
            limit = getattr(device, 'loading_stall_seconds', None)
            loading = observed.get('page') == 'loading' and type(limit) in (int, float) and (limit >= STALL_SECONDS)
            if loading:
                if self.loading_since is None:
                    self.loading_since = captured
                observed.update(loading_since_monotonic=self.loading_since, loading_stall_seconds=limit)
            else:
                self.loading_since = None
            self.last = dict(observed)
            device._last_observation = observed
            if loading and captured - self.loading_since >= limit or (not loading and captured - self.since >= STALL_SECONDS):
                raise RuntimeError('game_stalled')
            return observed
        device.capture = watched

def watch_device(device):
    watch = _active.get()
    if watch is not None:
        watch.attach(device)

def without_watch(function):

    @wraps(function)
    def call(*args, **kwargs):
        token = _active.set(None)
        try:
            return function(*args, **kwargs)
        finally:
            _active.reset(token)
    return call

@without_watch
def recover_stall(result, runtime_factory, stop, observed=None):
    """One verified restart, no task replay and no success fabrication."""
    if result.get('ok') or stop.is_set() or result.get('restart_receipts') or result.get('stall_recovery'):
        return None
    if result.get('account_conflict') or result.get('account_paused') or result.get('action_outcome', {}).get('execution_status') in {'uncertain', 'unknown', 'partial'}:
        return None
    observed = observed or result.get('last_observation')
    if not isinstance(observed, dict) or not observed.get('frame_sha256') or observed.get('page') in WAIT_PAGES:
        return None
    runtime = runtime_factory()
    receipt = runtime.restart_if_stalled(observed)
    if receipt is None:
        return None
    recovery = dict(ok=False, receipt=receipt)
    try:
        startup = runtime.prepare(after_restart=True)
        recovery.update(ok=startup.get('ok') is True, startup=startup)
    except (RuntimeError, OSError) as error:
        recovery['reason'] = str(error)
    runtime.save('stall-recovery.json', recovery)
    return recovery
