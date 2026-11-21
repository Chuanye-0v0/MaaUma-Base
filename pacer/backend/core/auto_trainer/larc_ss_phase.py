"""Current L'Arc free-action phase; no strategy, forecast or execution."""
from collections.abc import Mapping

from core.auto_trainer.larc_calendar import FORCED_RACES, free_action_days, is_overseas, period_ordinal


PRE_FIRST_ARC_DOMESTIC = 'pre_first_arc_domestic'
POST_FIRST_ARC_DOMESTIC = 'post_first_arc_domestic'
OVERSEAS = 'overseas'
UNAVAILABLE = 'unavailable'
FIRST_ARC_ORDINAL = next(day for day, race in FORCED_RACES.items() if race == 'classic_arc')


def phase_of(state):
    """Classify a verified present training menu, including pre-debut.

    The first Arc boundary is the shared scenario calendar's classic Arc,
    not the junior/classic year boundary. Mandatory race menus are not free
    actions and an unknown date never acquires a domestic SS preference.
    """
    context = getattr(state, 'scenario_state', None)
    if (getattr(state, 'screen', None) != 'training_menu' or not isinstance(context, Mapping)
            or context.get('scenario') != 'larc'):
        return UNAVAILABLE
    packet = context.get('larc_period')
    if not isinstance(packet, Mapping) or packet.get('status') not in ('verified', 'accepted'):
        return UNAVAILABLE
    period = packet.get('value')
    if not isinstance(period, Mapping):
        return UNAVAILABLE
    year = period.get('year_index')
    if type(year) is not int or year not in (1, 2, 3):
        return UNAVAILABLE
    if period.get('stage') not in (None, {1: 'junior', 2: 'classic', 3: 'senior'}[year]):
        return UNAVAILABLE
    if period.get('pre_debut') is True:
        return PRE_FIRST_ARC_DOMESTIC if year == 1 and period.get('stage') == 'junior' else UNAVAILABLE
    ordinal = period_ordinal(period)
    if ordinal is None or ordinal not in free_action_days(ordinal, ordinal + 1):
        return UNAVAILABLE
    if is_overseas(ordinal):
        return OVERSEAS
    return PRE_FIRST_ARC_DOMESTIC if ordinal < FIRST_ARC_ORDINAL else POST_FIRST_ARC_DOMESTIC


def post_first_arc_free_action(state):
    """Domestic return + second expedition, never the first expedition/races."""
    if phase_of(state) not in {POST_FIRST_ARC_DOMESTIC, OVERSEAS}:
        return False
    period=(getattr(state,'scenario_state',{})or{}).get('larc_period')or{}
    ordinal=period_ordinal(period.get('value')or{})
    return ordinal is not None and ordinal>FIRST_ARC_ORDINAL


def year1_full_comparison_status(state, goal, preview=None):
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    if unified_larc_enabled(goal):
        from core.auto_trainer.larc_ss_matched_stock import comparison_status
        status = comparison_status(state, goal, preview)
        if status is not None:
            return status
    return {
        'enabled': False,
        'active': False,
        'reason': 'revision_not_enabled',
        'current_full': None,
        'numeric_known': False,
        'victory_guaranteed': False,
        'collect_pending': False,
        'historical_deferral_cap_applied': False,
    }
