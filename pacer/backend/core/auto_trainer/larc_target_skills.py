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
    "track_id", "ground_condition", "season", "weather", "rotation"})


@lru_cache(maxsize=1)
def _compiler():
    path = Path(__file__).resolve().parents[1] / "skill_planner/cm_skill_compiler.py"
    spec = importlib.util.spec_from_file_location("_pacer_cm_skill_compiler", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def normalize_arc_policy(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict) or type(value.get("enabled", False)) is not bool:
        raise ValueError("arc_policy_invalid")
    target = value.get("final_target") or {}
    if not isinstance(target, dict) or set(target) - TARGET_FIELDS:
        raise ValueError("arc_policy_final_target_invalid")
    target = {k: v for k, v in target.items() if v not in (None, "", "any", "unknown")}
    for key, options in (("distance_type", {"short", "mile", "middle", "long"}),
                         ("style", {"leader", "front", "betweener", "chaser"}),
                         ("surface", {"turf", "dirt", "草地", "泥地"})):
        if key in target and target[key] not in options:
            raise ValueError("arc_policy_final_target_" + key + "_invalid")
    for key in TARGET_FIELDS - {"distance_type", "style", "surface"}:
        if key in target and (type(target[key]) is not int or target[key] <= 0):
            raise ValueError("arc_policy_final_target_" + key + "_invalid")
    if "distance_m" in target and "distance_type" in target:
        if target["distance_type"] != _compiler().distance_type_for(target["distance_m"]):
            raise ValueError("arc_policy_distance_conflict")
    entries = value.get("classic_allowlist", [])
    if not isinstance(entries, list) or any(not isinstance(v, str) or not v.strip() for v in entries):
        raise ValueError("arc_policy_classic_allowlist_invalid")
    preset_id = value.get("preset_id")
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
    return {"enabled": value.get("enabled", False), "final_target": target,
            "classic_allowlist": list(dict.fromkeys(v.strip() for v in entries)), "preset_id": preset_id,
            "auto_cm": auto_cm, "mid_reserve_pt": mid_reserve_pt, "min_mid_discount_percent": min_mid_discount}


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
    if policy["auto_cm"]:
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
