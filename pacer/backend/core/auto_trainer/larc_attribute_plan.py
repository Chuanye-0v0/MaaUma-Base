"""Soft, run-local attribute planning. No device calls or guaranteed random income.

All five attributes share one training budget. Each sampled layout is evaluated
before averaging. Forecasts are ranges, not a fixed future training allocation.
The constants below are policy estimates to tune in replays, not game rules.
"""
from __future__ import annotations

from contextvars import ContextVar
from functools import lru_cache, wraps
import json
import math

from core.auto_trainer.larc_recovery import period_value
from core.auto_trainer.larc_phase import PHASE_WEIGHTS, phase
from core.auto_trainer.larc_calendar import free_action_days, period_ordinal as _ordinal
from core.auto_trainer.larc_opportunity_forecast import (
    MAX_GAP_PREMIUM, display_gain as _display_gain, raw_gain as _raw_gain,
    marginal_gains, project_opportunities, soft_gaps,
)
from core.auto_trainer.state import TRAINING_TYPES

VERSION = "larc_attribute_plan.v3"
CONDITIONAL_CREDIT = .5
_evaluation_plans = ContextVar('larc_evaluation_plans', default=None)


def planning_evaluation(func):
    """Reuse a frozen state/goal within one synchronous scoring evaluation.

    Keep strong references to prevent identity reuse. The cache never crosses
    an evaluation, a game action, or a new observation. Nested SS comparisons
    may share it; hypothetical replacement states get separate entries.
    """
    @wraps(func)
    def wrapped(*args, **kwargs):
        if _evaluation_plans.get() is not None:
            return func(*args, **kwargs)
        token = _evaluation_plans.set({})
        try:
            return func(*args, **kwargs)
        finally:
            _evaluation_plans.reset(token)
    return wrapped


def _number(value):
    return type(value) in {int, float} and math.isfinite(value)


def _vector(values=None):
    values = values if isinstance(values, dict) else {}
    return {key: float(values[key]) if _number(values.get(key)) else 0. for key in TRAINING_TYPES}


def _round(values):
    return {key: round(value, 3) for key, value in values.items()}


def _abroad(period):
    month = period.get("month")
    return period.get("year_index") in {2, 3} and type(month) is int and 7 <= month <= 9


def _usable(candidate):
    if candidate.failure_rate is None:
        return False
    gains = candidate.stats_gain
    legacy_usable = candidate.confidence >= .65 and any(
        type(gains.get(key)) is int and gains[key] > 0 for key in TRAINING_TYPES)
    if not legacy_usable:
        from core.auto_trainer.larc_candidate_quality import usable_numeric_evidence
        # History writes a complete five-attribute vector. Omitted columns
        # must be verified zero when admitting a page through numeric evidence.
        if not usable_numeric_evidence(candidate, require_complete_attributes=True):
            return False
    return not any(row.get("occupancy") == "present" and row.get("status") != "accepted"
                   for column in candidate.raw_data.get("stats_gain_ocr", {}).get("columns", [])
                   for row in column.get("rows", []))


def free_windows(state, goal, inputs):
    period = period_value(state)
    current = _ordinal(period)
    explicit = inputs.get("remaining_free_actions")
    if type(explicit) is int and 0 <= explicit <= goal.total_turns:
        return explicit, "run_plan_including_current"
    if current is not None:
        return len(free_action_days(current)), "observed_calendar_including_current"
    # Pre-debut / missing half-month: action indices are not calendar dates.
    # Keep a labelled upper estimate until the normal date reader resolves it.
    return max(0, goal.total_turns - (state.turn or 1) + 1 - 5), "action_index_estimate"


def _run_id(state):
    ids = {item.raw_data.get("capture", {}).get("run_session_id") for item in state.training_options}
    ids.discard(None)
    return next(iter(ids)) if len(ids) == 1 else None


def _inputs(state):
    packet = state.scenario_state.get("larc_attribute_inputs", {})
    if not isinstance(packet, dict):
        return {}, ["attribute_inputs_invalid"]
    run = _run_id(state)
    if packet and (not run or packet.get("run_session_id") != run):
        return {}, ["attribute_inputs_run_mismatch"]
    through = packet.get("through_turn")
    if type(through) is int and type(state.turn) is int and through > state.turn:
        return {}, ["attribute_inputs_from_future_turn"]
    return packet, []


def _effects(effects):
    result = _vector()
    for effect in effects if isinstance(effects, list) else []:
        if (isinstance(effect, dict) and effect.get("kind") in result
                and effect.get("op") == "add" and _number(effect.get("value"))):
            result[effect["kind"]] += effect["value"]
    return result


def _reward_scenarios(row):
    if isinstance(row.get("stats_gain"), dict):
        return [(_vector(row["stats_gain"]), 1.)]
    # U3 emits fixed effects and conditional outcomes as separate rows for the
    # same option. Add the fixed part to each outcome; never sum alternatives.
    groups = {}
    for alternative in row.get("alternatives", []):
        if isinstance(alternative, dict):
            groups.setdefault(alternative.get("option_id"), []).append(alternative)
    scenarios = []
    for alternatives in groups.values():
        fixed = _vector()
        branches = []
        for alternative in alternatives:
            effects = _effects(alternative.get("effects", alternative.get("attribute_effects", [])))
            if alternative.get("branch_basis") == "fixed_choice_effect":
                fixed = {key: fixed[key] + effects[key] for key in fixed}
            else:
                branches.append((effects, alternative.get("probability")))
        if not branches:
            scenarios.append((fixed, 1. if len(groups) == 1 else None))
        else:
            known_distribution = len(groups) == 1 and all(
                _number(probability) and 0 <= probability <= 1 for _, probability in branches)
            known_distribution = known_distribution and abs(sum(p for _, p in branches) - 1.) < .001
            for effects, probability in branches:
                scenarios.append(({key: fixed[key] + effects[key] for key in fixed},
                                  probability if known_distribution else None))
    return scenarios


def remaining_rewards(state, inputs):
    guaranteed, expected, possible = (_vector() for _ in range(3))
    details, unknown = [], []
    rows = list(inputs.get("remaining_rewards", []))
    summary = state.scenario_state.get("larc_event_progress", {})
    if (isinstance(summary, dict) and summary.get("run_id") == _run_id(state)
            and not (type(summary.get("current_turn_index")) is int and type(state.turn) is int
                     and summary["current_turn_index"] > state.turn)):
        rows.extend(summary.get("remaining_rewards", []))
    seen = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        identity = row.get("reward_id", row.get("event_id", f"row:{index}"))
        if identity in seen:
            continue
        seen.add(identity)
        if row.get("realized") is True or row.get("status") in {"realized", "settled", "observed"}:
            continue
        due = row.get("due_turn")
        due_date = _ordinal(row.get("due_period", {}))
        now = _ordinal(period_value(state))
        if (type(due) is int and type(state.turn) is int and due < state.turn
                or due_date is not None and now is not None and due_date < now):
            continue
        scenarios = _reward_scenarios(row)
        if not scenarios or row.get("effect_status") == "unknown":
            unknown.append(str(identity))
            continue
        low = {key: min(values[key] for values, _ in scenarios) for key in guaranteed}
        high = {key: max(values[key] for values, _ in scenarios) for key in guaranteed}
        trigger = row.get("trigger_probability")
        if row.get("trigger_status") == "guaranteed":
            trigger = 1.
        known_trigger = _number(trigger) and 0 <= trigger <= 1
        # Exact amounts in a reference entry do not prove the future trigger,
        # game version, selected option or random branch will be that amount.
        confirmed = row.get("effect_status") in {"confirmed", "observed_rule", "accepted"}
        floor = low if trigger == 1. and confirmed else _vector()
        distribution_known = all(_number(probability) for _, probability in scenarios)
        mean = ({key: sum(values[key] * probability for values, probability in scenarios)
                 for key in guaranteed} if distribution_known else None)
        credit = ({key: mean[key] * trigger - floor[key] for key in guaranteed}
                  if known_trigger and mean is not None else _vector())
        for key in guaranteed:
            guaranteed[key] += floor[key]
            expected[key] += credit[key]
            possible[key] += max(0., high[key] - floor[key])
        details.append({"reward_id": identity, "guaranteed": _round(floor),
                        "conditional_expected": _round(credit), "possible": _round(high),
                        "trigger_probability": trigger if known_trigger else None,
                        "trigger_status": row.get("trigger_status", "unknown"),
                        "effect_status": row.get("effect_status", "unknown")})
    return {"guaranteed": _round(guaranteed), "conditional_expected": _round(expected),
            "conditional_possible": _round(possible), "sources": details,
            "unknown_sources": unknown,
            "fixed_race_reward_coverage": inputs.get("fixed_race_reward_coverage", "unknown")}


def _opportunity_data(state, inputs):
    """Keep joint layouts; averaging each tab first invents a new layout."""
    period = period_value(state)
    current = {candidate.training_type: candidate for candidate in state.training_options if _usable(candidate)}
    now = {tab: {key: _raw_gain(state.current_stats.get(key, 0), max(0., candidate.stats_gain.get(key, 0)))
                 for key in TRAINING_TYPES} for tab, candidate in current.items()}
    points = {tab: max(0., candidate.stats_gain.get("skill_pt", 0) or 0.) for tab, candidate in current.items()}
    from core.auto_trainer.larc_expedition_forecast import forecast_metadata
    layouts = [{"gains": now, "skill_points": points, "period": period,
                "forecast_metadata": {tab: forecast_metadata(candidate, state) for tab, candidate in current.items()}}] if now else []
    seen_turns, estimated_pt = set(), False
    for sample in reversed(inputs.get("training_history", [])):
        turn = sample.get("turn_index")
        if type(turn) is not int or type(state.turn) is not int or not 0 < state.turn - turn <= 12 or turn in seen_turns:
            continue
        old = sample.get("period", {})
        if old.get("year_index") != period.get("year_index"):
            continue
        seen_turns.add(turn)
        gains = {tab: _vector(row) for tab, row in sample.get("raw_gains", {}).items()
                 if tab in TRAINING_TYPES and isinstance(row, dict)}
        old_points = {}
        for tab, row in gains.items():
            pt = sample.get("skill_points", {}).get(tab)
            if not _number(pt) or pt < 0:
                # Old snapshots omitted Pt. Label the estimate instead of
                # silently pretending their skills contributed nothing.
                ratio = min(2., sum(row.values()) / max(1., sum(now.get(tab, {}).values())))
                pt = points.get(tab, 0.) * ratio
                estimated_pt = True
            old_points[tab] = pt
        if gains:
            layouts.append({"gains": gains, "skill_points": old_points, "period": old,
                            "forecast_metadata": sample.get("forecast_metadata", {})})
        if len(seen_turns) >= 12:
            break
    from core.auto_trainer.larc_training_forecast import remaining_calendar_layouts
    observed_counts = {tab: sum(tab in layout["gains"] for layout in layouts) for tab in TRAINING_TYPES}
    observed_layout_count = len(layouts)
    layouts = remaining_calendar_layouts(state, layouts)
    rates, counts = {}, {}
    for tab in TRAINING_TYPES:
        rows = [layout["gains"][tab] for layout in layouts if tab in layout["gains"]]
        if rows:
            discount = .65 if observed_counts[tab] == 1 else .85
            total_weight = sum(layout.get("weight", 1.) for layout in layouts if tab in layout["gains"])
            rates[tab] = {key: sum(layout["gains"][tab][key] * layout.get("weight", 1.)
                                  for layout in layouts if tab in layout["gains"]) / total_weight * discount
                          for key in TRAINING_TYPES}
            counts[tab] = observed_counts[tab]
    discount = .65 if observed_layout_count < 4 else .85
    scenarios = [{"gains": {tab: {key: amount * discount for key, amount in row.items()}
                             for tab, row in layout["gains"].items()},
                  "skill_points": {tab: amount * discount for tab, amount in layout["skill_points"].items()},
                  "weight": layout.get("weight", 1.)}
                 for layout in layouts]
    return rates, counts, scenarios, estimated_pt


def late_surplus_options(utility, period, goal):
    """Late-career (d63, d65) far-surplus credit adjustment parameterized via LarcStrategyParams."""
    from core.auto_trainer.larc_calendar import period_ordinal
    from core.auto_trainer.larc_strategy_extension import active_provider
    from core.auto_trainer.larc_strategy_params import params_for_goal, unified_larc_enabled

    value = dict(utility)
    value.pop("far_surplus_credit", None)
    value.pop("far_surplus_buffer", None)
    value.pop("far_surplus_exact_buffer", None)
    params = params_for_goal(goal)
    ord_now = period_ordinal(period or {})
    if unified_larc_enabled(goal) and value.get("attribute_weights") is not None:
        ext = active_provider().late_surplus_options(params, value, ord_now)
        if ext is not None:
            return {**value, **ext}
        if value.get("after_target_credit") == params.default_after_target_credit:
            if ord_now in params.late_far_surplus_ordinals:
                value.update(
                    far_surplus_credit=params.late_far_surplus_credit,
                    far_surplus_buffer=params.late_far_surplus_buffer,
                )
    return value


def attribute_plan(state, goal):
    cache = _evaluation_plans.get()
    key = (id(state), id(goal))
    if cache is not None and key in cache:
        return cache[key][2]
    result = _attribute_plan(state, goal)
    if cache is not None:
        cache[key] = (state, goal, result)
    return result


def _attribute_plan(state, goal):
    inputs, input_unknown = _inputs(state)
    windows, basis = free_windows(state, goal, inputs)
    rewards = remaining_rewards(state, inputs)
    cache = _evaluation_plans.get()
    opp_key = None
    if cache is not None:
        try:
            opp_key = (
                "opp",
                state.turn,
                tuple((k, (state.current_stats or {}).get(k, 0)) for k in TRAINING_TYPES),
                tuple(id(c) for c in (state.training_options or ())),
                id(state.scenario_state),
                id(inputs.get("training_history")),
            )
        except Exception:
            opp_key = None
    if cache is not None and opp_key is not None and opp_key in cache:
        rates, counts, layouts, estimated_pt = cache[opp_key]
    else:
        rates, counts, layouts, estimated_pt = _opportunity_data(state, inputs)
        if cache is not None and opp_key is not None:
            cache[opp_key] = (rates, counts, layouts, estimated_pt)
    weights = PHASE_WEIGHTS[phase(state)]
    from core.auto_trainer.larc_policy import attribute_options, future_skill_factor
    utility = attribute_options(goal)
    from core.auto_trainer.larc_timeline_workflow import resolve
    arrangement = resolve(state, goal)
    if arrangement.get("status") == "matched":
        effects = arrangement["effects"]
        if effects.get("attribute_weights"):
            # Attribute gains share one utility scale across training, SS and
            # optional upgrade comparisons. Training-type multipliers remain
            # a separate final-score preference and never reapply these here.
            utility["attribute_weights"] = {
                **utility.get("attribute_weights", {}), **effects["attribute_weights"]}
        if "after_target_credit" in effects:
            utility.setdefault("attribute_weights", {})
            utility["after_target_credit"] = effects["after_target_credit"]
    utility=late_surplus_options(utility,period_value(state),goal)
    skill_factor = future_skill_factor(state, goal)
    # Layouts have already been normalized by the shared calendar/effect model.
    # Retain this diagnostic field without applying another blanket discount.
    future_scale = 1.
    # Key only the numerical planning inputs. UI/SS evidence changes do not
    # trigger another long projection for each candidate and energy comparison.
    from core.auto_trainer.larc_strategy_params import params_for_goal
    from core.auto_trainer.larc_strategy_rules import effective_stamina_target, year3_shortfall_payload
    params = params_for_goal(goal)
    shortfall_cfg = year3_shortfall_payload(state, goal)
    eff_sta = effective_stamina_target(goal) if params.year3_shortfall_balance_enabled else None
    payload = {"current": _vector(state.current_stats), "target": _vector(goal.target_stats),
               "windows": windows, "window_basis": basis, "vitality": state.vitality,
               "reserved": inputs.get("reserved_recovery_actions", 0),
               "rates": rates, "sample_counts": counts, "rewards": rewards,
               "layouts": layouts, "historical_skill_pt_estimated": estimated_pt,
               "pt_weight": weights["skill_pt"] * skill_factor / weights["stats"],
               "utility": utility,
               "future_phase_yield_scale": future_scale,
               "stage_pressure_weight": {1: .45, 2: .75}.get(period_value(state).get("year_index"), 1.),
               "strategy22_shortfall": shortfall_cfg,
               "strategy22_eff_stamina_target": eff_sta,
               "input_unknown": input_unknown}
    return _plan_cached(json.dumps(payload, sort_keys=True))


@lru_cache(maxsize=32)
def _plan_cached(serialized):
    from core.auto_trainer.larc_strategy_rules import (
        shortfall_premium as rule_shortfall_premium,
        surplus_scarcity_factors as rule_surplus_scarcity_factors,
    )
    data = json.loads(serialized)
    current, target = data["current"], data["target"]
    eff_sta = data.get("strategy22_eff_stamina_target")
    plan_target = dict(target)
    if eff_sta is not None:
        plan_target["stamina"] = float(eff_sta)
    rates = {tab: {key: value * data["future_phase_yield_scale"] for key, value in row.items()}
             for tab, row in data["rates"].items()}
    layouts = [{"gains": {tab: {key: value * data["future_phase_yield_scale"] for key, value in row.items()}
                           for tab, row in layout["gains"].items()},
                "skill_points": {tab: value * data["future_phase_yield_scale"]
                                 for tab, value in layout["skill_points"].items()},
                "weight": layout.get("weight", 1.)}
               for layout in data["layouts"]]
    windows = data["windows"]
    reserved = data["reserved"] if _number(data["reserved"]) else 0.
    # One action now is available. Later SS / outings / rests consume a share
    # of calendar windows; recovery training is still part of the training pool.
    future = max(0., windows - 1 - max(0., reserved))
    vitality = data["vitality"] if _number(data["vitality"]) else 50
    recovery_debt = min(1., max(0., 35 - vitality) / 50)
    budget = min(windows, max(0., (1. if windows else 0.) + future * .8 - recovery_debt))
    rewards = data["rewards"]
    income = {key: rewards["guaranteed"][key] + CONDITIONAL_CREDIT * rewards["conditional_expected"][key]
              for key in current}
    common = dict(stage_weight=data["stage_pressure_weight"], pt_weight=data["pt_weight"], **data.get("utility",{}))
    base = project_opportunities(current, plan_target, rates, layouts, budget, rewards=income, **common)
    low = project_opportunities(current, plan_target, rates, layouts, max(0., budget * .8),
                                scale=.75, rewards=rewards["guaranteed"], **common)
    high_income = {key: rewards["guaranteed"][key] + rewards["conditional_possible"][key] for key in current}
    high = project_opportunities(current, plan_target, rates, layouts, min(windows, budget * 1.1),
                                 scale=1.2, rewards=high_income, **common)
    gaps = soft_gaps(current, plan_target, rates, budget, data["stage_pressure_weight"], income)
    shortfall_cfg = data.get("strategy22_shortfall")
    attributes = {}
    for key in current:
        eff_t = plan_target[key]
        gap = max(0., eff_t - current[key] - income[key])
        collateral = min(gap, base["collateral"][key])
        active = gaps[key]["active_gap"]
        needed = gaps[key]["estimated_trainings_needed"]
        residual = max(0., eff_t - base["stats"][key])
        premium = gaps[key]["gap_premium"]
        if isinstance(shortfall_cfg, dict) and shortfall_cfg.get("active"):
            credit = float((shortfall_cfg.get("shortfall_credits") or {}).get(key, 0.0))
            adj_residual = max(0., eff_t - (base["stats"][key] + credit))
            premium = rule_shortfall_premium(
                premium, adj_residual, data["stage_pressure_weight"], key=key, cfg=shortfall_cfg
            )
        attributes[key] = {"current": current[key], "target": target[key],
            **({"effective_target": eff_t} if eff_t != target[key] else {}),
            "remaining_guaranteed": rewards["guaranteed"][key],
            "remaining_conditional_credit": round(income[key] - rewards["guaranteed"][key], 3),
            "gap_after_remaining_rewards": round(gap, 3), "expected_collateral": round(collateral, 3),
            "active_gap": round(active, 3), "estimated_trainings_needed": round(needed, 3) if needed is not None else None,
            "projected_shortfall": round(residual, 3), "gap_premium": round(premium, 4),
            "projected_final": {"low": min(low["stats"][key], base["stats"][key]),
                                "estimate": base["stats"][key],
                                "high": max(high["stats"][key], base["stats"][key])}}
    scarcity_factors = (
        rule_surplus_scarcity_factors(attributes, shortfall_cfg)
        if isinstance(shortfall_cfg, dict) and shortfall_cfg.get("active")
        else {}
    )
    return {"version": VERSION, "utility": data.get("utility",{}), "remaining_free_actions": windows, "window_basis": data["window_basis"],
            "estimated_training_budget": round(budget, 3), "allocated_training_actions": base["allocation"],
            "allocation_basis": "expected_opportunity_shares_not_prescribed_actions",
            "attributes": attributes, **({"scarcity_factors": scarcity_factors} if scarcity_factors else {}),
            "remaining_rewards": rewards, "raw_training_rates": rates,
            "opportunity_layouts": layouts, "opportunity_layout_count": len(layouts),
            "historical_skill_pt_estimated": data["historical_skill_pt_estimated"],
            "forecast_pt_weight": data["pt_weight"],
            "sample_counts": data["sample_counts"], "future_income_credit": income,
            "future_phase_yield_scale": round(data["future_phase_yield_scale"], 4),
            "stage_pressure_weight": data["stage_pressure_weight"],
            "input_unknown": data["input_unknown"], "forecast_basis": "choose_within_empirical_layouts_then_average_shared_budget",
            "gap_score_basis": "observed_deficit_remaining_rewards_and_bounded_opportunity_pressure",
            "limitations": ["policy_range_not_confidence_interval", "future_layouts_and_race_results_unknown",
                            "unlisted_fixed_rewards_not_assumed", "targets_are_preferences_not_stat_caps",
                            "long_range_nontraining_allowance_is_an_estimate_short_energy_routes_are_compared_separately",
                            "future_deck_growth_upgrades_and_unobserved_stat_caps_unknown"]}


def score_attribute_gains(state, gains, goal, *, project=True):
    plan = attribute_plan(state, goal)
    observed = {key: amount for key, amount in gains.items()
                if key in TRAINING_TYPES and type(amount) is int and amount >= 0}
    components = marginal_gains(state.current_stats, observed, plan["attributes"],
                                scarcity_factors=plan.get("scarcity_factors"), **plan.get("utility",{}))
    if not project:
        return {**components, "plan": plan, "projected_final_after_action": None}
    after = {key: state.current_stats.get(key, 0) + max(0, gains.get(key, 0)) for key in TRAINING_TYPES}
    projection = project_opportunities(after, _vector(goal.target_stats), plan["raw_training_rates"],
        plan["opportunity_layouts"], max(0., plan["estimated_training_budget"] - 1),
        rewards=plan["future_income_credit"], stage_weight=plan["stage_pressure_weight"],
        pt_weight=plan["forecast_pt_weight"], **plan.get("utility",{}))
    return {**components, "plan": plan,
            "projected_final_after_action": projection["stats"]}


def history_sample(state):
    """Compact natural collection record; no outcomes or invented observations."""
    rows = {}
    for candidate in state.training_options:
        if _usable(candidate):
            rows[candidate.training_type] = {
                key: _raw_gain(state.current_stats.get(key, 0), max(0., candidate.stats_gain.get(key, 0)))
                for key in TRAINING_TYPES}
    from core.auto_trainer.larc_training_value import score_training_progress
    progress = {candidate.training_type: score_training_progress(state, candidate)["forecast_units"]
                for candidate in state.training_options if _usable(candidate)}
    from core.auto_trainer.larc_expedition_forecast import forecast_metadata
    from core.auto_trainer.larc_mei_contact import mei_present
    return {"turn_index": state.turn, "period": period_value(state), "raw_gains": rows,
            "mei_available": any(mei_present(candidate) for candidate in state.training_options if _usable(candidate)),
            "forecast_metadata": {candidate.training_type: forecast_metadata(candidate, state)
                                  for candidate in state.training_options if _usable(candidate)},
            "progress_units": progress, "skill_points": {candidate.training_type: candidate.stats_gain.get("skill_pt")
                             for candidate in state.training_options if _usable(candidate)}}
