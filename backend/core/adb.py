"""ADB operations for MuMu emulator screenshots and input."""
from __future__ import annotations
from desktop.umaa_public_context import endpoint as _public_endpoint
import os
import re
import subprocess
import tempfile
import time
import math
from config.settings import ADB_HOST, ADB_PATH, ADB_PORT, SWIPE_DELAY, TAP_DELAY
from core.coordinates import CoordinateConfig, CoordinateScaler, DEFAULT_REFERENCE_SIZE

class ADB:

    def __init__(self, host=ADB_HOST, port=ADB_PORT, *, skill_scroll_endpoint=_public_endpoint()):
        self.address = f'{host}:{port}'
        self._skill_scroll_endpoint = skill_scroll_endpoint
        self._connected = False
        self._coordinate_config = CoordinateConfig()
        self._screen_size_cache: tuple[int, int] | None = None

    def _run(self, *args, timeout=10):
        """Run an adb command and return (CompletedProcess, ok)."""
        cmd = [ADB_PATH, '-s', self.address] + list(args)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
            return (result, result.returncode == 0)
        except subprocess.TimeoutExpired:
            return (None, False)
        except FileNotFoundError as exc:
            raise RuntimeError('Cannot find adb. Check ADB_PATH in config/settings.py.') from exc

    def connect(self):
        """Connect to the emulator."""
        cmd = [ADB_PATH, 'connect', self.address]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
            stdout = result.stdout or ''
            stderr = result.stderr or ''
            output_lines = {line.strip().lower() for line in f'{stdout}\n{stderr}'.splitlines() if line.strip()}
            accepted_lines = {f'connected to {self.address}'.lower(), f'already connected to {self.address}'.lower()}
            if result.returncode == 0 and output_lines & accepted_lines:
                self._connected = True
                print(f'[ADB] connected to {self.address}')
                return True
            print(f'[ADB] connect failed: {stdout.strip()} {stderr.strip()}')
            return False
        except Exception as exc:
            print(f'[ADB] connect error: {exc}')
            return False

    def disconnect(self):
        """Disconnect from the emulator."""
        cmd = [ADB_PATH, 'disconnect', self.address]
        subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
        self._connected = False
        self._screen_size_cache = None
        print(f'[ADB] disconnected from {self.address}')

    def is_connected(self):
        """Return whether adb reports the device as connected."""
        result, ok = self._run('get-state')
        return ok and 'device' in (result.stdout if result else '')

    def screenshot(self, save_path=None, *, timeout: float | None=None, deadline: float | None=None):
        """Capture a screenshot and return it as a cv2 image."""
        import cv2
        capture_deadline: float | None = None
        if deadline is not None:
            capture_deadline = float(deadline)
            if not math.isfinite(capture_deadline):
                print('[ADB] screenshot timeout')
                return None
        elif timeout is not None:
            budget_seconds = float(timeout)
            if not math.isfinite(budget_seconds) or budget_seconds <= 0:
                print('[ADB] screenshot timeout')
                return None
            capture_deadline = time.monotonic() + budget_seconds

        def remaining_timeout(default_timeout: float) -> float:
            if capture_deadline is None:
                return default_timeout
            return capture_deadline - time.monotonic()
        remote_path = '/sdcard/screen.png'
        screencap_timeout = remaining_timeout(15)
        if screencap_timeout <= 0:
            print('[ADB] screenshot timeout')
            return None
        result, ok = self._run('shell', 'screencap', '-p', remote_path, timeout=screencap_timeout)
        if not ok:
            print('[ADB] screenshot failed')
            return None
        if save_path is None:
            save_path = os.path.join('logs', 'screenshots', 'screen.png')
        save_path = os.path.realpath(save_path)
        if save_path.startswith('\\\\?\\') and len(save_path) - 4 < 260:
            save_path = save_path[4:]
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        pull_timeout = remaining_timeout(10)
        if pull_timeout <= 0:
            print('[ADB] screenshot pull timeout')
            return None
        pull_path = save_path
        staged_pull_path: str | None = None
        if os.name == 'nt' and len(save_path) >= 220:
            destination_drive = os.path.splitdrive(save_path)[0].lower()
            stage_candidates = (os.getcwd(), tempfile.gettempdir())
            stage_dir = next((candidate for candidate in stage_candidates if os.path.splitdrive(os.path.abspath(candidate))[0].lower() == destination_drive), os.path.splitdrive(save_path)[0] + os.sep)
            descriptor, staged_pull_path = tempfile.mkstemp(prefix='uma-adb-screenshot-', suffix='.png', dir=stage_dir)
            os.close(descriptor)
            os.unlink(staged_pull_path)
            pull_path = staged_pull_path
        try:
            result, ok = self._run('pull', remote_path, pull_path, timeout=pull_timeout)
            if not ok:
                print('[ADB] screenshot pull failed')
                return None
            if staged_pull_path is not None:
                os.replace(staged_pull_path, save_path)
        finally:
            if staged_pull_path is not None and os.path.exists(staged_pull_path):
                os.unlink(staged_pull_path)
        img = cv2.imread(save_path)
        if img is None:
            print(f'[ADB] cannot read screenshot: {save_path}')
        return img

    def get_screen_size(self):
        """Return Android wm size, for example (1440, 2560)."""
        result, ok = self._run('shell', 'wm', 'size')
        if ok and result:
            match = re.search('(\\d+)x(\\d+)', result.stdout)
            if match:
                return (int(match.group(1)), int(match.group(2)))
        return (None, None)

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
        scaler = CoordinateScaler(reference_size=self._coordinate_config.reference_size, current_size=self._current_screen_size())
        return scaler.scale_point(x, y)

    def tap(self, x, y, scaled=True):
        """Tap a coordinate. Coordinates are reference-screen based by default."""
        if scaled:
            x, y = self.scale_point(x, y)
        _result, ok = self._run('shell', 'input', 'tap', str(int(x)), str(int(y)))
        time.sleep(TAP_DELAY)
        return bool(ok)

    def swipe(self, x1, y1, x2, y2, duration_ms=300, scaled=True):
        """Swipe. Coordinates are reference-screen based by default."""
        if scaled:
            x1, y1 = self.scale_point(x1, y1)
            x2, y2 = self.scale_point(x2, y2)
        self._run('shell', 'input', 'swipe', str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(int(duration_ms)))
        time.sleep(SWIPE_DELAY)

    def long_press(self, x, y, duration_ms=1000):
        """Long press at a coordinate."""
        self.swipe(x, y, x, y, duration_ms)

    def swipe_skill_overlap(self, *, reverse=False, travel_px=490):
        from core.skill_scroll import skill_overlap_shell
        if self._skill_scroll_endpoint not in (_public_endpoint(), _public_endpoint()) or self.address != self._skill_scroll_endpoint:
            return False
        _result, ok = self._run('shell', skill_overlap_shell(reverse=reverse, travel_px=travel_px))
        return bool(ok)

    def key_event(self, keycode):
        """Send an Android key event, for example BACK=4, HOME=3."""
        self._run('shell', 'input', 'keyevent', str(keycode))
        time.sleep(TAP_DELAY)

    def back(self):
        """Press BACK."""
        self.key_event(4)

    def home(self):
        """Press HOME."""
        self.key_event(3)
