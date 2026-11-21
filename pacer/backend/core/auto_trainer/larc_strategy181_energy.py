"""Strategy 1.8.1: sampled future wisdom uses the shared numeric value scale.

Derived from frozen n2; only the extra relative wisdom numeric cap is removed.
HP credit, one-use wisdom, action budget, risk and earned-SS handling remain.
Root gates the leaf to ordinary domestic 43..55 and must bind both parent and
resource paths consistently. The opt-in post-Arc leaf also values the
already-held SS through the shared numeric-only function, never a second action
selector. No environment mutation or hidden future evidence.
"""
from functools import lru_cache

from core.auto_trainer.larc_calendar import free_action_days, is_overseas, period_at, period_ordinal
from core.auto_trainer.larc_capacity import capacity_bounds, planning_capacity, recover_points
from core.auto_trainer.larc_recovery import (
    scheduled_recovery_after,
    recovery_value,
    ss_recovery,
    PLANNING_START_ENERGY,
    FUTURE_WISDOM_RECOVERY_CREDIT,
)
from core.auto_trainer.larc_recovery_routes import REST_SCENARIOS, FUTURE_DISCOUNT, terminal_energy_weight
from core.auto_trainer.larc_ss_energy import (
    _bound_full_ss,
    _known_mei,
    _layouts,
    _number,
    _observed_delta,
    _range,
    applicable,
)
from core.auto_trainer.larc_strategy_params import unified_larc_enabled

CANDIDATE_ID = 'larc_strategy181_domestic_ABC'
NEW_SS_ACTION = 'newly_ready_ss_estimate'


def _asset_values(asset):
    """Only accept finite nonnegative estimates; no default reward or cap."""
    if not isinstance(asset, dict) or asset.get('eligible') is not True:
        return False, 0., 0., False, 'no_eligible_asset'
    qualification = asset.get('qualification_only') is True
    if qualification:
        return True, 0., 0., True, 'qualification_only_zero_credit'
    numeric = asset.get('numeric_value')
    recovery = asset.get('recovery_points')
    if (not _number(numeric) or not _number(recovery)
            or numeric < 0 or recovery < 0):
        return False, 0., 0., False, 'invalid_or_missing_estimate'
    return True, float(numeric), float(recovery), False, 'root_validated_labelled_estimate'


def _evaluate_inventory_channel(state, goal, delta_percent, reference, *, asset=None,
        consumed_outing=False, consumed_held_ss=False, include_held_ss=True, price_held_numeric=True):
    """Return the numeric-parity baseline and success-only SS resource branch."""
    from core.auto_trainer.larc_recovery import estimated_training_cost
    from core.auto_trainer.larc_risk import risk_at_energy
    from core.auto_trainer.larc_ss_phase import phase_of,POST_FIRST_ARC_DOMESTIC,year1_full_comparison_status
    unified = unified_larc_enabled(goal)
    data_enabled = unified
    consistent = unified
    now = period_ordinal((state.scenario_state.get('larc_period')or{}).get('value')or{})
    dated=now is not None
    if dated:
        future = free_action_days(now + 1)[:2]
        calendar = [{'period': period_at(day), 'kind': 'overseas' if is_overseas(day) else 'domestic'} for day in future]
    else:
        future = [None, None]
        calendar = [{'period': {}, 'kind': 'domestic', 'relative_free_action': i + 1,
            'calendar_date_known': False} for i in range(2)]
    capacity = planning_capacity(state)
    before_points = min(100., max(0., state.vitality)) * capacity / 100
    cost_percent, cost_basis = estimated_training_cost(state)
    cost_points = cost_percent * capacity / 100
    energy_audit={'rows':[]}
    layouts, source_turns, reference_basis = _layouts(reference, state, cost_points, capacity,
        data_enabled=data_enabled,audit=energy_audit)
    preview = state.scenario_state.get('larc_ss_preview') or {}
    year1_full=year1_full_comparison_status(state,goal)['active']
    bound_held=(phase_of(state)==POST_FIRST_ARC_DOMESTIC or year1_full) and _bound_full_ss(state)
    reward = ss_recovery(preview)if not consistent or bound_held else{
        'observed_points':0,'rule_points':0,'max_increase':0}
    post_reward_rules = (phase_of(state) == POST_FIRST_ARC_DOMESTIC and bound_held and unified)
    rule_points_used = reward.get('rule_points', 0) if post_reward_rules else 0
    held_points = reward.get('observed_points', 0) + rule_points_used
    held_maximum = reward.get('max_increase', 0)
    held_numeric = 0.
    held_numeric_details = None
    if post_reward_rules and price_held_numeric:
        from core.auto_trainer.larc_ss_visible import ss_victory_evidence
        gains = preview.get('stats_gain') or {}
        # Match the current SS comparison's numeric eligibility. This adds no
        # new restriction to real actions and never invents a missing column.
        complete = all(type(gains.get(key)) is int and gains[key] >= 0
            for key in ('speed','stamina','power','guts','wisdom','skill_pt'))
        if ss_victory_evidence(preview)['guaranteed'] and complete:
            from core.auto_trainer.larc_training_value import gain_value
            direct = gain_value(state, gains, goal, project=False)
            if _number(direct['score']):
                held_numeric = direct['score']
                held_numeric_details = {'score': held_numeric, 'components': direct['components'],
                    'unknown_fields': direct['unknown_fields'],
                    'basis': 'current_bound_full_SS_numeric_preview_as_short_horizon_reference',
                    'future_roster_known': False, 'future_execution_guaranteed': False,
                    'current_reward_credited_again_when_consumed': False}
    held_available = include_held_ss and (held_points > 0 or held_maximum > 0 or held_numeric > 0)
    if consumed_held_ss:
        after_points, after_capacity = recover_points(before_points, capacity, held_points, held_maximum)
    else:
        after_points = min(capacity, max(0., before_points + delta_percent * capacity / 100))
        after_capacity = capacity
    outings = _known_mei(state)
    reserve = PLANNING_START_ENERGY[getattr(goal, 'risk_tolerance', 'low')]
    from core.auto_trainer.larc_opening import LarcOpeningStrategy
    failure_limit = LarcOpeningStrategy.failure_limit(goal, state)
    window = {'kind': 'rolling_recovery', 'steps': len(future), 'action_calendar': calendar}
    terminal = terminal_energy_weight(state, goal, window) if future else 0.
    terminal_usable=bool(future and free_action_days(future[-1]+1))if dated else None
    if consistent and terminal_usable is False:
        # The inherited overseas coefficient returns before its calendar-end
        # check. Carry no HP beyond the last actual free action; no retuning.
        terminal=0.

    def fixed_heals(start, end):
        return sum(scheduled_recovery_after(state, period_at(day)) * capacity / 100
            for day in range(start, end))

    initial_fixed = fixed_heals(now, future[0]) if dated and future else 0.
    post_fixed = ([fixed_heals(day, (future[i + 1] if i + 1 < len(future) else day + 1))
        for i, day in enumerate(future)]if dated else[0.,0.])

    asset_ok, asset_numeric, asset_recovery, qualification, asset_reason = _asset_values(asset)
    asset_eligible = asset_ok
    if asset_eligible and not dated:
        asset_eligible = False
        asset_reason = 'new_asset_calendar_unknown_baseline_retained'
    elif asset_eligible and (is_overseas(now) or consumed_outing or consumed_held_ss):
        asset_eligible = False
        asset_reason = 'new_asset_not_earned_by_current_domestic_training'
    elif asset_eligible and not any(not is_overseas(day) for day in future):
        asset_eligible = False
        asset_reason = 'no_future_domestic_free_action'

    def key(points): return round(max(0., points), 6)

    @lru_cache(maxsize=None)
    def solve(index, points, maximum, outing_index, ss_left, wisdom_left, new_ss_left=False):
        if index == len(future):
            common_bar = points * 100 / capacity
            return terminal * (2 * common_bar - .008 * common_bar * common_bar), ()
        domestic = not is_overseas(future[index])if dated else True

        def following(action, change, *, next_maximum=maximum, outing=outing_index,
                ss=ss_left, wisdom=wisdom_left, new_ss=new_ss_left):
            next_points = min(next_maximum, max(0., points + change))
            next_points = min(next_maximum, next_points + post_fixed[index])
            value, path = solve(index + 1, key(next_points), next_maximum, outing, ss, wisdom, new_ss)
            return FUTURE_DISCOUNT * value, (action,) + path

        common = []
        mei_available = domestic and outing_index < len(outings)
        if not mei_available:
            scenarios = REST_SCENARIOS if domestic else ((50., 1.),)
            rests = [(weight, following('rest', amount)) for amount, weight in scenarios]
            common.append((sum(weight * result[0] for weight, result in rests), rests[len(rests) // 2][1][1]))
        if mei_available:
            common.append(following('outing:' + str(outings[outing_index][0]), outings[outing_index][1],
                outing=outing_index + 1))
        if domestic and ss_left:
            healed, new_maximum = recover_points(points, maximum, held_points, held_maximum)
            next_value, path = following('held_ss_recovery', healed - points, next_maximum=new_maximum, ss=False)
            # The already-held SS is one future main action. Its numeric reward
            # is removed with ss_left after use, including when consumed now.
            common.append((held_numeric + next_value, path))
        values = []
        energy = points * 100 / maximum
        shortfall = min(1., max(0., (reserve - energy) / max(1., reserve)))
        readiness = 1. - {'low': .45, 'medium': .35, 'high': .25}[getattr(goal, 'risk_tolerance', 'low')] * shortfall
        for layout in layouts:
            choices = list(common)
            train_cap = max((row['numeric'] for row in layout if row['tab'] != 'wisdom'), default=0.)
            for row in layout:
                risk = risk_at_energy(row['numeric'], row['risk_metadata'], energy)
                if risk['failure_rate'] is not None and risk['failure_rate'] > failure_limit:
                    continue
                if row['tab'] == 'wisdom':
                    if not wisdom_left or row['wisdom_points'] <= 0:
                        continue
                    value, path = following('wisdom', row['wisdom_points'], wisdom=False)
                    # Same sampled layout and shared forecast discount as other tabs.
                    # Keep HP credit / one-use wisdom / risk unchanged.
                    numeric = risk['value']
                else:
                    if points < row['cost_points']:
                        continue
                    value, path = following('train:' + str(row['tab']),row.get('training_delta_points',-row['cost_points']))
                    numeric = risk['value']
                choices.append((numeric * readiness + value, path))
            # Appending after every original choice preserves the original
            # path on exact ties. This is one future main action, not a bonus
            # training slot; recovery clips at the existing maximum only.
            if domestic and new_ss_left:
                value, path = following(NEW_SS_ACTION, asset_recovery, new_ss=False)
                choices.append((asset_numeric + value, path))
            values.append(max(choices, key=lambda item: item[0]))
        representative = sorted(values, key=lambda item: item[0])[len(values) // 2]
        return sum(item[0] for item in values) / len(values), representative[1]

    try:
        before_value, before_route = solve(0, key(min(capacity, before_points + initial_fixed)), capacity,
            0, held_available, True)
        after_value, after_route = solve(0, key(min(after_capacity, after_points + initial_fixed)), after_capacity,
            int(bool(consumed_outing)), held_available and not consumed_held_ss, True)
        resource_after_value, resource_after_route = (after_value, after_route)
        if asset_eligible:
            resource_after_value, resource_after_route = solve(
                0, key(min(after_capacity, after_points + initial_fixed)), after_capacity,
                int(bool(consumed_outing)), held_available and not consumed_held_ss, True, True)
            if resource_after_value <= after_value:
                resource_after_value, resource_after_route = after_value, after_route
    finally:
        solve.cache_clear()
    delta_value = after_value - before_value
    paid = recovery_value(state, delta_percent, include_held_ss=include_held_ss and not consumed_held_ss)
    from core.auto_trainer.larc_strategy_extension import active_provider
    from core.auto_trainer.larc_strategy_params import params_for_goal
    short_support = None
    if future and after_capacity and active_provider().short_window_energy_support_enabled(params_for_goal(goal)):
        buffer_pct = (initial_fixed + (post_fixed[0] if post_fixed else 0.0)) * 100.0 / after_capacity
        after_pct = after_points * 100.0 / after_capacity
        short_support = round(min(float(len(future)), max(0.0, (after_pct + buffer_pct - reserve) / max(float(cost_percent), 1.0) + 1.0)), 4)
    baseline = {'active': True, 'kind': 'symmetric_short_energy_only', 'basis': 'symmetric_short_energy_only',
        'score': round(delta_value - paid, 4), 'continuation_value_delta': round(delta_value, 4),
        'already_counted_recovery_value': paid, 'future_value_before': round(before_value, 4),
        'future_value_after': round(after_value, 4), 'future_route_before': list(before_route),
        'future_route_after': list(after_route), 'steps': len(future), 'future_trainings': len(future),
        **({'estimated_trainings_supported': short_support} if short_support is not None else {}),
        'free_actions_including_current': 1 + len(future), 'action_calendar': calendar,
        'domestic_steps': sum(not is_overseas(day) for day in future)if dated else len(future),
        **({'calendar_basis':'undated_first_year_domestic_two_action_estimate',
            'calendar_date_known':False,'specific_fixed_events_modelled':False,
            'unknown_fields':['future_calendar_dates','intervening_race_or_event_recovery',
                'exact_free_actions_remaining_after_window']}if not dated else{}),
        'planning_capacity': capacity, 'capacity_bounds': list(capacity_bounds(state)),
        'after_energy_points': after_points, 'after_capacity_points': after_capacity,
        'after_vitality_percent': round(after_points * 100 / after_capacity, 4),
        'estimated_training_cost_percent': cost_percent, 'training_cost_basis': cost_basis,
        'held_ss_consumed_now': bool(consumed_held_ss), 'held_ss_reward':
            {'points': held_points, 'max_increase': held_maximum}, 'held_ss_numeric_value': held_numeric,
        'held_ss_attribute_Pt_fan_credit': held_numeric,
        **({'held_ss_numeric_reference':held_numeric_details, 'held_ss_fan_credit': 0.,
            'valuation_contents':['future_training_numeric','HP','existing_held_SS_numeric'],
            'held_SS_numeric_timing_is_not_HP_consumption':True,
            'legacy_energy_channel_label_retained':True} if held_numeric_details is not None else {}),
        'generated_ss_modelled': False,
        **({'held_ss_future_recovery_basis':'current_observed_group_once_as_planning_estimate',
            'next_ss_roster_known':False,'member_inventory_modelled':False,
            'future_recovery_is_execution_guarantee':False}if year1_full else{}),
        'stateful_statistics_revaluation': False,
        'current_ss_rule_heal_not_credited': reward.get('rule_points', 0) - rule_points_used,
        **({'held_ss_rule_recovery_forecast': {
            'observed_points': reward.get('observed_points', 0),
            'rule_points_used': rule_points_used,
            'same_request_for_immediate_and_held': True,
            'clip_at_each_use_capacity': True,
            'earned_or_execution_confirmed': False}} if post_reward_rules else {}),
        'known_outing_stages': [stage for stage, _ in outings], 'current_outing_consumed': bool(consumed_outing),
        'historical_source_turns': source_turns, 'future_numeric_reference_basis': reference_basis,
        'future_wisdom_max_credited_turns': 1, 'future_risk_basis': 'existing_sampled_rate_only_at_observed_energy',
        'initial_fixed_recovery_points': initial_fixed, 'fixed_recovery_after_actions_points': post_fixed,
        'terminal_energy_weight': terminal, 'future_value_reference': reference,
        **({'historical_energy_data':{**energy_audit,
            'requested_used':sum(r['basis']=='real_requested_delta_points'for r in energy_audit['rows']),
            'unclipped_applied_used':sum(r['basis']=='real_unclipped_applied_delta_points'for r in energy_audit['rows']),
            'fallback_rows':sum(r['basis']=='named_policy_fallback'for r in energy_audit['rows'])},
            'held_ss_current_bound_full':bound_held,
            'terminal_energy_has_later_free_action':terminal_usable,
            'phase_scope':('first_year_bound_full_current_comparison'if year1_full else
                'post_first_arc_all_free_actions'if consistent else'post_domestic_full5_data_only')}
           if data_enabled else {}),
        'no_extra_main_action_budget': True, 'representative_path_not_committed_plan': True}

    baseline['strategy181_C'] = {
        'enabled': True, 'effective_impl': 'shared_future_wisdom_numeric_scale',
        'scope_calendar_ordinal': now, 'future_wisdom_max_credited_turns': 1,
        'wisdom_HP_credit': FUTURE_WISDOM_RECOVERY_CREDIT,
        'future_actions_budget': len(future),
    }
    return {
        'candidate_id': CANDIDATE_ID,
        'future_wisdom_numeric_policy': 'shared_forecast_discount_no_nonwisdom_relative_cap',
        'baseline': baseline,
        'resource_after_value': round(resource_after_value, 4),
        'resource_after_route': list(resource_after_route),
        'success_resource_delta': round(max(0., resource_after_value - after_value), 4),
        'asset_evaluation': {
            'eligible_input': asset_ok,
            'eligible_in_window': asset_eligible,
            'reason': asset_reason,
            'qualification_only': qualification,
            'numeric_value': asset_numeric if asset_eligible else 0.,
            'recovery_points': asset_recovery if asset_eligible else 0.,
            'maximum_increase': 0.,
            'AP_credit': 0., 'fan_credit': 0.,
            'success_only': True,
            'before_has_new_asset': False,
            'new_groups_available': int(asset_eligible),
            'resource_used': NEW_SS_ACTION in resource_after_route,
            'resource_use_count': resource_after_route.count(NEW_SS_ACTION),
            'future_actions_budget': len(future),
            'failure_mixing_or_progress_proxy_replacement_applied': False,
            'future_outcome_or_recovery_guaranteed': False,
            'reference_layout_or_fixed_recovery_changed': False,
        },
    }


def evaluate_energy_bridge(state, goal, delta_percent, reference, *, asset=None,
        consumed_outing=False, consumed_held_ss=False, include_held_ss=True):
    """Blend a new inventory timing model with the existing HP model.

    The short inventory model omits regeneration of later SS groups. Its value
    is a partial decision correction, not a game probability or half a reward.
    Current/held recovery facts and current SS numeric rewards stay unchanged.
    """
    from copy import deepcopy
    kwargs = dict(asset=asset, consumed_outing=consumed_outing,
        consumed_held_ss=consumed_held_ss, include_held_ss=include_held_ss)
    full = _evaluate_inventory_channel(state, goal, delta_percent, reference,
        price_held_numeric=True, **kwargs)
    fb = full['baseline']
    if fb.get('held_ss_numeric_reference') is None:
        return full
    stock = None
    if unified_larc_enabled(goal):
        from core.auto_trainer.larc_held_ss_stock import held_ss_stock
        stock = held_ss_stock(state)
    hp = _evaluate_inventory_channel(state, goal, delta_percent, reference,
        price_held_numeric=False, **kwargs)
    hb = hp['baseline']
    # Partial spare charge is not yet another executable group. Change the
    # single-group model credit only when a complete spare group is known.
    additional_group = stock is not None and stock['extra_ready_lower_bound'] >= 5
    weight = 0. if additional_group else .5
    result = deepcopy(full)
    b = result['baseline']
    for key in ('future_value_before','future_value_after'):
        b[key] = round(weight * fb[key] + (1 - weight) * hb[key], 4)
    b['continuation_value_delta'] = round(b['future_value_after'] - b['future_value_before'], 4)
    b['score'] = round(b['continuation_value_delta'] - b['already_counted_recovery_value'], 4)
    result['resource_after_value'] = round(weight * full['resource_after_value']
        + (1 - weight) * hp['resource_after_value'], 4)
    result['success_resource_delta'] = round(max(0., result['resource_after_value']
        - b['future_value_after']), 4)
    b['held_inventory_timing_mix'] = {
        'weight': weight, 'is_game_probability': False, 'actual_reward_scaled': False,
        'reason': 'partial_credit_for_short_model_without_next_group_regeneration',
        **({
            'stock_timing': stock, 'original_weight': .5,
            'stock_timing_variant': 'complete_spare_group_r2',
            'confirmed_additional_group': additional_group,
        } if stock is not None else {}),
        'HP_only_score': hb['score'], 'inventory_model_score': fb['score'],
        **({
            'HP_only_future_values': {key: hb[key] for key in ('future_value_before', 'future_value_after')},
            'inventory_model_future_values': {key: fb[key] for key in ('future_value_before', 'future_value_after')},
            'HP_only_resource_after_value': hp['resource_after_value'],
            'inventory_model_resource_after_value': full['resource_after_value'],
            'already_counted_recovery_value': b['already_counted_recovery_value'],
        } if stock is not None else {}),
        'HP_only_route_before': hb['future_route_before'],
        'HP_only_route_after': hb['future_route_after'],
        'inventory_route_before': fb['future_route_before'],
        'inventory_route_after': fb['future_route_after'],
        'timing_adjustment_to_HP_only': round(b['score'] - hb['score'], 4),
        'route_fields_are_inventory_model_representatives_not_mixed_execution_plans': True,
    }
    return result
