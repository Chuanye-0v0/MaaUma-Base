"""U1-only frame pacing for D1's skill completion transport.

No action selection, retries, or purchase authorization. This waits only after
the existing owner has tapped a visible success-overlay close control.
"""
import time

import cv2
import numpy as np


def success_close_box(image):
    """Cheap animation predicate, not proof that any skill was purchased."""
    if image is None or image.shape[:2] != (1280, 720):
        return None
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    green = (hsv[:, :, 0] >= 30) & (hsv[:, :, 0] <= 90) & (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 110)
    rows = np.flatnonzero(green[200:601, 15:705].mean(axis=1) > .75) + 200
    candidates = []
    for band in np.split(rows, np.flatnonzero(np.diff(rows) > 1) + 1):
        if not 40 <= len(band) <= 90:
            continue
        end = int(band[-1]) + 1
        y = end + 392
        icons = hsv[end+30:end+165, 30:680]
        button = hsv[y-32:y+32, 230:490]
        # A single pale/orange icon occupies only about 9% of this full row.
        # This is a pacing hint, never purchase or close authorization.
        if (np.mean((icons[:, :, 1] > 120) & (icons[:, :, 2] > 150)) > .05
                and np.mean((button[:, :, 1] < 55) & (button[:, :, 2] > 185)) > .70):
            candidates.append((218, y-38, 502, y+38))
    return candidates[0] if len(candidates) == 1 else None


def _box_from_png(raw):
    if not isinstance(raw, bytes) or not raw:
        return None, False
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    valid = image is not None and image.shape[:2] == (1280, 720)
    return success_close_box(image), valid


class CompletionFrameWaitMixin:
    completion_frame_timeout = 2.0
    completion_frame_interval = .08
    completion_clock = staticmethod(time.monotonic)
    completion_sleep = staticmethod(time.sleep)

    def command(self, *args):
        endpoint = self.endpoint
        capture = args == ("-s", endpoint, "exec-out", "screencap", "-p")
        if not capture:
            tap = len(args) == 7 and args[:5] == ("-s", endpoint, "shell", "input", "tap")
            box = getattr(self, "_completion_close_box", None)
            closing = False
            if tap and box is not None:
                try:
                    x, y = int(args[5]), int(args[6])
                    closing = box[0] <= x <= box[2] and box[1] <= y <= box[3]
                except (TypeError, ValueError):
                    pass
            result = super().command(*args)
            if args[:4] == ("-s", endpoint, "shell", "input"):
                self._completion_close_box = None
                self._completion_close_pending = closing
            return result

        raw = super().command(*args)
        box, valid = _box_from_png(raw)
        if getattr(self, "_completion_close_pending", False):
            self._completion_close_pending = False
            started = self.completion_clock()
            polls = 1
            deadline = started + self.completion_frame_timeout
            # No minimum wait: a ready frame is handed off immediately. Never
            # repeat the close input, even if the overlay fails to disappear.
            while box is not None and self.completion_clock() < deadline and polls < 26:
                self.completion_sleep(min(self.completion_frame_interval,
                    max(0., deadline - self.completion_clock())))
                raw = super().command(*args)
                box, valid = _box_from_png(raw)
                polls += 1
            self.completion_frame_waits = getattr(self, "completion_frame_waits", []) + [{
                "seconds": self.completion_clock() - started, "polls": polls,
                "overlay_gone": box is None and valid,
                "extra_actions": 0,
            }]
        self._completion_close_box = box
        return raw


def completion_device_class(base):
    class CompletionDevice(CompletionFrameWaitMixin, base):
        pass
    # Loaded by path: do not import U1's core into the isolated D1 namespace.
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("u1_worker_control",
        Path(__file__).with_name("larc_worker_control.py"))
    control = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(control)
    return control.controlled_device_class(CompletionDevice)
