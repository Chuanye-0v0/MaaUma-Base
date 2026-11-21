"""Transparent Windows compression for old diagnostic JSON, never a new format.

Callers own lifecycle, path and maintenance-lock checks. The opened file keeps
its logical bytes, name and timestamps; normal readers and SHA bindings work.
"""
import os
from pathlib import Path

MIN_BYTES = 64 * 1024
MAX_BYTES = 8 * 1024 * 1024


def _io(path):
    path = Path(path).absolute()
    if os.name != 'nt':
        return path
    value = str(path)
    if not value.startswith('\\\\?\\'):
        value = '\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value
    return Path(value)


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def compress_file(path, *, expected=None):
    """Compress one already-selected file, returning physical size separately.

    No content rewrite, rename, directory inheritance, subprocess or recursion.
    Unsupported volumes and temporarily busy files leave the JSON readable.
    """
    if os.name != 'nt':
        return {'status': 'unsupported_platform'}
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class CompressionInfo(ctypes.Structure):
        _fields_ = [('size', ctypes.c_longlong), ('format', wintypes.WORD),
                    ('unit', ctypes.c_ubyte), ('chunk', ctypes.c_ubyte),
                    ('cluster', ctypes.c_ubyte), ('reserved', ctypes.c_ubyte * 3)]

    class StandardInfo(ctypes.Structure):
        _fields_ = [('allocation', ctypes.c_longlong), ('end', ctypes.c_longlong),
                    ('links', wintypes.DWORD), ('delete_pending', ctypes.c_ubyte),
                    ('directory', ctypes.c_ubyte)]

    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.GetFileInformationByHandleEx.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    api.GetFileInformationByHandleEx.restype = wintypes.BOOL
    api.DeviceIoControl.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
    api.DeviceIoControl.restype = wintypes.BOOL
    api.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    api.CloseHandle.restype = wintypes.BOOL
    try:
        native = _io(path)
        before = native.lstat()
        attrs = getattr(before, 'st_file_attributes', 0)
        if attrs & (0x400 | 0x4000 | 0x10) or before.st_nlink != 1:
            return {'status': 'linked_encrypted_or_directory'}
        if not MIN_BYTES <= before.st_size <= MAX_BYTES:
            return {'status': 'outside_size_range'}
        if expected is not None and tuple(expected) != _identity(before):
            return {'status': 'file_changed'}
        # Share normal read/write/replace access. If another owner denies the
        # required access, leave this historical JSON untouched and retry later.
        handle_value = api.CreateFileW(str(native), 0xC0000000, 7, None, 3, 0x200000, None)
        if handle_value == ctypes.c_void_p(-1).value:
            return {'status': 'compression_unavailable', 'winerror': ctypes.get_last_error()}
        try:
            descriptor = msvcrt.open_osfhandle(handle_value, os.O_RDWR | os.O_BINARY)
        except OSError as error:
            api.CloseHandle(handle_value)
            return {'status': 'compression_unavailable', 'winerror': getattr(error, 'winerror', None)}
        with os.fdopen(descriptor, 'r+b', buffering=0) as stream:
            opened = os.fstat(stream.fileno())
            if _identity(opened) != _identity(before) or opened.st_nlink != 1:
                return {'status': 'file_changed'}
            handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
            initial = CompressionInfo()
            if not api.GetFileInformationByHandleEx(handle, 8, ctypes.byref(initial), ctypes.sizeof(initial)):
                return {'status': 'compression_unavailable', 'winerror': ctypes.get_last_error()}
            if initial.format:
                return {'status': 'already_compressed'}
            initial_allocation = StandardInfo()
            if not api.GetFileInformationByHandleEx(handle, 1, ctypes.byref(initial_allocation), ctypes.sizeof(initial_allocation)):
                return {'status': 'compression_size_unavailable', 'winerror': ctypes.get_last_error()}
            method = wintypes.WORD(2)  # COMPRESSION_FORMAT_LZNT1
            returned = wintypes.DWORD()
            if not api.DeviceIoControl(handle, 0x9C040, ctypes.byref(method), ctypes.sizeof(method),
                                       None, 0, ctypes.byref(returned), None):
                return {'status': 'compression_unavailable', 'winerror': ctypes.get_last_error()}
            final = CompressionInfo()
            if not api.GetFileInformationByHandleEx(handle, 8, ctypes.byref(final), ctypes.sizeof(final)):
                return {'status': 'compression_size_unavailable', 'winerror': ctypes.get_last_error()}
            if _identity(os.fstat(stream.fileno())) != _identity(before) or _identity(native.stat()) != _identity(before):
                return {'status': 'file_changed'}
            # CompressionInfo returns EOF for an ordinary uncompressed file;
            # StandardInfo gives its actual allocated clusters before change.
            return {'status': 'compressed', 'logical_bytes': before.st_size,
                    'allocated_before': initial_allocation.allocation, 'allocated_after': final.size,
                    'allocated_bytes_saved': max(0, initial_allocation.allocation - final.size),
                    'logical_bytes_unchanged': True}
    except OSError as error:
        return {'status': 'compression_unavailable', 'winerror': getattr(error, 'winerror', None)}
