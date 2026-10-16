"""Fresh-frame JJC executor. Only observer action IDs are accepted."""
import json
import time
import cv2
import numpy as np
from core.jjc.observer import observe, observe_skip, observe_results_fast, observe_ready_fast, observe_summary_fast, observe_loading_fast, read_tokens
from core.support_library.live import SupportDevice
from core.observation_timing import observation_is_stale
from core.action_recovery import action_boundary, await_candidate, ReobserveRequired, require_fresh_observation
ACTION_PAGES = {'accelerate_weekly': 'weekly_settlement', 'continue_weekly': 'weekly_settlement', 'return_story_races': 'story_detail', 'accelerate_summary': 'summary', 'open_races': 'home', 'open_arena': 'races', 'open_opponents': 'arena', 'choose_top': 'opponents', 'continue_matchup': 'matchup', 'enter_race': 'items', 'choose_gift': 'opponents', 'enable_short': 'race_ready', 'view_results': 'race_ready', 'skip': 'animation', 'continue_results': 'results', 'close_story': 'story', 'next': 'summary', 'continue_rewards': 'rewards', 'cancel_items': 'items', 'cancel_limited_sale': 'limited_sale', 'rematch': 'summary', 'dismiss_high_score': 'high_score', 'resume_race': 'arena', 'resume_matchup': 'arena', 'open_pending_race': 'arena'}
FAST_NAVIGATION = {'accelerate_weekly', 'continue_weekly', 'continue_results', 'next', 'continue_rewards', 'cancel_items', 'cancel_limited_sale', 'dismiss_high_score', 'skip', 'view_results', 'rematch', 'choose_top', 'choose_gift', 'continue_matchup', 'accelerate_summary'}

def authorize_action(action, observed, context):
    if observed.get('arena_tallying'):
        raise RuntimeError('jjc_weekly_tally_in_progress')
    if action not in ACTION_PAGES or observed['page'] != ACTION_PAGES[action]:
        raise RuntimeError('jjc_action_forbidden')
    if action == 'open_pending_race' and (not (observed.get('race_in_progress') is True and type(observed.get('rp')) is int and (0 <= observed['rp'] <= 5) and observed.get('actions', {}).get('resume_race') and (observed.get('navigation_actions', {}).get(action) == observed['actions']['resume_race']))):
        raise RuntimeError('pending_race_navigation_unverified')
    if action == 'enter_race' and (not (observed.get('items_empty') is True and type(observed.get('rp')) is int and (observed['rp'] > 0) and (observed.get('rp_after') == observed['rp'] - 1))):
        raise RuntimeError('race_entry_not_bound_or_cost_invalid')
    if action == 'resume_race' and (not (observed.get('race_in_progress') is True and observed.get('actions', {}).get('resume_race'))):
        raise RuntimeError('race_resume_not_bound')
    if action == 'resume_matchup' and (not (observed.get('race_in_progress') is True and observed.get('navigation_actions', {}).get('resume_matchup'))):
        raise RuntimeError('matchup_resume_not_bound')

def transition_pending(action, before, after):
    if after.get('arena_tallying'):
        return False
    if after['page'] in ('unknown', 'loading'):
        return True
    if after['page'] != 'disconnected' and (not after['actions']):
        return not (after['page'] == 'arena' and after.get('rp') == 0)
    if action == 'enter_race' and after['page'] == 'matchup':
        return True
    if action in {'next', 'rematch'} and after['page'] == 'summary':
        return True
    return action not in ('skip', 'close_story', 'accelerate_summary') and after['page'] == before['page'] and (action in after['actions'] or action in after.get('navigation_actions', {}))

class JjcDevice(SupportDevice):

    def __init__(self, endpoint, adb_path, output_dir):
        self.context = {}
        super().__init__(endpoint, adb_path, output_dir)

    def capture(self):
        raw = self.command('-s', self.endpoint, 'exec-out', 'screencap', '-p')
        captured = time.monotonic()
        image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError('screenshot_invalid')
        observed = observe_loading_fast(raw, image, self.ocr)
        if observed is None and getattr(self, '_playback', False):
            observed = observe_skip(raw, image)
        if observed is None and getattr(self, '_playback', False):
            observed = observe_results_fast(raw, image, self.ocr)
        if observed is None and getattr(self, '_settlement', False):
            observed = observe_summary_fast(raw, image, self.ocr)
        if observed is None:
            observed = observe_ready_fast(raw, image, self.ocr)
        if observed is None:
            observed = observe(raw, read_tokens(image, self.ocr), settlement=getattr(self, '_settlement', False))
        self._loading = observed['page'] == 'loading'
        if getattr(self, '_last_frame_sha', None) != observed['frame_sha256']:
            self._last_frame_sha = observed['frame_sha256']
            self._unchanged_since = captured
        observed['unchanged_since_monotonic'] = self._unchanged_since
        stem = str(time.time_ns())
        path = self.output_dir / (stem + '.png')
        path.write_bytes(raw)
        observed.update(screenshot_path=str(path), captured_monotonic=captured, endpoint=self.endpoint)
        (self.output_dir / (stem + '.json')).write_text(json.dumps(observed, ensure_ascii=False, indent=2), encoding='utf-8')
        self._latest = observed
        return observed

    @action_boundary
    def step(self, action_id, stop_event):
        if stop_event.is_set():
            raise RuntimeError('operator_stop')
        before = getattr(self, '_latest', None)
        if action_id not in FAST_NAVIGATION or before is None or time.monotonic() - before['captured_monotonic'] > 2.5:
            before = self.capture()
        self._latest = None
        if before.get('arena_tallying'):
            raise ReobserveRequired(action_id, before)
        deadline = time.monotonic() + 20
        if action_id == 'accelerate_weekly' and (before['page'] == 'weekly_settlement' and 'continue_weekly' in before['actions'] or before['page'] == 'arena'):
            return dict(action_id=action_id, performed=False, observation=before)
        while action_id in {'next', 'rematch', 'continue_matchup', 'close_story'} and before['page'] in ('unknown', 'loading', ACTION_PAGES[action_id]) and (not before['actions']) and (time.monotonic() < deadline):
            if stop_event.wait(0.5):
                raise RuntimeError('operator_stop')
            before = self.capture()
        if action_id == 'close_story' and before['page'] in {'summary', 'story_detail'} and before['actions']:
            return dict(action_id=action_id, performed=False, observation=before)
        if action_id == 'continue_matchup' and before['page'] == 'items':
            return dict(action_id=action_id, performed=False, observation=before)
        while action_id == 'skip' and before['page'] in ('unknown', 'loading', 'animation') and (action_id not in before['actions']) and (time.monotonic() < deadline):
            if stop_event.wait(0.5):
                raise RuntimeError('operator_stop')
            before = self.capture()
        if action_id == 'skip' and before['page'] == 'results':
            while not before['actions'] and time.monotonic() < deadline:
                if stop_event.wait(0.5):
                    raise RuntimeError('operator_stop')
                before = self.capture()
            return dict(action_id=action_id, performed=False, observation=before)
        for _ in range(2):
            if action_id not in before['actions'] and action_id not in before.get('navigation_actions', {}):
                break
            if not observation_is_stale(before, action_id):
                break
            if stop_event.is_set():
                raise RuntimeError('operator_stop')
            before = self.capture()
        if action_id == 'accelerate_summary' and (before['page'] == 'summary' and 'next' in before['actions'] or (before['page'] == 'limited_sale' and set(before['actions']) == {'cancel_limited_sale'})):
            return dict(action_id=action_id, performed=False, observation=before)
        if action_id not in before['actions'] and action_id not in before.get('navigation_actions', {}):
            before = await_candidate(self.capture, stop_event, action_id, before, ACTION_PAGES.get(action_id), reason='action_not_allowed:' + action_id)
        candidate = before['actions'].get(action_id)
        if action_id in {'cancel_items', 'rematch', 'choose_gift', 'resume_matchup', 'open_pending_race'}:
            candidate = before.get('navigation_actions', {}).get(action_id)
        if candidate is None:
            raise RuntimeError('action_not_allowed:' + action_id)
        if time.monotonic() - self.context.get('bound_at', time.monotonic()) > 60:
            self.context = {}
        authorize_action(action_id, before, self.context)
        require_fresh_observation(before, action_id)
        if stop_event.is_set():
            raise RuntimeError('operator_stop')
        if action_id == 'continue_matchup':
            self.context = dict(entry_rp=before['rp'], bound_at=time.monotonic())
        if action_id == 'enter_race':
            self.context = {}
        x, y = candidate['point']
        self._playback = action_id in {'view_results', 'skip', 'resume_race'}
        self._settlement = action_id in {'continue_results', 'next', 'continue_rewards', 'accelerate_summary', 'cancel_limited_sale', 'close_story'}
        self.command('-s', self.endpoint, 'shell', 'input', 'tap', str(x), str(y))
        if stop_event.is_set() or (action_id not in FAST_NAVIGATION and stop_event.wait(1)):
            raise RuntimeError('operator_stop')
        after = self.capture()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if not transition_pending(action_id, before, after):
                break
            if stop_event.wait(0.3 if action_id in FAST_NAVIGATION else 0.5):
                raise RuntimeError('operator_stop')
            after = self.capture()
        result = dict(action_id=action_id, source_frame_sha256=before['frame_sha256'], observation=after)
        (self.output_dir / (str(time.time_ns()) + '-receipt.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return result
