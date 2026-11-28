"""Location-first routing, independent of recognition and input execution.

Only reviewed non-consuming edges belong here. A recognized page is not by
itself permission to act: the current observation must offer the route action,
and the device still performs its own fresh-frame/scope check before input.
"""
import time
import json
from pathlib import Path
from core.action_recovery import ReobserveRequired, ReobserveBudget
from core.passive_popup_runtime import without_passive_popup
HOME_ROUTES = {'arena_empty': 'back_home', 'arena_idle': 'back_home', 'arena_pending': 'back_home', 'races': 'back_home', 'arena_opponents': 'back_home', 'race_selection': 'back_home', 'daily_legend_list': 'back_home', 'daily_schedule': 'back_home', 'story_event': 'back_home', 'other_event': 'back_home', 'event_roulette': 'back_event', 'event_missions': 'back_event', 'training_menu': 'back_home', 'training_trainee': 'back_training_setup', 'training_exam_mode': 'back_training_setup', 'training_scenario': 'back_training_setup', 'uma_detail': 'close_detail', 'uma_list': 'back_home', 'profile': 'back_home', 'enhance_menu': 'back_home', 'uma_menu': 'back_home', 'menu': 'close_menu'}
SETTLEMENT_ROUTES = {'autonomous_history': 'close_training_history', 'weekly_settlement': 'continue_weekly', 'trained_detail': 'close_trained_detail', 'score_record': 'continue_score_record', 'scenario_record': 'continue_scenario_record', 'title_notice': 'confirm_title_notice', 'training_summary': 'next_training_summary', 'affinity_result': 'next_affinity_result', 'training_rewards': 'next_training_rewards', 'activity_reward_animation': 'advance_activity_reward', 'activity_points_result': 'next_activity_points', 'activity_points_claim': 'close_activity_points_claim', 'activity_story_unlock': 'close_activity_story_unlock', 'character_story_unlock': 'close_character_story_unlock', 'follow_trainer': 'cancel_follow_trainer', 'training_complete': 'close_training_complete'}
TASK_OWNERS = {page: 'harvest' for page in ('finish_ready', 'finish_confirm', 'factor_result', 'factor_confirm', 'training_rating', 'skills', 'skill_confirmation', 'skill_success', 'team_formation_save_confirm')}
TASK_OWNERS.update({f'jjc_{page}': 'jjc' for page in ('race_ready', 'results', 'summary', 'rewards')})
MODAL_ROUTES = {**SETTLEMENT_ROUTES, 'notice': 'close_notice', 'story_unlock': 'close_story', 'profile_help': 'continue_profile_help', 'profile_support_help': 'close_profile_help', 'daily_legend_help': 'close_legend_help', 'event_bingo': 'dismiss_event_result', 'event_notice': 'dismiss_event_result', 'event_story_playing': 'open_event_story_menu', 'event_story_menu': 'skip_event_story', 'event_story_skip_confirm': 'confirm_event_story_skip'}

def inventory_routes(destination):
    routes = dict(HOME_ROUTES, **MODAL_ROUTES)
    if destination == 'recent':
        routes.update(home='open_menu', menu='return_title', title_confirm='confirm_title', title='enter_game', autonomous_running='open_training_menu', training_complete='close_training_complete', date_change='confirm_date_change')
    elif destination == 'profile':
        routes.update(home='open_menu', menu='open_profile')
        routes.pop('profile', None)
    elif destination != 'home':
        raise ValueError('navigation_destination_invalid')
    return routes

def navigation_handoff(observed):
    """Yield only a recognized ordinary or post-completion navigation page."""
    page = observed.get('page')
    routes = {**HOME_ROUTES, **SETTLEMENT_ROUTES}
    return page in routes and (routes[page] in observed.get('actions', {}) or page == 'training_menu')

def task_owner_handoff(observed):
    """A known task location is not a generic navigation/input capability."""
    owner = TASK_OWNERS.get(observed.get('page'))
    return owner is not None and observed.get('task_owner') == owner

class NavigationFailure(RuntimeError):

    def __init__(self, kind, observation, trace):
        super().__init__(kind)
        self.kind, self.observation, self.trace = (kind, observation, list(trace))

@without_passive_popup
def wait_for_transition(device, stop, observed, ready, *, waiting_pages, timeout=15, endpoint=None, check=None):
    """Read a delayed postcondition without dispatching or repeating input.

    The caller retains its real receipt and transaction state. A successful
    wait is only an observation, not proof that a task or purchase completed.
    Old-page animation and changing pixels never extend the semantic deadline.
    """
    current = observed
    waiting_pages = set(waiting_pages)
    expected_endpoint = endpoint if endpoint is not None else observed.get('endpoint')
    deadline = time.monotonic() + timeout
    trace, reads, final_read = ([], 0, False)

    def note(decision):
        item = dict(page=current.get('page'), decision=decision, reads=reads)
        if not trace or trace[-1]['page'] != item['page'] or trace[-1]['decision'] != decision:
            trace.append(item)

    def fail(kind):
        note(kind)
        raise NavigationFailure(kind, current, trace)
    while True:
        if stop.is_set():
            fail('operator_stop')
        if check is not None:
            check()
        if expected_endpoint is not None and current.get('endpoint') != expected_endpoint:
            fail('scope_unverified')
        if current.get('page') in {'auth_blocked', 'disconnected', 'connection_interrupted', 'connection_conflict'}:
            fail('blocked')
        if ready(current):
            note('handoff')
            return current
        if current.get('page') not in waiting_pages:
            fail('task_owner_required' if task_owner_handoff(current) else 'page_changed')
        note('wait')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if final_read:
                fail('transition_unverified')
            final_read = True
        elif stop.wait(min(0.3, remaining)):
            fail('operator_stop')
        current = device.capture()
        reads += 1

def navigate_location(device, stop, routes, terminal, *, observed=None, receipt_sink=None, trace_sink=None, wait_seconds=15, max_steps=24, total_seconds=120, repeat_pages=(), readiness_pages=('training_menu',), action_limits=None):
    """Replan after every verified location change; never replay uncertain input.

    Time limits use semantic location/action readiness, not screenshot hashes.
    Background animation cannot extend a wait. Route cycles are also bounded
    by the overall deadline and input limit. ``terminal`` can hand settlement
    or authentication back to its owner, but cannot synthesize task success.
    """
    trace, steps, counts = ([], 0, {})
    limits = {'enter_game': 3} if action_limits is None else action_limits
    started = time.monotonic()
    waiting_since = started
    last_source = None
    last_action_at = None
    gate_rechecks = 0
    replans = ReobserveBudget()
    last_trace = None

    def fail(kind):
        raise NavigationFailure(kind, current, trace)

    def note(decision):
        nonlocal last_trace
        entry = dict(page=current.get('page'), decision=decision, action=routes.get(current.get('page')), steps=steps)
        if entry != last_trace:
            trace.append(entry)
            last_trace = entry
            if trace_sink:
                trace_sink(list(trace))
    if stop.is_set():
        raise NavigationFailure('operator_stop', {}, [])
    current = device.capture() if observed is None else observed
    while True:
        if stop.is_set():
            fail('operator_stop')
        now = time.monotonic()
        page = current.get('page')
        if terminal(current):
            note('handoff')
            return current
        if now - started >= total_seconds:
            fail('deadline')
        if page in {'auth_blocked', 'disconnected', 'connection_interrupted', 'connection_conflict'} and page not in routes:
            fail('blocked')
        if page not in routes and task_owner_handoff(current):
            note('task_owner_required')
            fail('task_owner_required')
        action = routes.get(page)
        offered = action is not None and action in current.get('actions', {})
        same_source = page == last_source and last_action_at is not None
        title_retry = action == 'enter_game' and same_source and (now - last_action_at >= 2)
        waiting = page in {'unknown', 'loading'} or (same_source and page not in repeat_pages and (not title_retry)) or (page in readiness_pages and (not offered))
        if waiting:
            note('wait')
            if now - waiting_since >= wait_seconds:
                final = device.capture()
                if stop.is_set():
                    fail('operator_stop')
                final_page = final.get('page')
                final_action = routes.get(final_page)
                may_dispatch = final_action is not None and final_action in final.get('actions', {}) and (final_page != last_source or final_page in repeat_pages or (final_action == 'enter_game' and last_action_at is not None and (time.monotonic() - last_action_at >= 2)))
                if final.get('endpoint') != current.get('endpoint'):
                    fail('scope_unverified')
                current = final
                if terminal(final) or may_dispatch or task_owner_handoff(final):
                    continue
                if final_page in {'auth_blocked', 'disconnected', 'connection_interrupted', 'connection_conflict'} and final_page not in routes:
                    fail('blocked')
                fail('transition_unverified' if last_action_at is not None else 'page_unverified')
            if stop.wait(0.3):
                fail('operator_stop')
            current = device.capture()
            continue
        if not offered:
            note('unowned')
            fail('page_unverified')
        if steps >= max_steps:
            fail('step_limit')
        if counts.get(action, 0) >= limits.get(action, max_steps):
            fail('transition_unverified')
        note('dispatch')
        try:
            receipt = device.step(action, stop)
        except ReobserveRequired as error:
            outcome = getattr(error, 'action_outcome', {})
            fresh = error.observation
            if outcome.get('execution_status') != 'not_executed' or outcome.get('input_attempts') != 0 or outcome.get('acknowledged_inputs') != 0:
                raise
            if fresh.get('endpoint') != current.get('endpoint'):
                fail('scope_unverified')
            if fresh.get('page') == page:
                current = replans.accept(error)
                note('reobserve_zero_input')
                if stop.wait(0.3):
                    fail('operator_stop')
                current = device.capture()
                if current.get('endpoint') != fresh.get('endpoint'):
                    fail('scope_unverified')
                continue
            receipt = dict(ok=False, tap_count=0, reason='action_page_mismatch', execution_status='not_executed', observation=fresh)
        if receipt_sink:
            receipt_sink(receipt)
        fresh = receipt.get('observation')
        if stop.is_set() or receipt.get('reason') in {'operator_stop', 'operator_stop_after_tap'}:
            if isinstance(fresh, dict):
                current = fresh
            fail('operator_stop')
        if receipt.get('ok') is not True:
            zero_input = receipt.get('ok') is False and receipt.get('execution_status', 'not_executed') == 'not_executed' and (type(receipt.get('tap_count')) is int) and (receipt['tap_count'] == 0) and (receipt.get('reason') in {'action_not_allowed', 'action_page_mismatch'})
            fresh_action = routes.get(fresh.get('page')) if isinstance(fresh, dict) else None
            ready = isinstance(fresh, dict) and (terminal(fresh) or (fresh_action is not None and fresh_action in fresh.get('actions', {}) and (fresh.get('page') not in repeat_pages or set(fresh.get('actions', {})) == {fresh_action})))
            transient = isinstance(fresh, dict) and fresh.get('page') in {'unknown', 'loading'} and (not fresh.get('actions'))
            if zero_input and (ready or transient) and (fresh.get('page') != page) and (gate_rechecks < 3):
                current = fresh
                gate_rechecks += 1
                last_source = None
                if transient:
                    note('reobserve_zero_input')
                continue
            if isinstance(fresh, dict):
                current = fresh
                if zero_input and task_owner_handoff(fresh):
                    note('task_owner_required')
                    fail('task_owner_required')
            fail('action_unverified')
        if not isinstance(fresh, dict):
            fail('action_unverified')
        steps += 1
        replans.executed()
        counts[action] = counts.get(action, 0) + 1
        if action in {'return_title', 'confirm_title'}:
            counts.pop('enter_game', None)
        source_page = receipt.get('source_page')
        last_source = source_page if isinstance(source_page, str) else page
        last_action_at = time.monotonic()
        waiting_since = last_action_at
        current = fresh

def navigate_runner(runner, routes, destinations, reason):
    """Use a task runner's own guarded actions and journal, not inventory input.

    Call only from its navigation phase. Never use this to retry a consuming
    action or to escape an in-flight settlement. Domain exceptions propagate.
    """

    class Adapter:

        @staticmethod
        def capture():
            runner._capture()
            return runner.current

        @staticmethod
        def step(action, stop):
            try:
                runner._act(action)
            except ReobserveRequired as error:
                if getattr(error, 'action_outcome', {}).get('execution_status') != 'not_executed':
                    raise
                if getattr(error, 'reason', None) == 'stale_frame':
                    raise
                return dict(ok=False, tap_count=0, reason='action_page_mismatch', execution_status='not_executed', observation=error.observation)
            return dict(ok=True, observation=runner.current, source_page=getattr(runner, 'previous_page', None))

    def trace_sink(trace):
        runner.navigation_trace = trace
        directory = getattr(runner.device, 'output_dir', None)
        if isinstance(directory, Path):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / 'navigation.json').write_text(json.dumps(dict(destinations=sorted(destinations), trace=trace), ensure_ascii=False, indent=2), encoding='utf8')
    try:
        runner.current = navigate_location(Adapter(), runner.stop, routes, lambda state: state.get('page') in destinations, observed=runner.current, wait_seconds=30, max_steps=24, readiness_pages=set(routes), trace_sink=trace_sink)
    except NavigationFailure as error:
        runner.current = error.observation
        message = reason(error) if callable(reason) else reason
        failure = RuntimeError('operator_stop' if error.kind == 'operator_stop' else message)
        failure.navigation_trace = error.trace
        raise failure from error
    return runner.current
