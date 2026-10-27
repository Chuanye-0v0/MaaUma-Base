"""Explicit live-task scope for passive interruptions at unknown observations.

Outside this scope capture remains read-only. Known reward pages retain their
owner and only receive a verified missing Close candidate. A dismissal on a
navigation/unknown page is an independently verified/journalled auxiliary
action, not a replay of the caller's task action or a completion receipt.
"""
from desktop.umaa_public_context import endpoint as _public_endpoint
from contextvars import ContextVar
from functools import wraps
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import time
import cv2
import numpy as np
from core.passive_popup import detect, detect_factor_research, detect_date_change
from core.passive_skip import detect as detect_skip
from core.passive_tutorial import detect as detect_tutorial, pager_identity
_active = ContextVar('uma_passive_popup', default=None)

@contextmanager
def owned_reward(device, page):
    """Keep an in-flight domain claim's generic reward under its owner's control."""
    previous = getattr(device, '_passive_reward_owner', None)
    device._passive_reward_owner = page
    try:
        yield
    finally:
        device._passive_reward_owner = previous

def without_passive_popup(function):
    """Read-only diagnostic captures must not advance an interrupted task."""

    @wraps(function)
    def call(*args, **kwargs):
        token = _active.set(None)
        try:
            return function(*args, **kwargs)
        finally:
            _active.reset(token)
    return call
OWNER_REWARD_CLOSE = {'champions_rewards': 'close_registration_rewards', 'limited_mission_rewards': 'close_mission_rewards', 'claim_success': 'close_claim', 'claim': 'close'}
PASSIVE_NAVIGATION_PAGES = {'unknown', 'home', 'races', 'events', 'other_event'}
OWNER_TUTORIAL_ADVANCE = {'st_notice': 'continue_notice', 'notice': 'close_notice', 'event_notice': 'dismiss_event_result', 'tutorial_help': 'dismiss_tutorial'}

class PassivePopupSession:

    def __init__(self, endpoint, stop):
        self.endpoint, self.stop = (endpoint, stop)
        self.receipts = []
        self.failure = None

    def __enter__(self):
        self.token = _active.set(self)
        return self

    def __exit__(self, *args):
        _active.reset(self.token)

    def check(self):
        if self.stop.is_set():
            raise RuntimeError('operator_stop')
        if self.failure:
            raise RuntimeError(self.failure)

    def annotate(self, result):
        if self.receipts:
            result['passive_popup_receipts'] = list(self.receipts)
        if self.failure:
            result.update(ok=False, game_task_complete=False, reason=self.failure, device_action_count_complete=False)

def install_passive_popup_recovery(device):
    session = _active.get()
    if session is None or session.endpoint != _public_endpoint() or device.endpoint != session.endpoint:
        return
    capture, command = (device.capture, device.command)
    latest = {}

    @wraps(command)
    def track_raw(*args, **kwargs):
        raw = command(*args, **kwargs)
        if args == ('-s', device.endpoint, 'exec-out', 'screencap', '-p'):
            latest.update(raw=raw, captured=time.monotonic())
        return raw

    def candidate(observed):
        if isinstance(observed, dict) and observed.get('page') == 'connection_conflict':
            session.failure = 'account_connection_conflict'
            raise RuntimeError(session.failure)
        if not isinstance(observed, dict):
            return None
        page = observed.get('page')
        owner_close = OWNER_REWARD_CLOSE.get(page)
        if page in {'connection_conflict', 'connection_interrupted', 'auth_blocked', 'blocked', 'password', 'purchase_confirm', 'skill_confirm'}:
            return None
        raw = latest.get('raw')
        if not raw or observed.get('endpoint') != session.endpoint or observed.get('frame_sha256') != hashlib.sha256(raw).hexdigest():
            return None
        image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.shape[:2] != (1280, 720):
            return None
        if page == 'autonomous_tutorial' and 'continue_autonomous_tutorial' in observed.get('actions', {}) or (page == 'skill_tutorial' and 'continue_skill_tutorial' in observed.get('actions', {})):
            return None
        research = detect_factor_research(image, observed.get('tokens', [])) or detect_date_change(image, observed.get('tokens', []))
        if research:
            research.update(source_frame_sha256=observed['frame_sha256'], captured_monotonic=latest['captured'], endpoint=session.endpoint)
            return research
        tutorial = detect_tutorial(image, observed.get('tokens', []))
        if tutorial:
            owner = observed.get('actions', {}).get(OWNER_TUTORIAL_ADVANCE.get(page), {})
            point = owner.get('point')
            if isinstance(point, (list, tuple)) and len(point) == 2 and all((isinstance(v, (int, float)) for v in point)) and (max((abs(a - b) for a, b in zip(point, tutorial['point']))) <= 12):
                return None
            tutorial.update(source_frame_sha256=observed['frame_sha256'], captured_monotonic=latest['captured'], endpoint=session.endpoint)
            return tutorial
        if pager_identity(image):
            observed.update(page='loading', actions={})
            return None
        if owner_close and owner_close in observed.get('actions', {}):
            return None
        if not owner_close and page not in PASSIVE_NAVIGATION_PAGES:
            return None
        if page != 'unknown' and (not owner_close) and (detect(image, observed.get('tokens', [])) is None):
            return None
        from core.account_login.observer import read_tokens
        tokens = read_tokens(image, device.ocr)
        from core.session_recovery import conflict_button
        if conflict_button(image, tokens):
            session.failure = 'account_connection_conflict'
            raise RuntimeError(session.failure)
        found = detect(image, tokens)
        requested_owner = getattr(device, '_passive_reward_owner', None)
        if page == 'unknown' and requested_owner in OWNER_REWARD_CLOSE and found and (found['kind'] == 'reward_received') and (found['button'] == '关闭'):
            observed.update(page=requested_owner, actions={OWNER_REWARD_CLOSE[requested_owner]: {'point': found['point']}}, shared_reward_close=found)
            return None
        if owner_close:
            if found and found['kind'] == 'reward_received' and (found['button'] == '关闭'):
                observed.setdefault('actions', {})[owner_close] = {'point': found['point']}
                observed['shared_reward_close'] = found
            return None
        if found is None and page == 'unknown':
            found = detect_skip(image, tokens)
            if found and found['button'] == 'champions_round_start' and (requested_owner is not None or observed.get('task_owner') is not None):
                return None
        if found:
            found.update(source_frame_sha256=observed['frame_sha256'], captured_monotonic=latest['captured'], endpoint=session.endpoint)
        return found

    def same(a, b):
        keys = ('kind', 'button') if a['kind'] == 'presentation_skip' else ('kind', 'button', 'content_key')
        return b is not None and all((a[k] == b[k] for k in keys)) and (max((abs(x - y) for x, y in zip(a['point'], b['point']))) <= 12)

    def save(path, receipt):
        path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf8')

    @wraps(capture)
    def recover(*args, **kwargs):
        if _active.get() is not session:
            return capture(*args, **kwargs)
        session.check()
        latest.clear()
        observed = capture(*args, **kwargs)
        found = candidate(observed)
        deadline = time.monotonic() + 30
        chain = 0
        tutorial_steps = 0
        last_tutorial = None
        while found:
            session.check()
            is_tutorial = found['kind'] == 'tutorial_pager'
            if (tutorial_steps >= 12 if is_tutorial else chain >= 4) or len(session.receipts) >= 32 or time.monotonic() >= deadline:
                session.failure = 'passive_popup_limit'
                raise RuntimeError(session.failure)
            if is_tutorial and last_tutorial is not None and (found['page_count'] != last_tutorial['page_count'] or found['page_index'] != last_tutorial['page_index'] + 1):
                session.failure = 'passive_tutorial_progress_unverified'
                raise RuntimeError(session.failure)
            latest.clear()
            observed = capture(*args, **kwargs)
            fresh = candidate(observed)
            if not same(found, fresh):
                return observed
            if not 0 <= time.monotonic() - fresh['captured_monotonic'] <= 15:
                raise RuntimeError('passive_popup_stale_frame')
            session.check()
            directory = Path(device.output_dir) / 'popup-actions'
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f'{time.time_ns()}.json'
            receipt = dict(fresh, ok=False, input_status='pending', tap_count=None)
            save(path, receipt)
            session.receipts.append(str(path))
            if session.stop.is_set():
                receipt.update(input_status='not_dispatched', tap_count=0, reason='operator_stop')
                save(path, receipt)
                raise RuntimeError('operator_stop')
            session.failure = 'passive_popup_transition_unverified'
            try:
                device.command('-s', session.endpoint, 'shell', 'input', 'tap', *map(str, fresh['point']))
            except Exception:
                session.failure = 'passive_popup_dispatch_uncertain'
                receipt.update(input_status='uncertain', reason='passive_popup_dispatch_uncertain')
                save(path, receipt)
                raise RuntimeError(session.failure) from None
            if is_tutorial:
                tutorial_steps += 1
                last_tutorial = fresh
            else:
                chain += 1
            receipt.update(input_status='dispatched', tap_count=1)
            save(path, receipt)
            for _ in range(6 if is_tutorial else 3):
                if session.stop.wait(0.4):
                    raise RuntimeError('operator_stop')
                observed = capture(*args, **kwargs)
                found = candidate(observed)
                if is_tutorial and found is None and (observed.get('page') in {'unknown', 'loading'}):
                    continue
                if observed.get('frame_sha256') != fresh['source_frame_sha256'] and (not same(fresh, found)):
                    break
            else:
                session.failure = 'passive_popup_transition_unverified'
                receipt['reason'] = session.failure
                save(path, receipt)
                raise RuntimeError(session.failure)
            receipt.update(ok=True, reason='passive_popup_transition_observed', after_frame_sha256=observed.get('frame_sha256'), after_page=observed.get('page'))
            save(path, receipt)
            session.failure = None
            if is_tutorial and found and (found.get('kind') == 'tutorial_pager'):
                deadline = time.monotonic() + 30
        return observed
    device.command, device.capture = (track_raw, recover)
