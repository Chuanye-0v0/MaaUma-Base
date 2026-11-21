"""Calendar-based training forecasts shared by recovery and upgrade comparisons.

Layouts describe opportunities, not a promised next rainbow. Missing effect
breakdowns use labelled estimates; unknown upgrades are never treated as owned.
"""
from dataclasses import replace
from statistics import median

from core.auto_trainer.larc_calendar import is_overseas, period_ordinal
from core.auto_trainer.larc_expedition_forecast import BASES, TRAINING_SLOTS, _context
from core.auto_trainer.larc_opportunity_forecast import display_gain, raw_gain
from core.auto_trainer.larc_recovery import period_value


def abroad(period):
    day = period_ordinal(period or {})
    return day is not None and is_overseas(day)


def strategy_v4_enabled(state, goal=None):
    """Explicit local opt-in; the legacy and purchase-V3 defaults stay intact."""
    return (state.scenario_state.get("larc_strategy_v4") is True
            or getattr(goal, "larc_policy", {}).get("strategy_v4") is True)


def expectation_bonus(value):
    """Guide-derived step estimate, not a measured CN multiplier breakdown."""
    if type(value) not in {int, float}:
        return None
    if value >= 100:
        return 30 + int((value - 100) // 10)
    if value >= 30:
        return 16 + int((value - 30) // 5)
    return (0, 5, 8, 10, 13, 15)[max(0, min(5, int(value // 5)))]


def growth_snapshot(state, goal=None):
    context = _context(state)
    result = {"period": period_value(state), "levels": context.get("levels")}
    if state.scenario_state.get("larc_purchase_model_v3") is True:
        result["purchase_v3"] = True
    observed = state.scenario_state.get("larc_growth_observation", {})
    if (observed.get("status") == "accepted" and observed.get("turn_index") == state.turn
            and type(observed.get("expectation")) in {int, float}
            and 0 <= observed["expectation"] <= 200):
        result["expectation"] = observed["expectation"]
    if goal is not None:
        from core.auto_trainer.larc_strategy_params import unified_larc_enabled
        if unified_larc_enabled(goal):
            from core.auto_trainer.larc_action_history import expectation_transition
            transition = expectation_transition(state)
            if isinstance(transition, dict):
                result.update(expectation_transition=transition,
                    run_session_id=transition.get('run_session_id'), source_turn_index=state.turn)
    return result


def project_gains(current_stats, gains, tab, source, target):
    """Remove source-only effects, then apply destination effects exactly once."""
    unknown = []
    old_levels, new_levels = source.get("levels"), target.get("levels")
    # Legacy samples have no purchase snapshot. Hold permanent effects equal;
    # the source period still identifies whether an overseas effect was active.
    old_levels = old_levels or new_levels or [0] * 10
    new_levels = new_levels or old_levels
    old_overseas, new_overseas = abroad(source.get("period")), abroad(target.get("period"))
    slot = TRAINING_SLOTS[tab]
    old_boost = 50 if old_overseas and old_levels[slot] == 3 else 0
    new_boost = 50 if new_overseas and new_levels[slot] == 3 else 0
    old_expect = expectation_bonus(source.get("expectation"))
    new_expect = expectation_bonus(target.get("expectation"))
    expectation_delta = new_expect - old_expect if old_expect is not None and new_expect is not None else 0
    effect = source.get("training_effect_percent")
    if type(effect) not in {int, float}:
        effect = 50 + old_boost + (old_expect or 0)
        unknown.append("shared_effect_estimated")
    factor = (100 + effect + new_boost - old_boost + expectation_delta) / (100 + effect)
    friendship = source.get("friendship")
    if friendship is True:
        factor *= (1.2 if new_levels[7] == 3 else 1.) / (1.2 if old_levels[7] == 3 else 1.)
    elif old_levels[7] != new_levels[7]:
        unknown.append("friendship_unknown_no_upgrade_credit")
    old_level = 5 if old_overseas else source.get("training_level")
    new_level = 5 if new_overseas else target.get("training_level")
    facility = 1.
    if type(old_level) in {int, float} and type(new_level) in {int, float}:
        first, last = BASES[tab]
        base = lambda level: first + (last - first) * (min(5, max(1, level)) - 1) / 4
        # Unknown flat support bonuses dilute facility growth. Symmetric in
        # both directions, unlike applying a fresh +50% dilution each time.
        facility = (base(new_level) + first) / (base(old_level) + first)
    elif old_overseas != new_overseas:
        unknown.append("domestic_training_level")
    pt_bonus = lambda level: 10 * (level >= 1) + 10 * (level == 3)
    pt_delta = pt_bonus(new_levels[5]) - pt_bonus(old_levels[5])
    transformed = {}
    pt_report = None
    purchase_v3 = target.get("purchase_v3", source.get("purchase_v3", False)) is True
    for key, amount in gains.items():
        if type(amount) not in {int, float} or amount < 0:
            continue
        if key == "skill_pt" and purchase_v3:
            from core.auto_trainer.larc_skill_pt_scaling import project_skill_pt
            transformed[key], pt_report = project_skill_pt(amount, pt_delta, source, target,
                old_boost=old_boost, new_boost=new_boost, old_expect=old_expect, new_expect=new_expect,
                old_levels=old_levels, new_levels=new_levels)
            unknown.extend(field for field in pt_report["unknown_fields"] if field not in unknown)
        else:
            transformed[key] = int(max(0., amount + pt_delta) * factor) if key == "skill_pt" else int(
                display_gain(current_stats.get(key, 0), raw_gain(current_stats.get(key, 0), amount) * factor * facility))
    old_discount = .8 if old_overseas and old_levels[6] == 3 else 1.
    new_discount = .8 if new_overseas and new_levels[6] == 3 else 1.
    return transformed, {"attribute_factor": round(factor * facility, 6),
        "skill_pt_factor": pt_report["relative_multiplier_estimate"] if pt_report else round(factor, 6),
        "cost_factor": new_discount / old_discount,
        "expectation_bonus_delta": expectation_delta, "unknown_fields": unknown,
        **({"skill_pt_scaling": pt_report, "skill_pt_gain_range": pt_report["gain_range"]}
           if pt_report else {})}


def numeric_at_period(state, goal, gains, period, *, attribute_context=None):
    from core.auto_trainer.larc_training_value import first_arc_speed_value
    from core.auto_trainer.larc_attribute_plan import attribute_plan, late_surplus_options
    from core.auto_trainer.larc_opportunity_forecast import marginal_gains
    from core.auto_trainer.larc_phase import phase_weights
    from core.auto_trainer.larc_user_preferences import values
    future = replace(state, scenario_state={**state.scenario_state,
        "larc_period": {"status": "verified", "value": period}})
    plan = attribute_context if attribute_context is not None else attribute_plan(state, goal)
    utility = dict(plan.get("utility", {}))
    # Use the same objective for this decision's whole comparison, including
    # its forecast. Earlier decisions retain their own original objective.
    utility=late_surplus_options(utility,(state.scenario_state.get('larc_period')or{}).get('value')or{},goal)
    if strategy_v4_enabled(state, goal):
        utility["high_stat_scarcity"] = False
    components = marginal_gains(state.current_stats, gains, plan["attributes"], **utility)
    weights = phase_weights(future,goal)
    from core.auto_trainer.larc_policy import future_skill_factor
    skill_factor = future_skill_factor(state, goal)
    attributes = sum(components[k] for k in ("base", "gap_premium", "scarcity"))
    return (attributes * weights["stats"]
            + gains.get("skill_pt", 0) * weights["skill_pt"] * skill_factor + first_arc_speed_value(future, gains)*values(goal)['numeric'])


def _expectation_rate(reference, *, domestic_only=False):
    samples = {}
    for layout in reference.get("phase_layouts", []):
        source = layout[0].get("forecast_metadata", {}) if layout else {}
        day, value = period_ordinal(source.get("period", {})), source.get("expectation")
        if (day is not None and type(value) in {int, float}
                and not (domestic_only and is_overseas(day))):
            samples[day] = value
    ordered = sorted(samples.items())
    rates = [(b[1] - a[1]) / (b[0] - a[0]) for a, b in zip(ordered, ordered[1:]) if b[1] >= a[1]]
    # Recent SS/race income is not guaranteed to repeat. No observations means
    # no invented growth curve; permanent upgrades still transform samples.
    return min(1.25, median(rates) * .5) if len(rates) >= 2 else 0.


def remaining_calendar_layouts(state, layouts):
    """Phase-weighted long-range inputs; no fixed overseas/domestic haircut."""
    from core.auto_trainer.larc_calendar import free_action_days, period_at
    day = period_ordinal(period_value(state))
    if day is None or not layouts:
        return layouts
    dates = free_action_days(day)
    if not dates:
        return layouts
    now = growth_snapshot(state)
    groups = {}
    for date in dates:
        period = period_at(date)
        groups.setdefault((period["year_index"], abroad(period)), []).append(period)
    result = []
    for (_, overseas), periods in groups.items():
        selected = [layout for layout in layouts if abroad(layout.get("period")) == overseas] or layouts
        for layout in selected:
            gains, points = {}, {}
            for tab, raw in layout["gains"].items():
                source = {**now, "period": layout.get("period", now["period"]),
                          **layout.get("forecast_metadata", {}).get(tab, {})}
                domestic = [row.get("forecast_metadata", {}).get(tab, {}) for row in layouts
                            if not abroad(row.get("period"))
                            and row.get("forecast_metadata", {}).get(tab, {}).get("training_level")]
                target = {**now, "period": periods[0],
                          "training_level": domestic[0]["training_level"] if domestic else None}
                transformed, _ = project_gains({}, {**raw, "skill_pt": layout["skill_points"].get(tab, 0)},
                                                tab, source, target)
                points[tab] = transformed.pop("skill_pt", 0)
                gains[tab] = transformed
            result.append({"gains": gains, "skill_points": points,
                           "weight": len(periods) / len(dates) / len(selected)})
    return result


def training_scenarios(state, goal, reference, window, training_cost, *, cases=None,
                       include_wisdom_reference=False):
    if not window or not window.get("action_calendar"):
        return {"active": False, "scenarios": []}
    now = growth_snapshot(state, goal)
    pool = reference.get("phase_layouts") or reference.get("opportunity_layouts", [])
    if not pool:
        return {"active": False, "scenarios": []}
    cases = cases or [{"name": "committed", "levels": now.get("levels"), "cost": 0, "weight": 1.}]
    from core.auto_trainer.larc_attribute_plan import attribute_plan
    attribute_context = attribute_plan(state, goal)
    from core.auto_trainer.larc_policy import overseas_purchase_v2_enabled
    opportunity_v2 = overseas_purchase_v2_enabled(goal)
    from core.auto_trainer import larc_policy
    purchase_v3 = getattr(larc_policy, "overseas_purchase_v3_enabled", lambda goal: False)(goal)
    v4 = strategy_v4_enabled(state, goal)
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    focused = unified_larc_enabled(goal)
    attribution = None
    if focused:
        from core.auto_trainer.larc_expectation_history import ordinary_expectation_report, with_current_transition
        inputs = state.scenario_state.get('larc_attribute_inputs', {})
        run_id = (inputs.get('run_session_id') if isinstance(inputs, dict) else None) or now.get('run_session_id')
        attributed_reference = with_current_transition(reference, now, state.turn, run_id,
            now.get('expectation_transition'))
        attribution = ordinary_expectation_report(attributed_reference, run_id=run_id,
            as_of_turn=state.turn, as_of_period=now.get('period'))
        # The generic calendar knows domestic/overseas dates, not the chosen
        # future train/SS/rest sequence. Preserve the clean empirical train
        # rate as data, but never give it to every unspecified future action.
        attribution.update(applied_growth_per_action_estimate=0.,
            application_status='withheld_without_future_training_action_binding',
            credited_future_ordinary_actions=0)
        rate = 0.
    else:
        rate = _expectation_rate(reference, domestic_only=True) if v4 else _expectation_rate(reference)
    unknown, reports = set(), []
    for case in cases:
        calendar_layouts = []
        references = []
        domestic_elapsed = 0
        for elapsed, step in enumerate(window["action_calendar"]):
            period = step["period"]
            domestic_elapsed += not abroad(period)
            matches = [layout for layout in pool if layout and
                abroad(layout[0].get("forecast_metadata", {}).get("period")) == abroad(period)]
            # On entry, one or two overseas observations must not erase the
            # mature deck's recently observed domestic rainbow layouts. Their
            # source-only effects are normalized below, not copied overseas.
            selected = (pool if opportunity_v2 and abroad(period) else matches or pool)[:8]
            projected = []
            for layout in selected:
                rows = []
                for row in layout:
                    source = {**now, **row.get("forecast_metadata", {})}
                    target = {**now, "period": period, "levels": case.get("levels") or now.get("levels"),
                              "purchase_v3": purchase_v3}
                    # Use the latest observed domestic level, not overseas Lv5.
                    domestic = [r.get("forecast_metadata", {}) for sample in pool for r in sample
                        if r["tab"] == row["tab"] and not abroad(r.get("forecast_metadata", {}).get("period"))
                        and r.get("forecast_metadata", {}).get("training_level")]
                    target["training_level"] = domestic[0]["training_level"] if domestic else None
                    if "expectation" in now:
                        delta = case.get("expectation_delta", 0.)
                        after = case.get("expectation_delta_after_ordinal")
                        day = period_ordinal(period)
                        if v4 and after is not None and (day is None or day <= after):
                            delta = 0.
                        # Ordinary domestic member income is not observed
                        # overseas. Known future race income is not invented.
                        growth_actions = 0 if focused else domestic_elapsed if v4 else elapsed + 1
                        target["expectation"] = min(200, now["expectation"] + rate * growth_actions + delta)
                        if target["training_level"] and not abroad(period):
                            thresholds = (20, 60, 100)
                            source_expect = domestic[0].get("expectation", now["expectation"])
                            target["training_level"] = min(5, target["training_level"] + sum(
                                source_expect < limit <= target["expectation"] for limit in thresholds))
                    gains, factors = project_gains(state.current_stats, row["gains"], row["tab"], source, target)
                    unknown.update(factors["unknown_fields"])
                    destination = dict(target)
                    for key in ("friendship", "friendship_basis"):
                        if key in source:
                            destination[key] = source[key]
                    effect = source.get("training_effect_percent")
                    if type(effect) in {int, float} and effect >= 0:
                        source_levels = source.get("levels") or target.get("levels") or [0] * 10
                        target_levels = target.get("levels") or source_levels
                        slot = TRAINING_SLOTS[row["tab"]]
                        source_boost = 50 if abroad(source.get("period")) and source_levels[slot] == 3 else 0
                        target_boost = 50 if abroad(period) and target_levels[slot] == 3 else 0
                        destination["training_effect_percent"] = max(0., effect + target_boost - source_boost
                            + factors["expectation_bonus_delta"])
                        destination["training_effect_percent_basis"] = "projected_destination_effect"
                    net = source.get("net_vitality_percent")
                    cost = -net if type(net) in {int, float} and net < 0 else training_cost
                    rows.append({**row, "gains": gains,
                        "numeric": round(numeric_at_period(state, goal, gains, period,
                            attribute_context=attribute_context) * row.get("forecast_discount", 1.), 4),
                        "bond": 0. if abroad(period) else row["bond"],
                        "charge": 0. if abroad(period) else row["charge"],
                        "cost": round(cost * factors["cost_factor"], 1),
                        "overseas_gain_factor": factors["attribute_factor"], "growth_projection": factors,
                        "source_period": source.get("period"),
                        "destination_metadata": destination})
                    if opportunity_v2 and abroad(period) and row['tab']=='wisdom':
                        rows[-1].update(
                            wisdom_friendship_opportunity=source.get('friendship') is True,
                            wisdom_recovery_percent=max(0., net) if type(net) in {int,float} else None,
                            wisdom_recovery_basis='observed_layout_net' if type(net) in {int,float} else 'unknown')
                projected.append(rows)
            # Keep one typical cost per tab. Historical layouts must not make
            # the finite energy search branch on dozens of obsolete costs.
            costs = {tab: median(values) for tab in TRAINING_SLOTS
                     if (values := [r["cost"] for layout in projected for r in layout if r["tab"] == tab])}
            for layout in projected:
                for row in layout:
                    row["cost"] = round(costs[row["tab"]], 1)
            calendar_layouts.append(projected)
            references.append(median(max(r["numeric"] for r in layout
                if include_wisdom_reference or r["tab"] != "wisdom") for layout in projected))
        reports.append({**case, "calendar_layouts": calendar_layouts, "numeric_references": references,
            "layouts": next((layouts for layouts, step in zip(calendar_layouts, window["action_calendar"])
                              if step["kind"] == "overseas"), calendar_layouts[0])})
    reference_values = [sum(case["weight"] * case["numeric_references"][i] for case in reports)
                        for i in range(window["steps"])]
    return {"active": True, "scenarios": reports, "unknown_fields": sorted(unknown),
        "future_numeric_by_period": [{**step, "value": round(value, 4)}
            for step, value in zip(window["action_calendar"], reference_values)],
        "numeric_training": round(sum(reference_values) / len(reference_values), 4),
        "expectation_growth_per_action_estimate": rate,
        **({'expectation_growth_attribution': attribution,
            'expectation_growth_basis': 'current_observed_expectation_plus_explicit_action_cases; empirical_train_rate_withheld_without_future_action_binding'} if focused else {}),
        **({"expectation_growth_basis": "domestic_actions_only; overseas_natural_income_not_assumed"} if v4 and not focused else {}),
        "basis": "destination-phase layouts; normalized facilities, expectation and owned effects; no promised rainbow",
        "probability_status": "discounted observed opportunities and conditional purchase scenarios",
        "overseas_layout_selection": "recent_complete_layouts_normalized_to_destination" if opportunity_v2
            else "same_phase_when_available"}


def ss_expectation_value(state, goal, preview, *, deferred_actions=None):
    """Small value of gaining expectation now rather than in a later SS.

The 0.5% per winner (double for SSS) is a guide-derived planning estimate.
Only the next two opportunities are credited, with a further 50% discount;
it is not a lifetime training bonus attached to every charge and every SS.
"""
    if strategy_v4_enabled(state, goal):
        return _ss_expectation_value_v4(state, goal, preview, deferred_actions=deferred_actions)
    from core.auto_trainer.larc_recovery_routes import recovery_window
    from core.auto_trainer.larc_recovery import estimated_training_cost
    from core.auto_trainer.larc_training_value import _future_numeric_values
    now, window = growth_snapshot(state), recovery_window(state)
    count = preview.get("ready_count")
    if "expectation" not in now or not window or type(count) is not int or not 1 <= count <= 5:
        return {"score": 0., "reason": "expectation_or_future_window_unknown"}
    delta = count * (.5 if preview.get("kind") == "SS" else 1.)
    reference = _future_numeric_values(state, goal)
    cost, _ = estimated_training_cost(state)
    forecast = training_scenarios(state, goal, reference, window, cost, cases=[
        {"name": "later", "weight": .5}, {"name": "now", "weight": .5, "expectation_delta": delta}])
    if not forecast["active"]:
        return {"score": 0., "reason": "future_layouts_unknown"}
    first, second = forecast["scenarios"]
    benefits = [max(0., b - a) * .5 * .95 ** i for i, (a, b) in enumerate(zip(
        first["numeric_references"][:2], second["numeric_references"][:2]))]
    return {"score": round(sum(benefits), 4), "expectation_delta_estimate": delta,
            "credited_future_actions": len(benefits), "credit": .5,
            "basis": "earlier expectation benefit only; remaining two-action window; guide estimate"}


def _ss_expectation_value_v4(state, goal, preview, *, deferred_actions=None):
    """Marginal expectation timing over the remaining actual action calendar.

    The comparison changes only when the current SS's extra expectation starts.
    Already owned expectation effects cancel. A delayed SS needs a domestic
    action; after the last one its expectation cannot be redeemed overseas.
    """
    from core.auto_trainer.larc_calendar import free_action_days, period_at
    from core.auto_trainer.larc_recovery import estimated_training_cost
    from core.auto_trainer.larc_training_value import _future_numeric_values
    now = growth_snapshot(state)
    count = preview.get("ready_count")
    day = period_ordinal(now.get("period", {}))
    if ("expectation" not in now or day is None or is_overseas(day)
            or type(count) is not int or not 1 <= count <= 5):
        return {"score": 0., "reason": "expectation_or_executable_domestic_ss_unknown"}
    horizon = state.scenario_state.get("larc_ss_planning_horizon",
        getattr(goal, "larc_policy", {}).get("ss_planning_horizon", "full_career"))
    days = free_action_days(day + 1, 23) if horizon == "junior_cutoff_discard" else free_action_days(day + 1)
    calendar = [{"period": period_at(value), "kind": "overseas" if is_overseas(value) else "domestic"}
        for value in days]
    if not calendar:
        return {"score": 0., "reason": "no_future_training_calendar", "credited_future_actions": 0}
    # Reuse an existing held-SS route's delay when supplied. The conservative
    # fallback is the next feasible domestic action, never an overseas SS.
    requested = max(1, deferred_actions) if type(deferred_actions) is int else 1
    eligible = [value for index, value in enumerate(days, 1)
        if index >= requested and not is_overseas(value)]
    deferred_day = eligible[0] if eligible else None
    delta = count * (.5 if preview.get("kind") == "SS" else 1.)
    later = {"name": "later", "weight": .5}
    if deferred_day is not None:
        later.update(expectation_delta=delta, expectation_delta_after_ordinal=deferred_day)
    reference = _future_numeric_values(state, goal)
    cost, _ = estimated_training_cost(state)
    window = {"steps": len(days), "action_calendar": calendar}
    forecast = training_scenarios(state, goal, reference, window, cost, cases=[later,
        {"name": "now", "weight": .5, "expectation_delta": delta}], include_wisdom_reference=True)
    if not forecast["active"]:
        return {"score": 0., "reason": "future_layouts_unknown"}
    first, second = forecast["scenarios"]
    benefits = [{**step, "before": a, "after": b,
        "score": round(max(0., b - a) * .5 * .95 ** index, 6)}
        for index, (step, a, b) in enumerate(zip(calendar,
            first["numeric_references"], second["numeric_references"]))]
    return {"score": round(sum(row["score"] for row in benefits), 4),
        "expectation_delta_estimate": delta, "expectation_delta_basis": "guide_estimate_per_ss_winner",
        "credited_future_actions": len(benefits), "credit": .5,
        "deferred_ss_available": deferred_day is not None,
        "deferred_ss_period": period_at(deferred_day) if deferred_day is not None else None,
        "deferred_actions_requested": requested, "benefits_by_period": benefits,
        "expectation_growth_per_domestic_action_estimate": forecast["expectation_growth_per_action_estimate"],
        **({'expectation_growth_attribution': forecast['expectation_growth_attribution']}
           if 'expectation_growth_attribution' in forecast else {}),
        "basis": "current_observed_expectation; full_calendar_increment_vs_feasible_domestic_delay; no_overseas_natural_growth",
        "unknown_fields": forecast["unknown_fields"]}
