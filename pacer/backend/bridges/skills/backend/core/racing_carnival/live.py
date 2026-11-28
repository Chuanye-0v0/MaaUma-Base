"""SDK transport for Racing Carnival; one observed input per action boundary."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import time

import cv2
import numpy as np

from core.action_recovery import action_boundary, await_candidate, ReobserveRequired, require_fresh_observation
from core.navigation_wait import wait_transition
from core.support_library.live import SupportDevice

from .observer import observe
from .policy import authorize


def same_candidate(left, right):
    for field in ['effect', 'resource_kind', 'cost', 'quantity', 'stock_before', 'stock_after', 'label']:
        if left.get(field) != right.get(field):
            return False
    key = 'swipe' if 'swipe' in left else 'point'
    if ('swipe' in left) != ('swipe' in right):
        return False
    a, b = left.get(key), right.get(key)
    return (
        isinstance(a, (list, tuple))
        and isinstance(b, (list, tuple))
        and len(a) == len(b)
        and all(type(x) in (int, float) and math.isfinite(x) for x in [*a, *b])
        and all(abs(x - y) <= 4 for x, y in zip(a, b))
    )


class RacingCarnivalDevice(SupportDevice):
    def __init__(self, endpoint, adb_path, output_dir, *, journal=None, options=None):
        self.journal = journal
        self.options = options or {}
        self._last_raw_sha = None
        self._last_raw_path = None
        self._last_token_sha = None
        self._last_tokens = None
        from .journal import game_day
        self.day_clock = game_day
        super().__init__(endpoint, adb_path, output_dir)

    def _region(self, image, tokens, box):
        x, y, r, b = box
        rows, _ = self.ocr(cv2.resize(image[y:b, x:r], None, fx=3, fy=3))
        for corners, text, confidence in rows or []:
            tokens.append(
                dict(
                    text=text,
                    confidence=float(confidence),
                    bbox=[
                        x + int(min(p[0] for p in corners) / 3),
                        y + int(min(p[1] for p in corners) / 3),
                        x + int(max(p[0] for p in corners) / 3),
                        y + int(max(p[1] for p in corners) / 3),
                    ],
                )
            )

    def _digits(self, image, tokens, box):
        x, y, r, b = box
        rows, _ = self.ocr(cv2.resize(image[y:b, x:r], None, fx=3, fy=3), use_det=False, use_cls=False)
        if rows and isinstance(rows[0], (tuple, list)) and len(rows[0]) == 2:
            text, confidence = rows[0]
            if str(text).isdigit() and float(confidence) >= .9:
                tokens.append(dict(text=str(text), confidence=float(confidence), bbox=list(box)))

    def capture(self):
        raw = self.command('-s', self.endpoint, 'exec-out', 'screencap', '-p')
        captured = time.monotonic()
        image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.shape[:2] != (1280, 720):
            raise RuntimeError('racing_carnival_portrait_720_required')
        digest = hashlib.sha256(raw).hexdigest()
        if digest == self._last_token_sha and self._last_tokens is not None:
            tokens = deepcopy(self._last_tokens)
        else:
            rows, _ = self.ocr(image)
            tokens = [
                dict(
                    text=text,
                    confidence=float(confidence),
                    bbox=[
                        int(min(p[0] for p in box)),
                        int(min(p[1] for p in box)),
                        int(max(p[0] for p in box)),
                        int(max(p[1] for p in box)),
                    ],
                )
                for box, text, confidence in rows or []
            ]
            if any('选择参赛' in t['text'] and t['bbox'][1] < 55 for t in tokens):
                from core.legend_selection.observer import read_tokens as read_selection_tokens
                tokens = read_selection_tokens(image, self.ocr)
        context = self.journal.data if self.journal is not None else {}
        frame = observe(image, tokens, context, raw=raw)
        if frame['page'] == 'carnival_refill_qty' and 'confirm_refill' not in frame['actions']:
            self._region(image, tokens, (300, 710, 465, 810))
            self._region(image, tokens, (505, 710, 650, 810))
            if frame.get('stock_before') is None:
                self._digits(image, tokens, (320, 725, 450, 785))
            if frame.get('stock_after') is None:
                self._digits(image, tokens, (515, 725, 635, 785))
            frame = observe(image, tokens, context, raw=raw)
        if 'home' not in frame['actions'] and frame['page'] in {'carnival_hub', 'carnival_closed', 'events'}:
            self._region(image, tokens, (280, 1200, 430, 1280))
            frame = observe(image, tokens, context, raw=raw)
        self._last_token_sha = digest
        self._last_tokens = deepcopy(tokens)
        path = Path(self._last_raw_path) if self._last_raw_sha == digest and self._last_raw_path else None
        if path is None or not path.is_file() or path.parent.resolve() != Path(self.output_dir).resolve():
            path = Path(self.output_dir) / (str(time.time_ns()) + '.png')
            path.write_bytes(raw)
        self._last_raw_sha = digest
        self._last_raw_path = str(path)
        frame.update(
            endpoint=self.endpoint,
            screenshot_path=str(path),
            captured_monotonic=captured,
            frame_sha256=digest,
            screen_size=[720, 1280],
        )
        from .storage import register
        register(self.journal, frame)
        from .journal import atomic_write
        atomic_write(Path(self.output_dir) / 'racing-carnival-observation.json', frame)
        self._latest = frame
        return frame

    @action_boundary
    def step(self, action, stop, expected):
        if stop.is_set():
            raise RuntimeError('operator_stop')
        if action == 'retry_network':
            self.connect()
        before = self.capture()
        if before.get('native_busy'):
            raise ReobserveRequired(action, before)
        alternatives = ()
        if action == 'claim_all':
            alternatives = ('close_missions', 'tab_period')
        elif action in {'maximum_refill', 'minimum_refill', 'increase_refill', 'decrease_refill'}:
            alternatives = ('confirm_refill', 'cancel')
        elif action in {'open_track_1', 'open_track_2', 'open_track_3', 'open_track_4'}:
            alternatives = ('hub_scroll_down', 'hub_scroll_up', 'enter_challenge', 'resume_race')
        elif action == 'confirm_selected':
            alternatives = ('cancel_selection',)
        elif action == 'view_result':
            alternatives = ('continue_race', 'cancel_preview')
        if before.get('page') in {'unknown', 'loading'} or action not in before.get('actions', {}):
            before = await_candidate(
                self.capture, stop, action, before, expected.get('page'),
                alternatives=alternatives,
            )
        if (
            before.get('page') != expected.get('page')
            or not same_candidate(before['actions'].get(action, {}), expected['actions'].get(action, {}))
        ):
            raise ReobserveRequired(action, before)
        ok, reason = authorize(action, before, self.journal.data, self.options)
        if not ok:
            raise RuntimeError(reason)
        require_fresh_observation(before, action)
        if action in {'enter_race', 'confirm_refill', 'claim_all'}:
            if self.day_clock() != self.journal.data['scope']['day']:
                raise RuntimeError('racing_carnival_game_day_changed_before_submit')
        candidate = before['actions'][action]
        key = 'swipe' if 'swipe' in candidate else 'point'
        values = candidate[key]
        if (
            len(values) != (5 if key == 'swipe' else 2)
            or any(type(x) not in (int, float) or not math.isfinite(x) for x in values)
            or any(not 0 <= values[i] < (720 if i % 2 == 0 else 1280) for i in range(4 if key == 'swipe' else 2))
            or key == 'swipe' and not 0 < values[4] <= 1500
        ):
            raise RuntimeError('racing_carnival_target_invalid')
        if stop.is_set():
            raise RuntimeError('operator_stop')
        self.journal.save()
        sent = time.monotonic()
        self.command(
            '-s', self.endpoint, 'shell', 'input', 'swipe' if key == 'swipe' else 'tap',
            *[str(int(x)) for x in values],
        )
        if action == 'skip_race':
            after = wait_transition(
                self.capture,
                stop,
                before,
                timeout=1.2,
                same_page_ready=lambda obs: (
                    (obs.get('captured_monotonic', 0) - sent >= 0.15)
                    and 'skip_race' in obs.get('actions', {})
                ),
            )
        elif action == 'continue_notice':
            after = wait_transition(
                self.capture,
                stop,
                before,
                timeout=3.5 if before.get('reason') == 'native_carnival_challenge_expired' else 1.5,
                same_page_ready=lambda obs: (
                    (obs.get('captured_monotonic', 0) - sent >= 0.20)
                    and obs.get('frame_sha256') != before.get('frame_sha256')
                ),
            )
            if before.get('reason') == 'native_carnival_challenge_expired':
                deadline = time.monotonic() + 3.5
                while after.get('page') in {'selection', 'carnival_detail', 'loading', 'unknown'} and time.monotonic() < deadline:
                    if stop.wait(0.25):
                        raise RuntimeError('operator_stop_after_submit')
                    after = self.capture()
        elif action.startswith('horse_') or action in {'sort_evaluation', 'descending', 'scroll_top', 'scroll_next'}:
            if stop.wait(0.55):
                raise RuntimeError('operator_stop_after_submit')
            after = self.capture()
        elif action in {'hub_scroll_down', 'hub_scroll_up', 'event_scroll_down', 'event_scroll_up'}:
            after = wait_transition(self.capture, stop, before, timeout=2.0)
        else:
            after = wait_transition(self.capture, stop, before)
        receipt = dict(
            action_id=action,
            performed=True,
            source_frame_sha256=before['frame_sha256'],
            source_screenshot_path=before.get('screenshot_path'),
            input_sent_monotonic=sent,
            observation=after,
        )
        path = Path(self.output_dir) / (str(time.time_ns()) + '-receipt.json')
        path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf8')
        return receipt
