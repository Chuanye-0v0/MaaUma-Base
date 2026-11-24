"""Tiered priority-ladder skill compiler for race & terminal LoH/CM builds.

Compiled output uses the standard `uma.skill_preference_profile.v1` +
`-arc-target-cm` suffix so D1's existing family-aware knapsack optimizer,
batch validator, and multi-batch purchase workers execute it unchanged.
"""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import math
from pathlib import Path
import re
import sys


_WITHIN_ORDERS = frozenset({"discount_cost_first", "strict_order"})
_UNLISTED_POLICIES = frozenset({"skip", "safe_cm_fallback"})
_CLASSIC_MODES = frozenset({"stat_rescue", "skip", "allowlist"})
_SENIOR_MODES = frozenset({"t0_only", "skip", "full"})
_TERMINAL_MODES = frozenset({"full_ladder"})
_EVO_BRANCHES = frozenset({"original", "scenario", "skip"})

_LADDER_KEYS = frozenset({
    "tiers", "avoid", "unlisted_policy", "within_tier_order", "allow_lower_fallback",
    "groundwork_gate", "stamina_gap_boost", "green_skill_quota", "accel_group_caps",
    "combo_promotions",
})
_TIER_KEYS = frozenset({"id", "label", "skills", "within_tier_order", "max_count"})
_GROUNDWORK_KEYS = frozenset({"enabled", "min_sources", "reliable_active_pair"})
_STAMINA_BOOST_KEYS = frozenset({"enabled", "min_stamina", "use_hp_gate", "promote_skills"})
_GREEN_QUOTA_KEYS = frozenset({"enabled", "min_count", "skills", "last_resort_skills"})
_ACCEL_CAP_KEYS = frozenset({"group_id", "skills", "max_count", "overflow_tier_shift"})
_COMBO_PROMO_KEYS = frozenset({"combo_id", "require_all", "promote_skills", "target_tier", "exclusive_recovery_trigger"})
_STAGE_KEYS = frozenset({"classic_arc", "senior_arc", "terminal"})
_CLASSIC_KEYS = frozenset({
    "mode", "speed_threshold", "stamina_threshold", "max_skills",
    "allowed_tiers", "min_discount_percent", "reserve_pt", "prefer_white", "allow_recovery",
})
_SENIOR_KEYS = frozenset({"mode", "allowed_tiers", "max_skills", "reserve_pt", "allow_recovery"})
_TERMINAL_KEYS = frozenset({"mode"})
_EVO_KEYS = frozenset({"enabled", "default_branch", "overrides"})


def _cm():
    cached = sys.modules.get("_pacer_cm_skill_compiler")
    if cached is not None:
        return cached
    path = Path(__file__).with_name("cm_skill_compiler.py")
    spec = importlib.util.spec_from_file_location("_pacer_cm_skill_compiler", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _dedupe_str_list(raw, err_prefix: str, *, max_items: int = 200) -> list[str]:
    if not isinstance(raw, list) or len(raw) > max_items:
        raise ValueError(f"{err_prefix}_invalid")
    seen = set()
    out = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{err_prefix}_entry_invalid")
        clean = item.strip()
        if clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def _require_bool(val, err: str) -> bool:
    if type(val) is not bool:
        raise ValueError(err)
    return val


def _require_int_range(val, low: int, high: int, err: str) -> int:
    if type(val) is not int or not low <= val <= high:
        raise ValueError(err)
    return val


def normalize_ladder_policy(value=None) -> dict:
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - _LADDER_KEYS:
        raise ValueError("arc_policy_ladder_invalid")

    within_order = value.get("within_tier_order", "discount_cost_first")
    if within_order not in _WITHIN_ORDERS:
        raise ValueError("arc_policy_ladder_within_order_invalid")

    unlisted = value.get("unlisted_policy", "skip")
    if unlisted not in _UNLISTED_POLICIES:
        raise ValueError("arc_policy_ladder_unlisted_policy_invalid")

    allow_lower = _require_bool(
        value.get("allow_lower_fallback", True),
        "arc_policy_ladder_allow_lower_fallback_invalid",
    )

    raw_tiers = value.get("tiers", [])
    if not isinstance(raw_tiers, list) or len(raw_tiers) > 8:
        raise ValueError("arc_policy_ladder_tiers_invalid")
    tiers = []
    tier_ids = set()
    for row in raw_tiers:
        if not isinstance(row, dict) or set(row) - _TIER_KEYS:
            raise ValueError("arc_policy_ladder_tier_invalid")
        tid = row.get("id")
        if not isinstance(tid, str) or not tid.strip() or tid.strip() in tier_ids:
            raise ValueError("arc_policy_ladder_tier_id_invalid")
        tid = tid.strip()
        tier_ids.add(tid)
        label = row.get("label", tid)
        if not isinstance(label, str) or not label.strip():
            raise ValueError("arc_policy_ladder_tier_label_invalid")
        t_order = row.get("within_tier_order", within_order)
        if t_order is None:
            t_order = within_order
        if t_order not in _WITHIN_ORDERS:
            raise ValueError("arc_policy_ladder_tier_order_invalid")
        max_count = row.get("max_count")
        if max_count is not None:
            max_count = _require_int_range(max_count, 1, 48, "arc_policy_ladder_tier_max_count_invalid")
        skills = _dedupe_str_list(row.get("skills", []), "arc_policy_ladder_tier_skills")
        tiers.append({
            "id": tid,
            "label": label.strip(),
            "skills": skills,
            "within_tier_order": t_order,
            "max_count": max_count,
        })

    avoid = _dedupe_str_list(value.get("avoid", []), "arc_policy_ladder_avoid")

    gw_raw = value.get("groundwork_gate") or {}
    if not isinstance(gw_raw, dict) or set(gw_raw) - _GROUNDWORK_KEYS:
        raise ValueError("arc_policy_groundwork_gate_invalid")
    groundwork_gate = {
        "enabled": _require_bool(gw_raw.get("enabled", True), "arc_policy_groundwork_enabled_invalid"),
        "min_sources": _require_int_range(
            gw_raw.get("min_sources", 3), 1, 6, "arc_policy_groundwork_min_sources_invalid"
        ),
        "reliable_active_pair": _require_bool(
            gw_raw.get("reliable_active_pair", False), "arc_policy_groundwork_reliable_active_pair_invalid"
        ),
    }

    sb_raw = value.get("stamina_gap_boost") or {}
    if not isinstance(sb_raw, dict) or set(sb_raw) - _STAMINA_BOOST_KEYS:
        raise ValueError("arc_policy_stamina_boost_invalid")
    min_sta = sb_raw.get("min_stamina")
    if min_sta is not None:
        min_sta = _require_int_range(min_sta, 0, 3000, "arc_policy_stamina_boost_min_stamina_invalid")
    stamina_gap_boost = {
        "enabled": _require_bool(sb_raw.get("enabled", False), "arc_policy_stamina_boost_enabled_invalid"),
        "min_stamina": min_sta,
        "use_hp_gate": _require_bool(sb_raw.get("use_hp_gate", True), "arc_policy_stamina_boost_hp_gate_invalid"),
        "promote_skills": _dedupe_str_list(
            sb_raw.get("promote_skills", []), "arc_policy_stamina_boost_promote_skills"
        ),
    }

    gq_raw = value.get("green_skill_quota") or {}
    if not isinstance(gq_raw, dict) or set(gq_raw) - _GREEN_QUOTA_KEYS:
        raise ValueError("arc_policy_green_quota_invalid")
    green_skill_quota = {
        "enabled": _require_bool(gq_raw.get("enabled", False), "arc_policy_green_quota_enabled_invalid"),
        "min_count": _require_int_range(
            gq_raw.get("min_count", 3), 1, 12, "arc_policy_green_quota_min_count_invalid"
        ),
        "skills": _dedupe_str_list(gq_raw.get("skills", []), "arc_policy_green_quota_skills"),
        "last_resort_skills": _dedupe_str_list(
            gq_raw.get("last_resort_skills", []), "arc_policy_green_quota_last_resort"
        ),
    }

    raw_caps = value.get("accel_group_caps", [])
    if not isinstance(raw_caps, list) or len(raw_caps) > 16:
        raise ValueError("arc_policy_accel_caps_invalid")
    caps = []
    cap_ids = set()
    for row in raw_caps:
        if not isinstance(row, dict) or set(row) - _ACCEL_CAP_KEYS:
            raise ValueError("arc_policy_accel_cap_invalid")
        gid = row.get("group_id")
        if not isinstance(gid, str) or not gid.strip() or gid.strip() in cap_ids:
            raise ValueError("arc_policy_accel_cap_group_id_invalid")
        gid = gid.strip()
        cap_ids.add(gid)
        caps.append({
            "group_id": gid,
            "skills": _dedupe_str_list(row.get("skills", []), "arc_policy_accel_cap_skills"),
            "max_count": _require_int_range(row.get("max_count", 1), 1, 12, "arc_policy_accel_cap_max_count_invalid"),
            "overflow_tier_shift": _require_int_range(
                row.get("overflow_tier_shift", 1), 1, 8, "arc_policy_accel_cap_shift_invalid"
            ),
        })

    raw_combos = value.get("combo_promotions", [])
    if not isinstance(raw_combos, list) or len(raw_combos) > 16:
        raise ValueError("arc_policy_combo_promotions_invalid")
    combos = []
    combo_ids = set()
    for row in raw_combos:
        if not isinstance(row, dict) or set(row) - _COMBO_PROMO_KEYS:
            raise ValueError("arc_policy_combo_promotion_invalid")
        cid = row.get("combo_id")
        if not isinstance(cid, str) or not cid.strip() or cid.strip() in combo_ids:
            raise ValueError("arc_policy_combo_id_invalid")
        cid = cid.strip()
        combo_ids.add(cid)
        req_all = _dedupe_str_list(row.get("require_all", []), "arc_policy_combo_require_all")
        promo_skills = _dedupe_str_list(row.get("promote_skills", []), "arc_policy_combo_promote_skills")
        if not req_all or not promo_skills:
            raise ValueError("arc_policy_combo_empty_invalid")
        t_tier = row.get("target_tier", tiers[0]["id"] if tiers else "T0")
        if not isinstance(t_tier, str) or not t_tier.strip() or (tier_ids and t_tier.strip() not in tier_ids):
            raise ValueError("arc_policy_combo_target_tier_invalid")
        combos.append({
            "combo_id": cid,
            "require_all": req_all,
            "promote_skills": promo_skills,
            "target_tier": t_tier.strip(),
            "exclusive_recovery_trigger": _require_bool(
                row.get("exclusive_recovery_trigger", False),
                "arc_policy_combo_exclusive_recovery_invalid",
            ),
        })

    return {
        "tiers": tiers,
        "avoid": avoid,
        "unlisted_policy": unlisted,
        "within_tier_order": within_order,
        "allow_lower_fallback": allow_lower,
        "groundwork_gate": groundwork_gate,
        "stamina_gap_boost": stamina_gap_boost,
        "green_skill_quota": green_skill_quota,
        "accel_group_caps": caps,
        "combo_promotions": combos,
    }


def normalize_stage_policy(value=None, *, mode: str = "auto_cm") -> dict:
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - _STAGE_KEYS:
        raise ValueError("arc_policy_stage_policy_invalid")

    default_classic = "stat_rescue" if mode == "tiered_ladder" else "allowlist"
    default_senior = "t0_only" if mode == "tiered_ladder" else "full"

    c_raw = value.get("classic_arc") or {}
    if not isinstance(c_raw, dict) or set(c_raw) - _CLASSIC_KEYS:
        raise ValueError("arc_policy_classic_stage_invalid")
    c_mode = c_raw.get("mode", default_classic)
    if c_mode not in _CLASSIC_MODES:
        raise ValueError("arc_policy_classic_stage_mode_invalid")
    classic_arc = {
        "mode": c_mode,
        "speed_threshold": _require_int_range(
            c_raw.get("speed_threshold", 900), 0, 3000, "arc_policy_classic_speed_threshold_invalid"
        ),
        "stamina_threshold": _require_int_range(
            c_raw.get("stamina_threshold", 600), 0, 3000, "arc_policy_classic_stamina_threshold_invalid"
        ),
        "max_skills": _require_int_range(
            c_raw.get("max_skills", 2), 1, 5, "arc_policy_classic_max_skills_invalid"
        ),
        "allowed_tiers": _dedupe_str_list(
            c_raw.get("allowed_tiers", ["T0", "T1"]), "arc_policy_classic_allowed_tiers", max_items=8
        ),
        "min_discount_percent": _require_int_range(
            c_raw.get("min_discount_percent", 20), 0, 60, "arc_policy_classic_min_discount_invalid"
        ),
        "reserve_pt": _require_int_range(
            c_raw.get("reserve_pt", 360), 0, 2000, "arc_policy_classic_reserve_pt_invalid"
        ),
        "prefer_white": _require_bool(
            c_raw.get("prefer_white", True), "arc_policy_classic_prefer_white_invalid"
        ),
        "allow_recovery": _require_bool(
            c_raw.get("allow_recovery", True), "arc_policy_classic_allow_recovery_invalid"
        ),
    }

    s_raw = value.get("senior_arc") or {}
    if not isinstance(s_raw, dict) or set(s_raw) - _SENIOR_KEYS:
        raise ValueError("arc_policy_senior_stage_invalid")
    s_mode = s_raw.get("mode", default_senior)
    if s_mode not in _SENIOR_MODES:
        raise ValueError("arc_policy_senior_stage_mode_invalid")
    senior_arc = {
        "mode": s_mode,
        "allowed_tiers": _dedupe_str_list(
            s_raw.get("allowed_tiers", ["T0"]), "arc_policy_senior_allowed_tiers", max_items=8
        ),
        "max_skills": _require_int_range(
            s_raw.get("max_skills", 5), 1, 10, "arc_policy_senior_max_skills_invalid"
        ),
        "reserve_pt": _require_int_range(
            s_raw.get("reserve_pt", 500), 0, 2000, "arc_policy_senior_reserve_pt_invalid"
        ),
        "allow_recovery": _require_bool(
            s_raw.get("allow_recovery", True), "arc_policy_senior_allow_recovery_invalid"
        ),
    }

    t_raw = value.get("terminal") or {}
    if not isinstance(t_raw, dict) or set(t_raw) - _TERMINAL_KEYS:
        raise ValueError("arc_policy_terminal_stage_invalid")
    t_mode = t_raw.get("mode", "full_ladder")
    if t_mode not in _TERMINAL_MODES:
        raise ValueError("arc_policy_terminal_stage_mode_invalid")

    return {
        "classic_arc": classic_arc,
        "senior_arc": senior_arc,
        "terminal": {"mode": t_mode},
    }


def normalize_evolution_policy(value=None) -> dict:
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - _EVO_KEYS:
        raise ValueError("arc_policy_evolution_invalid")
    enabled = _require_bool(value.get("enabled", False), "arc_policy_evolution_enabled_invalid")
    branch = value.get("default_branch", "original")
    if branch not in _EVO_BRANCHES:
        raise ValueError("arc_policy_evolution_default_branch_invalid")
    raw_over = value.get("overrides") or {}
    if not isinstance(raw_over, dict) or len(raw_over) > 64:
        raise ValueError("arc_policy_evolution_overrides_invalid")
    overrides = {}
    for key, val in raw_over.items():
        if not isinstance(key, str) or not key.strip() or val not in _EVO_BRANCHES:
            raise ValueError("arc_policy_evolution_override_entry_invalid")
        overrides[key.strip()] = val
    return {"enabled": enabled, "default_branch": branch, "overrides": overrides}


def _resolve_ordered(entries, rows, masters, knowledge, cm):
    resolved_ids = []
    seen = set()
    resolution = []
    for entry in entries:
        try:
            identity = knowledge.get(entry)["skill_id"]
        except ValueError:
            identity = None
        if entry in rows and cm._is_learnable(rows[entry], masters[entry]):
            matches = [entry]
        else:
            matches = [
                sid
                for sid, row in rows.items()
                if cm._is_learnable(row, masters[sid])
                and (row.get("name_zh_cn") == entry or (masters[sid] or {}).get("skill_id") in {entry, identity})
            ]
        status = "resolved" if len(matches) == 1 else "ambiguous_or_missing"
        resolution.append({"entry": entry, "matches": matches, "status": status})
        if len(matches) == 1 and matches[0] not in seen:
            seen.add(matches[0])
            resolved_ids.append(matches[0])
    return resolved_ids, resolution


def _family_id(sid: str, rows: dict, masters: dict) -> str:
    master = masters.get(sid) or {}
    chain = master.get("cost", {}).get("chain", [])
    if chain:
        return str(chain[0])
    row = rows.get(sid) or {}
    lower = row.get("relations", {}).get("lower", [])
    return str(lower[0]) if lower else sid


def _effective_discount_percent(item: dict, base: int | None) -> float | None:
    obs = item.get("observed_discount_percent")
    if type(obs) in {int, float} and math.isfinite(obs):
        return float(obs)
    cost = item.get("display_cost")
    if type(cost) is int and cost > 0 and type(base) is int and base > 0:
        return max(0.0, 100.0 * (base - cost) / base)
    return None


def compile_tiered_skill_profile(
    profile: dict,
    inventory: dict,
    catalog: dict,
    *,
    policy: dict,
    current_stats=None,
    year: int = 3,
    terminal=None,
    current_race_profile=None,
    planner_policy=None,
    knowledge=None,
    cm_module=None,
) -> tuple[dict, dict]:
    """Compile a tiered ladder + stage policy + lightweight gap checks into a CM profile."""
    cm = cm_module or _cm()
    if knowledge is None:
        from core.skill_knowledge import SkillKnowledgeLibrary
        knowledge = SkillKnowledgeLibrary.load()
    if hasattr(knowledge, "arc_race_inputs"):
        race_inputs, evaluate = knowledge.arc_race_inputs, knowledge.arc_evaluate_condition
    else:
        from core.skill_knowledge import race_inputs
        from core.skill_conditions import evaluate_condition as evaluate

    if terminal is None:
        terminal = year >= 3
    classic = (year == 2 and not terminal)
    senior_pre = (year == 3 and not terminal)

    ladder = normalize_ladder_policy(policy.get("ladder"))
    stage_policy = normalize_stage_policy(policy.get("stage_policy"), mode="tiered_ladder")
    target, geometry = cm.resolve_cm_target(policy.get("final_target") or {}, policy.get("preset_id"))
    inputs = cm._enrich_race_inputs(race_inputs(target), target, current_stats=current_stats)
    course = cm._course(target, geometry)

    result = deepcopy(profile)
    planner_policy = planner_policy or {}
    result["base_profile"] = "race"
    result["max_batch_size"] = min(result["max_batch_size"], 24)
    batch_cap = min(result["max_batch_size"], planner_policy.get("max_purchase_count") or 48)

    if terminal:
        result["reserve_skill_pt"] = 0
        result["max_batch_size"] = min(max(result["max_batch_size"], 20), 24)
        batch_cap = min(result["max_batch_size"], planner_policy.get("max_purchase_count") or 48)
        result["filler_policy"] = "spend_all_safe" if ladder["unlisted_policy"] == "safe_cm_fallback" else "safe_useful_only"
    elif senior_pre:
        s_cfg = stage_policy["senior_arc"]
        if s_cfg["mode"] == "t0_only":
            result["reserve_skill_pt"] = max(result["reserve_skill_pt"], s_cfg["reserve_pt"])
            result["max_batch_size"] = min(result["max_batch_size"], s_cfg["max_skills"])
            batch_cap = min(batch_cap, s_cfg["max_skills"])
        result["filler_policy"] = "safe_useful_only"
    else:
        c_cfg = stage_policy["classic_arc"]
        if c_cfg["mode"] == "stat_rescue":
            result["reserve_skill_pt"] = max(result["reserve_skill_pt"], c_cfg["reserve_pt"])
            result["max_batch_size"] = min(result["max_batch_size"], c_cfg["max_skills"])
            batch_cap = min(batch_cap, c_cfg["max_skills"])
        result["filler_policy"] = "safe_useful_only"

    reserve_floor = max(result["reserve_skill_pt"], planner_policy.get("reserve_skill_pt", 0))
    points = inventory.get("current_skill_points", 0)
    if type(points) is not int or points < 0:
        points = 0
    budget = max(0, points - reserve_floor)
    if type(planner_policy.get("trigger_budget")) is int:
        budget = min(budget, planner_policy["trigger_budget"])

    rows = {row["skill_id"]: row for row in catalog.get("skills", [])}
    masters = {sid: cm._resolve_master(knowledge, row) for sid, row in rows.items()}

    owned = {}
    for sid in inventory.get("owned_skill_ids", []):
        try:
            row = masters.get(sid) or knowledge.get(sid)
            owned[row["skill_id"]] = row
        except ValueError:
            continue
    # Buying ○ replaces the row in-place with unacquired ◎; treat visible ◎ candidates as implicit ownership of ○.
    for cand in inventory.get("candidates", []):
        cand_id = cand.get("skill_id")
        cand_row = rows.get(cand_id, {})
        cand_name = cand_row.get("name_zh_cn") or str(cand_id or "")
        if not str(cand_name).endswith("◎"):
            continue
        for lower_id in cand_row.get("relations", {}).get("lower", []):
            try:
                lower_row = masters.get(lower_id) or knowledge.get(lower_id)
            except ValueError:
                lower_row = None
            if lower_row and str(lower_row.get("name_zh_cn") or "").endswith("○"):
                owned.setdefault(lower_row["skill_id"], lower_row)

    gate = cm.stamina_recovery_gate(target, current_stats)
    owned_ratings = {
        mid: cm._rating(master, target, course, gate, inputs, evaluate)[0]
        for mid, master in owned.items()
    }
    superseded_masters = {
        lower for master in owned.values() for lower in master.get("relations", {}).get("supersedes", [])
    }
    owned_heal = math.fsum(
        r.recovery_rate for mid, r in owned_ratings.items() if mid not in superseded_masters
    )
    if gate["initial_hp"] is not None and owned_heal:
        gate = cm.stamina_recovery_gate(target, current_stats, owned_recovery_hp=owned_heal * gate["initial_hp"])

    current_target, current_geometry = (
        cm.resolve_cm_target(current_race_profile) if classic and current_race_profile else (None, None)
    )
    if current_target:
        current_inputs = cm._enrich_race_inputs(race_inputs(current_target), current_target, current_stats=current_stats)
        current_course = cm._course(current_target, current_geometry)
        current_gate = cm.stamina_recovery_gate(current_target, current_stats)

    avoid_ids, avoid_res = _resolve_ordered(ladder["avoid"], rows, masters, knowledge, cm)
    avoid_set = set(avoid_ids)

    tier_defs = ladder["tiers"]
    tier_id_to_idx = {row["id"]: idx for idx, row in enumerate(tier_defs)}
    explicit_assignment: dict[str, tuple[int, int, str]] = {}
    ladder_resolution = {"tiers": [], "avoid": avoid_res}
    for idx, tier in enumerate(tier_defs):
        t_ids, t_res = _resolve_ordered(tier["skills"], rows, masters, knowledge, cm)
        ladder_resolution["tiers"].append({"id": tier["id"], "resolution": t_res})
        for rank, sid in enumerate(t_ids):
            if sid in avoid_set or sid in explicit_assignment:
                continue
            explicit_assignment[sid] = (idx, rank, tier["id"])

    items = {item["skill_id"]: item for item in inventory.get("candidates", [])}
    action_ids = set(inventory.get("allowed_action_ids", items))
    decisions = {}
    learnable_clickable = {}
    cand_ratings = {}
    cand_exclusions = {}
    cand_quotes = {}

    for sid, item in sorted(items.items()):
        detail = decisions[sid] = {"skill_id": sid, "reason": None}
        row, master = rows.get(sid, {}), masters.get(sid)
        if not cm._is_learnable(row, master):
            detail["reason"] = "not_pt_learnable"
            continue
        if sid not in action_ids:
            detail["reason"] = "unselectable_capture_row"
            continue
        if sid in avoid_set:
            detail["reason"] = "ladder_avoid"
            continue
        if (type(item.get("display_cost")) is not int or item["display_cost"] <= 0
                or item.get("cost_basis") not in {"base", "observed_final"}):
            detail["reason"] = "unverified_skill_price"
            continue
        quote = knowledge.quote_cost(master["skill_id"], owned=tuple(owned))
        base_pt = quote.get("cost_pt")
        if base_pt == 0:
            detail["reason"] = quote["cost_basis"]
            continue
        cost = item["display_cost"]
        discount = 100 * (base_pt - cost) / base_pt if type(base_pt) is int and base_pt > 0 else None
        detail.update(
            remaining_chain_base_pt=base_pt,
            cost_quote=quote,
            effective_discount_percent=discount,
            observed_step_discount_percent=item.get("observed_discount_percent"),
        )
        if item.get("cost_basis") != "observed_final" and len(quote.get("steps", [])) > 1:
            detail["reason"] = "unverified_chain_price"
            continue

        rating, exclusion = cm._rating(master, target, course, gate, inputs, evaluate)
        detail["target_status"] = (
            "conditional" if exclusion is None
            else "unknown" if exclusion == "unknown_target_requirement" else "incompatible"
        )
        learnable_clickable[sid] = item
        cand_ratings[sid] = rating
        cand_exclusions[sid] = exclusion
        cand_quotes[sid] = quote

    # Assign base tiers on learnable clickable candidates
    eff_tier: dict[str, int] = {}
    eff_rank: dict[str, int] = {}
    fallback_tier_idx = len(tier_defs)
    promoted_reasons: dict[str, str] = {}
    dynamic_promotions: list[dict] = []

    for sid in learnable_clickable:
        if sid in explicit_assignment:
            t_idx, rank, tid = explicit_assignment[sid]
            eff_tier[sid] = t_idx
            eff_rank[sid] = rank
            decisions[sid]["tier_id"] = tid

    # Lower fallback if upper tier skill is unavailable or unaffordable
    if ladder["allow_lower_fallback"] and tier_defs:
        for sid, (t_idx, _rank, _tid) in explicit_assignment.items():
            need_fallback = sid not in learnable_clickable or learnable_clickable[sid]["display_cost"] > budget
            if not need_fallback:
                continue
            for low_sid in rows.get(sid, {}).get("relations", {}).get("lower", []):
                if (low_sid in learnable_clickable and low_sid not in avoid_set
                        and low_sid not in eff_tier and low_sid not in owned):
                    fb_idx = min(t_idx + 1, len(tier_defs) - 1)
                    eff_tier[low_sid] = fb_idx
                    eff_rank[low_sid] = 50
                    decisions[low_sid]["lower_fallback_of"] = sid
                    decisions[low_sid]["tier_id"] = tier_defs[fb_idx]["id"]

    # Unlisted fallback (safe_cm_fallback)
    if ladder["unlisted_policy"] == "safe_cm_fallback":
        for sid in learnable_clickable:
            if sid in eff_tier:
                continue
            rating, exclusion = cand_ratings[sid], cand_exclusions[sid]
            if not exclusion and rating.tier < 3 and not rating.start_requirement:
                eff_tier[sid] = fallback_tier_idx
                eff_rank[sid] = 999
                decisions[sid]["tier_id"] = "__fallback__"

    def _is_recovery_skill(sid: str) -> bool:
        return (
            rows.get(sid, {}).get("type") == "recovery"
            or (masters.get(sid) or {}).get("type") == "recovery"
        )

    def _is_speed_green(sid: str) -> bool:
        row = rows.get(sid, {})
        master = masters.get(sid) or {}
        kb = row.get("knowledge") if isinstance(row.get("knowledge"), dict) else {}
        type_raw = str(kb.get("type_raw") or row.get("type_raw") or master.get("type_raw") or "")
        if "被动（速度）" in type_raw:
            return True
        effs = master.get("effects") or kb.get("effects") or row.get("effects") or []
        return any(
            isinstance(ef, dict)
            and ef.get("kind") in {"passive_speed", "speed", "target_speed"}
            and float(ef.get("value") or 0) > 0
            for ef in effs
        )

    def _is_inherited_unique(sid: str) -> bool:
        return (
            re.fullmatch(r"cn-db:9\d{5}", str(sid or "")) is not None
            or rows.get(sid, {}).get("rarity") in {"inherited", "unique"}
            or (masters.get(sid) or {}).get("rarity") in {"inherited", "unique"}
        )

    def _has_positive_recovery(sid: str) -> bool:
        rating = cand_ratings.get(sid)
        if rating is not None and float(getattr(rating, "recovery_rate", 0.0) or 0.0) > 0.0:
            return True
        effs = (masters.get(sid) or {}).get("effects") or rows.get(sid, {}).get("effects") or []
        rec_effs = [ef for ef in effs if isinstance(ef, dict) and ef.get("kind") == "stamina_recovery"]
        if any(float(ef.get("value") or 0.0) > 0.0 for ef in rec_effs):
            return True
        if rec_effs:
            return False
        return _is_recovery_skill(sid)

    def _is_owned_sid(r_sid: str) -> bool:
        r_master_id = (masters.get(r_sid) or {}).get("skill_id")
        if r_sid in owned or (r_master_id is not None and r_master_id in owned):
            return True
        if _is_inherited_unique(r_sid):
            target_name = rows.get(r_sid, {}).get("name_zh_cn") or (masters.get(r_sid) or {}).get("name_zh_cn")
            if target_name and any(m.get("name_zh_cn") == target_name for m in owned.values()):
                return True
        return False

    # Lightweight online gap checks (for terminal or senior pre-race when enabled)
    stage_skip = (
        (classic and stage_policy["classic_arc"]["mode"] == "skip")
        or (senior_pre and stage_policy["senior_arc"]["mode"] == "skip")
    )
    stage_allow_recovery = (
        True if terminal
        else stage_policy["classic_arc"]["allow_recovery"] if classic
        else stage_policy["senior_arc"]["allow_recovery"]
    )
    active_gap_stage = (terminal or (senior_pre and stage_policy["senior_arc"]["mode"] != "skip")) and not stage_skip

    exclusive_recovery_active = False
    exclusive_allowed_recovery_ids: set[str] = set()
    exclusive_combo_ids: list[str] = []

    if active_gap_stage:
        # 1. Combo conditional promotions (evaluate before stamina_gap_boost so exclusive recovery triggers suppress extra heals)
        for combo in ladder["combo_promotions"]:
            req_ids, _ = _resolve_ordered(combo["require_all"], rows, masters, knowledge, cm)
            if len(req_ids) != len(combo["require_all"]):
                continue
            promo_ids, _ = _resolve_ordered(combo["promote_skills"], rows, masters, knowledge, cm)
            target_tid = combo["target_tier"]
            target_t_idx = tier_id_to_idx.get(target_tid, 0)
            req_ok = True
            req_cost = 0
            for r_sid in req_ids:
                if _is_owned_sid(r_sid):
                    continue
                if r_sid in avoid_set or r_sid not in learnable_clickable:
                    req_ok = False
                    break
                req_cost += learnable_clickable[r_sid]["display_cost"]
            combo_fired = req_ok and req_cost <= budget
            if combo_fired:
                for p_rank, p_sid in enumerate(promo_ids):
                    if p_sid in avoid_set or p_sid not in learnable_clickable:
                        continue
                    cur_t = eff_tier.get(p_sid, fallback_tier_idx + 1)
                    if cur_t > target_t_idx:
                        eff_tier[p_sid] = target_t_idx
                        eff_rank[p_sid] = 50 + p_rank
                        decisions[p_sid]["tier_id"] = target_tid
                        promoted_reasons[p_sid] = f"combo:{combo['combo_id']}"
                        dynamic_promotions.append({
                            "skill_id": p_sid,
                            "reason": f"combo:{combo['combo_id']}",
                            "target_tier": target_tid,
                        })
            if combo.get("exclusive_recovery_trigger"):
                anchor_owned = any(
                    _is_owned_sid(r_sid)
                    for r_sid in req_ids
                    if not _is_recovery_skill(r_sid) or _is_inherited_unique(r_sid)
                )
                if combo_fired or anchor_owned:
                    exclusive_recovery_active = True
                    exclusive_allowed_recovery_ids.update(req_ids)
                    exclusive_allowed_recovery_ids.update(promo_ids)
                    exclusive_combo_ids.append(combo["combo_id"])

        # 2. Stamina gap boost (suppressed when an exclusive recovery combo like Xmas Oguri + 777 is active)
        sb = ladder["stamina_gap_boost"]
        if sb["enabled"] and stage_allow_recovery and not exclusive_recovery_active:
            obs_stats = cm.merge_current_stats(current_stats)
            obs_sta = obs_stats.get("stamina")
            by_threshold = sb["min_stamina"] is not None and obs_sta is not None and obs_sta < sb["min_stamina"]
            gate_status = gate.get("status")
            initial_hp = float(gate.get("initial_hp") or 2000.0)
            if sb["use_hp_gate"] and gate_status in {"surplus", "deficit"}:
                hp_gap = max(0.0, float(gate.get("conservation_deficit_hp") or 0.0)) if gate_status == "deficit" else 0.0
                should_boost = gate_status == "deficit" and hp_gap > 0
            else:
                raw_gap = (
                    max(0.0, float(sb["min_stamina"] - obs_sta) * 1.6) - owned_heal * initial_hp
                    if by_threshold and obs_sta is not None
                    else 0.0
                )
                hp_gap = max(0.0, raw_gap)
                should_boost = by_threshold and hp_gap > 0
            if should_boost and hp_gap > 0:
                promo_ids, _ = _resolve_ordered(sb["promote_skills"], rows, masters, knowledge, cm)
                promo_order = {sid: idx for idx, sid in enumerate(promo_ids)}
                rec_cands = []
                for sid in learnable_clickable:
                    if sid in avoid_set:
                        continue
                    rating = cand_ratings[sid]
                    in_promo = sid in promo_order
                    has_early_rec = rating.recovery_rate > 0
                    if not (in_promo or has_early_rec):
                        continue
                    rec_cands.append((
                        0 if in_promo else 1,
                        promo_order.get(sid, 999),
                        -(rating.recovery_rate / max(1, learnable_clickable[sid]["display_cost"])),
                        learnable_clickable[sid]["display_cost"],
                        sid,
                    ))
                rec_cands.sort()
                promoted_recs = []
                for _in_p, _po, _eff_ratio, _cost, sid in rec_cands:
                    eff_tier[sid] = 0
                    eff_rank.setdefault(sid, 0)
                    if tier_defs:
                        decisions[sid]["tier_id"] = tier_defs[0]["id"]
                    promoted_reasons[sid] = "stamina_gap_boost"
                    promoted_recs.append(sid)
                    dynamic_promotions.append({"skill_id": sid, "reason": "stamina_gap_boost"})
                    heal_gain = cand_ratings[sid].recovery_rate * initial_hp
                    hp_gap -= heal_gain
                    if hp_gap <= 0 and promoted_recs:
                        break

        # 3. Green skill quota (terminal LoH build only, never pre-L'Arc races)
        gq = ladder["green_skill_quota"]
        if gq["enabled"] and terminal:
            gq_ids, _ = _resolve_ordered(gq["skills"], rows, masters, knowledge, cm)
            gq_set = set(gq_ids)
            gq_order = {sid: idx for idx, sid in enumerate(gq_ids)}
            lr_ids, _ = _resolve_ordered(gq.get("last_resort_skills", []), rows, masters, knowledge, cm)
            lr_set = set(lr_ids)
            owned_green_families = set()
            for mid, master in owned.items():
                if mid in superseded_masters:
                    continue
                row_id = master.get("skill_id")
                matching_sid = next((s for s, m in masters.items() if m and m.get("skill_id") == row_id), row_id)
                r = owned_ratings[mid]
                if r.reason == "matched_green" or row_id in gq_set or matching_sid in gq_set or row_id in lr_set or matching_sid in lr_set:
                    owned_green_families.add(_family_id(matching_sid, rows, masters))

            green_cands = []
            for sid in learnable_clickable:
                if sid in avoid_set or decisions[sid].get("reason") == "blocked_by_exclusive_recovery_combo":
                    continue
                r, exc = cand_ratings[sid], cand_exclusions[sid]
                in_gq = sid in gq_set
                in_lr = sid in lr_set
                if not in_gq and not in_lr and (exc or r.reason != "matched_green"):
                    continue
                base_pt = cand_quotes[sid].get("cost_pt")
                disc = _effective_discount_percent(learnable_clickable[sid], base_pt) or 0.0
                t_rank = eff_tier.get(sid, fallback_tier_idx + 1)
                is_spd_green = _is_speed_green(sid)
                is_gold_spd = is_spd_green and (rows.get(sid, {}).get("rarity") == "gold" or (masters.get(sid) or {}).get("rarity") == "gold")
                green_cands.append((
                    1 if in_lr else 0,
                    0 if t_rank == 0 else 1,
                    0 if in_gq else 1,
                    0 if is_spd_green else 1,
                    0 if is_gold_spd else 1,
                    learnable_clickable[sid]["display_cost"],
                    -disc,
                    gq_order.get(sid, 999),
                    sid,
                ))
            green_cands.sort()
            need = max(0, gq["min_count"] - len(owned_green_families))
            used_families = set(owned_green_families)
            kept = 0
            for _lr, _t0, _in_gq, _spd, _gspd, _c, _d, _ord, sid in green_cands:
                if kept >= need:
                    break
                fam = _family_id(sid, rows, masters)
                if fam in used_families:
                    continue
                used_families.add(fam)
                kept += 1
                if eff_tier.get(sid, fallback_tier_idx + 1) > 0:
                    eff_tier[sid] = 0
                    eff_rank.setdefault(sid, 0)
                    if tier_defs:
                        decisions[sid]["tier_id"] = tier_defs[0]["id"]
                    promoted_reasons[sid] = "green_skill_quota"
                    dynamic_promotions.append({"skill_id": sid, "reason": "green_skill_quota"})

        if exclusive_recovery_active:
            for sid in list(learnable_clickable):
                if sid not in exclusive_allowed_recovery_ids and _has_positive_recovery(sid):
                    eff_tier.pop(sid, None)
                    eff_rank.pop(sid, None)
                    promoted_reasons.pop(sid, None)
                    decisions[sid]["tier_id"] = None
                    decisions[sid]["reason"] = "blocked_by_exclusive_recovery_combo"

    eligible = set()
    if stage_skip:
        for sid in learnable_clickable:
            decisions[sid]["reason"] = "stage_skipped_by_policy"
    elif classic:
        c_cfg = stage_policy["classic_arc"]
        for sid in sorted(learnable_clickable):
            if not stage_allow_recovery and _is_recovery_skill(sid):
                decisions[sid]["reason"] = "recovery_blocked_before_terminal"
                continue
            if sid not in eff_tier:
                decisions[sid]["reason"] = "not_in_ladder"
                continue
            tid = decisions[sid].get("tier_id")
            if tid not in c_cfg["allowed_tiers"]:
                decisions[sid]["reason"] = "not_in_classic_allowed_tiers"
                continue
            base_pt = cand_quotes[sid].get("cost_pt")
            if not cm._eligible_discount(learnable_clickable[sid], base_pt, c_cfg["min_discount_percent"]):
                decisions[sid]["reason"] = "classic_discount_below_minimum_or_unverified"
                continue
            disc = _effective_discount_percent(learnable_clickable[sid], base_pt)
            if _is_inherited_unique(sid) and (disc is None or disc < 40.0):
                decisions[sid]["reason"] = "classic_inherited_unique_deferred_before_year3_inheritance"
                continue
            if current_target:
                act_r, act_exc = cm._rating(
                    masters[sid], current_target, current_course, current_gate, current_inputs, evaluate
                )
                decisions[sid]["current_race_reason"] = act_exc or act_r.reason
                if act_exc or act_r.tier >= 3 or act_r.start_requirement:
                    decisions[sid]["reason"] = "not_classic_race_compatible"
                    continue
            eligible.add(sid)
    elif senior_pre and stage_policy["senior_arc"]["mode"] == "t0_only":
        s_cfg = stage_policy["senior_arc"]
        allowed_tids = set(s_cfg["allowed_tiers"])
        for sid in sorted(learnable_clickable):
            if not stage_allow_recovery and _is_recovery_skill(sid):
                decisions[sid]["reason"] = "recovery_blocked_before_terminal"
                continue
            if sid not in eff_tier:
                decisions[sid]["reason"] = "not_in_ladder"
                continue
            tid = decisions[sid].get("tier_id")
            is_t0_promo = sid in promoted_reasons and eff_tier.get(sid) == 0
            if tid not in allowed_tids and not is_t0_promo:
                decisions[sid]["reason"] = "not_in_senior_allowed_tiers"
                continue
            eligible.add(sid)
    else:
        for sid in sorted(learnable_clickable):
            if decisions[sid].get("reason") == "blocked_by_exclusive_recovery_combo":
                continue
            if not terminal and not stage_allow_recovery and _is_recovery_skill(sid):
                decisions[sid]["reason"] = "recovery_blocked_before_terminal"
                continue
            if sid in eff_tier:
                eligible.add(sid)
            else:
                decisions[sid]["reason"] = "not_in_ladder"

    # Groundwork gate & atomic source bundling
    groundwork_bundles = {}
    link_priorities = {}
    gw_cfg = ladder["groundwork_gate"]
    if gw_cfg["enabled"]:
        gw_ids = [sid for sid in sorted(eligible) if cand_ratings[sid].start_requirement > 0]
        owned_gw_reqs = [
            owned_ratings[mid].start_requirement
            for mid in owned
            if mid not in superseded_masters and owned_ratings[mid].start_requirement > 0
        ]
        if gw_ids or (terminal and owned_gw_reqs):
            gq_cfg = ladder["green_skill_quota"]
            gq_ids, _ = _resolve_ordered(gq_cfg.get("skills", []), rows, masters, knowledge, cm)
            gq_order = {sid: idx for idx, sid in enumerate(gq_ids)}
            lr_ids, _ = _resolve_ordered(gq_cfg.get("last_resort_skills", []), rows, masters, knowledge, cm)
            lr_set = set(lr_ids)

            owned_source_fams = set()
            owned_green_fams = set()
            owned_active_fams = set()
            for mid in owned:
                if mid in superseded_masters or not owned_ratings[mid].start_source:
                    continue
                m_row = owned[mid]
                fam = str((m_row.get("cost") or {}).get("chain", [mid])[0])
                owned_source_fams.add(fam)
                if m_row.get("type") == "green":
                    owned_green_fams.add(fam)
                else:
                    owned_active_fams.add(fam)

            avail_sources = []
            seen_src_fams = set(owned_source_fams)
            for sid in learnable_clickable:
                if sid in avoid_set or decisions[sid].get("reason") == "blocked_by_exclusive_recovery_combo":
                    continue
                if not cand_ratings[sid].start_source:
                    continue
                fam = _family_id(sid, rows, masters)
                if fam in seen_src_fams:
                    continue
                if sid not in eligible and cand_exclusions[sid]:
                    continue
                is_green = (rows.get(sid, {}).get("type") or (masters.get(sid) or {}).get("type")) == "green"
                green_bucket = (0 if sid in gq_order else 2 if sid in lr_set else 1) if is_green else 0
                green_tier = (0 if _is_speed_green(sid) else 1) if is_green else 0
                green_rank = gq_order.get(sid, 999) if is_green else 999
                base_pt = cand_quotes[sid].get("cost_pt")
                disc = _effective_discount_percent(learnable_clickable[sid], base_pt) or 0.0
                avail_sources.append((
                    0 if sid in eligible else 1,
                    eff_tier.get(sid, fallback_tier_idx + 1),
                    green_bucket,
                    green_tier,
                    learnable_clickable[sid]["display_cost"],
                    -disc,
                    green_rank,
                    sid,
                    fam,
                ))
            avail_sources.sort()
            dedup_sources = []
            picked_fams = set(owned_source_fams)
            for _el, _t, _gb, _gt, _c, _d, _gr, sid, fam in avail_sources:
                if fam in picked_fams:
                    continue
                picked_fams.add(fam)
                dedup_sources.append(sid)

            def _pick_groundwork_sources(req: int, gw_cost: int, gw_tier: int, extra_slots: int) -> list[str] | None:
                missing = max(0, req - len(owned_source_fams))
                chosen = None
                if gw_cfg["reliable_active_pair"] and req >= 3:
                    avail_green = [
                        s for s in dedup_sources
                        if (rows.get(s, {}).get("type") or (masters.get(s) or {}).get("type")) == "green"
                    ]
                    avail_active = [
                        s for s in dedup_sources
                        if (rows.get(s, {}).get("type") or (masters.get(s) or {}).get("type")) != "green"
                    ]
                    need_g3 = max(0, req - len(owned_green_fams))
                    need_g2 = max(0, (req - 1) - len(owned_green_fams))
                    need_a2 = max(0, 2 - len(owned_active_fams))
                    rel_pkgs = []
                    if len(avail_green) >= need_g3:
                        rel_pkgs.append(avail_green[:need_g3])
                    if len(avail_green) >= need_g2 and len(avail_active) >= need_a2:
                        rel_pkgs.append(avail_green[:need_g2] + avail_active[:need_a2])
                    valid_pkgs = [
                        pkg for pkg in rel_pkgs
                        if extra_slots + len(pkg) <= batch_cap
                        and gw_cost + sum(learnable_clickable[x]["display_cost"] for x in pkg) <= budget
                    ]
                    if valid_pkgs:
                        chosen = min(
                            valid_pkgs,
                            key=lambda pkg: (
                                sum(1 for x in pkg if x not in eligible or eff_tier.get(x, fallback_tier_idx + 1) > gw_tier),
                                len(pkg),
                                sum(learnable_clickable[x]["display_cost"] for x in pkg),
                            ),
                        )
                        if not chosen:
                            return []
                if chosen is None:
                    if missing == 0:
                        return []
                    chosen = dedup_sources[:missing]
                    bundle_cost = gw_cost + sum(learnable_clickable[x]["display_cost"] for x in chosen)
                    if len(chosen) < missing or extra_slots + len(chosen) > batch_cap or bundle_cost > budget:
                        return None
                return chosen

            def _apply_chosen_sources(chosen: list[str], gw_tier: int) -> None:
                for src in chosen:
                    eligible.add(src)
                    if eff_tier.get(src, fallback_tier_idx + 1) > gw_tier:
                        eff_tier[src] = gw_tier
                        eff_rank.setdefault(src, 0)
                        if gw_tier < len(tier_defs):
                            decisions[src]["tier_id"] = tier_defs[gw_tier]["id"]
                        if gw_tier == 0 and src not in promoted_reasons:
                            promoted_reasons[src] = "groundwork_start_source"
                            dynamic_promotions.append({"skill_id": src, "reason": "groundwork_start_source"})
                    link_priorities[src] = cm.CM_START_SOURCE_LINK
                    decisions[src]["reason"] = None

            for gw_sid in gw_ids:
                req = max(cand_ratings[gw_sid].start_requirement, gw_cfg["min_sources"])
                chosen = _pick_groundwork_sources(
                    req, learnable_clickable[gw_sid]["display_cost"], eff_tier[gw_sid], 1
                )
                if chosen is None:
                    eligible.discard(gw_sid)
                    decisions[gw_sid]["reason"] = "insufficient_start_activation_sources"
                    continue
                if not chosen:
                    continue
                _apply_chosen_sources(chosen, eff_tier[gw_sid])
                link_priorities[gw_sid] = cm.CM_GROUNDWORK_LINK
                groundwork_bundles[gw_sid] = {
                    "required": req,
                    "owned_source_count": len(owned_source_fams),
                    "required_skill_ids": chosen,
                }

            if not gw_ids and terminal and owned_gw_reqs:
                req = max(max(owned_gw_reqs), gw_cfg["min_sources"])
                chosen = _pick_groundwork_sources(req, 0, 0, 0)
                if chosen:
                    _apply_chosen_sources(chosen, 0)
                    groundwork_bundles["__owned_groundwork__"] = {
                        "required": req,
                        "owned_source_count": len(owned_source_fams),
                        "required_skill_ids": chosen,
                    }

    # Compute within-tier bonuses
    def _within_bonus(sid: str) -> int:
        t_idx = eff_tier.get(sid, fallback_tier_idx)
        if t_idx >= len(tier_defs):
            r = cand_ratings[sid]
            return min(8000, max(1, int(r.utility)))
        order_mode = tier_defs[t_idx]["within_tier_order"]
        rank = eff_rank.get(sid, 0)
        if order_mode == "strict_order":
            return max(0, 8000 - 200 * min(rank, 39))
        item = learnable_clickable[sid]
        cost = item["display_cost"]
        base_pt = cand_quotes[sid].get("cost_pt")
        disc = _effective_discount_percent(item, base_pt) or 0.0
        white_bonus = (
            120
            if classic and stage_policy["classic_arc"]["prefer_white"] and rows.get(sid, {}).get("rarity") == "normal" and not _is_inherited_unique(sid)
            else 0
        )
        raw = round(60.0 * min(max(disc, 0.0), 50.0) + max(0, 220 - min(cost, 220)) * 20)
        tie = max(0, 40 - min(rank, 40))
        return min(8000, raw + white_bonus + tie)

    # Accel group caps (shift overflow candidates down)
    for cap in ladder["accel_group_caps"]:
        cap_ids, _ = _resolve_ordered(cap["skills"], rows, masters, knowledge, cm)
        cap_set = set(cap_ids)
        owned_count = sum(
            1
            for mid in owned
            if mid not in superseded_masters
            and (mid in cap_set or any(s in cap_set for s in (rows.get(mid, {}).get("relations", {}).get("supersedes", []))))
        )
        in_group = [sid for sid in eligible if sid in cap_set]
        in_group.sort(key=lambda sid: (eff_tier[sid], -_within_bonus(sid), learnable_clickable[sid]["display_cost"], sid))
        keep = max(0, cap["max_count"] - owned_count)
        max_explicit_idx = max(0, len(tier_defs) - 1)
        for sid in in_group[keep:]:
            eff_tier[sid] = min(max_explicit_idx, eff_tier[sid] + cap["overflow_tier_shift"])
            decisions[sid]["accel_cap_shifted"] = cap["group_id"]

    # Optional per-tier max_count cap
    for idx, tier in enumerate(tier_defs):
        cap_n = tier.get("max_count")
        if cap_n is None:
            continue
        in_tier = [sid for sid in eligible if eff_tier.get(sid) == idx and sid not in link_priorities]
        if len(in_tier) > cap_n:
            in_tier.sort(key=lambda sid: (-_within_bonus(sid), learnable_clickable[sid]["display_cost"], sid))
            for sid in in_tier[cap_n:]:
                eligible.discard(sid)
                decisions[sid]["reason"] = "tier_max_count_exceeded"

    # Shortlist to CM _MAX_CANDIDATES (48)
    ordered = sorted(
        eligible,
        key=lambda sid: (
            learnable_clickable[sid]["display_cost"] > budget,
            eff_tier.get(sid, fallback_tier_idx),
            -_within_bonus(sid),
            learnable_clickable[sid]["display_cost"],
            sid,
        ),
    )
    kept = set(ordered[: cm._MAX_CANDIDATES])
    for sid in eligible - kept:
        decisions[sid]["reason"] = "optimizer_candidate_shortlist"
    eligible = kept

    for gw_sid, bundle in list(groundwork_bundles.items()):
        if gw_sid in eligible and not set(bundle["required_skill_ids"]) <= eligible:
            eligible.remove(gw_sid)
            decisions[gw_sid]["reason"] = "insufficient_start_activation_sources"

    # Compute target integer utility & emit exact SkillPreferenceProfile rules
    num_explicit = max(1, len(tier_defs))
    step_utilities: dict[str, int] = {}
    for sid in sorted(eligible):
        t_idx = eff_tier.get(sid, fallback_tier_idx)
        bonus = _within_bonus(sid)
        if t_idx < len(tier_defs):
            band = 100_000 * (12 ** max(0, num_explicit - 1 - t_idx))
            step_utilities[sid] = int(band + bonus)
        else:
            r = cand_ratings[sid]
            step_utilities[sid] = int(max(1, min(150, round(r.utility))))

    utilities = dict(step_utilities)
    if planner_policy.get("allow_gold_without_lower", True) is not False:
        for sid in sorted(eligible):
            if rows.get(sid, {}).get("rarity") != "gold":
                continue
            unowned_lows = [
                low_sid
                for low_sid in rows.get(sid, {}).get("relations", {}).get("lower", [])
                if not _is_owned_sid(low_sid) and low_sid in step_utilities
            ]
            if unowned_lows:
                best_low = max(unowned_lows, key=lambda l: step_utilities[l])
                utilities[sid] = int(
                    step_utilities[sid]
                    + step_utilities[best_low]
                    + cm._native_utility(rows[best_low])
                )

    candidate_ids = set(rows) | set(items)
    result["rules"] = [
        rule
        for rule in result.get("rules", [])
        if not (rule["selector"]["kind"] == "skill_id" and rule["selector"]["value"] in candidate_ids)
    ]
    result["rules"].extend(cm._rule(sid, "avoid") for sid in sorted(candidate_ids - eligible))

    for sid in sorted(eligible):
        t_idx = eff_tier.get(sid, fallback_tier_idx)
        base = cm._native_utility(rows[sid])
        target_util = utilities[sid]
        if t_idx == 0 and tier_defs:
            must_prio = 1000 + min(4000, _within_bonus(sid) // 2)
            result["rules"].append(cm._rule(sid, "must_buy_if_safe", priority=must_prio))
        if t_idx >= len(tier_defs):
            # Fallback skill: keep prefer_weight <= 0 so is_cm_core_candidate == False (pass 2 filler)
            fallback_weight = min(0, int(target_util) - base)
            if int(target_util) - base > 0:
                fallback_weight = 0
            result["rules"].append(
                cm._rule(sid, "prefer", priority=link_priorities.get(sid, 0), weight=fallback_weight)
            )
        else:
            result["rules"].append(
                cm._rule(sid, "prefer", priority=link_priorities.get(sid, 0), weight=target_util - base)
            )
        decisions[sid].update(
            tier=f"T{t_idx}" if t_idx < len(tier_defs) else None,
            cm_reason=promoted_reasons.get(sid, decisions[sid].get("lower_fallback_of") and "lower_fallback" or "ladder_tier"),
            cm_utility=target_util,
        )

    result["profile_id"] += cm.CM_PROFILE_SUFFIX
    diagnostic = {
        "enabled": True,
        "mode": "tiered_ladder",
        "auto_cm": False,
        "year": year,
        "terminal_build": terminal,
        "final_target": target,
        "preset_id": geometry,
        "current_stats": cm.merge_current_stats(current_stats),
        "stamina_recovery_gate": gate,
        "eligible_skill_ids": sorted(eligible),
        "groundwork_bundles": {sid: data for sid, data in groundwork_bundles.items() if sid in eligible or sid == "__owned_groundwork__"},
        "reserve_skill_pt": result["reserve_skill_pt"],
        "ladder_resolution": ladder_resolution,
        "dynamic_promotions": dynamic_promotions,
        "exclusive_recovery_combos": exclusive_combo_ids,
        "skills": list(decisions.values()),
    }
    return result, diagnostic
