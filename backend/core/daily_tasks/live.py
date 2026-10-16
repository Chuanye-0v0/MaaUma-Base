"""Fresh-frame daily actions with persisted consumption intent, never blind retries."""
from desktop.umaa_public_context import endpoint as _public_endpoint
import json
import time
import cv2
import numpy as np
from core.daily_tasks.observer import observe, observe_claim_fast, read_tokens, supplement_missing_fields
from core.legend_race.observer import selected_horse
from core.legend_selection.live import authorize_action as authorize_selection
from core.legend_selection.observer import same_list
from core.support_library.live import SupportDevice
from core.observation_timing import observation_freshness
from core.action_recovery import action_boundary, await_candidate, require_fresh_observation, ReobserveRequired, ObservationTimeout
from core.navigation_wait import same_controls, wait_transition
from core.passive_popup_runtime import without_passive_popup
PAGES = {'open_shop': 'home', 'open_sale_from_menu': 'shop_menu', 'scroll_shop_menu': 'shop_menu', 'open_missions': 'home', 'open_gifts': 'home', 'open_races': 'home', 'skip_mission_dialog': 'mission_dialog', 'claim_missions': 'missions', 'close_claim': 'claim_success', 'claim_gifts': 'gifts', 'close_gifts': 'gifts', 'open_schedule': 'races', 'open_daily_races': 'schedule', 'close_race_help': 'race_help', 'open_daily_details': 'daily_races', 'enable_multi': 'daily_details', 'open_moon_course': 'daily_races', 'scroll_courses': 'daily_races', 'scroll_lineup_top': 'race_grid', 'resume_race': 'race_resume', 'open_selection': 'daily_details', 'confirm_selected': 'selection', 'continue_lineup': 'lineup', 'enter_race': 'items', 'start_race': 'race_ready', 'select_portrait': 'orientation', 'confirm_portrait': 'orientation', 'run_race': 'race_grid', 'skip_race': 'animation', 'next_result': 'race_result', 'next_rewards': 'race_rewards', 'increase_count': 'multi_confirm', 'enter_multi': 'multi_confirm', 'close_multi_rewards': 'multi_rewards', 'open_limited_shop': 'limited_sale', 'cancel_limited_sale': 'limited_sale', 'select_all_shop': 'limited_shop', 'confirm_shop': 'limited_shop', 'exchange_shop': 'shop_confirm', 'close_shop_success': 'shop_success', 'close_details': 'daily_details', 'close_shop_unavailable': 'shop_unavailable'}
SELECTION_ACTIONS = {'open_sort', 'sort_evaluation', 'apply_sort', 'descending', 'scroll_top', 'scroll_next'}
SAFE_DISMISS_ACTIONS = {'close_multi_rewards', 'next_rewards', 'close_claim', 'close_gifts', 'close_details', 'close_shop_success', 'close_race_help', 'close_shop_unavailable'}
NEUTRAL_REPLAN_ACTIONS = {'return_home', 'cancel_limited_sale', 'close_details', 'close_gifts', 'close_race_help', 'close_shop_unavailable', 'open_races', 'open_schedule', 'open_daily_races'}

def authorize(action, current, expected, context):
    if current.get('endpoint') != expected.get('endpoint') or current.get('endpoint') != _public_endpoint():
        raise RuntimeError('daily_endpoint_not_bound')
    if not expected.get('frame_sha256') or not current.get('frame_sha256'):
        raise RuntimeError('daily_page_not_bound')
    if current.get('page') != expected.get('page'):
        if action in NEUTRAL_REPLAN_ACTIONS:
            raise ReobserveRequired(action, current)
        raise RuntimeError('daily_page_not_bound')
    if action in SELECTION_ACTIONS or action.startswith('horse_'):
        if context.get('course') != '月光奖' or (current.get('page') == 'selection' and (current.get('course') != '月光奖' or expected.get('course') != '月光奖')):
            raise RuntimeError('daily_course_not_bound')
        return authorize_selection(action, current, expected)
    candidate = current.get('actions', {}).get(action)
    page = current.get('page')
    if action == 'return_home':
        allowed = page in {'missions', 'limited_shop', 'shop_menu', 'arena', 'daily_races', 'selection', 'races', 'schedule'}
    elif action.startswith('tab_'):
        allowed = page == 'missions' and action[4:] in current.get('claimable_tabs', [])
    elif action.startswith('course_'):
        allowed = page == 'daily_races'
    else:
        allowed = PAGES.get(action) == page
    if not candidate or not allowed:
        raise RuntimeError('daily_action_not_allowed:' + action)

    def same(*keys):
        return all((current.get(key) is not None and current.get(key) == expected.get(key) for key in keys))
    if action == 'open_daily_details' or action.startswith('course_'):
        prior = expected.get('actions', {}).get(action, {})
        if not same('tickets') or type(current['tickets']) is not int or current['tickets'] <= 0 or (candidate.get('course') != '月光奖') or (candidate.get('difficulty') not in ('简单', '普通', '困难')) or any((candidate.get(k) != prior.get(k) for k in ('course', 'difficulty'))):
            raise RuntimeError('daily_course_not_bound')
    if action in {'enable_multi', 'open_selection', 'confirm_selected', 'continue_lineup', 'resume_race'}:
        if current.get('course') != context.get('course') or context.get('course') != '月光奖':
            raise RuntimeError('daily_course_not_bound')
        if current.get('difficulty') is not None and current['difficulty'] != context.get('difficulty'):
            raise RuntimeError('daily_difficulty_changed')
    if action == 'confirm_selected':
        horse = selected_horse(current)
        if not horse or horse != selected_horse(expected) or (not same_list(current, expected)):
            raise RuntimeError('daily_horse_not_bound')
    if action == 'enable_multi' and (current.get('multi_enabled') is not False or expected.get('multi_enabled') is not False or current.get('multi_available') is not True or (context.get('pending') is True)):
        raise RuntimeError('daily_multi_toggle_unverified')
    if action == 'resume_race':
        horse = context.get('horse') or {}
        if not same('resume_horse_name', 'resume_horse_score') or current.get('resume_horse_name') != horse.get('name') or current.get('resume_horse_score') != horse.get('score') or (context.get('race_cost') != 1) or (context.get('selection_confirmed') is not True):
            raise RuntimeError('daily_resume_horse_not_bound')
    if action == 'continue_lineup':
        if not context.get('horse') or context['horse']['name'] not in current.get('lineup_names', []):
            raise RuntimeError('daily_lineup_not_bound')
    if action in {'start_race', 'run_race', 'scroll_lineup_top'}:
        if not context.get('horse') or context.get('selection_confirmed') is not True or context.get('race_cost') != 1 or (type(context.get('tickets')) is not int) or (context['tickets'] <= 0) or (context.get('tickets_after') != context['tickets'] - 1):
            raise RuntimeError('daily_submitted_horse_not_bound')
    if action in {'enter_race', 'enter_multi'}:
        if context.get('pending') or not context.get('horse') or (not context.get('selection_confirmed')):
            raise RuntimeError('daily_submission_not_ready')
        if not same('tickets', 'tickets_after') or type(current['tickets']) is not int or current['tickets'] <= 0:
            raise RuntimeError('daily_ticket_cost_not_bound')
        if context.get('tickets') != current['tickets']:
            raise RuntimeError('daily_tickets_changed')
        if action == 'enter_race':
            if current.get('items_empty') is not True or expected.get('items_empty') is not True or current['tickets_after'] != current['tickets'] - 1 or (context.get('stage') != 'continue_lineup'):
                raise RuntimeError('daily_items_or_cost_unverified')
        elif not context.get('multi_enabled') or not same('count', 'maximum') or current['count'] != current['maximum'] or (current['count'] != current['tickets']) or (current['tickets_after'] != 0):
            raise RuntimeError('daily_multi_cost_not_bound')
    if action in {'start_race', 'select_portrait', 'confirm_portrait', 'run_race', 'skip_race', 'next_result', 'next_rewards', 'close_multi_rewards', 'scroll_lineup_top', 'resume_race'} and (not context.get('pending')):
        raise RuntimeError('daily_race_not_submitted')
    if action == 'confirm_portrait' and current.get('portrait_selected') is not True:
        raise RuntimeError('daily_portrait_not_selected')
    if action == 'open_limited_shop' and current.get('sale_remaining') != 0:
        raise RuntimeError('daily_sale_not_last')
    if action == 'confirm_shop' and (not same('coin_cost', 'coins') or not context.get('sale_authorized')):
        raise RuntimeError('daily_shop_cost_not_bound')
    if action == 'exchange_shop':
        purchase = context.get('shop_purchase') or {}
        if context.get('shop_submitted') or not context.get('sale_authorized') or (not same('coin_cost', 'coins_before', 'coins_after')) or (current.get('currency') != '金币') or (current['coin_cost'] != purchase.get('cost')) or (current['coins_before'] != purchase.get('before')) or (current['coins_after'] != purchase.get('before', 0) - purchase.get('cost', 0)):
            raise RuntimeError('daily_shop_purchase_not_bound')
    if action == 'claim_missions' and (not same('selected_tab') or current['selected_tab'] not in current.get('claimable_tabs', [])):
        raise RuntimeError('daily_claim_tab_not_bound')
    return candidate

class DailyDevice(SupportDevice):

    def __init__(self, endpoint, adb_path, output_dir):
        if endpoint != _public_endpoint():
            raise ValueError('daily_test_device_only')
        super().__init__(endpoint, adb_path, output_dir)
        self.context = {}

    def save_context(self):
        (self.output_dir / 'context.json').write_text(json.dumps(self.context, ensure_ascii=False, indent=2), encoding='utf8')

    def capture(self):
        for _ in range(3):
            raw = self.command('-s', self.endpoint, 'exec-out', 'screencap', '-p')
            captured = time.monotonic()
            image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR) if raw else None
            if image is not None:
                break
        if image is None:
            raise RuntimeError('screenshot_invalid')
        observed = observe_claim_fast(raw, image, self.ocr) if self.context.get('stage') in {'claim_missions', 'claim_gifts'} else None
        if observed is None:
            tokens = read_tokens(image, self.ocr)
            observed = observe(raw, tokens)
            supplemented = supplement_missing_fields(image, tokens, observed, self.ocr)
            if supplemented != tokens:
                observed = observe(raw, supplemented)
        path = self.output_dir / (str(time.time_ns()) + '.png')
        path.write_bytes(raw)
        observed.update(endpoint=self.endpoint, captured_monotonic=captured, screenshot_path=str(path))
        path.with_suffix('.json').write_text(json.dumps(observed, ensure_ascii=False, indent=2), encoding='utf8')
        return observed

    def _mission_claim_departure(self, before, expected, stop):
        selected = expected.get('selected_tab')
        if not expected.get('frame_sha256') or expected.get('page') != 'missions' or selected not in {'日常', '主要', '称号', '限定'} or (selected not in expected.get('claimable_tabs', [])):
            return None

        def cleared(state):
            tabs = state.get('claimable_tabs')
            return state.get('page') == 'missions' and state.get('endpoint') == expected.get('endpoint') == self.endpoint == _public_endpoint() and (state.get('selected_tab') == selected) and isinstance(tabs, list) and (selected not in tabs) and ('claim_missions' not in state.get('actions', {}))
        if not cleared(before):
            return None
        for _ in range(2):
            if stop.wait(0.25):
                raise RuntimeError('operator_stop')
            before = self.capture()
            if not cleared(before):
                return before
        return dict(action_id=None, performed=False, reason='mission_badge_cleared', observation=before)

    def _fresh_observation(self, observed, action, stop):
        """Acquire missing clocks, never fabricate one or repeat a submission."""
        for attempt in range(3):
            if observed.get('endpoint') != self.endpoint:
                raise RuntimeError('daily_endpoint_not_bound')
            status = observation_freshness(observed, action)
            if status == 'fresh' and observed.get('frame_sha256'):
                return observed
            if attempt == 2:
                if not observed.get('frame_sha256'):
                    raise RuntimeError('daily_frame_not_bound')
                if status in {'missing', 'invalid'}:
                    raise RuntimeError('daily_frame_timestamp_' + status)
                require_fresh_observation(observed, action)
            if stop.is_set():
                raise RuntimeError('operator_stop')
            observed = self.capture()
        return observed

    @action_boundary
    @without_passive_popup
    def step(self, action, stop, expected):
        if stop.is_set():
            raise RuntimeError('operator_stop')
        before = self.capture()
        if action in NEUTRAL_REPLAN_ACTIONS and before.get('page') in {'unknown', 'loading'} and (before.get('endpoint') == expected.get('endpoint') == self.endpoint) and expected.get('frame_sha256'):
            raise ReobserveRequired(action, before)
        deadline = time.monotonic() + 20
        while before.get('page') in {'unknown', 'loading'} and time.monotonic() < deadline:
            if stop.wait(0.5):
                raise RuntimeError('operator_stop')
            before = self.capture()
        if action == 'skip_race' and (before.get('page') != 'animation' or action not in before.get('actions', {})):
            return dict(action_id=None, performed=False, reason='skip_page_changed', observation=before)
        before = self._fresh_observation(before, action, stop)
        if action == 'open_gifts' and expected.get('page') == before.get('page') == 'home' and (before.get('endpoint') == expected.get('endpoint') == _public_endpoint()) and (before.get('gifts_badge') is False):
            for _ in range(2):
                if stop.wait(0.25):
                    raise RuntimeError('operator_stop')
                before = self.capture()
                if before.get('page') != 'home' or before.get('gifts_badge') is not False:
                    break
            if before.get('page') == 'home' and before.get('endpoint') == self.endpoint and (before.get('gifts_badge') is False):
                return dict(action_id=None, performed=False, reason='gift_badge_cleared', observation=before)
        if expected.get('page') == 'missions' and before.get('page') == 'mission_dialog' and (action == 'claim_missions' or action.startswith('tab_')) and (before.get('endpoint') == expected.get('endpoint') == _public_endpoint()) and ('skip_mission_dialog' in before.get('actions', {})):
            return dict(action_id=None, performed=False, reason='mission_dialog_appeared', observation=before)
        if action == 'return_home' and expected.get('page') in {'selection', 'daily_races'} and (before.get('page') == 'limited_sale') and (before.get('endpoint') == expected.get('endpoint') == _public_endpoint()) and ('cancel_limited_sale' in before.get('actions', {})):
            return dict(action_id=None, performed=False, reason='limited_sale_appeared', observation=before)
        if action == 'return_home' and expected.get('page') == 'selection' and (before.get('page') == 'daily_races') and (before.get('endpoint') == expected.get('endpoint') == _public_endpoint()) and (self.context.get('stage') == 'close_multi_rewards') and (self.context.get('result_seen') is True):
            expected = before
        if action == 'claim_missions':
            departure = self._mission_claim_departure(before, expected, stop)
            if departure is not None:
                if departure.get('reason') == 'mission_badge_cleared':
                    return departure
                before = departure
        if action not in before.get('actions', {}):
            try:
                before = await_candidate(self.capture, stop, action, before, expected.get('page'))
            except ObservationTimeout as error:
                departure = self._mission_claim_departure(error.observation, expected, stop) if action == 'claim_missions' else None
                if departure is None or departure.get('reason') != 'mission_badge_cleared':
                    raise
                return departure
        if action == 'exchange_shop' and expected.get('page') == before.get('page') == 'shop_confirm' and (expected.get('endpoint') == before.get('endpoint') == _public_endpoint()) and (expected.get('currency') == before.get('currency') == '金币') and any((expected.get(key) is None for key in ('coin_cost', 'coins_before', 'coins_after'))):
            purchase = self.context.get('shop_purchase') or {}
            fields = dict(coin_cost=purchase.get('cost'), coins_before=purchase.get('before'))
            if all((type(value) is int for value in fields.values())):
                fields['coins_after'] = fields['coins_before'] - fields['coin_cost']
                if all((before.get(key) == value for key, value in fields.items())) and all((expected.get(key) in (None, value) for key, value in fields.items())):
                    expected = before
        before = self._fresh_observation(before, action, stop)
        candidate = authorize(action, before, expected, self.context)
        require_fresh_observation(before, action)
        if stop.is_set():
            raise RuntimeError('operator_stop')
        if action == 'open_daily_details' or action.startswith('course_'):
            self.context.update(course=candidate['course'], difficulty=candidate['difficulty'], tickets=before['tickets'], selection_confirmed=False, horse=None)
        if action == 'open_selection':
            self.context['multi_enabled'] = before.get('multi_enabled') is True
        if action == 'confirm_selected':
            self.context.update(horse=selected_horse(before), selection_confirmed=True)
        if action in {'enter_race', 'enter_multi'}:
            self.context.update(pending=True, tickets_after=before['tickets_after'], race_cost=before['tickets'] - before['tickets_after'], result_seen=False)
        if action in {'next_rewards', 'close_multi_rewards'}:
            self.context['result_seen'] = True
        if action in {'open_limited_shop', 'open_sale_from_menu'}:
            self.context['sale_authorized'] = True
        if action == 'close_shop_unavailable':
            self.context['sale_authorized'] = False
        if action == 'confirm_shop':
            self.context['shop_purchase'] = dict(cost=before['coin_cost'], before=before['coins'])
        if action == 'exchange_shop':
            self.context['shop_submitted'] = True
        self.context['stage'] = action
        self.save_context()
        receipt = dict(action_id=action, endpoint=self.endpoint, source_frame_sha256=before['frame_sha256'], expected_frame_sha256=expected['frame_sha256'], source_page=before['page'], performed=None)
        path = self.output_dir / (str(time.time_ns()) + '-receipt.json')
        path.write_text(json.dumps(receipt, ensure_ascii=False), encoding='utf8')
        fast_navigation = action in {'return_home', 'close_details', 'close_gifts', 'close_claim', 'claim_gifts', 'claim_missions', 'next_rewards', 'close_multi_rewards', 'close_shop_success', 'close_race_help', 'close_shop_unavailable'}
        retryable = action in SAFE_DISMISS_ACTIONS and 'point' in candidate and ('swipe' not in candidate)
        for attempt in range(3 if retryable else 1):
            if attempt:
                fresh = self.capture()
                if fresh.get('page') != before.get('page') or not same_controls(before.get('actions'), fresh.get('actions')):
                    receipt['observation'] = fresh
                    break
                fresh = self._fresh_observation(fresh, action, stop)
                if fresh.get('page') != before.get('page') or not same_controls(before.get('actions'), fresh.get('actions')):
                    receipt['observation'] = fresh
                    break
                candidate = authorize(action, fresh, before, self.context)
                require_fresh_observation(fresh, action)
                before = fresh
            if stop.is_set():
                raise RuntimeError('operator_stop_after_submit' if attempt else 'operator_stop')
            if retryable:
                receipt.setdefault('dismiss_attempts', []).append(dict(source_frame_sha256=before['frame_sha256'], point=candidate['point'], acknowledged=False))
                path.write_text(json.dumps(receipt, ensure_ascii=False), encoding='utf8')
            if 'swipe' in candidate:
                self.command('-s', self.endpoint, 'shell', 'input', 'swipe', *map(str, candidate['swipe']))
            else:
                self.command('-s', self.endpoint, 'shell', 'input', 'tap', *map(str, candidate['point']))
            receipt['performed'] = True
            if retryable:
                receipt['dismiss_attempts'][-1]['acknowledged'] = True
            path.write_text(json.dumps(receipt, ensure_ascii=False), encoding='utf8')
            if fast_navigation:
                receipt['observation'] = wait_transition(self.capture, stop, before, timeout=3 if retryable else 20)
            else:
                if stop.wait(0.15 if action == 'skip_race' else 0.6):
                    raise RuntimeError('operator_stop_after_submit')
                receipt['observation'] = self.capture()
            after = receipt['observation']
            if not retryable or after.get('page') != before.get('page') or (not same_controls(before.get('actions'), after.get('actions'))):
                break
        path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf8')
        return receipt
