"""Optional installed-emulator frame readers for the portable package."""
from pathlib import Path
import time

_recent_failures = {}
_preferred_readers = {}


def reset_capture_probe(adb_path, port):
    """An explicit connection check may retry an unavailable SDK immediately."""
    target = (str(Path(adb_path).resolve()), port)
    for key in list(_recent_failures):
        if key[:2] == target:
            _recent_failures.pop(key, None)


class PortableNativeCapture:
    def __init__(self, adb_path, port, screen_size, *, preference='auto', dimension_provider=None):
        from core.ldplayer_capture import LDPlayerCapture
        from core.nemu_ipc_capture import NemuIpcCapture
        self.backend = 'adb-png'
        self.failures = []
        self.active = None
        self.dimensions = {}
        self._next = 0
        self.candidates = []
        self._cooldown = False
        adb = Path(adb_path).resolve()
        self._key = (str(adb), port, preference)
        previous = _recent_failures.get(self._key)
        if previous is not None and time.monotonic() - previous[0] < 30:
            self.failures = ['native_probe_cooldown'] + list(previous[1])
            self.dimensions.update(previous[2])
            self._cooldown = True
            return
        if preference in {'auto', 'ldplayer'} and (adb.parent / 'ldconsole.exe').is_file():
            # Share the same observed dimensions across the two documented
            # PIDs; a failed SDK must not repeat expensive display queries.
            observed = []
            def dimensions():
                if not observed:
                    from core.ldplayer_capture import discover_display_dimensions
                    try:
                        observed.append(dimension_provider() if dimension_provider is not None else
                                        discover_display_dimensions(adb, port))
                    except (OSError, RuntimeError, ValueError) as exc:
                        observed.append(exc)
                if isinstance(observed[0], Exception):
                    raise observed[0]
                return observed[0]
            # Both PID fields belong to the same explicitly selected list2
            # instance. SDK sample and current Maa versions use different ones.
            for pid_kind in ('player', 'vbox'):
                self.candidates.append((f'ldplayer-opengl:{pid_kind}',
                    lambda kind=pid_kind: LDPlayerCapture(adb, port, screen_size, pid_kind=kind,
                                                         dimension_provider=dimensions)))
        if preference in {'auto', 'mumu'}:
            for root in list(adb.parents)[:4]:
                sdk = root / 'nx_device/12.0/shell/sdk/external_renderer_ipc.dll'
                # MuMu's device manager exposes two ADB addresses per VM.
                # #0: 5555/16384; #1: 5557/16416 (confirmed by the user's
                # manager view). Keep input on the selected live address;
                # renderer IPC selects the same VM independently of ADB.
                index = {5555: 0, 16384: 0, 5557: 1, 16416: 1, 16448: 2}.get(port)
                if (sdk.is_file() and index is not None
                        and adb in {sdk.parent.parent/'adb.exe', root/'nx_main/adb.exe'}):
                    self.candidates.append(('mumu-ipc', lambda home=root, number=index: NemuIpcCapture(home, number)))
                    break
        preferred = _preferred_readers.get(self._key)
        self.candidates.sort(key=lambda row: row[0] != preferred)
        if not self.candidates:
            self.failures.append('installed_emulator_sdk_not_found_for_selected_adb_and_port')

    @property
    def available(self):
        return self.active is not None

    def read_image(self):
        import numpy as np
        if self._cooldown:
            return None
        while self.active is not None or self._next < len(self.candidates):
            name = self.backend
            try:
                if self.active is None:
                    name, factory = self.candidates[self._next]
                    self._next += 1
                    self.active = factory()
                    self.active.start()
                image = self.active.read_image()
                self.dimensions.update(getattr(self.active, 'dimensions', {}))
                if (not isinstance(image, np.ndarray) or image.dtype != np.uint8
                        or image.ndim != 3 or image.shape[2] != 3 or not image.size):
                    raise RuntimeError('native_frame_invalid')
                self.backend = name
                _preferred_readers[self._key] = name
                _recent_failures.pop(self._key, None)
                return image
            except (OSError, RuntimeError, ValueError, AttributeError) as exc:
                self.dimensions.update(getattr(self.active, 'dimensions', {}))
                self.failures.append(f'{name}:{type(exc).__name__}:{exc}')
                self.close()
        _recent_failures[self._key] = (time.monotonic(), list(self.failures), dict(self.dimensions))
        return None

    def close(self):
        active, self.active = self.active, None
        self.backend = 'adb-png'
        if active is not None:
            try:
                active.close()
            except (OSError, RuntimeError, AttributeError):
                pass

    def diagnostics(self):
        return {'backend': self.backend, 'native_available': self.available,
                'fallback_reasons': list(self.failures), 'dimensions': dict(self.dimensions)}
