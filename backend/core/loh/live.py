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

from core.action_recovery import (
    ReobserveRequired, action_boundary, await_candidate, require_fresh_observation,
)
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
    for key in ('effect', 'uses_items', 'like_key', 'like_group', 'like_race','watch_key','watch_group'):
        if left.get(key) != right.get(key):
            return False
    if left.get('enabled', True) != right.get('enabled', True):
        return False
    from core.loh.policy import ENTRY_ACTIONS
    def resource(frame, action):
        # Match policy: a page-level entry quote describes admission only. It
        # must not turn unrelated result/navigation controls into consumption.
        if (action_id in ENTRY_ACTIONS and action.get('effect') == 'admission'
                and 'resource_kind' not in action):
            return (frame.get('entry_resource') or {}).get('kind')
        return action.get('resource_kind')
    if resource(before, left) != resource(expected, right):
        return False
    kind = 'swipe' if 'swipe' in left else 'point'
    if ('swipe' in left) != ('swipe' in right):
        return False
    if kind == 'swipe' and left.get('duration_ms') != right.get('duration_ms'):
        return False
    a, b = left.get(kind), right.get(kind)
    return (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple))
            and len(a) == len(b) and all(
                type(v) in (int, float) and math.isfinite(v) for v in [*a, *b])
            and all(abs(x-y) <= 4 for x, y in zip(a, b)))


class LohDevice(SupportDevice):
    """Explicitly connected by run_loh, or supplied already connected by D1."""

    def __init__(self, endpoint, adb_path, output_dir, *, observer=None):
        self.observer = observer or _observer()
        self.book = {}
        self.options = {}
        super().__init__(endpoint, adb_path, output_dir)

    @staticmethod
    def _merge_spectator_caption(tokens, token):
        from core.loh.observer import _text
        x,y,r,b = token['bbox']
        old = next((t for t in tokens if _text(t['text']) == _text(token['text'])
            and abs(t['bbox'][0]+t['bbox'][2]-x-r) <= 16
            and abs(t['bbox'][1]+t['bbox'][3]-y-b) <= 16), None)
        if old is None:
            tokens.append(token)
        elif token['confidence'] > old['confidence']:
            tokens[tokens.index(old)] = token

    @staticmethod
    def _merge_home_caption(tokens, regional):
        """Replace only the same measured Home caption from the full pass."""
        x, y, r, b = regional['bbox']
        def same_caption(token):
            if token.get('text', '').replace(' ', '') != '主页':
                return False
            a, c, d, e = token['bbox']
            return abs((a+d-x-r)/2) <= 4 and abs((c+e-y-b)/2) <= 4
        tokens[:] = [token for token in tokens if not same_caption(token)]
        tokens.append(regional)

    def capture(self):
        raw = self.command('-s', self.endpoint, 'exec-out', 'screencap', '-p')
        captured = time.monotonic()
        image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError('screenshot_invalid')
        if image.shape[:2] != (1280, 720):
            raise RuntimeError('portrait_720_required')
        pending = self.book.get('pending') or {}
        if (not getattr(self, '_loh_fast_skip_submitted', False)
                and self.book.get('loh_context_verified') and self.book.get('prefer_short_mode')
                and pending.get('progress_seen') and pending.get('input_status') in {'submitted', 'adopted'}
                and (self.book.get('last_action') or {}).get('action_id') in {'view_result', 'skip_race'}):
            # This brief skip-all window can close before full-frame OCR ends.
            # Only this owned, neutral glyph gets the read-only visual fast path.
            from core.loh.observer import observe_owned_fast_skip
            fast = observe_owned_fast_skip(image)
            if fast is not None:
                return self._save_capture(raw, captured, fast, [])
            sent = getattr(self, '_loh_view_result_sent', None)
            if (self.book['last_action']['action_id'] == 'view_result' and sent is not None
                    and 0 <= time.monotonic() - sent < 1.5):
                # Old preview/white animation frames do not need full OCR in
                # this brief read-only window. Keep polling for the live glyph.
                return self._save_capture(raw, captured,
                    dict(page='loading',reason='loh_short_presentation_rendering',actions={}), [])
        from .final_watch import observe_fast_skip
        fast_watch=observe_fast_skip(image,self.book)
        if fast_watch is not None:
            return self._save_capture(raw,captured,fast_watch,[])
        rows, _ = self.ocr(image)
        tokens = [dict(text=text, confidence=float(confidence), bbox=[
            int(min(p[0] for p in box)), int(min(p[1] for p in box)),
            int(max(p[0] for p in box)), int(max(p[1] for p in box))])
            for box, text, confidence in rows or []]
        observed = dict(self.observer(image, ocr_items=tokens))
        if (observed.get('page') == 'loh_spectator_list'
                and (observed.get('like_group') is None or
                     not observed.get('like_voted') and 'spectator_vote' not in observed.get('actions', {}))
                or observed.get('page') == 'loh_spectator_reward' and not observed.get('actions')):
            from core.ocr_regions import read_regions
            regions = ([(10,450,710,530),(510,650,710,1030)]
                       if observed.get('page') == 'loh_spectator_list' else [(200,740,520,950)])
            for token in read_regions(image, self.ocr, regions):
                self._merge_spectator_caption(tokens, token)
            observed = dict(self.observer(image, ocr_items=tokens))
        if ((observed.get('page') == 'loh_no_tickets' and 'back_loh' not in observed.get('actions', {})
                and 'continue_result' not in observed.get('actions', {})) or
                (not observed.get('actions') and
                 {'集结战积分','获得的集结战积分','英杰槽'} <= {t['text'].replace(' ','') for t in tokens})):
            # Reuse the normal regional reader on this exact frame: whole-frame
            # OCR can miss the stylized Next caption at the settled summary.
            from core.ocr_regions import read_regions
            tokens += read_regions(image, self.ocr, [(20,1100,705,1250)])
            observed = dict(self.observer(image, ocr_items=tokens))
        if (observed.get('page') in {'loh_no_tickets', 'loh_closed'}
                and (observed.get('page') == 'loh_no_tickets'
                     or observed.get('unavailable_verified') is True)
                and not observed.get('actions')):
            # The tiny two-character Home footer can split/disappear in the
            # full-frame pass. Read that same native caption enlarged, then
            # map its actual OCR box back; never substitute a fixed tap point.
            rows, _ = self.ocr(cv2.resize(image[1200:1280,290:430], None, fx=2, fy=2))
            for box, text, confidence in rows or []:
                if text.replace(' ', '') != '主页':
                    continue
                self._merge_home_caption(tokens, dict(text=text,confidence=float(confidence),bbox=[
                    290+int(min(p[0] for p in box)/2),1200+int(min(p[1] for p in box)/2),
                    290+int(max(p[0] for p in box)/2),1200+int(max(p[1] for p in box)/2)]))
            observed = dict(self.observer(image, ocr_items=tokens))
        if self.book.get('loh_context_verified'):
            # Tutorial steps also belong to this task. The shared recovery
            # recognizes the same owner/control and leaves capture read-only.
            from core.loh.observer import observe_owned_tutorial
            tutorial = observe_owned_tutorial(image,tokens)
            if tutorial is not None:
                observed = dict(tutorial)
        pending = self.book.get('pending') or {}
        if (observed.get('page') == 'unknown' and self.book.get('loh_context_verified')
                and pending.get('progress_seen') and pending.get('input_status') in {'submitted','adopted'}):
            # Repeated race playback belongs to LOH's own single-input loop,
            # rather than the finite generic-interruption budget.
            from core.loh.observer import observe_owned_playback
            playback = observe_owned_playback(image,tokens)
            if playback is not None:
                observed = dict(playback)
        from .final_watch import observe as observe_watch, requirement as watch_requirement
        if watch_requirement(self.book):
            viewing=observe_watch(image,tokens,self.book,observed)
            if viewing is None or not viewing.get('actions'):
                if observed.get('page') in {'unknown','notice'} or viewing and viewing.get('page') in {'loh_watch_preview','loh_watch_settings','loh_watch_reward','loh_watch_result','loh_watch_lineup'}:
                    from core.ocr_regions import read_regions
                    for token in read_regions(image,self.ocr,[(180,350,600,470),(60,760,690,930),(160,500,560,750),(180,1100,700,1270)]):
                        self._merge_spectator_caption(tokens,token)
                    observed=dict(self.observer(image,ocr_items=tokens))
                    viewing=observe_watch(image,tokens,self.book,observed)
            if viewing is not None:observed=viewing
        return self._save_capture(raw, captured, observed, tokens)

    def _save_capture(self, raw, captured, observed, tokens):
        # Actual capture metadata cannot be supplied by an injected observer.
        observed = dict(observed)
        path = self.output_dir / (str(time.time_ns()) + '.png')
        path.write_bytes(raw)
        observed.update(endpoint=self.endpoint, screenshot_path=str(path),
                        frame_sha256=hashlib.sha256(raw).hexdigest(),
                        captured_monotonic=captured, screen_size=[720, 1280],
                        scale=1, tokens=tokens)
        path.with_suffix('.json').write_text(
            json.dumps(observed, ensure_ascii=False, indent=2), encoding='utf8')
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
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            raise RuntimeError('loh_target_invalid')
        if any(not 0 <= values[i] < (720 if i % 2 == 0 else 1280)
               for i in range(4 if swipe is not None else 2)):
            raise RuntimeError('loh_target_invalid')
        if swipe is not None and len(values) == 5 and not 0 < values[4] <= 5000:
            raise RuntimeError('loh_target_invalid')
        return ('swipe' if swipe is not None else 'tap'), [str(int(v)) for v in values]

    @action_boundary
    def step(self, action_id, stop_event, expected):
        if stop_event.is_set():
            raise RuntimeError('operator_stop')
        if action_id=='retry_network':
            # Native screenshots can remain available after the ADB input
            # transport disconnects. Reconnect before this fresh neutral retry.
            self.connect()
        before = self.capture()
        # An acknowledged shortening toggle can render after the runner's
        # post-input frame. Its replacement result control requires a fresh
        # policy decision, never another toggle or a replay of entry.
        alternatives={'enable_short':('view_result',),
                      'select_title_candidate':('confirm_title',)}.get(action_id,())
        if before.get('page') in {'unknown','loading'}:
            # A pulsing caption can vanish on the fresh frame. Wait read-only
            # for this same control, rather than spending the replan budget.
            before = await_candidate(self.capture,stop_event,action_id,before,expected.get('page'),
                                     alternatives=alternatives)
        if before.get('page') != expected.get('page'):
            raise ReobserveRequired(action_id, before)
        if action_id not in before.get('actions', {}):
            before = await_candidate(self.capture, stop_event, action_id, before,
                                     expected.get('page'),alternatives=alternatives)
        if not _same_candidate(action_id, before, expected):
            raise ReobserveRequired(action_id, before)
        if before['actions'][action_id].get('effect') == 'admission':
            pending = self.book.get('pending') or {}
            last_action = self.book.get('last_action') or {}
            if (pending.get('input_status') != 'reserved'
                    or pending.get('action_id') != action_id
                    or pending.get('source_page') != before.get('page')
                    or not pending.get('dispatch_id')
                    or pending['dispatch_id'] != last_action.get('dispatch_id')):
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
        sent = time.monotonic()
        if action_id == 'view_result':
            self._loh_view_result_sent = sent
        self._loh_fast_skip_submitted = action_id == 'skip_race'
        try:
            self.command('-s', self.endpoint, 'shell', 'input', kind, *values)
            if action_id=='watch_skip' and stop_event.wait(.3):
                raise RuntimeError('operator_stop_after_submit')
            from core.navigation_wait import wait_transition
            # Cinematic segments share one page and the same native glyph.
            # Return its fresh frame immediately so the runner can advance
            # the next segment, instead of waiting twenty seconds for a page.
            ready=(lambda value: 'watch_skip' in value.get('actions',{})) if action_id=='watch_skip' else None
            after = wait_transition(self.capture, stop_event, before,same_page_ready=ready)
        finally:
            self._loh_fast_skip_submitted = False
        receipt = dict(action_id=action_id, performed=True,
                       source_frame_sha256=before['frame_sha256'],
                       source_screenshot_path=before['screenshot_path'],
                       observation=after, input_sent_monotonic=sent)
        if action_id == 'skip_race' and getattr(self, '_loh_view_result_sent', None) is not None:
            receipt['since_view_result_input_seconds'] = sent - self._loh_view_result_sent
        (self.output_dir / (str(time.time_ns()) + '-receipt.json')).write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf8')
        return receipt


def run_loh(endpoint, adb_path, output, emit, stop, options=None, resume_from=None,
            device=None, observer=None):
    """The explicit daily-task entry; injected devices retain caller ownership."""
    from core.loh.policy import normalize_options
    from core.loh.runner import LohRunner
    options = normalize_options(options)
    if device is not None:
        if device.endpoint != endpoint:
            raise ValueError('loh_device_endpoint_mismatch')
        if observer is not None:
            device.observer = observer
        return LohRunner(device, emit, stop, options=options,
                         resume_from=resume_from).run()

    from core.screenshot_retention import ScreenshotSession
    # A resumed attempt keeps its real state/capture root. No intents are copied
    # into a fresh output directory or reset by constructing the live adapter.
    directory = Path(resume_from).resolve().parent if resume_from else Path(output)
    owned = None
    with ScreenshotSession() as screenshots:
        try:
            owned = LohDevice(endpoint, adb_path, directory, observer=observer)
            owned.connect()
            result = LohRunner(owned, emit, stop, options=options,
                               resume_from=resume_from).run()
            result['screenshot_retention'] = screenshots.finish(
                result, complete=bool(result.get('game_task_complete')))
            return result
        finally:
            if owned is not None:
                owned.close()
