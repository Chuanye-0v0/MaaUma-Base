"""Shared frame transport and bounded observation; never chooses game actions."""
from dataclasses import dataclass, field
import math
import os
import time

import cv2
from core.auto_trainer.pacer_storage import _io
from core.auto_trainer.runtime_control import for_device, capture_checkpoint


@dataclass
class FrameObservation:
    frame: object = None
    value: object = None
    reason: str = "timeout"
    polls: int = 0
    seconds: float = 0.
    error: str | None = None
    capture_latencies: list[float] = field(default_factory=list)


def wait_for_frame(adb, ready, *, timeout, interval=.1, max_polls=None,
                   initial_delay=False, observe=None, clock=time.monotonic, sleep=time.sleep):
    """Read each frame once; optional nonblocking observation sees the same frame.

    Read failures are returned to the caller's existing fallback. No taps, OCR,
    disk writes, retries of actions, or extra confirmation frames are introduced.
    """
    if not math.isfinite(timeout) or timeout < 0 or not math.isfinite(interval) or interval <= 0:
        raise ValueError("invalid_frame_wait_budget")
    limit = max_polls if max_polls is not None else math.ceil(timeout / interval) + 1
    started = clock()
    deadline = started + timeout
    result = FrameObservation()
    control = for_device(getattr(adb, "address", ""))
    accounted_pause = control.paused_seconds if control else 0.
    while clock() < deadline and result.polls < limit:
        if initial_delay or result.polls:
            sleep(min(interval, max(0., deadline - clock())))
        if clock() >= deadline:
            break
        try:
            capture_started = clock()
            frame = adb.read_frame(deadline=deadline)
        except (OSError, RuntimeError, AttributeError, cv2.error) as error:
            result.reason, result.error = "native_fallback", type(error).__name__
            break
        result.capture_latencies.append(max(0., clock() - capture_started))
        if control:
            deadline += control.paused_seconds - accounted_pause
            accounted_pause = control.paused_seconds
        if frame is None:
            result.reason = "frame_unavailable"
            break
        result.polls += 1
        result.frame = frame
        if clock() >= deadline:
            break
        value = ready(frame)
        if observe is not None:
            observe(frame, value)
        if value:
            result.value, result.reason = value, "ready"
            break
    result.seconds = max(0., clock() - started)
    return result


def tap_without_settle(adb, x, y, *, scaled=False):
    """Native input has no hidden sleep; ordinary ADB retains its old behavior."""
    tap = adb.tap_no_wait if getattr(adb, "supports_in_memory_capture", False) is True else adb.tap
    return tap(x, y, scaled=scaled)


class ExecutionFrames:
    """Route-local connection and one-use handoff, without owning route policy."""
    def __init__(self, adb):
        self.adb = adb
        self.connected = False
        self.size = None
        self.last_frame = None
        self.pending_frame = None
        self._frame_epoch = 0

    def __getattr__(self, name):
        return getattr(self.adb, name)

    def connect(self):
        if not self.connected:
            self.connected = bool(self.adb.connect())
        return self.connected

    def get_screen_size(self):
        if self.last_frame is not None:
            return self.last_frame.shape[1::-1]
        if self.size is None:
            self.size = self.adb.get_screen_size()
        return self.size

    def screenshot(self, path):
        control = for_device(getattr(self.adb, "address", ""))
        if control:
            capture_checkpoint(self.adb)
            epoch = control.epoch
            if epoch != self._frame_epoch:
                self.pending_frame = None
            self._frame_epoch = epoch
        frame, self.pending_frame = self.pending_frame, None
        if frame is None:
            frame = self.adb.screenshot(path)
            # Legacy screenshot adapters return the saved path, native returns
            # the pixels. Only the legacy path needs a disk decode.
            if isinstance(frame, (str, os.PathLike)):
                frame = cv2.imread(str(_io(frame)))
        else:
            self.adb.save_frame(path, frame)
        self.last_frame = frame
        return frame

    def tap(self, x, y, *, scaled=False):
        self.pending_frame = None
        return tap_without_settle(self.adb, x, y, scaled=scaled)

    def close(self):
        self.pending_frame = self.last_frame = self.size = None
        self.connected = False
        self.adb.close()
