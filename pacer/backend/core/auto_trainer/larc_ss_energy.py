"""Symmetric two-action energy channel, without SS numeric inventory resale."""
from functools import lru_cache
import math

from core.auto_trainer.larc_calendar import free_action_days, is_overseas, period_at, period_ordinal
from core.auto_trainer.larc_capacity import capacity_bounds, planning_capacity, recover_points
from core.auto_trainer.larc_recovery import (period_value, scheduled_recovery_after, recovery_value,
    ss_recovery, PLANNING_START_ENERGY, FUTURE_WISDOM_RECOVERY_CREDIT, FUTURE_WISDOM_VALUE_CREDIT)
from core.auto_trainer.larc_recovery_routes import REST_SCENARIOS, FUTURE_DISCOUNT, terminal_energy_weight


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def domestic_increment_scope(state, goal=None):
    """Ordinary post-first-Arc domestic free-action window (d43..55) for unified L'Arc."""
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled

    if not unified_larc_enabled(goal):
        return False
    day = period_ordinal(period_value(state))
    return type(day) is int and 43 <= day <= 55 and not is_overseas(day)


def applicable(state, goal):
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    from core.auto_trainer.larc_ss_phase import (
        post_first_arc_free_action,
        year1_full_comparison_status,
    )
    if year1_full_comparison_status(state, goal)['active']:
        return True
    if unified_larc_enabled(goal):
        return post_first_arc_free_action(state)
    return False


def _bound_full_ss(state):
    preview = state.scenario_state.get('larc_ss_preview') or {}
    source = preview.get('source') or (preview.get('participants_observation') or {}).get('source') or {}
    captures = [(getattr(c, 'raw_data', {}) or {}).get('capture') or {}
        for c in getattr(state, 'training_options', ())]
    return (preview.get('status') == 'accepted' and preview.get('stage') == 'preview'
        and preview.get('kind') in {'SS', 'SSS'} and preview.get('ready_count') == 5
        and len(captures) == 5 and bool(source.get('run_session_id'))
        and source.get('turn_index') == state.turn and all(c.get('run_session_id') == source['run_session_id']
            and c.get('turn_index') == state.turn for c in captures))


def _known_mei(state):
    from core.auto_trainer.larc_friend_outing import bound_outing_packet, bound_outing_inputs, outing_effects
    packet = bound_outing_packet(state)
    if not packet:
        return ()
    bonus = bound_outing_inputs(packet) or {}
    return tuple((stage, float(outing_effects(stage, recovery_bonus=bonus.get('recovery'),
        event_bonus=bonus.get('event'))['vitality_points'][0])) for stage in range(packet['completed'] + 1, 6))


def _range(value):
    return (tuple(value)if isinstance(value,(list,tuple))and len(value)==2
        and all(_number(v)for v in value)and -120<=value[0]<=value[1]<=120 else None)


def _observed_delta(metadata,state):
    """Consume real point bounds, never reinterpret clipped zero as a request."""
    observation=metadata.get('energy_observation')or{}
    if not isinstance(observation,dict):return None,'energy_observation_unavailable'
    turn=metadata.get('source_turn_index');source=observation.get('source')or{}
    runs={(getattr(c,'raw_data',{})or{}).get('capture',{}).get('run_session_id')
        for c in getattr(state,'training_options',())}
    runs.discard(None)
    valid=(observation.get('schema_version')=='larc.training_energy_observation.v1'
        and type(observation.get('version'))is int and observation['version']==1 and observation.get('status')=='accepted'
        and observation.get('unit')=='points'and len(runs)==1
        and source.get('run_session_id')in runs and source.get('turn_index')==turn
        and observation.get('source_turn_index')==turn)
    if not valid:return None,'energy_observation_unavailable_or_wrong_scope'
    requested=_range(observation.get('requested_delta_points'))
    applied=_range(observation.get('applied_delta_points'))
    if requested is not None:
        bounds,basis=requested,'real_requested_delta_points'
    elif applied is not None and observation.get('clipped')is False:
        bounds,basis=applied,'real_unclipped_applied_delta_points'
    else:
        return None,'clipped_or_unknown_request_no_uncapped_delta'
    # Choose the conservative physical bound, label it as such, and retain
    # the whole original range. Exact public amounts remain exact, no midpoint.
    return {'delta':bounds[0],'bounds':list(bounds),'basis':basis,
        'bound_selection':'exact'if bounds[0]==bounds[1]else'conservative_lower_energy_bound',
        'source_kind':observation.get('source_kind')},None


def _layouts(reference, state, cost_points, initial_capacity, *, data_enabled=False,audit=None):
    # The current visible rainbow is a current opportunity, not a forecast.
    # A finite historical wisdom sample can be used once, never on each step.
    layouts = []
    turns = set()
    for layout in reference.get('opportunity_layouts') or []:
        if not isinstance(layout,(list,tuple)):
            continue
        historical = []
        for row in layout:
            if not isinstance(row,dict)or row.get('tab')not in {'speed','stamina','power','guts','wisdom'}:
                continue
            metadata = row.get('forecast_metadata') or {}
            turn = metadata.get('source_turn_index')
            if (type(turn) is not int or turn >= state.turn or not _number(row.get('numeric'))
                    or row['numeric'] < 0):
                continue
            turns.add(turn)
            wisdom = row.get('tab') == 'wisdom'
            delta = metadata.get('net_vitality_percent')
            heal = (max(0., delta) * initial_capacity / 100 if _number(delta) else 5.)
            model={'tab': row.get('tab'), 'numeric': float(row['numeric']),
                'cost_points': cost_points, 'wisdom_points': heal * FUTURE_WISDOM_RECOVERY_CREDIT,
                'risk_metadata': row.get('risk_metadata') or metadata.get('risk_metadata') or {}}
            if data_enabled:
                observed,reason=_observed_delta(metadata,state)
                if observed is not None:
                    chosen=observed['delta'];model['cost_points']=max(0.,-chosen)
                    model['training_delta_points']=chosen
                    model['wisdom_points']=max(0.,chosen)*FUTURE_WISDOM_RECOVERY_CREDIT
                    evidence={'source_turn_index':turn,'tab':row['tab'],**observed}
                else:
                    # A clipped/unknown net0 is not a real request0. Retain
                    # the named old policy fallback instead of inventing 10.
                    model['wisdom_points']=5.*FUTURE_WISDOM_RECOVERY_CREDIT
                    evidence={'source_turn_index':turn,'tab':row['tab'],'basis':'named_policy_fallback',
                        'reason':reason,'delta':None,'bounds':None}
                evidence.update(cost_points=model['cost_points'],credited_wisdom_points=model['wisdom_points']if wisdom else 0.)
                if audit is not None:audit.setdefault('rows',[]).append(evidence)
            historical.append(model)
        if any(r['tab'] != 'wisdom' for r in historical):
            layouts.append(tuple(historical))
    if layouts:
        return tuple(layouts), sorted(turns), 'historical_numeric_only_no_current_layout'
    value = reference.get('numeric_training')
    value = max(0., value) if _number(value) else 0.
    return (({'tab': 'training', 'numeric': value, 'cost_points': cost_points,
        'wisdom_points': 0., 'risk_metadata': {}},),), [], 'generic_numeric_reference_no_future_wisdom'


def short_energy_continuity(state, goal, delta_percent, reference, *, consumed_outing=False,
        consumed_held_ss=False, include_held_ss=True):
    """Only energy/resources can differ across before/after, not future stats.

    Numeric training is the shared opportunity cost of spending a scarce main
    action on recovery. Held/generated SS and Mei attributes/Pt/fans are never
    future yield in this channel. This function changes no state or receipt.
    """
    if domestic_increment_scope(state, goal):
        from core.auto_trainer.larc_strategy181_energy import evaluate_energy_bridge
        return evaluate_energy_bridge(state, goal, delta_percent, reference,
            consumed_outing=consumed_outing, consumed_held_ss=consumed_held_ss,
            include_held_ss=include_held_ss)['baseline']
    from core.auto_trainer.larc_recovery import estimated_training_cost
    from core.auto_trainer.larc_risk import risk_at_energy
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    from core.auto_trainer.larc_ss_phase import phase_of,POST_FIRST_ARC_DOMESTIC,year1_full_comparison_status
    data_enabled = unified_larc_enabled(goal)
    consistent = unified_larc_enabled(goal)
    now = period_ordinal((state.scenario_state.get('larc_period')or{}).get('value')or{})
    dated=now is not None
    if dated:
        future = free_action_days(now + 1)[:2]
        calendar = [{'period': period_at(day), 'kind': 'overseas' if is_overseas(day) else 'domestic'} for day in future]
    else:
        # A verified junior pre-debut menu has no month/half by contract.
        # Keep the same two-action planning budget without inventing dates,
        # a debut boundary or any specific race/event recovery between them.
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
    # Optional icon-only 20-point rules remain diagnostics; do not turn an
    # unread current heal into physically known energy in the short channel.
    held_points = reward.get('observed_points', 0)
    held_maximum = reward.get('max_increase', 0)
    held_available = include_held_ss and (held_points > 0 or held_maximum > 0)
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

    def key(points): return round(max(0., points), 6)

    @lru_cache(maxsize=None)
    def solve(index, points, maximum, outing_index, ss_left, wisdom_left):
        if index == len(future):
            common_bar = points * 100 / capacity
            return terminal * (2 * common_bar - .008 * common_bar * common_bar), ()
        domestic = not is_overseas(future[index])if dated else True

        def following(action, change, *, next_maximum=maximum, outing=outing_index,
                ss=ss_left, wisdom=wisdom_left):
            next_points = min(next_maximum, max(0., points + change))
            next_points = min(next_maximum, next_points + post_fixed[index])
            value, path = solve(index + 1, key(next_points), next_maximum, outing, ss, wisdom)
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
            common.append(following('held_ss_recovery', healed - points, next_maximum=new_maximum, ss=False))
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
                    numeric = min(risk['value'], train_cap * FUTURE_WISDOM_VALUE_CREDIT)
                else:
                    if points < row['cost_points']:
                        continue
                    value, path = following('train:' + str(row['tab']),row.get('training_delta_points',-row['cost_points']))
                    numeric = risk['value']
                choices.append((numeric * readiness + value, path))
            values.append(max(choices, key=lambda item: item[0]))
        representative = sorted(values, key=lambda item: item[0])[len(values) // 2]
        return sum(item[0] for item in values) / len(values), representative[1]

    try:
        before_value, before_route = solve(0, key(min(capacity, before_points + initial_fixed)), capacity,
            0, held_available, True)
        after_value, after_route = solve(0, key(min(after_capacity, after_points + initial_fixed)), after_capacity,
            int(bool(consumed_outing)), held_available and not consumed_held_ss, True)
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
    return {'active': True, 'kind': 'symmetric_short_energy_only', 'basis': 'symmetric_short_energy_only',
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
            {'points': held_points, 'max_increase': held_maximum}, 'held_ss_numeric_value': 0.,
        'held_ss_attribute_Pt_fan_credit': 0., 'generated_ss_modelled': False,
        **({'held_ss_future_recovery_basis':'current_observed_group_once_as_planning_estimate',
            'next_ss_roster_known':False,'member_inventory_modelled':False,
            'future_recovery_is_execution_guarantee':False}if year1_full else{}),
        'stateful_statistics_revaluation': False, 'current_ss_rule_heal_not_credited': reward.get('rule_points', 0),
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
