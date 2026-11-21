"""Small energy-route comparison over future free actions; no game simulator.

Future layouts are estimates from recent observations. Rest weights describe
planning scenarios, not a measured probability distribution of game outcomes.
"""
from functools import lru_cache

from core.auto_trainer.larc_recovery import (
    FUTURE_WISDOM_VALUE_CREDIT, FUTURE_WISDOM_RECOVERY_CREDIT,
    PLANNING_START_ENERGY, critical_window, period_value, planning_context,
    points_to_percent, recovery_value,
)
from core.auto_trainer.larc_ss_opportunity import ss_redemption
from core.auto_trainer.larc_capacity import recover_points
from core.auto_trainer.larc_calendar import FORCED_RACES, free_action_days, is_overseas, period_at, period_ordinal

REST_SCENARIOS = ((30, .25), (50, .5), (70, .25))
FUTURE_DISCOUNT = .95


def terminal_energy_weight(state, goal, window):
    """Carry energy once past a domestic rolling horizon, never an ending.

    The old .5 potential remains frozen. V2 modestly raises its coefficient
    only when a further free training can still use the stored energy. This
    lives inside route valuation; no second immediate recovery score is added.
    """
    if window["kind"] != "rolling_recovery":
        return 0.
    from core.auto_trainer.larc_policy import training_balance_v2_enabled
    if not training_balance_v2_enabled(goal):
        return .5
    period = period_value(state)
    year, month = period.get("year_index"), period.get("month")
    domestic = year == 1 or year in {2, 3} and type(month) is int and not 7 <= month <= 9
    if not domestic:
        return .5
    calendar = window.get("action_calendar", [])
    ending = period_ordinal(calendar[-1]["period"]) if calendar else None
    if ending is not None:
        horizon = state.scenario_state.get("larc_ss_planning_horizon",
            getattr(goal, 'larc_policy', {}).get('ss_planning_horizon', 'full_career'))
        cutoff = 23 if horizon == "junior_cutoff_discard" else None
        remaining = free_action_days(ending + 1, cutoff) if cutoff is not None else free_action_days(ending + 1)
        if not remaining:
            return 0.
    elif window.get("steps") == 0:
        return 0.
    elif not (year == 1 and period.get("pre_debut") is True):
        return .5
    return .7


def recovery_window(state):
    context = planning_context(state, include_held_ss=False)
    if context["final_training"]:
        return None
    critical = critical_window(state)
    period = period_value(state)
    now = period_ordinal(period)
    if now is not None:
        if critical:
            end = (period["year_index"] - 1) * 24 + 18
            days = free_action_days(now + 1, end)
        else:
            days = free_action_days(now + 1)[:4]
        if not days:
            return None
        calendar = [{"period": period_at(day), "kind": "overseas" if is_overseas(day) else "domestic"}
                    for day in days]
        domestic = sum(row["kind"] == "domestic" for row in calendar)
        base = critical or {"kind": "rolling_recovery", "future_trainings": len(days),
                            "free_actions_including_current": 1 + len(days), "race_before_entry": False}
        return {**base, "steps": len(days), "domestic_steps": domestic, "action_calendar": calendar,
                "skipped_races": [{"period": period_at(day), "race": race} for day, race in FORCED_RACES.items()
                                  if now < day < days[-1]]}
    # Ordinary domestic turns also need a recovery plan. Four future actions
    # can distinguish one full recovery from repeatedly taking a small heal.
    return {"kind": "rolling_recovery", "steps": 4, "future_trainings": 4,
            "free_actions_including_current": 5, "race_before_entry": False,
            "domestic_steps": 4 if period.get("month") not in {7, 8, 9}
                              or period.get("year_index") == 1 else 0}


def effective_recovery_window(state, goal):
    """V2 bounds all future energy uses by an explicitly discarded career."""
    window = recovery_window(state)
    from core.auto_trainer.larc_policy import training_balance_v2_enabled
    horizon = state.scenario_state.get("larc_ss_planning_horizon",
        getattr(goal, 'larc_policy', {}).get('ss_planning_horizon', 'full_career'))
    if window is None or not training_balance_v2_enabled(goal) or horizon != 'junior_cutoff_discard':
        return window
    now = period_ordinal(period_value(state))
    if now is None:
        return window
    calendar = [step for step in window.get("action_calendar", [])
                if period_ordinal(step["period"]) < 23]
    if len(calendar) == window["steps"]:
        return window
    return {**window, "kind": "rolling_recovery", "steps": len(calendar),
            "future_trainings": len(calendar), "domestic_steps": len(calendar),
            "free_actions_including_current": 1 + len(calendar), "action_calendar": calendar,
            "discard_cutoff_ordinal": 23,
            "basis": "future_energy_actions_end_before_explicit_junior_discard"}


def route_continuation(state, goal, delta_percent, *, training_value, training_cost,
                       wisdom_value, wisdom_recovery, outings=(), consumed_outing=False,
                       include_held_ss=True, layouts=(), opportunity_layouts=(), expedition=None,
                       held_numeric_value=0., consumed_held_ss=False, held_fan_values=(), held_numeric_value_after=None,
                       consumed_gains=None, held_gains=None, stateful_context=None, historical_layout_count=0,
                       consumed_training_type=None, ss_cycle_context=None):
    window = effective_recovery_window(state, goal)
    if window is None:
        return {"active": False, "score": 0.}
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    unified = unified_larc_enabled(goal)
    mei_first = unified
    future_inventory = None
    if mei_first:
        from core.auto_trainer.larc_recovery_policy import known_future_outings, future_mei_available
        outings, future_inventory = known_future_outings(outings)
    from core.auto_trainer.larc_risk import strategy_v4_enabled, risk_at_energy
    from core.auto_trainer.larc_recovery import scheduled_recovery_after
    v4 = strategy_v4_enabled(state, goal)
    from core.auto_trainer.larc_charge_unlock import charge_unlock_window
    opening=charge_unlock_window(state,goal)
    from core.auto_trainer.larc_policy import overseas_purchase_v2_enabled
    opportunity_v2 = overseas_purchase_v2_enabled(goal)
    context = planning_context(state, include_held_ss=include_held_ss)
    before = min(100., max(0., state.vitality))
    after = min(100., max(0., before + delta_percent))
    scheduled = context["scheduled_recovery_percent"]
    held = context["held_ss_reward"]
    held_available = context["held_ss_available"] and (held["points"] > 0 or held_numeric_value > 0)
    cycle = None
    cycle_info = None
    followup = unified
    cycle_enabled = unified
    if cycle_enabled:
        from core.auto_trainer.larc_ss_resource_cycle import current_packet, from_packet
        observed_discounts=[row.get('forecast_discount') for layout in opportunity_layouts for row in layout
            if type(row.get('forecast_discount'))in (int,float) and 0<row['forecast_discount']<=1]
        try:
            packet = ss_cycle_context if ss_cycle_context is not None else current_packet(state,goal,held_numeric_value,held_gains,
                min(observed_discounts) if observed_discounts else .65)
            cycle, cycle_info = from_packet(packet,followup=followup)
        except (AttributeError,TypeError,ValueError,KeyError,ArithmeticError) as error:
            cycle=None
            cycle_info={'status':'unknown','reason':'optional_member_cycle_input_unavailable','error_kind':type(error).__name__}
        if not include_held_ss or not held_available:
            cycle = None
            cycle_info = {'status': 'unknown', 'reason': 'held_resource_not_in_current_comparison'}
    capacity = context["planning_capacity"]
    # Energy and all costs use percent of the original planning capacity.
    # Growing the cap changes storage, never grants additional healing points.
    expanded_limit = min(120., capacity + held["max_increase"]) * 100 / capacity
    reserve = PLANNING_START_ENERGY[goal.risk_tolerance]
    cost = max(1., training_cost)
    # At most one discounted future wisdom layout is credited. Repeated weak
    # wisdom is not assumed to restore a full bar while retaining today's yield.
    wisdom_value = min(wisdom_value, training_value * FUTURE_WISDOM_VALUE_CREDIT)
    wisdom_recovery = max(0., wisdom_recovery) * FUTURE_WISDOM_RECOVERY_CREDIT
    layouts = tuple(layouts) or ((training_value, wisdom_value),)
    steps = window["steps"]
    domestic_steps = window["domestic_steps"]
    calendar = window.get("action_calendar") or [
        {"period": None, "kind": "domestic" if elapsed < domestic_steps else "overseas"}
        for elapsed in range(steps)]
    automatic_after = [scheduled_recovery_after(state, step.get('period') or {}) if v4 else 0.
                       for step in calendar]
    charge_factors = [ss_redemption(state, earliest_actions=elapsed + 3)["value_factors"]["net_charge"]
                      if calendar[elapsed]["kind"] == "domestic" else 0. for elapsed in range(steps)]
    scenario_values = []
    scenario_actions = []
    opening_credits=[]
    for elapsed in range(steps):
        charge_credit=(opening['forecast_credit'] if opening and elapsed+1>=opening['actions_until_unlock'] else 0.)
        opening_credits.append(charge_credit)
        current_layouts = []
        current_actions = []
        for index, pair in enumerate(layouts):
            if index < len(opportunity_layouts):
                # Re-select within each layout using the shared value scale.
                # This depends on the calendar, not on simulated energy, so
                # compute it once rather than for every energy branch below.
                values = [(row["tab"], row["numeric"] + row["bond"] + row["charge"] * charge_factors[elapsed])
                          for row in opportunity_layouts[index]]
                pair = (max(value for tab, value in values if tab != "wisdom"),
                        max((value for tab, value in values if tab == "wisdom"), default=0.))
                current_actions.append([(row['tab'], value + charge_credit *
                    (FUTURE_WISDOM_VALUE_CREDIT if row['tab'] == 'wisdom' else 1.),
                    row.get('risk_metadata', {}), row)
                    for row, (_, value) in zip(opportunity_layouts[index], values)])
            else:
                current_actions.append([])
            # Both types can charge after unlock. Future wisdom is one
            # uncertain location, not a promise of the same heads as the best
            # of four consuming locations; retain its existing prior discount.
            current_layouts.append((pair[0]+charge_credit,pair[1]+charge_credit*FUTURE_WISDOM_VALUE_CREDIT))
        scenario_values.append(current_layouts)
        scenario_actions.append(current_actions)
    # Carry some energy value past an ordinary short horizon; overseas the
    # window already ends at its last training. Do not also price it now.
    terminal_weight = terminal_energy_weight(state, goal, window)
    rest_percent = tuple((points_to_percent(state, points), weight) for points, weight in REST_SCENARIOS)
    cases = (expedition or {}).get("scenarios") or [{"name": "current_phase", "weight": 1.}]
    cycle_models=[]
    if cycle is not None:
        expanded=[]
        for case in cases:
            for model,weight,label in cycle.variants():
                expanded.append({**case,'name':case['name']+'|'+label,'weight':case['weight']*weight})
                cycle_models.append(model)
        cases=expanded
    else:cycle_models=[None]*len(cases)
    # Layout values/costs do not depend on simulated energy. Prepare them once,
    # preserving row order (including tie breaking) for each overseas case.
    period_cases = []
    for case in cases:
        days = []
        for elapsed, step in enumerate(calendar):
            projected = (case["calendar_layouts"][elapsed] if case.get("calendar_layouts") else
                         case.get("layouts", ()) if step["kind"] == "overseas" else ())
            prepared = []
            for layout in projected:
                values = [(row, row["numeric"] + row.get("bond", 0.)
                           + row.get("charge", 0.) * charge_factors[elapsed]) for row in layout]
                train_reference = max((value for row, value in values if row["tab"] != "wisdom"), default=0.)
                prepared_rows=[]
                for row,value in values:
                    is_wisdom=row['tab']=='wisdom'
                    recovery=row.get('wisdom_recovery_percent')
                    observed_friendship=(opportunity_v2 and step['kind']=='overseas' and is_wisdom
                        and row.get('wisdom_friendship_opportunity') is True
                        and type(recovery) in {int,float} and recovery>0)
                    numeric=(min(value,train_reference*FUTURE_WISDOM_VALUE_CREDIT)
                        if is_wisdom and not observed_friendship else value)
                    prepared_rows.append((is_wisdom,numeric,max(1.,row.get('cost',cost)),
                        recovery if observed_friendship else 0.,observed_friendship,
                        row.get('risk_metadata', {})))
                prepared.append(tuple(prepared_rows))
            days.append((prepared, tuple(dict.fromkeys(
                row_cost for layout in prepared for wisdom, _, row_cost, _, _, _ in layout if not wisdom))))
        period_cases.append(days)

    def readiness_at(energy):
        # A soft estimate of access to good future training, not its actual
        # failure probability. Avoid valuing a low-energy rainbow at zero.
        shortfall = min(1., max(0., (reserve - energy) / max(1., reserve)))
        return 1. - {"low": .45, "medium": .35, "high": .25}[goal.risk_tolerance] * shortfall

    def energy_key(energy, ss_left=held_available):
        limit = expanded_limit if held_available and not ss_left else 100.
        return round(min(limit, max(0., energy)), 1)

    # This cache is local to one finite-horizon comparison. Evicting states
    # mid-search makes the longer pre-expedition tree recompute recursively.
    # Keep each state once; this changes runtime, not scores or route rules.
    @lru_cache(maxsize=None)
    def solve(left, energy, wisdom_left, outing_index, ss_left, case_index, after_action=False,
              cycle_cells=(), generated_used=False,post_trainings=0,post_actions=0,resource_enabled=True):
        cycle=cycle_models[case_index] if resource_enabled else None
        if left == 0:
            return terminal_weight * (2 * energy - .008 * energy * energy), ()
        elapsed = steps - left
        domestic = calendar[elapsed]["kind"] == "domestic"

        def continuation(change, *, wisdom=wisdom_left, outing=outing_index, ss=ss_left,
                         cells=cycle_cells, generated=generated_used,trainings=post_trainings,
                         actions=post_actions+int(cycle is not None and not ss_left)):
            # The fixed heal belongs after its own calendar action. It does
            # not fund that action's energy requirement and is not advanced
            # to an earlier November/December turn.
            next_energy = energy_key(energy + change, ss)
            next_energy = energy_key(next_energy + automatic_after[elapsed], ss)
            return solve(left - 1, next_energy, wisdom, outing, ss, case_index, after_action,cells,generated,trainings,actions,resource_enabled)

        consuming = None
        if energy >= cost:
            # Soft loss of access to useful consuming training at low energy.
            # This is a planning preference, never a new execution risk limit.
            readiness = readiness_at(energy)
            value, path = continuation(-cost,trainings=post_trainings+int(cycle is not None and domestic and not ss_left))
            consuming = (readiness, FUTURE_DISCOUNT * value, ("train",) + path)
        wisdom_option = None
        if wisdom_left and wisdom_recovery > 0:
            value, path = continuation(wisdom_recovery, wisdom=False,
                trainings=post_trainings+int(cycle is not None and domestic and not ss_left))
            wisdom_option = (FUTURE_DISCOUNT * value, ("wisdom",) + path)
        options = []
        # Overseas rest is a fixed 50-point recovery, not domestic 30/50/70.
        rest_here = rest_percent if domestic else ((points_to_percent(state, 50), 1.),)
        if not (mei_first and future_mei_available(domestic, outing_index, outings)):
            branches = [(weight, *continuation(amount)) for amount, weight in rest_here]
            options.append((FUTURE_DISCOUNT * sum(weight * value for weight, value, _ in branches),
                            ("rest",) + branches[len(branches)//2][2]))
        if domestic and outing_index < len(outings):
            outing = outings[outing_index]
            value, path = continuation(outing["recovery_percent"], outing=outing_index + 1)
            options.append((outing["numeric_value"] + FUTURE_DISCOUNT * value,
                            (f"outing:{outing['stage']}",) + path))
        if domestic and ss_left:
            recovered, _ = recover_points(energy * capacity / 100, capacity,
                                          held["points"], held["max_increase"])
            value, path = continuation(recovered * 100 / capacity - energy, ss=False,
                cells=cycle.consume_held(cycle_cells) if cycle is not None else cycle_cells,trainings=0,actions=0)
            # One observed resource, one future action; no repeat rewards and
            # no overseas use. The common baseline also owns this resource.
            fan_value = held_fan_values[elapsed] if elapsed < len(held_fan_values) else 0.
            held_here = held_numeric_value_after if after_action else held_numeric_value
            options.append((held_here + fan_value + FUTURE_DISCOUNT * value,
                            ("held_ss" if held_numeric_value else "held_ss_recovery",) + path))
        if cycle is not None and domestic and not ss_left and not generated_used and cycle.available(cycle_cells,post_trainings,post_actions):
            value, path = continuation(0., cells=cycle.consume_generated(cycle_cells), generated=True)
            options.append((cycle.numeric + FUTURE_DISCOUNT * value, ('regenerated_ss_conditional',) + path))
        # Choose after seeing each possible layout, then average its value.
        # Averaging layouts before choosing would erase the value of being
        # ready for a good rainbow and of recovering during a weak layout.
        scenario_results = []
        overseas_layouts, overseas_costs = period_cases[case_index][elapsed]
        if overseas_layouts:
            # All rows with the same cost reach exactly the same next state.
            # Calculate its continuation once per energy state, not once per
            # row in every forecast layout. Scores and representative routes
            # retain the original ordering and arithmetic.
            train_options = {}
            readiness = readiness_at(energy)
            for row_cost in overseas_costs:
                if energy >= row_cost:
                    value, path = continuation(-row_cost)
                    train_options[row_cost] = (FUTURE_DISCOUNT * value, ("train",) + path)
            base_option = max(options, key=lambda row: row[0])
            for layout in overseas_layouts:
                best = base_option
                for wisdom, numeric, row_cost, recovery, observed_friendship, risk_metadata in layout:
                    choice = None
                    if v4:
                        numeric = risk_at_energy(numeric, risk_metadata, energy)['value']
                    if wisdom:
                        if observed_friendship:
                            # Availability already comes from this sampled
                            # complete layout. Do not also replace its healing
                            # with today's weak wisdom or cap it at half a
                            # different tab. Repeated rainbows remain conditional
                            # on later layouts; ordinary weak wisdom stays capped.
                            value,path=continuation(recovery)
                            choice=(numeric*readiness + FUTURE_DISCOUNT*value, ('wisdom',)+path)
                        elif wisdom_option:
                            choice = (numeric * (readiness if v4 else 1.) + wisdom_option[0], wisdom_option[1])
                    elif row_cost in train_options:
                        value, path = train_options[row_cost]
                        choice = (numeric * readiness + value, path)
                    if choice is not None and choice[0] > best[0]:
                        best = choice
                scenario_results.append(best)
            representative = sorted(scenario_results, key=lambda row: row[0])[len(scenario_results)//2]
            return sum(row[0] for row in scenario_results) / len(scenario_results), representative[1]
        for index, (train, wisdom) in enumerate(scenario_values[elapsed]):
            choices = list(options)
            if (v4 or cycle is not None) and scenario_actions[elapsed][index]:
                train_cap=train
                if cycle is not None:
                    train_cap=max(value-row.get('charge',0.)*charge_factors[elapsed]
                        for tab,value,risk,row in scenario_actions[elapsed][index] if tab!='wisdom')
                for tab, value, risk_metadata, resource_row in scenario_actions[elapsed][index]:
                    if cycle is not None:
                        # Explicit matching replaces the old scalar charge proxy.
                        value -= resource_row.get('charge', 0.) * charge_factors[elapsed]
                    risk_adjusted = risk_at_energy(value, risk_metadata, energy)['value'] if v4 else value
                    if tab != 'wisdom' and consuming:
                        if cycle is not None:
                            cells = cycle.advance(cycle_cells,resource_row,domestic=domestic)
                            future,path = continuation(-cost,cells=cells,trainings=post_trainings+int(domestic and not ss_left))
                            choices.append((risk_adjusted * consuming[0] + FUTURE_DISCOUNT*future,('train',)+path))
                        else:
                            choices.append((risk_adjusted * consuming[0] + consuming[1], consuming[2]))
                    elif tab == 'wisdom' and wisdom_option:
                        if cycle is not None:
                            cells = cycle.advance(cycle_cells,resource_row,domestic=domestic)
                            future,path = continuation(wisdom_recovery,wisdom=False,cells=cells,
                                trainings=post_trainings+int(domestic and not ss_left))
                            choices.append((min(risk_adjusted,train_cap*FUTURE_WISDOM_VALUE_CREDIT)
                                * (readiness_at(energy) if v4 else 1.) + FUTURE_DISCOUNT*future,('wisdom',)+path))
                        else:
                            choices.append((min(risk_adjusted, train * FUTURE_WISDOM_VALUE_CREDIT)
                                            * readiness_at(energy) + wisdom_option[0], wisdom_option[1]))
                scenario_results.append(max(choices, key=lambda row: row[0]))
                continue
            if consuming:
                choices.append((train * consuming[0] + consuming[1], consuming[2]))
            if wisdom_option:
                choices.append((min(wisdom, train * FUTURE_WISDOM_VALUE_CREDIT)
                                * (readiness_at(energy) if v4 else 1.) + wisdom_option[0], wisdom_option[1]))
            scenario_results.append(max(choices, key=lambda row: row[0]))
        representative = sorted(scenario_results, key=lambda row: row[0])[len(scenario_results)//2]
        return sum(row[0] for row in scenario_results) / len(scenario_results), representative[1]

    initial = 1 if consumed_outing else 0
    branches = []
    cycle_before_states=[model.initial if model is not None else ()for model in cycle_models]
    cycle_after_states=[(model.consume_held(model.initial) if consumed_held_ss else
        model.after_current(model.initial,consumed_training_type)) if model is not None else ()for model in cycle_models]
    if cycle_info is not None:
        represented_net=represented_full=0;represented_members=[]
        if cycle is not None and (not cycle.empirical_lags or followup) and consumed_training_type is not None and not consumed_held_ss:
            updated=cycle.after_current(cycle.initial,consumed_training_type)
            represented_net=sum(b-a for a,b in zip(cycle.initial,updated))
            represented_full=sum(a<3 and b==3 for a,b in zip(cycle.initial,updated))
            represented_members=[{'member_id':mid,'before':a,'after':b,'net_cells':b-a,'newly_full':a<3 and b==3}
                for mid,a,b in zip(cycle.ids,cycle.initial,updated)if b>a]
        cycle_info={**cycle_info,'current_training_charge_coverage':{
            'training_type':consumed_training_type,'identified_net_cells':represented_net,'identified_newly_full_members':represented_full,
            'members':represented_members,
            'fraction':1. if represented_net else 0.,
            'basis':'same current stable IDs and accepted net preview only; empirical release-lag does not cover current held training'}}
    for index, case in enumerate(cases):
        before_value, before_route = solve(steps, energy_key(before + scheduled), True, 0, held_available, index,
            cycle_cells=cycle_before_states[index])
        ss_after = held_available and not consumed_held_ss
        after_value, after_route = solve(steps, energy_key(after + scheduled, ss_after), True, initial, ss_after, index,
            held_numeric_value_after is not None,cycle_after_states[index])
        used='regenerated_ss_conditional' in before_route or 'regenerated_ss_conditional' in after_route
        selection={}
        if followup and cycle_models[index] is not None:
            from core.auto_trainer.larc_ss_resource_cycle import select_cycle_side
            explicit_before=(before_value,before_route);explicit_after=(after_value,after_route)
            proxy_before=solve(steps,energy_key(before+scheduled),True,0,held_available,index,resource_enabled=False)
            proxy_after=solve(steps,energy_key(after+scheduled,ss_after),True,initial,ss_after,index,
                held_numeric_value_after is not None,resource_enabled=False)
            (before_value,before_route),before_selected=select_cycle_side(proxy_before,explicit_before)
            (after_value,after_route),after_selected=select_cycle_side(proxy_after,explicit_after)
            used=before_selected or after_selected
            selection={'before_cycle_model_selected':before_selected,'after_cycle_model_selected':after_selected,
                'before_proxy_value':proxy_before[0],'before_cycle_value':explicit_before[0],
                'after_proxy_value':proxy_after[0],'after_cycle_value':explicit_after[0],
                'before_model_selection_basis':'unchanged state-only max(proxy,resource), ties keep proxy',
                'after_model_selection_basis':'same non-additive max on this conditional after state',
                'before_original_charge_proxy_retained':not before_selected,
                'after_original_charge_proxy_retained':not after_selected,
                'regeneration_in_representative_path':'regenerated_ss_conditional' in before_route or 'regenerated_ss_conditional' in after_route}
            if after_selected and represented_net>0 and not consumed_held_ss:
                uncharged=solve(steps,energy_key(after+scheduled,ss_after),True,initial,ss_after,index,
                    held_numeric_value_after is not None,cycle_before_states[index])
                selection['current_identified_charge_repriced']=explicit_after[0]>uncharged[0]+1e-9
            else:selection['current_identified_charge_repriced']=False
        elif cycle_models[index] is not None and not used:
            # An unused explicit cycle must not erase the old beyond-window
            # charging proxy or claim it replaced the separate release credit.
            before_value,before_route=solve(steps,energy_key(before+scheduled),True,0,held_available,index,
                resource_enabled=False)
            after_value,after_route=solve(steps,energy_key(after+scheduled,ss_after),True,initial,ss_after,index,
                held_numeric_value_after is not None,resource_enabled=False)
        branches.append({"name": case["name"], "weight": case["weight"],
                         "future_value_before": before_value, "future_value_after": after_value,
                         "future_route_before": list(before_route), "future_route_after": list(after_route),
                         **({'ss_cycle_used':used,'original_charge_proxy_retained':
                             not selection.get('after_cycle_model_selected',used),**selection} if cycle_models[index] is not None else {})})
    stateful = None
    if unified:
        from core.auto_trainer.larc_stateful_routes import compare_stateful_routes, MAX_PREFIX_STEPS
        if steps > MAX_PREFIX_STEPS:
            stateful = {'status': 'fallback', 'prefix_steps': 0,
                'reason': 'full_route_exceeds_stateful_bound', 'remaining_steps': steps,
                'max_stateful_steps': MAX_PREFIX_STEPS,
                'sample_basis': 'unchanged legacy full route; no frozen-utility stateful tail'}
        else:
            try:
                stateful = compare_stateful_routes(state, goal, calendar=calendar, cases=cases,
                    opportunity_layouts=opportunity_layouts, context=stateful_context,
                    consumed_gains=consumed_gains, historical_layout_count=historical_layout_count,
                    before_energy=energy_key(before + scheduled),
                    after_energy=energy_key(after + scheduled, held_available and not consumed_held_ss),
                    capacity=capacity, cost=cost, reserve=reserve, automatic_after=automatic_after,
                    charge_factors=charge_factors, rest_percent=rest_percent, terminal_weight=terminal_weight,
                    outings=outings, held=held, held_available=held_available,
                    held_numeric_value=held_numeric_value, held_numeric_value_after=held_numeric_value_after,
                    held_gains=held_gains,
                    consumed_outing=consumed_outing, consumed_held_ss=consumed_held_ss,
                    held_fan_values=held_fan_values, energy_key=energy_key, legacy_solve=solve,
                    discount=FUTURE_DISCOUNT,ss_cycles=tuple(cycle_models),
                    cycle_before_states=tuple(cycle_before_states),cycle_after_states=tuple(cycle_after_states))
            except (KeyError, TypeError, ValueError, ArithmeticError) as error:
                stateful = {'status': 'fallback', 'prefix_steps': 0,
                    'reason': 'stateful_optional_input_unavailable', 'error_kind': type(error).__name__}
        if stateful['status'] == 'applied':
            branches = stateful['branches']
    solve.cache_clear()  # Do not retain the search table until cyclic GC runs.
    before_value = sum(row["weight"] * row["future_value_before"] for row in branches)
    after_value = sum(row["weight"] * row["future_value_after"] for row in branches)
    before_route, after_route = branches[0]["future_route_before"], branches[0]["future_route_after"]
    difference = after_value - before_value
    if cycle_info is not None:
        coverage=(sum(row['weight']for row in branches if row.get('after_cycle_model_selected'))
                  if followup else sum(row['weight']for row in branches if row.get('ss_cycle_used')))
        current_coverage=(sum(row['weight']for row,model in zip(branches,cycle_models)
            if row.get('current_identified_charge_repriced'))
            if followup else coverage)
        current=cycle_info.get('current_training_charge_coverage',{})
        if not current_coverage:current={**current,'identified_net_cells':0,'identified_newly_full_members':0,'fraction':0.,'members':[]}
        elif current.get('identified_net_cells',0)>0:current={**current,'fraction':current_coverage}
        cycle_info={**cycle_info,'scenario_branches':branches,
            'configured_release_fraction':cycle_info.get('replaces_battery_release_fraction',0.),
            'replaces_battery_release_credit':bool(coverage),'replaces_battery_release_fraction':coverage,
            'replaces_future_charge_scalar':bool(coverage),'replaces_future_charge_scalar_fraction':coverage,
            'current_training_charge_coverage':current,
            **({'shared_before_selection':'state-only non-additive proxy/resource envelope',
                'release_overlap_basis':'after resource valuation selected; no coverage inferred solely from before'}if followup else {}),
            'representative_path_not_committed_plan':True,'no_extra_main_action_budget':True}
    # Replace the old marginal energy value; adding both would price the same
    # recovery twice and can make resting beat a useful high-energy training.
    adjustment = difference - recovery_value(state, delta_percent,
        include_held_ss=include_held_ss and not consumed_held_ss)
    return {"active": True, **window, "score": round(adjustment, 4),
            **({'ss_resource_cycle':cycle_info} if cycle_info is not None else {}),
            **({'recovery_policy': {'mei_first': True, 'future_inventory': future_inventory,
                'known_outing_stages': [item['stage'] for item in outings],
                'current_outing_consumed': bool(consumed_outing), 'dedicated_recovery_cap': None}}
               if mei_first else {}),
            **({'stateful_prefix': stateful} if stateful is not None else {}),
            **({'scheduled_recovery_after_actions': [
                {'period': step['period'], 'recovery_percent': round(amount, 4), 'timing': 'after_action'}
                for step, amount in zip(calendar, automatic_after) if amount],
                'future_training_risk': 'sampled_rate_only_at_its_observed_energy; otherwise_unknown',
                'readiness_basis': 'access_and_risk_preference_not_measured_failure_probability'} if v4 else {}),
            **({"terminal_energy_weight": terminal_weight,
                "terminal_energy_basis": "once_at_end_of_domestic_route; no_extra_immediate_recovery_credit"}
               if terminal_weight != (.5 if window["kind"] == "rolling_recovery" else 0.) else {}),
            "opening_charge_unlock":opening,"opening_charge_future_credits":opening_credits,
            "opening_charge_wisdom_credits":[v*FUTURE_WISDOM_VALUE_CREDIT for v in opening_credits],
            "continuation_value_delta": round(difference, 4),
            "future_value_before": round(before_value, 4), "future_value_after": round(after_value, 4),
            "after_vitality_percent": round(after, 4),
            "future_route_before": list(before_route), "future_route_after": list(after_route),
            "held_ss_reward": held,
            "unready_ss_reward":context['unready_ss_reward'],
            "held_ss_numeric_value": round(held_numeric_value, 4),
            "held_ss_ready_count":context['held_ss_ready_count'],
            "held_ss_numeric_value_after": round(held_numeric_value_after, 4) if held_numeric_value_after is not None else round(held_numeric_value, 4),
            "held_ss_conditional_fan_values": list(held_fan_values),
            "held_ss_consumed_now": consumed_held_ss,
            "future_energy_basis": "percent_of_initial_capacity; held SS clips only at use and may raise capacity",
            "future_training_value": round(training_value, 4),
            "future_wisdom_value_credit": round(wisdom_value, 4),
            "future_wisdom_max_credited_turns": steps if opportunity_v2 else 1,
            "future_ordinary_wisdom_max_credited_turns": 1,
            "future_wisdom_opportunity_model": "conditional_observed_layouts" if opportunity_v2 else "one_discounted_current_wisdom",
            "rest_scenarios": [{"points": p, "planning_weight": w} for p, w in REST_SCENARIOS],
            "rest_probability_status": "unknown; weights are policy scenarios",
            "future_layout_scenarios": len(layouts),
            "future_charge_redemption_factors": charge_factors,
            "expedition_branches": branches if (expedition or {}).get("active") else [],
            "basis": "same future action budget; empirical layout scenarios; recovery uses a turn; no random event heal promised"}
