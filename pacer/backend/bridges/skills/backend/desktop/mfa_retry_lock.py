"""Nonblocking process-held lock for retry marker maintenance, not game execution."""
from contextlib import contextmanager
import os
from pathlib import Path

@contextmanager
def retry_cleanup_guard(root):
    """A leftover file is harmless; only a live OS lock prevents cleanup.

    Keep the lock file permanently to avoid unlink/recreate races. Process exit
    releases its byte lock even when Python cannot run its finally block.
    Legacy workers must still be excluded by the existing caller's process check.
    """
    path = Path(root) / 'retry-cleanup.lock'
    with path.open('a+b') as stream:
        if path.stat().st_size == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
        try:
            yield True
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
