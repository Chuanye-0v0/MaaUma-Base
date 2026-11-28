"""Decide from structured Racing Carnival observations; no screenshot/AI decision path."""
from core.event_menu import unavailable

from .options import normalize_options

ALLOWED = {
    'foreign_event': {'home'},
    'home': {'open_races', 'open_carnival'},
    'races': {'open_events'},
    'events': {'open_carnival', 'home', 'event_scroll_down', 'event_scroll_up'},
    'carnival_closed': {'home'},
    'carnival_network': {'retry_network'},
    'carnival_paid': {'cancel'},
    'carnival_hub': {
        'home', 'open_missions', 'open_refill', 'enter_challenge', 'resume_race',
        'open_track_1', 'open_track_2', 'open_track_3', 'open_track_4',
        'hub_scroll_down', 'hub_scroll_up',
    },
    'carnival_detail': {
        'close_detail', 'open_selection',
        'select_difficulty_困难', 'select_difficulty_普通', 'select_difficulty_简单',
    },
    'sort': {'sort_evaluation', 'apply_sort'},
    'selection': {
        'open_sort', 'descending', 'scroll_top', 'scroll_next',
        'confirm_selected', 'cancel_selection',
    },
    'carnival_race_grid': {'open_items', 'start_race', 'cancel_grid'},
    'carnival_lineup': {'enter_race', 'cancel', 'open_refill'},
    'carnival_race_ready': {'view_result', 'continue_race', 'cancel_preview'},
    'carnival_playback_confirm': {'confirm_playback', 'cancel'},
    'carnival_race': {'skip_race'},
    'carnival_result': {'continue_result'},
    'carnival_points': {'close_points'},
    'carnival_rewards': {'continue_rewards'},
    'carnival_factor': {'continue_factor'},
    'carnival_race_end': {'close_race_end'},
    'carnival_refill_prompt': {'open_refill', 'cancel'},
    'carnival_refill_select': {'select_jelly', 'close_refill_items'},
    'carnival_refill_qty': {
        'confirm_refill', 'cancel', 'minimum_refill', 'maximum_refill',
        'increase_refill', 'decrease_refill',
    },
    'carnival_refill_limit': {'close_refill_limit'},
    'carnival_refill_done': {'close_refill_done'},
    'carnival_missions': {'close_missions', 'tab_daily', 'tab_period', 'claim_all'},
    'carnival_claimed': {'close_claimed'},
    'carnival_notice': {'continue_notice'},
}
PREFIXES = {'selection': 'horse_'}
FORBIDDEN_FRAGMENTS = ('兑换', '养成', '宝石', 'exchange', 'cultivate', 'jewel')
RACE_OWNED_PAGES = {
    'carnival_race_ready', 'carnival_playback_confirm', 'carnival_race',
    'carnival_result', 'carnival_points', 'carnival_rewards',
    'carnival_factor', 'carnival_race_end',
}


def authorize(action, frame, data, options=None):
    options = normalize_options(options)
    page = frame.get('page')
    candidate = frame.get('actions', {}).get(action)
    if not candidate or candidate.get('enabled') is False:
        return False, 'racing_carnival_action_unavailable'
    label = str(candidate.get('label', ''))
    if any(bad in action or bad in label for bad in FORBIDDEN_FRAGMENTS):
        return False, 'racing_carnival_forbidden_control'
    if candidate.get('effect') == 'purchase':
        return False, 'racing_carnival_paid_resource_forbidden'
    if candidate.get('resource_kind') not in {'free', 'carnival_rp', 'mini_jelly', 'earned_rewards'}:
        return False, 'racing_carnival_paid_resource_forbidden'
    if page == 'foreign_event' and (data.get('pending_entry') or data.get('pending_operation')):
        return False, 'racing_carnival_original_operation_preserved'
    if (page in RACE_OWNED_PAGES or action == 'start_race') and not data.get('pending_entry'):
        return False, 'racing_carnival_unowned_race'
    if action not in ALLOWED.get(page, set()) and not action.startswith(PREFIXES.get(page, '\0')):
        return False, 'racing_carnival_action_out_of_page'
    if frame.get('requires_context', True) and not data.get('context_verified'):
        return False, 'racing_carnival_context_unverified'
    if action == 'enter_race':
        if frame.get('items_empty') is not True:
            return False, 'racing_carnival_boost_items_must_be_empty'
        rp = frame.get('rp')
        if type(rp) is not int or rp <= 0:
            return False, 'racing_carnival_rp_unverified'
    op = data.get('pending_operation')
    if op and action in {'enter_race', 'confirm_refill', 'claim_all'}:
        if op.get('status') != 'reserved' or op.get('action') != action:
            return False, 'racing_carnival_operation_pending'
    return True, 'racing_carnival_observed_action'


def _next_track(data, options):
    strategy = options['track_strategy']
    if strategy == 'single_track':
        return options['target_track']
    if strategy == 'daily_rotate':
        return (data.get('rotation_index', 0) % 4) + 1
    if strategy == 'first_win_then_target':
        if not data.get('first_win_sweep_done'):
            probed = data.get('probed_tracks') or {}
            for track in (1, 2, 3, 4):
                if probed.get(str(track)) is not True:
                    return track
    return options['target_track']


def choose(frame, journal, options=None):
    options = normalize_options(options)
    data = journal.data
    page = frame.get('page')
    actions = frame.get('actions', {})
    count = data['races_completed']
    entry = data.get('pending_entry')
    op = data.get('pending_operation')

    def finish(status, reason):
        return dict(kind='finish', status=status, reason=reason)

    def wait(reason):
        return dict(kind='wait', reason=reason)

    def action(name, reason, **updates):
        if name not in actions:
            return wait('racing_carnival_control_rendering:' + name)
        ok, error = authorize(name, frame, data, options)
        return dict(kind='action', action=name, reason=reason, updates=updates) if ok else finish('failed', error)

    def first(names, reason):
        for name in names:
            if name in actions:
                return action(name, reason)
        return wait(reason + ':controls_rendering')

    if page in {'connection_conflict', 'connection_interrupted', 'disconnected'}:
        return finish('failed', 'racing_carnival_' + page)
    if frame.get('requires_context', True) and not data.get('context_verified'):
        if page in {'unknown', 'loading'}:
            return wait('racing_carnival_loading')
        return finish('failed', 'racing_carnival_context_unverified')
    if page == 'carnival_paid':
        return action('cancel', 'racing_carnival_cancel_paid_dialog', exit_error='paid_resource_dialog')
    if page == 'carnival_network':
        return action('retry_network', 'racing_carnival_neutral_network_retry')
    if frame.get('native_busy'):
        return wait('racing_carnival_native_request_in_flight')
    if op and op.get('status') == 'uncertain':
        if page in {'unknown', 'loading'}:
            return wait('racing_carnival_uncertain_observe')
        return finish('failed', 'racing_carnival_uncertain_operation_preserved')
    if op and op.get('status') == 'submitted' and page == op.get('source_page'):
        return wait('racing_carnival_submitted_operation_rendering')
    if page in {'unknown', 'loading'}:
        return wait(frame.get('reason', 'racing_carnival_loading'))

    if page == 'foreign_event':
        if journal.pending:
            return finish('failed', 'racing_carnival_original_operation_preserved')
        return action('home', 'racing_carnival_leave_explicit_foreign_event')

    if page == 'home':
        if journal.pending:
            if (
                not op
                and entry
                and entry.get('status') == 'submitted'
                and entry.get('result_seen') is True
            ):
                return first(['open_races'], 'racing_carnival_original_result_settlement_navigation')
            return finish('failed', 'racing_carnival_pending_owner_at_home')
        if data.get('day_changed'):
            return finish('cancelled', 'racing_carnival_game_day_changed_checkpoint_yield')
        if data.get('exit_error'):
            return finish('failed', 'racing_carnival_' + data['exit_error'])
        if data.get('done') or data.get('exit_requested'):
            return finish(
                'done' if count > 0 else 'skipped',
                'racing_carnival_home_verified',
            )
        return first(['open_races'], 'racing_carnival_original_races_navigation')

    if page == 'races':
        return first(['open_events'], 'racing_carnival_original_event_navigation')

    if page == 'events':
        if 'open_carnival' in actions:
            return action('open_carnival', 'racing_carnival_enter_named_event')
        if unavailable(frame, 'carnival', frame.get('endpoint')):
            return action(
                'home', 'racing_carnival_event_unavailable_return_home',
                event_unavailable=True, exit_requested=True,
            )
        scrolls = data.get('event_scrolls', 0)
        if scrolls < 4 and ('event_scroll_down' in actions or 'event_scroll_up' in actions):
            direction = 'event_scroll_down' if scrolls < 2 or 'event_scroll_up' not in actions else 'event_scroll_up'
            if direction not in actions:
                direction = 'event_scroll_up' if 'event_scroll_up' in actions else 'event_scroll_down'
            return action(direction, 'racing_carnival_scroll_event_menu', event_scrolls=scrolls + 1)
        return action(
            'home', 'racing_carnival_event_missing_return_home',
            event_unavailable=True, exit_requested=True,
        )

    if page == 'carnival_closed':
        if journal.pending:
            return finish('failed', 'racing_carnival_pending_closed_event')
        return action('home', 'racing_carnival_closed_return_home', exit_requested=True)

    scans_complete = data.get('mission_scan_at') == count and data.get('period_scan_at') == count
    can_refill = options['allow_existing_jelly'] and data['jelly_reserved'] < options['max_jellies']
    exhausted = bool(data.get('resources_exhausted') or journal.capped())

    if page == 'carnival_hub':
        if data.get('day_changed'):
            return action('home', 'racing_carnival_day_boundary_return_home', exit_requested=True, return_to_hub=False)
        if data.get('exit_error'):
            return action('home', 'racing_carnival_failed_return_home', exit_requested=True, return_to_hub=False)
        if data.get('exit_requested'):
            return action('home', 'racing_carnival_finish_exit_to_home', return_to_hub=False)
        if 'resume_race' in actions:
            return action(
                'resume_race', 'racing_carnival_resume_in_progress_match',
                active_track=None if frame.get('ongoing_is_challenge') else data.get('active_track'),
                current_is_challenge=bool(frame.get('ongoing_is_challenge')),
                hub_scrolls=0,
                return_to_hub=False,
            )
        if journal.pending:
            return finish('failed', 'racing_carnival_pending_entry_at_hub')
        if (exhausted or frame.get('rp') == 0) and not scans_complete and 'open_missions' in actions:
            return action('open_missions', 'racing_carnival_check_missions_before_refill_or_exit', return_to_hub=False)
        if exhausted:
            return action('home', 'racing_carnival_budget_or_resources_finished', exit_requested=True, return_to_hub=False)
        if frame.get('rp') == 0:
            if can_refill:
                if 'open_refill' in actions:
                    return action('open_refill', 'racing_carnival_native_hub_rp_refill', refill_rp_before=0, return_to_hub=False, refill_just_completed=False)
                return wait('racing_carnival_control_rendering:open_refill')
            return action(
                'home', 'racing_carnival_rp_exhausted_return_home',
                resources_exhausted=True, exit_requested=True, return_to_hub=False,
            )
        if (
            options['challenge_enabled']
            and (frame.get('challenge_full') or frame.get('challenge_banner_active'))
            and 'enter_challenge' in actions
        ):
            return action(
                'enter_challenge', 'racing_carnival_challenge_priority',
                active_track=None, current_is_challenge=True, hub_scrolls=0, return_to_hub=False,
            )
        track = _next_track(data, options)
        track_action = f'open_track_{track}'
        if track_action in actions:
            return action(
                track_action, 'racing_carnival_select_track',
                active_track=track, current_is_challenge=False, hub_scrolls=0, return_to_hub=False,
            )
        scrolls = data.get('hub_scrolls', 0)
        if scrolls < 4 and ('hub_scroll_down' in actions or 'hub_scroll_up' in actions):
            if track in (3, 4) and 'hub_scroll_down' in actions:
                direction = 'hub_scroll_down'
            elif track in (1, 2) and 'hub_scroll_up' in actions:
                direction = 'hub_scroll_up'
            else:
                direction = 'hub_scroll_down' if 'hub_scroll_down' in actions else 'hub_scroll_up'
            return action(direction, 'racing_carnival_scroll_hub_tracks', hub_scrolls=scrolls + 1)
        return wait('racing_carnival_track_grid_rendering')

    if page == 'carnival_detail':
        if entry:
            return finish('failed', 'racing_carnival_entry_pending_at_detail')
        if data.get('day_changed') or exhausted or data.get('return_to_hub') or (frame.get('rp') == 0 and not scans_complete):
            return action('close_detail', 'racing_carnival_close_detail_before_exit')
        if (
            not frame.get('is_challenge')
            and options['track_strategy'] == 'first_win_then_target'
        ):
            if not data.get('first_win_sweep_done') and frame.get('first_win_claimed') is True:
                return action('close_detail', 'racing_carnival_skip_claimed_first_win_track')
            if data.get('first_win_sweep_done') and data.get('active_track') not in (None, options['target_track']):
                return action('close_detail', 'racing_carnival_return_to_target_track')
        if not frame.get('is_challenge'):
            desired_diff = options['difficulty']
            diff_action = 'select_difficulty_' + desired_diff
            if diff_action in actions and frame.get('selected_difficulty') != desired_diff:
                return action(
                    diff_action, 'racing_carnival_select_configured_difficulty',
                    active_difficulty=desired_diff,
                )
        return action(
            'open_selection', 'racing_carnival_open_horse_selection',
            active_difficulty=frame.get('selected_difficulty') or options['difficulty'],
        )

    if page == 'sort':
        if frame.get('evaluation_selected') is True and 'apply_sort' in actions:
            return action('apply_sort', 'racing_carnival_apply_evaluation_sort')
        if 'sort_evaluation' in actions:
            return action('sort_evaluation', 'racing_carnival_select_evaluation_sort')
        return wait('racing_carnival_sort_dialog_rendering')

    if page == 'selection':
        if (data.get('day_changed') or exhausted or data.get('return_to_hub') or (frame.get('rp') == 0 and not scans_complete)) and 'cancel_selection' in actions:
            return action('cancel_selection', 'racing_carnival_leave_selection_to_hub')
        if frame.get('horse') is not None and 'confirm_selected' in actions:
            return action('confirm_selected', 'racing_carnival_confirm_qualified_horse')
        return wait('racing_carnival_horse_selection_pending')

    if page == 'carnival_race_grid':
        if op:
            return finish('failed', 'racing_carnival_entry_pending_at_grid')
        if entry:
            return action('start_race', 'racing_carnival_start_owned_race')
        if journal.pending:
            return finish('failed', 'racing_carnival_entry_pending_at_grid')
        if (data.get('day_changed') or exhausted or data.get('return_to_hub') or (frame.get('rp') == 0 and not scans_complete)) and 'cancel_grid' in actions:
            return action('cancel_grid', 'racing_carnival_leave_grid_to_hub', return_to_hub=True)
        return action('open_items', 'racing_carnival_open_items_confirm')

    if page == 'carnival_lineup':
        if entry:
            return finish('failed', 'racing_carnival_entry_already_submitted')
        if data.get('day_changed'):
            return action('cancel', 'racing_carnival_day_boundary_no_new_entry', return_to_hub=True)
        if journal.capped():
            return action('cancel', 'racing_carnival_daily_race_cap', return_to_hub=True)
        if frame.get('rp') == 0:
            if not scans_complete:
                return action('cancel', 'racing_carnival_check_missions_before_refill', return_to_hub=True)
            if not can_refill:
                return action('cancel', 'racing_carnival_no_more_refill', resources_exhausted=True, return_to_hub=True)
            if 'open_refill' in actions:
                return action('open_refill', 'racing_carnival_verified_zero_rp_refill', refill_rp_before=0)
            return action('cancel', 'racing_carnival_disabled_zero_rp_preview', return_to_hub=True)
        if frame.get('rp') is None:
            return wait('racing_carnival_rp_unverified')
        if frame.get('items_empty') is None:
            return wait('racing_carnival_items_modal_rendering')
        if frame.get('items_empty') is not True:
            if 'cancel' in actions:
                return action(
                    'cancel', 'racing_carnival_non_empty_boost_items_forbidden',
                    exit_error='non_empty_boost_items',
                )
            return finish('failed', 'racing_carnival_boost_items_must_be_empty')
        return action('enter_race', 'racing_carnival_one_existing_rp')

    if page == 'carnival_playback_confirm':
        if not entry:
            return finish('failed', 'racing_carnival_unowned_race')
        return first(['confirm_playback', 'cancel'], 'racing_carnival_confirm_race_playback')

    if page == 'carnival_race_ready':
        if not entry:
            return finish('failed', 'racing_carnival_unowned_race')
        return first(['view_result', 'continue_race'], 'racing_carnival_progress_entered_race')

    if page == 'carnival_race':
        if not entry:
            return finish('failed', 'racing_carnival_unowned_race')
        return action('skip_race', 'racing_carnival_same_entry_progress')

    if page == 'carnival_result':
        if not entry:
            return finish('failed', 'racing_carnival_unowned_result')
        return action('continue_result', 'racing_carnival_accept_result')

    if page == 'carnival_points':
        if not entry:
            return finish('failed', 'racing_carnival_unowned_result')
        return action('close_points', 'racing_carnival_close_points_modal')

    if page == 'carnival_rewards':
        if not entry:
            return finish('failed', 'racing_carnival_unowned_result')
        return action('continue_rewards', 'racing_carnival_continue_rewards')

    if page == 'carnival_factor':
        if not entry:
            return finish('failed', 'racing_carnival_unowned_result')
        return action('continue_factor', 'racing_carnival_continue_factor')

    if page == 'carnival_race_end':
        if not entry:
            return finish('failed', 'racing_carnival_unowned_result')
        return action('close_race_end', 'racing_carnival_close_race_end_left_only')

    if page == 'carnival_refill_done':
        return action('close_refill_done', 'racing_carnival_close_verified_rp_refill', refill_just_completed=True)

    if page == 'carnival_refill_select':
        if entry:
            return finish('failed', 'racing_carnival_owned_entry_at_item_selector')
        if data.get('refill_just_completed') or (type(frame.get('rp')) is int and frame['rp'] >= 5):
            return action('close_refill_items', 'racing_carnival_close_selector_after_refill', refill_just_completed=False)
        if (
            data.get('day_changed')
            or not can_refill
            or journal.capped()
            or frame.get('stock_before') == 0
        ):
            return action('close_refill_items', 'racing_carnival_no_existing_item_budget', resources_exhausted=True)
        return action('select_jelly', 'racing_carnival_select_existing_mini_jelly_preview')

    if page == 'carnival_refill_prompt':
        if entry:
            return finish('failed', 'racing_carnival_owned_entry_at_refill_prompt')
        if data.get('day_changed') or not can_refill or journal.capped():
            return action('cancel', 'racing_carnival_no_refill_budget', resources_exhausted=True)
        if not scans_complete:
            return action('cancel', 'racing_carnival_check_missions_before_refill')
        return action('open_refill', 'racing_carnival_open_existing_item_selector', refill_rp_before=0, refill_just_completed=False)

    if page == 'carnival_refill_qty':
        if entry:
            return finish('failed', 'racing_carnival_entry_pending_at_refill')
        if data.get('day_changed'):
            return action('cancel', 'racing_carnival_day_boundary_no_refill')
        rp_val = frame.get('rp') if frame.get('rp') is not None else data.get('refill_rp_before')
        if type(rp_val) is not int:
            return wait('racing_carnival_refill_rp_count_unknown')
        cap_rem = journal.cap_remaining()
        bounds = [options['max_jellies'] - data['jelly_reserved'], 5 - rp_val]
        if cap_rem is not None:
            bounds.append(cap_rem)
        left = min(bounds)
        if not options['allow_existing_jelly'] or options['max_jellies'] - data['jelly_reserved'] <= 0 or cap_rem == 0:
            return action('cancel', 'racing_carnival_refill_budget_finished', resources_exhausted=True)
        if 5 - rp_val <= 0:
            return action('cancel', 'racing_carnival_rp_already_full')
        if frame.get('stock_before') == 0:
            return action('cancel', 'racing_carnival_no_existing_jelly', resources_exhausted=True)
        if frame.get('quantity') is None or frame.get('stock_before') is None:
            return wait('racing_carnival_refill_values_rendering')
        wanted = min(left, frame['stock_before'])
        if frame['quantity'] > wanted:
            return first(['decrease_refill', 'minimum_refill'], 'racing_carnival_reduce_refill_quantity')
        if frame['quantity'] < wanted:
            if wanted == 5 and 'maximum_refill' in actions:
                return action('maximum_refill', 'racing_carnival_set_bounded_refill_quantity')
            return action('increase_refill', 'racing_carnival_set_bounded_refill_quantity')
        return action('confirm_refill', 'racing_carnival_existing_mini_jelly_only')

    if page == 'carnival_refill_limit':
        return action('close_refill_limit', 'racing_carnival_native_refill_limit_verified', resources_exhausted=True)

    if page == 'carnival_missions':
        if data.get('day_changed'):
            return action('close_missions', 'racing_carnival_day_boundary_no_new_claim')
        if frame.get('claimable', 0) > 0:
            if op:
                return wait('racing_carnival_claim_submission_settling')
            return action('claim_all', 'racing_carnival_claim_visible_earned_rewards_once')
        if data.get('mission_scan_at') != count:
            return action('tab_daily', 'racing_carnival_scan_daily_rewards')
        if data.get('period_scan_at') != count:
            return action('tab_period', 'racing_carnival_scan_period_rewards')
        return action('close_missions', 'racing_carnival_missions_checked')

    if page == 'carnival_claimed':
        return action('close_claimed', 'racing_carnival_close_actual_rewards')

    if page == 'carnival_notice':
        return action('continue_notice', 'racing_carnival_neutral_presentation')

    return wait('racing_carnival_wait_known_page')
