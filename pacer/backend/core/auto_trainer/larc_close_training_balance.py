"""Bounded selection preference for close, post-first-Arc training choices.

Actual gains, recovery values and user priorities are unchanged. The logged
preference only breaks a small score band; it is not another energy reward.
"""
from core.auto_trainer.rejection_policy import is_hard_rejected
import math

from core.auto_trainer.larc_ss_energy import domestic_increment_scope
from core.auto_trainer.larc_strategy_params import DEFAULT_PARAMS, params_for_goal, unified_larc_enabled
from core.auto_trainer.state import TRAINING_TYPES

CLOSE_SCORE_FRACTION = DEFAULT_PARAMS.close_score_fraction
CANDIDATE_ID = 'larc_strategy181_domestic_ABC'


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _recovery_steps(route):
    return sum(step == 'rest' or step == 'outing' or step.startswith('outing:')
               for step in route)


def _recovery_order(route):
    first = next((index for index, step in enumerate(route)
                  if step == 'rest' or step == 'outing' or step.startswith('outing:')), len(route))
    productive = sum(step == 'train' or step.startswith('train:') or step == 'wisdom'
                     or step == 'held_ss' for step in route[:first])
    return first, productive


def _profile(state, goal, row, candidates):
    candidate = candidates.get(row.get('training_type'))
    if candidate is None or not _number(state.vitality):
        return None
    plan = row.get('planning') or {}
    delta = plan.get('training_vitality_percent')
    if not _number(delta):
        return None
    after = min(100., max(0., state.vitality + delta))
    horizon = plan.get('energy_horizon') or {}
    route = horizon.get('future_route_after')
    steps = horizon.get('steps')
    if (horizon.get('active') is not True or type(steps) is not int or steps <= 0
            or not isinstance(route, list) or len(route) != steps
            or any(not isinstance(step, str) for step in route)):
        return None
    observed_after = horizon.get('after_vitality_percent')
    if not _number(observed_after) or abs(observed_after - after) > .02:
        return None
    cost = horizon.get('estimated_training_cost_percent')
    if not _number(cost) or cost <= 0:
        return None
    from core.auto_trainer.larc_policy import attribute_options
    weights = ((row.get('attribute_plan') or {}).get('utility', {}).get('attribute_weights')
        or attribute_options(goal).get('attribute_weights', {}))
    repair = 0.
    for key in TRAINING_TYPES:
        target, current = goal.target_stats.get(key), state.current_stats.get(key)
        gain = candidate.stats_gain.get(key, 0)
        weight = weights.get(key, 1.)
        if not all(_number(value) for value in (target, current, gain, weight)) or target <= 0:
            return None
        repair += weight * min(max(0., gain), max(0., target - current)) / target
    failure = row.get('failure_rate')
    if not _number(failure):
        return None
    detail = {'kind': horizon.get('kind'), 'steps': steps,
        'dedicated_recoveries': _recovery_steps(route), 'failure_rate': failure,
        'after_vitality_percent': round(after, 4),
        'usable_heal_percent': round(max(0., after - state.vitality), 4),
        'target_repair': round(repair, 8),
        'estimated_training_cost_percent': cost}
    if unified_larc_enabled(goal):
        first, productive = _recovery_order(route)
        if domestic_increment_scope(state, goal):
            productive += route[:first].count('newly_ready_ss_estimate')
        detail.update(first_recovery_index=first,
                      productive_actions_before_recovery=productive)
    return detail


def _verified_axis_gains(candidate):
    from core.auto_trainer.larc_candidate_quality import numeric_evidence, _has_verified_zero_column
    if candidate is None:
        return None
    evidence = numeric_evidence(candidate)
    if evidence.get('usable') is not True:
        return None
    verified = evidence.get('verified_gains') or {}
    data = (candidate.raw_data or {}).get('stats_gain_ocr') or {}
    columns = data.get('columns', []) if isinstance(data, dict) else []
    gains = {}
    for axis in ('speed', 'stamina'):
        value = verified.get(axis)
        if _number(value) and value >= 0:
            gains[axis] = value
        elif _has_verified_zero_column(data, columns, axis):
            gains[axis] = 0
        else:
            return None
    return gains


def _r53_speed_stamina(state, goal, period, eligible, candidates):
    from core.auto_trainer.larc_calendar import period_ordinal
    from core.auto_trainer.larc_strategy_rules import effective_stamina_target
    day = period_ordinal(period)
    if not params_for_goal(goal).r53_speed_stamina_tiebreak_enabled or type(day) is not int or not 48 <= day <= 58 or len(eligible) < 2:
        return None
    speed, stamina = eligible[:2]
    if speed.get('training_type') != 'speed' or stamina.get('training_type') != 'stamina' or not _number(speed.get('score')) or not _number(stamina.get('score')):
        return None
    if (speed.get('score_workflow') or {}).get('priority_rank', 0) != (stamina.get('score_workflow') or {}).get('priority_rank', 0):
        return None
    if any(row.get('failure_rate') != 0 or (row.get('planning') or {}).get('training_vitality_basis') not in {'observed_bar_preview', 'observed'} for row in (speed, stamina)):
        return None
    before = _profile(state, goal, speed, candidates)
    after = _profile(state, goal, stamina, candidates)
    if before is None or after is None or (before['kind'], before['steps']) != (after['kind'], after['steps']) or after['after_vitality_percent'] < before['after_vitality_percent'] or after['dedicated_recoveries'] > before['dedicated_recoveries']:
        return None
    s_gain = _verified_axis_gains(candidates.get('speed'))
    t_gain = _verified_axis_gains(candidates.get('stamina'))
    raw_targets = getattr(goal, 'target_stats', {})
    if s_gain is None or t_gain is None or not all(_number(raw_targets.get(k)) and raw_targets[k] > 0 for k in ('speed','stamina')):
        return None
    targets = {'speed': float(raw_targets['speed']), 'stamina': effective_stamina_target(goal)}
    if targets['stamina'] <= 0:
        return None
    if (t_gain['stamina'] - s_gain['stamina']) / targets['stamina'] <= max(0., s_gain['speed'] - t_gain['speed']) / targets['speed']:
        return None
    plan = speed.get('attribute_plan') or {}
    other = stamina.get('attribute_plan') or {}
    shared = ('attributes', 'utility', 'raw_training_rates', 'opportunity_layouts', 'estimated_training_budget', 'future_income_credit')
    if any(k not in plan or plan.get(k) != other.get(k) for k in shared):
        return None
    projections = [row.get('projected_final_after_action') for row in (speed, stamina)]
    if any(not isinstance(v, dict) or not all(_number(v.get(k)) for k in ('speed','stamina')) for v in projections):
        return None
    deficits = [{k: max(0., targets[k]-v[k])/targets[k] for k in ('speed','stamina')} for v in projections]
    if not (max(deficits[1].values()) < max(deficits[0].values()) and deficits[1]['stamina'] > deficits[1]['speed'] and deficits[1]['stamina'] > 0):
        return None
    component = (speed.get('components') or {}).get('high_stat_target_scarcity')
    weight = (speed.get('weights') or {}).get('stats')
    if not _number(component) or component <= 0 or not _number(weight) or weight <= 0:
        return None
    from core.auto_trainer.larc_opportunity_forecast import marginal_gains
    try:
        isolated = marginal_gains(state.current_stats, {'speed': s_gain['speed']}, plan['attributes'],
                                  scarcity_factors=plan.get('scarcity_factors'), **plan['utility'])['scarcity'] * weight
    except (ValueError, TypeError, KeyError):
        return None
    if not _number(isolated) or isolated <= 0:
        return None
    ratio = min(1., deficits[1]['speed'] / deficits[1]['stamina'])
    discount = min(component, isolated) * (1. - ratio)
    if not stamina['score'] > speed['score'] - discount:
        return None
    return {'discount': discount, 'speed_only_scarcity': min(component, isolated),
        'speed_to_stamina_remaining_deficit_ratio': ratio, 'normalized_shortfalls': deficits,
        'speed_score': speed['score'], 'stamina_score': stamina['score'],
        'basis': 'same_current_page_conditional_forecast_score_comparison_no_new_gross_points'}


def apply_close_training_balance(state, goal, scored):
    if not unified_larc_enabled(goal):
        return
    from core.auto_trainer.larc_recovery import period_value
    period = period_value(state)
    year, month = period.get('year_index'), period.get('month')
    if not (year == 3 or year == 2 and type(month) is int and month >= 10):
        return
    candidate_scope = domestic_increment_scope(state, goal)
    if candidate_scope:
        from core.auto_trainer.larc_calendar import period_ordinal
    from core.auto_trainer.larc_timeline_workflow import selection_key
    eligible = sorted((row for row in scored if not is_hard_rejected(row)), key=selection_key)
    if not eligible:
        return
    best = eligible[0]
    if not best.get('training_type') or not _number(best.get('score')) or best['score'] <= 0:
        return
    candidates = {row.training_type: row for row in state.training_options}
    best_profile = _profile(state, goal, best, candidates)
    if best_profile is None:
        return
    diagnostic = _r53_speed_stamina(state, goal, period, eligible, candidates)
    if diagnostic is not None:
        challenger = eligible[1]
        ceiling = best['score']
        challenger.setdefault('planning', {})['r53_speed_premium_vs_stamina'] = diagnostic
        challenger.setdefault('components', {})['r53_bounded_training_tie_preference'] = round(ceiling - challenger['score'], 4)
        best.setdefault('components', {})['r53_bounded_training_tie_order'] = -.0001
        best['score'] = round(ceiling - .0001, 4)
        challenger['score'] = ceiling
        return
    rank = (best.get('score_workflow') or {}).get('priority_rank', 0)
    band_fraction = params_for_goal(goal).close_score_fraction
    band = best['score'] * band_fraction
    close = []
    for row in eligible:
        if not row.get('training_type') or best['score'] - row.get('score', -math.inf) > band:
            continue
        if (row.get('score_workflow') or {}).get('priority_rank', 0) != rank:
            continue
        detail = _profile(state, goal, row, candidates)
        if (detail is None or (detail['kind'], detail['steps']) !=
                (best_profile['kind'], best_profile['steps'])):
            continue
        if detail['failure_rate'] > best_profile['failure_rate']:
            continue
        row.setdefault('planning', {})['close_training_balance'] = {
            **detail, 'active': True, 'base_score': row['score'],
            'score_band_fraction': band_fraction,
            'basis': 'same energy horizon; risk then recovery turns then target repair; selection preference only'}
        if candidate_scope:
            row['planning']['close_training_balance'].update(
                candidate_id=CANDIDATE_ID,
                scope_calendar_ordinal=period_ordinal(period),
                basis='same energy horizon; risk then recovery turns then original score; selection preference only',
                decision_order='risk_then_dedicated_recoveries_then_original_score',
                target_repair_only_diagnostic=True,
                recovery_timing_only_diagnostic=True)
        if candidate_scope:
            row['planning'].setdefault('strategy181', {}).update(
                A={'enabled': True, 'effective_impl': 'risk_recovery_then_original_score',
                   'scope_calendar_ordinal': period_ordinal(period),
                   'target_repair_only_diagnostic': True,
                   'recovery_timing_only_diagnostic': True})
        close.append((row, detail))
    if len(close) < 2:
        return
    if candidate_scope:
        chosen, detail = min(close, key=lambda item: (
            item[1]['failure_rate'], item[1]['dedicated_recoveries'],
            -item[0]['score'], item[0]['action_id']))
    else:
        chosen, detail = min(close, key=lambda item: (
            item[1]['failure_rate'], item[1]['dedicated_recoveries'],
            -item[1]['first_recovery_index'],
            -item[1]['productive_actions_before_recovery'],
            -item[1]['target_repair'],
            -item[0]['score'], item[0]['action_id']))
    if candidate_scope:
        reason = ('same_risk_recovery_cost_keep_original_score' if chosen is best
                  else 'bounded_preference_for_lower_risk_or_fewer_dedicated_recoveries')
        chosen['planning']['close_training_balance'].update(
            selected=True, previous_best=best['action_id'], adjustment=0.0,
            training_group_score_ceiling=best['score'], candidate_reason=reason,
            game_gain_changed=False, energy_gain_recounted=False)
    if chosen is best:
        return
    base = chosen['score']
    ceiling = best['score']
    adjustment = round(ceiling - base, 4)
    # Keep the training group's original top value against SS/recovery.
    # Only the internal training tie order needs a tiny numerical nudge.
    for row, _ in close:
        if row is not chosen and row['score'] == ceiling:
            row.setdefault('components', {})['close_training_balance_tie_order'] = -.0001
            row['score'] = round(ceiling - .0001, 4)
    chosen.setdefault('components', {})['close_training_balance_preference'] = adjustment
    chosen['score'] = ceiling
    chosen['planning']['close_training_balance'].update(selected=True,
        previous_best=best['action_id'], adjustment=adjustment,
        training_group_score_ceiling=ceiling,
        game_gain_changed=False, energy_gain_recounted=False)
