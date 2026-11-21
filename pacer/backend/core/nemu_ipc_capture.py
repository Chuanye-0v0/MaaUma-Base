"""Optional MuMu renderer-IPC capture for device 2 experiments."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
from threading import RLock


class NemuIpcCapture:
    """Read frames from MuMu's local renderer IPC without changing game state."""
    _leases = {}
    _lease_lock = RLock()

    def __init__(self, mumu_root: str | Path, instance_id: int):
        self.mumu_root = Path(mumu_root)
        self.instance_id = int(instance_id)
        self._lib = None
        self._connection_id = 0
        self._width = ctypes.pointer(ctypes.c_int(0))
        self._height = ctypes.pointer(ctypes.c_int(0))
        self._pixels = None
        self._lease_key = None

    def start(self) -> "NemuIpcCapture":
        if self._connection_id > 0:
            return self
        dll_path = (
            self.mumu_root
            / "nx_device"
            / "12.0"
            / "shell"
            / "sdk"
            / "external_renderer_ipc.dll"
        )
        if not dll_path.is_file():
            raise RuntimeError(f"mumu_ipc_dll_missing:{dll_path}")
        self._lib = ctypes.CDLL(str(dll_path))
        with self._lease_lock:
            connection_id = self._lib.nemu_connect(str(self.mumu_root), self.instance_id)
            if connection_id <= 0:
                raise RuntimeError(f"mumu_ipc_connect_failed:instance={self.instance_id}")
            self._connection_id = connection_id
            self._lease_key = (str(dll_path.resolve()).lower(), self.instance_id, connection_id)
            self._leases[self._lease_key] = self._leases.get(self._lease_key, 0) + 1
        try:
            empty = ctypes.POINTER(ctypes.c_int)()
            result = self._lib.nemu_capture_display(
                connection_id, 0, 0, self._width, self._height, empty
            )
            size = (self._width.contents.value, self._height.contents.value)
            if result != 0 or size not in {(720, 1280), (1280, 720)}:
                raise RuntimeError(f"mumu_ipc_resolution_failed:result={result}:size={size}")
            self._pixels = ctypes.pointer((ctypes.c_ubyte * (720 * 1280 * 4))())
            return self
        except BaseException:
            self.close()
            raise

    def screenshot(self, save_path: str):
        image = self.read_image()
        self.save_image(save_path, image)
        return image

    def read_image(self):
        import cv2
        import numpy as np

        if self._lib is None or self._connection_id <= 0 or self._pixels is None:
            raise RuntimeError("mumu_ipc_not_connected")
        # Race scenes can rotate without closing the renderer connection.
        result = self._lib.nemu_capture_display(
            self._connection_id, 0, 0, self._width, self._height,
            ctypes.POINTER(ctypes.c_int)(),
        )
        size = (self._width.contents.value, self._height.contents.value)
        if result != 0 or size not in {(720, 1280), (1280, 720)}:
            raise RuntimeError(f"mumu_ipc_resolution_failed:result={result}:size={size}")
        result = self._lib.nemu_capture_display(
            self._connection_id,
            0,
            720 * 1280 * 4,
            self._width,
            self._height,
            self._pixels,
        )
        if result != 0:
            raise RuntimeError(f"mumu_ipc_capture_failed:result={result}")
        # This installed MuMu build returns upside-down RGBA, not BGRA.
        width, height = self._width.contents.value, self._height.contents.value
        if (width, height) not in {(720, 1280), (1280, 720)}:
            raise RuntimeError(f"mumu_ipc_resolution_failed:size={(width, height)}")
        rgba = np.ctypeslib.as_array(self._pixels.contents).reshape(height, width, 4)
        return cv2.cvtColor(cv2.flip(rgba, 0), cv2.COLOR_RGBA2BGR)

    @staticmethod
    def save_image(save_path: str, image) -> None:
        import cv2

        encoded, png = cv2.imencode(
            ".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 1]
        )
        if not encoded:
            raise RuntimeError("mumu_ipc_png_encode_failed")
        from core.auto_trainer.pacer_storage import _io
        target = _io(os.path.abspath(save_path))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(png.tobytes())

    def close(self) -> None:
        with self._lease_lock:
            if self._lease_key is not None:
                remaining = self._leases[self._lease_key] - 1
                if remaining:
                    self._leases[self._lease_key] = remaining
                else:
                    del self._leases[self._lease_key]
                    self._lib.nemu_disconnect(self._connection_id)
                self._lease_key = None
            self._connection_id = 0
        self._pixels = None
