import os
import time

class FileStop:

    def __init__(self, path, parent_pid=None):
        self.path = path
        self.parent = None
        if parent_pid is not None and os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self.kernel.OpenProcess.restype = wintypes.HANDLE
            self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.parent = self.kernel.OpenProcess(1048576, False, parent_pid)
            if not self.parent:
                self.path.touch(exist_ok=True)

    def is_set(self):
        return self.path.exists() or bool(self.parent and self.kernel.WaitForSingleObject(self.parent, 0) != 258)

    def close(self):
        if self.parent:
            self.kernel.CloseHandle(self.parent)
            self.parent = None

    def wait(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.1, remaining))
        return True
