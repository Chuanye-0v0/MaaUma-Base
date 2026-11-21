"""Selection-only energy tie preference at the last free domestic action.

The caller supplies original current scorer rows after their normal legality,
risk and user-priority checks. This module adds no candidate or score bonus.
"""
from core.auto_trainer.rejection_policy import is_hard_rejected
import math

from core.auto_trainer.larc_close_training_balance import CLOSE_SCORE_FRACTION
from core.auto_trainer.larc_calendar import free_action_days, is_overseas, period_at, period_ordinal
from core.auto_trainer.larc_recovery import period_value, scheduled_recovery_after, PLANNING_START_ENERGY
from core.auto_trainer.larc_capacity import capacity_bounds, planning_capacity, recover_points
from core.auto_trainer.state import TRAINING_TYPES


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _kind(row):
    if row.get('training_type') in TRAINING_TYPES:
        return 'train'
    if row.get('action_id') == 'ss' or row.get('action') == 'ss':
        return 'ss'
    if str(row.get('action_id', '')).startswith('outing.satake_mei.'):
        return 'mei'
    return None


def _boundary(state):
    now = period_ordinal(period_value(state))
    if now is None or is_overseas(now):
        return None
    if now not in free_action_days(now, now + 1):
        return None
    later = free_action_days(now + 1)
    if not later or not is_overseas(later[0]):
        return None
    return now, later[0]


def _mei_currently_usable(state, row):
    # The normal recovery producer is the authority for whether this stage is
    # currently executable. Do not revive a rejected or unavailable stage.
    packet = state.scenario_state.get('larc_friend_outing') or {}
    if (packet.get('status') != 'accepted' or packet.get('unlocked') is not True
            or type(packet.get('completed')) is not int or not 0 <= packet['completed'] < 5
            or packet.get('pending') not in (None, {})):
        return False
    expected = 'outing.satake_mei.' + str(packet['completed'] + 1)
    return row.get('action_id') == expected


def _risk(row, kind, preview):
    if kind == 'train':
        value = row.get('failure_rate')
        return value if _number(value) and 0 <= value <= 100 else None
    if kind == 'mei':
        return 0.
    if kind == 'ss':
        from core.auto_trainer.larc_ss_visible import ss_victory_evidence
        if (not isinstance(preview, dict) or preview.get('status') != 'accepted'
                or preview.get('stage') != 'preview' or preview.get('ready_count') != 5
                or not ss_victory_evidence(preview).get('guaranteed')):
            return None
        return 0.
    return None


def _energy_profile(state, row, preview, dates):
    kind = _kind(row)
    if kind is None or not _number(state.vitality) or not 0 <= state.vitality <= 100:
        return None
    risk = _risk(row, kind, preview)
    if risk is None:
        return None
    low, high = capacity_bounds(state)
    if not all(_number(x) for x in (low, high)) or not 0 < low <= high:
        return None
    bar = state.vitality
    bounds = (low, high) if low != high else (low,)
    direct = []
    if kind == 'train':
        plan = row.get('planning') or {}
        delta = plan.get('training_vitality_percent')
        if (not _number(delta) or plan.get('training_vitality_basis')
                not in ('observed', 'observed_bar_preview')):
            return None
        after = min(100., max(0., bar + delta))
        direct = [(after * maximum / 100, maximum) for maximum in bounds]
        basis = 'current_observed_net_training_change'
    elif kind == 'mei':
        if not _mei_currently_usable(state, row):
            return None
        amounts = (row.get('effects') or {}).get('vitality_points')
        known_points = (isinstance(amounts, (list, tuple)) and len(amounts) == 2
                        and all(_number(x) for x in amounts) and 0 <= amounts[0] <= amounts[1])
        if known_points:
            direct = [recover_points(bar * maximum / 100, maximum, amount)
                      for maximum in bounds for amount in amounts]
        else:
            delta = row.get('recovery_percent')
            if (low != high or not isinstance(delta, (list, tuple)) or len(delta) != 2
                    or not all(_number(x) for x in delta) or not 0 <= delta[0] <= delta[1]):
                return None
            direct = [(min(100., bar + amount) * maximum / 100, maximum)
                      for maximum in bounds for amount in delta]
        if not any(points > bar * maximum / 100 + 1e-6 for points, maximum in direct):
            return None
        basis = 'current_legal_mei_usable_recovery_interval'
    else:
        recovery = row.get('recovery') or {}
        amount, observed = recovery.get('points'), recovery.get('observed_points')
        increase = recovery.get('max_increase', 0)
        exact = (type(amount) is int and amount >= 0 and type(observed) is int and observed == amount
                 and recovery.get('rule_points', 0) == 0)
        # A safe SS is productive and has no base energy consumption. Missing
        # recovery is not an invented heal: compare its known zero-heal lower
        # bound, with an explicitly unknown upper bound.
        minimum = observed if type(observed) is int and observed >= 0 else 0
        if type(increase) is not int or not 0 <= increase <= 20:
            increase = 0
            exact = False
        direct = [recover_points(bar * maximum / 100, maximum, minimum, increase)
                  for maximum in bounds]
        basis = 'current_safe_ss_known_zero_consumption_plus_observed_heal_lower_bound'
    # Each known heal belongs after its own actual date. Generic reserve,
    # random events and another future main action are deliberately excluded.
    scheduled = []
    denominator = planning_capacity(state)
    if not _number(denominator) or denominator <= 0:
        return None
    for day in range(dates[0], dates[1]):
        percentage = scheduled_recovery_after(state, period_at(day))
        if not _number(percentage) or percentage < 0:
            return None
        if percentage:
            scheduled.append((day, percentage * denominator / 100))
    values = []
    if kind == 'mei':
        original_capacities = [maximum for maximum in bounds for _ in range(2)]
    else:
        original_capacities = list(bounds)
    for (points, maximum), original in zip(direct, original_capacities):
        for _, heal in scheduled:
            points, maximum = recover_points(points, maximum, heal)
        # Use a common original-capacity bar: raising capacity alone is not
        # recovery, and no second heal reward is added to the scorer.
        values.append(points * 100 / original)
    return {'kind': kind, 'failure_rate': risk,
            'entry_energy_base_bar_bounds': [round(min(values), 6),
                None if kind == 'ss' and not exact else round(max(values), 6)],
            'unknown_ss_recovery_not_credited': kind == 'ss' and not exact,
            'direct_energy_basis': basis, 'known_scheduled_heals':
            [{'after_ordinal': day, 'points': amount} for day, amount in scheduled]}


def boundary_readiness(state, goal, rows, *, incumbent_action_id=None, ss_preview=None):
    """Return a labelled tie choice; never mutate scores or authorize inputs."""
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    enabled = unified_larc_enabled(goal)
    result = {'active': False, 'changed': False, 'selected_action_id': incumbent_action_id,
              'score_changed': False, 'extra_recovery_value_added': False,
              'future_layout_or_training_count_promised': False}
    if not enabled:
        return {**result, 'reason': 'revision_not_enabled'}
    dates = _boundary(state)
    if dates is None:
        return {**result, 'reason': 'not_last_free_domestic_action'}
    from core.auto_trainer.larc_timeline_workflow import selection_key
    eligible = [row for row in rows if isinstance(row, dict) and not is_hard_rejected(row)
                and isinstance(row.get('action_id'), str) and _number(row.get('score'))]
    if not eligible:
        return {**result, 'reason': 'no_current_eligible_rows'}
    best = next((r for r in eligible if r['action_id'] == incumbent_action_id), None) if incumbent_action_id else min(eligible, key=selection_key)
    if best is None or best['score'] <= 0:
        return {**result, 'reason': 'incumbent_unavailable'}
    result['selected_action_id'] = result['incumbent_action_id'] = best['action_id']
    preview = ss_preview if ss_preview is not None else state.scenario_state.get('larc_ss_preview')
    before = _energy_profile(state, best, preview, dates)
    if before is None:
        return {**result, 'reason': 'incumbent_energy_or_risk_unknown'}
    from core.auto_trainer.larc_strategy_rules import pre_expedition_energy_spec
    band_spec = pre_expedition_energy_spec(state, goal)
    reference = band_spec['band_low'] if band_spec is not None else PLANNING_START_ENERGY.get(getattr(goal, 'risk_tolerance', None))
    if not _number(reference):
        return {**result, 'reason': 'preparation_reference_unknown'}
    upper = before['entry_energy_base_bar_bounds'][1]
    if upper is None:
        return {**result, 'reason': 'incumbent_energy_requirement_uncertain', 'incumbent_energy': before}
    if upper >= reference:
        return {**result, 'reason': 'incumbent_entry_energy_already_prepared',
                'preparation_reference_percent': reference, 'incumbent_energy': before}
    rank = (best.get('score_workflow') or {}).get('priority_rank', 0)
    band = best['score'] * CLOSE_SCORE_FRACTION
    profiles = []
    for row in eligible:
        if ((row.get('score_workflow') or {}).get('priority_rank', 0) != rank
                or abs(best['score'] - row['score']) > band + 1e-9):
            continue
        profile = _energy_profile(state, row, preview, dates)
        if profile is None or profile['failure_rate'] > before['failure_rate']:
            continue
        profiles.append({'action_id': row['action_id'], 'original_score': row['score'], **profile})
    improvements = [r for r in profiles if r['action_id'] != best['action_id']
                    and r['entry_energy_base_bar_bounds'][0] > upper + 1e-6]
    chosen = min(improvements, key=lambda r: (-r['entry_energy_base_bar_bounds'][0], -r['original_score'], r['action_id'])) if improvements else None
    return {**result, 'active': True, 'changed': chosen is not None,
            'selected_action_id': chosen['action_id'] if chosen else best['action_id'],
            'reason': 'close_score_known_entry_energy_preferred' if chosen else 'no_dominating_close_energy_alternative',
            'current_ordinal': dates[0], 'next_free_ordinal': dates[1],
            'skipped_fixed_calendar_ordinals': list(range(dates[0] + 1, dates[1])),
            'score_band_fraction': CLOSE_SCORE_FRACTION, 'score_band': round(band, 6),
            'preparation_reference_percent': reference,
            'preparation_reference_basis': 'existing_PLANNING_START_ENERGY_not_a_new_execution_gate',
            'incumbent_original_score': best['score'], 'incumbent_energy': before,
            'candidates': profiles, 'basis': 'existing5percent band; same current priority/risk; known usable energy only'}
