"""Optional LDPlayer 9 native read-only frames; input remains with ADB.

ABI: MaaXYZ/EmulatorExtras 54d3a3ad448f0541df3759ae91b571a6762daaf1,
LD/dnopengl/dnopengl.h and main.cpp (manufacturer SDK sample).
MSVC x64 vtable: destructor=0, cap=1, release=2; the SDK sample uses
playerpid. MaaFramework's newer device finder uses VBox PID instead;
pid_kind defaults to the SDK sample and never probes another PID on failure.
Pixels: MaaFramework cc5fef675f42ca899e12bc4932ff37ac1278853c,
source/MaaAdbControlUnit/EmulatorExtras/LDPlayerExtras.cpp: BGR, bottom-up.
Port/list2: https://help.ldmnq.com/docs/LDPlayer-ming-ling-hang-kai-fa-zhe-shi-yong-shuo-ming
No manufacturer DLL is distributed or downloaded by this module.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from functools import lru_cache
import os
import re
from pathlib import Path
import struct
import subprocess
from threading import RLock


class LDPlayerCaptureError(RuntimeError):
    """Native capture is unavailable; the caller may use its ADB fallback."""


@dataclass(frozen=True)
class LDPlayerInstance:
    index: int
    name: str
    player_pid: int
    vbox_pid: int
    width: int | None
    height: int | None
    root: Path
    port: int

    @property
    def dll_path(self):
        return self.root / "ldopengl64.dll"


def _decode_output(output):
    if isinstance(output, str):
        return output
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return output.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise LDPlayerCaptureError("ldplayer_list2_encoding_unknown")


def _size(screen_size):
    if (not isinstance(screen_size, (tuple, list)) or len(screen_size) != 2
            or any(type(n) is not int or not 1 <= n <= 8192 for n in screen_size)
            or screen_size[0] * screen_size[1] > 16_777_216):
        raise LDPlayerCaptureError("ldplayer_screen_size_invalid")
    return tuple(screen_size)


def parse_display_dimensions(wm_text, window_text, input_text):
    """Keep geometry only, never dumpsys window/account text in diagnostics.

    MaaFramework's LD reader uses DisplayFrames w/h as the BGR24 layout.
    Only one unambiguous display geometry is accepted; an external display
    must not silently become the selected emulator's render dimensions.
    """
    info = {}
    for label, key in (("Physical", "physical_size"), ("Override", "override_size")):
        matches = re.findall(rf"{label} size:\s*(\d+)x(\d+)", wm_text, re.I)
        if len(set(matches)) == 1:
            info[key] = list(_size(tuple(map(int, matches[0]))))
    frames = re.findall(r"DisplayFrames\b[^\r\n]*?\bw=(\d+)\s+h=(\d+)\s+r=(\d+)\b", window_text)
    if len(set(frames)) != 1:
        info["dimension_error"] = "ldplayer_display_frames_missing_or_ambiguous"
        return info
    width, height, rotation = map(int, frames[0])
    info["display_size"] = list(_size((width, height)))
    info["display_rotation"] = rotation
    orientations = set(re.findall(r"SurfaceOrientation:\s*(\d+)\b", input_text))
    if len(orientations) == 1:
        info["surface_orientation"] = int(next(iter(orientations)))
    if rotation not in range(4) or info.get("surface_orientation") != rotation:
        info["dimension_error"] = "ldplayer_display_orientation_unconfirmed"
    return info


def discover_display_dimensions(adb_path, port, *, runner=None):
    """Three bounded read-only queries to the already selected endpoint."""
    outputs = []
    for command in (("wm", "size"), ("dumpsys", "window", "displays"), ("dumpsys", "input")):
        try:
            result = (runner or subprocess.run)(
                [str(adb_path), "-s", f"127.0.0.1:{port}", "shell", *command],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=5., shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LDPlayerCaptureError("ldplayer_display_query_failed") from exc
        if result.returncode:
            raise LDPlayerCaptureError("ldplayer_display_query_failed")
        outputs.append(_decode_output(result.stdout))
    return parse_display_dimensions(*outputs)


def _parse_row(line):
    first, separator, remaining = line.strip().partition(",")
    try:
        index = int(first)
        if not separator or index < 0:
            raise ValueError
    except ValueError as exc:
        raise LDPlayerCaptureError("ldplayer_list2_layout_invalid") from exc
    candidates = []
    for numeric_columns in (8, 5):
        fields = remaining.rsplit(",", numeric_columns)
        if len(fields) != numeric_columns + 1:
            continue
        try:
            top, bound, running, player_pid, vbox_pid, *dimensions = map(int, fields[1:])
            if running not in (0, 1) or top < 0 or bound < 0:
                raise ValueError
            width = height = None
            if dimensions:
                width, height, dpi = dimensions
                _size((width, height))
                if dpi <= 0:
                    raise ValueError
            candidates.append((index, fields[0], running, player_pid, vbox_pid, width, height))
        except (ValueError, LDPlayerCaptureError):
            continue
    if len(candidates) != 1:
        raise LDPlayerCaptureError("ldplayer_list2_layout_invalid_or_ambiguous")
    return candidates[0]


def discover_ldplayer(adb_path, port, *, runner=None):
    """Inspect only the installation beside the selected ADB and exact port.

    LD9/14 documents port=5555+2*index. Names may contain commas: the eight
    numeric columns following the name are parsed from the right. Traditional
    seven-column rows leave dimensions unknown; the caller supplies Android's
    confirmed display size. Ambiguous numeric names fail closed.
    """
    if type(port) is not int or not 5555 <= port <= 65535 or (port - 5555) % 2:
        raise LDPlayerCaptureError("ldplayer_port_not_mapped")
    adb = Path(adb_path).expanduser().resolve()
    if not adb.is_file() or adb.name.lower() != "adb.exe":
        raise LDPlayerCaptureError("ldplayer_selected_adb_missing")
    root = adb.parent
    console, dll = root / "ldconsole.exe", root / "ldopengl64.dll"
    for path in (console, dll):
        if not path.is_file() or path.resolve().parent != root:
            raise LDPlayerCaptureError(f"ldplayer_installation_file_missing:{path.name}")
    try:
        process = (runner or subprocess.run)(
            [str(console), "list2"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=5., shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), cwd=str(root),
        )
    except subprocess.TimeoutExpired as exc:
        raise LDPlayerCaptureError("ldplayer_list2_timeout") from exc
    except OSError as exc:
        raise LDPlayerCaptureError("ldplayer_list2_failed") from exc
    if process.returncode != 0:
        raise LDPlayerCaptureError(f"ldplayer_list2_exit:{process.returncode}")
    target, matches = (port - 5555) // 2, []
    for line in _decode_output(process.stdout).splitlines():
        if not line.strip():
            continue
        # Any malformed line could hide a duplicate target. Fail rather than
        # silently choosing a different or ambiguously identified instance.
        index, name, running, player_pid, vbox_pid, width, height = _parse_row(line)
        if index == target:
            matches.append((running, LDPlayerInstance(index, name, player_pid, vbox_pid,
                                                     width, height, root, port)))
    if len(matches) != 1:
        raise LDPlayerCaptureError("ldplayer_target_missing_or_ambiguous")
    running, instance = matches[0]
    if not running:
        raise LDPlayerCaptureError("ldplayer_target_not_running")
    return instance


@lru_cache(maxsize=1)
def _process_memory_api():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    kernel.ReadProcessMemory.restype = ctypes.c_int
    return kernel, kernel.GetCurrentProcess()


def _read_memory(address, length):
    """OS-checked copy: a null/unreadable pointer never becomes a Python view."""
    if type(address) is not int or address <= 0 or length <= 0:
        raise LDPlayerCaptureError("ldplayer_native_pointer_invalid")
    kernel, process = _process_memory_api()
    buffer, read = ctypes.create_string_buffer(length), ctypes.c_size_t()
    if not kernel.ReadProcessMemory(process, address, buffer, length, ctypes.byref(read)) or read.value != length:
        raise LDPlayerCaptureError("ldplayer_native_memory_unreadable")
    return buffer.raw


def _require_executable(address):
    """Reject non-code vtable entries before invoking a native method.

    MEMORY_BASIC_INFORMATION is 48 bytes on Win64; State/Protect start at 32.
    This validates page protection, not an arbitrary DLL version's ABI.
    """
    kernel, _ = _process_memory_api()
    query = kernel.VirtualQuery
    query.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    query.restype = ctypes.c_size_t
    information = ctypes.create_string_buffer(48)
    if not query(address, information, 48):
        raise LDPlayerCaptureError("ldplayer_native_method_unreadable")
    state, protection = struct.unpack_from("<II", information.raw, 32)
    if state != 0x1000 or protection & 0x101 or (protection & 0xFF) not in (0x10, 0x20, 0x40, 0x80):
        raise LDPlayerCaptureError("ldplayer_native_method_not_executable")


class LDPlayerCapture:
    """Persistent x64 screenshot object; start/read/close are serialized.

    screen_size is (width, height) from the caller's Android display query.
    Dependency injection supports ABI tests without an emulator or vendor DLL.
    """
    def __init__(self, adb_path, port, screen_size, *, pid_kind="player", runner=None, dll_loader=None, memory_reader=None,
                 dimension_provider=None):
        if pid_kind not in ("player", "vbox"):
            raise LDPlayerCaptureError("ldplayer_pid_kind_invalid")
        self.adb_path, self.port = adb_path, port
        self.pid_kind = pid_kind
        self.screen_size = _size(screen_size)
        self._runner, self._dll_loader = runner, dll_loader
        self._dimension_provider = dimension_provider
        self.dimensions = {"logical_size": list(self.screen_size)}
        self.native_size = None
        self.output_size = None
        self._read_memory = memory_reader or _read_memory
        self._lock = RLock()
        self._lib = self._handle = self._cap = self._release = None
        self.instance = None

    def start(self):
        with self._lock:
            if self._handle is not None:
                return self
            if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
                raise LDPlayerCaptureError("ldplayer_requires_windows_x64")
            instance = discover_ldplayer(self.adb_path, self.port, runner=self._runner)
            self.dimensions["list2_size"] = ([instance.width, instance.height]
                                             if instance.width is not None else None)
            info = (self._dimension_provider() if self._dimension_provider is not None else
                    discover_display_dimensions(self.adb_path, self.port, runner=self._runner))
            self.dimensions.update(info)
            if info.get("dimension_error"):
                raise LDPlayerCaptureError(info["dimension_error"])
            native = _size(info.get("display_size"))
            # The SDK returns no allocation length. Never increase the copy
            # beyond independently observed render/configuration geometry.
            bounds = [size for size in (self.dimensions["list2_size"], info.get("physical_size")) if size]
            if not bounds or any(native[0] * native[1] > _size(size)[0] * _size(size)[1] for size in bounds):
                raise LDPlayerCaptureError("ldplayer_native_size_unbounded")
            logical = self.screen_size
            wm_logical = info.get("override_size") or info.get("physical_size")
            if wm_logical is None or _size(wm_logical) != logical:
                raise LDPlayerCaptureError("ldplayer_logical_size_unconfirmed")
            rotation = info.get("display_rotation")
            if rotation not in range(4) or info.get("surface_orientation") != rotation:
                raise LDPlayerCaptureError("ldplayer_display_orientation_unconfirmed")
            output = logical[::-1] if rotation % 2 else logical
            if native[0] * output[1] != native[1] * output[0]:
                raise LDPlayerCaptureError("ldplayer_display_aspect_mismatch")
            self.native_size, self.output_size = native, output
            self.dimensions.update(native_size=list(native), expected_output_size=list(output),
                                   native_read_bytes=native[0] * native[1] * 3,
                                   native_dimension_source="dumpsys_display_frames")
            pid = instance.player_pid if self.pid_kind == "player" else instance.vbox_pid
            if not 0 < pid < 2**32:
                raise LDPlayerCaptureError(f"ldplayer_target_pid_invalid:{self.pid_kind}")
            handle = release = None
            try:
                lib = (self._dll_loader or ctypes.WinDLL)(str(instance.dll_path))
                create = lib.CreateScreenShotInstance
                create.argtypes, create.restype = [ctypes.c_uint, ctypes.c_uint], ctypes.c_void_p
                handle = create(instance.index, pid)
                handle = handle.value if isinstance(handle, ctypes.c_void_p) else handle
                if not handle:
                    raise LDPlayerCaptureError("ldplayer_create_instance_null")
                # Read the object/vtable using the same checked local-memory
                # copy used for frames, and pass explicit this on Win64.
                table, = struct.unpack("<Q", self._read_memory(handle, 8))
                if not table:
                    raise LDPlayerCaptureError("ldplayer_vtable_null")
                _, cap_address, release_address = struct.unpack("<QQQ", self._read_memory(table, 24))
                if not release_address:
                    raise LDPlayerCaptureError("ldplayer_release_method_null")
                _require_executable(release_address)
                release = ctypes.WINFUNCTYPE(None, ctypes.c_void_p)(release_address)
                if not cap_address:
                    raise LDPlayerCaptureError("ldplayer_capture_method_null")
                _require_executable(cap_address)
                cap = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(cap_address)
            except LDPlayerCaptureError:
                if release is not None:
                    release(handle)
                raise
            except (OSError, AttributeError, ValueError, TypeError, struct.error) as exc:
                if release is not None:
                    release(handle)
                raise LDPlayerCaptureError("ldplayer_native_initialization_failed") from exc
            self._lib, self._handle, self._cap, self._release = lib, handle, cap, release
            self.instance = instance
            return self

    def read_image(self):
        import numpy as np

        with self._lock:
            if self._handle is None:
                raise LDPlayerCaptureError("ldplayer_capture_not_started")
            try:
                pixels = self._cap(self._handle)
                if not pixels:
                    raise LDPlayerCaptureError("ldplayer_capture_pointer_null")
                width, height = self.native_size
                raw = self._read_memory(pixels, width * height * 3)
                # Preserve BGR channel order. Flip bottom-up rows and own the
                # result before a subsequent cap can overwrite SDK storage.
                image = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)[::-1].copy()
                if self.output_size != self.native_size:
                    import cv2
                    image = cv2.resize(image, self.output_size, interpolation=cv2.INTER_LINEAR)
                self.dimensions['output_size'] = list(self.output_size)
                return image
            except LDPlayerCaptureError:
                raise
            except (OSError, ValueError, TypeError) as exc:
                raise LDPlayerCaptureError("ldplayer_capture_failed") from exc

    def close(self):
        with self._lock:
            handle, release = self._handle, self._release
            self._handle = self._cap = self._release = None
            try:
                if handle is not None and release is not None:
                    release(handle)
            except OSError as exc:
                raise LDPlayerCaptureError("ldplayer_release_failed") from exc
            finally:
                self._lib = None
                self.instance = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()
