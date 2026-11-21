"""Default supported MuMu transport: persistent native frames, ADB input."""

from __future__ import annotations

import math
import os
import time
from pathlib import Path

from config.settings import ADB_PATH
from core.adb import ADB, _capture_exceptions
from core.nemu_ipc_capture import NemuIpcCapture
from core.auto_trainer.runtime_control import capture_checkpoint


def native_instance_id(port: int | None) -> int | None:
    """Explicit capture targets; never fall through to another emulator."""
    from core.cut_portable_runtime import generic_adb_capture
    if generic_adb_capture():return None
    return {16384: 0, 16416: 1, 16448: 2}.get(port)


class NativeCaptureADB(ADB):
    """Use renderer IPC for frames; keep ADB for taps and as capture fallback."""

    supports_in_memory_capture = True

    def __init__(self, *, port: int = 16448):
        instance_id = native_instance_id(port)
        if instance_id is None:
            raise ValueError("native_capture_requires_mapped_mumu_device")
        super().__init__(port=port)
        self._instance_id = instance_id
        self._mumu_root = Path(ADB_PATH).resolve().parents[3]
        self._native_available = True
        self._capture = None
        self._native_size = None

    def close(self):
        capture, self._capture = self._capture, None
        self._native_size = None
        if capture is not None:
            capture.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def capture_backend_name(self):
        return 'mumu-ipc' if self._native_available and self._capture is not None else 'adb-png'

    def capture_diagnostics(self):
        return {'backend': self.capture_backend_name, 'native_available': self.capture_backend_name == 'mumu-ipc',
                'fallback_reasons': [] if self._native_available else ['mumu_ipc_unavailable']}

    def __del__(self):
        if getattr(self, "_capture", None) is not None:
            self.close()

    @_capture_exceptions
    def read_frame(self, *, deadline: float | None = None):
        self.last_capture_failure = None
        self._capture_stage = ('mumu-ipc', 'native')
        paused_seconds = capture_checkpoint(self)
        if deadline is not None:
            deadline += paused_seconds
        if not self._native_available:
            self._capture_failed('mumu-ipc', 'native', 'native_unavailable')
            raise RuntimeError("mumu_ipc_disabled_for_invocation")
        if deadline is not None and (not math.isfinite(deadline) or time.monotonic() >= deadline):
            self._capture_failed('mumu-ipc', 'native', 'timeout', timed_out=True)
            return None
        try:
            if self._capture is None:
                self._capture = NemuIpcCapture(self._mumu_root, instance_id=self._instance_id)
                self._capture.start()
            image = self._capture.read_image()
        except (OSError, RuntimeError, AttributeError) as error:
            self._capture_failed('mumu-ipc', 'native', 'exception', error=error)
            self._native_available = False
            self.close()
            raise
        if deadline is not None and time.monotonic() >= deadline:
            self._capture_failed('mumu-ipc', 'native', 'timeout', timed_out=True)
            return None
        if image is not None:
            self._native_size = (int(image.shape[1]), int(image.shape[0]))
            self._capture_succeeded()
        else:
            self._capture_failed('mumu-ipc', 'native', 'empty_frame')
        return image

    def get_screen_size(self):
        """Use native pixel geometry; avoid a redundant/occasionally empty wm query."""
        if self._native_size is None and self._native_available:
            try:
                self.read_frame()
            except (OSError, RuntimeError, AttributeError):
                pass  # Existing ADB fallback stays available.
        return self._native_size or super().get_screen_size()

    @_capture_exceptions
    def screenshot(self, save_path=None, *, timeout=None, deadline=None):
        self._begin_capture()
        self._capture_stage = ('mumu-ipc', 'native')
        if deadline is None and timeout is not None:
            if not math.isfinite(float(timeout)) or timeout <= 0:
                self._capture_failed('mumu-ipc', 'capture', 'invalid_budget')
                return None
            deadline = time.monotonic() + timeout
        if not self._native_available:
            self._capture_pending_native_fallback = {'backend': 'mumu-ipc', 'reason': 'native_unavailable'}
            return super().screenshot(save_path, deadline=deadline)
        try:
            image = self.read_frame(deadline=deadline)
        except (OSError, RuntimeError, AttributeError) as exc:
            self._capture_pending_native_fallback = {'backend': 'mumu-ipc', 'reason': 'native_unavailable'}
            if type(exc).__name__ in {'OSError', 'RuntimeError', 'AttributeError', 'PermissionError', 'FileNotFoundError'}:
                self._capture_pending_native_fallback['error_class'] = type(exc).__name__
            print(f"[MuMu IPC] capture unavailable ({exc}); falling back to ADB")
            return super().screenshot(save_path, deadline=deadline)
        if image is None:
            if self.last_capture_failure is None:
                self._capture_failed('mumu-ipc', 'native', 'empty_frame')
            return None
        if save_path is None:
            save_path = os.path.join("logs", "screenshots", "screen.png")
        self._capture_stage = ('mumu-ipc', 'write')
        NemuIpcCapture.save_image(str(save_path), image)
        self._capture_succeeded()
        return image

    @staticmethod
    def save_frame(save_path, image):
        NemuIpcCapture.save_image(str(save_path), image)

def adb_for_capture(port: int | None):
    if native_instance_id(port) is not None:
        return NativeCaptureADB(port=port)
    return ADB(port=port) if port is not None else ADB()
