"""One post-score resource alternative; no executor, device or model inputs."""
from core.auto_trainer.larc_strategy181_asset import (
    earned_qualification, estimate_asset, number, replacement_credit,
)
from core.auto_trainer.larc_ss_energy import domestic_increment_scope as enabled


def apply_training_increment(state, candidate, goal, valuation, *, delta_percent, gain_value):
    """Keep the inherited score on missing coverage, never block training."""
    if not enabled(state, goal):
        return valuation
    from core.auto_trainer.larc_calendar import period_ordinal
    from core.auto_trainer.larc_recovery import period_value
    day = period_ordinal(period_value(state))
    planning = valuation.setdefault('planning', {})
    horizon = planning.get('energy_horizon') or {}
    diagnostic = planning.setdefault('strategy181', {})
    diagnostic.update(strategy_id='1.8.1', algorithm_build='larc_strategy181_domestic_ABC',
        scope_calendar_ordinal=day, C=horizon.get('strategy181_C') or {
            'enabled': False, 'effective_impl': 'parent_1.8',
            'reason': 'outside_existing_short_energy_channel'})
    qualification = earned_qualification(state, candidate, valuation, calendar=day)
    report = {'enabled': True, 'applied': False, 'effective_impl': 'parent_1.8',
              'qualification': qualification, 'score_credit': 0.,
              'reason': qualification['reason']}
    diagnostic['B'] = report
    if not qualification['eligible']:
        return valuation
    if (horizon.get('active') is not True or
            horizon.get('kind') != 'symmetric_short_energy_only' or
            horizon.get('strategy181_C', {}).get('enabled') is not True):
        report['reason'] = 'outside_shared_1_8_1_short_energy_channel'
        return valuation
    from core.auto_trainer.larc_strategy181_observation import asset_inputs
    inputs = asset_inputs(state, candidate)
    report['observation'] = inputs
    if inputs.get('available') is not True:
        report['reason'] = inputs.get('reason', 'production_asset_inputs_unavailable')
        return valuation
    reference = horizon.get('future_value_reference') or {}
    asset = estimate_asset(state, goal, inputs.get('prior') or {},
        inputs.get('current_public') or {}, reference, gain_value)
    report['asset_estimate'] = asset
    if not asset['available']:
        report['reason'] = asset['reason']
        return valuation
    if not number(delta_percent):
        report['reason'] = 'original_current_HP_input_unavailable'
        return valuation
    from core.auto_trainer.larc_strategy181_energy import evaluate_energy_bridge
    bridge = evaluate_energy_bridge(state, goal, delta_percent, reference,
        asset={**asset, 'eligible': True})
    report['baseline_parity'] = bridge['baseline'] == horizon
    if not report['baseline_parity']:
        report['reason'] = 'short_energy_baseline_parity_mismatch'
        return valuation
    credit = replacement_credit(valuation['components'], bridge['success_resource_delta'],
        candidate.failure_rate)
    report.update(replacement=credit, score_credit=credit['credit'],
        reason=credit['reason'], success_resource_delta=bridge['success_resource_delta'],
        resource_after_route=bridge['resource_after_route'],
        asset_evaluation=bridge['asset_evaluation'],
        effective_impl='strict_past_earned_SS_resource_alternative')
    if credit['credit'] <= 0:
        return valuation
    valuation['components']['earned_ss_resource_replacement'] = credit['credit']
    valuation['score'] = round(sum(valuation['components'].values()), 4)
    report['applied'] = True
    horizon.update(baseline_kind=horizon['kind'],
        earned_ss_credit_in_separate_component=True,
        baseline_future_route_after=horizon['future_route_after'],
        future_route_after=bridge['resource_after_route'],
        earned_ss_success_resource_value_after=bridge['resource_after_value'],
        earned_ss_resource_replacement_credit=credit['credit'],
        earned_ss_success_probability=qualification['current_training_success_probability'],
        earned_ss_future_win_probability=None, earned_ss_route_is_policy_estimate=True)
    return valuation
