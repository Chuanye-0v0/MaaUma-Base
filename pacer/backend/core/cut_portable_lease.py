"""Process-held exclusion for the explicitly configured portable emulator."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import stat
import threading
import time

from core.cut_portable_runtime import device_ports

_held=ContextVar('cut_portable_device_leases',default=frozenset())
_handles=ContextVar('cut_portable_native_handles',default=None)


def _plain(path):
    for part in (path,*path.parents):
        try:info=part.lstat()
        except FileNotFoundError:continue
        if stat.S_ISLNK(info.st_mode)or getattr(info,'st_file_attributes',0)&0x400:return False
    return True


class _NativeLease:
    """A live same-thread native handle, never a serializable permission flag."""
    def __init__(self,directory,endpoint,path,stream):
        self.directory,self.endpoint,self.canonical_path=directory,endpoint,path
        self.stream,self.thread_id=stream,threading.get_ident()

    def valid(self):
        try:
            key=str(self.canonical_path)
            if (self.thread_id!=threading.get_ident()or self.stream.closed
                    or key not in _held.get()or(_handles.get()or{}).get(key)is not self
                    or _path(self.directory,self.endpoint)!=self.canonical_path
                    or not _plain(self.canonical_path)):
                return False
            # _path resolves the configured root; a newly introduced reparse
            # component in the original spelling must not gain owner authority.
            base=os.environ.get('UMA_PUBLIC_LOCK_ROOT')or os.environ.get('LOCALAPPDATA')
            lexical=Path(base).absolute()/'UmaSuite/device-locks'/self.canonical_path.name
            if not _plain(lexical)or lexical!=self.canonical_path:return False
            opened=os.fstat(self.stream.fileno());named=self.canonical_path.lstat()
            return (stat.S_ISREG(opened.st_mode)and opened.st_nlink==1
                and(named.st_dev,named.st_ino)==(opened.st_dev,opened.st_ino))
        except (OSError,ValueError,RuntimeError,TypeError):return False


def current_native_lease(directory,endpoint):
    """Return only the actual held handle in this thread's original context."""
    try:
        proof=(_handles.get()or{}).get(str(_path(directory,endpoint)))
        return proof if isinstance(proof,_NativeLease)and proof.valid()else None
    except (OSError,ValueError,RuntimeError):return None


def _path(directory,endpoint):
    if endpoint not in {f'127.0.0.1:{p}' for p in device_ports()}:
        raise ValueError('device_endpoint_not_configured')
    # Match the public daily backend even when callers pass a private run dir.
    base=os.environ.get('UMA_PUBLIC_LOCK_ROOT') or os.environ.get('LOCALAPPDATA')
    if not base:
        raise RuntimeError('public_device_lock_unavailable')
    root=(Path(base)/'UmaSuite'/'device-locks').resolve()
    return root/(endpoint.replace(':','_')+'.lock')


def _lock(stream,unlock=False):
    import msvcrt
    stream.seek(0)
    msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK,1)


def probe(directory,endpoint):
    path=_path(directory,endpoint)
    if not path.exists():return None
    with path.open('r+b') as stream:
        try:_lock(stream)
        except OSError:
            try:return json.loads(path.with_suffix('.json').read_text(encoding='utf8'))
            except (OSError,ValueError):return {'owner':'unknown'}
        _lock(stream,True)
    return None


@contextmanager
def hold(directory,endpoint,owner):
    path=_path(directory,endpoint);key=str(path)
    if key in _held.get():yield;return
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as stream:
        if path.stat().st_size==0:stream.write(b'0');stream.flush()
        try:_lock(stream)
        except OSError:raise RuntimeError('device_busy') from None
        token=None;handle_token=None
        try:
            path.with_suffix('.json').write_text(json.dumps({'owner':owner,'pid':os.getpid(),
                'endpoint':endpoint,'started_at':time.time()}),encoding='utf8')
            token=_held.set(_held.get()|{key})
            handle_token=_handles.set({**(_handles.get()or{}),key:_NativeLease(directory,endpoint,path,stream)})
            yield
        finally:
            if handle_token is not None:_handles.reset(handle_token)
            if token is not None:_held.reset(token)
            _lock(stream,True)
