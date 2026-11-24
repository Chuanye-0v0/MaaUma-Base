"""Matched-stock two-action comparison for Year-1 full-SS offers."""
from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
from math import isfinite
from statistics import mean

from core.auto_trainer.larc_participant_evidence import effective_domestic_charge, training_rows
from core.auto_trainer.larc_strategy_params import params_for_goal, unified_larc_enabled
from core.auto_trainer.rejection_policy import is_hard_rejected

QUOTE_VERSION = "research-matched-stock-r1"
GATES_VERSION = "research-matched-partial-r2"
SCHEMA = "larc.upper185_matched_partial.v1"
TYPES = {"speed", "stamina", "power", "guts", "wisdom"}

FAIR_SCOPE = ContextVar("upper185_fair_current_full_scope", default=None)
ENTRY = ContextVar("upper185_matched_entry", default=False)
LEGACY = ContextVar("upper185_legacy_fallback", default=False)

CURRENT_DROP = frozenset({
    "vitality",
    "known_recovery",
    "rule_recovery",
    "verified_cost_reduction",
    "energy_continuity",
    "failure_expected_loss",
    "failure_penalty",
    "risk_aversion_preference",
    "cycle_reward_preparation",
    "charming_charge_progress",
    "future_recovery_cycle_progress",
    "conditional_fan_route",
    "supporter_deadline_preference",
})


def number(value):
    return type(value) in (int, float) and isfinite(value)


def _recover(hp, cap, points, cap_delta=0.0):
    maximum = min(120.0, cap + max(0.0, cap_delta))
    return min(maximum, max(0.0, hp + points)), maximum


def _terminal(hp, cap, common_cap, weight):
    bar = hp * 100 / common_cap
    return weight * (2 * bar - 0.008 * bar * bar)


def _pure_quote(data):
    required = (
        "hp",
        "cap",
        "held_value",
        "held_hp",
        "held_cap",
        "terminal_weight",
        "current_training_value",
        "current_hp",
        "future_training_value",
        "future_hp",
        "release_one",
        "expectation_one",
        "generic_future_charge_value",
    )
    if any(not number(data.get(key)) for key in required):
        return {"valid": False, "reason": "atomic_matched_stock_value_unavailable"}
    hp, cap = data["hp"], data["cap"]
    if not 0 <= hp <= cap <= 120 or cap <= 0:
        return {"valid": False, "reason": "current_energy_scope_invalid"}
    a0, ac = _recover(hp, cap, data["held_hp"], data["held_cap"])
    ah, ac = _recover(a0, ac, data["future_hp"])
    b0, bc = _recover(hp, cap, data["current_hp"])
    bh, bc = _recover(b0, bc, data["held_hp"], data["held_cap"])
    origin = _terminal(hp, cap, cap, data["terminal_weight"])
    a_energy = _terminal(ah, ac, cap, data["terminal_weight"]) - origin
    b_energy = _terminal(bh, bc, cap, data["terminal_weight"]) - origin
    common = data["held_value"]
    core_a = common + data["future_training_value"] + a_energy + data["expectation_one"]
    main_a = core_a + data["release_one"]
    sensitivity_a = core_a + data["generic_future_charge_value"]
    b = common + data["current_training_value"] + b_energy
    a = max(main_a, sensitivity_a)
    return {
        "valid": True,
        "version": QUOTE_VERSION,
        "A_score": a,
        "B_score": b,
        "delta_B_minus_A": b - a,
        "training_preferred": b > a + 1e-8,
        "A_release_channel_score": main_a,
        "A_generic_charge_channel_score": sensitivity_a,
        "A_conservative_guard": "B_must_beat_both_nonadditive_A_charge_channels",
        "main_actions_A": ["held_SS", "estimated_training"],
        "main_actions_B": ["current_Mei_training", "held_SS"],
        "main_action_budget_each": 2,
        "held_consumptions_each": 1,
        "held_current_value_each": common,
        "held_frozen_price_not_stateful": True,
        "held_current_rewards_each": data.get("held_reward_ledger"),
        "next_ss_roster_known": False,
        "new_SS_or_SSS_generated": False,
        "current_HP": hp,
        "current_capacity": cap,
        "A_HP_after_SS": a0,
        "A_HP_end": ah,
        "A_capacity_end": ac,
        "B_HP_after_training": b0,
        "B_HP_end": bh,
        "B_capacity_end": bc,
        "A_terminal_energy_once": a_energy,
        "B_terminal_energy_once": b_energy,
        "old_action_HP_or_continuation_added": False,
        "future_non_H_charge_modelled": False,
        "generic_charge_and_release_summed": False,
        "training_failure_HP_effect": "unknown_not_invented_current_cost_planning_estimate",
    }


def _same_scope(source, run, turn):
    return (
        isinstance(source, dict)
        and source.get("run_session_id") == run
        and type(source.get("turn_index")) is int
        and source["turn_index"] == turn
    )


def _reference(person, run):
    value = person.get("member_ref") or {}
    return (
        value.get("member_id")
        if (
            value.get("status") == "accepted"
            and value.get("run_session_id") == run
            and isinstance(value.get("member_id"), str)
            and value["member_id"]
        )
        else None
    )


def _is_mei(person):
    identity, role = person.get("identity") or {}, person.get("role") or {}
    return (
        identity.get("status") == "known"
        and role.get("status") == "accepted"
        and role.get("kind") == "friend"
        and (
            identity.get("character_id") in {"satake_mei", "9043", 9043}
            or str(identity.get("matched_card_id")) in {"30160", "10094"}
        )
    )


def _scope(state, preview):
    from core.auto_trainer.larc_ss_deadline import _current_full
    options = getattr(state, "training_options", ())
    if len(options) != 5 or {x.training_type for x in options} != TYPES:
        return None
    captures = [option.raw_data.get("capture") or {} for option in options]
    runs = {source.get("run_session_id") for source in captures}
    if len(runs) != 1:
        return None
    run = next(iter(runs))
    if (
        not isinstance(run, str)
        or not run
        or not all(_same_scope(source, run, state.turn) for source in captures)
        or _current_full(preview, run, state.turn) is not True
    ):
        return None
    packet = preview.get("participants_observation") or {}
    if packet.get("status") != "accepted" or not _same_scope(packet.get("source"), run, state.turn):
        return None
    rows = packet.get("participants") or []
    refs = [_reference(p, run) for p in rows]
    if len(rows) != 5 or None in refs or len(set(refs)) != 5:
        return None
    signature = []
    for option in options:
        capture = option.raw_data.get("capture") or {}
        observation = (option.scenario_mechanics.get("larc") or {}).get("training_participants") or {}
        view = training_rows(option, typed=True)
        if not _same_scope(capture, run, state.turn) or view is None:
            return None
        osource = observation.get("source")
        if not _same_scope(osource, run, state.turn):
            return None
        if any(
            key in capture and key in osource and capture[key] != osource[key]
            for key in ("sha256", "source_sha256", "observation_id")
        ):
            return None
        signature.append((option.training_type, capture.get("sha256"), capture.get("observation_id")))
    return {
        "run": run,
        "turn": state.turn,
        "full_members": set(refs),
        "signature": tuple(sorted(signature)),
        "preview_signature": (
            preview.get("kind"),
            preview.get("ready_count"),
            tuple(sorted((preview.get("stats_gain") or {}).items())),
            preview.get("simulation_risk_percent"),
        ),
    }


def _next_ss_available(state):
    from core.auto_trainer.larc_calendar import FORCED_RACES, is_overseas, period_ordinal
    context = state.scenario_state
    period = (context.get("larc_period") or {}).get("value") or {}
    day = period_ordinal(period)
    if day is None:
        remaining = context.get("larc_remaining_turns")
        packet = context.get("larc_period") or {}
        captures = [option.raw_data.get("capture") or {} for option in state.training_options]
        runs = {capture.get("run_session_id") for capture in captures}
        if len(runs) != 1:
            return False
        run = next(iter(runs))
        current = isinstance(run, str) and bool(run) and all(_same_scope(s, run, state.turn) for s in captures)
        if "source" in packet:
            header = packet["source"]
            current = (
                current
                and _same_scope(header, run, state.turn)
                and not any(
                    k in header and k in s and header[k] != s[k]
                    for s in captures
                    for k in ("step_count", "collection_epoch")
                )
            )
        else:
            current = (
                current
                and packet.get("status") == "verified"
                and isinstance(packet.get("source_frame_sha256"), str)
                and bool(packet["source_frame_sha256"])
            )
        return (
            current
            and period.get("year_index") == 1
            and period.get("pre_debut") is True
            and type(remaining) is int
            and remaining > 1
        )
    nxt = day + 1
    return nxt != 11 and nxt not in FORCED_RACES and not is_overseas(nxt) and nxt <= 58


def structural_status(state, goal, preview):
    from core.auto_trainer.larc_ss_deadline import _history
    from core.auto_trainer.larc_ss_phase import PRE_FIRST_ARC_DOMESTIC, phase_of
    from core.auto_trainer.larc_ss_visible import ss_victory_evidence
    from core.auto_trainer.larc_supporter_goal import deadline_larc_supporter_goal

    params = params_for_goal(goal)
    result = {"eligible": False, "reason": "not_current_firstyear_free_full5", "candidates": []}
    context = getattr(state, "scenario_state", {}) or {}
    period = context.get("larc_period") or {}
    if (
        getattr(goal, "scenario", None) != "larc"
        or period.get("status") not in {"verified", "accepted"}
        or (period.get("value") or {}).get("year_index") != 1
        or phase_of(state) != PRE_FIRST_ARC_DOMESTIC
        or not isinstance(preview, dict)
        or preview.get("status") != "accepted"
        or preview.get("stage") != "preview"
        or preview.get("ready_count") != params.full_group_min_count
    ):
        return result
    scope = _scope(state, preview)
    if scope is None:
        return {**result, "reason": "incomplete_or_wrong_scope_five_pages_members"}
    result.update(run_session_id=scope["run"], turn=state.turn)
    if deadline_larc_supporter_goal(context.get("larc_supporter_goal")) or not _next_ss_available(state):
        return {**result, "reason": "forced_deadline_or_next_SS_action_unavailable"}
    first, deferred = _history(context.get("larc_ss_deadline_history"), scope["run"], state.turn, True)
    result.update(
        first_ready_turn=first,
        deferred_main_actions=deferred,
        grace_basis="original_same_run_confirmed_main_actions_not_callback_count",
    )
    if deferred != 0:
        return {**result, "reason": "grace_spent_or_history_unknown_original_SS_exit"}
    gains = preview.get("stats_gain") or {}
    occupancy = preview.get("occupancy")
    numeric = all(type(gains.get(k)) is int and gains[k] >= 0 for k in (*TYPES, "skill_pt"))
    guaranteed = ss_victory_evidence(preview).get("guaranteed") is True
    risk = preview.get("simulation_risk_percent")
    if (
        not numeric
        or not guaranteed
        or number(risk) and risk > 0
        or isinstance(occupancy, dict) and occupancy.get("status") != "accepted"
    ):
        return {**result, "reason": "original178_fair_current_SS_value_unavailable"}
    raw_limit = getattr(goal, "training_failure_rate_limit", None)
    from core.auto_trainer.larc_opening import LarcOpeningStrategy
    from core.auto_trainer.larc_strategy_extension import active_provider
    if not number(raw_limit) and not active_provider().trajectory_risk_enabled(params):
        return {**result, "reason": "explicit_current_training_risk_limit_required"}
    limit = LarcOpeningStrategy.failure_limit(goal, state)
    known_cells, conflicts = {}, set()
    for option in state.training_options:
        people, _ = training_rows(option, typed=True)
        for person in people:
            ref = _reference(person, scope["run"])
            charge = person.get("charge") or {}
            filled = charge.get("filled_cells")
            if ref and type(filled) is int:
                if ref in known_cells and known_cells[ref] != filled:
                    conflicts.add(ref)
                known_cells[ref] = filled
    mei_seen = legal_mei = 0
    for option in state.training_options:
        risk = option.failure_rate
        people, _ = training_rows(option, typed=True)
        if not any(_is_mei(person) for person in people):
            continue
        mei_seen += 1
        if not number(risk) or not 0 <= risk <= limit:
            continue
        legal_mei += 1
        positive, converted = {}, set()
        for person in people:
            ref = _reference(person, scope["run"])
            resolved = effective_domestic_charge(
                person, state=state, tab_participants=people, candidate=option
            )
            if (
                ref
                and ref not in conflicts
                and ref not in scope["full_members"]
                and resolved is not None
            ):
                filled, direct, _ = resolved
                if 0 <= filled < 3 and 0 < direct <= 3 - filled:
                    positive[ref] = direct
                    if filled + direct == 3:
                        converted.add(ref)
        new = len(converted)
        success = 1 - risk / 100
        if positive:
            original = (
                new >= params.matched_stock_min_direct_new_full
                and new * success >= params.matched_stock_min_expected_new_full
            )
            result["candidates"].append({
                "action_id": "train:" + option.training_type,
                "training_type": option.training_type,
                "new_nonfull_members": sorted(converted),
                "direct_new_full": new,
                "training_success_probability": success,
                "expected_direct_new_full": new * success,
                "positive_nonH_members": sorted(positive),
                "direct_positive_net_cells": sum(positive.values()),
                "charge_qualification": "original_two_newfull" if original else "partial_positive_nonfull",
                "original_R1_qualification": original,
                "additional_Mei_proc_or_future_layout_assumed": False,
            })
    result.update(
        Mei_training_pages_seen=mei_seen,
        legal_Mei_training_pages=legal_mei,
        positive_partial_qualified=sum(c["charge_qualification"] == "partial_positive_nonfull" for c in result["candidates"]),
        original_two_newfull_qualified=sum(c["original_R1_qualification"] for c in result["candidates"]),
    )
    reason = (
        "qualifying_partial_positive_nonH_charge" if result["candidates"]
        else "no_current_Mei_training" if mei_seen == 0
        else "no_legal_current_Mei_training" if legal_mei == 0
        else "Mei_without_confirmed_positive_nonH_charge"
    )
    return {**result, "eligible": bool(result["candidates"]), "reason": reason, "_scope": scope}


def _make_quote(state, goal, preview, comparison, candidate):
    from core.auto_trainer.larc_capacity import planning_capacity, recover_points
    from core.auto_trainer.larc_charge_value import ss_battery_release_value
    from core.auto_trainer.larc_energy_observation import training_energy_observation
    from core.auto_trainer.larc_mei_cycle import enabled as mei_cycle_enabled
    from core.auto_trainer.larc_recovery import (
        FUTURE_WISDOM_RECOVERY_CREDIT,
        PLANNING_START_ENERGY,
        estimated_training_cost,
        ss_recovery,
    )
    from core.auto_trainer.larc_risk import risk_at_energy, risk_value
    from core.auto_trainer.larc_ss_energy import _observed_delta
    from core.auto_trainer.larc_ss_opportunity import ss_redemption
    from core.auto_trainer.larc_training_forecast import ss_expectation_value

    params = params_for_goal(goal)
    horizon = (comparison.get("planning") or {}).get("energy_horizon") or {}
    reference = horizon.get("future_value_reference")
    if not isinstance(reference, dict):
        return {"valid": False, "reason": "shared_current_numeric_reference_unavailable"}
    cap = planning_capacity(state)
    hp = state.vitality * cap / 100
    packet = training_energy_observation(candidate, state)
    source = candidate.raw_data.get("capture") or {}
    if (
        packet.get("status") != "accepted"
        or packet.get("unit") != "points"
        or (packet.get("source") or {}).get("run_session_id") != source.get("run_session_id")
        or packet.get("source_turn_index") != state.turn
    ):
        return {"valid": False, "reason": "current_training_energy_unavailable"}
    for key in ("capacity_points", "before_points", "applied_delta_points"):
        bounds = packet.get(key)
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 2 or not all(number(x) for x in bounds):
            return {"valid": False, "reason": "current_training_energy_bounds_unavailable"}
    if packet["capacity_points"][0] == packet["capacity_points"][1] and packet["before_points"][0] == packet["before_points"][1]:
        cap, hp = packet["capacity_points"][0], packet["before_points"][0]
    current_hp = packet["applied_delta_points"][0]
    row = next(
        (
            r for r in comparison.get("training_candidates", [])
            if r.get("action_id") == candidate.action_id and not is_hard_rejected(r)
        ),
        None,
    )
    if row is None:
        return {"valid": False, "reason": "candidate_not_in_original_eligible_pool"}
    components = row.get("components") or {}
    retained = {k: v for k, v in components.items() if k not in CURRENT_DROP and number(v)}
    removed = {k: v for k, v in components.items() if k in CURRENT_DROP and number(v)}
    current_risk = risk_value(sum(retained.values()), candidate.failure_rate)
    weights = row["weights"]
    release = ss_battery_release_value(
        state, preview, weights, hold_actions=1, cycle_model=mei_cycle_enabled(state, goal), goal=goal
    )
    expectation = ss_expectation_value(state, goal, preview, deferred_actions=1)
    reward = ss_recovery(preview)
    held_hp, held_cap = reward.get("observed_points", 0), reward.get("max_increase", 0)
    common_h = comparison.get("gain_value", {}).get("score")
    if not number(common_h):
        return {"valid": False, "reason": "original_held_numeric_value_unavailable"}
    common_h += sum(comparison.get("components", {}).get(k, 0.0) for k in ("effective_bond", "first_charming_preference"))
    held_ledger = {
        "stats_gain": deepcopy(preview.get("stats_gain")),
        "skill_Pt": (preview.get("stats_gain") or {}).get("skill_pt"),
        "adaptation_points": preview.get("adaptation_points_if_all_win"),
        "supporter_points": preview.get("supporter_points_if_all_win"),
        "kind_current": preview.get("kind"),
        "observed_HP_points": held_hp,
        "max_HP_delta": held_cap,
        "uncredited_icon_rule_HP": reward.get("rule_points", 0),
        "AP_same_entitlement_each_branch_not_a_new_purchase_forecast": True,
    }
    cost_percent, cost_basis = estimated_training_cost(state)
    common_cost = cost_percent * cap / 100
    layouts = []
    for layout in reference.get("opportunity_layouts") or []:
        history = [
            r for r in layout
            if isinstance(r, dict)
            and type((r.get("forecast_metadata") or {}).get("source_turn_index")) is int
            and r["forecast_metadata"]["source_turn_index"] < state.turn
        ]
        if history:
            layouts.append(history)
    history_available = bool(layouts)
    if not layouts:
        priors = [
            row
            for layout in reference.get("opportunity_layouts") or []
            for row in layout
            if row.get("tab") != "wisdom" and all(number(row.get(k)) for k in ("numeric", "bond", "charge"))
        ]
        prior = max(priors, key=lambda row: row["numeric"] + row["bond"] + row["charge"]) if priors else {}
        layouts = [[{
            "tab": "generic",
            "numeric": reference.get("numeric_training", 0.0),
            "bond": prior.get("bond", 0.0),
            "charge": prior.get("charge", 0.0),
            "forecast_metadata": {},
            "risk_metadata": {},
        }]]
    first_factor = ss_redemption(state, earliest_actions=3)["value_factors"]["net_charge"]
    hp_a, cap_a = recover_points(hp, cap, held_hp, held_cap)
    energy_a = hp_a * 100 / cap_a
    reserve = PLANNING_START_ENERGY[goal.risk_tolerance]
    shortfall = min(1.0, max(0.0, (reserve - energy_a) / max(1.0, reserve)))
    readiness = 1 - {"low": 0.45, "medium": 0.35, "high": 0.25}[goal.risk_tolerance] * shortfall
    results = []
    for layout in layouts:
        alternatives = []
        for future in layout:
            if not all(number(future.get(k, 0)) for k in ("numeric", "bond", "charge")):
                continue
            metadata = future.get("forecast_metadata") or {}
            delta = -common_cost
            observed, _ = _observed_delta(metadata, state)
            if observed is not None:
                delta = observed["delta"]
            elif future.get("tab") == "wisdom":
                delta = 5.0 * FUTURE_WISDOM_RECOVERY_CREDIT
            if hp_a + delta < 0:
                continue
            positive = future["numeric"] + future.get("bond", 0.0)
            risk = risk_at_energy(positive, future.get("risk_metadata") or metadata.get("risk_metadata"), energy_a)
            from core.auto_trainer.larc_opening import LarcOpeningStrategy
            if risk["failure_rate"] is not None and risk["failure_rate"] > LarcOpeningStrategy.failure_limit(goal, state):
                continue
            probability = risk["success_probability"] if risk["success_probability"] is not None else 1.0
            q = _pure_quote({
                "hp": hp,
                "cap": cap,
                "held_value": common_h,
                "held_hp": held_hp,
                "held_cap": held_cap,
                "terminal_weight": horizon.get("terminal_energy_weight", params.domestic_terminal_energy_weight),
                "current_training_value": current_risk["value"],
                "current_hp": current_hp,
                "future_training_value": positive * probability * readiness - risk["preference_cost"],
                "future_hp": delta,
                "release_one": release["score"] * probability * readiness,
                "expectation_one": expectation["score"],
                "generic_future_charge_value": future.get("charge", 0.0) * first_factor * probability * readiness,
                "held_reward_ledger": held_ledger,
            })
            if q["valid"]:
                q.update(
                    future_tab=future["tab"],
                    future_source_turn=metadata.get("source_turn_index"),
                    future_risk_basis=risk["basis"],
                )
                alternatives.append(q)
        if not alternatives:
            return {"valid": False, "reason": "no_legal_future_training_in_shared_layout"}
        results.append(max(alternatives, key=lambda q: q["A_score"]))
    q = deepcopy(results[len(results) // 2])
    a, b = mean(r["A_score"] for r in results), mean(r["B_score"] for r in results)
    q.update(
        A_score=a,
        B_score=b,
        delta_B_minus_A=b - a,
        training_preferred=b > a + 1e-8,
        reference_scenarios=len(results),
        reference_is_history=history_available,
        generic_prior_basis="original_discounted_numeric_bond_charge_prior_not_a_future_page",
        current_components_retained_once=retained,
        current_components_removed=removed,
        current_risk_once=current_risk,
        old_SS_components_not_summed=deepcopy(comparison.get("components")),
        one_step_release=release,
        one_step_expectation=expectation,
        future_training_cost_basis=cost_basis,
        current_energy_basis=packet.get("source_kind"),
        current_energy_bounds=deepcopy(packet),
        current_AP_common_price_frozen_cancelled=True,
        old_H_hold3_release_not_reused=True,
        all_layout_quotes=results,
    )
    return q


def _explicit_override(detail):
    if not isinstance(detail, dict) or detail.get("status") not in {"applied", "matched"}:
        return False
    return (
        detail.get("changed") is True
        or detail.get("priority_active") is True
        or type(detail.get("adjustment")) in (int, float) and detail["adjustment"] != 0
        or detail.get("mode") == "formula" and detail.get("formula", "base_score").strip() != "base_score"
    )


def comparison_status(state, goal, preview=None):
    if not unified_larc_enabled(goal) or LEGACY.get():
        return None
    current = preview or state.scenario_state.get("larc_ss_preview")
    active = FAIR_SCOPE.get()
    if active is not None:
        scope = _scope(state, current) if isinstance(current, dict) else None
        if scope is None or scope != active["_scope"]:
            return None
    else:
        active = structural_status(state, goal, current)
        if not active["eligible"]:
            return None
    return {
        "enabled": True,
        "active": True,
        "reason": SCHEMA,
        "current_full": True,
        "numeric_known": True,
        "victory_guaranteed": True,
        "collect_pending": False,
        "historical_deferral_cap_applied": True,
        "deferred_main_actions": 0,
        "max_deferred_main_actions": 1,
        "numeric_basis": "unchanged_current_guaranteed_all_win_preview",
        "future_ss_numeric_modelled": False,
        "member_inventory_modelled": False,
        "matched_stock_numeric_priced_in_local_quote_only": True,
        "one_unused_grace_recomputed_from_confirmed_history": True,
    }


def _original(original, state, goal, preview, *, ss_executions, preflight_only=False):
    token = LEGACY.set(True)
    try:
        return original(state, goal, preview, ss_executions=ss_executions, preflight_only=preflight_only)
    finally:
        LEGACY.reset(token)


def choose(state, goal, preview, *, ss_executions, preflight_only=False, original, scored):
    if not unified_larc_enabled(goal) or preflight_only or ENTRY.get():
        return _original(original, state, goal, preview, ss_executions=ss_executions, preflight_only=preflight_only)
    token = ENTRY.set(True)
    try:
        return _choose(state, goal, preview, ss_executions=ss_executions, original=original, scored=scored)
    finally:
        ENTRY.reset(token)


def _choose(state, goal, preview, *, ss_executions, original, scored):
    from core.auto_trainer.larc_timeline_workflow import selection_key

    params = params_for_goal(goal)
    structural = structural_status(state, goal, preview)
    if not structural["eligible"]:
        result = _original(original, state, goal, preview, ss_executions=ss_executions)
        return {
            **result,
            "matched_stock": {
                "schema": SCHEMA,
                "eligible": False,
                "changed": False,
                "structural": {k: v for k, v in structural.items() if not k.startswith("_")},
                "reason": structural["reason"],
            },
        }
    before = deepcopy(goal.to_dict())
    token = FAIR_SCOPE.set(structural)
    try:
        comparison = scored(state, goal, preview, ss_executions=ss_executions)
    finally:
        FAIR_SCOPE.reset(token)
    assert goal.to_dict() == before
    rows = comparison.get("training_candidates") or []
    eligible = [
        r for r in rows
        if r.get("training_type") in TYPES and not is_hard_rejected(r) and number(r.get("score"))
    ]
    if not number(comparison.get("score")):
        eligible = []
    best = min(eligible, key=selection_key) if eligible else None
    floor = best["score"] - abs(best["score"]) * params.matched_stock_nearbest_fraction if best else None
    ss_rank = (comparison.get("score_workflow") or {}).get("priority_rank", 0)
    approved, quotes, tier_band = [], [], 0
    for allowed in structural["candidates"]:
        row = next((r for r in eligible if r["action_id"] == allowed["action_id"]), None)
        if (
            row is None
            or best is None
            or row["score"] < floor
            or selection_key(row)[0] != selection_key(best)[0]
            or selection_key(row)[0] != ss_rank
        ):
            continue
        tier_band += 1
        if any(
            _explicit_override(detail)
            for detail in (row.get("score_tuning"), comparison.get("score_tuning"), row.get("score_workflow"))
        ):
            continue
        candidate = next(c for c in state.training_options if c.action_id == row["action_id"])
        if params.full_ss_vitality_conservation_enabled:
            from core.auto_trainer.larc_strategy_rules import mediocre_training

            if mediocre_training(row, candidate, state, goal=goal):
                continue
        token = FAIR_SCOPE.set(structural)
        try:
            q = _make_quote(state, goal, preview, comparison, candidate)
        except Exception:
            FAIR_SCOPE.reset(token)
            raise
        FAIR_SCOPE.reset(token)
        q["action_id"] = row["action_id"]
        quotes.append(q)
        if q.get("valid") and q.get("training_preferred"):
            approved.append((q["delta_B_minus_A"], row))
    if approved:
        _, winner = max(approved, key=lambda pair: (pair[0], pair[1]["score"]))
        result = {
            **comparison,
            "selected": False,
            "local_action": "train",
            "local_target": winner["training_type"],
            "training_score": winner["score"],
            "local_reason": "Matched two-action stock quote preserves current Mei opportunity; SS due next main action",
            "local_risk_flags": list(winner.get("risk_flags") or []),
            "reason": "research_matched_stock_train_then_current_H_once",
            "selection_basis": "two_equal_main_actions_current_H_once_each_no_old_energy_proxy",
        }
        changed = True
    else:
        result = _original(original, state, goal, preview, ss_executions=ss_executions)
        changed = False
    note = {
        "schema": SCHEMA,
        "version": QUOTE_VERSION,
        "eligible": True,
        "changed": changed,
        "structural": {k: v for k, v in structural.items() if not k.startswith("_")},
        "nearbest_fraction": params.matched_stock_nearbest_fraction,
        "same_tier_floor": floor,
        "stage_counts": {
            "structural_candidates": len(structural["candidates"]),
            "same_tier_and_band": tier_band,
            "quotes": len(quotes),
            "valid_quotes": sum(q.get("valid") is True for q in quotes),
        },
        "quotes": quotes,
        "final_selected_SS": result.get("selected"),
        "final_local_target": result.get("local_target"),
        "final_training_score_lookup_by_action_id": result.get("training_score"),
        "current_full_pool": [
            {
                "action_id": r.get("action_id"),
                "score": r.get("score"),
                "components": deepcopy(r.get("components")),
                "rejected": r.get("rejected"),
            }
            for r in rows
        ],
        "original_SS_score_not_used_as_route_value": comparison.get("score"),
        "grace_from_confirmed_history_not_offer_signature_or_callback": True,
        "Goal_state_probability_or_physics_changed": False,
    }
    return {**result, "matched_stock": note}
