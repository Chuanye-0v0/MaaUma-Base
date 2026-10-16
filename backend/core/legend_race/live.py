"""Fresh observations and one-shot submissions for an authorized race."""
import json
import time
import cv2
import numpy as np
from core.legend_race.observer import observe, read_tokens, selected_horse
from core.legend_selection.live import ACTION_PAGES as SELECTION_ACTION_PAGES
from core.legend_selection.live import authorize_action as authorize_selection_action
from core.legend_selection.observer import same_list
from core.support_library.live import SupportDevice
from core.action_recovery import action_boundary, await_candidate, require_fresh_observation
RESULT_ACTION_PAGES = {'close_trophy': 'trophy', 'continue_victory': 'victory', 'continue_defeat': 'defeat', 'continue_rewards': 'rewards', 'return_home': 'legend'}
SELECTION_ACTIONS = set(SELECTION_ACTION_PAGES) | {'scroll_top', 'scroll_next'}

def authorize_action(action, current, expected, horse, event=None):
    if action in SELECTION_ACTIONS or action.startswith('horse_'):
        return authorize_selection_action(action, current, expected)
    if action in RESULT_ACTION_PAGES and action in current['actions']:
        if current['page'] != RESULT_ACTION_PAGES[action] or expected['page'] != current['page']:
            raise RuntimeError('result_page_not_bound')
        if action == 'return_home':
            return current['actions'][action]
        if not horse:
            raise RuntimeError('race_horse_not_bound')
        return current['actions'][action]
    if action in ('start_race', 'run_race', 'view_result') and action in current['actions']:
        page = 'race_grid' if action == 'run_race' else 'race_ready'
        if current['page'] != page or expected['page'] != page or (not horse):
            raise RuntimeError('race_horse_not_bound')
        for observed in (current, expected):
            if observed.get('race_horse') is not None and observed['race_horse'] != horse.get('name'):
                raise RuntimeError('race_horse_not_bound')
            course = observed.get('event', {}).get('course')
            if course is not None and event and (course != event.get('course')):
                raise RuntimeError('race_event_not_bound')
        return current['actions'][action]
    if action == 'enter_race' and action in current['actions']:
        if not horse or current['page'] != 'items' or expected['page'] != 'items' or (current.get('items_empty') is not True) or (expected.get('items_empty') is not True) or (type(current.get('tickets')) is not int) or (not 0 < current['tickets'] <= 3) or (current.get('tickets_after') != current['tickets'] - 1) or (current.get('tickets') != expected.get('tickets')) or (current.get('tickets_after') != expected.get('tickets_after')):
            raise RuntimeError('ticket_cost_not_bound')
        return current['actions'][action]
    if action == 'continue_lineup' and action in current['actions']:
        if not horse or current['page'] != 'lineup' or expected['page'] != 'lineup':
            raise RuntimeError('lineup_not_bound')
        return current['actions'][action]
    if action != 'confirm_selected' or action not in current['actions']:
        raise RuntimeError('race_action_not_allowed')
    if not horse or selected_horse(current) != horse or selected_horse(expected) != horse or (current.get('selected') != expected.get('selected')) or (not same_list(expected, current)):
        raise RuntimeError('selected_horse_not_bound')
    return current['actions'][action]

class LegendRaceDevice(SupportDevice):
    navigation_actions = SELECTION_ACTIONS
    additional_predecessors = {}
    candidate_replacements = {'start_race': ('view_result',), 'view_result': ('start_race',), 'resume_start_race': ('resume_view_result',), 'resume_view_result': ('resume_start_race',)}

    def __init__(self, endpoint, adb_path, output_dir, horse=None):
        super().__init__(endpoint, adb_path, output_dir)
        self.horse = horse
        self.event = None

    def read_observation(self, raw, image, navigation):
        if getattr(self, 'horse', None):
            from core.legend_race.items import fast_items
            fast = fast_items(raw, image, self.ocr)
            if fast:
                return fast
        return observe(raw, read_tokens(image, self.ocr), navigation=navigation, event=getattr(self, 'event', None))

    def authorize(self, action, current, expected):
        return authorize_action(action, current, expected, self.horse, event=getattr(self, 'event', None))

    def preflight(self, action, expected):
        """Historical proof only; fresh capture and domain authorization still follow."""

    def capture(self, *, navigation=True):
        raw = self.command('-s', self.endpoint, 'exec-out', 'screencap', '-p')
        captured = time.monotonic()
        image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError('screenshot_invalid')
        observed = self.read_observation(raw, image, navigation)
        path = self.output_dir / (str(time.time_ns()) + '.png')
        path.write_bytes(raw)
        observed.update(screenshot_path=str(path), captured_monotonic=captured, endpoint=self.endpoint)
        path.with_suffix('.json').write_text(json.dumps(observed, ensure_ascii=False, indent=2), encoding='utf-8')
        return observed

    @action_boundary
    def step(self, action_id, stop_event, expected):
        if stop_event.is_set():
            raise RuntimeError('operator_stop')
        previous = {'continue_lineup': ('confirm_selected',), 'enter_race': ('continue_lineup',), 'start_race': ('enter_race',), 'run_race': ('start_race',), 'view_result': ('enter_race',), 'continue_victory': ('view_result', 'run_race'), 'continue_defeat': ('view_result', 'run_race', 'start_race'), 'close_trophy': ('continue_victory', 'view_result', 'run_race'), 'continue_rewards': ('close_trophy', 'continue_victory', 'continue_defeat', 'view_result', 'run_race')}.get(action_id, ())
        previous += self.additional_predecessors.get(action_id, ())
        if previous:
            markers = [self.output_dir / (stage + '-submitted.json') for stage in previous]
            marker = next((path for path in markers if path.exists()), None)
            if marker is None and action_id == 'enter_race' and getattr(self, 'unsubmitted_entry_source', None):
                from core.daily_legend.resume import load_unsubmitted_entry
                original = load_unsubmitted_entry(self.unsubmitted_entry_source, self.endpoint)
                if original is None or original[0]['horse'] != self.horse or original[0]['event'] != self.event:
                    raise RuntimeError('previous_stage_not_bound')
                marker = original[1] / 'continue_lineup-submitted.json'
                while not marker.exists():
                    origin = json.loads((original[1].parent / 'entry-origin.json').read_text(encoding='utf8'))
                    original = load_unsubmitted_entry(origin['source'], self.endpoint)
                    if original is None or original[0]['horse'] != self.horse or original[0]['event'] != self.event:
                        raise RuntimeError('previous_stage_not_bound')
                    marker = original[1] / 'continue_lineup-submitted.json'
            if marker is None:
                raise RuntimeError('previous_stage_not_submitted')
            prior = json.loads(marker.read_text(encoding='utf-8'))
            if prior.get('action_id') not in previous or prior.get('horse') != self.horse or prior.get('event') != getattr(self, 'event', None):
                raise RuntimeError('previous_stage_not_bound')
        owner = json.dumps((getattr(self, 'horse', None), getattr(self, 'event', None)), sort_keys=True)
        self.preflight(action_id, expected)
        navigation = action_id in self.navigation_actions or action_id.startswith('horse_')
        before = self.capture(navigation=True)
        returning_daily = action_id == 'return_home' and expected.get('page') == 'daily_legend_complete'
        if action_id == 'dismiss_rank' and expected.get('page') == 'rank_overlay' or (action_id == 'run_race' and expected.get('page') == 'race_grid') or (action_id == 'open_details' and expected.get('page') == 'legend') or returning_daily or (action_id in ('continue_victory', 'continue_defeat', 'close_trophy', 'continue_rewards') and expected.get('page') == RESULT_ACTION_PAGES[action_id]):
            for _ in range(6):
                if before.get('page') not in ('unknown', 'loading') and (not (returning_daily and before.get('page') == 'events')):
                    break
                if stop_event.wait(0.3):
                    raise RuntimeError('operator_stop')
                before = self.capture(navigation=True)
        if action_id not in before.get('actions', {}):
            before = await_candidate(lambda: self.capture(navigation=True), stop_event, action_id, before, expected.get('page'), alternatives=self.candidate_replacements.get(action_id, ()))
        if json.dumps((getattr(self, 'horse', None), getattr(self, 'event', None)), sort_keys=True) != owner:
            raise RuntimeError('previous_stage_not_bound')
        candidate = self.authorize(action_id, before, expected)
        require_fresh_observation(before, action_id)
        if stop_event.is_set():
            raise RuntimeError('operator_stop')
        if not navigation:
            marker = self.output_dir / (action_id + '-submitted.json')
            try:
                with marker.open('x', encoding='utf-8') as f:
                    json.dump(dict(action_id=action_id, source_frame_sha256=before['frame_sha256'], horse=self.horse, state='pending', tickets=before.get('tickets'), result_outcome=before.get('result_outcome'), tickets_after=before.get('tickets_after'), event=getattr(self, 'event', None)), f, ensure_ascii=False, indent=2)
            except FileExistsError:
                raise RuntimeError('already_submitted') from None
        if 'swipe' in candidate:
            self.command('-s', self.endpoint, 'shell', 'input', 'swipe', *map(str, candidate['swipe']))
        else:
            self.command('-s', self.endpoint, 'shell', 'input', 'tap', *map(str, candidate['point']))
        from core.navigation_wait import wait_transition
        if action_id in {'continue_victory', 'continue_defeat', 'close_trophy', 'continue_rewards', 'continue_sale_rewards', 'dismiss_rank', 'close_legend_help', 'cancel_limited_sale'}:
            after = wait_transition(lambda: self.capture(navigation=True), stop_event, before)
        else:
            if stop_event.wait(0.8):
                raise RuntimeError('operator_stop_after_submit')
            after = self.capture(navigation=True)
        result = dict(action_id=action_id, source_frame_sha256=before['frame_sha256'], observation=after)
        (self.output_dir / (str(time.time_ns()) + '-receipt.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return result
