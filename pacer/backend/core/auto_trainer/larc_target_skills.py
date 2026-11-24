"""L'Arc target policy and Pacer-local modules for the isolated D1 workers."""
from __future__ import annotations

import builtins
from copy import deepcopy
from functools import lru_cache
import importlib.util
import json
from pathlib import Path
import sys


TARGET_FIELDS = frozenset({"distance_type", "style", "surface", "distance_m",
    "track_id", "ground_condition", "season", "weather", "rotation", "field_size"})


@lru_cache(maxsize=1)
def _compiler():
    path = Path(__file__).resolve().parents[1] / "skill_planner/cm_skill_compiler.py"
    spec = importlib.util.spec_from_file_location("_pacer_cm_skill_compiler", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@lru_cache(maxsize=1)
def _tiered_compiler():
    path = Path(__file__).resolve().parents[1] / "skill_planner/tiered_skill_compiler.py"
    spec = importlib.util.spec_from_file_location("_pacer_tiered_skill_compiler", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@lru_cache(maxsize=1)
def _ladder_presets():
    path = Path(__file__).resolve().parents[1] / "skill_planner/tiered_ladder_presets.py"
    spec = importlib.util.spec_from_file_location("_pacer_tiered_ladder_presets", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _merge_preset_dict(base: dict, override) -> dict:
    if not isinstance(override, dict):
        return deepcopy(base)
    merged = deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _merge_preset_dict(merged[k], v)
        else:
            merged[k] = deepcopy(v)
    return merged


def normalize_arc_policy(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict) or type(value.get("enabled", False)) is not bool:
        raise ValueError("arc_policy_invalid")
    ladder_preset_id = value.get("ladder_preset_id")
    ladder_preset = None
    if ladder_preset_id is not None:
        if not isinstance(ladder_preset_id, str) or not ladder_preset_id.strip():
            raise ValueError("arc_policy_ladder_preset_id_invalid")
        ladder_preset_id = ladder_preset_id.strip()
        ladder_preset = _ladder_presets().resolve_tiered_ladder_preset(ladder_preset_id)

    raw_target = value.get("final_target")
    if raw_target is not None and not isinstance(raw_target, dict):
        raise ValueError("arc_policy_final_target_invalid")
    target = {**(ladder_preset.get("final_target") or {}), **(raw_target or {})} if ladder_preset else (raw_target or {})
    if set(target) - TARGET_FIELDS:
        raise ValueError("arc_policy_final_target_invalid")
    target = {k: v for k, v in target.items() if v not in (None, "", "any", "unknown")}
    for key, options in (("distance_type", {"short", "mile", "middle", "long"}),
                         ("style", {"leader", "front", "betweener", "chaser"}),
                         ("surface", {"turf", "dirt", "草地", "泥地"})):
        if key in target and target[key] not in options:
            raise ValueError("arc_policy_final_target_" + key + "_invalid")
    if "field_size" in target and (type(target["field_size"]) is not int or target["field_size"] not in {9, 12}):
        raise ValueError("arc_policy_final_target_field_size_invalid")
    for key in TARGET_FIELDS - {"distance_type", "style", "surface", "field_size"}:
        if key in target and (type(target[key]) is not int or target[key] <= 0):
            raise ValueError("arc_policy_final_target_" + key + "_invalid")
    if "distance_m" in target and "distance_type" in target:
        if target["distance_type"] != _compiler().distance_type_for(target["distance_m"]):
            raise ValueError("arc_policy_distance_conflict")
    entries = value.get("classic_allowlist", [])
    if not isinstance(entries, list) or any(not isinstance(v, str) or not v.strip() for v in entries):
        raise ValueError("arc_policy_classic_allowlist_invalid")
    preset_id = value.get("preset_id") or (ladder_preset.get("preset_id") if ladder_preset else None)
    if preset_id is not None:
        if not isinstance(preset_id, str) or preset_id not in _compiler().CM_TRACK_PRESETS:
            raise ValueError("arc_policy_preset_id_invalid")
        target, _ = _compiler().resolve_cm_target(target, preset_id)
    auto_cm = value.get("auto_cm", True)
    if type(auto_cm) is not bool:
        raise ValueError("arc_policy_auto_cm_invalid")
    mid_reserve_pt = value.get("mid_reserve_pt", 360)
    min_mid_discount = value.get("min_mid_discount_percent", 20)
    if type(mid_reserve_pt) is not int or not 0 <= mid_reserve_pt <= 2000:
        raise ValueError("arc_policy_mid_reserve_pt_invalid")
    if type(min_mid_discount) is not int or not 0 <= min_mid_discount <= 60:
        raise ValueError("arc_policy_min_mid_discount_percent_invalid")
    mode = value.get("mode", "tiered_ladder" if ladder_preset else "auto_cm")
    if mode not in {"auto_cm", "tiered_ladder"}:
        raise ValueError("arc_policy_mode_invalid")
    result = {"enabled": value.get("enabled", False), "final_target": target,
            "classic_allowlist": list(dict.fromkeys(v.strip() for v in entries)), "preset_id": preset_id,
            "auto_cm": auto_cm, "mid_reserve_pt": mid_reserve_pt, "min_mid_discount_percent": min_mid_discount,
            "mode": mode}
    if ladder_preset_id is not None:
        result["ladder_preset_id"] = ladder_preset_id
    if mode == "tiered_ladder" or value.get("ladder") is not None:
        raw_ladder = value.get("ladder")
        if raw_ladder is not None and not isinstance(raw_ladder, dict):
            raise ValueError("arc_policy_ladder_invalid")
        merged_ladder = _merge_preset_dict(ladder_preset.get("ladder") or {}, raw_ladder) if ladder_preset else raw_ladder
        result["ladder"] = _tiered_compiler().normalize_ladder_policy(merged_ladder)
    if mode == "tiered_ladder" or value.get("stage_policy") is not None:
        raw_stage = value.get("stage_policy")
        if raw_stage is not None and not isinstance(raw_stage, dict):
            raise ValueError("arc_policy_stage_policy_invalid")
        merged_stage = _merge_preset_dict(ladder_preset.get("stage_policy") or {}, raw_stage) if ladder_preset else raw_stage
        result["stage_policy"] = _tiered_compiler().normalize_stage_policy(merged_stage, mode=mode)
    if mode == "tiered_ladder" or value.get("evolution_policy") is not None:
        raw_evo = value.get("evolution_policy")
        if raw_evo is not None and not isinstance(raw_evo, dict):
            raise ValueError("arc_policy_evolution_invalid")
        merged_evo = _merge_preset_dict(ladder_preset.get("evolution_policy") or {}, raw_evo) if ladder_preset else raw_evo
        result["evolution_policy"] = _tiered_compiler().normalize_evolution_policy(merged_evo)
    return result


def policy_from_run(run_root):
    path = Path(run_root) / "goal.json"
    goal = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    return normalize_arc_policy(goal.get("skill_timing", {}).get("arc_policy"))


def target_context(context, policy):
    policy = normalize_arc_policy(policy)
    result = deepcopy(context)
    if not policy["enabled"]:
        return result
    target = policy["final_target"]
    if policy["auto_cm"] or policy["mode"] == "tiered_ladder":
        target, _ = _compiler().resolve_cm_target(target, policy["preset_id"])
    result.update(arc_policy=policy, final_target=target,
        current_race_profile=deepcopy(context.get("current_race_profile", context.get("race_profile", {}))),
        race_profile={"distance_type": "any", "style": "any", **target},
        target_mode="race", larc_skill_purpose="arc_target")
    result.setdefault("policy", {}).update(require_condition_match=False)
    result["policy"].setdefault("allow_gold_without_lower", True)
    # Terminal harvest filters must never overwrite the final race purpose.
    result["policy"].pop("harvest_only", None)
    return result


def read_current_stats(run_root, progress, *snapshots):
    """Use saved observations from this run and the current or preceding turn."""
    merge = _compiler().merge_current_stats
    stats = merge(progress.get("current_stats"), *snapshots)
    if len(stats) == 5 or type(progress.get("next_turn")) is not int:
        return stats
    turn = progress["next_turn"]
    available = {}
    for path in sorted(Path(run_root).glob("autonomous_*/summary.json"), reverse=True):
        try:
            summary = json.loads(path.read_text(encoding="utf-8-sig"))
            if (summary.get("run_session_id") != progress.get("run_session_id")
                    or summary.get("device") != progress.get("device")
                    or summary.get("final_progress", {}).get("next_turn") not in {turn, turn - 1}):
                continue
            for row in reversed(summary.get("turns", [])):
                index = row.get("turn_index")
                if index in {turn, turn - 1} and index not in available:
                    observation = merge(row.get("before", {}).get("current_stats"),
                                        row.get("after", {}).get("current_stats"), row.get("current_stats"))
                    if observation:
                        available[index] = observation
        except (OSError, ValueError, AttributeError, TypeError):
            continue
        if turn in available:
            break
    return merge(available.get(turn - 1), available.get(turn), stats)


def load_knowledge(planner_catalog):
    """Load U3 without replacing D1's core or scripts packages."""
    u3_root = Path(__file__).resolve().parents[2]
    catalog_dir = u3_root / "data/skills/cn_larc_end"
    def module(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        result = importlib.util.module_from_spec(spec)
        return spec, result
    spec, conditions = module("_arc_u3_conditions", u3_root / "core/skill_conditions.py")
    spec.loader.exec_module(conditions)
    spec, knowledge = module("_arc_u3_knowledge", u3_root / "core/skill_knowledge.py")
    def private_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "core.skill_conditions" and level == 0:
            return conditions
        return builtins.__import__(name, globals, locals, fromlist, level)
    knowledge.__dict__["__builtins__"] = {**vars(builtins), "__import__": private_import}
    spec.loader.exec_module(knowledge)
    library = knowledge.SkillKnowledgeLibrary.load(catalog_dir / "cn_skill_master.json")
    library.arc_race_inputs = knowledge.race_inputs
    library.arc_evaluate_condition = conditions.evaluate_condition
    return library


def stage_skill_preference(profile, inventory, catalog, *, context, year, knowledge=None):
    policy = normalize_arc_policy(context.get("arc_policy"))
    if not policy["enabled"]:
        return deepcopy(profile), {"enabled": False}
    if knowledge is None:
        knowledge = load_knowledge(context.get("planner_catalog"))
    if policy["mode"] == "tiered_ladder":
        return _tiered_compiler().compile_tiered_skill_profile(
            profile,
            inventory,
            catalog,
            policy=policy,
            current_stats=context.get("current_stats"),
            year=year,
            terminal=context.get("terminal_build"),
            current_race_profile=context.get("current_race_profile"),
            planner_policy=context.get("policy"),
            knowledge=knowledge,
            cm_module=_compiler(),
        )
    return _compiler().compile_cm_skill_profile(profile, inventory, catalog,
        final_target=policy["final_target"], preset_id=policy["preset_id"],
        current_stats=context.get("current_stats"), year=year, terminal=context.get("terminal_build"),
        classic_allowlist=policy["classic_allowlist"], auto_cm=policy["auto_cm"],
        mid_reserve_pt=policy["mid_reserve_pt"], min_mid_discount_percent=policy["min_mid_discount_percent"],
        current_race_profile=context.get("current_race_profile"),
        planner_policy=context.get("policy"), knowledge=knowledge)


def validate_target_plan(actions, diagnostic, *, remaining_skill_points=None):
    if not diagnostic.get("enabled"):
        return
    selected = set(actions)
    if not selected <= set(diagnostic["eligible_skill_ids"]):
        raise ValueError("arc_target_plan_outside_qualified_inventory")
    if (selected and remaining_skill_points is not None
            and remaining_skill_points < diagnostic.get("reserve_skill_pt", 0)):
        raise ValueError("arc_target_plan_spends_reserved_points")
    for sid, bundle in diagnostic.get("groundwork_bundles", {}).items():
        if sid in selected and not set(bundle["required_skill_ids"]) <= selected:
            raise ValueError("arc_target_groundwork_sources_missing")


def _safe_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return None


def _spent_from_worker(path):
    data = _safe_json(path)
    if not isinstance(data, dict):
        return 0
    if data.get("ok") is not True or data.get("stage") != "complete":
        return 0
    if data.get("reason") in {"no_suitable_skills", "no_purchase_return_verified"}:
        return 0
    if "selected_action_ids" in data and not data["selected_action_ids"]:
        return 0
    before = (data.get("before") or {}).get("current_skill_points")
    rem = data.get("remaining_skill_points")
    after = rem if type(rem) is int else (data.get("after") or {}).get("current_skill_points")
    if type(before) is int and type(after) is int and 0 <= after < before:
        return before - after
    return 0


def summarize_run_skill_pt(run_root, *, current_pt=None) -> dict:
    """Read-only per-stage Skill Pt spent/earned ledger for a run directory."""
    root = Path(run_root)

    def _norm_key(raw_path):
        p = Path(raw_path)
        if not p.is_absolute():
            p = root / p
        try:
            return str(p.resolve()), p
        except OSError:
            return str(p), p

    mid = 0
    support_ledger = _safe_json(root / "session/larc_support_skill_startup.json")
    seen_mid = set()
    has_history = False
    if isinstance(support_ledger, dict):
        history = support_ledger.get("purchase_history")
        if isinstance(history, list) and history:
            has_history = True
            for row in history:
                if not isinstance(row, dict):
                    continue
                raw_w = row.get("worker_report_path")
                if raw_w:
                    key, _ = _norm_key(raw_w)
                    if key in seen_mid:
                        continue
                    seen_mid.add(key)
                spent = row.get("spent_pt")
                if type(spent) is int and spent > 0:
                    mid += spent
    if not has_history:
        for wp in sorted(root.glob("**/support_skills_*/worker/worker_report.json")):
            key, p = _norm_key(wp)
            if key in seen_mid:
                continue
            seen_mid.add(key)
            mid += _spent_from_worker(p)

    arc_ledger = _safe_json(root / "session/larc_arc_skills.json")
    classic_arc = 0
    senior_arc = 0
    seen_arc = set()
    stages = arc_ledger.get("stages") if isinstance(arc_ledger, dict) else None
    if isinstance(stages, dict):
        for stage_key, bucket in (("2", "classic"), ("3", "senior")):
            st = stages.get(stage_key)
            if not isinstance(st, dict):
                continue
            for raw_batch in st.get("batches") or []:
                if not isinstance(raw_batch, (str, Path)):
                    continue
                key, p = _norm_key(raw_batch)
                if key in seen_arc:
                    continue
                seen_arc.add(key)
                spent = _spent_from_worker(p)
                if bucket == "classic":
                    classic_arc += spent
                else:
                    senior_arc += spent

    terminal = 0
    term_state = _safe_json(root / "session/larc_terminal_skills.json")
    term_report = None
    if isinstance(term_state, dict) and term_state.get("report_path"):
        _, rp = _norm_key(term_state["report_path"])
        term_report = _safe_json(rp)
    seen_term = set()
    if isinstance(term_report, dict):
        for raw_batch in term_report.get("batches") or []:
            if not isinstance(raw_batch, (str, Path)):
                continue
            key, p = _norm_key(raw_batch)
            if key in seen_term:
                continue
            seen_term.add(key)
            terminal += _spent_from_worker(p)

    resolved_current_pt = current_pt if (type(current_pt) is int and current_pt >= 0) else None
    if resolved_current_pt is None and isinstance(term_report, dict):
        rem = term_report.get("remaining_skill_points")
        if type(rem) is int and rem >= 0:
            resolved_current_pt = rem
    if resolved_current_pt is None and isinstance(stages, dict):
        for stage_key in sorted(stages.keys(), reverse=True):
            st = stages.get(stage_key)
            if isinstance(st, dict):
                rem = st.get("remaining_skill_points")
                if type(rem) is int and rem >= 0:
                    resolved_current_pt = rem
                    break
    if resolved_current_pt is None and isinstance(support_ledger, dict):
        rem = support_ledger.get("last_checked_pt")
        if type(rem) is int and rem >= 0:
            resolved_current_pt = rem

    spent_total = mid + classic_arc + senior_arc + terminal
    return {
        "schema_version": "uma.run_skill_pt_ledger.v1",
        "mid": mid,
        "classic_arc": classic_arc,
        "senior_arc": senior_arc,
        "terminal": terminal,
        "spent_pt_total": spent_total,
        "current_pt": resolved_current_pt,
        "earned_pt_total": (spent_total + resolved_current_pt) if resolved_current_pt is not None else None,
    }
