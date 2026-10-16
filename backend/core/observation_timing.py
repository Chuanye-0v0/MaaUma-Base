"""Age budgets for static navigation, distinct from submissions and animation taps."""
import time
import math
import re
STATIC_NAVIGATION_ACTIONS = {'return_title_after_conflict', 'open_menu', 'return_title', 'confirm_title', 'enter_game', 'open_records', 'select_record', 'scroll_records', 'scroll_records_up', 'switch_other', 'agree', 'open_password', 'fill_username', 'fill_password', 'open_training', 'close_notice', 'open_skills', 'cancel_training_status', 'close_exam_detail', 'close_exams', 'return_home', 'leave_empty_scenario', 'open_training_menu', 'close_trained_detail', 'formation_home', 'open_races', 'open_arena', 'open_opponents', 'choose_top', 'choose_gift', 'continue_matchup', 'cancel_items', 'enable_short', 'open_shop', 'open_sale_from_menu', 'scroll_shop_menu', 'open_missions', 'open_gifts', 'close_gifts', 'open_schedule', 'open_daily_races', 'close_race_help', 'open_daily_details', 'open_moon_course', 'scroll_courses', 'open_selection', 'close_details', 'cancel_limited_sale', 'missions', 'stories', 'roulette', 'options', 'continuous', 'eight_lines', 'save', 'cancel_settings', 'portrait', 'silent', 'back', 'home', 'open_details', 'open_sort', 'sort_evaluation', 'apply_sort', 'descending', 'scroll_top', 'scroll_next', 'confirm_selected', 'continue_lineup', 'back_home', 'scroll_trainees', 'scroll_trainees_up', 'back_training_setup', 'return_title_after_disconnect'}
CHAMPIONS_PRESENTATION_PAGES = {'view_match_result': 'champions_preview', 'continue_standings': 'champions_standings', 'resume_continue_standings': 'champions_standings', 'continue_viewed_standings': 'champions_standings', 'continue_match_result': 'champions_match_result', 'resume_continue_match_result': 'champions_match_result', 'retry_continue_match_result': 'champions_match_result', 'dismiss_match_outcome': 'champions_outcome', 'dismiss_group_promotion': 'champions_promotion'}
CHAMPIONS_PRESENTATION_PAGES.update({f'retry_view_match_result_{attempt}': 'champions_preview' for attempt in range(1, 4)})
CHAMPIONS_PROGRESS_PAGES = {'open_entry': 'champions_hub', 'cancel_entry': 'champions_entry_confirm', 'auto_lineup': 'champions_lineup', 'confirm_auto_lineup': 'champions_auto_confirm', 'confirm_lineup': 'champions_lineup', 'confirm_team': 'champions_team_confirm', 'match_round': 'champions_board', 'launch_match': 'champions_board', 'resume_matching': 'champions_board', 'open_current_round': 'champions_active_hub', 'resume_match': 'champions_board', 'claim_round': 'champions_board', 'choose_intermediate': 'champions_choose', 'choose_advanced': 'champions_choose', 'confirm_registration': 'champions_confirm'}
INVENTORY_NAVIGATION_PAGES = {'open_enhance': {'home'}, 'open_uma': {'enhance_menu'}, 'open_uma_list': {'uma_menu'}, 'open_profile': {'menu'}, 'close_detail': {'uma_detail'}, 'close_menu': {'menu'}, 'close_uma_help': {'uma_unlock_help'}, 'continue_profile_help': {'profile_help'}, 'close_profile_help': {'profile_support_help'}, 'confirm_date_change': {'date_change'}, 'scroll_uma_top': {'uma_list'}, 'scroll_uma': {'uma_list'}, 'dismiss_event_result': {'event_bingo', 'event_notice'}, 'back_event': {'event_roulette', 'event_missions'}, 'open_event_story_menu': {'event_story_playing'}, 'skip_event_story': {'event_story_menu'}, 'confirm_event_story_skip': {'event_story_skip_confirm'}}

def action_kind(observed, action):
    """A page-bound role, not a verb prefix, identifies a neutral action.

    Unknown/new submissions remain strict. Ordinals namespace receipts only;
    they do not turn the same presentation into a resource transaction.
    """
    if not isinstance(observed, dict) or not isinstance(action, str):
        return 'transaction'
    page = observed.get('page')
    ordinal = re.fullmatch('(.+)_([1-5])', action)
    role = ordinal.group(1) if ordinal else action
    if role in CHAMPIONS_PRESENTATION_PAGES and CHAMPIONS_PRESENTATION_PAGES[role] == page:
        return 'presentation'
    if role in CHAMPIONS_PROGRESS_PAGES and CHAMPIONS_PROGRESS_PAGES[role] == page:
        return 'non_resource'
    if page in INVENTORY_NAVIGATION_PAGES.get(action, ()):
        return 'navigation'
    from core.location_navigation import SETTLEMENT_ROUTES
    if page in SETTLEMENT_ROUTES and SETTLEMENT_ROUTES[page] == action:
        return 'presentation'
    if (page, action) in {('champions_round_reward', 'advance_round_reward'), ('champions_round_reward', 'continue_round_reward'), ('champions_claim_receipt', 'close_claim'), ('promotion', 'skip_promotion'), ('story_unlock', 'close_story'), ('login_reward', 'continue_login_reward'), ('claim_success', 'close_claim'), ('multi_rewards', 'close_multi_rewards'), ('race_rewards', 'next_rewards'), ('shop_success', 'close_shop_success'), ('race_result', 'next_result'), ('results', 'continue_results'), ('summary', 'next'), ('story', 'close_story'), ('high_score', 'dismiss_high_score')}:
        return 'presentation'
    horse_selection = observed.get('page') == 'selection' and action.startswith('horse_') and action[6:].isdigit()
    result_view = action in {'view_result', 'view_results', 'resume_view_result'} and observed.get('page') == 'race_ready'
    daily_navigation = (observed.get('page'), action) in {('daily_legend_list', 'scroll_opponents'), ('daily_legend_list', 'choose_opponent'), ('daily_legend_help', 'close_legend_help'), ('champions_registered', 'registration_home'), ('champions_rewards', 'close_registration_rewards'), ('champions_choose', 'close_league_choice'), ('limited_missions', 'missions_daily'), ('limited_missions', 'missions_period'), ('limited_missions', 'close_missions'), ('limited_mission_rewards', 'close_mission_rewards'), ('races', 'open_events'), ('events', 'open_champions'), ('roulette_replace', 'close'), ('roulette_empty', 'close'), ('roulette_limit', 'close'), ('trophy', 'close_trophy'), ('victory', 'continue_victory'), ('defeat', 'continue_defeat'), ('rewards', 'continue_rewards'), ('rewards', 'continue_sale_rewards')}
    return 'navigation' if action in STATIC_NAVIGATION_ACTIONS or horse_selection or result_view or daily_navigation else 'transaction'

def maximum_age(observed, action):
    return 3 if action_kind(observed, action) == 'transaction' else 15

def observation_age(observed):
    stamp = observed.get('captured_monotonic') if isinstance(observed, dict) else None
    if type(stamp) not in (int, float) or not math.isfinite(stamp):
        return None
    age = time.monotonic() - stamp
    return age if math.isfinite(age) and age >= 0 else None

def observation_is_stale(observed, action):
    """Conservative legacy validity gate; unknown clocks never authorize input.

    Callers needing a failure reason should use observation_freshness so absent
    metadata is not reported as evidence that the frame has already expired.
    """
    return observation_freshness(observed, action) != 'fresh'

def observation_freshness(observed, action):
    """Separate missing/invalid acquisition metadata from a measured old frame."""
    if not isinstance(observed, dict) or observed.get('captured_monotonic') is None:
        return 'missing'
    age = observation_age(observed)
    if age is None:
        return 'invalid'
    return 'stale' if age > maximum_age(observed, action) else 'fresh'
