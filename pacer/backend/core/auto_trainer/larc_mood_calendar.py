"""Known fixed mood changes, used before choosing a turn-consuming action.

Reference calendar: https://wikiwiki.jp/vip_umamusu/Reach%20for%20the%20stars%20%E3%83%97%E3%83%AD%E3%82%B8%E3%82%A7%E3%82%AF%E3%83%88L%27Arc
Classic Oct late ends with -2; Nov early ends with +3 (not an absolute reset).
Thus the changed mood is visible on the following Nov early/late main menu.
These are scenario reference effects, not observations of a future outcome.
"""
from core.auto_trainer.larc_calendar import period_at, period_ordinal
from core.auto_trainer.larc_mood import mood_recovery_value
from core.auto_trainer.larc_policy import strategy_refinement_enabled


MOOD_WINDOW = 4
FIXED_MOOD_DELTAS = {43: -2, 44: 3}


def action_mood_value(state, goal, change, *, grants_mood_down_protection=False,
                      mood_down_protection=None):
    """Value a known action's mood effect until an approaching fixed event.

    Do not call this for a support event whose ordering within the current
    turn is unknown. Displayed training gains already include current mood.
    Unknown existing mood protection is evaluated both ways, not inferred
    from lucky constitution or an old acquisition event.
    """
    base = mood_recovery_value(state.scenario_state.get('mood', 'unknown'), change)
    if not strategy_refinement_enabled(goal) or base['before'] is None:
        return base
    packet = state.scenario_state.get('larc_period', {})
    period = packet.get('value', {}) if isinstance(packet, dict) else {}
    day = (period_ordinal(period) if isinstance(packet, dict) and packet.get('status') == 'verified'
           and isinstance(period, dict) else None)
    if day is None:
        return dict(base, calendar_status='date_unknown')
    events = [(date, delta) for date, delta in FIXED_MOOD_DELTAS.items()
              if day <= date < day + MOOD_WINDOW]
    if not events:
        return base
    if type(mood_down_protection) is not bool and mood_down_protection is not None:
        raise ValueError('mood_down_protection_must_be_bool_or_unknown')
    protection_states = (False, True) if mood_down_protection is None else (mood_down_protection,)
    projections = []
    for protected in protection_states:
        ordinary, changed = base['before'], base['after']
        ordinary_shield = protected
        changed_shield = protected or grants_mood_down_protection
        windows = []
        for offset in range(MOOD_WINDOW):
            end_day = day + offset
            delta = FIXED_MOOD_DELTAS.get(end_day, 0)
            if delta < 0:
                if ordinary_shield:
                    ordinary_shield = False
                else:
                    ordinary = max(-2, ordinary + delta)
                if changed_shield:
                    changed_shield = False
                else:
                    changed = max(-2, changed + delta)
            elif delta > 0:
                ordinary, changed = min(2, ordinary + delta), min(2, changed + delta)
            marginal = mood_recovery_value(ordinary, changed - ordinary)['score']
            windows.append({'period': period_at(end_day + 1), 'without_action_mood': ordinary,
                            'with_action_mood': changed, 'marginal_preference': marginal})
        projections.append({'existing_mood_protection': protected, 'windows': windows,
                            'score': round(sum(row['marginal_preference'] for row in windows) / MOOD_WINDOW, 4)})
    scores = [row['score'] for row in projections]
    return {**base, 'score': min(scores), 'immediate_preference': base['score'],
            'calendar_status': 'fixed_event_window', 'calendar_score_range': [min(scores), max(scores)],
            'scheduled_events': [{'after_period': period_at(date), 'mood_delta': delta}
                                 for date, delta in events],
            'projections': projections, 'grants_mood_down_protection': grants_mood_down_protection,
            'basis': 'four action windows of marginal mood preference; lower bound if existing protection is unknown; no random event prediction'}
