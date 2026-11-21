"""Unified Year-1 L'Arc action scoring engine (b197 + combo198 + contact + close).

Projects the current Year-1 menu into a canonical state/action packet, prices
marginal utility using ``LarcStrategyParams``, and applies HP/cross-80/contact/
full-SS-completion repricing in a single pass with zero revision branching.
"""
from __future__ import annotations

from collections import defaultdict
from contextvars import ContextVar
from copy import deepcopy
from itertools import product
from math import isclose, isfinite, prod

from core.auto_trainer.larc_calendar import period_ordinal
from core.auto_trainer.larc_capacity import capacity_bounds, recover_points
from core.auto_trainer.larc_friend_outing import outing_effects
from core.auto_trainer.larc_gain_evidence import gain_evidence
from core.auto_trainer.larc_mood import MOODS
from core.auto_trainer.larc_participant_evidence import training_rows, visible_charge
from core.auto_trainer.larc_phase import phase_for_period
from core.auto_trainer.larc_ss_visible import ss_victory_evidence
from core.auto_trainer.larc_strategy_params import DEFAULT_PARAMS, larc_enabled, params_for_goal
from core.auto_trainer.larc_vitality_preview import accepted_preview
from core.auto_trainer.mei_outing_effects import resolve_mei_effects

ATTRIBUTES = ("speed", "stamina", "power", "guts", "wisdom")
NUMERIC = ("skill_pt", "adaptation_pt", "hp_points", "hp_percent_points", "hp_cap_points", "mood", "bond_gain")
ROLE = {"support": "support_card", "npc": "scenario_npc", "friend": "friend"}
REWARD = {
    "aptitude_pt": "adaptation_pt",
    "max_vitality_and_recovery": "energy_and_cap",
    "motivation_and_vitality": "energy_and_mood",
    "charge_gain": "recharge",
    "charming": "charm",
}
DOMESTIC_REST_REFERENCE = ((30, 0.18), (50, 0.57), (70, 0.25))
EFFECTS = ("hp", "ap", "bond", "charge", "charm", "training_failure", "ss_some_member_failure")
DEFAULT_PREFERENCES = {"numeric": 1.0, "bond": 1.0, "charge": 1.0, "gold_ship_bonus": 0.0, "mei_bonus": 0.0}
NONFULL_ENTRY = "larc_b198_nonfull_entry"
ORDINARY_CONTACT_POINTS = 7.0
BOND_SOURCES = {"confirmed_points", "estimated_visible_bond_bar"}

_FALLBACK: ContextVar[str | None] = ContextVar("b197_fallback", default=None)


def fallback_reason() -> str | None:
    return _FALLBACK.get()


def enabled(goal) -> bool:
    policy = getattr(goal, "larc_policy", None)
    return isinstance(policy, dict) and policy.get("variant") == "baseline" and larc_enabled(goal)


def finite(value):
    return float(value) if type(value) in (int, float) and isfinite(value) else None


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _number(value):
    return value if type(value) in (int, float) and isfinite(value) else None


def _bounded(value, lo, hi):
    value = _number(value)
    return value if value is not None and lo <= value <= hi else None


def _unit(value):
    value = finite(value)
    return value if value is not None and 0 <= value <= 1 else None


def _count(value):
    value = finite(value)
    return int(value) if value is not None and value >= 0 and value.is_integer() else None


def _vector(values):
    if isinstance(values, (tuple, list)) and len(values) == 5:
        return dict(zip(ATTRIBUTES, map(_number, values)))
    values = _mapping(values)
    return {axis: _number(values.get(axis)) for axis in ATTRIBUTES}


def _direct(values=None, *, zero=False):
    values = _mapping(values)
    default = 0 if zero else None
    return {
        "stats": dict.fromkeys(ATTRIBUTES, 0) if zero else _vector(values.get("stats", values.get("stats_display_gain"))),
        **{key: _number(values.get(key, default)) for key in NUMERIC},
        "charm": values.get("charm") if type(values.get("charm")) is bool else False if zero else None,
    }


def _delta_packet(packet):
    packet = _mapping(packet)
    return _direct(
        {
            **packet,
            **{
                key: packet.get(key, packet.get(raw))
                for key, raw in (
                    ("skill_pt", "skill_pt_gain"),
                    ("adaptation_pt", "adaptation_pt_gain"),
                    ("hp_points", "energy_delta_points"),
                    ("hp_percent_points", "energy_delta_percent_points"),
                    ("hp_cap_points", "max_energy_delta_points"),
                    ("mood", "mood_delta"),
                    ("charm", "grants_charm"),
                )
            },
        }
    )


def _current_run(state):
    ids = {_mapping(option.raw_data.get("capture")).get("run_session_id") for option in state.training_options}
    ids.discard(None)
    return next(iter(ids)) if len(ids) == 1 else None


def _declared_profile(state, supplied):
    ctx = state.scenario_state
    outing = _mapping(ctx.get("larc_friend_outing"))
    profile = _mapping(supplied if supplied is not None else outing.get("support_card_profile", ctx.get("support_card_profile")))
    run = _current_run(state)
    return profile if run is None or profile.get("run_session_id") in {None, run} else {}


def _stage(ctx, turn, run):
    outing = _mapping(ctx.get("larc_friend_outing"))
    public = _mapping(ctx.get("mei_outing"))
    contact = _mapping(ctx.get("larc_mei_contact"))
    if run is not None and contact.get("run_session_id") not in {None, run}:
        contact = {}
    first = public.get("first_training_interaction_completed")
    if type(first) is bool:
        stage = "subsequent_proc" if first else "first_contact"
        basis = "current_first_interaction_flag"
    elif (outing.get("status") == "accepted" and outing.get("unlocked") is True) or contact.get("status") == "unlock_event_seen":
        stage, basis = "subsequent_proc", "confirmed_current_outing_or_unlock_event"
    elif contact.get("status") == "training_selected" and type(turn) is int and type(contact.get("turn_index")) is int and contact["turn_index"] < turn:
        stage, basis = "subsequent_proc", "estimated_from_prior_committed_contact"
    elif contact.get("status") == "unrecorded":
        stage, basis = "first_contact", "estimated_first_contact_without_record"
    else:
        stage, basis = None, "contact_stage_unknown"
    completed = _bounded(outing.get("completed", public.get("used")), 0, 5)
    unlocked = outing.get("unlocked", public.get("unlocked"))
    return {
        "contact_stage": stage,
        "contact_basis": basis,
        "unlocked": unlocked if type(unlocked) is bool else None,
        "completed": completed,
        "next_stage": int(completed + 1) if completed is not None and completed < 5 else None,
    }


def _state_packet(state, goal, preview, profile):
    ctx = state.scenario_state
    packet = _mapping(ctx.get("larc_period"))
    period = _mapping(packet.get("value")) if packet.get("status") in {"verified", "accepted"} else {}
    calendar = period_ordinal(period)
    capacity = _bounded(ctx.get("max_vitality"), 100, 120)
    capacity_source = "current_max_vitality"
    if capacity is None:
        lo, hi = capacity_bounds(state)
        declared = _bounded(profile.get("max_vitality"), 100, 120)
        capacity = lo if lo == hi else declared if declared is not None and lo <= declared <= hi else (lo + hi) / 2
        capacity_source = (
            "bound_capacity"
            if lo == hi
            else "estimated_profile_capacity_reference"
            if capacity == declared
            else "estimated_parent_capacity_midpoint"
        )
    hp = _bounded(state.vitality, 0, 100)
    growth = _mapping(ctx.get("larc_growth_observation"))
    adaptation = _number(growth.get("adaptation_points")) if growth.get("status") == "accepted" else None
    if adaptation is None:
        wallet = _mapping(ctx.get("larc_expedition_context"))
        if wallet.get("balance_current") is True and wallet.get("through_turn") == state.turn:
            adaptation = _number(wallet.get("points"))
    mood = ctx.get("mood")
    mood = 3 + MOODS[mood] if isinstance(mood, str) and mood in MOODS else _bounded(mood, 1, 5)
    target = goal.get("target_stats") if isinstance(goal, dict) else goal.target_stats
    policy = goal.get("larc_policy") if isinstance(goal, dict) else goal.larc_policy
    cards = profile.get("cards")
    support_types = [card.get("type") for card in cards if isinstance(card, dict)] if isinstance(cards, list) else None
    explicit_weights = _mapping(_mapping(policy).get("attribute_weights"))
    weights = dict(explicit_weights)
    rev = _mapping(policy).get("revision") if _mapping(policy).get("variant") == "baseline" else None
    from core.auto_trainer.larc_strategy_params import AUTO_ROUTED_REVISIONS, PRESET_REGISTRY, resolve_auto_preset
    from core.auto_trainer.larc_strategy_versions import _revision_version_key
    from core.auto_trainer.run_support_profile import canonical_type
    if isinstance(rev, str) and rev in PRESET_REGISTRY and (_revision_version_key(rev) >= (2, 3) or rev in AUTO_ROUTED_REVISIONS):
        if rev in AUTO_ROUTED_REVISIONS:
            raw_deck = (None if isinstance(goal, dict) else getattr(goal, "_support_deck_types", None)) or support_types or ()
            canon_deck = tuple(filter(None, (canonical_type(t) or (str(t).strip().lower() if isinstance(t, str) else None) for t in raw_deck)))
            preset = resolve_auto_preset(rev, canon_deck)
        else:
            preset = PRESET_REGISTRY[rev]
        weights = {**preset.attribute_weights_dict(), **explicit_weights}
    entry = _mapping(ctx.get("larc_ss_entry"))
    ready = next(
        (
            _bounded(offer.get("ready_count"), 0, 5)
            for offer in (preview, entry)
            if offer.get("status") == "accepted" and _bounded(offer.get("ready_count"), 0, 5) is not None
        ),
        None,
    )
    return {
        "year": period.get("year_index"),
        "junior_phase": phase_for_period(period),
        "calendar": calendar,
        "month": period.get("month"),
        "half": period.get("half"),
        "step": state.turn,
        "phase": period.get("stage"),
        "stats": _vector(state.current_stats),
        "target_stats": _vector(target),
        "goal_attribute_weights": {key: weights.get(key, 1.0) for key in ATTRIBUTES},
        "vitality": {"points": hp * capacity / 100 if hp is not None else None, "max_points": capacity, "percent": hp},
        "skill_pt": _number(state.current_stats.get("skill_pt")),
        "adaptation_pt": adaptation,
        "mood": mood,
        "ss_ready_count": ready,
        "sss": preview.get("kind") == "SSS" if preview.get("kind") in {"SS", "SSS"} else None,
        "support_types": support_types,
        "mei": _stage(ctx, state.turn, _current_run(state)),
        "sources": {"vitality_capacity": capacity_source, "vitality": "current_percent_bar"},
    }


def _person(raw, *, ss=False):
    identity, role = _mapping(raw.get("identity")), _mapping(raw.get("role"))
    name = identity.get("character_id") if identity.get("status") == "known" else None
    if name in {"1007", 1007}:
        name = "gold_ship"
    mei = name in {"satake_mei", "9043", 9043}
    kind = "mei_support" if mei else ROLE.get(role.get("kind"), role.get("kind")) if role.get("status") == "accepted" else None
    if (
        kind is None
        and identity.get("status") == "known"
        and identity.get("entity_kind") == "support_card"
        and identity.get("card_type") not in {None, "friend", "友人"}
    ):
        kind = "support_card"
    ref = _mapping(raw.get("member_ref"))
    member = ref.get("member_id") if ref.get("status") == "accepted" else None
    bond = _mapping(raw.get("bond"))
    points, ratio, bond_basis = None, None, "unknown"
    if bond.get("status") == "accepted":
        points = _bounded(bond.get("bond_points"), 0, 100)
        ratio = _bounded(bond.get("fill_ratio_estimate"), 0, 1)
        bond_basis = "confirmed_points" if points is not None else "estimated_visible_bond_bar"
        if points is None:
            points = 100 if bond.get("is_max") is True else ratio * 100 if ratio is not None else None
    charge = visible_charge(raw)
    reward = _mapping(raw.get("current_reward") if ss else raw.get("ss_reward"))
    reward_kind = reward.get("kind") if reward.get("status") == "accepted" and reward.get("earned") is not True else None
    hint = _mapping(raw.get("training_hint"))
    hint_reward = _mapping(hint.get("reward_on_hint")) if hint.get("status") == "accepted" else {}
    gain = _bounded(raw.get("bond_gain_regardless_member_success" if ss else "bond_gain_on_success_direct"), 0, 100)
    gain_basis = "known_direct_preview"
    if gain is None and kind in {"support_card", "mei_support"}:
        gain = 4 if kind == "mei_support" else 7
        gain_basis = "estimated_base_contact_no_unverified_charm"
    if gain is not None and points is not None:
        gain = min(gain, 100 - points)
    return {
        "member_id": member,
        "support_slot": raw.get("support_slot"),
        "role": kind,
        "character_id": "satake_mei" if mei else name,
        "identity_status": identity.get("status"),
        "card_id": identity.get("matched_card_id") if identity.get("status") == "known" else None,
        "bond_points": points,
        "bond_ratio": ratio,
        "bond_gain": gain,
        "charge_filled": charge[0] if charge is not None else None,
        "charge_capacity": 3 if charge is not None else None,
        "charge_gain": charge[1] if charge is not None else None,
        "hint": hint.get("present") if hint.get("status") == "accepted" else None,
        "hint_reward": hint_reward,
        "reward_kind": REWARD.get(reward_kind, reward_kind),
        "sources": {"bond": bond_basis, "bond_gain": gain_basis if gain is not None else "unknown"},
    }


def _bond(person, gain, probability, timing, *, before=None):
    before = person["bond_points"] if before is None else before
    threshold = {"support_card": 80, "mei_support": 60}.get(person["role"])
    effective = min(gain, max(0, threshold - before)) if None not in (gain, before, threshold) else None
    return {
        **{k: person[k] for k in ("member_id", "role", "support_slot")},
        "before_points": before,
        "gain_points": gain,
        "timing": timing,
        "threshold": threshold,
        "effective_gain_to_threshold": effective,
        "expected_gain_points": gain * probability if None not in (gain, probability) else None,
        "expected_effective_gain_to_threshold": effective * probability if None not in (effective, probability) else None,
        "basis": person["sources"]["bond_gain"],
    }


def _mei_reference(profile):
    run = profile.get("run_session_id") or "current_confirmed_mei"
    return resolve_mei_effects(profile or None, run, confirmed_outing=True)


def _training_mechanics(state, people, complete, success, profile, direct, inventory):
    unknown, bonds = [], []
    stats = dict.fromkeys(ATTRIBUTES, 0.0)
    pt, charge = 0.0, None
    hints = [p for p in people if p["role"] == "support_card" and p["hint"] is True]
    if hints:
        ambiguous = [p for p in people if p["role"] in {None, "support_card"} and p["hint"] is not False]
        pool = len(ambiguous) if complete else max(5, len(ambiguous))
        for person in hints:
            before, bond_gain = person["bond_points"], person["bond_gain"]
            after = min(100, before + bond_gain) if None not in (before, bond_gain) else None
            bond = _bond(
                person,
                min(5, 100 - after) if after is not None else None,
                success / pool if success is not None else None,
                "additional_event",
                before=after,
            )
            bonds.append({**bond, "event_probability_given_training_success": 1 / pool, "basis": "estimated_hint_selection_lower_bound"})
            reward = person["hint_reward"]
            if reward:
                for axis, value in _vector(reward.get("stats", reward.get("stats_display_gain"))).items():
                    if value is not None:
                        stats[axis] += value / pool
            else:
                unknown.append("hint_card_stat_and_proxy_effect")
    elif not complete or any(p["role"] in {None, "support_card"} and p["hint"] is not False for p in people):
        unknown.append("hint_pool")
    mei = next((p for p in people if p["role"] == "mei_support"), None)
    stage = state["mei"]["contact_stage"] if mei is not None else "no_visible_mei" if complete else "mei_presence_unknown"
    info = {"status": stage or "mei_contact_stage_unknown"}
    if mei is not None and stage in {"first_contact", "subsequent_proc"}:
        mood = state["mood"]
        mood_room = min(1, 5 - mood) if mood is not None else None
        prob = 1.0 if stage == "first_contact" else 0.4
        if stage == "first_contact":
            ref = _mei_reference(profile)
            bonus = ref["event"]
            guts, pt = 15 * (100 + bonus) // 100, 5 * (100 + bonus) // 100
            info.update(raw_guts=guts, spendable_pt=pt, mood_gain=mood_room, extra_charge=None, reference=ref["resolution"])
        else:
            priority = [p for p in people if p["charge_filled"] is not None and p["charge_filled"] + p["charge_gain"] < 3]
            current_ids = {p["member_id"] for p in people if p["member_id"] is not None}
            possible_overlap = sum(p["member_id"] is None and p["role"] != "mei_support" for p in people)
            outside = sum(member not in current_ids and cells < 3 for member, cells in inventory.items())
            outside = max(0, outside - possible_overlap) if complete else 0
            cells_floor = len(priority) + min(max(0, 5 - len(priority)), outside)
            charge = prob * cells_floor * success if success is not None else None
            guts, pt = 3 * prob, 3 * prob
            info.update(
                raw_guts_if_proc=3,
                spendable_pt_if_proc=3,
                mood_probability_if_proc=0.1,
                expected_mood_given_training_success=0.04 * mood_room if mood_room is not None else None,
                extra_charge={
                    "known_current_priority_count": len(priority),
                    "known_outside_draw_floor": outside,
                    "outside_pool_complete": len(inventory) == 15 and complete and possible_overlap == 0,
                },
            )
            if not info["extra_charge"]["outside_pool_complete"]:
                unknown.append("mei_outside_draw_roster")
        current_guts, direct_guts = state["stats"]["guts"], direct["stats"]["guts"]
        room_for_raw = 1200 - current_guts - (200 if direct_guts is None else direct_guts) if current_guts is not None else None
        if room_for_raw is not None and room_for_raw >= stats["guts"] + (guts if stage == "first_contact" else 3):
            stats["guts"] += guts
        else:
            if stats["guts"] == 0:
                stats["guts"] = None
            unknown.append("mei_guts_raw_display_conversion")
        before, bond_gain = mei["bond_points"], mei["bond_gain"]
        after = min(100, before + bond_gain) if None not in (before, bond_gain) else None
        bond = _bond(
            mei,
            min(10 if stage == "first_contact" else 7, 100 - after) if after is not None else None,
            prob * success if success is not None else None,
            "additional_event",
            before=after,
        )
        bonds.append({**bond, "event_probability_given_training_success": prob, "basis": "estimated_pinned_mei_event"})
        info.update(event_probability_given_training_success=prob, stage_basis=state["mei"]["contact_basis"])
    elif stage != "no_visible_mei":
        unknown.append("mei_current_event")
    return {
        "mei": info,
        "hint": {"status": "estimated_current_hint_bond" if hints else "no_confirmed_hint_card"},
        "additional_bond": bonds,
        "additional_stats_given_training_success": stats,
        "known_additional_skill_pt_given_training_success": pt,
        "known_expected_additional_charge_net": charge,
        "unknown_effects": unknown,
        "basis": "current_visible_mechanics_reference",
        "cn_verified": False,
    }


def _training(state, candidate, action, profile, page, inventory):
    evidence = gain_evidence(candidate)
    gains = {key: value for key, value in candidate.stats_gain.items() if key not in evidence["unverified_used_attributes"]}
    failure = _bounded(candidate.failure_rate, 0, 100)
    success = 1 - failure / 100 if failure is not None else None
    action["risk"].update(training_failure_percent=failure, failure_probability=failure / 100 if failure is not None else None)
    hp = state["vitality"]
    larc = _mapping(candidate.scenario_mechanics.get("larc"))
    preview = _mapping(larc.get("vitality_preview"))
    if accepted_preview(preview) and preview.get("training_type", candidate.training_type) == candidate.training_type:
        percent = preview["delta_percent"]
        delta, source = percent * hp["max_points"] / 100, "visible_bar_delta_and_" + state["sources"]["vitality_capacity"]
    else:
        delta = _number(candidate.vitality_delta)
        source = "current_vitality_delta_points" if delta is not None else "estimated_parent_training_cost"
        if delta is None:
            delta = 5 if candidate.training_type == "wisdom" else -20
        if hp["points"] is not None:
            delta = min(hp["max_points"] - hp["points"], max(-hp["points"], delta))
        percent = delta * 100 / hp["max_points"]
    action["direct"].update(
        stats=_vector(gains),
        skill_pt=_number(gains.get("skill_pt")),
        adaptation_pt=_number(larc.get("adaptation_pt_gain")),
        hp_points=delta,
        hp_percent_points=percent,
    )
    action["sources"].update(hp=source, stats="current_training_numeric_columns", risk="current_training_failure_rate")
    people, complete = page
    action.update(
        participants=people,
        resource_partial=not complete,
        training_mechanics=_training_mechanics(state, people, complete, success, profile, action["direct"], inventory),
    )
    packet = _mapping(larc.get("training_participants"))
    count = packet.get("visible_portrait_count")
    if (
        complete
        and packet.get("schema_version") == "larc_training_participants.v1"
        and packet.get("training_type") == candidate.training_type
        and type(count) is int
        and len(people) <= count <= 5
        and count == len(packet["participants"])
    ):
        action["participant_count"] = count
        action["sources"]["participant_count"] = "current_verified_training_lane"
    action["resource_effects"]["bond"] = [
        _bond(p, p["bond_gain"], success, "training_success") for p in people if p["role"] in {"support_card", "mei_support"}
    ]
    action["resource_effects"]["charge"] = [
        {
            "member_id": p["member_id"],
            "before_cells": p["charge_filled"],
            "gain_cells": p["charge_gain"],
            "expected_net": p["charge_gain"] * success if success is not None else None,
            "basis": "current_visible_net_cells",
        }
        for p in people
        if p["charge_gain"] is not None
    ]


def _page_people(rows):
    people, seen = [], {}
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        person = _person(raw)
        key = ("mei",) if person["role"] == "mei_support" else ("member", person["member_id"]) if person["member_id"] is not None else None
        if key is not None and key in seen:
            previous = seen[key]
            for field, value in person.items():
                if previous.get(field) is None:
                    previous[field] = value
            continue
        if key is not None:
            seen[key] = person
        people.append(person)
    return people


def _lower(value):
    return _number(value[0]) if isinstance(value, (list, tuple)) and value else _number(value)


def _heal(hp, points, cap=0):
    if hp["points"] is None or points is None:
        return None, None
    after, capacity = recover_points(hp["points"], hp["max_points"], points, cap)
    return after - hp["points"], after / capacity * 100 - hp["percent"]


def _mei_hp_distribution(effects):
    source = "current_mei_outing_effects"
    if "vitality_branches" in effects:
        branches = effects["vitality_branches"]
        if not isinstance(branches, (list, tuple)) or not branches:
            return None
    else:
        points = effects.get("vitality_points")
        if isinstance(points, (list, tuple)):
            if len(points) != 2 or _number(points[0]) != _number(points[1]):
                return None
            points = points[0]
        branches = [{"weight": 1.0, "hp_points": points}]
    resolved = []
    for branch in branches:
        branch = _mapping(branch)
        weight = _bounded(branch.get("weight"), 0, 1)
        points = _number(branch.get("hp_points"))
        cap = _number(branch.get("hp_cap_points", 0))
        if None in (weight, points, cap) or points < 0 or cap < 0:
            return None
        resolved.append({"weight": weight, "hp_points": points, "hp_cap_points": cap, "source": branch.get("source") or source})
    if not isclose(sum(branch["weight"] for branch in resolved), 1.0, abs_tol=1e-9, rel_tol=0.0):
        return None
    return {"branches": resolved, "source": source}


def _recovery(state, row, action, people, profile):
    kind, hp = action["kind"], state["vitality"]
    direct = _direct(zero=True)
    action.update(direct=direct, participants=[], direct_basis="estimated_normal_recovery_reference")
    if kind == "rest":
        branches = [_heal(hp, points) for points, _ in DOMESTIC_REST_REFERENCE]
        direct["hp_points"], direct["hp_percent_points"] = (
            sum(branch[i] * weight for branch, (_, weight) in zip(branches, DOMESTIC_REST_REFERENCE))
            if hp["points"] is not None
            else None
            for i in (0, 1)
        )
        action["risk"].update(failure_probability=0.0, failure_basis="rest_no_training_failure")
        action["sources"]["hp"] = "estimated_frozen_b_c1_domestic_rest_reference"
    elif kind == "outing":
        direct.update(mood=min(1, 5 - state["mood"]) if state["mood"] is not None else None, hp_points=None, hp_percent_points=None)
        action["sources"]["mood"] = "estimated_parent_ordinary_outing_one_level"
    else:
        effects = _mapping(row.get("effects"))
        distribution = _mei_hp_distribution(effects)
        if distribution is not None:
            action["hp_distribution"] = distribution
        if not effects:
            stage = action["action_id"].rsplit(".", 1)[-1]
            stage = int(stage) if stage in {"1", "2", "3", "4", "5"} else state["mei"]["next_stage"]
            if type(stage) is int and 1 <= stage <= 5:
                ref = _mei_reference(profile)
                effects = outing_effects(stage, recovery_bonus=ref["recovery"], event_bonus=ref["event"])
                effects["support_effect_resolution"] = ref["resolution"]
        heal = _lower(effects.get("vitality_points"))
        direct["hp_points"], direct["hp_percent_points"] = _heal(hp, heal)
        stats = _mapping(effects.get("stats"))
        direct["stats"] = {axis: _lower(stats[axis]) if axis in stats else 0 if stats else None for axis in ATTRIBUTES}
        direct["mood"] = _number(effects.get("mood_levels"))
        direct["bond_gain"] = _number(effects.get("friend_bond"))
        if direct["mood"] is not None and state["mood"] is not None:
            direct["mood"] = min(5 - state["mood"], direct["mood"])
        mei = next((p for p in people if p["role"] == "mei_support"), None)
        if mei is None:
            mei = {
                "member_id": None,
                "support_slot": None,
                "role": "mei_support",
                "bond_points": None,
                "sources": {"bond_gain": "current_legal_mei_outing_reference"},
            }
        if direct["bond_gain"] is not None:
            action["resource_effects"]["bond"] = [_bond(mei, direct["bond_gain"], 1.0, "fixed")]
        action["sources"].update(hp="estimated_normal_outing_effects", mei_effects=effects.get("support_effect_resolution"))


def _member_probability(row, rates, guaranteed):
    if guaranteed or row.get("win_guaranteed") is True:
        return 1.0
    rate = rates.get(row.get("slot"), {})
    if rate.get("guaranteed") is True and rate.get("band") == "blue":
        return 1.0
    return _bounded(row.get("success_probability", rate.get("success_probability")), 0, 1)


def _member_reward(raw, person):
    packet = _mapping(raw.get("reward_on_member_success", raw.get("reward_on_success")))
    reward = _delta_packet(packet)
    kind = person["reward_kind"]
    icon = _mapping(raw.get("current_reward"))
    amount = _number(icon.get("amount"))
    heal, cap = reward["hp_points"], reward["hp_cap_points"]
    if kind in {"energy", "energy_and_mood", "energy_and_cap"}:
        heal = amount if amount is not None else 20
        cap = 4 if kind == "energy_and_cap" else 0
    if kind == "energy_and_mood" and reward["mood"] is None:
        reward["mood"] = 1
    if kind == "adaptation_pt" and reward["adaptation_pt"] is None:
        reward["adaptation_pt"] = amount if amount is not None else 50
    if kind == "charm":
        reward["charm"] = True
    return reward, heal, cap


def _member_hp_on_success(kind, reward, heal, cap):
    if kind in {"adaptation_pt", "recharge", "skill_hint", "stat_gain", "skill_pt", "charm", "practice_good"}:
        heal, cap = 0, 0
    elif kind not in {"energy", "energy_and_mood", "energy_and_cap"} or heal < 0:
        return None
    for name, nominal in (("hp_points", heal), ("hp_cap_points", cap)):
        observed = reward[name]
        if observed is not None and not 0 <= observed <= nominal:
            return None
    return {"hp_points": heal, "hp_cap_points": cap, "source": "current_ss_reward_icon_and_frozen_recovery_rule"}


def _expected_healing(hp, fixed, members, independent):
    fixed_heal, fixed_cap = fixed["hp_points"], fixed["hp_cap_points"]
    known = [(p, heal or 0, cap or 0) for p, heal, cap in members if p is not None and (heal is not None or cap is not None)]
    if fixed_heal is None and fixed_cap is None and not known:
        return None, None, None
    certain = [row for row in known if row[0] == 1]
    heal, cap = (fixed_heal or 0) + sum(row[1] for row in certain), (fixed_cap or 0) + sum(row[2] for row in certain)
    uncertain = [row for row in known if 0 < row[0] < 1]
    if hp["points"] is None:
        return None, None, cap + sum(p * extra_cap for p, _, extra_cap in uncertain)
    if len(uncertain) <= 1 or independent:
        outcomes = []
        for hits in product((False, True), repeat=len(uncertain)):
            weight = prod(p if hit else 1 - p for hit, (p, _, _) in zip(hits, uncertain))
            applied = [row for hit, row in zip(hits, uncertain) if hit]
            h, c = heal + sum(row[1] for row in applied), cap + sum(row[2] for row in applied)
            outcomes.append((*_heal(hp, h, c), c, weight))
        return tuple(sum(row[i] * row[3] for row in outcomes) for i in range(3))
    base_hp, base_percent = _heal(hp, heal, cap)
    addition = max(p * (_heal(hp, heal + h, cap + c)[0] - base_hp) for p, h, c in uncertain)
    return base_hp + addition, base_percent if addition == 0 else None, cap + sum(p * c for p, _, c in uncertain)


def _ss_action(state, action, preview, win_policy):
    accepted = preview.get("status") == "accepted" and preview.get("stage") == "preview" and preview.get("kind") in {"SS", "SSS"}
    if not accepted:
        action["unknown"].append("current_ss_preview")
        return
    victory = win_policy if isinstance(win_policy, dict) else preview.get("win_policy")
    if not isinstance(victory, dict):
        victory = ss_victory_evidence(preview)
    guaranteed = victory.get("guaranteed") is True
    count = preview.get("ready_count")
    observed = _mapping(preview.get("participants_observation"))
    rows = observed.get("participants")
    rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    rates = {row.get("slot"): row for row in observed.get("win_rates") or [] if isinstance(row, dict)} if observed.get("win_rates_status") == "accepted" else {}
    all_win_p = 1.0 if guaranteed else _bounded(victory.get("all_members_success_probability"), 0, 1)
    independent = victory.get("member_draw_model") == "independent_bernoulli_in_upstream_runSS"
    action["risk"].update(ss_all_win_probability=all_win_p, failure_probability=1 - all_win_p if all_win_p is not None else None)
    panel = _delta_packet(preview.get("all_members_succeed_direct_rewards"))
    gains = _mapping(preview.get("stats_gain"))
    if gains:
        panel["stats"] = _vector(gains)
        panel["skill_pt"] = _number(gains.get("skill_pt"))
    panel_ap = _number(preview.get("adaptation_points_if_all_win"))
    if panel_ap is not None:
        panel["adaptation_pt"] = panel_ap
    fixed = _delta_packet(preview.get("fixed_direct", preview.get("base_rewards")))
    if fixed["adaptation_pt"] is None and preview.get("kind") == "SS" and count in {4, 5}:
        fixed["adaptation_pt"] = 10 * count
        action["sources"]["ss_base_ap"] = "estimated_parent_ordinary_ss_reference"
    people, conditional, hp_members, charge, bonds = [], [], [], [], []
    seen = set()
    for index, raw in enumerate(rows):
        person = _person(raw, ss=True)
        key = ("member", person["member_id"]) if person["member_id"] is not None else ("slot", raw.get("slot", index))
        if key in seen:
            continue
        seen.add(key)
        people.append(person)
        p = _member_probability(raw, rates, guaranteed)
        reward, heal, cap = _member_reward(raw, person)
        member = {"member_id": person["member_id"], "reward_kind": person["reward_kind"], "success_probability": p, "reward_on_success": reward}
        hp_on_success = _member_hp_on_success(person["reward_kind"], reward, heal, cap)
        if hp_on_success is not None:
            member["hp_on_success"] = hp_on_success
        conditional.append(member)
        hp_members.append((p, heal, cap))
        if person["role"] == "support_card":
            bonds.append(_bond(person, person["bond_gain"], 1.0, "fixed"))
        before = _bounded(raw.get("charge_before", person["charge_filled"]), 0, 3)
        before = 3 if before is None else before
        without = _bounded(raw.get("charge_after_without_member_reward"), 0, 3)
        without = 0 if without is None else without
        after = _bounded(raw.get("charge_after_on_member_success"), 0, 3)
        if after is None and person["reward_kind"] is not None:
            after = 3 if person["reward_kind"] == "recharge" else 0
        refill = (after - without) * p if None not in (p, after) else None
        charge.append({
            "member_id": person["member_id"],
            "fixed_consumed_cells": before - without,
            "expected_net": without - before + (refill or 0),
            "basis": "known_fixed_and_conditional" if refill is not None else "known_clear_refill_unknown",
        })
    if type(count) is int and 0 <= len(people) < count <= 5:
        charge.append({
            "member_id": None,
            "fixed_consumed_cells": 3 * (count - len(people)),
            "expected_net": -3 * (count - len(people)),
            "basis": "estimated_known_group_count_unmatched_rewards",
        })
    complete = type(count) is int and len(people) == count
    expected = _direct()
    known = [row for row in conditional if row["success_probability"] is not None]
    for key in ("skill_pt", "adaptation_pt"):
        values = [row["reward_on_success"][key] * row["success_probability"] for row in known if row["reward_on_success"][key] is not None]
        if fixed[key] is not None or values:
            expected[key] = (fixed[key] or 0) + sum(values)
    for axis in ATTRIBUTES:
        values = [row["reward_on_success"]["stats"][axis] * row["success_probability"] for row in known if row["reward_on_success"]["stats"][axis] is not None]
        base = fixed["stats"][axis]
        if base is not None or values:
            expected["stats"][axis] = (base or 0) + sum(values)
    expected["hp_points"], expected["hp_percent_points"], expected["hp_cap_points"] = _expected_healing(
        state["vitality"], fixed, hp_members, independent
    )
    if guaranteed:
        expected.update({key: value for key, value in panel.items() if key != "stats" and value is not None})
        expected["stats"].update({key: value for key, value in panel["stats"].items() if value is not None})
    charm = [row["success_probability"] for row in known if row["reward_on_success"]["charm"] is True]
    action.update(
        direct=panel,
        direct_basis="current_ss_all_members_success_preview",
        participants=people,
        resource_partial=not complete,
        displayed_preview={"direct": panel, "scope": "all_members_win", "is_actual_receipt": False},
        ss_effects={
            "fixed_direct": fixed,
            "expected_direct": expected,
            "conditional_members": conditional,
            "complete_visible_group": complete,
            "independent_members": independent,
            "expected_charm_lower_bound": max(charm) if charm else None,
            "expected_charm_basis": "confirmed_current_member_success_probability_lower_bound" if charm else None,
        },
    )
    action["resource_effects"].update(bond=bonds, charge=charge)
    action["sources"]["ss_victory"] = victory.get("basis", "unknown")
    action["sources"]["ss_current_reward_effects"] = "estimated_visible_icon_rule_where_preview_not_supplied"
    if not complete:
        action["unknown"].append("ss_other_member_rewards")
    if not guaranteed and all_win_p is None:
        action["unknown"].append("ss_member_risk_or_fixed_panel_split")


def project(state, goal, rows, *, ss_preview=None, win_policy=None, support_profile=None):
    """Return a canonical first-year menu without choosing or executing an action."""
    preview = _mapping(ss_preview if ss_preview is not None else state.scenario_state.get("larc_ss_preview"))
    profile = _declared_profile(state, support_profile)
    current = _state_packet(state, goal, preview, profile)
    menu = {"state": current, "actions": [], "unknown": []}
    if current["year"] != 1 or state.screen != "training_menu":
        menu["unknown"].append("outside_verified_first_year_training_menu")
        return menu
    candidates = {candidate.training_type: candidate for candidate in state.training_options}
    pages, sightings = {}, defaultdict(set)
    for tab, candidate in candidates.items():
        observed, complete = training_rows(candidate) or ([], False)
        people = _page_people(observed)
        pages[tab] = (people, complete)
        for person in people:
            if person["member_id"] is not None and person["charge_filled"] is not None:
                sightings[person["member_id"]].add(person["charge_filled"])
    inventory = {key: next(iter(values)) for key, values in sightings.items() if len(values) == 1}
    mei = [person for people, _ in pages.values() for person in people if person["role"] == "mei_support"]
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("action_id"), str):
            continue
        aid = row["action_id"]
        tab = row.get("training_type", aid.partition(":")[2]) if aid.startswith("train:") else None
        if tab not in ATTRIBUTES and aid.startswith("train:") and aid.partition(":")[2] in {"0", "1", "2", "3", "4"}:
            tab = ATTRIBUTES[int(aid.partition(":")[2])]
        kind = (
            "train"
            if aid.startswith("train:")
            else "ss"
            if aid == "ss"
            else "rest"
            if aid in {"rest", "rest.normal"}
            else "mei_outing"
            if aid == "mei_outing" or aid.startswith("outing.satake_mei.")
            else "outing"
            if aid in {"outing", "outing.normal"}
            else None
        )
        action = {
            "action_id": aid,
            "kind": kind,
            "tab": tab,
            "scorable": kind is not None,
            "direct": _direct(),
            "direct_basis": "current_normal_action",
            "risk": {"failure_probability": None, "training_failure_percent": None, "ss_all_win_probability": None},
            "participants": None,
            "resource_effects": {"bond": [], "charge": []},
            "ss_effects": None,
            "training_mechanics": None,
            "sources": {},
            "unknown": [],
        }
        if kind == "train" and tab in candidates:
            _training(current, candidates[tab], action, profile, pages[tab], inventory)
        elif kind == "ss":
            _ss_action(current, action, preview, win_policy)
        elif kind in {"rest", "outing", "mei_outing"}:
            _recovery(current, row, action, mei, profile)
        else:
            action["unknown"].append("current_action_details")
        menu["actions"].append(action)
    return menu


# ---------------------------------------------------------------------------
# Mechanics & Base Marginal Pricing
# ---------------------------------------------------------------------------

def apply_known_mechanics(action):
    """Apply each normalized V4 training event exactly once to a fresh action."""
    out = deepcopy(action)
    if out.get("mechanics_applied") is True or out.get("direct_basis") == "shadow_base_plus_known_pinned_training_events":
        return out, []
    extra = out.get("training_mechanics")
    unknown = []
    if not isinstance(extra, dict):
        if out.get("kind") == "train":
            unknown.append("training_mechanics")
        return out, unknown
    direct = out.get("direct")
    resources = out.get("resource_effects")
    if not isinstance(direct, dict):
        return out, ["direct"]
    stats = direct.get("stats")
    extra_stats = extra.get("additional_stats_given_training_success")
    if isinstance(stats, dict) and isinstance(extra_stats, dict):
        for axis in ATTRIBUTES:
            base, gain = finite(stats.get(axis)), finite(extra_stats.get(axis))
            if base is not None and gain is not None:
                stats[axis] = base + gain
            elif base is None and gain is not None and gain >= 0 and out.get("kind") == "train":
                stats[axis] = gain
                unknown.append("direct.stats_base." + axis)
            elif gain is None:
                unknown.append("mechanics.stats." + axis)
    else:
        unknown.append("mechanics.stats")
    pt = finite(extra.get("known_additional_skill_pt_given_training_success"))
    if finite(direct.get("skill_pt")) is not None and pt is not None:
        direct["skill_pt"] += pt
    elif pt is not None and pt >= 0:
        direct["skill_pt"] = pt
        unknown.append("direct.skill_pt_base")
    elif pt is None:
        unknown.append("mechanics.skill_pt")
    if isinstance(resources, dict):
        bonds = resources.get("bond")
        additions = extra.get("additional_bond")
        if isinstance(bonds, list) and isinstance(additions, list):
            bonds.extend(additions)
        elif not isinstance(bonds, list) and isinstance(additions, list):
            resources["bond"] = additions
            unknown.append("bond.base")
        elif additions is None:
            unknown.append("mechanics.bond")
        charge = finite(extra.get("known_expected_additional_charge_net"))
        if charge is not None and isinstance(resources.get("charge"), list):
            resources["charge"].append({"member_id": None, "expected_net": charge, "basis": "proc_visible_floor"})
        elif charge is not None and not isinstance(resources.get("charge"), list):
            resources["charge"] = [{"member_id": None, "expected_net": charge, "basis": "proc_visible_floor"}]
            unknown.append("charge.base")
        elif charge is None and (extra.get("mei") or {}).get("status") == "subsequent_proc":
            unknown.append("mechanics.extra_charge")
    else:
        unknown.append("resource_effects")
    unknown.extend(x for x in extra.get("unknown_effects", []) if isinstance(x, str))
    return out, unknown


def mei_phase_bonus(state, action, *, params=DEFAULT_PARAMS):
    phase = state.get("junior_phase")
    note = {"phase": phase, "nominal_q": 0.0, "value": 0.0, "status": "non_training"}
    if action.get("kind") != "train":
        return note
    if state.get("year") != 1:
        return {**note, "status": "outside_first_year"}
    amount = {
        "junior_early": params.year1_mei_junior_early_bonus,
        "junior_late": params.year1_mei_junior_late_bonus,
    }.get(phase)
    if amount is None:
        return {**note, "status": "phase_unknown"}
    people = action.get("participants")
    if not isinstance(people, list):
        return {**note, "status": "participants_unknown"}
    if not any(isinstance(row, dict) and row.get("role") == "mei_support" for row in people):
        return {**note, "status": "no_confirmed_mei"}
    risk = finite((action.get("risk") or {}).get("failure_probability"))
    if risk is None or not 0 <= risk <= 1:
        return {**note, "nominal_q": amount, "status": "training_risk_unknown"}
    return {
        **note,
        "nominal_q": amount,
        "value": amount * (1.0 - risk),
        "status": "confirmed_mei",
        "training_success_probability": 1.0 - risk,
    }


def _preferences(preferences):
    if preferences is None:
        return dict(DEFAULT_PREFERENCES)
    if not isinstance(preferences, dict):
        raise ValueError("preferences must be a dictionary")
    result = dict(DEFAULT_PREFERENCES)
    for key in result:
        if key in preferences:
            value = finite(preferences[key])
            if value is None or value < 0:
                raise ValueError("preference must be finite and nonnegative: " + key)
            result[key] = value
    return result


def _extra_presence(action, preferences, probability):
    notes = {"gold_ship_bonus": 0.0, "mei_bonus": 0.0}
    unknown = []
    if action.get("kind") != "train" or not any(preferences[k] for k in notes):
        return notes, unknown
    people = action.get("participants")
    if not isinstance(people, list):
        return notes, ["current_participants_for_user_bonus"]
    if probability is None or not 0 <= probability <= 1:
        return notes, ["training_risk_for_user_bonus"]
    known = set()
    identity_unknown = False
    for person in people:
        if not isinstance(person, dict):
            continue
        identity = person.get("identity")
        if isinstance(identity, dict):
            name = identity.get("character_id") if identity.get("status") == "known" else None
        else:
            name = person.get("character_id") if person.get("identity_status") == "known" else None
        if name in ("gold_ship", "satake_mei"):
            known.add(name)
        if name is None and person.get("role") != "mei_support":
            identity_unknown = True
        if person.get("role") == "mei_support":
            known.add("satake_mei")
    for character, key in (("gold_ship", "gold_ship_bonus"), ("satake_mei", "mei_bonus")):
        if character in known:
            notes[key] = preferences[key] * (1.0 - probability)
        elif preferences[key] and (action.get("resource_partial") is True or identity_unknown):
            unknown.append("user_bonus_presence." + character)
    return notes, unknown


def score_action(state, action, *, preferences=None, params=DEFAULT_PARAMS):
    """Return B marginal score, components and evidence; no mutation or I/O."""
    coefficients = params.year1_coefficients
    pref = _preferences(preferences)
    action, mechanics_unknown = apply_known_mechanics(action)
    direct = action.get("direct") or {}
    risk = action.get("risk") or {}
    kind = action.get("kind")
    unknown = list(mechanics_unknown)
    unknown.extend(item for item in action.get("unknown", []) if isinstance(item, str))
    explicit = finite(risk.get("failure_probability"))
    raw = finite(risk.get("training_failure_percent"))
    allwin = finite(risk.get("ss_all_win_probability"))
    probability = (
        explicit
        if explicit is not None and 0 <= explicit <= 1
        else raw / 100
        if raw is not None
        else 1 - allwin
        if allwin is not None
        else None
    )
    success = (1.0 - probability if probability is not None else 1.0) if kind == "train" else 1.0
    if probability is None:
        unknown.append("risk")
    if kind == "ss":
        ss = action.get("ss_effects") or {}
        expected, fixed = ss.get("expected_direct") or {}, ss.get("fixed_direct") or {}

        def pick(key):
            value = finite(expected.get(key))
            if value is None:
                unknown.append("ss_expected_" + key)
                return finite(fixed.get(key))
            return value

        exp_stats, fixed_stats = expected.get("stats") or {}, fixed.get("stats") or {}
        direct_stats = {}
        for key in ATTRIBUTES:
            value = finite(exp_stats.get(key))
            if value is None:
                unknown.append("ss_expected_stats." + key)
                value = finite(fixed_stats.get(key))
            direct_stats[key] = value
        pt, hp, ap = pick("skill_pt"), pick("hp_points"), pick("adaptation_pt")
    else:
        direct_stats = direct.get("stats") or {}
        pt, hp, ap = (finite(direct.get(key)) for key in ("skill_pt", "hp_points", "adaptation_pt"))
    weights, stats, targets = state.get("goal_attribute_weights") or {}, state.get("stats") or {}, state.get("target_stats") or {}
    numeric = 0.0
    for key in ATTRIBUTES:
        gain, current, target, weight = (
            finite(direct_stats.get(key)),
            finite(stats.get(key)),
            finite(targets.get(key)),
            finite(weights.get(key, 1.0)),
        )
        if None in (gain, current, target, weight):
            unknown.append("stats." + key)
        else:
            numeric += max(0.0, min(gain, max(0.0, target - current))) * weight * success
    if pt is None:
        unknown.append("skill_pt")
    else:
        numeric += 0.85 * max(0.0, pt) * success
    if hp is None:
        unknown.append("hp_points")
    if ap is None:
        unknown.append("adaptation_pt")
    resource = action.get("resource_effects") or {}
    bonds, charges = resource.get("bond"), resource.get("charge")
    support_bond = mei_bond = charge = 0.0
    if not isinstance(bonds, list):
        unknown.append("bond")
    else:
        for row in bonds:
            role = row.get("role")
            if role not in ("support_card", "mei_support"):
                if role is None:
                    unknown.append("bond_role")
                continue
            value = finite(row.get("expected_effective_gain_to_threshold"))
            if value is None:
                value = finite(row.get("expected_gain_points"))
                unknown.append("bond_threshold")
            if value is None:
                unknown.append("bond_effect")
            elif role == "support_card":
                support_bond += value
            else:
                mei_bond += value
    if kind == "mei_outing" and not bonds:
        value = finite(direct.get("bond_gain"))
        if value is not None:
            mei_bond += value
            unknown.append("bond_threshold")
    if not isinstance(charges, list):
        unknown.append("charge")
    else:
        for row in charges:
            basis = row.get("basis")
            if isinstance(basis, str) and "refill_unknown" in basis:
                unknown.append("charge.conditional_refill_unknown")
            value = finite(row.get("expected_net"))
            if value is None:
                unknown.append("charge_net")
            else:
                charge += value
    charm = direct.get("charm") if kind != "ss" else None
    if kind == "ss":
        ss = action.get("ss_effects") or {}
        members = ss.get("conditional_members")
        if isinstance(members, list) and ss.get("complete_visible_group") is True:
            charms = [row for row in members if (row.get("reward_on_success") or {}).get("charm") is True]
            probs = [finite(row.get("success_probability")) for row in charms]
            if not charms:
                charm = 0.0
            elif all(p is not None and 0 <= p <= 1 for p in probs) and (len(probs) == 1 or ss.get("independent_members") is True):
                charm = 1.0 - prod(1.0 - p for p in probs)
        if charm is None and isinstance(members, list):
            lower = finite(ss.get("expected_charm_lower_bound"))
            known = [finite(row.get("success_probability")) for row in members if (row.get("reward_on_success") or {}).get("charm") is True]
            known = [p for p in known if p is not None and 0 <= p <= 1]
            if lower is not None and 0 <= lower <= 1 and known and lower <= max(known) + 1e-12:
                charm = lower
                unknown.append("ss_other_member_charm")
    if type(charm) is bool:
        charm = float(charm) * success
    elif kind == "ss":
        charm = finite(charm)
    else:
        charm = None
    if charm is None:
        unknown.append("charm")
    values = (
        (hp if hp is not None else 0.0) / 100.0,
        (ap if ap is not None else 0.0) / 100.0 * success,
        (support_bond + mei_bond) / 100.0,
        charge / 10.0,
        charm or 0.0,
        (probability or 0.0) if kind == "train" else 0.0,
        (probability or 0.0) if kind == "ss" else 0.0,
    )
    from core.auto_trainer.larc_strategy_extension import active_provider

    hp_scale, charm_scale = active_provider().year1_scales(params)
    parts = {name: c * v for name, c, v in zip(EFFECTS, coefficients, values)}
    parts["hp"] *= float(hp_scale)
    parts["charm"] *= float(charm_scale)
    parts["bond"] = coefficients[2] * (support_bond * pref["bond"] + mei_bond) / 100.0
    parts["charge"] *= pref["charge"]
    parts["numeric"] = numeric * pref["numeric"]
    phase = mei_phase_bonus(state, action, params=params)
    parts["mei_phase"] = phase["value"]
    extra, presence_unknown = _extra_presence(action, pref, probability)
    parts.update(extra)
    unknown.extend(presence_unknown)
    if phase["status"] in ("phase_unknown", "participants_unknown", "training_risk_unknown"):
        unknown.append("mei_phase." + phase["status"])
    if action.get("resource_partial") is True:
        unknown.append("other_current_participant_charge_unknown")
    unknown = sorted(set(unknown))
    direct_basis = action.get("direct_basis") or "unknown"
    mechanics_basis = (action.get("training_mechanics") or {}).get("basis") or "unknown"
    known_sources = ["goal_target_and_weights", "configured_unit_prices"]
    estimated_sources = []
    if direct_basis != "unknown":
        (estimated_sources if "estimat" in str(direct_basis).lower() or "reference" in str(direct_basis).lower() else known_sources).append("direct:" + str(direct_basis))
    if kind == "train" and mechanics_basis != "unknown":
        estimated_sources.append("training_mechanics:" + str(mechanics_basis))
    if kind == "ss":
        known_sources.append("ss_fixed_when_supplied")
        estimated_sources.append("ss_member_conditional_expectation_when_supplied")
    return {
        "score": sum(parts.values()),
        "components": parts,
        "effects": dict(zip(EFFECTS, values)),
        "sources": {
            "known": known_sources,
            "estimated": estimated_sources,
            "unknown": unknown,
            "direct": direct_basis,
            "mechanics": mechanics_basis,
            "ss": "expected_fixed_and_conditional" if kind == "ss" else None,
            "charge_rows": [
                {"member_id": row.get("member_id"), "basis": row.get("basis"), "expected_net": row.get("expected_net")}
                for row in charges
                if isinstance(row, dict)
            ]
            if isinstance(charges, list)
            else None,
            "phase": phase,
            "preference": "user_configured",
        },
        "unknown": unknown,
        "preferences": pref,
        "value_kind": "current_menu_marginal_score_not_absolute_q",
    }


# ---------------------------------------------------------------------------
# Combo198 + Contact + Close Repricing
# ---------------------------------------------------------------------------

def _failure(action):
    risk = _mapping(action.get("risk"))
    percent = finite(risk.get("training_failure_percent"))
    from_percent = _unit(percent / 100) if percent is not None else None
    raw = risk.get("failure_probability")
    explicit = _unit(raw)
    if (raw is not None and explicit is None) or (percent is not None and from_percent is None):
        return None
    if from_percent is not None and explicit is not None and not isclose(from_percent, explicit, abs_tol=1e-12):
        return None
    return from_percent if from_percent is not None else explicit


def _branch(heal, cap, weight, source):
    return {"hp_points": heal, "hp_cap_points": cap, "weight": weight, "source": source}


def _hp_packet(packet):
    packet = _mapping(packet)
    heal, cap = finite(packet.get("hp_points")), finite(packet.get("hp_cap_points"))
    if heal is None or cap is None or cap < 0:
        return None
    return heal, cap


def _ss_branches(action):
    effects, risk = _mapping(action.get("ss_effects")), _mapping(action.get("risk"))
    displayed = _mapping(action.get("displayed_preview"))
    all_win = _unit(risk.get("ss_all_win_probability"))
    if all_win == 1 and displayed.get("scope") == "all_members_win":
        panel = _hp_packet(displayed.get("direct"))
        if panel is not None:
            source = "current_guaranteed_all_win_hp_and_cap"
            return [_branch(*panel, 1.0, source)], source

    fixed = _hp_packet(effects.get("fixed_direct"))
    rows = effects.get("conditional_members")
    if fixed is None or effects.get("complete_visible_group") is not True or not isinstance(rows, list) or not 1 <= len(rows) <= 5:
        return None, "ss_fixed_hp_cap_or_current_group_unknown"
    heal, cap = fixed
    uncertain = []
    sources = []
    probabilities = []
    for row in rows:
        row = _mapping(row)
        p = _unit(row.get("success_probability"))
        hp = _mapping(row.get("hp_on_success"))
        effect = _hp_packet(hp)
        if p != 0 and effect is None:
            return None, "ss_member_hp_or_cap_unknown"
        if p is None and effect != (0.0, 0.0):
            return None, "ss_member_probability_unknown"
        probabilities.append(p)
        if p == 0 or effect == (0.0, 0.0):
            continue
        sources.append(hp.get("source") or "canonical_member_nominal_hp_cap")
        if p == 1:
            heal += effect[0]
            cap += effect[1]
        else:
            uncertain.append((p, *effect))

    independent = effects.get("independent_members") is True
    if len(uncertain) > 1 and not independent:
        return None, "ss_member_dependence_unknown"
    if independent and all_win is not None and None not in probabilities and not isclose(prod(probabilities), all_win, abs_tol=1e-9):
        return None, "ss_all_win_probability_conflict"
    if all_win == 1 and any(p is not None and p != 1 for p in probabilities):
        return None, "ss_all_win_probability_conflict"
    source = "current_ss_independent_hp_members" if len(uncertain) > 1 else "current_ss_known_hp_members"
    branches = []
    for hits in product((False, True), repeat=len(uncertain)):
        weight = prod(p if hit else 1 - p for hit, (p, _, _) in zip(hits, uncertain))
        branches.append(
            _branch(
                heal + sum(h for hit, (_, h, _) in zip(hits, uncertain) if hit),
                cap + sum(c for hit, (_, _, c) in zip(hits, uncertain) if hit),
                weight,
                {"fixed": "current_ss_fixed_hp_cap", "members": sources},
            )
        )
    return branches, source


def _hp_branches(action):
    kind = action.get("kind")
    supplied = _mapping(action.get("hp_distribution"))
    if supplied:
        rows = supplied.get("branches")
        branches = []
        if isinstance(rows, list):
            for row in rows:
                effect = _hp_packet(row)
                weight = _unit(_mapping(row).get("weight"))
                if effect is None or weight is None:
                    return None, "current_hp_distribution_incomplete"
                branches.append(_branch(*effect, weight, row.get("source") or supplied.get("source")))
        if not branches or not isclose(sum(row["weight"] for row in branches), 1.0, abs_tol=1e-8):
            return None, "current_hp_distribution_incomplete"
        return branches, supplied.get("source") or "canonical_current_hp_distribution"
    if kind == "train":
        direct, sources = _mapping(action.get("direct")), _mapping(action.get("sources"))
        heal = finite(direct.get("hp_points"))
        failure = _failure(action)
        if failure is None or heal is None or sources.get("hp") == "estimated_parent_training_cost":
            return None, "current_training_hp_or_failure_unknown"
        big = failure ** 2 if failure >= 0.2 else 0.0
        return [
            _branch(heal, 0.0, 1 - failure, sources.get("hp") or "canonical_training_success_hp"),
            _branch(0.0, 0.0, failure - big, "estimated_model_small_failure_hp"),
            _branch(10.0, 0.0, big, "estimated_model_big_failure_hp"),
        ], "training_failure_branches_pinned_model"
    if kind in {"rest", "outing"}:
        reference = DOMESTIC_REST_REFERENCE if kind == "rest" else ((0, 0.5), (10, 0.5))
        source = "estimated_pinned_model_domestic_" + kind + "_hp_distribution"
        return [_branch(heal, 0.0, weight, source) for heal, weight in reference], source
    if kind == "ss":
        return _ss_branches(action)
    return None, "current_recovery_distribution_unknown"


def _hp_quote(state, action, evidence, *, params=DEFAULT_PARAMS):
    from core.auto_trainer.larc_strategy_extension import active_provider

    alpha = params.combo198_hp_alpha
    hp_scale, _ = active_provider().year1_scales(params)
    hp_price = (params.year1_coefficients[0] * float(hp_scale)) / 100.0
    vitality = _mapping(state.get("vitality"))
    current, capacity = finite(vitality.get("points")), finite(vitality.get("max_points"))
    old_q = finite(_mapping(evidence.get("components")).get("hp"))
    note = {
        "old_hp_q": old_q,
        "hp_delta_q": 0.0,
        "alpha": alpha,
        "vitality_source": _mapping(state.get("sources")).get("vitality"),
        "capacity_source": _mapping(state.get("sources")).get("vitality_capacity"),
        "model_reference_cn_verified": False,
    }
    if current is None or capacity is None or not 0 <= current <= capacity <= 120 or capacity == 0 or old_q is None:
        return {**note, "status": "unchanged_unknown", "reason": "current_hp_capacity_or_b_hp_component_unknown"}
    branches, basis = _hp_branches(action)
    if branches is None:
        return {**note, "status": "unchanged_unknown", "reason": basis}

    def value(hp, cap):
        return hp_price * ((1 + alpha) * hp - alpha * hp * hp / cap)

    before = value(current, capacity)
    quoted = []
    for branch in branches:
        after, cap = recover_points(current, capacity, branch["hp_points"], branch["hp_cap_points"])
        quoted.append({**branch, "after_hp_points": after, "after_cap_points": cap, "branch_q": value(after, cap) - before})
    new_q = sum(row["weight"] * row["branch_q"] for row in quoted)
    return {**note, "status": "priced", "basis": basis, "branches": quoted, "new_hp_q": new_q, "hp_delta_q": new_q - old_q}


def _identity(row):
    member, slot = row.get("member_id"), row.get("support_slot")
    member = member if type(member) in (str, int) and member != "" else None
    slot = slot if type(slot) is int and 0 <= slot < 6 else None
    return member, slot


def _charge_bounds(action):
    people = action.get("participants")
    if not isinstance(people, list):
        return {"lower": 0, "upper": None, "reason": "participant_count_unknown"}
    count = _count(action.get("participant_count"))
    if count is None and action.get("resource_partial") is False:
        count = len(people)
    members, slots = set(), set()
    lower = nonnew = 0
    for person in people:
        person = _mapping(person)
        member, slot = _identity(person)
        if member in members or slot in slots:
            return {"lower": 0, "upper": None, "reason": "participant_identity_conflict"}
        if member is not None:
            members.add(member)
        if slot is not None:
            slots.add(slot)
        before = _count(person.get("charge_filled", person.get("charge")))
        after = _count(person.get("charge_after_on_success_direct"))
        gain = _count(person.get("charge_gain"))
        from_gain = before + gain if before is not None and gain is not None else None
        if after is None:
            after = from_gain
        if person.get("charge_applicable") is False or (person.get("role") == "mei_support" and before is None):
            nonnew += 1
        elif after is not None and from_gain is not None and after != from_gain:
            continue
        elif before is not None and after is not None and 0 <= before <= after <= 3:
            lower += before < 3 and after == 3
            nonnew += before == 3 or after < 3
        elif before == 3 and after is None:
            nonnew += 1
        elif before is None and after is not None and after < 3 and person.get("charge_applicable") is True:
            nonnew += 1
    if count is None or count < len(people):
        return {"lower": lower, "upper": None, "reason": "participant_count_unknown"}
    unknown = count - lower - nonnew
    return {
        "lower": lower,
        "upper": lower + unknown,
        "participant_count": count,
        "unknown_participants": unknown,
        "reason": "partial_bounded" if unknown else "known",
    }


def _window(state, training):
    ready = _count(state.get("ss_ready_count"))
    ready_source = _mapping(state.get("sources")).get("ss_ready_count")
    note = {
        "current_ready": ready,
        "ready_source": ready_source or ("canonical_ss_ready_count" if ready is not None else None),
        "eligible_training": list(training),
        "enabled": False,
    }
    if ready is None:
        return {**note, "reason": "ready_unknown"}
    if ready >= 5:
        return {**note, "reason": "already_full"}
    slots = 5 - ready
    bounds = {key: _charge_bounds(action) for key, action in training.items()}
    note.update(slots_needed=slots, direct_bounds=bounds)
    if any(row["lower"] >= slots for row in bounds.values()):
        return {**note, "reason": "eligible_training_can_close"}
    if any(row["upper"] is None or row["upper"] >= slots for row in bounds.values()):
        return {**note, "reason": "closure_not_ruled_out"}
    return {**note, "enabled": True, "reason": "all_eligible_direct_upper_below_missing"}


def _cross80(action, *, cross80_price=6.0):
    failure = _failure(action)
    if failure is None:
        return {"option_q": 0.0, "cards": [], "status": "training_failure_probability_unknown"}
    shadow, _ = apply_known_mechanics(action)
    bonds = _mapping(shadow.get("resource_effects")).get("bond")
    people = action.get("participants")
    people = people if isinstance(people, list) else []
    rows = [row for row in bonds or [] if _mapping(row).get("role") == "support_card"]
    slots = defaultdict(set)
    sources = {}
    for row in [*people, *rows]:
        row = _mapping(row)
        if row.get("role") != "support_card":
            continue
        member, slot = _identity(row)
        if member is not None and slot is not None:
            slots[member].add(slot)
        if "sources" in row:
            sources[member, slot] = _mapping(row["sources"])
    groups = defaultdict(list)
    for row in rows:
        member, slot = _identity(row)
        if member is not None and len(slots[member]) > 1:
            continue
        if slot is None and member is not None and slots[member]:
            slot = next(iter(slots[member]))
        if slot is not None or member is not None:
            groups[("slot", slot) if slot is not None else ("member", member)].append(row)

    cards = []
    for key, group in groups.items():
        direct = [row for row in group if row.get("timing") == "training_success"]
        points = {(finite(row.get("before_points")), finite(row.get("gain_points"))) for row in direct}
        members = {_identity(row)[0] for row in group} - {None}
        if len(points) != 1 or len(members) > 1:
            continue
        before, gain = next(iter(points))
        if before is None or gain is None or not 0 <= before < 80 or gain < 0:
            continue
        after = before + gain
        hints = {
            (
                finite(row.get("before_points")),
                finite(row.get("gain_points")),
                _unit(row.get("event_probability_given_training_success")),
            )
            for row in group
            if row.get("timing") == "additional_event"
        }
        hint = None
        if after < 80 and len(hints) == 1:
            hint_before, hint_gain, chance = next(iter(hints))
            if hint_before == after and hint_gain is not None and hint_gain >= 0 and chance is not None:
                hint = chance if after + hint_gain >= 80 else 0.0
        conditional = 1.0 if after >= 80 else hint or 0.0
        probability = (1 - failure) * conditional
        member, slot = _identity(direct[0])
        card_source = sources.get((member, slot), sources.get((member, key[1]), {}))
        cards.append({
            "key": list(key),
            "before": before,
            "after_direct": after,
            "hint_cross_probability_given_success": hint,
            "cross_probability": probability,
            "option_q": cross80_price * probability,
            "bond_source": card_source.get("bond"),
            "direct_gain_sources": sorted({row["basis"] for row in direct if isinstance(row.get("basis"), str)}),
            "hint_gain_sources": sorted({row["basis"] for row in group if row.get("timing") == "additional_event" and isinstance(row.get("basis"), str)}),
        })
    return {
        "option_q": sum(row["option_q"] for row in cards),
        "cards": cards,
        "training_failure_probability": failure,
        "status": "priced_available_card_rows" if cards else "no_usable_crossing_rows",
    }


def _select(scores):
    return min(scores, key=lambda key: (-scores[key], key != "ss", key))


def _reprice_combo198(state, actions, scores, evidence, *, params=DEFAULT_PARAMS):
    result = dict(scores)
    base_selected = _select(result) if result else None
    note = {
        "b_selected": base_selected,
        "hp_selected": base_selected,
        "selected": base_selected,
        "b_values": dict(scores),
        "actions": {},
        "alpha": params.combo198_hp_alpha,
        "window": {"enabled": False, "reason": "outside_first_year_or_empty"},
    }
    if state.get("year") != 1 or not result:
        return result, note
    eligible = {action["action_id"]: action for action in actions if action["action_id"] in scores}
    if eligible.keys() != scores.keys():
        note["window"]["reason"] = "eligible_action_details_incomplete"
        return result, note
    for key, action in eligible.items():
        hp = _hp_quote(state, action, _mapping(evidence.get(key)), params=params)
        result[key] += hp["hp_delta_q"]
        note["actions"][key] = {
            "hp": hp,
            "b_q": scores[key],
            "after_hp_q": result[key],
            "cross80": {"option_q": 0.0, "cards": []},
            "final_q": result[key],
        }
    proposal = _select(result)
    note.update(hp_selected=proposal, selected=proposal, values_after_hp=dict(result))
    if eligible[proposal].get("kind") != "train":
        note["window"]["reason"] = "hp_non_training_proposal_preserved"
        return result, note
    training = {key: action for key, action in eligible.items() if action.get("kind") == "train"}
    note["window"] = _window(state, training)
    if note["window"]["enabled"]:
        for key, action in training.items():
            cross = _cross80(action, cross80_price=params.combo198_cross80_price)
            result[key] += cross["option_q"]
            note["actions"][key].update(cross80=cross, final_q=result[key])
        note["selected"] = _select({key: result[key] for key in training})
    return result, note


def _card_keys(row):
    member, slot = _identity(row)
    return {key for key in (("member", member), ("slot", slot)) if key[1] is not None}


def _confirmed_npc(person):
    return person.get("role") == "scenario_npc" and person.get("support_slot") is None and person.get("card_id") is None


def _solo_mei_status(action, people):
    mei = [p for p in people if p.get("role") == "mei_support" and p.get("identity_status") == "known" and p.get("character_id") == "satake_mei"]
    if len(mei) != 1:
        return "mei_presence_unconfirmed"
    if any(p.get("role") == "support_card" for p in people):
        return "ordinary_support_visible"
    count = action.get("participant_count")
    if (
        action.get("resource_partial") is not False
        or type(count) is not int
        or count != len(people)
        or (action.get("sources") or {}).get("participant_count") != "current_verified_training_lane"
    ):
        return "participant_lane_incomplete"
    if not all(p is mei[0] or _confirmed_npc(p) for p in people):
        return "other_participant_role_unresolved"
    return "confirmed_solo_mei"


def _ordinary_contacts(action, people, cross, preference, *, ordinary_contact_q):
    failure = _failure(action)
    if failure is None or preference is None or preference <= 0:
        return []
    direct = [
        row
        for row in (action.get("resource_effects") or {}).get("bond", [])
        if row.get("role") == "support_card" and row.get("timing") == "training_success"
    ]
    cross_cards = {tuple(row["key"]): row["option_q"] for row in cross.get("cards", [])}
    cards = []
    for row in direct:
        keys = _card_keys(row)
        before, gain = finite(row.get("before_points")), finite(row.get("gain_points"))
        if (
            not keys
            or before is None
            or not 0 <= before < 80
            or gain is None
            or gain <= 0
            or sum(bool(keys & _card_keys(other)) for other in direct) != 1
        ):
            continue
        matched = [p for p in people if keys & _card_keys(p)]
        if len(matched) != 1:
            continue
        person = matched[0]
        source = (person.get("sources") or {}).get("bond")
        if person.get("role") != "support_card" or source not in BOND_SOURCES or finite(person.get("bond_points")) != before:
            continue
        expected_gain = min(gain, 80 - before) * (1 - failure)
        contact_q = ordinary_contact_q * expected_gain / ORDINARY_CONTACT_POINTS * preference
        prior_cross_q = max((cross_cards.get(key, 0.0) for key in keys | _card_keys(person)), default=0.0)
        member, slot = _identity(person)
        cards.append({
            "member_id": member,
            "support_slot": slot,
            "before": before,
            "bond_source": source,
            "gain_source": row.get("basis"),
            "effective_gain_expected": expected_gain,
            "contact_q": contact_q,
            "cross80_q_already_priced": prior_cross_q,
            "delta_q": max(0.0, contact_q - prior_cross_q),
        })
    return cards


def _reprice_contact(state, actions, scores, evidence, *, combo, params=DEFAULT_PARAMS):
    ordinary_contact_q = params.combo198_cross80_price / 3.0
    result = dict(scores)
    audit = {"actions": {}, "ordinary_contact_q_per_7_points": ordinary_contact_q}
    eligible = {action["action_id"]: action for action in actions}
    cross_actions = combo.get("actions", {})
    for action_id, score in scores.items():
        action = eligible.get(action_id, {})
        note = {
            "solo_mei_status": "outside_first_year_training",
            "solo_mei_delta_q": 0.0,
            "manual_mei_bonus_q_retained": 0.0,
            "ordinary_contact_delta_q": 0.0,
            "ordinary_contacts": [],
        }
        audit["actions"][action_id] = note
        if state.get("year") != 1 or action.get("kind") != "train":
            continue
        people = action.get("participants") or []
        detail = evidence.get(action_id) or {}
        components = detail.get("components") or {}
        phase_q = finite(components.get("mei_phase")) or 0.0
        solo = _solo_mei_status(action, people)
        cross = (cross_actions.get(action_id) or {}).get("cross80") or {}
        preference = finite((detail.get("preferences") or {}).get("bond"))
        cards = _ordinary_contacts(action, people, cross, preference, ordinary_contact_q=ordinary_contact_q)
        note.update(
            solo_mei_status=solo,
            solo_mei_delta_q=-phase_q if solo == "confirmed_solo_mei" else 0.0,
            manual_mei_bonus_q_retained=finite(components.get("mei_bonus")) or 0.0,
            ordinary_contacts=cards,
            ordinary_contact_delta_q=sum(card["delta_q"] for card in cards),
        )
        result[action_id] = score + note["solo_mei_delta_q"] + note["ordinary_contact_delta_q"]
    return result, audit


def _ready_count_for_close(state, preview):
    from core.auto_trainer.larc_ss_deadline import _current_full, _run_id

    run_id = _run_id(state, None)
    context = state.scenario_state
    packets = (
        preview,
        context.get("larc_ss_preview"),
        context.get("larc_ss_entry"),
        context.get(NONFULL_ENTRY),
    )
    ready = {packet["ready_count"] for packet in packets if _current_full(packet, run_id, state.turn) is not None}
    return next(iter(ready)) if len(ready) == 1 else None


def _reprice_close(state, actions, scores, *, selected, preferences, ss_preview=None, params=DEFAULT_PARAMS):
    completion_q = params.year1_coefficients[EFFECTS.index("charge")] / 10.0
    ready = _ready_count_for_close(state, ss_preview)
    note = {"ready": ready, "actions": {}, "price_q": completion_q}
    if not selected.startswith("train:") or ready is None or ready >= 5:
        return scores, note

    result = dict(scores)
    charge_preference = _preferences(preferences)["charge"]
    for action in actions:
        if action["kind"] != "train":
            continue
        key = action["action_id"]
        new_full = _charge_bounds(action)["lower"]
        failure = _failure(action)
        q = completion_q * (1 - failure) * charge_preference if failure is not None and ready + new_full >= 5 else 0.0
        result[key] += q
        note["actions"][key] = {"known_new_full": new_full, "completion_q": q}
    return result, note


# ---------------------------------------------------------------------------
# Public Entry Points
# ---------------------------------------------------------------------------

def choose(state, goal, rows, *, ss_preview=None, ss_executions=None):
    """Return selected original ID and scored pool, or None outside Year-1 scope."""
    _FALLBACK.set(None)
    if not enabled(goal):
        return None
    period = (getattr(state, "scenario_state", {}) or {}).get("larc_period") or {}
    if (
        getattr(state, "screen", None) != "training_menu"
        or period.get("status") not in ("accepted", "verified")
        or (period.get("value") or {}).get("year_index") != 1
    ):
        return None
    from core.auto_trainer.larc_ss_choice import _bound_supporter_goal
    from core.auto_trainer.larc_supporter_goal import deadline_larc_supporter_goal
    from core.auto_trainer.rejection_policy import is_hard_rejected

    if deadline_larc_supporter_goal(_bound_supporter_goal(state)):
        return None
    from core.auto_trainer.larc_strategy_extension import active_provider

    params = params_for_goal(goal)
    clean = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        flags = set(row.get("risk_flags") or [])
        soft_recovery_only = (
            row.get("action") in {"rest", "outing"}
            and flags == {"no_effective_recovery_need"}
            and row.get("hard_rejected") is not True
        )
        if is_hard_rejected(row) and not soft_recovery_only:
            continue
        clean.append({**row, "rejected": False})
    ss_status = None
    if ss_preview is not None:
        from core.auto_trainer.larc_ss_deadline import _current_full, _history, _run_id
        from core.auto_trainer.larc_ss_visible import ss_victory_evidence

        run_id = _run_id(state, ss_preview)
        if _current_full(ss_preview, run_id, state.turn) is True:
            observation = ss_preview.get("participants_observation") or {}
            rates = observation.get("win_rates") or []
            known = (
                observation.get("win_rates_status") == "accepted"
                and len(rates) == 5
                and [p.get("slot") for p in rates] == list(range(5))
                and all(p.get("band") in {"blue", "green"} for p in rates)
            )
            valid = (
                ss_preview.get("status") == "accepted"
                and ss_preview.get("stage") == "preview"
                and ss_preview.get("kind") in {"SS", "SSS"}
                and ss_preview.get("ready_count") == 5
                and (ss_victory_evidence(ss_preview).get("guaranteed") or known)
            )
            if not valid:
                _FALLBACK.set("full_ss_risk_evidence_unavailable")
                return None
            first, deferred = _history(state.scenario_state.get("larc_ss_deadline_history"), run_id, state.turn, True)
            ss_status = {"first_ready_turn": first, "confirmed_deferrals": deferred}
            if deferred is None or deferred >= 1:
                return {"action_id": "ss", "reason": "full_group_deadline", "values": {}, "ss": ss_status}
            clean.append({"action_id": "ss", "rejected": False})
        elif (
            active_provider().early_4p_ss_allowed(params)
            and ss_preview.get("status") == "accepted"
            and ss_preview.get("stage") == "preview"
            and ss_preview.get("kind") in {"SS", "SSS"}
            and ss_preview.get("ready_count") == 4
            and ss_victory_evidence(ss_preview).get("guaranteed") is True
        ):
            visible = (ss_preview.get("participants_observation") or {}).get("participants") or []
            has_unearned_charming = any(
                isinstance(p, dict)
                and isinstance(p.get("current_reward"), dict)
                and p["current_reward"].get("status") == "accepted"
                and p["current_reward"].get("kind") == "charming"
                and p["current_reward"].get("earned") is not True
                for p in visible
            )
            if has_unearned_charming:
                clean.append({"action_id": "ss", "rejected": False})
    try:
        menu = project(state, goal, clean, ss_preview=ss_preview)
    except (ValueError, TypeError, KeyError, ArithmeticError) as error:
        _FALLBACK.set("projection_" + type(error).__name__)
        return None
    if not menu.get("actions"):
        _FALLBACK.set("no_scorable_projection_actions")
        return None
    actions = [a for a in menu["actions"] if a.get("scorable") is True]
    if any(a.get("kind") == "mei_outing" for a in actions):
        actions = [a for a in actions if a.get("kind") not in ("rest", "outing")]
    if not actions:
        _FALLBACK.set("no_scorable_eligible_actions")
        return None
    policy = getattr(goal, "larc_policy", None) or {}
    preferences = policy.get("decision_preferences") if isinstance(policy, dict) else None
    scores = {}
    evidence = {}
    for action in actions:
        try:
            result = score_action(menu["state"], action, preferences=preferences, params=params)
        except (ValueError, TypeError, KeyError, ArithmeticError) as error:
            _FALLBACK.set("pricing_" + type(error).__name__)
            return None
        value = result.get("score")
        if type(value) not in (int, float) or not isfinite(value):
            _FALLBACK.set("pricing_nonfinite")
            return None
        scores[action["action_id"]] = value
        evidence[action["action_id"]] = {
            **result,
            "projection_sources": action.get("sources"),
            "projection_unknown": action.get("unknown"),
        }

    base_scores = scores
    scores, combo = _reprice_combo198(menu["state"], actions, base_scores, evidence, params=params)
    selected = combo["selected"]
    contact = close = None
    if selected.startswith("train:"):
        scores, contact = _reprice_contact(menu["state"], actions, scores, evidence, combo=combo, params=params)
        scores, close = _reprice_close(
            state, actions, scores, selected=selected, preferences=preferences, ss_preview=ss_preview, params=params
        )
        combo["local_trial"] = {
            "selected_before": selected,
            "selection_scope": "training_to_training",
            "contact": contact,
            "close": close,
        }
        selected = min((key for key in scores if key.startswith("train:")), key=lambda key: (-scores[key], key))
        combo["selected"] = selected
    for action_id, value in scores.items():
        detail = evidence[action_id]
        adjustment = combo["actions"][action_id]
        adjustment["final_q"] = value
        detail["b197_score"] = base_scores[action_id]
        detail["score"] = value
        detail["components"] = {
            **detail["components"],
            "combo198_hp_delta": adjustment["hp"]["hp_delta_q"],
            "combo198_cross80": adjustment["cross80"]["option_q"],
        }
        if "local_trial" in combo:
            contact_note = contact["actions"].get(action_id, {})
            close_note = close["actions"].get(action_id, {})
            detail["components"].update(
                local_solo_mei=contact_note.get("solo_mei_delta_q", 0.0),
                local_card_contact=contact_note.get("ordinary_contact_delta_q", 0.0),
                local_close_five=close_note.get("completion_q", 0.0),
            )
    from core.auto_trainer.larc_strategy_extension import active_provider

    scores, selected, tuning_applied = active_provider().year1_apply_tuning_bridge(
        params,
        state,
        goal,
        scores,
        evidence,
        combo,
        clean,
        ss_preview,
        _select,
    )
    effective_revision = policy.get("revision") if isinstance(policy, dict) and policy.get("revision") else params.revision
    return {
        "action_id": selected,
        "reason": "year1_b197_unified",
        "values": scores,
        "evidence": evidence,
        "ss": ss_status,
        "state_sources": menu["state"].get("sources"),
        "projection_unknown": menu.get("unknown"),
        "legacy_score_tuning_workflow": "bounded_applied_to_b197_q" if tuning_applied else "not_applied_to_b197_q; explicit risk and user preferences retained",
        "score_basis": "year1_b198_combo",
        "effective_revision": effective_revision,
        "combo198": combo,
    }


def decorate_rows(rows, decision):
    """Keep parent score separately and expose the actual B comparable score."""
    scores = decision.get("values") or {}
    selected = decision.get("action_id")
    result = []
    for source in rows:
        row = deepcopy(source)
        aid = row.get("action_id")
        if aid in scores:
            row["parent_score"] = row.get("score")
            row["score"] = scores[aid]
            row["score_basis"] = decision.get("score_basis", "year1_b197_unified")
            row["rejected"] = False
            row["hard_rejected"] = False
            detail = (decision.get("evidence") or {}).get(aid) or {}
            row["b197_components"] = detail.get("components")
            row["b197_sources"] = {
                "state": decision.get("state_sources"),
                "action": detail.get("projection_sources"),
                "pricing": detail.get("sources"),
                "unknown": detail.get("unknown"),
            }
            if aid == selected:
                planning = {
                    "selected_action_id": selected,
                    "reason": decision.get("reason"),
                    "score_basis": row["score_basis"],
                    "state_sources": decision.get("state_sources"),
                    "ss": decision.get("ss"),
                }
                if "combo198" in decision:
                    planning.update(effective_revision=decision["effective_revision"], combo198=decision["combo198"])
                row.setdefault("planning", {})["year1_b197"] = planning
        result.append(row)
    return result
