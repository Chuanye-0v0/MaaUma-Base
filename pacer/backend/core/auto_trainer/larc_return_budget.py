"""Last first-expedition free action vs known domestic return opportunities.

Only current typed state and strictly earlier domestic layouts are consumed.
No unbought adaptation, future Mei, held/new SS, or race income is credited.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from functools import lru_cache
from math import isfinite

from core.auto_trainer.larc_strategy_params import params_for_goal, unified_larc_enabled
from core.auto_trainer.rejection_policy import is_hard_rejected

SOLVER_VERSION = "u2exp-20261102-energy-order-c1"
VERSION = "firstcamp-return-current-rest-three-four-agreement-v1"
PURCHASE_DEPTH = ContextVar("larc_return_budget_purchase_depth", default=0)

DROP = frozenset({
    "vitality",
    "recovery",
    "known_recovery",
    "rule_recovery",
    "verified_cost_reduction",
    "energy_continuity",
    "failure_expected_loss",
    "failure_penalty",
    "risk_aversion_preference",
    "outing_chain_progress",
    "unused_outing_recovery",
    "recovery_timing_preference",
})


def number(value):
    return type(value) in (int, float) and isfinite(value)


@contextmanager
def purchase_scope():
    """Wrap the complete purchase recommendation to suppress recursive return checks."""
    token = PURCHASE_DEPTH.set(PURCHASE_DEPTH.get() + 1)
    try:
        yield
    finally:
        PURCHASE_DEPTH.reset(token)


def enabled(goal):
    return unified_larc_enabled(goal)


def _apply_energy(hp, cap, delta, cap_delta=0.0):
    maximum = min(120.0, cap + max(0.0, cap_delta))
    return min(maximum, max(0.0, hp + delta)), maximum


def _terminal(hp, common_cap, weight):
    bar = hp * 100 / common_cap
    return weight * (2 * bar - 0.008 * bar * bar)


def evaluate_budget(data, *, risk_quote, future_count=2, held_reward_provider=None,
                    after_income_training_projector=None, initial_finance_state=None):
    """Current action + two or three future actions, same stock per branch."""
    if type(future_count) is not int or future_count not in (2, 3):
        raise ValueError("future_count_must_be_two_or_three")
    hp, cap = data["hp"], data["cap"]
    if not all(number(v) for v in (hp, cap)) or not 0 <= hp <= cap <= 120 or cap <= 0:
        return {"valid": False, "reason": "current_energy_unavailable"}
    layouts = data.get("layouts") or []
    if not layouts or any(not layout for layout in layouts):
        return {"valid": False, "reason": "shared_past_layout_distribution_unavailable"}
    outings, held = data.get("outings") or [], data.get("held")
    discount = data.get("discount", 1.0)
    fixed = data.get("fixed_after") or [0.0] * (future_count + 1)
    domestic = data.get("future_domestic") or [True] * future_count
    if len(fixed) < future_count + 1 or len(domestic) < future_count:
        return {"valid": False, "reason": "action_calendar_shorter_than_requested_budget"}
    end_weight = data["terminal_weight"]
    reserve = data["reserve_percent"]
    hp_discount = data["low_hp_discount"]
    fail_limit = data["failure_limit"]
    INVALID = -1e30
    try:
        hash(initial_finance_state)
    except TypeError:
        return {"valid": False, "reason": "immutable_branch_finance_state_required"}

    def key(value):
        return round(value, 5)

    def redeem(finance, action_index):
        reward = held
        if held_reward_provider is not None:
            result = held_reward_provider(finance, action_index, dict(held))
            reward = {**held, **result.get("reward", {})}
            finance = result.get("finance_state", finance)
            hash(finance)
        return reward, finance

    @lru_cache(maxsize=None)
    def solve(index, points, maximum, outing_index, held_left, finance):
        if index == future_count:
            if held_left:
                return INVALID, (), None, None
            return _terminal(points, cap, end_weight), (), finance, (points, maximum)

        def following(action_id, value, delta, *, cap_delta=0.0, next_outing=outing_index,
                      next_held=held_left, next_finance=finance):
            after, bound = _apply_energy(points, maximum, delta, cap_delta)
            after, bound = _apply_energy(after, bound, fixed[index + 1])
            later, path, final_finance, end_energy = solve(
                index + 1, key(after), key(bound), next_outing, next_held, next_finance
            )
            if later == INVALID:
                return INVALID, (), None, None
            return value + discount * later, (action_id,) + path, final_finance, end_energy

        common = []
        mei_available = domestic[index] and outing_index < len(outings)
        if mei_available:
            stage = outings[outing_index]
            common.append(following("mei:" + str(stage["stage"]), stage["value"], stage["heal"],
                                    next_outing=outing_index + 1))
        else:
            scenarios = data["rest_scenarios"] if domestic[index] else [(50.0, 1.0)]
            outcomes = [(weight, following("rest", 0.0, amount)) for amount, weight in scenarios]
            if all(result[0] != INVALID for _, result in outcomes):
                common.append((
                    sum(weight * result[0] for weight, result in outcomes),
                    outcomes[len(outcomes) // 2][1][1],
                    outcomes[len(outcomes) // 2][1][2],
                    outcomes[len(outcomes) // 2][1][3],
                ))
        if domestic[index] and held_left and points >= held["future_min_hp"]:
            reward, next_finance = redeem(finance, index + 1)
            common.append(following("held:ss", reward["value"], reward["heal"],
                                    cap_delta=reward["cap_delta"], next_held=False,
                                    next_finance=next_finance))

        outcomes = []
        energy = points * 100 / maximum
        readiness = 1.0 - hp_discount * min(1.0, max(0.0, reserve - energy) / max(1.0, reserve))
        for layout in layouts:
            choices = list(common)
            for row in layout:
                next_finance = finance
                if held and not held_left and after_income_training_projector is not None:
                    projected = after_income_training_projector(dict(row), finance, index + 1)
                    row = {**row, **projected.get("row", {})}
                    next_finance = projected.get("finance_state", finance)
                    hash(next_finance)
                if row["delta"] < 0 and points + row["delta"] < 0:
                    continue
                risk = risk_quote(row["value"], row.get("risk_metadata") or {}, energy)
                if risk.get("failure_rate") is not None and risk["failure_rate"] > fail_limit:
                    continue
                preference_cost = risk.get("preference_cost", 0.0)
                expected = (risk["value"] + preference_cost) * readiness - preference_cost
                choices.append(following("train:" + row["tab"], expected, row["delta"],
                                        next_finance=next_finance))
            feasible = [item for item in choices if item[0] != INVALID]
            if not feasible:
                return INVALID, (), None, None
            outcomes.append(max(feasible, key=lambda item: item[0]))
        representative = sorted(outcomes, key=lambda item: item[0])[len(outcomes) // 2]
        return sum(row[0] for row in outcomes) / len(outcomes), representative[1], representative[2], representative[3]

    rows = []
    origin = _terminal(hp, cap, end_weight)
    for first in data["current_actions"]:
        kind = first["kind"]
        if kind == "mei" and (not outings or first.get("stage") != outings[0]["stage"]):
            continue
        if kind == "ss" and not held:
            continue
        outcomes = first.get("outcomes") or [(first["delta"], 1.0)]
        branches = []
        first_value = first["value"]
        finance = initial_finance_state
        cap_delta = first.get("cap_delta", 0.0)
        if kind == "ss":
            reward, finance = redeem(finance, 0)
            first_value, cap_delta = reward["value"], reward["cap_delta"]
            outcomes = [(reward["heal"], 1.0)]
        for delta, weight in outcomes:
            after, maximum = _apply_energy(hp, cap, delta, cap_delta)
            after, maximum = _apply_energy(after, maximum, fixed[0])
            tail, path, final_finance, end_energy = solve(
                0, key(after), key(maximum), int(kind == "mei"), bool(held) and kind != "ss", finance
            )
            branches.append((weight, tail, (first["action_id"],) + path, after, final_finance, end_energy))
        if any(row[1] == INVALID for row in branches):
            continue
        score = first_value + discount * sum(row[0] * row[1] for row in branches) - origin
        representative = branches[len(branches) // 2]
        rows.append({
            "action_id": first["action_id"],
            "sequence_score": score,
            "current_atomic_value": first_value,
            "representative_route": list(representative[2]),
            "current_HP_after_action_and_fixed": representative[3],
            "representative_finance_end": representative[4],
            "representative_HP_end": representative[5][0],
            "representative_capacity_end": representative[5][1],
            "main_action_budget": future_count + 1,
            "future_action_count": future_count,
            "current_held_SS_count": int(bool(held)),
            "held_SS_redemptions_in_route": int(bool(held)),
            "Mei_stage_initial": outings[0]["stage"] if outings else None,
        })
    solve.cache_clear()
    return {
        "valid": bool(rows),
        "rows": rows,
        "reason": (
            "same_three_actions_full_numeric_and_recovery_order"
            if future_count == 2
            else "same_four_actions_full_numeric_and_recovery_order"
        ) if rows else "no_feasible_common_budget",
        "version": SOLVER_VERSION,
        "new_World_or_random_draws": 0,
        "current_big_training_repeated_as_future_guarantee": False,
        "shared_past_layout_count": len(layouts),
        "fixed_after_actions": fixed,
        "future_domestic": domestic,
        "old_vitality_or_continuity_added": False,
        "held_SS_future_risk_basis": "only_redeem_at_HP_not_below_its_current_verified_offer" if held else None,
        "future_numeric_state": "frozen_current_valuation_of_shared_past_layouts",
        "finance_callbacks_enabled": held_reward_provider is not None or after_income_training_projector is not None,
        "finance_callback_contract": "immutable_per_branch_state; H_income_only_at_redemption; upgrades_only_after_actual_income",
        "representative_route_is_execution_commitment": False,
    }


def _explicit_override(detail):
    if not isinstance(detail, dict) or detail.get("status") not in {"applied", "matched"}:
        return False
    effects = detail.get("effects") or {}
    return (
        bool(effects.get("preferred_actions"))
        or any(
            value != 1
            for key in ("training_multipliers", "component_factors")
            for value in (effects.get(key) or {}).values()
        )
        or detail.get("changed") is True
        or detail.get("priority_active") is True
        or number(detail.get("adjustment")) and detail["adjustment"] != 0
        or detail.get("mode") == "formula" and detail.get("formula", "base_score").strip() != "base_score"
    )


def _typed(packet, run, turn):
    if (
        not isinstance(packet, dict)
        or packet.get("schema_version") != "larc.training_energy_observation.v1"
        or packet.get("version") != 1
        or packet.get("status") != "accepted"
        or packet.get("unit") != "points"
        or packet.get("source_turn_index") != turn
        or (packet.get("source") or {}).get("run_session_id") != run
        or (packet.get("source") or {}).get("turn_index") != turn
    ):
        return None
    result = {key: packet.get(key) for key in ("capacity_points", "before_points", "applied_delta_points")}
    if any(
        not isinstance(bounds, (list, tuple))
        or len(bounds) != 2
        or not all(number(x) for x in bounds)
        or bounds[0] > bounds[1]
        for bounds in result.values()
    ):
        return None
    return result


def choose(state, goal, pool, *, incumbent_action_id):
    empty = {
        "version": VERSION,
        "changed": False,
        "selected_action_id": incumbent_action_id,
        "reason": "outside_firstcamp_last_free_rest",
        "World": 0,
    }
    if incumbent_action_id != "rest.normal":
        return empty
    params = params_for_goal(goal)
    from core.auto_trainer.larc_calendar import FORCED_RACES, free_action_days, is_overseas, period_at, period_ordinal
    scenario = state.scenario_state or {}
    period = scenario.get("larc_period") or {}
    now = period_ordinal(period.get("value") or {}) if period.get("status") == "verified" else None
    future = free_action_days(now + 1)[:3] if now is not None else []
    if (
        now is None
        or (period.get("value") or {}).get("year_index") != 2
        or not is_overseas(now)
        or FORCED_RACES.get(now + 1) != "classic_arc"
        or len(future) != 3
        or future[0] != now + 2
        or any(is_overseas(day) for day in future)
    ):
        return empty
    current = next((r for r in pool if r.get("action_id") == incumbent_action_id), None)
    if current is None or is_hard_rejected(current):
        return {**empty, "reason": "incumbent_not_existing_eligible_rest"}
    if any(_explicit_override(row.get(key)) for row in pool for key in ("score_tuning", "score_workflow")):
        return {**empty, "reason": "explicit_user_preference_preserved"}
    from core.auto_trainer.larc_timeline_workflow import selection_key
    eligible = [
        r for r in pool
        if not is_hard_rejected(r) and number(r.get("score")) and selection_key(r)[0] == selection_key(current)[0]
    ]
    captures = [(c.raw_data.get("capture") or {}) for c in state.training_options]
    runs = {c.get("run_session_id") for c in captures}
    if len(captures) != 5 or len(runs) != 1 or not next(iter(runs)):
        return {**empty, "reason": "complete_bound_current_menu_missing"}
    run = next(iter(runs))
    if any(c.get("turn_index") != state.turn for c in captures):
        return {**empty, "reason": "current_menu_scope_mismatch"}

    from core.auto_trainer.larc_expedition_forecast import _context
    finance = _context(state)
    levels, cash = finance.get("levels"), finance.get("points")
    if (
        finance.get("through_turn") != state.turn
        or finance.get("balance_current") is not True
        or type(cash) is not int
        or cash < 0
        or not isinstance(levels, list)
        or len(levels) != 10
    ):
        return {**empty, "reason": "fresh_committed_adaptation_cash_missing"}
    from core.auto_trainer.larc_adaptation import build_adaptation_plan
    from core.auto_trainer.larc_purchase_v3 import first_arc_language_reserve_decision
    deferred, _, _ = first_arc_language_reserve_decision(state, goal, levels, cash, now, balance_resources=True)
    phase = "first_arc_defer_language" if deferred else "first_arc"
    try:
        plan = build_adaptation_plan({"levels": levels, "points": cash, "pending_cost": 0, "errors": []}, phase=phase)
    except ValueError:
        return {**empty, "reason": "original_first_Arc_cash_plan_unavailable"}
    if plan.get("deferred_slots") or not 0 <= plan["cost"] <= cash:
        return {**empty, "reason": "current_cash_cannot_cover_original_required_Arc_plan"}

    from core.auto_trainer.larc_capacity import planning_capacity
    from core.auto_trainer.larc_energy_observation import training_energy_observation
    cap = planning_capacity(state)
    hp = state.vitality * cap / 100
    energies, exact = {}, []
    for candidate in state.training_options:
        packet = training_energy_observation(candidate, state)
        bounds = _typed(packet, run, state.turn)
        if bounds:
            energies[candidate.training_type] = bounds
            if all(bounds[key][0] == bounds[key][1] for key in ("capacity_points", "before_points")):
                exact.append((bounds["before_points"][0], bounds["capacity_points"][0]))
    if exact:
        if len(set(exact)) != 1:
            return {**empty, "reason": "conflicting_current_typed_HP"}
        hp, cap = exact[0]
    if not number(cap) or not number(hp) or not 0 <= hp <= cap <= 120 or cap <= 0:
        return {**empty, "reason": "current_energy_missing"}
    from core.auto_trainer.larc_risk import risk_at_energy, risk_value
    from core.auto_trainer.larc_opening import LarcOpeningStrategy
    failure_limit = LarcOpeningStrategy.failure_limit(goal, state)
    actions, atoms = [], {}
    for row in eligible:
        aid = row["action_id"]
        retained = {k: v for k, v in (row.get("components") or {}).items() if k not in DROP and number(v)}
        atom = sum(retained.values())
        if aid.startswith("train:"):
            tab = row.get("training_type") or aid.split(":", 1)[1]
            rate = row.get("failure_rate")
            if tab not in energies or not number(rate) or not 0 <= rate <= failure_limit:
                continue
            atom = risk_value(atom, rate)["value"]
            actions.append({
                "action_id": aid,
                "kind": "train",
                "value": atom,
                "delta": energies[tab]["applied_delta_points"][0],
            })
        elif aid == "rest.normal":
            scenarios = (row.get("effects") or {}).get("planning_scenarios")
            if (
                not isinstance(scenarios, list)
                or not scenarios
                or any(
                    not isinstance(x, dict)
                    or not number(x.get("points"))
                    or x["points"] < 0
                    or not number(x.get("weight"))
                    or x["weight"] < 0
                    for x in scenarios
                )
                or abs(sum(x["weight"] for x in scenarios) - 1) > 1e-6
            ):
                return {**empty, "reason": "current_rest_effect_scenarios_missing"}
            actions.append({
                "action_id": aid,
                "kind": "rest",
                "value": atom,
                "delta": 0.0,
                "outcomes": [(x["points"], x["weight"]) for x in scenarios],
            })
        else:
            continue
        atoms[aid] = {
            "retained": retained,
            "current_atom": atom,
            "removed": {k: v for k, v in (row.get("components") or {}).items() if k in DROP},
        }
    if not any(x["kind"] == "train" for x in actions) or not any(x["kind"] == "rest" for x in actions):
        return {**empty, "reason": "no_existing_priceable_training_and_rest"}

    reference = next(
        (
            (r.get("planning") or {}).get("energy_horizon", {}).get("future_value_reference")
            for r in pool
            if isinstance((r.get("planning") or {}).get("energy_horizon", {}).get("future_value_reference"), dict)
        ),
        None,
    )
    if reference is None:
        return {**empty, "reason": "original_strict_past_forecast_missing"}
    from core.auto_trainer.larc_attribute_plan import attribute_plan
    from core.auto_trainer.larc_recovery import (
        FUTURE_WISDOM_RECOVERY_CREDIT,
        PLANNING_START_ENERGY,
        estimated_training_cost,
        scheduled_recovery_after,
    )
    from core.auto_trainer.larc_recovery_routes import terminal_energy_weight
    from core.auto_trainer.larc_training_forecast import growth_snapshot, numeric_at_period, project_gains
    training_cost, cost_basis = estimated_training_cost(state)
    target = {**growth_snapshot(state, goal), "period": period_at(future[0]), "levels": list(levels), "purchase_v3": True}
    layouts, source_turns, energy_bases = [], set(), set()
    attributes = attribute_plan(state, goal)
    for layout in reference.get("phase_layouts") or []:
        rows = []
        for row in layout:
            source = {**(row.get("forecast_metadata") or {}), "purchase_v3": True}
            old, old_day = source.get("source_turn_index"), period_ordinal(source.get("period") or {})
            if type(old) is not int or not 0 < state.turn - old <= 12 or old_day is None or is_overseas(old_day):
                continue
            if not isinstance(row.get("gains"), dict) or not number(row.get("forecast_discount", 1.0)):
                return {**empty, "reason": "past_domestic_numeric_ledger_missing"}
            gains, projection = project_gains(state.current_stats, row["gains"], row["tab"], source, target)
            numeric = numeric_at_period(state, goal, gains, period_at(future[0]), attribute_context=attributes)
            packet = source.get("energy_observation") or {}
            bounds = _typed(packet, run, old)
            request = packet.get("requested_delta_points") if bounds else None
            if (
                isinstance(request, (list, tuple))
                and len(request) == 2
                and all(number(x) for x in request)
                and request[0] <= request[1]
                and not (packet.get("native_preview_already_calculator_clipped") is True and request[0] == 0)
            ):
                delta, basis = request[0], "bound_past_domestic_requested_points"
            elif bounds and (
                bounds["applied_delta_points"][0] < 0
                or packet.get("clipped") is False and bounds["applied_delta_points"][0] > 0
            ):
                delta, basis = bounds["applied_delta_points"][0], "bound_past_domestic_unclipped_points"
            elif row["tab"] == "wisdom":
                delta, basis = 5.0 * FUTURE_WISDOM_RECOVERY_CREDIT, "original_wisdom_recovery_prior"
            else:
                delta, basis = -training_cost * cap / 100, cost_basis
            rows.append({
                "tab": row["tab"],
                "value": round(
                    numeric * row.get("forecast_discount", 1.0) + row.get("bond", 0.0) + row.get("charge", 0.0),
                    4,
                ),
                "delta": delta,
                "risk_metadata": row.get("risk_metadata") or source.get("risk_metadata") or {},
                "source_turn_index": old,
                "projected_gains": gains,
                "projection": projection,
            })
            source_turns.add(old)
            energy_bases.add(basis)
        if rows:
            layouts.append(rows)
    if not layouts:
        return {**empty, "reason": "strict_past_domestic_layouts_missing"}
    domestic = replace(
        state,
        scenario_state={**scenario, "larc_period": {"status": "verified", "value": period_at(future[0])}},
    )
    outcomes = []
    for count in (2, 3):
        days = [now, *future[:count]]
        fixed = [
            sum(
                scheduled_recovery_after(state, period_at(x)) * cap / 100
                for x in range(start, days[index + 1] if index + 1 < len(days) else start + 1)
            )
            for index, start in enumerate(days)
        ]
        window = {
            "kind": "rolling_recovery",
            "steps": count,
            "action_calendar": [{"period": period_at(day), "kind": "domestic"} for day in future[:count]],
        }
        data = {
            "hp": hp,
            "cap": cap,
            "terminal_weight": terminal_energy_weight(domestic, goal, window),
            "current_actions": actions,
            "layouts": layouts,
            "held": None,
            "outings": [],
            "fixed_after": fixed,
            "future_domestic": [True] * count,
            "rest_scenarios": params.rest_scenarios,
            "discount": params.future_discount,
            "reserve_percent": PLANNING_START_ENERGY[goal.risk_tolerance],
            "low_hp_discount": {"low": 0.45, "medium": 0.35, "high": 0.25}[goal.risk_tolerance],
            "failure_limit": failure_limit,
        }
        quote = evaluate_budget(data, risk_quote=risk_at_energy, future_count=count)
        if not quote.get("valid"):
            return {**empty, "reason": "common_return_budget_unavailable", "quote": quote}
        rest = next((r for r in quote["rows"] if r["action_id"] == incumbent_action_id), None)
        if rest is None:
            return {**empty, "reason": "current_rest_route_unavailable"}
        winner = max(quote["rows"], key=lambda r: (r["sequence_score"], r["action_id"] == incumbent_action_id))
        outcomes.append({
            "budget": count + 1,
            "winner": winner["action_id"],
            "delta": winner["sequence_score"] - rest["sequence_score"],
            "quote": quote,
        })
    agree = (
        outcomes[0]["winner"] == outcomes[1]["winner"] != incumbent_action_id
        and all(x["delta"] > 1e-6 for x in outcomes)
    )
    return {
        **empty,
        "changed": agree,
        "selected_action_id": outcomes[0]["winner"] if agree else incumbent_action_id,
        "reason": (
            "both_return_budgets_prefer_existing_training"
            if agree
            else "return_budgets_do_not_agree_to_leave_rest"
        ),
        "calendar": now,
        "budgets": outcomes,
        "current_atoms": atoms,
        "strict_past_domestic_turns": sorted(source_turns),
        "historical_layout_count": len(layouts),
        "current_hp_points": hp,
        "current_capacity_points": cap,
        "current_typed_exact_energy_used": bool(exact),
        "future_energy_bases": sorted(energy_bases),
        "cash": cash,
        "original_Arc_plan_cost": plan["cost"],
        "original_Arc_plan_phase": phase,
        "future_levels": "committed_only_no_unpaid_friendship3_credit",
        "future_Mei": "omitted_no_current_domestic_binding; no_new_inventory_invented",
        "no_new_SS": True,
        "future_risk": "original_risk_at_energy_unknown_readiness_fallback",
        "fixed_race_has_no_new_free_action_or_unobserved_heal": True,
        "terminal_HP_once": True,
        "current_scores_and_pool_unchanged": True,
        "proposal_not_execution_commitment": True,
    }


def apply_local(state, goal, result):
    """Return the identical inherited object unless both budgets agree."""
    if PURCHASE_DEPTH.get() or getattr(result, "action", None) != "rest" or not enabled(goal):
        return result
    pool = getattr(result, "scored_candidates", None)
    if not isinstance(pool, (list, tuple)):
        return result
    try:
        diagnostic = choose(state, goal, pool, incumbent_action_id="rest.normal")
        if diagnostic.get("changed") is not True:
            return result
        selected = diagnostic.get("selected_action_id")
        chosen = next((row for row in pool if row.get("action_id") == selected), None)
        if (
            not isinstance(chosen, dict)
            or is_hard_rejected(chosen)
            or not isinstance(selected, str)
            or not selected.startswith("train:")
        ):
            return result
        return replace(
            result,
            action="train",
            target=chosen.get("training_type") or selected.split(":", 1)[1],
            confidence=chosen.get("source_confidence", result.confidence),
            risk_flags=list(chosen.get("risk_flags") or []),
            reason="Known return opportunities preserve the current training in both action budgets",
        )
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, ArithmeticError):
        return result
