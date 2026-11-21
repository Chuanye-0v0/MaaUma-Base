"""Finite joint-layout prefix with real five-stat transitions.

This is a conditional short-route estimate, not a game simulator or a claim
that the next rainbow is known. Saved 1.6 keeps its legacy tail; 1.6.1 uses
only a complete bounded window and otherwise keeps the full legacy route.
"""
from dataclasses import replace
from functools import lru_cache
import math

from core.auto_trainer.state import TRAINING_TYPES
from core.auto_trainer.larc_opportunity_forecast import raw_gain, display_gain, soft_gaps

MAX_PREFIX_STEPS = 2
MAX_LAYOUTS = 4
MAX_STATES = 12000


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _stats(values):
    return tuple(round(max(0., values.get(key, 0.)), 3) for key in (*TRAINING_TYPES, 'skill_pt'))


def _mapping(values):
    return dict(zip((*TRAINING_TYPES, 'skill_pt'), values))


def add_gains(values, gains):
    """Apply actual displayed/conditional gains, never valuation equivalents."""
    current = _mapping(values)
    return _stats({key: amount + max(0., gains.get(key, 0.)) for key, amount in current.items()})


def revalue_gain(state, goal, current, gains, period, context, remaining_budget):
    """Reprice the marginal gain without restarting whole-career planning."""
    from core.auto_trainer.larc_training_forecast import numeric_at_period
    targets = {key: goal.target_stats[key] for key in TRAINING_TYPES}
    income = context.get('future_income_credit', {})
    gaps = soft_gaps(current, targets, context.get('raw_training_rates', {}),
        max(1., remaining_budget), context.get('stage_pressure_weight', 1.), income)
    dynamic = {**context, 'attributes': gaps}
    observed = replace(state, current_stats=current)
    return numeric_at_period(observed, goal, gains, period, attribute_context=dynamic)


def _complete(layout):
    if (not isinstance(layout, (list, tuple)) or len(layout) != len(TRAINING_TYPES)
            or any(not isinstance(row, dict) for row in layout)
            or {row.get('tab') for row in layout} != set(TRAINING_TYPES)):
        return False
    for row in layout:
        gains = row.get('gains')
        if (not isinstance(gains, dict) or not _number(gains.get('skill_pt'))
                or not any(_number(gains.get(key)) and gains[key] > 0 for key in TRAINING_TYPES)):
            return False
        if any(not _number(value) or value < 0 for key, value in gains.items() if key in (*TRAINING_TYPES, 'skill_pt')):
            return False
    return True


def _historical(layout, state):
    from core.auto_trainer.larc_calendar import period_ordinal
    from core.auto_trainer.larc_recovery import period_value
    now = period_ordinal(period_value(state))
    for row in layout:
        meta = row.get('forecast_metadata') or {}
        turn = meta.get('source_turn_index')
        if type(turn) is int and 0 < turn < state.turn:
            continue
        before = period_ordinal(row.get('source_period') or meta.get('period') or {})
        if now is not None and before is not None and before < now:
            continue
        return False
    return True


def _stable_historical(layout, state):
    """Require one actual earlier joint sample, not mixed per-tab dates."""
    from core.auto_trainer.larc_calendar import period_ordinal
    from core.auto_trainer.larc_recovery import period_value
    turns = [(row.get('forecast_metadata') or {}).get('source_turn_index') for row in layout]
    if any(type(turn) is int for turn in turns):
        return (all(type(turn) is int for turn in turns) and len(set(turns)) == 1
                and 0 < turns[0] < state.turn)
    now = period_ordinal(period_value(state))
    dates = [period_ordinal(row.get('source_period') or
        (row.get('forecast_metadata') or {}).get('period') or {}) for row in layout]
    return now is not None and all(day is not None for day in dates) and len(set(dates)) == 1 and dates[0] < now


def _held_skill_adjustment(state, goal, current, gains, period):
    """One observed SS can claim only its remaining finite skill demand.

    Ordinary hypothetical trainings use the existing future floor. Exact
    integral projected Pt can additionally price this single held resource;
    fractional projected balances conservatively keep that floor only.
    """
    from core.auto_trainer.larc_policy import skill_marginal, future_skill_factor
    from core.auto_trainer.larc_phase import phase_weights
    points = gains.get('skill_pt', 0.)
    if not _number(points) or points <= 0:
        return 0., 'no_skill_pt_gain'
    observed = dict(current)
    owned = observed.get('skill_pt')
    if _number(owned) and float(owned).is_integer():
        observed['skill_pt'] = int(owned)
    future = replace(state, current_stats=observed,
        scenario_state={**state.scenario_state,
            'larc_period': {'status': 'verified', 'value': period}})
    floor = future_skill_factor(future, goal)
    if type(observed.get('skill_pt')) is not int:
        return 0., 'fractional_projected_pt_floor_only'
    resource = skill_marginal(future, goal, points)
    return points * phase_weights(future, goal)['skill_pt'] * (resource['factor'] - floor), resource['status']


def compare_stateful_routes(state, goal, *, calendar, cases, opportunity_layouts,
        context, consumed_gains, historical_layout_count, before_energy, after_energy,
        capacity, cost, reserve, automatic_after, charge_factors, rest_percent,
        terminal_weight, outings, held, held_available, held_numeric_value,
        held_gains, consumed_outing, consumed_held_ss, held_fan_values,
        energy_key, legacy_solve, discount=.95, held_numeric_value_after=None,
        ss_cycles=(),cycle_before_states=(),cycle_after_states=()):
    """Replace at most two future steps; keep exact legacy budget/resources.

    Each future choice sees one complete sampled layout. Sampling weights are
    policy scenarios, not calibrated probabilities of actual card placement.
    """
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    unified = unified_larc_enabled(goal)
    cycle_enabled = goal is not None and unified
    if not cycle_enabled:ss_cycles=();cycle_before_states=();cycle_after_states=()
    followup = unified and any(model is not None for model in ss_cycles)
    fallback = {'status': 'fallback', 'prefix_steps': 0,
        'basis': 'stateful prefix unavailable; unchanged legacy route retained'}
    stabilized = unified
    mei_first = unified
    future_inventory = None
    if mei_first:
        from core.auto_trainer.larc_recovery_policy import known_future_outings, future_mei_available
        outings, future_inventory = known_future_outings(outings)
    steps = len(calendar)
    if stabilized and steps > MAX_PREFIX_STEPS:
        return {**fallback, 'reason': 'full_route_exceeds_stateful_bound',
            'remaining_steps': steps, 'max_stateful_steps': MAX_PREFIX_STEPS,
            'sample_basis': 'unchanged legacy full route; no frozen-utility stateful tail'}
    if type(historical_layout_count) is not int or historical_layout_count < 1:
        return {**fallback, 'reason': 'no_bound_historical_joint_layout'}
    after_known=isinstance(consumed_gains,dict)and all(_number(v)and v>=0
        for k,v in consumed_gains.items()if k in (*TRAINING_TYPES,'skill_pt'))
    if not after_known and not followup:
        return {**fallback, 'reason': 'current_action_gains_unknown'}
    if not isinstance(context, dict) or not context.get('utility') or not context.get('attributes'):
        return {**fallback, 'reason': 'attribute_context_unavailable'}
    if any(not _number(state.current_stats.get(key)) for key in (*TRAINING_TYPES, 'skill_pt')):
        return {**fallback, 'reason': 'current_stats_unknown'}
    if held_available and held_numeric_value > 0 and not isinstance(held_gains, dict):
        return {**fallback, 'reason': 'held_ss_gains_unknown'}
    if held_numeric_value_after is not None and not _number(held_numeric_value_after):
        return {**fallback, 'reason': 'held_ss_after_value_unknown'}
    prefix = min(MAX_PREFIX_STEPS, steps)
    if prefix <= 0:
        return {**fallback, 'reason': 'no_future_action'}
    prepared = []
    initial = _stats(state.current_stats)
    initial_map = _mapping(initial)
    historical_layout_counts = []
    excluded_current_layouts = 0
    for case in cases:
        days = []
        case_counts = []
        for elapsed in range(prefix):
            source = (case.get('calendar_layouts', [])[elapsed]
                if case.get('calendar_layouts') else case.get('layouts') if calendar[elapsed]['kind'] == 'overseas'
                else opportunity_layouts)
            complete = [layout for layout in (source or ()) if _complete(layout)]
            historical = [layout for layout in complete if
                (_stable_historical(layout, state) if stabilized else _historical(layout, state))]
            if stabilized:
                excluded_current_layouts += len(complete) - len(historical)
                layouts = historical[:MAX_LAYOUTS]
            else:
                layouts = complete[:MAX_LAYOUTS]
            if not layouts:
                reason = ('complete_historical_joint_layout_unavailable' if stabilized and complete
                          else 'complete_joint_layout_unavailable')
                return {**fallback, 'reason': reason}
            if not any(_historical(layout, state) for layout in layouts):
                return {**fallback, 'reason': 'complete_historical_joint_layout_unavailable'}
            converted = []
            for layout in layouts:
                converted_rows = []
                for row in layout:
                    tab = row['tab']
                    net = (row.get('destination_metadata') or {}).get('net_vitality_percent')
                    if not _number(net):
                        net = (row.get('forecast_metadata') or {}).get('net_vitality_percent')
                    if tab == 'wisdom':
                        recovery = row.get('wisdom_recovery_percent', net)
                        if not _number(recovery) or recovery < 0:
                            return {**fallback, 'reason': 'sampled_wisdom_recovery_unknown'}
                        change = recovery
                    else:
                        row_cost = row.get('cost', -net if _number(net) and net < 0 else cost)
                        if not _number(row_cost) or row_cost <= 0:
                            return {**fallback, 'reason': 'sampled_training_cost_unknown'}
                        change = -row_cost
                    raw = {key: raw_gain(initial_map[key], max(0., row['gains'].get(key, 0.))) for key in TRAINING_TYPES}
                    raw['skill_pt'] = row['gains'].get('skill_pt', 0.)
                    converted_rows.append({**row, 'raw_gains': raw, 'energy_change': change})
                converted.append(tuple(converted_rows))
            days.append(tuple(converted))
            case_counts.append(len(layouts))
        prepared.append(tuple(days))
        historical_layout_counts.append(case_counts)

    from core.auto_trainer.larc_risk import risk_at_energy
    from core.auto_trainer.larc_capacity import recover_points
    from core.auto_trainer.larc_recovery import period_value
    source_period = period_value(state)
    visits = 0

    @lru_cache(maxsize=4096)
    def numeric(values, gains_key, elapsed, factor=1.):
        current = _mapping(values)
        gains = _mapping(gains_key)
        period = calendar[elapsed].get('period') or source_period
        return revalue_gain(state, goal, current, gains, period, context,
            max(1., context.get('estimated_training_budget', steps) - 1 - elapsed)) * factor

    def gain_key(gains):
        return tuple(round(max(0., gains.get(key, 0.)), 3) for key in (*TRAINING_TYPES, 'skill_pt'))

    outing_extras = []
    for item in outings:
        gains = item.get('stats_gain')
        if not isinstance(gains, dict):
            return {**fallback, 'reason': 'future_outing_gains_unknown'}
        base = revalue_gain(state, goal, initial_map, gains, source_period, context,
                           context.get('estimated_training_budget', steps))
        outing_extras.append(item['numeric_value'] - base)
    held_gains = held_gains or {}
    held_base = revalue_gain(state, goal, initial_map, held_gains, source_period,
                            context, context.get('estimated_training_budget', steps)) * .85
    # Only the current action and this observed SS may claim finite known
    # skill demand. Preserve the caller's separate after-action skill value;
    # five-stat changes continue to be repriced by numeric(values, ...) below.
    held_extras = {False: held_numeric_value - held_base,
        True: (held_numeric_value_after if held_numeric_value_after is not None else held_numeric_value) - held_base}

    def readiness(energy):
        shortfall = min(1., max(0., (reserve - energy) / max(1., reserve)))
        return 1. - {'low': .45, 'medium': .35, 'high': .25}[goal.risk_tolerance] * shortfall

    class BudgetExceeded(Exception):
        pass

    @lru_cache(maxsize=None)
    def solve(elapsed, energy, values, outing_index, ss_left, wisdom_left, case_index, after_action,
              cycle_cells=(),generated_used=False,post_trainings=0,post_actions=0,resource_enabled=True):
        nonlocal visits
        cycle=ss_cycles[case_index] if ss_cycles and resource_enabled else None
        visits += 1
        if visits > MAX_STATES:
            raise BudgetExceeded
        if elapsed == prefix:
            if stabilized:
                return terminal_weight * (2 * energy - .008 * energy * energy), (), ()
            if not resource_enabled and ss_cycles:
                tail_value,tail_route=legacy_solve(steps-elapsed,energy,wisdom_left,outing_index,ss_left,case_index,after_action,
                    (),False,0,0,False)
            elif cycle is None:
                tail_value, tail_route = legacy_solve(steps - elapsed, energy, wisdom_left,
                    outing_index, ss_left, case_index, after_action)
            else:
                tail_value, tail_route = legacy_solve(steps - elapsed, energy, wisdom_left,
                    outing_index, ss_left, case_index, after_action,cycle_cells,generated_used,post_trainings,post_actions,resource_enabled)
            return tail_value, tail_route, ()
        domestic = calendar[elapsed]['kind'] == 'domestic'

        def continuation(change, next_values=values, *, outing=outing_index,
                         ss=ss_left, wisdom=wisdom_left,cells=cycle_cells,generated=generated_used,trainings=post_trainings,
                         actions=post_actions+int(cycle is not None and not ss_left)):
            next_energy = energy_key(energy + change, ss)
            next_energy = energy_key(next_energy + automatic_after[elapsed], ss)
            return solve(elapsed + 1, next_energy, next_values, outing, ss,
                         wisdom, case_index, after_action,cells,generated,trainings,actions,resource_enabled)

        def choice(action, immediate, change, gains=None, **resources):
            updated = add_gains(values, gains or {})
            future, path, trace = continuation(change, updated, **resources)
            action_energy = energy_key(energy + change, resources.get('ss', ss_left))
            entry = {'action': action, 'stats_after': _mapping(updated),
                     'energy_after_action': round(action_energy, 4),
                     'scheduled_recovery_after': automatic_after[elapsed],
                     'energy_after': round(energy_key(action_energy + automatic_after[elapsed],
                         resources.get('ss', ss_left)), 4)}
            return immediate + discount * future, (action,) + path, (entry,) + trace

        rest_here = rest_percent if domestic else ((50 * 100 / capacity, 1.),)
        common = []
        if not (mei_first and future_mei_available(domestic, outing_index, outings)):
            rests = [(weight, choice('rest', 0., amount)) for amount, weight in rest_here]
            middle = rests[len(rests)//2][1]
            common.append((sum(weight * item[0] for weight, item in rests), middle[1], middle[2]))
        if domestic and outing_index < len(outings):
            item = outings[outing_index]
            gain = item['stats_gain']
            immediate = numeric(values, gain_key(gain), elapsed) + outing_extras[outing_index]
            common.append(choice('outing:' + str(item['stage']), immediate,
                item['recovery_percent'], gain, outing=outing_index + 1))
        if domestic and ss_left:
            recovered, _ = recover_points(energy * capacity / 100, capacity,
                                          held['points'], held['max_increase'])
            if stabilized:
                period = calendar[elapsed].get('period') or source_period
                skill_adjustment, skill_credit_status = _held_skill_adjustment(
                    state, goal, _mapping(values), held_gains, period)
                immediate = numeric(values, gain_key(held_gains), elapsed, .85) + .85 * skill_adjustment
            else:
                skill_credit_status = None
                immediate = numeric(values, gain_key(held_gains), elapsed, .85) + held_extras[after_action]
            immediate += held_fan_values[elapsed] if elapsed < len(held_fan_values) else 0.
            ss_choice = choice('held_ss' if held_numeric_value else 'held_ss_recovery',
                immediate, recovered * 100 / capacity - energy, held_gains, ss=False,
                cells=cycle.consume_held(cycle_cells) if cycle is not None else cycle_cells,trainings=0,actions=0)
            if stabilized:
                ss_choice[2][0].update(immediate_value=immediate,
                    held_skill_credit_status=skill_credit_status)
            common.append(ss_choice)
        if cycle is not None and domestic and not ss_left and not generated_used and cycle.available(cycle_cells,post_trainings,post_actions):
            current=_mapping(values)
            gains={key:display_gain(current[key],raw_gain(initial_map[key],cycle.gains.get(key,0.)))for key in TRAINING_TYPES}
            gains['skill_pt']=cycle.gains.get('skill_pt',0.)
            immediate=numeric(values,gain_key(gains),elapsed,cycle.valuation_discount)
            common.append(choice('regenerated_ss_conditional',immediate,0.,gains,
                cells=cycle.consume_generated(cycle_cells),generated=True))
        results = []
        for layout in prepared[case_index][elapsed]:
            options = list(common)
            for row in layout:
                change = row['energy_change']
                if change < 0 and energy < -change:
                    continue
                current = _mapping(values)
                gains = {key: display_gain(current[key], row['raw_gains'][key]) for key in TRAINING_TYPES}
                gains['skill_pt'] = row['raw_gains']['skill_pt']
                factor = row.get('forecast_discount', 1.)
                value = numeric(values, gain_key(gains), elapsed, factor)
                value += row.get('bond', 0.) + (row.get('charge', 0.) * charge_factors[elapsed] if cycle is None else 0.)
                value = risk_at_energy(value, row.get('risk_metadata', {}), energy)['value'] * readiness(energy)
                action = 'wisdom' if row['tab'] == 'wisdom' else 'train:' + row['tab']
                options.append(choice(action, value, change, gains,
                    wisdom=False if row['tab'] == 'wisdom' else wisdom_left,
                    cells=cycle.advance(cycle_cells,row,domestic=domestic) if cycle is not None else cycle_cells,
                    trainings=post_trainings+int(cycle is not None and domestic and not ss_left)))
            results.append(max(options, key=lambda item: item[0]))
        representative = sorted(results, key=lambda item: item[0])[len(results)//2]
        return sum(item[0] for item in results) / len(results), representative[1], representative[2]

    branches = []
    try:
        after_stats = add_gains(initial, consumed_gains)if after_known else initial
        if followup:
            from core.auto_trainer.larc_ss_resource_cycle import select_cycle_side
            # Finish every unchanged-state comparison before looking at any
            # candidate after. A difficult/unknown after cannot change these.
            before_rows=[]
            for index,case in enumerate(cases):
                explicit=solve(0,before_energy,initial,0,held_available,True,index,False,
                    cycle_before_states[index]if cycle_before_states else ())
                cycle=ss_cycles[index]if ss_cycles else None
                proxy=(solve(0,before_energy,initial,0,held_available,True,index,False,resource_enabled=False)
                       if cycle is not None else explicit)
                selected,resource_selected=select_cycle_side(proxy,explicit)if cycle is not None else(explicit,False)
                before_rows.append((selected,resource_selected,proxy[0],explicit[0]))
            before_visits=visits;visits=0;after_budget=False
            for index,case in enumerate(cases):
                selected_before,before_selected,bproxy,bresource=before_rows[index]
                cycle=ss_cycles[index]if ss_cycles else None
                fallback_reason=None;after_selected=False;charge_repriced=False
                if after_known and not after_budget:
                    try:
                        explicit=solve(0,after_energy,after_stats,1 if consumed_outing else 0,
                            held_available and not consumed_held_ss,True,index,held_numeric_value_after is not None,
                            cycle_after_states[index]if cycle_after_states else ())
                        proxy=(solve(0,after_energy,after_stats,1 if consumed_outing else 0,
                            held_available and not consumed_held_ss,True,index,held_numeric_value_after is not None,
                            resource_enabled=False)if cycle is not None else explicit)
                        selected_after,after_selected=select_cycle_side(proxy,explicit)if cycle is not None else(explicit,False)
                        if after_selected and not consumed_held_ss and cycle_before_states and cycle_after_states[index]!=cycle_before_states[index]:
                            try:
                                uncharged=solve(0,after_energy,after_stats,1 if consumed_outing else 0,
                                    held_available,True,index,held_numeric_value_after is not None,cycle_before_states[index])
                                charge_repriced=explicit[0]>uncharged[0]+1e-9
                            except BudgetExceeded:
                                # This optional overlap proof is not permission
                                # to erase unproven current-charge proxy value.
                                charge_repriced=False;after_budget=True
                    except BudgetExceeded:
                        after_budget=True;fallback_reason='after_state_budget_exceeded'
                else:fallback_reason='after_state_budget_exceeded'if after_budget else'current_action_gains_unknown'
                if fallback_reason is not None:
                    value,path=legacy_solve(steps,after_energy,True,1 if consumed_outing else 0,
                        held_available and not consumed_held_ss,index,held_numeric_value_after is not None,
                        (),False,0,0,False)
                    selected_after=(value,path,());proxy=explicit=selected_after
                    after_selected=False;charge_repriced=False
                bv,br,bt=selected_before;av,ar,at=selected_after
                branches.append({'name':case.get('name'),'weight':case.get('weight',1.),
                    'future_value_before':bv,'future_value_after':av,
                    'future_route_before':list(br),'future_route_after':list(ar),
                    'stat_trace_before':list(bt),'stat_trace_after':list(at),
                    **({'after_stateful_fallback':fallback_reason}if fallback_reason else {}),
                    **({'ss_cycle_used':before_selected or after_selected,
                        'original_charge_proxy_retained':not after_selected,
                        'before_cycle_model_selected':before_selected,'after_cycle_model_selected':after_selected,
                        'before_proxy_value':bproxy,'before_cycle_value':bresource,
                        'after_proxy_value':proxy[0],'after_cycle_value':explicit[0],
                        'before_model_selection_basis':'unchanged state-only max(proxy,resource), ties keep proxy',
                        'after_model_selection_basis':'same non-additive max on this conditional after state',
                        'before_original_charge_proxy_retained':not before_selected,
                        'after_original_charge_proxy_retained':not after_selected,
                        'current_identified_charge_repriced':charge_repriced,
                        'regeneration_in_representative_path':'regenerated_ss_conditional'in br or'regenerated_ss_conditional'in ar}
                       if cycle is not None else {})})
            visits+=before_visits
        else:
            for index, case in enumerate(cases):
                before_value, before_route, before_trace = solve(0, before_energy, initial,
                    0, held_available, True, index, False,cycle_before_states[index] if cycle_before_states else ())
                after_value, after_route, after_trace = solve(0, after_energy, after_stats,
                    1 if consumed_outing else 0, held_available and not consumed_held_ss, True, index,
                    held_numeric_value_after is not None,cycle_after_states[index] if cycle_after_states else ())
                used='regenerated_ss_conditional' in before_route or 'regenerated_ss_conditional' in after_route
                cycle=ss_cycles[index] if ss_cycles else None
                if cycle is not None and not used:
                    before_value,before_route,before_trace=solve(0,before_energy,initial,0,held_available,True,index,False,
                        resource_enabled=False)
                    after_value,after_route,after_trace=solve(0,after_energy,after_stats,1 if consumed_outing else 0,
                        held_available and not consumed_held_ss,True,index,held_numeric_value_after is not None,resource_enabled=False)
                branches.append({'name': case.get('name'), 'weight': case.get('weight', 1.),
                    'future_value_before': before_value, 'future_value_after': after_value,
                    'future_route_before': list(before_route), 'future_route_after': list(after_route),
                    'stat_trace_before': list(before_trace), 'stat_trace_after': list(after_trace),
                    **({'ss_cycle_used':used,'original_charge_proxy_retained':not used}if cycle is not None else {})})
    except BudgetExceeded:
        return {**fallback, 'reason': 'bounded_state_budget_exceeded', 'evaluated_states': visits}
    finally:
        solve.cache_clear()
        numeric.cache_clear()
    weight = sum(item['weight'] for item in branches)
    if weight <= 0:
        return {**fallback, 'reason': 'scenario_weights_invalid'}
    before_value = sum(item['weight'] * item['future_value_before'] for item in branches) / weight
    after_value = sum(item['weight'] * item['future_value_after'] for item in branches) / weight
    return {'status': 'applied', 'prefix_steps': prefix, 'tail_steps': steps - prefix,
        'tail_basis': 'legacy frozen-utility route' if steps > prefix else 'none',
        'future_value_before': before_value, 'future_value_after': after_value,
        'future_route_before': branches[0]['future_route_before'],
        'future_route_after': branches[0]['future_route_after'], 'branches': branches,
        'after_current_action_stats': _mapping(after_stats)if after_known else None,
        'current_transition_basis': ('conditional on supplied current-action yield being realized; not a receipt'
            if after_known else 'current yield unknown; only after uses the declared legacy fallback'),
        **({'before_basis_state_only':True,'state_budget_scope':'independent unchanged-before and candidate-after',
            'before_evaluated_states':before_visits,'after_evaluated_states':visits-before_visits}if followup else {}),
        'evaluated_states': visits,
        'layout_sample_cap': MAX_LAYOUTS, 'sample_status': 'conditional joint observed layouts; not measured attendance probabilities',
        **({'recovery_policy': {'mei_first': True, 'future_inventory': future_inventory,
            'known_outing_stages': [item['stage'] for item in outings],
            'current_outing_consumed': bool(consumed_outing), 'dedicated_recovery_cap': None}}
           if mei_first else {}),
        **({'sample_basis': 'historical_complete_joint_layouts_only',
            'historical_layout_counts': historical_layout_counts,
            'excluded_current_layouts': excluded_current_layouts} if stabilized else {}),
        'stat_cap_status': 'unobserved caps not invented; projected transitions remain conditional',
        'basis': 'real displayed stat transitions within finite prefix; marginal utility recomputed per action'}
