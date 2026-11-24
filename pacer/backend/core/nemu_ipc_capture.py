"""Optional MuMu renderer-IPC capture for device 2 experiments."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
from threading import RLock

DEFAULT_GAME_PACKAGES: tuple[str, ...] = ("com.bilibili.umamusu",)
_VALID_SIZES = frozenset({(720, 1280), (1280, 720)})


def _configure_lib_signatures(lib) -> None:
    """Configure explicit ctypes prototypes for external_renderer_ipc.dll."""
    cdll_type = ctypes.CDLL
    if not (isinstance(cdll_type, type) and isinstance(lib, cdll_type)):
        return
    lib.nemu_connect.argtypes = [ctypes.c_wchar_p, ctypes.c_int]
    lib.nemu_connect.restype = ctypes.c_int
    lib.nemu_disconnect.argtypes = [ctypes.c_int]
    lib.nemu_disconnect.restype = None
    lib.nemu_capture_display.argtypes = [
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_void_p,
    ]
    lib.nemu_capture_display.restype = ctypes.c_int
    if getattr(lib, "nemu_get_display_id", None) is not None:
        lib.nemu_get_display_id.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        lib.nemu_get_display_id.restype = ctypes.c_int


class NemuIpcCapture:
    """Read frames from MuMu's local renderer IPC without changing game state."""
    _leases = {}
    _lease_lock = RLock()

    def __init__(
        self,
        mumu_root: str | Path,
        instance_id: int,
        *,
        packages: tuple[str, ...] | list[str] | None = None,
    ):
        self.mumu_root = Path(mumu_root)
        self.instance_id = int(instance_id)
        self.packages = tuple(packages) if packages is not None else DEFAULT_GAME_PACKAGES
        self._lib = None
        self._connection_id = 0
        self._display_id = 0
        self.dimensions: dict[str, int | str] = {}
        self._width = ctypes.pointer(ctypes.c_int(0))
        self._height = ctypes.pointer(ctypes.c_int(0))
        self._pixels = None
        self._lease_key = None

    def _candidate_display_ids(self) -> list[tuple[int, str]]:
        candidates: list[tuple[int, str]] = []
        seen: set[int] = set()
        if self._lib is not None and self._connection_id > 0:
            get_display_id = getattr(self._lib, "nemu_get_display_id", None)
            if callable(get_display_id):
                for pkg in self.packages:
                    try:
                        did = int(get_display_id(self._connection_id, pkg.encode("utf-8"), 0))
                    except (OSError, AttributeError, ctypes.ArgumentError, TypeError, ValueError):
                        continue
                    if did >= 0 and did not in seen:
                        candidates.append((did, pkg))
                        seen.add(did)
        if 0 not in seen:
            candidates.append((0, "default"))
        return candidates

    def _probe_display_and_size(self) -> tuple[int, tuple[int, int]]:
        last_did = 0
        last_result = -1
        last_size = (0, 0)
        empty = ctypes.POINTER(ctypes.c_int)()
        for display_id, source in self._candidate_display_ids():
            self._width.contents.value = 0
            self._height.contents.value = 0
            result = self._lib.nemu_capture_display(
                self._connection_id, display_id, 0, self._width, self._height, empty
            )
            size = (self._width.contents.value, self._height.contents.value)
            last_did, last_result, last_size = display_id, result, size
            if result == 0 and size in _VALID_SIZES:
                self._display_id = display_id
                self.dimensions = {
                    "display_id": display_id,
                    "display_source": source,
                    "width": size[0],
                    "height": size[1],
                }
                return display_id, size
        raise RuntimeError(
            f"mumu_ipc_resolution_failed:display={last_did}:result={last_result}:size={last_size}"
        )

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
        _configure_lib_signatures(self._lib)
        with self._lease_lock:
            connection_id = self._lib.nemu_connect(str(self.mumu_root), self.instance_id)
            if connection_id <= 0:
                raise RuntimeError(f"mumu_ipc_connect_failed:instance={self.instance_id}")
            self._connection_id = connection_id
            self._lease_key = (str(dll_path.resolve()).lower(), self.instance_id, connection_id)
            self._leases[self._lease_key] = self._leases.get(self._lease_key, 0) + 1
        try:
            self._probe_display_and_size()
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
        # Race scenes can rotate and background keep-alive tabs can move display IDs.
        display_id, _ = self._probe_display_and_size()
        result = self._lib.nemu_capture_display(
            self._connection_id,
            display_id,
            720 * 1280 * 4,
            self._width,
            self._height,
            self._pixels,
        )
        if result != 0:
            raise RuntimeError(f"mumu_ipc_capture_failed:display={display_id}:result={result}")
        # This installed MuMu build returns upside-down RGBA, not BGRA.
        width, height = self._width.contents.value, self._height.contents.value
        if (width, height) not in _VALID_SIZES:
            raise RuntimeError(f"mumu_ipc_resolution_failed:size={(width, height)}")
        self.dimensions["width"] = width
        self.dimensions["height"] = height
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
        self._display_id = 0
        self._pixels = None
