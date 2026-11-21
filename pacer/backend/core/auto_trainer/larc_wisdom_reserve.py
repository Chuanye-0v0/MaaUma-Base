"""Bounded pressure to use good observed wisdom opportunities before time runs out.

This prices a selection preference, not a promised future rainbow or a fixed
number of wisdom actions. It does not change game gains or the route budget.
"""
import math
from statistics import median


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _overseas(period):
    return period.get('year_index') in {2, 3} and period.get('month') in {7, 8, 9}


def _current_ss_repair(state, period, gap):
    """A currently observed guaranteed full SS is an alternative, not free gain."""
    if _overseas(period):
        return 0., 'ss_unavailable_overseas'
    preview = state.scenario_state.get('larc_ss_preview')
    if (not isinstance(preview, dict) or preview.get('status') != 'accepted'
            or preview.get('stage') != 'preview' or preview.get('ready_count') != 5):
        return 0., 'current_full_ss_unknown'
    from core.auto_trainer.larc_ss_visible import visible_ss_participants, ss_victory_evidence
    if len(visible_ss_participants(preview)) != 5 or not ss_victory_evidence(preview)['guaranteed']:
        return 0., 'current_ss_not_guaranteed'
    from core.auto_trainer.state import STAT_KEYS
    gains = preview.get('stats_gain')
    if (not isinstance(gains, dict)
            or any(type(gains.get(key)) is not int or gains[key] < 0 for key in STAT_KEYS)):
        # Match choose_larc_ss's minimum numeric contract. A guaranteed win
        # alone does not make a partially unread reward panel executable.
        return 0., 'current_ss_numeric_incomplete'
    return min(gap, gains['wisdom']), 'observed_guaranteed_full_ss'


def wisdom_reserve_value(state, candidate, goal, plan, *, numeric_value):
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    if not unified_larc_enabled(goal):
        return {'active': False, 'status': 'legacy_revision', 'score': 0.}
    result = {'active': False, 'score': 0.,
        'basis': 'same-run observed wisdom layouts and shared remaining budget; not a future proc probability',
        'real_gains_changed': False, 'fixed_training_quota': False}
    def unavailable(reason):
        return {**result, 'status': reason}
    from core.auto_trainer.larc_recovery import period_value
    period_record = state.scenario_state.get('larc_period') or {}
    if not isinstance(period_record, dict) or period_record.get('status') != 'verified':
        return unavailable('period_unknown')
    period = period_value(state)
    year, month = period.get('year_index'), period.get('month')
    if not (year == 3 or year == 2 and type(month) is int and month >= 10):
        return unavailable('before_post_first_arc')
    current, target = state.current_stats.get('wisdom'), goal.target_stats.get('wisdom')
    if not _number(current) or not _number(target) or target <= 0:
        return unavailable('wisdom_state_unknown')
    if current >= target:
        return unavailable('wisdom_target_satisfied')
    attribute = plan.get('attributes', {}).get('wisdom', {})
    gap = attribute.get('active_gap')
    budget = plan.get('estimated_training_budget')
    if not _number(gap) or not _number(budget) or budget <= 1:
        return unavailable('remaining_budget_or_rewards_unknown')
    gap = min(max(0., target - current), max(0., gap))
    if gap <= 0:
        return unavailable('known_rewards_cover_wisdom')
    ss_repair, ss_status = _current_ss_repair(state, period, gap)
    result.update(alternative_ss_wisdom_repair=ss_repair, alternative_ss_status=ss_status)
    from core.auto_trainer.larc_attribute_plan import _inputs
    inputs, unknown = _inputs(state)
    if unknown:
        return unavailable('wisdom_history_unbound')
    from core.auto_trainer.larc_opportunity_forecast import display_gain
    seen, samples = set(), []
    history = inputs.get('training_history')
    if not isinstance(history, list):
        return unavailable('insufficient_wisdom_history')
    for sample in reversed(history):
        if not isinstance(sample, dict):
            continue
        turn, previous = sample.get('turn_index'), sample.get('period') or {}
        if not isinstance(previous, dict):
            continue
        if (type(turn) is not int or turn in seen or type(state.turn) is not int
                or not 0 < state.turn - turn <= 12 or previous.get('year_index') != year
                or _overseas(previous) != _overseas(period)):
            continue
        raw_rows = sample.get('raw_gains') or {}
        wisdom_row = raw_rows.get('wisdom') if isinstance(raw_rows, dict) else None
        raw = wisdom_row.get('wisdom') if isinstance(wisdom_row, dict) else None
        if not _number(raw) or raw <= 0:
            continue
        seen.add(turn)
        samples.append((turn, display_gain(current, raw)))
    if len(samples) < 2:
        return unavailable('insufficient_wisdom_history')
    reference = median(value for _, value in samples)
    if reference <= 0:
        return unavailable('wisdom_reference_unknown')
    from core.auto_trainer.larc_policy import attribute_options
    weights = (plan.get('utility', {}).get('attribute_weights')
        or attribute_options(goal).get('attribute_weights', {}))
    importance = weights.get('wisdom', 1.)
    if not _number(importance) or importance <= 0:
        return unavailable('wisdom_importance_unknown')
    need = gap / reference
    pressure = need / (need + budget)
    result.update(active=True, status='planning_only', known_gap=round(gap, 4),
        reference_gain=round(reference, 4), history_samples=len(samples),
        sample_turns=sorted(turn for turn, _ in samples),
        reference_basis='median observed raw wisdom gains mapped to current display segment',
        remaining_training_budget=budget, remaining_free_actions=plan.get('remaining_free_actions'),
        needed_trainings=round(need, 4), needed_trainings_ceil=math.ceil(need),
        reference_can_fit_shared_budget=need <= budget,
        pressure=round(pressure, 4), attribute_importance=importance)
    if candidate.training_type != 'wisdom':
        return result
    gain = candidate.stats_gain.get('wisdom')
    if not _number(gain) or gain <= 0:
        return {**result, 'status': 'current_wisdom_unknown'}
    from core.auto_trainer.larc_candidate_quality import usable_numeric_evidence
    if not usable_numeric_evidence(candidate):
        return {**result, 'status': 'current_wisdom_unverified'}
    if candidate.failure_rate != 0:
        return {**result, 'status': 'risky_wisdom_no_extra_preference'}
    quality = min(1., gain / reference)
    result['current_quality'] = round(quality, 4)
    if quality < .8:
        return {**result, 'status': 'weak_current_wisdom'}
    if not _number(numeric_value) or numeric_value <= 0:
        return {**result, 'status': 'no_positive_numeric_value'}
    effective = min(gain, gap)
    incremental = max(0., effective - ss_repair)
    result.update(incremental_wisdom_repair=incremental)
    if incremental <= 0:
        return {**result, 'status': 'current_full_ss_covers_same_wisdom_repair'}
    from core.auto_trainer.larc_strategy_extension import active_provider
    from core.auto_trainer.larc_strategy_params import params_for_goal
    params = params_for_goal(goal)
    wisdom_cards = 0
    try:
        from core.auto_trainer.larc_adaptation_strategy import deck_training_coverage
        coverage = deck_training_coverage(state)
        if coverage.get('complete') is True or (coverage.get('known_slots') or 0) >= 5:
            wisdom_cards = int((coverage.get('counts') or {}).get('wisdom', 0) or 0)
        else:
            raw_deck = getattr(goal, '_support_deck_types', None) or ()
            wisdom_cards = sum(1 for t in raw_deck if str(t).strip().lower() in {'wisdom', '智力'})
    except Exception:
        raw_deck = getattr(goal, '_support_deck_types', None) or ()
        wisdom_cards = sum(1 for t in raw_deck if str(t).strip().lower() in {'wisdom', '智力'})
    scale = float(active_provider().wisdom_reserve_scale(params, state, goal, wisdom_cards))
    base_preference = min(numeric_value * .15, incremental * importance * pressure * .5)
    preference = base_preference * scale
    result.update(status='good_observed_wisdom_opportunity',
                  score=round(max(0., preference), 4),
                  wisdom_reserve_scale=round(scale, 4),
                  wisdom_cards_detected=wisdom_cards,
                  effective_wisdom_gain=effective, numeric_preference_cap=round(numeric_value * .15, 4))
    return result
