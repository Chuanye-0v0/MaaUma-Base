"""ADB operations for MuMu emulator screenshots and input."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
import math
import struct
import threading
from functools import wraps
from contextvars import ContextVar

from config.settings import ADB_HOST, ADB_PATH, ADB_PORT, SWIPE_DELAY, TAP_DELAY
from core.coordinates import CoordinateConfig, CoordinateScaler, DEFAULT_REFERENCE_SIZE
from core.auto_trainer.runtime_control import capture_checkpoint, guarded_input, controlled_sleep, for_device


CAPTURE_FAILURE_SCHEMA = 'uma.capture_failure.v1'
_CAPTURE_BACKENDS = {'adb-png', 'adb-raw', 'adb-shell-png', 'mumu-ipc',
                     'ldplayer-opengl', 'portable-native', 'unknown'}
_CAPTURE_STAGES = {'connect', 'native', 'exec_out', 'shell', 'pull', 'decode', 'write', 'capture'}
_CAPTURE_REASONS = {'command_failed', 'timeout', 'empty_frame', 'decode_failed', 'write_failed',
                    'connection_failed', 'native_unavailable', 'exception', 'capture_failed',
                    'invalid_budget', 'unsupported_raw_protocol'}
_CAPTURE_ERRORS = {'OSError', 'PermissionError', 'FileNotFoundError', 'TimeoutError',
                   'TimeoutExpired', 'RuntimeError', 'ValueError', 'AttributeError', 'error'}

_PRE_SHELL_TRANSPORT_ERROR_PATTERNS = (
    "device not found",
    "device offline",
    "device unauthorized",
    "device still authorizing",
    "cannot connect to daemon",
    "protocol fault",
    "error: closed",
    "no devices/emulators found",
)


def _is_pre_shell_transport_disconnect(stdout: str | None, stderr: str | None) -> bool:
    """True only when adb host/daemon rejected transport before opening a device shell."""
    out = (stdout or "").strip()
    err = (stderr or "").strip()
    if out and not all(
        line.strip().lower().startswith(("* daemon ", "adb server "))
        for line in out.splitlines()
        if line.strip()
    ):
        return False
    combined = f"{out}\n{err}".lower()
    if "device '" in combined and "' not found" in combined:
        return True
    if "adb server version" in combined and "doesn't match" in combined:
        return True
    return any(pattern in combined for pattern in _PRE_SHELL_TRANSPORT_ERROR_PATTERNS)

# Optional capture-return telemetry, scoped to the calling context. The generic
# transport has no dependency on private career modules and never captures for it.
_capture_listener = ContextVar('adb_capture_listener', default=None)
_capture_listener_depth = ContextVar('adb_capture_listener_depth', default=0)


def bind_capture_listener(listener):
    return _capture_listener.set(listener)


def reset_capture_listener(token):
    _capture_listener.reset(token)


def safe_capture_failure(value):
    """Copy only bounded capture enums and numbers, never command output."""
    if not isinstance(value, dict) or value.get('schema_version') != CAPTURE_FAILURE_SCHEMA:
        return None
    if any(type(value.get(key)) is not str for key in ('backend', 'stage', 'reason')):
        return None
    if (value.get('backend') not in _CAPTURE_BACKENDS or value.get('stage') not in _CAPTURE_STAGES
            or value.get('reason') not in _CAPTURE_REASONS):
        return None
    result = {key: value[key] for key in ('schema_version', 'backend', 'stage', 'reason')}
    if type(value.get('timed_out')) is bool:
        result['timed_out'] = value['timed_out']
    if type(value.get('return_code')) is int and -(2**31) <= value['return_code'] < 2**32:
        result['return_code'] = value['return_code']
    if type(value.get('os_error_code')) is int and -(2**31) <= value['os_error_code'] < 2**32:
        result['os_error_code'] = value['os_error_code']
    if type(value.get('error_class')) is str and value['error_class'] in _CAPTURE_ERRORS:
        result['error_class'] = value['error_class']
    fallback = value.get('native_fallback')
    if (isinstance(fallback, dict) and type(fallback.get('backend')) is str and type(fallback.get('reason')) is str
            and fallback.get('backend') in _CAPTURE_BACKENDS
            and fallback.get('reason') in _CAPTURE_REASONS):
        result['native_fallback'] = {key: fallback[key] for key in ('backend', 'reason')}
        if type(fallback.get('error_class')) is str and fallback['error_class'] in _CAPTURE_ERRORS:
            result['native_fallback']['error_class'] = fallback['error_class']
    return result


def capture_failure(backend, stage, reason, *, timed_out=False, return_code=None, error=None, native_fallback=None):
    backend = 'ldplayer-opengl' if isinstance(backend, str) and backend.startswith('ldplayer-opengl:') else backend
    os_code = getattr(error, 'errno', None) if isinstance(error, OSError) else None
    if os_code is None and isinstance(error, OSError):
        os_code = getattr(error, 'winerror', None)
    value = {'schema_version': CAPTURE_FAILURE_SCHEMA,
             'backend': backend if type(backend) is str and backend in _CAPTURE_BACKENDS else 'unknown',
             'stage': stage if type(stage) is str and stage in _CAPTURE_STAGES else 'capture',
             'reason': reason if type(reason) is str and reason in _CAPTURE_REASONS else 'capture_failed',
             'timed_out': timed_out is True, 'return_code': return_code,
             'os_error_code': os_code,
             'error_class': type(error).__name__ if isinstance(error, Exception) else None,
             'native_fallback': native_fallback}
    return safe_capture_failure(value)


def _capture_exceptions(function):
    """Annotate a failure without changing the original exception or retry."""
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        listener = _capture_listener.get()
        depth = _capture_listener_depth.get()
        token = _capture_listener_depth.set(depth + 1) if listener is not None else None
        image = None
        try:
            image = function(self, *args, **kwargs)
            return image
        except Exception as error:
            backend, stage = getattr(self, '_capture_stage', ('unknown', 'capture'))
            previous = safe_capture_failure(getattr(self, 'last_capture_failure', None))
            if previous is None or previous['stage'] != stage:
                reason = 'write_failed' if stage == 'write' else 'decode_failed' if stage == 'decode' else 'exception'
                self._capture_failed(backend, stage, reason, error=error)
            raise
        finally:
            if token is not None:
                try:
                    if depth == 0:
                        listener(self, function.__name__, image)
                except Exception:
                    pass  # Optional telemetry cannot fail or repeat a game read.
                finally:
                    _capture_listener_depth.reset(token)
    return wrapped


class ADB:
    def __init__(self, host=ADB_HOST, port=ADB_PORT, *, skill_scroll_endpoint: str | None = None):
        self.address = f"{host}:{port}"
        self._runtime_control = for_device(self.address)
        self._connected = False
        self._coordinate_config = CoordinateConfig()
        self._screen_size_cache: tuple[int, int] | None = None
        self._portable_capture = None
        self._portable_capture_checked = False
        self._capture_dimensions = {}
        self._display_dimensions_cache = None
        self._wm_size_text = None
        if skill_scroll_endpoint is None:
            try:
                from desktop.umaa_public_context import endpoint as _public_endpoint
                skill_scroll_endpoint = _public_endpoint()
            except Exception:
                skill_scroll_endpoint = self.address
        self._skill_scroll_endpoint: str = skill_scroll_endpoint
        self._adb_frame_backend = 'adb-png'
        self._adb_comparison = None
        self.last_capture_failure = None
        self._capture_native_fallback = None
        self._last_adb_command_failure = None

    def _capture_failed(self, backend, stage, reason, *, result=None, error=None, timed_out=False):
        self.last_capture_failure = capture_failure(backend, stage, reason,
            timed_out=timed_out, return_code=getattr(result, 'returncode', None), error=error,
            native_fallback=getattr(self, '_capture_native_fallback', None))
        return self.last_capture_failure

    def _capture_succeeded(self):
        self.last_capture_failure = None
        self._capture_native_fallback = None

    def _begin_capture(self):
        self.last_capture_failure = None
        self._capture_native_fallback = getattr(self, '_capture_pending_native_fallback', None)
        self._capture_pending_native_fallback = None
        self._capture_stage = ('unknown', 'capture')

    def _capture_run(self, stage, *args, **kwargs):
        self._capture_stage = ('adb-shell-png', stage)
        self.last_capture_failure = None
        self._last_adb_command_failure = None
        try:
            result, ok = self._run(*args, **kwargs)
        except Exception as error:
            value = self._capture_failed('adb-shell-png', stage, 'exception', error=error)
            kind = (getattr(self, '_last_adb_command_failure', None) or {}).get('error_class')
            if type(kind) is str and kind in _CAPTURE_ERRORS:
                value['error_class'] = kind
            raise
        if not ok:
            timed_out = (getattr(self, '_last_adb_command_failure', None) or {}).get('timed_out') is True
            value = self._capture_failed('adb-shell-png', stage, 'timeout' if timed_out else 'command_failed',
                                         result=result, timed_out=timed_out)
            if timed_out:
                value['error_class'] = 'TimeoutExpired'
        return result, ok

    def _save_capture(self, save_path, image):
        try:
            self.save_frame(save_path, image)
        except Exception as error:
            self._capture_failed(self._diagnostic_capture_backend(), 'write', 'write_failed', error=error)
            raise
        self._capture_succeeded()
        return image

    def _diagnostic_capture_backend(self):
        try:
            return self.capture_backend_name
        except Exception:
            return 'unknown'

    def _native_fallback_diagnostic(self):
        native = getattr(self, '_portable_capture', None)
        failures = getattr(native, 'failures', None) or []
        if not failures:
            return
        raw = failures[-1]
        # SDK failure strings can contain local DLL paths. Keep only known
        # backend / exception classes, with a fixed public reason.
        parts = raw.split(':') if isinstance(raw, str) else []
        backend = parts[0] if parts and parts[0] == 'mumu-ipc' else (
            'ldplayer-opengl' if parts and parts[0] == 'ldplayer-opengl' else 'portable-native')
        value = {'backend': backend, 'reason': 'native_unavailable'}
        kind = next((part for part in parts[1:3] if part in _CAPTURE_ERRORS), None)
        if kind:
            value['error_class'] = kind
        self._capture_native_fallback = value

    @property
    def supports_in_memory_capture(self):
        return self._portable_capture is not None and self._portable_capture.available

    @property
    def capture_backend_name(self):
        return self._portable_capture.backend if self.supports_in_memory_capture else self._adb_frame_backend

    def capture_diagnostics(self):
        details = (self._portable_capture.diagnostics() if self._portable_capture is not None
                   else {'backend': 'adb-png', 'native_available': False, 'fallback_reasons': []})
        dimensions = dict(details.get('dimensions', {}))
        dimensions.update(self._capture_dimensions)
        details['dimensions'] = dimensions
        if not self.supports_in_memory_capture:details['backend']=self._adb_frame_backend
        if self._adb_comparison is not None:details['adb_comparison']=dict(self._adb_comparison)
        return details

    def _capture_display_dimensions(self):
        """Cache metadata for this connection; no display query on each frame."""
        if self._display_dimensions_cache is None:
            from core.ldplayer_capture import parse_display_dimensions
            if self._wm_size_text is None:
                self.get_screen_size()
            outputs = []
            for args in (("dumpsys", "window", "displays"), ("dumpsys", "input")):
                result, ok = self._run("shell", *args, timeout=5)
                outputs.append(result.stdout if ok and result else '')
            self._display_dimensions_cache = parse_display_dimensions(self._wm_size_text or '', *outputs)
            self._capture_dimensions.update(self._display_dimensions_cache)
        return dict(self._display_dimensions_cache)

    def close(self):
        capture, self._portable_capture = self._portable_capture, None
        self._portable_capture_checked = False
        if capture is not None:
            capture.close()

    def __del__(self):
        if getattr(self, '_portable_capture', None) is not None:
            self.close()

    @staticmethod
    def save_frame(save_path, image):
        from core.nemu_ipc_capture import NemuIpcCapture
        NemuIpcCapture.save_image(save_path, image)

    def _read_portable_native_frame(self, *, deadline=None):
        from core.cut_portable_runtime import portable_root, portable_capture_preference, device_ports
        if portable_root() is None or portable_capture_preference() == 'adb':
            return None
        port = next(iter(device_ports()))
        if self.address != f'127.0.0.1:{port}':
            raise ValueError('portable_capture_device_mismatch')
        paused = capture_checkpoint(self)
        if deadline is not None:
            deadline += paused
            if not math.isfinite(deadline) or time.monotonic() >= deadline:
                return None
        if not self._portable_capture_checked:
            self._portable_capture_checked = True
            from core.portable_capture import PortableNativeCapture
            # The SDK exposes a pixel pointer without a length. Never pass
            # the coordinate scaler's guessed fallback size to that reader.
            logical = self._capture_dimensions.get('logical_size')
            self._portable_capture = PortableNativeCapture(ADB_PATH, port,
                tuple(logical) if logical is not None else self.get_screen_size(),
                preference=portable_capture_preference(), dimension_provider=self._capture_display_dimensions)
        if self._portable_capture is None:
            return None
        image = self._portable_capture.read_image()
        if image is not None:
            self._capture_dimensions['output_size'] = [int(image.shape[1]), int(image.shape[0])]
        if deadline is not None and time.monotonic() >= deadline:
            return None
        return image

    def _reconnect_transport(self, *, timeout=5) -> bool:
        """Reconnect TCP adb transport without disturbing native capture state."""
        cmd = [ADB_PATH, "connect", self.address]
        try:
            result = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
        except Exception:
            return False
        output_lines = {
            line.strip().lower()
            for line in f"{result.stdout or ''}\n{result.stderr or ''}".splitlines()
            if line.strip()
        }
        accepted_lines = {
            f"connected to {self.address}".lower(),
            f"already connected to {self.address}".lower(),
        }
        if result.returncode == 0 and (output_lines & accepted_lines):
            self._connected = True
            return True
        return False

    def _run(self, *args, timeout=10):
        """Run an adb command and return (CompletedProcess, ok)."""
        self._last_adb_command_failure = None
        cmd = [ADB_PATH, "-s", self.address] + list(args)
        run_kwargs = dict(
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
        try:
            result = subprocess.run(cmd, **run_kwargs)
            if (
                result.returncode != 0
                and args
                and args[0] in {"shell", "get-state"}
                and _is_pre_shell_transport_disconnect(result.stdout, result.stderr)
                and self._reconnect_transport(timeout=min(5, max(1, timeout)))
            ):
                result = subprocess.run(cmd, **run_kwargs)
            return result, result.returncode == 0
        except subprocess.TimeoutExpired:
            self._last_adb_command_failure = {'timed_out': True, 'error_class': 'TimeoutExpired'}
            return None, False
        except FileNotFoundError as exc:
            self._last_adb_command_failure = {'error_class': 'FileNotFoundError'}
            raise RuntimeError(
                "Cannot find adb. Check ADB_PATH in config/settings.py."
            ) from exc

    def connect(self):
        """Connect to the emulator."""
        self._begin_capture()
        cmd = [ADB_PATH, "connect", self.address]
        try:
            result = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
            stdout = result.stdout or ""
            stderr = result.stderr or ""
            output_lines = {
                line.strip().lower()
                for line in f"{stdout}\n{stderr}".splitlines()
                if line.strip()
            }
            accepted_lines = {
                f"connected to {self.address}".lower(),
                f"already connected to {self.address}".lower(),
            }
            if result.returncode == 0 and output_lines & accepted_lines:
                self.close()
                self._screen_size_cache = None
                self._display_dimensions_cache = None
                self._wm_size_text = None
                self._capture_dimensions = {}
                self._connected = True
                self._capture_succeeded()
                print(f"[ADB] connected to {self.address}")
                return True
            print(f"[ADB] connect failed: {stdout.strip()} {stderr.strip()}")
            self._capture_failed('adb-shell-png', 'connect', 'connection_failed', result=result)
            return False
        except Exception as exc:
            self._capture_failed('adb-shell-png', 'connect', 'timeout' if isinstance(exc, subprocess.TimeoutExpired) else 'exception',
                                 error=exc, timed_out=isinstance(exc, subprocess.TimeoutExpired))
            print(f"[ADB] connect error: {exc}")
            return False

    def disconnect(self):
        """Disconnect from the emulator."""
        self.close()
        self._portable_capture_checked = False
        cmd = [ADB_PATH, "disconnect", self.address]
        subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
        self._connected = False
        self._screen_size_cache = None
        self._display_dimensions_cache = None
        self._wm_size_text = None
        self._capture_dimensions = {}
        print(f"[ADB] disconnected from {self.address}")

    def is_connected(self):
        """Return whether adb reports the device as connected."""
        result, ok = self._run("get-state")
        return ok and "device" in (result.stdout if result else "")

    @_capture_exceptions
    def screenshot(
        self,
        save_path=None,
        *,
        timeout: float | None = None,
        deadline: float | None = None,
    ):
        """Capture a screenshot and return it as a cv2 image."""
        capture_checkpoint(self)
        self._begin_capture()
        import cv2

        capture_deadline: float | None = None
        if deadline is not None:
            capture_deadline = float(deadline)
            if not math.isfinite(capture_deadline):
                self._capture_failed('adb-shell-png', 'capture', 'invalid_budget')
                print("[ADB] screenshot timeout")
                return None
        elif timeout is not None:
            budget_seconds = float(timeout)
            if not math.isfinite(budget_seconds) or budget_seconds <= 0:
                self._capture_failed('adb-shell-png', 'capture', 'invalid_budget')
                print("[ADB] screenshot timeout")
                return None
            capture_deadline = time.monotonic() + budget_seconds

        def remaining_timeout(default_timeout: float) -> float:
            if capture_deadline is None:
                return default_timeout
            return capture_deadline - time.monotonic()

        from core.cut_portable_runtime import generic_adb_capture
        self._capture_stage = ('portable-native', 'native')
        image = self._read_portable_native_frame(deadline=capture_deadline)
        if image is not None:
            self._capture_stage = (self._diagnostic_capture_backend(), 'write')
            return self._save_capture(save_path or os.path.join('logs', 'screenshots', 'screen.png'), image)
        self._native_fallback_diagnostic()
        if generic_adb_capture() and not getattr(self,"_portable_direct_failed",False):
            image=self._read_compatible_adb_frame(deadline=capture_deadline)
            if image is not None:
                self._capture_stage = (self._diagnostic_capture_backend(), 'write')
                self._save_capture(save_path or os.path.join('logs','screenshots','screen.png'),image)
            if image is not None:return image
            # Read-only compatibility fallback for an ADB build without PNG
            # exec-out support. Do not replay an input or change the target.
            self._portable_direct_failed=True

        remote_path = "/sdcard/screen.png"
        screencap_timeout = remaining_timeout(15)
        if screencap_timeout <= 0:
            self._capture_failed('adb-shell-png', 'shell', 'timeout', timed_out=True)
            print("[ADB] screenshot timeout")
            return None
        result, ok = self._capture_run('shell',
            "shell", "screencap", "-p", remote_path, timeout=screencap_timeout
        )
        if not ok:
            print("[ADB] screenshot failed")
            return None

        if save_path is None:
            save_path = os.path.join("logs", "screenshots", "screen.png")
        self._capture_stage = ('adb-shell-png', 'write')
        save_path = os.path.realpath(save_path)
        if save_path.startswith("\\\\?\\") and len(save_path) - 4 < 260:
            save_path = save_path[4:]
        self._capture_stage = ('adb-shell-png', 'write')
        os.makedirs(os.path.dirname(save_path), exist_ok=True)

        pull_timeout = remaining_timeout(10)
        if pull_timeout <= 0:
            self._capture_failed('adb-shell-png', 'pull', 'timeout', timed_out=True)
            print("[ADB] screenshot pull timeout")
            return None
        pull_path = save_path
        staged_pull_path: str | None = None
        if os.name == "nt" and len(save_path) >= 220:
            destination_drive = os.path.splitdrive(save_path)[0].lower()
            stage_candidates = (os.getcwd(), tempfile.gettempdir())
            stage_dir = next(
                (
                    candidate
                    for candidate in stage_candidates
                    if os.path.splitdrive(os.path.abspath(candidate))[0].lower()
                    == destination_drive
                ),
                os.path.splitdrive(save_path)[0] + os.sep,
            )
            descriptor, staged_pull_path = tempfile.mkstemp(
                prefix="uma-adb-screenshot-",
                suffix=".png",
                dir=stage_dir,
            )
            os.close(descriptor)
            os.unlink(staged_pull_path)
            pull_path = staged_pull_path
        try:
            result, ok = self._capture_run('pull',
                "pull",
                remote_path,
                pull_path,
                timeout=pull_timeout,
            )
            if not ok:
                print("[ADB] screenshot pull failed")
                return None
            if staged_pull_path is not None:
                self._capture_stage = ('adb-shell-png', 'write')
                os.replace(staged_pull_path, save_path)
        finally:
            if staged_pull_path is not None and os.path.exists(staged_pull_path):
                self._capture_stage = ('adb-shell-png', 'write')
                os.unlink(staged_pull_path)

        self._capture_stage = ('adb-shell-png', 'decode')
        img = cv2.imread(save_path)
        if img is None:
            self._capture_failed('adb-shell-png', 'decode', 'decode_failed')
            print(f"[ADB] cannot read screenshot: {save_path}")
        elif getattr(img, 'shape', None) is not None:
            self._capture_dimensions['output_size'] = [int(img.shape[1]), int(img.shape[0])]
            self._capture_dimensions['adb_png_size'] = [int(img.shape[1]), int(img.shape[0])]
        if img is not None:
            self._capture_succeeded()
        return img

    @_capture_exceptions
    def screenshot_exec_out(self, save_path=None, *, deadline=None):
        """Capture one PNG directly from adb, without an on-device file and pull."""
        image,payload=self._read_png_frame(deadline=deadline)
        if image is None:return None
        self._capture_stage = ('adb-png', 'write')
        save_path = os.path.realpath(save_path or os.path.join("logs","screenshots","screen.png"))
        if save_path.startswith("\\\\?\\") and len(save_path) - 4 < 260:
            save_path = save_path[4:]
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(save_path,"wb") as output:output.write(payload)
        self._capture_succeeded()
        return image

    @_capture_exceptions
    def read_frame(self, *, deadline=None):
        """Native or measured ADB encoding; no new input capability is implied."""
        self._begin_capture()
        self._capture_stage = ('portable-native', 'native')
        image = self._read_portable_native_frame(deadline=deadline)
        if image is not None:
            self._capture_succeeded()
            return image
        self._native_fallback_diagnostic()
        return self._read_compatible_adb_frame(deadline=deadline)

    def _read_compatible_adb_frame(self, *, deadline=None):
        from pathlib import Path
        from core.cut_portable_runtime import portable_root, portable_capture_preference
        if (portable_root() is None or portable_capture_preference() not in {'auto','ldplayer'}
                or not (Path(ADB_PATH).parent/'ldconsole.exe').is_file()):
            return self._read_png_frame(deadline=deadline)[0]
        from core.portable_adb_capture import read_adb_frame
        image,backend,comparison=read_adb_frame((str(Path(ADB_PATH).resolve()),self.address),
            self._read_raw_frame,lambda **kw:self._read_png_frame(**kw)[0],deadline=deadline,
            paused_seconds=lambda:self._runtime_control.paused_seconds if self._runtime_control else 0.)
        if image is not None:self._adb_frame_backend=backend
        self._adb_comparison=comparison
        return image

    @_capture_exceptions
    def _read_raw_frame(self, *, deadline=None):
        from pathlib import Path
        from core.adb_raw_capture import parse_adb_raw_screencap
        from core.portable_adb_capture import raw_protocol
        paused=capture_checkpoint(self)
        self.last_capture_failure = None
        self._capture_stage = ('adb-raw', 'exec_out')
        if deadline is not None:
            deadline=float(deadline)+paused
            if not math.isfinite(deadline) or deadline<=time.monotonic():
                self._capture_failed('adb-raw', 'exec_out', 'timeout', timed_out=True)
                return None
        def sdk_reader():
            remaining=5 if deadline is None else min(5,max(.001,deadline-time.monotonic()))
            try:
                result,ok=self._run('shell','getprop','ro.build.version.sdk',timeout=remaining)
            except (OSError,RuntimeError):
                return None
            return result.stdout if ok and result else None
        protocol=raw_protocol((str(Path(ADB_PATH).resolve()),self.address),sdk_reader)
        self._capture_dimensions['adb_raw_protocol']=protocol
        if protocol['header_bytes'] is None:
            self._capture_failed('adb-raw', 'exec_out', 'unsupported_raw_protocol')
            return None
        if deadline is not None and time.monotonic()>=deadline:
            self._capture_failed('adb-raw', 'exec_out', 'timeout', timed_out=True)
            return None
        timeout=15 if deadline is None else min(15,max(.001,deadline-time.monotonic()))
        try:
            result=subprocess.run([ADB_PATH,'-s',self.address,'exec-out','screencap'],
                stdin=subprocess.DEVNULL,capture_output=True,timeout=timeout,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0))
            if result.returncode or not result.stdout:
                self._capture_failed('adb-raw', 'exec_out', 'command_failed' if result.returncode else 'empty_frame', result=result)
                return None
            if deadline is not None and time.monotonic()>=deadline:
                self._capture_failed('adb-raw', 'exec_out', 'timeout', timed_out=True)
                return None
            self._capture_stage = ('adb-raw', 'decode')
            image,metadata=parse_adb_raw_screencap(result.stdout,header_bytes=protocol['header_bytes'])
        except (subprocess.TimeoutExpired,ValueError) as error:
            timed_out = isinstance(error, subprocess.TimeoutExpired)
            self._capture_failed('adb-raw', 'exec_out' if timed_out else 'decode',
                                 'timeout' if timed_out else 'decode_failed', error=error, timed_out=timed_out)
            return None
        except FileNotFoundError as exc:
            self._capture_failed('adb-raw', 'exec_out', 'exception', error=exc)
            raise RuntimeError('Cannot find adb. Check ADB_PATH in config/settings.py.') from exc
        except OSError as error:
            self._capture_failed('adb-raw', 'exec_out', 'exception', error=error)
            return None
        self._capture_dimensions.update(output_size=[int(image.shape[1]),int(image.shape[0])],
                                        adb_raw=metadata)
        self._adb_frame_backend='adb-raw'
        self._capture_succeeded()
        return image

    @_capture_exceptions
    def _read_png_frame(self, *, deadline=None):
        paused=capture_checkpoint(self)
        import cv2
        import numpy as np
        self.last_capture_failure = None
        self._capture_stage = ('adb-png', 'exec_out')

        if deadline is not None:
            deadline=float(deadline)+paused
            if not math.isfinite(deadline) or deadline<=time.monotonic():
                self._capture_failed('adb-png', 'exec_out', 'timeout', timed_out=True)
                return None,None
        timeout=15 if deadline is None else min(15,max(.001,deadline-time.monotonic()))

        cmd = [ADB_PATH, "-s", self.address, "exec-out", "screencap", "-p"]
        try:
            result = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
        except subprocess.TimeoutExpired as error:
            self._capture_failed('adb-png', 'exec_out', 'timeout', error=error, timed_out=True)
            print("[ADB] screenshot timeout")
            return None,None
        except FileNotFoundError as exc:
            self._capture_failed('adb-png', 'exec_out', 'exception', error=exc)
            raise RuntimeError("Cannot find adb. Check ADB_PATH in config/settings.py.") from exc
        if result.returncode != 0 or not result.stdout:
            self._capture_failed('adb-png', 'exec_out', 'command_failed' if result.returncode else 'empty_frame', result=result)
            print("[ADB] screenshot failed")
            return None,None
        if deadline is not None and time.monotonic()>=deadline:
            self._capture_failed('adb-png', 'exec_out', 'timeout', timed_out=True)
            return None,None
        self._capture_stage = ('adb-png', 'decode')
        img = cv2.imdecode(np.frombuffer(result.stdout, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            self._capture_failed('adb-png', 'decode', 'decode_failed')
            print("[ADB] cannot decode screenshot")
            return None,None
        self._capture_dimensions['output_size'] = [int(img.shape[1]), int(img.shape[0])]
        self._capture_dimensions['adb_png_size'] = [int(img.shape[1]), int(img.shape[0])]
        self._adb_frame_backend='adb-png'
        self._capture_succeeded()
        return img,result.stdout

    def open_raw_frame_stream(self):
        """Start a short-lived binary frame stream for training-page capture."""
        return RawScreencapStream(self.address).start()

    def get_screen_size(self):
        """Return Android wm size, for example (1440, 2560)."""
        result, ok = self._run("shell", "wm", "size")
        if ok and result:
            self._wm_size_text = result.stdout
            for label, key in (("Physical", "physical_size"), ("Override", "override_size")):
                observed = re.search(rf"{label} size:\s*(\d+)x(\d+)", result.stdout, re.IGNORECASE)
                if observed:
                    self._capture_dimensions[key] = [int(observed.group(1)), int(observed.group(2))]
            match = re.search(r"Override size:\s*(\d+)x(\d+)", result.stdout, re.IGNORECASE)
            match = match or re.search(r"(\d+)x(\d+)", result.stdout)
            if match:
                size = int(match.group(1)), int(match.group(2))
                self._capture_dimensions['logical_size'] = list(size)
                return size
        return None, None

    def _current_screen_size(self):
        if self._screen_size_cache:
            return self._screen_size_cache

        size = self.get_screen_size()
        if size and size[0] and size[1]:
            self._screen_size_cache = size
            return size

        self._screen_size_cache = DEFAULT_REFERENCE_SIZE
        return self._screen_size_cache

    def scale_point(self, x, y):
        """Scale reference coordinates to the current Android wm size."""
        scaler = CoordinateScaler(
            reference_size=self._coordinate_config.reference_size,
            current_size=self._current_screen_size(),
        )
        return scaler.scale_point(x, y)

    def tap(self, x, y, scaled=True):
        """Tap a coordinate. Coordinates are reference-screen based by default."""
        ok = self.tap_no_wait(x, y, scaled=scaled)
        controlled_sleep(TAP_DELAY)
        return ok

    @guarded_input
    def tap_no_wait(self, x, y, scaled=True):
        """Tap without sleeping; callers that observe the next frame handle settling."""
        if scaled:
            x, y = self.scale_point(x, y)
        _result, ok = self._run(
            "shell", "input", "tap", str(int(x)), str(int(y))
        )
        return bool(ok)

    @guarded_input
    def swipe(self, x1, y1, x2, y2, duration_ms=300, scaled=True):
        """Swipe. Coordinates are reference-screen based by default."""
        if scaled:
            x1, y1 = self.scale_point(x1, y1)
            x2, y2 = self.scale_point(x2, y2)
        _result, ok = self._run(
            "shell",
            "input",
            "swipe",
            str(int(x1)),
            str(int(y1)),
            str(int(x2)),
            str(int(y2)),
            str(int(duration_ms)),
        )
        time.sleep(SWIPE_DELAY)
        return bool(ok)

    def long_press(self, x, y, duration_ms=1000):
        """Long press at a coordinate."""
        self.swipe(x, y, x, y, duration_ms)

    @guarded_input
    def key_event(self, keycode):
        """Send an Android key event, for example BACK=4, HOME=3."""
        self._run("shell", "input", "keyevent", str(keycode))
        time.sleep(TAP_DELAY)

    def back(self):
        """Press BACK."""
        self.key_event(4)

    def home(self):
        """Press HOME."""
        self.key_event(3)

    def swipe_skill_overlap(self, *, reverse: bool = False, travel_px: int = 490) -> bool:
        from core.skill_scroll import skill_overlap_shell
        try:
            from desktop.umaa_public_context import endpoint as _public_endpoint
            target_endpoint = _public_endpoint()
        except Exception:
            target_endpoint = self.address
        if self._skill_scroll_endpoint != target_endpoint or self.address != target_endpoint:
            return False
        _result, ok = self._run("shell", skill_overlap_shell(reverse=reverse, travel_px=travel_px))
        return bool(ok)


class RawScreencapStream:
    """Keep adb transport open and retain only its newest complete RGBA frame."""

    _FRAME_SIZE = 720 * 1280 * 4

    def __init__(self, address: str):
        self.address = address
        self._process = None
        self._thread = None
        self._condition = threading.Condition()
        self._latest: bytes | None = None
        self._sequence = 0
        self._delivered_sequence = 0
        self._error: str | None = None
        self._closed = False

    def start(self):
        cmd = [
            ADB_PATH, "-s", self.address, "exec-out", "sh", "-c",
            "while true; do screencap; done",
        ]
        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
        self._thread = threading.Thread(target=self._read_frames, daemon=True)
        self._thread.start()
        return self

    def _read_exact(self, size: int) -> bytes:
        chunks = []
        remaining = size
        while remaining:
            chunk = self._process.stdout.read(remaining)
            if not chunk:
                raise RuntimeError("raw_screencap_stream_eof")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_frames(self):
        try:
            while True:
                header = self._read_exact(16)
                width, height, pixel_format, _color_space = struct.unpack("<IIII", header)
                if (width, height, pixel_format) != (720, 1280, 1):
                    raise RuntimeError(
                        f"unsupported_raw_screencap:{width}x{height}:format={pixel_format}"
                    )
                frame = self._read_exact(self._FRAME_SIZE)
                with self._condition:
                    if self._closed:
                        return
                    self._latest = frame
                    self._sequence += 1
                    self._condition.notify_all()
        except Exception as exc:
            with self._condition:
                if not self._closed:
                    self._error = f"{type(exc).__name__}:{exc}"
                    self._condition.notify_all()

    def screenshot(self, save_path: str, *, timeout: float = 15.0):
        import cv2
        import numpy as np

        deadline = time.monotonic() + timeout
        with self._condition:
            while self._sequence <= self._delivered_sequence and self._error is None:
                if self._closed:
                    raise RuntimeError("raw_screencap_stream_closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("raw_screencap_stream_timeout")
                self._condition.wait(remaining)
            if self._error is not None:
                raise RuntimeError(self._error)
            frame = self._latest
            self._delivered_sequence = self._sequence
        if frame is None:
            raise RuntimeError("raw_screencap_stream_empty")
        image = cv2.cvtColor(
            np.frombuffer(frame, dtype=np.uint8).reshape(1280, 720, 4),
            cv2.COLOR_RGBA2BGR,
        )
        encoded, png = cv2.imencode(
            ".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 1]
        )
        if not encoded:
            raise RuntimeError("raw_screencap_png_encode_failed")
        save_path = os.path.realpath(save_path)
        if save_path.startswith("\\\\?\\") and len(save_path) - 4 < 260:
            save_path = save_path[4:]
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(save_path, "wb") as output:
            output.write(png.tobytes())
        return image

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)
            if self._process.stdout is not None:
                self._process.stdout.close()
            self._process = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
