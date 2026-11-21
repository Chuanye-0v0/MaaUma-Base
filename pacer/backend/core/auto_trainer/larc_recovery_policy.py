"""Bounded Mei recovery preference."""
import math

from core.auto_trainer.rejection_policy import is_hard_rejected
from core.auto_trainer.thresholds import MEI_RECOVERY_PREFERENCE_BONUS

NO_LOCAL_ACTION_REASON = 'no_eligible_action_after_mei_priority'


def _domestic(state):
    period = state.scenario_state.get('larc_period') or {}
    if period.get('status') != 'verified':
        return False
    value = period.get('value') or {}
    if value.get('year_index') == 1:
        return True
    return (value.get('year_index') in {2, 3}
        and type(value.get('month')) is int
        and value.get('month') in {1, 2, 3, 4, 5, 6, 10, 11, 12}
        and value.get('half') in {'first', 'second'})


def current_mei_available(state, scored):
    """Consume an already verified candidate, not a new capture or file read."""
    if not _domestic(state):
        return False
    packet = state.scenario_state.get('larc_friend_outing') or {}
    completed = packet.get('completed')
    if (packet.get('schema_version') != 'larc_friend_outing.v1'
            or packet.get('status') != 'accepted' or packet.get('character') != 'satake_mei'
            or packet.get('unlocked') is not True or type(completed) is not int
            or not 0 <= completed < 5 or packet.get('pending') is not None):
        return False
    source = packet.get('source') or {}
    run_id = source.get('run_session_id')
    captures = [candidate.raw_data.get('capture', {}) for candidate in state.training_options]
    if (not run_id or source.get('turn_index') != state.turn or len(captures) != 5
            or any(capture.get('run_session_id') != run_id
                   or capture.get('turn_index') != state.turn for capture in captures)):
        return False
    wanted = f'outing.satake_mei.{completed + 1}'
    # recovery_candidates only emits this row after bound_outing_packet has
    # verified its actual source. Tuning may reject it for no recovery need;
    # that does not make the physical Mei entry unavailable.
    return any(row.get('action_id') == wanted and row.get('action') == 'outing'
        and (row.get('source') or {}).get('run_session_id') == run_id
        and (row.get('source') or {}).get('turn_index') == state.turn for row in scored)


def apply_recovery_rules(state, goal, scored):
    """Favor the bound Mei outing without excluding other recovery."""
    from core.auto_trainer.larc_strategy_extension import active_provider
    from core.auto_trainer.larc_strategy_params import params_for_goal, unified_larc_enabled
    if not unified_larc_enabled(goal):
        return {'active': False}
    params = params_for_goal(goal)
    provider = active_provider()
    mei_cmp = provider.mei_comparison_enabled(params)
    available = current_mei_available(state, scored)
    packet = state.scenario_state.get('larc_friend_outing') or {}
    wanted = f"outing.satake_mei.{packet['completed'] + 1}" if available else None
    rest_row = next((r for r in scored if r.get('action_id') == 'rest.normal'), None)
    rest_after = ((rest_row or {}).get('planning') or {}).get('energy_horizon', {}).get('after_vitality_percent')
    for row in scored:
        if row.get('action') not in {'rest', 'outing'}:
            continue
        favored = (wanted is not None and row.get('action_id') == wanted
                   and row.get('action') == 'outing' and not is_hard_rejected(row)
                   and row.get('allowed') is not False and row.get('eligible') is not False)
        if favored and mei_cmp:
            bonus = provider.apply_recovery_policy_bonus(params, row, rest_after, MEI_RECOVERY_PREFERENCE_BONUS)
        else:
            bonus = MEI_RECOVERY_PREFERENCE_BONUS if favored else 0.
        components = row.get('components') or {}
        previous = components.pop('mei_recovery_preference', 0.)
        if bonus or previous:
            row['score'] = round(row['score'] + bonus - previous, 4)
            if bonus:
                components['mei_recovery_preference'] = bonus
            row['components'] = components
        row.setdefault('planning', {})['recovery_policy'] = {
            'mei_currently_available': available, 'dedicated_recovery_cap': None,
            'mei_preference_bonus': bonus, 'basis': 'bounded preference for the current eligible Mei outing'}
    return {'active': True, 'mei_currently_available': available,
        'excluded_recovery_actions': [], 'dedicated_recovery_cap': None,
        'no_local_action_reason': None}


def known_future_outings(outings):
    """Keep only a finite consecutive inventory already supplied by the run."""
    if not isinstance(outings, (list, tuple)):
        return (), 'unknown'
    if not outings:
        return (), 'unavailable_in_known_context'
    stages = [item.get('stage') if isinstance(item, dict) else None for item in outings]
    if (any(type(stage) is not int or not 1 <= stage <= 5 for stage in stages)
            or stages != list(range(stages[0], stages[0] + len(stages)))):
        return (), 'unknown'
    if any(type(item.get(key)) not in (int, float) or not math.isfinite(item[key])
           or key == 'recovery_percent' and item[key] < 0
           for item in outings for key in ('recovery_percent', 'numeric_value')):
        return (), 'unknown'
    return tuple(outings), 'known_finite_inventory'


def future_mei_available(domestic, outing_index, outings):
    """Only domestic steps can consume the next known, unspent Mei stage."""
    return bool(domestic and type(outing_index) is int and 0 <= outing_index < len(outings))
