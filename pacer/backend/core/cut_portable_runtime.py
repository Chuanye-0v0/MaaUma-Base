"""Explicit configuration for the independent cut-farming distribution."""
import os
from pathlib import Path
import atexit

_UPDATE_LEASE=None
_LEASE_CHECKED=set()


def portable_root():
    value=os.environ.get('UMA_CUT_PORTABLE_ROOT')
    root=Path(value).resolve() if value else None
    if root is not None and root not in _LEASE_CHECKED:
        application=root.parent if root.name.casefold()=='backend' else root
        if (application/'product-package-manifest.json').is_file():
            from desktop.product_updates import backend_process_lease
            global _UPDATE_LEASE
            _UPDATE_LEASE=backend_process_lease(application)
            _UPDATE_LEASE.__enter__()
            atexit.register(_UPDATE_LEASE.__exit__,None,None,None)
        _LEASE_CHECKED.add(root)
    return root


def device_ports():
    if portable_root() is None:return {16384,16416,16448}
    try:port=int(os.environ['UMA_ADB_PORT'])
    except (KeyError,ValueError):raise ValueError('portable_adb_port_invalid') from None
    if not 1<=port<=65535:raise ValueError('portable_adb_port_invalid')
    return {port}


def screenshot_output_allowed(path):
    path=Path(path).resolve();root=portable_root()
    return path.is_relative_to(root/'runs') if root else path.drive.upper()=='E:'


def generic_adb_capture():
    # Portable frames use the selected ADB endpoint even when an installed
    # emulator SDK supplies the pixels. Never use local hardcoded VM mappings.
    return portable_root() is not None


def portable_capture_preference():
    value = os.environ.get('UMA_CUT_CAPTURE_BACKEND', 'auto')
    if value not in {'auto', 'adb', 'ldplayer', 'mumu'}:
        raise ValueError('portable_capture_backend_invalid')
    return value


def enable_unicode_image_paths(*, force: bool = False):
    """Keep OpenCV 4.10's ANSI path handling out of standalone Windows I/O.

    The adapter is process-local and only enabled by the standalone entrypoint.
    Short ASCII paths retain the original code path; non-ASCII or long (>=240)
    Windows paths route through core.frame_io.io_path (extended-length prefix).
    """
    if not force and portable_root() is None:return
    import cv2
    import numpy as np
    from core.frame_io import io_path
    if getattr(cv2.imread,'_cut_unicode_paths',False):return
    original_read,original_write=cv2.imread,cv2.imwrite

    def _needs_compat(path: str) -> bool:
        return (not path.isascii()) or len(path) >= 240 or path.startswith('\\\\?\\')

    def read(filename,flags=cv2.IMREAD_COLOR):
        path=os.fsdecode(filename)
        if not _needs_compat(path):return original_read(path,flags)
        try:data=io_path(path).read_bytes()
        except OSError:return None
        return cv2.imdecode(np.frombuffer(data,dtype=np.uint8),flags) if data else None

    def write(filename,img,params=None):
        path=os.fsdecode(filename)
        if not _needs_compat(path):return original_write(path,img,params or [])
        suffix=Path(path).suffix or '.png'
        ok,data=cv2.imencode(suffix,img,params or [])
        if not ok:return False
        try:io_path(path).write_bytes(data.tobytes())
        except OSError:return False
        return True

    read._cut_unicode_paths=True
    cv2.imread,cv2.imwrite=read,write
