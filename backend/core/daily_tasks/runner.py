"""Current-account daily races and reward collection; no ticket refills or items."""
import json
import re
import subprocess
import time
from pathlib import Path
from core.legend_race.observer import selected_horse
from core.legend_selection.runner import LegendSelectionRunner
from core.location_navigation import navigate_runner
from core.action_recovery import ReobserveRequired, ReobserveBudget, failure_details
from core.daily_tasks.live import PAGES, SAFE_DISMISS_ACTIONS, NEUTRAL_REPLAN_ACTIONS
from core.daily_tasks.resume import resolve_resume_source, account_protected
REWARD_ACTIONS = {'open_missions', 'skip_mission_dialog', 'claim_missions', 'close_claim', 'return_home', 'tab_日常', 'tab_主要', 'tab_称号', 'tab_限定', 'open_gifts', 'claim_gifts', 'close_gifts'}

def reward_outcome_acknowledged(outcome):
    return isinstance(outcome, dict) and outcome.get('action_id') in REWARD_ACTIONS and (outcome.get('decision') != 'pause_account') and (outcome.get('execution_status') in {'executed', 'not_executed'}) and (type(outcome.get('input_attempts')) is int) and (outcome['input_attempts'] >= 0) and (type(outcome.get('acknowledged_inputs')) is int) and (outcome['acknowledged_inputs'] == outcome['input_attempts']) and ((outcome['execution_status'] == 'executed') == (outcome['input_attempts'] > 0))

def acknowledged_reward_receipt(receipt, endpoint):
    """Legacy performed is written only after ACK; detailed uncertainty wins."""
    if not isinstance(receipt, dict) or receipt.get('action_id') not in REWARD_ACTIONS or receipt.get('performed') is not True or (receipt.get('endpoint') != endpoint) or (receipt.get('execution_status', 'executed') != 'executed') or (receipt.get('observation', {}).get('endpoint', endpoint) != endpoint):
        return False
    outcome = receipt.get('action_outcome')
    if outcome is not None and (not reward_outcome_acknowledged(outcome) or outcome['execution_status'] != 'executed' or outcome.get('action_id') != receipt['action_id'] or (outcome.get('source_frame_sha256') != receipt.get('source_frame_sha256'))):
        return False
    attempts = receipt.get('dismiss_attempts', [])
    return isinstance(attempts, list) and all((isinstance(attempt, dict) and attempt.get('acknowledged') is True for attempt in attempts))

def reward_recovery_acknowledged(source_chain, endpoint):
    """Do not hide an uncertain command behind an older performed receipt."""
    for folder in source_chain:
        path = Path(folder) / 'recovery/last-action.json'
        if not path.exists():
            continue
        outcome = json.loads(path.read_text(encoding='utf8'))
        if not reward_outcome_acknowledged(outcome):
            return False
        if outcome['input_attempts']:
            receipts = sorted(Path(folder).glob('*-receipt.json'))
            if not receipts:
                return False
            last = json.loads(receipts[-1].read_text(encoding='utf8'))
            if not acknowledged_reward_receipt(last, endpoint) or any((last.get(key) != outcome.get(key) for key in ('action_id', 'source_frame_sha256'))):
                return False
    return True

def reconcile_dismiss_receipts(receipts, endpoint):
    """Validation view only: a prior ineffective close may be retried on resume.

    Preserve every original receipt on disk, and never collapse consumption or
    closes whose first observation already left the dismissed page.
    """
    result = []
    for receipt in receipts:
        action = receipt.get('action_id')
        prior = result[-1] if result else {}
        if action in SAFE_DISMISS_ACTIONS and prior.get('action_id') == action and (prior.get('performed') is True) and (receipt.get('performed') is True) and (prior.get('endpoint') == receipt.get('endpoint') == endpoint) and (prior.get('observation', {}).get('page') == PAGES[action]) and (receipt.get('source_page') == PAGES[action]):
            result[-1] = receipt
        else:
            result.append(receipt)
    return result

def current_race_receipts(receipts):
    """Locate the latest submission only after every predecessor settled.

    This is a receipt-validation view. Keep the full chain on disk, and never
    infer a settlement from ticket totals or replace an unmatched submission.
    """
    start = None
    kind = None
    closed = False
    result_seen = result_shop = shop_submitted = False
    for index, receipt in enumerate(receipts):
        action = receipt.get('action_id')
        if action in {'enter_race', 'enter_multi'}:
            if kind is not None and (not closed):
                return None
            start, kind, closed = (index, action, False)
            result_seen = result_shop = shop_submitted = False
        elif action == 'next_rewards':
            if kind != 'enter_race':
                return None
            closed = True
        elif action == 'close_multi_rewards':
            if kind != 'enter_multi' or closed:
                return None
            closed = True
        elif action == 'next_result' and kind == 'enter_race':
            result_seen = True
        elif action == 'open_limited_shop' and kind == 'enter_race':
            result_shop = result_seen
        elif action == 'exchange_shop' and result_shop:
            shop_submitted = True
        elif action == 'close_shop_success' and result_shop and shop_submitted:
            closed = True
    return receipts[start:] if start is not None else []

class DailyTasksRunner:

    def __init__(self, device, emit, stop):
        self.device, self.emit, self.stop = (device, emit, stop)
        self.current = {}
        self.actions = self.completed = self.claims = 0
        self.initial = None
        self.difficulty = None
        self.previous_page = None

    def _capture(self):
        if self.stop.is_set():
            raise RuntimeError('operator_stop')
        self.current = self.device.capture()

    def _wait(self, pages, transient=(), timeout=30):
        deadline = time.monotonic() + timeout
        last_known = self.previous_page
        progressed = extended = False
        while self.current.get('page') not in pages:
            page = self.current.get('page')
            if page not in ('loading', 'unknown', self.previous_page) and page not in transient:
                raise RuntimeError('daily_unexpected_page:' + str(page))
            if page not in {'unknown', 'loading', None}:
                if last_known is not None and page != last_known:
                    progressed = True
                last_known = page
            if time.monotonic() >= deadline:
                if not extended and progressed and (timeout > 0):
                    deadline += min(10, timeout)
                    extended = True
                else:
                    raise RuntimeError('daily_transition_timeout')
            if self.stop.wait(0.5):
                raise RuntimeError('operator_stop')
            self._capture()

    def _act(self, action, pages=None):
        if self.stop.is_set():
            raise RuntimeError('operator_stop')
        if self.actions >= 250:
            raise RuntimeError('daily_action_limit')
        self.emit(dict(type='progress', phase='日活', message=action))
        self.previous_page = self.current.get('page')
        started = time.monotonic()
        replans = ReobserveBudget()

        def step():
            while True:
                if self.stop.is_set():
                    raise RuntimeError('operator_stop')
                try:
                    return self.device.step(action, self.stop, self.current)
                except ReobserveRequired as error:
                    outcome = getattr(error, 'action_outcome', {})
                    if outcome.get('execution_status') != 'not_executed':
                        raise
                    fresh = error.observation
                    if action in NEUTRAL_REPLAN_ACTIONS and fresh.get('page') in {'unknown', 'loading'} and pages:
                        self.current = replans.accept(error)
                        self._capture()
                        fresh = self.current
                        if fresh.get('page') in pages:
                            return None
                        if action == 'return_home' and set(pages) == {'home'}:
                            self._navigate_home()
                            return None
                        continue
                    if getattr(error, 'reason', None) == 'stale_frame' and fresh.get('page') == self.current.get('page') and (fresh.get('endpoint') == self.current.get('endpoint')) and (action in fresh.get('actions', {})):
                        self.current = replans.accept(error)
                        continue
                    if action not in NEUTRAL_REPLAN_ACTIONS or not pages or fresh.get('page') not in pages:
                        raise
                    self.current = fresh
                    return None
        receipt = step()
        if receipt is None:
            return
        for _ in range(2):
            if receipt.get('reason') != 'limited_sale_appeared':
                break
            self.current = receipt['observation']
            self._sale()
            if self.current['page'] == 'home':
                return
            if self.current['page'] == 'daily_details':
                self._act('close_details', {'daily_races'})
            self.previous_page = self.current['page']
            receipt = step()
            if receipt is None:
                return
        if receipt.get('reason') == 'limited_sale_appeared':
            raise RuntimeError('daily_limited_sale_dialog_limit')
        for _ in range(5):
            if receipt.get('reason') != 'mission_dialog_appeared':
                break
            self.current = receipt['observation']
            self._act('skip_mission_dialog', {'missions', 'mission_dialog'})
            if self.current['page'] == 'missions':
                self.previous_page = 'missions'
                receipt = step()
                if receipt is None:
                    return
            else:
                receipt = dict(reason='mission_dialog_appeared', observation=self.current)
        if receipt.get('reason') == 'mission_dialog_appeared':
            raise RuntimeError('daily_mission_dialog_limit')
        self.previous_page = receipt.get('source_page', self.previous_page)
        self.current = receipt['observation']
        if receipt.get('reason') in {'gift_badge_cleared', 'mission_badge_cleared'}:
            return receipt
        if receipt.get('reason') != 'skip_page_changed':
            attempts = receipt.get('action_outcome', {}).get('input_attempts')
            self.actions += attempts if type(attempts) is int and attempts > 0 else 1
        if pages:
            if action == 'return_home' and set(pages) == {'home'}:
                self._navigate_home()
                return receipt
            if action in {'open_shop', 'open_sale_from_menu', 'open_limited_shop'}:
                pages = set(pages) | {'shop_unavailable'}
            transient = {'open_selection': {'daily_races'}, 'confirm_portrait': {'race_ready'}, 'enter_race': {'lineup'}, 'enter_multi': {'selection'}, 'exchange_shop': {'limited_shop'}}
            timeout = max(0, 30 - (time.monotonic() - started)) if action in SAFE_DISMISS_ACTIONS else 30
            self._wait(pages, transient.get(action, ()), timeout=timeout)

    def _sale(self):
        if self.current['page'] == 'shop_unavailable':
            if self.device.context.get('shop_submitted') and (not self.device.context.get('shop_verified')):
                raise RuntimeError('daily_shop_result_unverified')
            self._act('close_shop_unavailable', {'shop_menu', 'home'})
            if self.current['page'] != 'home':
                self._act('return_home', {'home'})
            return
        if self.current['page'] == 'limited_sale':
            self._act('cancel_limited_sale', {'selection', 'daily_races', 'daily_details', 'home'})
            return
        elif self.current['page'] != 'limited_shop' or not self.device.context.get('sale_authorized'):
            return
        if not self.device.context.get('cleanup'):
            self._act('return_home', {'home'})
            return
        if self.current.get('shop_empty') is not True:
            self._act('select_all_shop', {'limited_shop'})
            if 'confirm_shop' not in self.current['actions']:
                if self.current.get('coin_cost') == 0 and self.current.get('shop_exchanged_rows', 0) > 0:
                    self.emit(dict(type='progress', message='限时特卖已兑换，无需重复购买'))
                    self._act('return_home', {'home'})
                    return
                raise RuntimeError('daily_shop_budget_or_selection_unverified')
            self._act('confirm_shop', {'shop_confirm'})
            self._act('exchange_shop', {'shop_success'})
            self._act('close_shop_success', {'limited_shop'})
            self._verify_shop_result()
        self._act('return_home', {'home'})

    def _verify_shop_result(self):
        purchase = self.device.context['shop_purchase']
        for _ in range(8):
            if self.current.get('page') == 'limited_shop' and self.current.get('coins') == purchase['before'] - purchase['cost'] and (self.current.get('shop_empty') is True or self.current.get('shop_exchanged_rows', 0) > 0):
                self.device.context['shop_verified'] = True
                self.device.save_context()
                return
            if self.stop.wait(0.5):
                raise RuntimeError('operator_stop')
            self._capture()
        raise RuntimeError('daily_shop_result_unverified')

    def _home(self):
        self._sale()
        self._navigate_home()

    def _navigate_home(self):
        routes = {page: 'return_home' for page in ('missions', 'limited_shop', 'shop_menu', 'arena', 'daily_races', 'selection', 'races', 'schedule')}
        routes.update(daily_details='close_details', gifts='close_gifts', limited_sale='cancel_limited_sale')
        navigate_runner(self, routes, {'home'}, lambda error: 'daily_transition_timeout' if error.kind in {'transition_unverified', 'deadline'} else 'daily_unexpected_page:' + str(error.observation.get('page')))

    def _race_list(self, *, include_pending=False):
        routes = dict(home='open_races', races='open_schedule', schedule='open_daily_races', race_help='close_race_help')
        targets = {'daily_races', 'race_resume'} if include_pending else {'daily_races'}
        if self.current['page'] not in {*routes, *targets, 'loading', 'unknown'}:
            self._home()
        navigate_runner(self, routes, targets, 'daily_races_navigation_unverified')
        if 'open_moon_course' in self.current['actions']:
            self._act('open_moon_course', {'daily_races'})

    def _select(self):
        action = 'open_daily_details' if self.difficulty is None else 'course_' + self.difficulty
        for _ in range(3):
            if action in self.current.get('actions', {}):
                break
            self._act('scroll_courses', {'daily_races'})
        self._act(action, {'daily_details'})
        if self.difficulty is None:
            self.difficulty = self.current.get('difficulty')
        if not self.difficulty or self.current.get('difficulty') != self.difficulty:
            raise RuntimeError('daily_difficulty_changed')
        self._enable_multi_if_available()
        self._act('open_selection', {'selection'})
        selector = LegendSelectionRunner(self.device, self.emit, self.stop)
        result = selector.run()
        self.actions += selector.actions
        self.current = selector.current
        if not result['ok'] or selected_horse(self.current) != result.get('selected'):
            raise RuntimeError(result.get('reason') or 'daily_selection_unverified')
        self._act('confirm_selected', {'multi_confirm', 'lineup'})

    def _enable_multi_if_available(self):
        if self.current.get('multi_enabled') is True:
            return
        if self.current.get('multi_available') is False and self.current.get('multi_enabled') is False:
            return
        attempts = 0
        for _ in range(9):
            if self.current.get('page') != 'daily_details':
                raise RuntimeError('daily_multi_page_changed')
            if self.current.get('multi_enabled') is True:
                return
            if self.current.get('multi_enabled') is False and attempts < 2 and (self.current.get('multi_available') is True) and ('enable_multi' in self.current.get('actions', {})) and (not self.device.context.get('pending')):
                self._act('enable_multi', {'daily_details'})
                attempts += 1
            if self.stop.wait(0.5):
                raise RuntimeError('operator_stop')
            self._capture()
        raise RuntimeError('daily_multi_state_unknown' if self.current.get('multi_enabled') is None else 'daily_multi_enable_exhausted')

    def _race(self):
        if self.current['page'] == 'multi_confirm':
            for _ in range(12):
                if self.current.get('count') == self.current.get('maximum') and self.current.get('count') is not None:
                    break
                self._act('increase_count', {'multi_confirm'})
            self._act('enter_multi', {'multi_rewards'})
            self._act('close_multi_rewards', {'selection', 'daily_races', 'daily_details', 'limited_sale', 'home'})
            return
        self._act('continue_lineup', {'items'})
        self._act('enter_race', {'race_ready'})
        self._act('start_race', {'orientation', 'race_grid', 'animation'})
        self._finish_playback()

    def _finish_playback(self):
        if self.current['page'] == 'race_resume':
            self._act('resume_race', {'race_ready', 'race_grid', 'animation', 'race_result'})
        if self.current['page'] == 'race_ready':
            self._act('start_race', {'orientation', 'race_grid', 'animation'})
        if self.current['page'] == 'limited_sale':
            self._cancel_result_sale()
        if self.current['page'] == 'orientation':
            if not self.current.get('portrait_selected'):
                self._act('select_portrait', {'orientation'})
            self._act('confirm_portrait', {'race_grid', 'animation'})
        if self.current['page'] == 'race_grid':
            for attempt in range(4):
                try:
                    self._act('run_race', {'animation', 'race_result'})
                    break
                except RuntimeError as error:
                    if str(error) != 'daily_lineup_not_bound' or attempt == 3:
                        raise
                    self._capture()
                    self._act('scroll_lineup_top', {'race_grid'})
        for _ in range(20):
            if self.current['page'] in {'race_result', 'race_rewards', 'limited_shop'}:
                break
            if 'skip_race' not in self.current.get('actions', {}):
                if self.stop.wait(0.5):
                    raise RuntimeError('operator_stop')
                self._capture()
                self._wait({'animation', 'race_result', 'race_rewards'})
                continue
            self._act('skip_race', {'animation', 'race_result'})
        else:
            raise RuntimeError('daily_race_result_timeout')
        if self.current['page'] == 'race_result':
            self._act('next_result', {'race_rewards', 'limited_sale'})
        if self.current['page'] == 'limited_sale':
            self._cancel_result_sale()
        if self.current['page'] == 'limited_shop':
            context = self.device.context
            if not (context.get('sale_authorized') is True and context.get('pending') is True and (context.get('stage') == 'open_limited_shop') and (context.get('race_cost') == 1)):
                raise RuntimeError('daily_result_shop_not_bound')
            context['result_seen'] = True
            self.device.save_context()
            return
        self._act('next_rewards', {'selection', 'daily_races', 'daily_details', 'limited_sale', 'limited_shop', 'home'})

    def _cancel_result_sale(self):
        self._act('cancel_limited_sale', {'race_rewards'})

    def cleanup(self):
        """Called once after the selected account tasks, never from race playback."""
        try:
            self.device.context['cleanup'] = True
            self._capture()
            self._wait({'home', 'limited_shop', 'shop_unavailable', 'shop_menu', 'arena', 'races'})
            if self.current['page'] in {'arena', 'races'}:
                self._home()
            if self.current['page'] == 'home':
                self._settle_home(require_reward_badges=False)
                if 'open_shop' in self.current['actions']:
                    self._act('open_shop', {'shop_menu', 'limited_shop'})
            if self.current['page'] == 'shop_menu':
                for _ in range(3):
                    if 'open_sale_from_menu' in self.current['actions']:
                        break
                    self._act('scroll_shop_menu', {'shop_menu'})
                if 'open_sale_from_menu' in self.current['actions']:
                    self._act('open_sale_from_menu', {'limited_shop'})
                else:
                    self._act('return_home', {'home'})
            if self.current['page'] in {'limited_shop', 'shop_unavailable'}:
                if self.current['page'] == 'limited_shop':
                    self.device.context['sale_authorized'] = True
                self._sale()
            if self.current['page'] != 'home':
                raise RuntimeError('account_cleanup_home_unverified')
            return dict(ok=True, reason='account_cleanup_home_verified', last_observation=self.current)
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
            return dict(ok=False, reason=str(error), last_observation=self.current, **failure_details(error), pending_exchange=bool(self.device.context.get('shop_submitted') and (not self.device.context.get('shop_verified'))))

    def _races(self):
        self._race_list()
        remaining = self.current.get('tickets')
        if type(remaining) is not int or remaining < 0:
            raise RuntimeError('daily_tickets_unverified')
        if self.initial is None:
            self.initial = remaining
        while remaining:
            self._select()
            self._race()
            remaining = self._verify_race_result(remaining)
        self._home()

    def _verify_race_result(self, remaining):
        self._sale()
        self._race_list()
        after = self.current.get('tickets')
        context = self.device.context
        if context.get('result_seen') is not True or type(after) is not int or after < 0:
            raise RuntimeError('daily_ticket_delta_unverified')
        self.completed += max(0, remaining - after)
        context.update(pending=False, tickets=after, selection_confirmed=False)
        self.device.save_context()
        return after

    def _rewards(self):
        self._home()
        self._settle_home()
        if 'open_missions' in self.current['actions']:
            self._act('open_missions', {'missions', 'mission_dialog'})
            for _ in range(5):
                if self.current['page'] != 'mission_dialog':
                    break
                self._act('skip_mission_dialog', {'missions', 'mission_dialog'})
            for _ in range(20):
                if self.current['page'] != 'missions':
                    raise RuntimeError('daily_missions_unverified')
                tabs = self.current.get('claimable_tabs')
                if not isinstance(tabs, list):
                    raise RuntimeError('daily_badges_unverified')
                if not tabs:
                    break
                selected = self.current.get('selected_tab')
                if selected not in tabs:
                    self._act('tab_' + tabs[0], {'missions'})
                    continue
                receipt = self._act('claim_missions', {'claim_success'})
                if receipt and receipt.get('reason') == 'mission_badge_cleared':
                    outcome = receipt.get('action_outcome', {})
                    if receipt.get('performed') is not False or outcome.get('execution_status') != 'not_executed' or outcome.get('input_attempts') != 0:
                        raise RuntimeError('daily_mission_handoff_unverified')
                    continue
                self.claims += 1
                self._act('close_claim', {'missions'})
            else:
                raise RuntimeError('daily_mission_claim_limit')
            self._home()
            self._settle_home()
        for _ in range(4):
            if 'open_gifts' not in self.current['actions']:
                break
            receipt = self._act('open_gifts', {'gifts'})
            if receipt and receipt.get('reason') == 'gift_badge_cleared':
                outcome = receipt.get('action_outcome', {})
                if receipt.get('performed') is not False or outcome.get('execution_status') != 'not_executed' or outcome.get('input_attempts') != 0:
                    raise RuntimeError('daily_gift_handoff_unverified')
                self._settle_home()
                continue
            for _ in range(20):
                if 'claim_gifts' not in self.current['actions']:
                    break
                self._act('claim_gifts', {'claim_success'})
                self.claims += 1
                self._act('close_claim', {'gifts'})
            else:
                raise RuntimeError('daily_gift_claim_limit')
            self._home()
            break
        else:
            raise RuntimeError('daily_gift_readiness_limit')
        self._settle_home()
        badges = (self.current.get('missions_badge'), self.current.get('gifts_badge'))
        if any((type(value) is not bool for value in badges)):
            raise RuntimeError('daily_reward_badges_unknown')
        if any(badges):
            raise RuntimeError('daily_rewards_remaining')

    def _settle_home(self, *, require_reward_badges=True):
        previous = None
        for _ in range(8):
            if self.stop.wait(0.5):
                raise RuntimeError('operator_stop')
            self._capture()
            if self.current.get('page') in {'unknown', 'loading'}:
                previous = None
                continue
            if self.current.get('page') != 'home':
                raise RuntimeError('daily_home_not_verified')
            badges = ((self.current.get('missions_badge'), self.current.get('gifts_badge'))
                      if require_reward_badges else (False, False))
            if all((type(value) is bool for value in badges)) and badges == previous:
                return
            previous = badges
        badges = (self.current.get('missions_badge'), self.current.get('gifts_badge'))
        raise RuntimeError('daily_reward_badges_unknown' if self.current.get('page') == 'home' and any((type(value) is not bool for value in badges)) else 'daily_home_badges_unstable')

    def run(self, *, races=True, rewards=True, resume_from=None, restart_verified=False, verify_resume_account=None):
        journal = Path(self.device.output_dir).parent / 'active.json'
        owns = complete = False
        recovered_home = False
        recovery_error = None
        failure_observation = None
        failure = {}
        pending_continuation = False
        pending_multi = False
        pending_shop = False
        reward_only = False
        reward_return = False
        reward_session_entry = False
        settled_continuation = False
        previous = None
        report = None
        context = {}
        try:
            if resume_from is None:
                with journal.open('x', encoding='utf8') as stream:
                    json.dump(dict(endpoint=self.device.endpoint, output=str(self.device.output_dir)), stream)
            else:
                previous = resolve_resume_source(resume_from, self.device.endpoint)
                saved = json.loads(journal.read_text(encoding='utf8'))
                report = json.loads((previous / 'result.json').read_text(encoding='utf8'))
                context = json.loads((previous / 'context.json').read_text(encoding='utf8'))
                source_chain = list(dict.fromkeys([str(previous), *saved.get('source_chain', []), *([saved['previous_output']] if saved.get('previous_output') else [])]))
                if any((Path(path).resolve().parent != journal.parent.resolve() for path in source_chain)):
                    raise RuntimeError('daily_resume_source_outside_run_root')
                receipts = []
                for path in reversed(source_chain):
                    if account_protected(json.loads((Path(path) / 'result.json').read_text(encoding='utf8')), json.loads((Path(path) / 'context.json').read_text(encoding='utf8'))):
                        raise RuntimeError('daily_resume_account_protected')
                    items = [(p, json.loads(p.read_text(encoding='utf8'))) for p in Path(path).glob('*-receipt.json')]
                    items.sort(key=lambda item: int(item[0].name.split('-')[0]) if item[0].name.split('-')[0].isdigit() else item[1].get('observation', {}).get('captured_monotonic', 0))
                    receipts.extend((receipt for _, receipt in items))
                navigation = {'open_races', 'open_schedule', 'open_daily_races', 'close_race_help', 'open_moon_course', 'open_daily_details', 'enable_multi', 'open_selection', 'return_home'}
                if report.get('reason') in {'candidate_list_changed', 'screenshot_invalid'} and context.get('selection_confirmed') is False and (context.get('horse') is None) and (context.get('course') == '月光奖') and (context.get('tickets') == report.get('initial_tickets')) and (report.get('completed_races') == 0):
                    navigation |= {'open_sort', 'sort_evaluation', 'apply_sort', 'descending', 'scroll_top', 'scroll_next'}
                    navigation |= {r['action_id'] for r in receipts if re.fullmatch('horse_\\d+', r.get('action_id', ''))}
                if any((not isinstance(receipt, dict) for receipt in receipts)):
                    raise RuntimeError('daily_navigation_resume_unverified')
                reward_receipts = receipts
                receipts = reconcile_dismiss_receipts(receipts, self.device.endpoint)
                race_receipts = current_race_receipts(receipts)
                latest_race = race_receipts[0].get('action_id') if race_receipts else None
                reward_ack = not races and all((acknowledged_reward_receipt(r, self.device.endpoint) for r in reward_receipts)) and reward_recovery_acknowledged(source_chain, self.device.endpoint) and (report.get('action_outcome') is None or reward_outcome_acknowledged(report['action_outcome']))
                reward_return = not races and context.get('stage') == 'return_home' and (report.get('reason') in {'game_stalled', 'daily_transition_timeout', 'operator_stop', 'operator_stop_after_submit'}) and (report.get('last_observation', {}).get('endpoint') == self.device.endpoint) and (report['last_observation'].get('page') in {'unknown', 'loading', 'home', 'missions', 'gifts'}) and (type(report.get('claims')) is int) and (report['claims'] > 0) and (report['claims'] == sum((r.get('action_id') in {'claim_missions', 'claim_gifts'} for r in reward_receipts))) and bool(receipts) and (receipts[-1].get('source_page') in {'missions', 'gifts'}) and (receipts[-1].get('observation', {}).get('endpoint') == self.device.endpoint) and bool(receipts[-1].get('source_frame_sha256'))
                reward_session_entry = not races and rewards and (context.get('stage') == 'open_gifts') and (report.get('reason') == 'daily_unexpected_page:connection_interrupted') and (report.get('last_observation', {}).get('page') == 'connection_interrupted') and (report['last_observation'].get('endpoint') == self.device.endpoint) and (type(report.get('claims')) is int) and (report['claims'] >= 0) and (report['claims'] == sum((r.get('action_id') in {'claim_missions', 'claim_gifts'} for r in reward_receipts))) and bool(receipts) and (receipts[-1].get('source_page') == 'home') and (receipts[-1].get('observation', {}).get('page') in {'loading', 'connection_interrupted'}) and (receipts[-1].get('observation', {}).get('endpoint') == self.device.endpoint) and bool(receipts[-1].get('source_frame_sha256'))
                reward_only = not races and (report.get('reason') in {'game_stalled', 'daily_transition_timeout'} or (report.get('reason') == 'daily_action_not_allowed:claim_missions' and context.get('stage') == 'close_claim') or reward_return or reward_session_entry) and (report.get('initial_tickets') is None) and (report.get('completed_races') == 0) and (report.get('pending_race') is False) and (report.get('pending_exchange') is False) and (context.get('stage') in {'claim_missions', 'claim_gifts', 'close_claim'} or reward_return or reward_session_entry) and (not context.get('pending')) and (not context.get('shop_submitted')) and bool(receipts) and (receipts[-1].get('action_id') == context['stage']) and reward_ack and (Path(saved['output']).resolve() == previous) and (saved['endpoint'] == self.device.endpoint)
                pending_rewards = context.get('stage') == 'next_rewards' and report.get('reason') in {'game_stalled', 'daily_transition_timeout'} and (report.get('last_observation', {}).get('page') == 'race_rewards')
                pending_continuation = report.get('pending_race') is True and context.get('pending') is True and (context.get('stage') in {'enter_race', 'confirm_portrait', 'run_race', 'skip_race', 'next_result', 'cancel_limited_sale', 'scroll_lineup_top', 'resume_race'} or pending_rewards or (context.get('stage') == 'open_limited_shop' and context.get('sale_authorized') is True and (report.get('reason') == 'daily_unexpected_page:limited_shop') and (report.get('last_observation', {}).get('page') == 'limited_shop') and (len(receipts) >= 2) and (receipts[-2].get('action_id') == 'next_result'))) and (context.get('race_cost') == 1) and (type(report.get('completed_races')) is int) and (report['completed_races'] >= 0) and (type(context.get('tickets')) is int) and (context['tickets'] > 0) and (context.get('tickets_after') == context['tickets'] - 1) and (context.get('course') == '月光奖') and bool(context.get('horse')) and (context.get('selection_confirmed') is True) and (context.get('result_seen') is pending_rewards) and (latest_race == 'enter_race') and ([r.get('action_id') for r in race_receipts if r.get('action_id') in {'enter_race', 'next_rewards', 'enter_multi', 'exchange_shop'}] in [['enter_race'] + ['next_rewards'] * n for n in ((1, 2) if pending_rewards else (0,))]) and bool(receipts) and (receipts[-1].get('action_id') == context.get('stage'))
                settled_continuation = report.get('pending_race') is False and context.get('pending') is False and (context.get('result_seen') is True) and (type(context.get('tickets')) is int) and (context['tickets'] >= 0) and (context.get('course') == '月光奖') and (context.get('selection_confirmed') is False) and (report.get('last_observation', {}).get('page') == 'daily_races') and (report['last_observation'].get('tickets') == context['tickets']) and (latest_race == 'enter_race') and any((r.get('action_id') == 'next_rewards' for r in race_receipts))
                settled_shop = context.get('shop_submitted') is True and context.get('shop_verified') is True and (report.get('pending_exchange') is False) and (sum((r.get('action_id') == 'exchange_shop' for r in receipts)) == 1) and (sum((r.get('action_id') == 'close_shop_success' for r in receipts)) == 1)
                settled_continuation = settled_continuation or (report.get('reason') == 'stale_frame' and report.get('pending_race') is False and (context.get('pending') is False) and (type(context.get('tickets')) is int) and (context['tickets'] >= 0) and (context.get('result_seen') is True) and (context.get('selection_confirmed') is False) and (context.get('course') == '月光奖') and (context.get('stage') == 'open_selection') and (report.get('last_observation', {}).get('page') == 'selection') and bool(receipts) and (receipts[-1].get('action_id') == 'open_selection') and (latest_race in {'enter_race', 'enter_multi'}) and ([r.get('action_id') for r in race_receipts if r.get('action_id') in {'enter_race', 'next_rewards', 'enter_multi', 'close_multi_rewards', 'exchange_shop'}] == (['enter_race', 'next_rewards'] if latest_race == 'enter_race' else ['enter_multi', 'close_multi_rewards'])))
                settled_continuation = settled_continuation or (report.get('pending_race') is False and context.get('pending') is False and (context.get('stage') in {'close_multi_rewards', 'open_missions', 'return_home'}) and (context.get('result_seen') is True) and (type(context.get('tickets')) is int) and (context['tickets'] >= 0) and (context.get('selection_confirmed') is False) and (context.get('course') == '月光奖') and (report.get('last_observation', {}).get('page') == 'daily_races' and report['last_observation'].get('tickets') == context['tickets'] or (context.get('stage') == 'open_missions' and report.get('last_observation', {}).get('page') == 'missions') or (context.get('stage') == 'return_home' and report.get('reason') in {'daily_home_not_verified', 'operator_stop', 'operator_stop_after_submit'} and (report.get('last_observation', {}).get('page') in {'home', 'unknown'}))) and (latest_race == 'enter_multi') and any((r.get('action_id') == 'close_multi_rewards' for r in race_receipts)) and bool(receipts) and (receipts[-1].get('action_id') == context.get('stage')) and all((r.get('action_id') in ({'return_home', 'open_missions', 'open_limited_shop', 'select_all_shop', 'confirm_shop', 'exchange_shop', 'close_shop_success', 'open_races', 'open_schedule', 'open_daily_races'} if settled_shop else {'return_home', 'open_missions'}) for r in race_receipts[next((i for i, r in enumerate(race_receipts) if r.get('action_id') == 'close_multi_rewards')) + 1:])))
                pending_shop = context.get('stage') in {'exchange_shop', 'close_shop_success'} and context.get('shop_submitted') is True and (context.get('shop_verified') is not True) and (context.get('sale_authorized') is True) and bool(context.get('shop_purchase')) and (report.get('pending_exchange') is True) and (sum((r.get('action_id') == 'exchange_shop' for r in receipts)) == 1) and bool(receipts) and (receipts[-1].get('action_id') == context.get('stage'))
                return_observation = report.get('last_observation', {})
                outcome = report.get('action_outcome') or {}
                zero_input_return = report.get('reason') == 'daily_page_not_bound' and outcome.get('execution_status') == 'not_executed' and (outcome.get('input_attempts') == 0) and (outcome.get('action_id') in {'return_home', 'cancel_limited_sale', 'close_details'}) and (return_observation.get('endpoint') == self.device.endpoint)
                pending_multi_return = context.get('stage') in {'return_home', 'cancel_limited_sale', 'close_details'} and (zero_input_return or report.get('reason') in {'daily_unexpected_page:daily_races', 'daily_unexpected_page:limited_sale', 'game_stalled', 'daily_transition_timeout', 'operator_stop', 'operator_stop_after_submit'}) and (return_observation.get('page') == 'daily_races' and return_observation.get('tickets') == 0 or (return_observation.get('page') == 'limited_sale' and return_observation.get('endpoint') == self.device.endpoint and ('cancel_limited_sale' in return_observation.get('actions', {}))) or (context.get('stage') in {'cancel_limited_sale', 'close_details'} and return_observation.get('page') in {'selection', 'daily_details', 'home'})) and (latest_race == 'enter_multi') and ([r.get('action_id') for r in race_receipts if r.get('action_id') in {'enter_race', 'next_rewards', 'enter_multi', 'close_multi_rewards', 'exchange_shop'}] == ['enter_multi', 'close_multi_rewards']) and all((r.get('action_id') in {'return_home', 'cancel_limited_sale', 'close_details'} for r in race_receipts[next((i for i, r in enumerate(race_receipts) if r.get('action_id') == 'close_multi_rewards')) + 1:]))
                pending_multi = report.get('pending_race') is True and context.get('pending') is True and (context.get('stage') in {'enter_multi', 'close_multi_rewards'} or pending_shop or pending_multi_return) and (context.get('multi_enabled') is True) and (context.get('selection_confirmed') is True) and bool(context.get('horse')) and (context.get('course') == '月光奖') and (context.get('result_seen') is (context.get('stage') != 'enter_multi')) and (type(context.get('tickets')) is int) and (context['tickets'] > 0) and (context.get('tickets_after') == 0) and (context.get('race_cost') == context['tickets']) and (latest_race == 'enter_multi') and bool(receipts) and (receipts[-1].get('action_id') == context.get('stage'))
                if not reward_only and (Path(saved['output']).resolve() != previous or saved['endpoint'] != self.device.endpoint or type(report.get('initial_tickets')) is not int or (report['initial_tickets'] < 0) or (type(report.get('completed_races')) is not int) or (report['completed_races'] < 0) or (race_receipts is None) or (not settled_continuation and (not pending_multi) and (not pending_continuation) and (report.get('completed_races') != 0)) or (report.get('claims') != 0) or (report.get('pending_exchange') and (not pending_shop)) or (not pending_continuation and (not pending_multi) and (report.get('pending_race') or context.get('pending'))) or (context.get('shop_submitted') and (not pending_shop) and (not settled_shop)) or (not receipts) or any((not pending_continuation and (not pending_multi) and (not settled_continuation) and (r.get('action_id') not in navigation) or r.get('performed') is not True or r.get('endpoint') != self.device.endpoint for r in receipts))):
                    raise RuntimeError('daily_navigation_resume_unverified')
                self.initial = report['initial_tickets']
                if pending_continuation or settled_continuation or pending_multi:
                    self.device.context = context
                    self.device.save_context()
                    self.difficulty = context['difficulty']
                    self.completed = report['completed_races']
                elif reward_only:
                    self.claims = report['claims']
                    self.device.context = dict(context)
                    self.device.save_context()
                    if reward_session_entry and restart_verified is not True:
                        if verify_resume_account is None or verify_resume_account() is not True:
                            raise RuntimeError('daily_reward_account_unverified')
            owns = True
            self._capture()
            if reward_only:
                if self.current.get('endpoint') != self.device.endpoint:
                    raise RuntimeError('daily_navigation_resume_unverified')
                if self.stop.is_set():
                    raise RuntimeError('operator_stop')
                if reward_session_entry and self.current.get('page') != 'home':
                    raise RuntimeError('daily_reward_session_home_unverified')
                if self.current.get('page') == 'home':
                    reward_only = False
            if restart_verified and self.current.get('page') == 'home':
                if pending_shop:
                    raise RuntimeError('restart_shop_exchange_requires_reconciliation')
                if reward_only:
                    reward_only = False
                elif pending_continuation or pending_multi:
                    self._race_list(include_pending=True)
                    expected = self.device.context['tickets_after']
                    if self.current.get('page') == 'race_resume':
                        self._finish_playback()
                        self._verify_race_result(self.device.context['tickets'])
                    elif self.current.get('page') != 'daily_races' or self.current.get('tickets') != expected:
                        raise RuntimeError('restart_daily_ticket_state_unverified')
                    else:
                        before = self.device.context['tickets']
                        if type(expected) is not int or not 0 <= expected < before:
                            raise RuntimeError('restart_daily_ticket_state_unverified')
                        self.completed += before - expected
                        self.device.context.update(pending=False, tickets=expected, selection_confirmed=False, restart_ticket_reconciliation=dict(before=before, after=expected, frame_sha256=self.current.get('frame_sha256'), outcome='unknown_after_restart'))
                        self.device.save_context()
                    pending_continuation = pending_multi = False
                    settled_continuation = True
            if reward_only:
                self._wait({'home', 'missions', 'gifts'} if reward_return else {'missions', 'claim_success'} if context.get('stage') == 'close_claim' else {'claim_success'})
            elif pending_multi:
                self.previous_page = 'selection'
                self._wait({'limited_shop', 'shop_success'} if pending_shop else {'multi_rewards'} if self.device.context['stage'] == 'enter_multi' else {'multi_rewards', 'selection', 'daily_races', 'daily_details', 'limited_sale', 'home'})
            elif pending_continuation:
                self.previous_page = 'race_ready'
                self._wait({'race_rewards', 'selection', 'daily_races', 'daily_details', 'limited_sale', 'limited_shop', 'home'} if pending_rewards else {'limited_shop'} if self.device.context['stage'] == 'open_limited_shop' else {'race_resume', 'race_ready', 'race_grid', 'animation', 'race_result', 'race_rewards', 'limited_sale'})
            else:
                self._wait({'home', 'daily_races', 'selection', 'daily_details', 'missions', 'mission_dialog'} if resume_from is not None else {'home', 'gifts', 'missions', 'arena', 'races'})
            if resume_from is not None:
                if self.current['page'] in {'selection', 'daily_details'} and any((self.current.get(key) != context.get(key) or context.get(key) is None for key in ('course', 'difficulty'))):
                    raise RuntimeError('daily_navigation_resume_course_changed')
                journal.write_text(json.dumps(dict(endpoint=self.device.endpoint, output=str(self.device.output_dir), previous_output=str(previous), source_chain=source_chain)), encoding='utf8')
            if reward_only:
                if self.current['page'] == 'claim_success':
                    self._act('close_claim', {'missions', 'gifts'})
                self._home()
            elif pending_multi:
                remaining = self.device.context['tickets']
                if pending_shop:
                    if self.current['page'] == 'shop_success':
                        self._act('close_shop_success', {'limited_shop'})
                    self._verify_shop_result()
                    self._act('return_home', {'home'})
                elif self.current['page'] == 'multi_rewards':
                    self._act('close_multi_rewards', {'selection', 'daily_races', 'daily_details', 'limited_sale', 'home'})
                self._verify_race_result(remaining)
            elif pending_continuation:
                if not pending_rewards or self.current['page'] == 'race_rewards':
                    self._finish_playback()
                self._verify_race_result(self.device.context['tickets'])
            else:
                if settled_continuation and self.current['page'] == 'mission_dialog':
                    for _ in range(12):
                        if self.current['page'] != 'mission_dialog':
                            break
                        self._act('skip_mission_dialog', {'mission_dialog', 'missions'})
                self._home()
            if races:
                self._races()
            if rewards:
                self._rewards()
            else:
                self._home()
                self._settle_home(require_reward_badges=False)
            complete = True
            reason = 'daily_races_and_rewards_home_verified' if races and rewards else 'daily_rewards_home_verified' if rewards else 'daily_races_home_verified'
        except FileExistsError:
            reason = 'daily_previous_run_unsettled'
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            reason = str(exc)
            failure = failure_details(exc)
            if owns and reason in {'daily_multi_state_unknown', 'daily_multi_enable_exhausted'} and (not self.stop.is_set()) and (self.current.get('page') == 'daily_details') and ('close_details' in self.current.get('actions', {})) and (failure.get('action_outcome', {}).get('execution_status') != 'uncertain') and (not self.device.context.get('pending')) and (not (self.device.context.get('shop_submitted') and (not self.device.context.get('shop_verified')))):
                failure_observation = self.current
                try:
                    self._act('close_details', {'daily_races', 'home'})
                    self._navigate_home()
                except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as recovery:
                    recovery_error = str(recovery)
            if owns and (not self.stop.is_set()) and (self.current.get('page') == 'selection') and (failure.get('action_outcome', {}).get('execution_status') != 'uncertain') and ('return_home' in self.current.get('actions', {})) and (not self.device.context.get('pending')) and (not (self.device.context.get('shop_submitted') and (not self.device.context.get('shop_verified')))):
                failure_observation = self.current
                try:
                    self._act('return_home', {'home'})
                    recovered_home = self.current.get('page') == 'home'
                except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as recovery:
                    recovery_error = str(recovery)
        result = dict(kind='daily_activities', ok=complete, reason=reason, completed_races=self.completed, **failure, initial_tickets=self.initial, claims=self.claims, items_used=0, refills=0, pending_race=self.device.context.get('pending', False), pending_exchange=bool(self.device.context.get('shop_submitted') and (not self.device.context.get('shop_verified'))), last_observation=self.current, device_actions=self.actions, recovered_home=recovered_home, recovery_error=recovery_error, failure_observation=failure_observation)
        boundary = result.get('action_outcome') or {}
        result['device_action_count_complete'] = bool(owns and self.actions == 0 and (not any(Path(self.device.output_dir).glob('*-receipt.json'))) and (not (Path(self.device.output_dir) / 'recovery/last-action.json').exists()) and (boundary.get('execution_status') not in {'executed', 'uncertain'}) and (boundary.get('input_attempts', 0) == 0))
        if resume_from is not None and (not owns) and (previous is not None) and (report is not None):
            result.update(daily_output_dir=str(previous), pending_race=bool(report.get('pending_race') or context.get('pending')), pending_exchange=bool(report.get('pending_exchange') or (context.get('shop_submitted') and (not context.get('shop_verified')))))
        (Path(self.device.output_dir) / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
        if owns and (complete or recovered_home):
            journal.unlink()
        return result
