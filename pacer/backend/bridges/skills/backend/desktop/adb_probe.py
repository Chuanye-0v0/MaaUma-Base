"""Read-only ADB observation for the desktop controller."""
from __future__ import annotations
from desktop.umaa_public_context import endpoint as _public_endpoint
import hashlib
import re
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
DEFAULT_ADB_ENDPOINT = _public_endpoint()
_PNG_MAGIC = b'\x89PNG\r\n\x1a\n'
_KNOWN_MUMU_ADB_PATH = Path('')

class AdbProbe:
    """Connect to one local MuMu endpoint and collect read-only observations."""

    def __init__(self, adb_path: str='', endpoint: str=DEFAULT_ADB_ENDPOINT, output_dir: str | Path | None=None, command_runner: Callable[..., Any] | None=None) -> None:
        self.adb_path = str(adb_path or '').strip()
        self.endpoint = str(endpoint or '').strip()
        self.output_dir = Path(output_dir) if output_dir is not None else Path(tempfile.gettempdir()) / 'uma_controller'
        self._command_runner = command_runner or self._run_subprocess

    def run(self, stop_event=None) -> dict[str, object]:
        self._validate_endpoint(self.endpoint)
        result = {'ok': False, 'read_only': True, 'endpoint': self.endpoint, 'adb_path': None, 'state': None, 'screen_size': None, 'screenshot_path': None, 'screenshot_sha256': None, 'reason': 'probe_not_started'}
        if self._stopped(stop_event):
            result['reason'] = 'operator_stop'
            return result
        adb_path = self._resolve_adb_path()
        result['adb_path'] = adb_path
        if adb_path is None:
            result['reason'] = 'adb_not_found'
            return result
        connect, connect_error = self._run_command([adb_path, 'connect', self.endpoint])
        if connect_error is not None or connect is None:
            result['reason'] = connect_error or 'adb_connect_failed'
            return result
        connect_text = self._decode(getattr(connect, 'stdout', b''))
        if any((marker in connect_text.lower() for marker in ('failed to connect', 'cannot connect', 'unable to connect'))):
            result['reason'] = 'adb_connect_failed'
            return result
        if self._stopped(stop_event):
            result['reason'] = 'operator_stop'
            return result
        state_response, state_error = self._run_command([adb_path, '-s', self.endpoint, 'get-state'])
        if state_error is not None or state_response is None:
            result['reason'] = 'adb_state_failed'
            return result
        state = self._decode(getattr(state_response, 'stdout', b'')).strip()
        result['state'] = state
        if state != 'device':
            result['reason'] = 'adb_device_not_ready'
            return result
        if self._stopped(stop_event):
            result['reason'] = 'operator_stop'
            return result
        size_response, size_error = self._run_command([adb_path, '-s', self.endpoint, 'shell', 'wm', 'size'])
        if size_error is not None or size_response is None:
            result['reason'] = 'adb_screen_size_failed'
            return result
        size_match = re.search('(\\d+)\\s*x\\s*(\\d+)', self._decode(getattr(size_response, 'stdout', b'')))
        if size_match is None:
            result['reason'] = 'adb_screen_size_invalid'
            return result
        result['screen_size'] = [int(size_match.group(1)), int(size_match.group(2))]
        if self._stopped(stop_event):
            result['reason'] = 'operator_stop'
            return result
        screenshot_response, screenshot_error = self._run_command([adb_path, '-s', self.endpoint, 'exec-out', 'screencap', '-p'])
        if screenshot_error is not None or screenshot_response is None:
            result['reason'] = 'adb_screenshot_failed'
            return result
        screenshot = self._as_bytes(getattr(screenshot_response, 'stdout', b''))
        if not screenshot.startswith(_PNG_MAGIC):
            result['reason'] = 'adb_screenshot_invalid'
            return result
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            target = self.output_dir / f'device_{uuid.uuid4().hex}.png'
            target.write_bytes(screenshot)
        except OSError:
            result['reason'] = 'adb_screenshot_save_failed'
            return result
        result['ok'] = True
        result['reason'] = 'device_observed'
        result['screenshot_path'] = str(target)
        result['screenshot_sha256'] = hashlib.sha256(screenshot).hexdigest()
        return result

    def _resolve_adb_path(self) -> str | None:
        candidates: list[Path] = []
        if self.adb_path:
            configured = Path(self.adb_path)
            if configured.is_file():
                return str(configured)
            located = shutil.which(self.adb_path)
            if located:
                return str(Path(located))
            candidates.append(configured)
        else:
            located = shutil.which('adb')
            if located:
                return str(Path(located))
        candidates.append(_KNOWN_MUMU_ADB_PATH)
        try:
            from config.settings import ADB_PATH
            candidates.append(Path(ADB_PATH))
        except (ImportError, AttributeError):
            pass
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        return None

    @staticmethod
    def _validate_endpoint(endpoint: str) -> tuple[str, int]:
        if endpoint.count(':') != 1:
            raise ValueError('adb_endpoint_invalid')
        host, port_text = endpoint.rsplit(':', 1)
        if host.lower() not in {'127.0.0.1', 'localhost'}:
            raise ValueError('adb_endpoint_loopback_required')
        try:
            port = int(port_text)
        except ValueError as error:
            raise ValueError('adb_endpoint_invalid') from error
        if not 1 <= port <= 65535:
            raise ValueError('adb_endpoint_invalid')
        return (host, port)

    def _run_command(self, argv: list[str]):
        try:
            response = self._command_runner(argv, capture_output=True, timeout=10)
        except FileNotFoundError:
            return (None, 'adb_not_found')
        except subprocess.TimeoutExpired:
            return (None, 'adb_command_timeout')
        except OSError:
            return (None, 'adb_command_failed')
        if getattr(response, 'returncode', 1) != 0:
            return (None, 'adb_command_failed')
        return (response, None)

    @staticmethod
    def _run_subprocess(argv: list[str], **kwargs):
        return subprocess.run(argv, **kwargs)

    @staticmethod
    def _stopped(stop_event) -> bool:
        return bool(stop_event is not None and stop_event.is_set())

    @staticmethod
    def _decode(value: bytes | str) -> str:
        if isinstance(value, bytes):
            return value.decode('utf-8', errors='replace')
        return str(value or '')

    @staticmethod
    def _as_bytes(value: bytes | str) -> bytes:
        if isinstance(value, bytes):
            return value
        return str(value or '').encode('utf-8')
