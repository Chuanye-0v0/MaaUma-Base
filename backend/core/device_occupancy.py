"""Process-held, per-device exclusion shared by UMAA and Pacer.

The OS lock, not the metadata file or a GUI flag, is authoritative. Nested
daily tasks on the same thread inherit the outer batch's lease.
"""
from desktop.umaa_public_context import endpoint as _public_endpoint
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import re
import time
_held = ContextVar('umaa_device_leases', default=frozenset())

class DeviceBusy(RuntimeError):

    def __init__(self, owner=None):
        self.owner = owner or {'owner': 'unknown'}
        super().__init__('device_busy')

def _paths(directory, endpoint):
    if not re.fullmatch(re.escape(_public_endpoint()), endpoint):
        raise ValueError('device_endpoint_not_allowed')
    root = Path(directory).resolve()
    return (root, root / (endpoint.replace(':', '_') + '.lock'))

def _lock(stream):
    stream.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

def _unlock(stream):
    stream.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

def _owner(path):
    try:
        value = json.loads(path.with_suffix('.json').read_text(encoding='utf8'))
        return {key: value.get(key) for key in ('owner', 'pid', 'endpoint', 'started_at')}
    except (OSError, ValueError, AttributeError):
        return {'owner': 'unknown'}

def probe(directory, endpoint):
    """Read occupancy without connecting to, capturing, or inputting a device."""
    _, path = _paths(directory, endpoint)
    if not path.exists():
        return None
    with path.open('r+b') as stream:
        try:
            _lock(stream)
        except OSError:
            return _owner(path)
        _unlock(stream)
    return None

@contextmanager
def hold(directory, endpoint, owner):
    root, path = _paths(directory, endpoint)
    key = str(path)
    if key in _held.get():
        yield
        return
    root.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if path.stat().st_size == 0:
            stream.write(b'0')
            stream.flush()
        try:
            _lock(stream)
        except OSError:
            raise DeviceBusy(_owner(path)) from None
        token = None
        try:
            metadata = path.with_suffix('.json')
            temporary = metadata.with_suffix(f'.{os.getpid()}.tmp')
            temporary.write_text(json.dumps(dict(owner=owner, pid=os.getpid(), endpoint=endpoint, started_at=time.time())), encoding='utf8')
            temporary.replace(metadata)
            token = _held.set(_held.get() | {key})
            yield
        finally:
            if token is not None:
                _held.reset(token)
            _unlock(stream)
