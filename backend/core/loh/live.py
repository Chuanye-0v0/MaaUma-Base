"""LOH's shared capture and single-input adapter; no device work on import.

The observer owns targets, policy owns resource permission, and the runner owns
durable entry intents. This module reuses the existing transport boundary and
task-scoped screenshot retention instead of introducing another controller.
"""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
import time
import cv2
import numpy as np
from core.action_recovery import ReobserveRequired, action_boundary, await_candidate, require_fresh_observation
from core.support_library.live import SupportDevice

def _observer():
    from core.loh.observer import observe
    return observe

def _same_candidate(action_id, before, expected):
    """A fresh target may move a few OCR pixels, but cannot change its effect."""
    left = before.get('actions', {}).get(action_id)
    right = expected.get('actions', {}).get(action_id)
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    for key in ('effect', 'uses_items'):
        if left.get(key) != right.get(key):
            return False
    if left.get('enabled', True) != right.get('enabled', True):
        return False
    from core.loh.policy import ENTRY_ACTIONS

    def resource(frame, action):
        if action_id in ENTRY_ACTIONS and action.get('effect') == 'admission' and ('resource_kind' not in action):
            return (frame.get('entry_resource') or {}).get('kind')
        return action.get('resource_kind')
    if resource(before, left) != resource(expected, right):
        return False
    kind = 'swipe' if 'swipe' in left else 'point'
    if ('swipe' in left) != ('swipe' in right):
        return False
    if kind == 'swipe' and left.get('duration_ms') != right.get('duration_ms'):
        return False
    a, b = (left.get(kind), right.get(kind))
    return isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)) and (len(a) == len(b)) and all((type(v) in (int, float) and math.isfinite(v) for v in [*a, *b])) and all((abs(x - y) <= 4 for x, y in zip(a, b)))

class LohDevice(SupportDevice):
    """Explicitly connected by run_loh, or supplied already connected by D1."""

    def __init__(self, endpoint, adb_path, output_dir, *, observer=None):
        self.observer = observer or _observer()
        self.book = {}
        self.options = {}
        super().__init__(endpoint, adb_path, output_dir)

    def capture(self):
        raw = self.command('-s', self.endpoint, 'exec-out', 'screencap', '-p')
        captured = time.monotonic()
        image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError('screenshot_invalid')
        if image.shape[:2] != (1280, 720):
            raise RuntimeError('portrait_720_required')
        rows, _ = self.ocr(image)
        tokens = [dict(text=text, confidence=float(confidence), bbox=[int(min((p[0] for p in box))), int(min((p[1] for p in box))), int(max((p[0] for p in box))), int(max((p[1] for p in box)))]) for box, text, confidence in rows or []]
        observed = dict(self.observer(image, ocr_items=tokens))
        path = self.output_dir / (str(time.time_ns()) + '.png')
        path.write_bytes(raw)
        observed.update(endpoint=self.endpoint, screenshot_path=str(path), frame_sha256=hashlib.sha256(raw).hexdigest(), captured_monotonic=captured, screen_size=[720, 1280], scale=1, tokens=tokens)
        path.with_suffix('.json').write_text(json.dumps(observed, ensure_ascii=False, indent=2), encoding='utf8')
        self._latest = observed
        return observed

    @staticmethod
    def _input(candidate):
        """Only transport the current observer's bounded native coordinates."""
        swipe = candidate.get('swipe')
        if swipe is not None:
            if not isinstance(swipe, (list, tuple)) or len(swipe) not in (4, 5):
                raise RuntimeError('loh_target_invalid')
            values = list(swipe)
            duration = candidate.get('duration_ms')
            if duration is not None:
                if len(values) == 5 and values[4] != duration:
                    raise RuntimeError('loh_target_invalid')
                if len(values) == 4:
                    values.append(duration)
        else:
            point = candidate.get('point')
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise RuntimeError('loh_target_invalid')
            values = list(point)
        if any((type(v) not in (int, float) or not math.isfinite(v) for v in values)):
            raise RuntimeError('loh_target_invalid')
        if any((not 0 <= values[i] < (720 if i % 2 == 0 else 1280) for i in range(4 if swipe is not None else 2))):
            raise RuntimeError('loh_target_invalid')
        if swipe is not None and len(values) == 5 and (not 0 < values[4] <= 5000):
            raise RuntimeError('loh_target_invalid')
        return ('swipe' if swipe is not None else 'tap', [str(int(v)) for v in values])

    @action_boundary
    def step(self, action_id, stop_event, expected):
        if stop_event.is_set():
            raise RuntimeError('operator_stop')
        before = self.capture()
        if before.get('page') != expected.get('page'):
            raise ReobserveRequired(action_id, before)
        if action_id not in before.get('actions', {}):
            before = await_candidate(self.capture, stop_event, action_id, before, expected.get('page'))
        if not _same_candidate(action_id, before, expected):
            raise ReobserveRequired(action_id, before)
        if before['actions'][action_id].get('effect') == 'admission':
            pending = self.book.get('pending') or {}
            last_action = self.book.get('last_action') or {}
            if pending.get('input_status') != 'reserved' or pending.get('action_id') != action_id or pending.get('source_page') != before.get('page') or (not pending.get('dispatch_id')) or (pending['dispatch_id'] != last_action.get('dispatch_id')):
                raise RuntimeError('loh_admission_not_reserved')
        from core.loh.policy import authorize
        permission = authorize(action_id, before, self.book, self.options)
        if not permission.get('ok'):
            raise RuntimeError(permission.get('reason') or 'loh_action_not_authorized')
        candidate = before['actions'][action_id]
        require_fresh_observation(before, action_id)
        kind, values = self._input(candidate)
        if stop_event.is_set():
            raise RuntimeError('operator_stop')
        self.command('-s', self.endpoint, 'shell', 'input', kind, *values)
        from core.navigation_wait import wait_transition
        after = wait_transition(self.capture, stop_event, before)
        receipt = dict(action_id=action_id, performed=True, source_frame_sha256=before['frame_sha256'], source_screenshot_path=before['screenshot_path'], observation=after)
        (self.output_dir / (str(time.time_ns()) + '-receipt.json')).write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf8')
        return receipt

def run_loh(endpoint, adb_path, output, emit, stop, options=None, resume_from=None, device=None, observer=None):
    """The explicit daily-task entry; injected devices retain caller ownership."""
    from core.loh.policy import normalize_options
    from core.loh.runner import LohRunner
    options = normalize_options(options)
    if device is not None:
        if device.endpoint != endpoint:
            raise ValueError('loh_device_endpoint_mismatch')
        if observer is not None:
            device.observer = observer
        return LohRunner(device, emit, stop, options=options, resume_from=resume_from).run()
    from core.screenshot_retention import ScreenshotSession
    directory = Path(resume_from).resolve().parent if resume_from else Path(output)
    owned = None
    with ScreenshotSession() as screenshots:
        try:
            owned = LohDevice(endpoint, adb_path, directory, observer=observer)
            owned.connect()
            result = LohRunner(owned, emit, stop, options=options, resume_from=resume_from).run()
            result['screenshot_retention'] = screenshots.finish(result, complete=bool(result.get('game_task_complete')))
            return result
        finally:
            if owned is not None:
                owned.close()
