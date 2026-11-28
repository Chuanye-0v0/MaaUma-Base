"""Current-page dispatch for an already submitted training, never a new start."""
from desktop.umaa_public_context import endpoint as _public_endpoint
MATURE_ENTRY_PAGES = {'history', 'history_summary', 'history_races', 'finish_ready'}
OWNED_HARVEST_PAGES = MATURE_ENTRY_PAGES | {'skills', 'skill_tutorial', 'skill_confirmation', 'skill_success', 'finish_confirm', 'training_rating', 'factor_result', 'factor_reroll_confirm', 'factor_confirm', 'trained_detail', 'score_record', 'scenario_record', 'title_notice', 'settlement_navigation', 'training_summary', 'affinity_result', 'training_rewards', 'activity_story_unlock', 'activity_reward_animation', 'activity_points_result', 'activity_points_claim', 'follow_trainer', 'training_complete', 'character_story_unlock', 'team_formation', 'team_formation_confirm', 'team_formation_save_confirm', 'team_rank_reward', 'team_rank_update', 'team_unlock'}
PROTECTED_PAGES = {'auth_blocked', 'connection_conflict', 'connection_interrupted', 'recent', 'records', 'password', 'terms', 'startup_terms'}

def continuation_phase(observed, context=None):
    """Classify a location, not permission to finish, spend or edit a team."""
    context = context or {}
    page = observed.get('page')
    if page in PROTECTED_PAGES:
        return 'protected'
    if page in {'unknown', 'loading'}:
        return 'wait'
    if page == 'home':
        return 'home'
    if page in OWNED_HARVEST_PAGES:
        return 'harvest'
    if page == 'team_arena' and context.get('formation_requested') is True:
        return 'harvest'
    if page == 'training_status':
        seconds = observed.get('remaining_seconds')
        if type(seconds) is int and seconds == 0:
            return 'harvest'
        if type(seconds) is int and seconds > 0:
            return 'navigation'
        return 'wait'
    if page == 'autonomous_running':
        if observed.get('autonomous_active') is True and observed.get('remaining_seconds') == 0:
            return 'wait'
        return 'navigation' if observed.get('autonomous_active') is True else 'unowned'
    if page == 'training_menu':
        return 'navigation'
    return 'unowned'

def continue_training_navigation(device, observed, emit, stop, *, probe_home=False):
    """Only leave an existing training, yielding immediately if it has matured.

    No setup/start, finish or purchase action is available to this dispatcher.
    Fresh executor refusals are handed back as observations, not retried taps.
    """
    import time
    from core.action_recovery import ReobserveRequired, ReobserveBudget
    from core.location_navigation import wait_for_transition
    routes = {'home': 'open_training', 'autonomous_running': 'open_training_menu', 'training_menu': 'return_home', 'training_status': 'cancel_training_status'}
    endpoint = device.endpoint
    records, counts = ([], {})
    budget = ReobserveBudget()
    started = time.monotonic()
    saw_training = False
    for _ in range(24):
        if stop.is_set():
            raise RuntimeError('operator_stop')
        if observed.get('endpoint') != endpoint or endpoint != _public_endpoint():
            raise RuntimeError('plant_recovery_scope_unverified')
        phase = continuation_phase(observed, device.context)
        if phase == 'harvest' or (phase == 'home' and (not probe_home or saw_training)):
            return (observed, records)
        if phase == 'wait':
            observed = wait_for_transition(device, stop, observed, lambda state: continuation_phase(state, device.context) != 'wait', waiting_pages={'unknown', 'loading', 'training_status', 'autonomous_running'}, endpoint=endpoint)
            continue
        if phase not in {'navigation', 'home'} or time.monotonic() - started > 60:
            raise RuntimeError('plant_recovery_page_unverified')
        saw_training = saw_training or phase == 'navigation'
        source_page = observed['page']
        action = routes[source_page]
        if counts.get(action, 0) >= 2:
            raise RuntimeError('plant_recovery_navigation_repeat_limit')
        emit(dict(type='progress', message='原育成收尾：' + action))
        try:
            receipt = device.step(action, stop)
        except ReobserveRequired as error:
            observed = budget.accept(error)
            continue
        observed = receipt.get('observation') or {}
        if receipt.get('performed') is not True:
            if receipt.get('status') == 'navigation_arrived':
                continue
            raise RuntimeError('plant_recovery_navigation_input_unverified')
        if receipt.get('action_id') != action or receipt.get('endpoint') != endpoint:
            raise RuntimeError('plant_recovery_navigation_receipt_unverified')
        records.append({key: value for key, value in receipt.items() if key != 'observation'})
        counts[action] = counts.get(action, 0) + 1
        if observed.get('page') == source_page or observed.get('page') in {'unknown', 'loading'}:
            observed = wait_for_transition(device, stop, observed, lambda state: state.get('page') not in {source_page, 'unknown', 'loading'} or continuation_phase(state, device.context) == 'harvest', waiting_pages={source_page, 'unknown', 'loading'}, endpoint=endpoint)
    raise RuntimeError('plant_recovery_navigation_step_limit')
