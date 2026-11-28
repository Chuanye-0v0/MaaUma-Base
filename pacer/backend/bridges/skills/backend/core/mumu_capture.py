"""Configuration-bound native capture for UMAA; no input or device discovery calls."""
from pathlib import Path
import os
import re
import threading
import xml.etree.ElementTree as ET

import cv2

from core.nemu_ipc_capture import NemuIpcCapture


def resolve_instance(endpoint, adb_paths):
    """Resolve only explicit loopback ADB forwards, never a port-number formula."""
    match = re.fullmatch(r'127\.0\.0\.1:(\d+)', endpoint)
    if not match:
        return None
    port = int(match[1])
    roots = set()
    for adb in adb_paths:
        path = Path(adb).resolve()
        for parent in path.parents:
            if (parent / 'nx_device/12.0/shell/sdk/external_renderer_ipc.dll').is_file():
                roots.add(parent)
                break
    matches = set()
    for root in roots:
        for config in (root / 'vms').glob('MuMuPlayer-12.0-*/*.nemu'):
            instance = re.fullmatch(r'MuMuPlayer-12\.0-(\d+)', config.parent.name)
            if not instance or config.stem != config.parent.name:
                continue
            try:
                nodes = ET.parse(config).getroot().iter()
                for node in nodes:
                    if (node.tag.rsplit('}', 1)[-1] == 'Forwarding'
                            and node.get('hostip') == '127.0.0.1'
                            and node.get('proto') == '1'
                            and node.get('guestport') == '5555'
                            and node.get('hostport') == str(port)
                            and node.get('name', '').startswith('ADB_PORT')):
                        matches.add((root, int(instance[1])))
            except (OSError, ET.ParseError):
                # Incomplete configuration may conceal a conflicting mapping.
                return None
    return next(iter(matches)) if len(matches) == 1 else None


class NativeFrameSource:
    """Reuse one renderer connection, but read a fresh frame on every request."""
    def __init__(self, endpoint, adb_paths):
        self.endpoint = endpoint
        self.adb_paths = tuple(adb_paths)
        self._capture = None
        self._mapping = None
        adb_only = os.environ.get('UMA_CUT_CAPTURE_BACKEND', '').strip().lower() == 'adb'
        self._disabled = adb_only
        self._lock = threading.Lock()
        self.last_status = 'adb_configured' if adb_only else 'not_started'
        self.last_dimensions = {}

    def read_png(self):
        with self._lock:
            if self._disabled:
                return None
            try:
                mapping = resolve_instance(self.endpoint, self.adb_paths)
                if mapping is None or (self._mapping is not None and mapping != self._mapping):
                    raise RuntimeError('mapping_unavailable_or_changed')
                if self._capture is None:
                    self._mapping = mapping
                    self._capture = NemuIpcCapture(*mapping)
                    self._capture.start()
                frame = self._capture.read_image()
                self.last_dimensions = dict(getattr(self._capture, 'dimensions', {}) or {})
                if frame is None or frame.shape != (1280, 720, 3):
                    raise RuntimeError('native_frame_invalid')
                ok, png = cv2.imencode('.png', frame, [cv2.IMWRITE_PNG_COMPRESSION, 1])
                if not ok:
                    raise RuntimeError('native_encode_failed')
                self.last_status = 'native'
                return png.tobytes()
            except (OSError, RuntimeError, AttributeError, ValueError, cv2.error):
                self._disabled = True
                self.last_status = 'adb_fallback'
                self._close_capture()
                return None

    def disable_native(self, reason: str = 'native_fallback') -> None:
        with self._lock:
            self._disabled = True
            self.last_status = reason
            self._close_capture()

    def _close_capture(self):
        capture, self._capture = self._capture, None
        if capture is not None:
            try:
                capture.close()
            except (OSError, RuntimeError, AttributeError):
                pass

    def close(self):
        with self._lock:
            self._disabled = True
            self._close_capture()
