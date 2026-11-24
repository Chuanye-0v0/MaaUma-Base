"""Unit tests for dynamic MuMu 12 display_id resolution in NemuIpcCapture."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
PACER_BACKEND = Path(os.environ.get("MAAUMA_PACER_BACKEND_DIR") or (REPO_ROOT / "pacer" / "backend"))


def _ensure_pacer_backend_path() -> None:
    resolved = str(PACER_BACKEND.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)


class _FakeNemuLib:
    """Fake external_renderer_ipc.dll supporting per-display sizes and pixels."""

    def __init__(
        self,
        *,
        pkg_display_map: dict[bytes, list[int]] | None = None,
        display_sizes: dict[int, tuple[int, int]] | None = None,
        display_pixels: dict[int, tuple[int, int, int, int]] | None = None,
        has_get_display_id: bool = True,
    ) -> None:
        self.pkg_display_map = {k: list(v) for k, v in (pkg_display_map or {}).items()}
        self.display_sizes = dict(display_sizes or {0: (720, 1280)})
        self.display_pixels = dict(display_pixels or {})
        self.calls: list[tuple] = []
        self.disconnected: list[int] = []
        if has_get_display_id:
            self.nemu_get_display_id = self._nemu_get_display_id

    def nemu_connect(self, root: str, instance_id: int) -> int:
        self.calls.append(("nemu_connect", root, instance_id))
        return 101 + instance_id

    def nemu_disconnect(self, connection_id: int) -> None:
        self.disconnected.append(connection_id)

    def _nemu_get_display_id(self, connection_id: int, pkg: bytes, app_index: int) -> int:
        self.calls.append(("nemu_get_display_id", connection_id, pkg, app_index))
        seq = self.pkg_display_map.get(pkg)
        if not seq:
            return -1
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def nemu_capture_display(
        self,
        connection_id: int,
        display_id: int,
        buffer_size: int,
        width_ptr,
        height_ptr,
        pixels_ptr,
    ) -> int:
        self.calls.append(("nemu_capture_display", connection_id, display_id, buffer_size))
        if display_id not in self.display_sizes:
            return -1
        w, h = self.display_sizes[display_id]
        width_ptr.contents.value = w
        height_ptr.contents.value = h
        if buffer_size > 0 and pixels_ptr:
            # Fill upside-down RGBA buffer: row 0 = bottom of final BGR image, row h-1 = top.
            rgba = np.zeros((h, w, 4), dtype=np.uint8)
            color = self.display_pixels.get(display_id, (10, 20, 30, 255))
            rgba[:] = color
            # Paint top row of final image (which is last row in raw MuMu bottom-up buffer) differently
            # so vertical flip is directly asserted.
            rgba[-1, :, :] = (200, 100, 50, 255)  # R=200, G=100, B=50 -> BGR (50, 100, 200) at y=0
            raw = rgba.tobytes()
            ctypes.memmove(pixels_ptr, raw, len(raw))
        return 0


class TestNemuIpcDisplayId(unittest.TestCase):
    def setUp(self) -> None:
        _ensure_pacer_backend_path()
        from core.nemu_ipc_capture import NemuIpcCapture

        NemuIpcCapture._leases.clear()
        self.NemuIpcCapture = NemuIpcCapture

    def _run_with_fake_lib(self, fake_lib: _FakeNemuLib, fn):
        dll_suffix = Path("nx_device/12.0/shell/sdk/external_renderer_ipc.dll")
        real_is_file = Path.is_file

        def fake_is_file(p: Path) -> bool:
            if p.as_posix().endswith(dll_suffix.as_posix()):
                return True
            return real_is_file(p)

        with patch("core.nemu_ipc_capture.ctypes.CDLL", return_value=fake_lib), patch.object(
            Path, "is_file", autospec=True, side_effect=fake_is_file
        ):
            cap = self.NemuIpcCapture(r"E:\FakeMuMu", 0)
            try:
                return fn(cap)
            finally:
                cap.close()

    def test_resolves_non_zero_display_id_and_flips_rgba_to_bgr(self) -> None:
        fake = _FakeNemuLib(
            pkg_display_map={b"com.bilibili.umamusu": [2]},
            display_sizes={0: (720, 1280), 2: (720, 1280)},
            display_pixels={2: (40, 80, 120, 255)},
        )

        def check(cap):
            cap.start()
            img = cap.read_image()
            self.assertEqual(img.shape, (1280, 720, 3))
            self.assertEqual(img.dtype, np.uint8)
            # Top row after flip should come from last row of upside-down RGBA (R=200,G=100,B=50 -> BGR=[50,100,200])
            self.assertEqual(tuple(int(v) for v in img[0, 0]), (50, 100, 200))
            # Body rows should convert RGBA (40,80,120,255) -> BGR (120,80,40)
            self.assertEqual(tuple(int(v) for v in img[10, 10]), (120, 80, 40))
            self.assertEqual(
                cap.dimensions,
                {
                    "display_id": 2,
                    "display_source": "com.bilibili.umamusu",
                    "width": 720,
                    "height": 1280,
                },
            )
            cap_dids = [c[2] for c in fake.calls if c[0] == "nemu_capture_display"]
            self.assertEqual(cap_dids, [2, 2, 2])
            self.assertNotIn(0, cap_dids)

        self._run_with_fake_lib(fake, check)
        self.assertEqual(fake.disconnected, [101])

    def test_dynamic_display_id_migration_mid_session(self) -> None:
        fake = _FakeNemuLib(
            pkg_display_map={b"com.bilibili.umamusu": [2, 2, 3]},
            display_sizes={0: (720, 1280), 2: (720, 1280), 3: (1280, 720)},
            display_pixels={2: (10, 20, 30, 255), 3: (70, 80, 90, 255)},
        )

        def check(cap):
            cap.start()
            self.assertEqual(cap.dimensions["display_id"], 2)
            img1 = cap.read_image()
            self.assertEqual(img1.shape, (1280, 720, 3))
            self.assertEqual(cap.dimensions["display_id"], 2)

            img2 = cap.read_image()
            self.assertEqual(img2.shape, (720, 1280, 3))
            self.assertEqual(tuple(int(v) for v in img2[10, 10]), (90, 80, 70))
            self.assertEqual(
                cap.dimensions,
                {
                    "display_id": 3,
                    "display_source": "com.bilibili.umamusu",
                    "width": 1280,
                    "height": 720,
                },
            )

        self._run_with_fake_lib(fake, check)

    def test_game_not_running_falls_back_to_display_zero(self) -> None:
        fake = _FakeNemuLib(
            pkg_display_map={b"com.bilibili.umamusu": [-1]},
            display_sizes={0: (720, 1280)},
            display_pixels={0: (11, 22, 33, 255)},
        )

        def check(cap):
            cap.start()
            img = cap.read_image()
            self.assertEqual(img.shape, (1280, 720, 3))
            self.assertEqual(
                cap.dimensions,
                {
                    "display_id": 0,
                    "display_source": "default",
                    "width": 720,
                    "height": 1280,
                },
            )

        self._run_with_fake_lib(fake, check)

    def test_transient_invalid_resolution_falls_back_to_display_zero(self) -> None:
        fake = _FakeNemuLib(
            pkg_display_map={b"com.bilibili.umamusu": [2]},
            display_sizes={2: (0, 0), 0: (720, 1280)},
        )

        def check(cap):
            cap.start()
            img = cap.read_image()
            self.assertEqual(img.shape, (1280, 720, 3))
            self.assertEqual(cap.dimensions["display_id"], 0)
            self.assertEqual(cap.dimensions["display_source"], "default")
            cap_dids = [c[2] for c in fake.calls if c[0] == "nemu_capture_display"]
            # start probes 2 (0x0 invalid) -> 0 (valid); read_image probes 2 -> 0 then captures 0
            self.assertEqual(cap_dids, [2, 0, 2, 0, 0])

        self._run_with_fake_lib(fake, check)

    def test_legacy_dll_without_get_display_id_falls_back_to_zero(self) -> None:
        fake = _FakeNemuLib(
            display_sizes={0: (720, 1280)},
            has_get_display_id=False,
        )

        def check(cap):
            cap.start()
            img = cap.read_image()
            self.assertEqual(img.shape, (1280, 720, 3))
            self.assertEqual(cap.dimensions["display_id"], 0)
            self.assertEqual(cap.dimensions["display_source"], "default")

        self._run_with_fake_lib(fake, check)


if __name__ == "__main__":
    unittest.main()
