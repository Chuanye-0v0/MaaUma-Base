"""Comprehensive Phase A unit tests for outer-loop takeover, surface graduation,
mid-skill per-date overrides, α-star speed_effect, Kawasaki 2100m LoH preset,
tiered skill ladder compilation, stage gating, and Pt ledger accounting.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import types
import unittest
from unittest.mock import patch

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
PACER_BACKEND = Path(os.environ.get("MAAUMA_PACER_BACKEND_DIR") or (REPO_ROOT / "pacer" / "backend"))
CORE_BACKEND = REPO_ROOT / "backend"


def _ensure_paths() -> dict[str, object]:
    for path in (PACER_BACKEND, CORE_BACKEND):
        resolved = str(path.resolve())
        if resolved in sys.path:
            sys.path.remove(resolved)
    # Keep pacer/backend ahead of backend so core.auto_trainer resolves to pacer/backend
    # while backend-only modules (core.skill_planner.global_optimizer, catalog, ...) stay importable.
    sys.path.insert(0, str(CORE_BACKEND.resolve()))
    sys.path.insert(0, str(PACER_BACKEND.resolve()))
    injected: dict[str, object] = {}
    if "config.settings" not in sys.modules:
        cfg_mod = types.ModuleType("config")
        cfg_set = types.ModuleType("config.settings")
        cfg_set.ADB_HOST = "127.0.0.1"
        cfg_set.ADB_ENDPOINT = "127.0.0.1:16384"
        cfg_set.ADB_PORT = 16384
        cfg_set.ADB_PATH = ""
        cfg_set.SWIPE_DELAY = 0.2
        cfg_set.TAP_DELAY = 0.2
        cfg_set.ASSETS_DIR = PACER_BACKEND / "assets"
        cfg_mod.settings = cfg_set
        sys.modules["config"] = cfg_mod
        sys.modules["config.settings"] = cfg_set
        injected = {"config": cfg_mod, "config.settings": cfg_set}
    return injected


def _valid_binding(local_db_id: str) -> dict:
    return {
        "status": "auto_confirmed",
        "confidence": 0.96,
        "margin": 0.65,
        "evidence_dimensions": 4,
        "binding_policy": "current_run_multi_evidence",
        "evidence": {"cost": 1.0, "rarity": 1.0, "icon_color": 1.0},
        "current_run_contract": {
            "version": "current_run_multi_evidence.v1",
            "local_db_id": local_db_id,
            "identity_status": "exact_local_db_id_join",
            "metadata_status": "external_cross_checked",
            "description_anchor_exact": True,
        },
    }


def _master_row(
    skill_id: str,
    name: str,
    *,
    skill_type: str = "speed",
    rarity: str = "normal",
    base_cost: int = 180,
    evaluation_score: int = 217,
    trigger_code: str = "distance_rate>=50",
    type_raw: str = "速度",
    value_raw: str = "0.15",
    duration_raw: str = "2.40",
    group_id: int = 1000,
    group_rate: int = 1,
    relations: dict | None = None,
    chain: list[str] | None = None,
    conditions: dict | None = None,
    condition_terms: list[dict] | None = None,
    effects: list[dict] | None = None,
) -> dict:
    try:
        dur_sec = float(duration_raw)
    except (TypeError, ValueError):
        dur_sec = 0.0
    terms = condition_terms if condition_terms is not None else []
    effs = effects if effects is not None else []
    branches = (
        [
            {
                "condition_ast": {"all": terms} if terms else {"constant": True},
                "precondition_ast": {"constant": True},
                "duration": {"base_seconds": dur_sec},
                "effects": effs,
            }
        ]
        if (terms or effs)
        else []
    )
    return {
        "skill_id": skill_id,
        "name_zh_cn": name,
        "type": skill_type,
        "rarity": rarity,
        "is_verified": True,
        "requires_manual_verification": False,
        "manual_status": "manual_verified",
        "identity_status": "manual_verified",
        "metadata_status": "manual_verified",
        "acquisition": {"kind": "pt_purchase"},
        "evaluation_score": evaluation_score,
        "base_cost": base_cost,
        "upgrade_cost": base_cost,
        "total_cost": base_cost,
        "type_raw": type_raw,
        "value_raw": value_raw,
        "duration_raw": duration_raw,
        "trigger_code": trigger_code,
        "precondition": "",
        "conditions": conditions or {"distance": ["any"], "style": ["any"]},
        "group_id": group_id,
        "group_rate": group_rate,
        "db_id": skill_id.split(":")[-1],
        "local_db_id": skill_id.split(":")[-1],
        "relations": relations or {},
        "cost": {"chain": chain or [skill_id], "base_pt": base_cost},
        "effects": effs,
        "activation_branches": branches,
        "knowledge": {
            "trigger_code": trigger_code,
            "precondition": "",
            "type_raw": type_raw,
            "value_raw": value_raw,
            "duration_raw": duration_raw,
            "evaluation_score": evaluation_score,
            "upgrade_cost": base_cost,
            "effects": effs,
            "activation_branches": branches,
        },
    }


def _build_fixture_catalog_and_knowledge(tmp: Path) -> tuple[dict, object]:
    from core.skill_knowledge import SkillKnowledgeLibrary

    rows = [
        _master_row(
            "cn-db:1001", "地固", skill_type="acceleration", rarity="normal", base_cost=120,
            trigger_code="phase==0&activate_count_start>=3", type_raw="加速度", value_raw="0.2",
            duration_raw="3.0", group_id=1001,
            condition_terms=[
                {"variable": "phase", "operator": "==", "value": 0},
                {"variable": "activate_count_start", "operator": ">=", "value": 3},
            ],
            effects=[{"kind": "acceleration", "value": 0.2, "target_type": 1}],
        ),
        _master_row(
            "cn-db:1002", "先手必胜", skill_type="acceleration", rarity="gold", base_cost=300,
            trigger_code="phase==0", type_raw="加速度", value_raw="0.4",
            duration_raw="1.2", group_id=1002, group_rate=2,
            relations={"lower": ["cn-db:1003"]}, chain=["cn-db:1003", "cn-db:1002"],
            condition_terms=[{"variable": "phase", "operator": "==", "value": 0}],
            effects=[{"kind": "acceleration", "value": 0.4, "target_type": 1}],
        ),
        _master_row(
            "cn-db:1003", "先驱", skill_type="acceleration", rarity="normal", base_cost=140,
            trigger_code="phase==0", type_raw="加速度", value_raw="0.2",
            duration_raw="1.2", group_id=1002, group_rate=1,
            relations={"upper": ["cn-db:1002"]}, chain=["cn-db:1003"],
            condition_terms=[{"variable": "phase", "operator": "==", "value": 0}],
            effects=[{"kind": "acceleration", "value": 0.2, "target_type": 1}],
        ),
        _master_row(
            "cn-db:1004", "专注力", skill_type="start", rarity="normal", base_cost=130,
            trigger_code="phase==0", type_raw="出闸时间缩短", value_raw="0.4",
            duration_raw="0", group_id=1004,
            condition_terms=[{"variable": "phase", "operator": "==", "value": 0}],
            effects=[{"kind": "start_delay_multiplier", "value": 0.4, "target_type": 1}],
        ),
        _master_row(
            "cn-db:1005", "泥地弯道○", skill_type="speed", rarity="normal", base_cost=120,
            trigger_code="ground_type==2&corner!=0", type_raw="速度", value_raw="0.15",
            duration_raw="3.0", group_id=1005,
            condition_terms=[
                {"variable": "ground_type", "operator": "==", "value": 2},
                {"variable": "corner", "operator": "!=", "value": 0},
            ],
            effects=[{"kind": "speed", "value": 0.15, "target_type": 1}],
        ),
        _master_row(
            "cn-db:1006", "泥地直线○", skill_type="speed", rarity="normal", base_cost=120,
            trigger_code="ground_type==2&is_last_straight==1", type_raw="速度", value_raw="0.15",
            duration_raw="3.0", group_id=1006,
            condition_terms=[
                {"variable": "ground_type", "operator": "==", "value": 2},
                {"variable": "is_last_straight", "operator": "==", "value": 1},
            ],
            effects=[{"kind": "speed", "value": 0.15, "target_type": 1}],
        ),
        _master_row(
            "cn-db:1007", "左回转○", skill_type="green", rarity="normal", base_cost=90,
            trigger_code="rotation==2", type_raw="被动(速度)", value_raw="40",
            duration_raw="0", group_id=1007,
            condition_terms=[{"variable": "rotation", "operator": "==", "value": 2}],
            effects=[{"kind": "passive_speed", "value": 40, "target_type": 1}],
        ),
        _master_row(
            "cn-db:1008", "沙地○", skill_type="green", rarity="normal", base_cost=90,
            trigger_code="ground_type==2", type_raw="被动(力量)", value_raw="40",
            duration_raw="0", group_id=1008,
            condition_terms=[{"variable": "ground_type", "operator": "==", "value": 2}],
            effects=[{"kind": "passive_power", "value": 40, "target_type": 1}],
        ),
        _master_row(
            "cn-db:1009", "非标准距离○", skill_type="green", rarity="normal", base_cost=90,
            trigger_code="is_basis_distance==0", type_raw="被动(耐力)", value_raw="40",
            duration_raw="0", group_id=1009,
            condition_terms=[{"variable": "is_basis_distance", "operator": "==", "value": 0}],
            effects=[{"kind": "passive_stamina", "value": 40, "target_type": 1}],
        ),
        _master_row(
            "cn-db:1010", "良场○", skill_type="green", rarity="normal", base_cost=90,
            trigger_code="ground_condition==1", type_raw="被动(力量)", value_raw="40",
            duration_raw="0", group_id=1010,
            condition_terms=[{"variable": "ground_condition", "operator": "==", "value": 1}],
            effects=[{"kind": "passive_power", "value": 40, "target_type": 1}],
        ),
        _master_row(
            "cn-db:1011", "圆弧的艺术家", skill_type="recovery", rarity="gold", base_cost=300,
            trigger_code="corner!=0", type_raw="体力回复", value_raw="0.055",
            duration_raw="0", group_id=1011,
            condition_terms=[{"variable": "corner", "operator": "!=", "value": 0}],
            effects=[{"kind": "stamina_recovery", "value": 0.055, "target_type": 1}],
        ),
        _master_row(
            "cn-db:1012", "加速群A", skill_type="acceleration", rarity="gold", base_cost=280,
            trigger_code="is_finalcorner==1", type_raw="加速度", value_raw="0.4",
            duration_raw="2.40", group_id=1012,
            condition_terms=[{"variable": "is_finalcorner", "operator": "==", "value": 1}],
            effects=[{"kind": "acceleration", "value": 0.4, "target_type": 1}],
        ),
        _master_row(
            "cn-db:1013", "加速群B", skill_type="acceleration", rarity="gold", base_cost=280,
            trigger_code="is_finalcorner==1", type_raw="加速度", value_raw="0.4",
            duration_raw="2.40", group_id=1013,
            condition_terms=[{"variable": "is_finalcorner", "operator": "==", "value": 1}],
            effects=[{"kind": "acceleration", "value": 0.4, "target_type": 1}],
        ),
        _master_row(
            "cn-db:1014", "尾盘之巅", skill_type="speed", rarity="normal", base_cost=140,
            trigger_code="is_finalcorner==1", type_raw="速度", value_raw="0.15",
            duration_raw="2.40", group_id=1014,
            condition_terms=[{"variable": "is_finalcorner", "operator": "==", "value": 1}],
            effects=[{"kind": "speed", "value": 0.15, "target_type": 1}],
        ),
        _master_row(
            "cn-db:1015", "未分级便宜白", skill_type="speed", rarity="normal", base_cost=100,
            trigger_code="ground_type==2&corner!=0", type_raw="速度", value_raw="0.15",
            duration_raw="2.40", group_id=1015,
            condition_terms=[
                {"variable": "ground_type", "operator": "==", "value": 2},
                {"variable": "corner", "operator": "!=", "value": 0},
            ],
            effects=[{"kind": "speed", "value": 0.15, "target_type": 1}],
        ),
        _master_row(
            "cn-db:1016", "避开技能", skill_type="speed", rarity="normal", base_cost=100,
            trigger_code="corner!=0", type_raw="速度", value_raw="0.15",
            duration_raw="2.40", group_id=1016,
            condition_terms=[{"variable": "corner", "operator": "!=", "value": 0}],
            effects=[{"kind": "speed", "value": 0.15, "target_type": 1}],
        ),
    ]
    catalog = {"schema_version": "uma.cn_larc_skill_library.v1", "skills": rows}
    master_path = tmp / "test_skill_master.json"
    master_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    knowledge = SkillKnowledgeLibrary.load(master_path)
    return catalog, knowledge


def _inv_cand(skill_id: str, display_cost: int, *, discount: int = 0) -> dict:
    return {
        "skill_id": skill_id,
        "display_cost": display_cost,
        "cost_basis": "observed_final",
        "observed_discount_percent": discount,
        "recognition_confidence": 0.99,
        "identity_binding": _valid_binding(skill_id.split(":")[-1]),
        "source_evidence": [{"source_page": "skills", "page_index": 0, "row_index": 0, "fragment_only": False}],
    }


def _base_profile(max_batch_size: int = 12) -> dict:
    return {
        "schema_version": "uma.skill_preference_profile.v1",
        "profile_id": "larc-race",
        "base_profile": "race",
        "reserve_skill_pt": 0,
        "filler_policy": "safe_useful_only",
        "max_batch_size": max_batch_size,
        "rules": [],
    }


def _worker_report(run_id: str, endpoint: str, before_pt: int, after_pt: int, actions: list[str], reason: str = "prerace_skills_learned") -> dict:
    return {
        "ok": True,
        "stage": "complete",
        "reason": reason,
        "run_session_id": run_id,
        "endpoint": endpoint,
        "before": {"current_skill_points": before_pt},
        "after": {"current_skill_points": after_pt},
        "remaining_skill_points": after_pt,
        "selected_action_ids": actions,
    }


class TestPhaseAOuterLoopAndTieredSkills(unittest.TestCase):
    def setUp(self) -> None:
        self._injected_cfg = _ensure_paths()
        self._prev_pack_env = os.environ.get("MAAUMA_STRATEGY_PACK_DIR")
        os.environ["MAAUMA_STRATEGY_PACK_DIR"] = "__NONE__"

    def tearDown(self) -> None:
        if self._prev_pack_env is None:
            os.environ.pop("MAAUMA_STRATEGY_PACK_DIR", None)
        else:
            os.environ["MAAUMA_STRATEGY_PACK_DIR"] = self._prev_pack_env
        for mod_name in self._injected_cfg:
            sys.modules.pop(mod_name, None)

    # --- Step 1: Kawasaki 2100m Dirt LoH preset & 12-runner ranks ---
    def test_step1_kawasaki_2100_dirt_loh_and_12_runner_ranks(self) -> None:
        from core.skill_planner import cm_skill_compiler as cm

        self.assertIn("kawasaki_2100_dirt_loh", cm.CM_TRACK_PRESETS)
        target, preset_id = cm.resolve_cm_target({"style": "front"}, "kawasaki_2100_dirt_loh")
        self.assertEqual(preset_id, "kawasaki_2100_dirt_loh")
        self.assertEqual(target["track_id"], 10103)
        self.assertEqual(target["distance_m"], 2100)
        self.assertEqual(target["distance_type"], "middle")
        self.assertEqual(target["surface"], "dirt")
        self.assertEqual(target["field_size"], 12)
        self.assertEqual(target["rotation"], 2)
        self.assertEqual(target["ground_condition"], 1)
        self.assertEqual(target["season"], 4)
        course = cm._course(target, preset_id)
        self.assertEqual(course.distance, 2100)
        self.assertEqual(course.preset_id, "kawasaki_2100_dirt_loh")
        last_sec = course.sections[-1]
        self.assertEqual(last_sec[2]["is_last_straight"], 1)
        self.assertEqual(last_sec[1] - last_sec[0], 300)
        self.assertTrue(any(sec[2]["straight_front_type"] == 2 for sec in course.sections))
        self.assertTrue(any(sec[2]["corner"] == 4 and sec[2]["is_finalcorner"] == 1 for sec in course.sections))

        # Verify 12-runner rank mapping vs 9-runner rank mapping for front style with order <= 5
        from core.skill_conditions import evaluate_condition
        atoms = [{"variable": "order", "operator": "<=", "value": 5}]
        q9 = cm._rank_quality(atoms, "front", evaluate_condition, field_size=9)
        q12 = cm._rank_quality(atoms, "front", evaluate_condition, field_size=12)
        self.assertAlmostEqual(q9, 0.45 + 0.55 * (4 / 5), places=4)
        self.assertAlmostEqual(q12, 0.45 + 0.55 * (4 / 6), places=4)
        self.assertNotAlmostEqual(q9, q12, places=3)

    # --- Step 2: Tiered ladder compiler + Global Optimizer end-to-end ---
    def test_step2_tiered_compiler_end_to_end_optimizer(self) -> None:
        from core.skill_planner import cm_skill_search, tiered_skill_compiler as tsc
        from core.skill_planner.candidate_set import build_skill_candidate_set
        from core.skill_planner.catalog import SkillCatalog
        from core.skill_planner.global_optimizer import optimize_final_skill_build
        from core.skill_planner.models import SkillPurchaseContext
        from core.skill_planner.preferences import parse_skill_preference_profile
        from core.auto_trainer.larc_target_skills import validate_target_plan

        with TemporaryDirectory() as td:
            tmp = Path(td)
            catalog_dict, knowledge = _build_fixture_catalog_and_knowledge(tmp)
            catalog = SkillCatalog.load(tmp / "test_skill_master.json")

            # Case A: Strict tier dominance T0 > T1 > T2 & within-tier discount_cost_first + optimizer check
            policy_a = {
                "enabled": True,
                "mode": "tiered_ladder",
                "preset_id": "kawasaki_2100_dirt_loh",
                "final_target": {"style": "front"},
                "ladder": {
                    "within_tier_order": "discount_cost_first",
                    "unlisted_policy": "skip",
                    "tiers": [
                        {"id": "T0", "skills": ["泥地弯道○", "泥地直线○"]},
                        {"id": "T1", "skills": ["尾盘之巅"]},
                    ],
                    "avoid": ["避开技能"],
                },
            }
            inventory_a = {
                "current_skill_points": 200,
                "owned_skill_ids": [],
                "candidates": [
                    _inv_cand("cn-db:1005", 120, discount=0),   # T0 expensive, no discount
                    _inv_cand("cn-db:1006", 84, discount=30),   # T0 cheaper + 30% discount
                    _inv_cand("cn-db:1014", 90, discount=35),   # T1
                    _inv_cand("cn-db:1015", 50, discount=50),   # unlisted -> skip
                    _inv_cand("cn-db:1016", 50, discount=50),   # avoid
                ],
            }
            compiled_a, diag_a = tsc.compile_tiered_skill_profile(
                _base_profile(), inventory_a, catalog_dict, policy=policy_a, year=3, terminal=True, knowledge=knowledge,
            )
            profile_a = parse_skill_preference_profile(compiled_a)
            self.assertTrue(cm_skill_search.is_cm_profile(profile_a))

            ctx_a = SkillPurchaseContext.from_dict({
                "schema_version": "uma_skill_purchase_context.v1",
                "run_session_id": "run_a",
                "turn_id": "turn_a",
                "target_mode": "race",
                "trigger": "terminal",
                "current_skill_pt": 200,
                "owned_skills": [],
                "policy": {"reserve_skill_pt": 0, "allow_gold_without_lower": True, "require_condition_match": False},
                "available_skills": inventory_a["candidates"],
                "race_profile": {"distance_type": "middle", "style": "front", "surface": "dirt"},
            })
            cset_a = build_skill_candidate_set(ctx_a, profile_sha256=profile_a.profile_sha256)
            plan_a = optimize_final_skill_build(ctx_a, catalog, profile_a, cset_a)
            # Only 1 of the two T0 must_buy_if_safe skills fits in 200 Pt -> status is partial, highest-bonus T0 (1006) + T1 (1014) selected
            self.assertIn(plan_a.status, {"ok", "partial"})
            picked_a = [p.skill_id for p in plan_a.purchases]
            # With 200 Pt: 1006 (84, T0 discounted) + 1014 (90, T1) = 174 <= 200 vs 1005 (120) + 1006 (84) = 204!
            # Hold on: both 1005 & 1006 are in T0! Does T0+T0=204 exceed 200 Pt, so it buys best T0 (1006, 84pt) + best T1 (1014, 90pt)?
            self.assertIn("cn-db:1006", picked_a)
            self.assertIn("cn-db:1014", picked_a)
            self.assertNotIn("cn-db:1005", picked_a)
            self.assertNotIn("cn-db:1015", picked_a)
            self.assertNotIn("cn-db:1016", picked_a)
            validate_target_plan(picked_a, diag_a, remaining_skill_points=plan_a.remaining_skill_pt)

            # Strict Tier Dominance: single T0 (cost 120) must beat T1 (60) + T2 (50) under 120 Pt budget
            policy_dom = {
                "enabled": True,
                "mode": "tiered_ladder",
                "preset_id": "kawasaki_2100_dirt_loh",
                "final_target": {"style": "front"},
                "ladder": {
                    "tiers": [
                        {"id": "T0", "skills": ["泥地弯道○"]},
                        {"id": "T1", "skills": ["尾盘之巅"]},
                        {"id": "T2", "skills": ["未分级便宜白"]},
                    ],
                },
            }
            inv_dom = {
                "current_skill_points": 120,
                "owned_skill_ids": [],
                "candidates": [
                    _inv_cand("cn-db:1005", 120, discount=0),
                    _inv_cand("cn-db:1014", 60, discount=50),
                    _inv_cand("cn-db:1015", 50, discount=50),
                ],
            }
            prof_dom, _ = tsc.compile_tiered_skill_profile(
                _base_profile(), inv_dom, catalog_dict, policy=policy_dom, year=3, terminal=True, knowledge=knowledge,
            )
            p_dom = parse_skill_preference_profile(prof_dom)
            ctx_dom = SkillPurchaseContext.from_dict({
                "schema_version": "uma_skill_purchase_context.v1",
                "run_session_id": "run_dom", "turn_id": "turn_dom", "target_mode": "race", "trigger": "terminal",
                "current_skill_pt": 120, "owned_skills": [],
                "policy": {"reserve_skill_pt": 0, "allow_gold_without_lower": True, "require_condition_match": False},
                "available_skills": inv_dom["candidates"],
                "race_profile": {"distance_type": "middle", "style": "front"},
            })
            plan_dom = optimize_final_skill_build(ctx_dom, catalog, p_dom, build_skill_candidate_set(ctx_dom, profile_sha256=p_dom.profile_sha256))
            self.assertEqual(plan_dom.status, "ok")
            self.assertEqual([p.skill_id for p in plan_dom.purchases], ["cn-db:1005"])

            # Case B: Within-tier discount_cost_first vs strict_order when only 1 skill in T0 can be bought
            inv_b = {
                "current_skill_points": 130,
                "owned_skill_ids": [],
                "candidates": [
                    _inv_cand("cn-db:1005", 120, discount=0),   # rank 0 in list, full price 120
                    _inv_cand("cn-db:1006", 84, discount=30),   # rank 1 in list, discounted 84
                ],
            }
            prof_disc, _ = tsc.compile_tiered_skill_profile(
                _base_profile(), inv_b, catalog_dict, policy=policy_a, year=3, terminal=True, knowledge=knowledge,
            )
            policy_strict = deepcopy(policy_a)
            policy_strict["ladder"]["within_tier_order"] = "strict_order"
            prof_strict, _ = tsc.compile_tiered_skill_profile(
                _base_profile(), inv_b, catalog_dict, policy=policy_strict, year=3, terminal=True, knowledge=knowledge,
            )
            p_disc = parse_skill_preference_profile(prof_disc)
            p_strict = parse_skill_preference_profile(prof_strict)
            ctx_b = SkillPurchaseContext.from_dict({
                "schema_version": "uma_skill_purchase_context.v1",
                "run_session_id": "run_b", "turn_id": "turn_b", "target_mode": "race", "trigger": "terminal",
                "current_skill_pt": 130, "owned_skills": [],
                "policy": {"reserve_skill_pt": 0, "allow_gold_without_lower": True, "require_condition_match": False},
                "available_skills": inv_b["candidates"],
                "race_profile": {"distance_type": "middle", "style": "front"},
            })
            plan_disc = optimize_final_skill_build(ctx_b, catalog, p_disc, build_skill_candidate_set(ctx_b, profile_sha256=p_disc.profile_sha256))
            plan_strict = optimize_final_skill_build(ctx_b, catalog, p_strict, build_skill_candidate_set(ctx_b, profile_sha256=p_strict.profile_sha256))
            self.assertEqual([p.skill_id for p in plan_disc.purchases], ["cn-db:1006"])
            self.assertEqual([p.skill_id for p in plan_strict.purchases], ["cn-db:1005"])

            # Case C: allow_lower_fallback + accel_group_caps + unlisted_policy=safe_cm_fallback
            policy_c = {
                "enabled": True,
                "mode": "tiered_ladder",
                "preset_id": "kawasaki_2100_dirt_loh",
                "final_target": {"style": "front"},
                "ladder": {
                    "allow_lower_fallback": True,
                    "unlisted_policy": "safe_cm_fallback",
                    "tiers": [
                        {"id": "T0", "skills": ["先手必胜", "加速群A", "加速群B"]},
                        {"id": "T1", "skills": ["尾盘之巅"]},
                    ],
                    "accel_group_caps": [
                        {"group_id": "sub_accel", "skills": ["加速群A", "加速群B"], "max_count": 1, "overflow_tier_shift": 1}
                    ],
                },
            }
            inv_c = {
                "current_skill_points": 600,
                "owned_skill_ids": [],
                "candidates": [
                    # 先手必胜 omitted/unaffordable; 1003 (先驱) should be fallback promoted
                    _inv_cand("cn-db:1003", 140, discount=0),
                    _inv_cand("cn-db:1012", 280, discount=0),  # same tier & cost as 1013
                    _inv_cand("cn-db:1013", 280, discount=20), # cheaper/discounted -> wins accel group cap!
                    _inv_cand("cn-db:1014", 140, discount=0),
                    _inv_cand("cn-db:1015", 100, discount=0),  # unlisted filler
                ],
            }
            comp_c, diag_c = tsc.compile_tiered_skill_profile(
                _base_profile(), inv_c, catalog_dict, policy=policy_c, year=3, terminal=True, knowledge=knowledge,
            )
            by_sid_c = {d["skill_id"]: d for d in diag_c["skills"]}
            self.assertEqual(by_sid_c["cn-db:1003"]["cm_reason"], "lower_fallback")
            self.assertEqual(by_sid_c["cn-db:1012"].get("accel_cap_shifted"), "sub_accel")
            # Under terminal + safe_cm_fallback: 1015 is pass-2 filler with prefer_weight <= 0
            self.assertEqual(comp_c["filler_policy"], "spend_all_safe")
            rule_1015 = next(r for r in comp_c["rules"] if r["selector"]["value"] == "cn-db:1015")
            self.assertEqual(rule_1015["mode"], "prefer")
            self.assertLessEqual(rule_1015["weight"], 0)
            p_c = parse_skill_preference_profile(comp_c)
            self.assertGreater(p_c.profile_sha256, "")

            # Case D: groundwork_gate excludes 地固 when <3 start sources, bundles when >=3 start sources
            policy_gw = {
                "enabled": True,
                "mode": "tiered_ladder",
                "preset_id": "kawasaki_2100_dirt_loh",
                "final_target": {"style": "front"},
                "ladder": {
                    "groundwork_gate": {"enabled": True, "min_sources": 3},
                    "tiers": [{"id": "T0", "skills": ["地固"]}],
                },
            }
            inv_gw_fail = {
                "current_skill_points": 600,
                "owned_skill_ids": [],
                "candidates": [
                    _inv_cand("cn-db:1001", 120),
                    _inv_cand("cn-db:1007", 90),  # only 2 green sources -> <3!
                    _inv_cand("cn-db:1008", 90),
                ],
            }
            _, diag_gw_fail = tsc.compile_tiered_skill_profile(
                _base_profile(), inv_gw_fail, catalog_dict, policy=policy_gw, year=3, terminal=True, knowledge=knowledge,
            )
            self.assertNotIn("cn-db:1001", diag_gw_fail["eligible_skill_ids"])

            inv_gw_ok = {
                "current_skill_points": 600,
                "owned_skill_ids": [],
                "candidates": [
                    _inv_cand("cn-db:1001", 120),
                    _inv_cand("cn-db:1007", 90),
                    _inv_cand("cn-db:1008", 90),
                    _inv_cand("cn-db:1010", 90),
                ],
            }
            _, diag_gw_ok = tsc.compile_tiered_skill_profile(
                _base_profile(), inv_gw_ok, catalog_dict, policy=policy_gw, year=3, terminal=True, knowledge=knowledge,
            )
            self.assertIn("cn-db:1001", diag_gw_ok["eligible_skill_ids"])
            self.assertIn("cn-db:1001", diag_gw_ok["groundwork_bundles"])
            self.assertEqual(len(diag_gw_ok["groundwork_bundles"]["cn-db:1001"]["required_skill_ids"]), 3)

            # Case E: stamina_gap_boost & green_skill_quota
            policy_boost = {
                "enabled": True,
                "mode": "tiered_ladder",
                "preset_id": "kawasaki_2100_dirt_loh",
                "final_target": {"style": "front"},
                "ladder": {
                    "stamina_gap_boost": {
                        "enabled": True, "min_stamina": 850, "use_hp_gate": False, "promote_skills": ["圆弧的艺术家"],
                    },
                    "green_skill_quota": {"enabled": True, "min_count": 2, "skills": ["左回转○", "沙地○"]},
                    "tiers": [
                        {"id": "T0", "skills": ["泥地弯道○"]},
                        {"id": "T1", "skills": ["圆弧的艺术家"]},
                    ],
                },
            }
            inv_boost = {
                "current_skill_points": 700,
                "owned_skill_ids": [],
                "candidates": [
                    _inv_cand("cn-db:1005", 120),
                    _inv_cand("cn-db:1011", 300),
                    _inv_cand("cn-db:1007", 90),
                    _inv_cand("cn-db:1008", 90),
                ],
            }
            _, diag_low = tsc.compile_tiered_skill_profile(
                _base_profile(), inv_boost, catalog_dict, policy=policy_boost,
                current_stats={"speed": 1500, "stamina": 500, "power": 1200, "guts": 900, "wisdom": 1100},
                year=3, terminal=True, knowledge=knowledge,
            )
            by_low = {d["skill_id"]: d for d in diag_low["skills"]}
            self.assertEqual(by_low["cn-db:1011"]["tier"], "T0")
            self.assertEqual(by_low["cn-db:1011"]["cm_reason"], "stamina_gap_boost")
            self.assertEqual(by_low["cn-db:1007"]["cm_reason"], "green_skill_quota")
            self.assertEqual(by_low["cn-db:1008"]["cm_reason"], "green_skill_quota")

            _, diag_high = tsc.compile_tiered_skill_profile(
                _base_profile(), inv_boost, catalog_dict, policy=policy_boost,
                current_stats={"speed": 1500, "stamina": 950, "power": 1200, "guts": 900, "wisdom": 1100},
                year=3, terminal=True, knowledge=knowledge,
            )
            by_high = {d["skill_id"]: d for d in diag_high["skills"]}
            self.assertEqual(by_high["cn-db:1011"]["tier"], "T1")

    # --- Step 3: Policy normalization, Arc stage gating, Worker skip guard & Pt ledger ---
    def test_step3_policy_normalization_arc_gating_and_pt_ledger(self) -> None:
        from core.auto_trainer import larc_target_skills as lts
        from scripts import larc_autonomy_arc_skills as arc_mod
        from scripts import larc_d1_prerace_skill_worker as prerace_worker

        # Backward compatibility + idempotency
        legacy = lts.normalize_arc_policy({})
        self.assertEqual(legacy["mode"], "auto_cm")
        self.assertNotIn("ladder", legacy)
        self.assertEqual(lts.normalize_arc_policy(legacy), legacy)

        tiered_raw = {
            "enabled": True,
            "mode": "tiered_ladder",
            "preset_id": "kawasaki_2100_dirt_loh",
            "final_target": {"style": "front"},
            "ladder": {"tiers": [{"id": "T0", "skills": ["泥地弯道○"]}]},
        }
        norm1 = lts.normalize_arc_policy(tiered_raw)
        norm2 = lts.normalize_arc_policy(norm1)
        self.assertEqual(norm1, norm2)
        self.assertEqual(norm1["mode"], "tiered_ladder")
        self.assertEqual(norm1["final_target"]["field_size"], 12)
        self.assertEqual(norm1["stage_policy"]["classic_arc"]["mode"], "stat_rescue")
        self.assertEqual(norm1["stage_policy"]["senior_arc"]["mode"], "t0_only")
        with self.assertRaises(ValueError):
            lts.normalize_arc_policy({"final_target": {"field_size": 10}})
        with self.assertRaises(ValueError):
            lts.normalize_arc_policy({"mode": "bad_mode"})

        # stage_skill_preference dispatch + Year-2/3 Arc gating + Pt ledger
        with TemporaryDirectory() as td:
            tmp = Path(td)
            catalog_dict, knowledge = _build_fixture_catalog_and_knowledge(tmp)
            ctx_pref = lts.target_context({"current_stats": {"stamina": 900}}, norm1)
            self.assertEqual(ctx_pref["final_target"]["field_size"], 12)
            prof, diag = lts.stage_skill_preference(
                _base_profile(),
                {"current_skill_points": 500, "owned_skill_ids": [], "candidates": [_inv_cand("cn-db:1005", 120)]},
                catalog_dict,
                context={**ctx_pref, "terminal_build": True},
                year=3,
                knowledge=knowledge,
            )
            self.assertEqual(diag["mode"], "tiered_ladder")
            self.assertTrue(prof["profile_id"].endswith("-arc-target-cm"))

            # Test summarize_run_skill_pt across mid, classic_arc, senior_arc, terminal (no double counting)
            run_root = tmp / "run_ledger"
            (run_root / "session").mkdir(parents=True)
            endpoint = "127.0.0.1:16448"
            (run_root / "progress.json").write_text(
                json.dumps({"run_session_id": "run_ledger", "device": endpoint, "next_turn": 40}), encoding="utf-8"
            )
            mid_wr = run_root / "support_skills_010/worker/worker_report.json"
            mid_wr.parent.mkdir(parents=True)
            mid_wr.write_text(json.dumps(_worker_report("run_ledger", endpoint, 400, 260, ["a1"])), encoding="utf-8")
            mid_nop = run_root / "support_skills_018/worker/worker_report.json"
            mid_nop.parent.mkdir(parents=True)
            mid_nop.write_text(json.dumps(_worker_report("run_ledger", endpoint, 260, 260, [], reason="no_suitable_skills")), encoding="utf-8")

            c_wr = run_root / "arc2/batch_01/worker_report.json"
            c_wr.parent.mkdir(parents=True)
            c_wr.write_text(json.dumps(_worker_report("run_ledger", endpoint, 500, 380, ["c1"])), encoding="utf-8")

            s_wr = run_root / "arc3/batch_01/worker_report.json"
            s_wr.parent.mkdir(parents=True)
            s_wr.write_text(json.dumps(_worker_report("run_ledger", endpoint, 700, 420, ["s1"])), encoding="utf-8")

            t_wr1 = run_root / "terminal/batch_001/worker_report.json"
            t_wr1.parent.mkdir(parents=True)
            t_wr1.write_text(json.dumps(_worker_report("run_ledger", endpoint, 620, 120, ["t1", "t2"])), encoding="utf-8")
            t_wr2 = run_root / "terminal/batch_002/worker_report.json"
            t_wr2.parent.mkdir(parents=True)
            t_wr2.write_text(json.dumps(_worker_report("run_ledger", endpoint, 120, 120, [], reason="no_purchase_return_verified")), encoding="utf-8")
            term_rep = run_root / "terminal/report.json"
            term_rep.write_text(json.dumps({
                "batches": [str(t_wr1), str(t_wr1), str(t_wr2)],  # duplicate path intentional
                "remaining_skill_points": 120,
            }), encoding="utf-8")

            (run_root / "session/larc_support_skill_startup.json").write_text(json.dumps({
                "run_session_id": "run_ledger",
                "last_checked_pt": 260,
                "purchase_history": [
                    {"turn_index": 10, "before_pt": 400, "after_pt": 260, "spent_pt": 140, "worker_report_path": str(mid_wr)},
                    {"turn_index": 10, "before_pt": 400, "after_pt": 260, "spent_pt": 140, "worker_report_path": str(mid_wr)},
                ],
            }), encoding="utf-8")
            (run_root / "session/larc_arc_skills.json").write_text(json.dumps({
                "schema_version": "larc_arc_skills.v1",
                "run_session_id": "run_ledger",
                "device": endpoint,
                "stages": {
                    "2": {"status": "complete", "batches": [str(c_wr), str(c_wr)], "remaining_skill_points": 380},
                    "3": {"status": "complete", "batches": [str(s_wr)], "remaining_skill_points": 420},
                },
            }), encoding="utf-8")
            (run_root / "session/larc_terminal_skills.json").write_text(json.dumps({
                "run_session_id": "run_ledger",
                "device": endpoint,
                "turn_index": 40,
                "status": "ready",
                "report_path": str(term_rep),
            }), encoding="utf-8")

            ledger = lts.summarize_run_skill_pt(run_root)
            self.assertEqual(ledger["mid"], 140)
            self.assertEqual(ledger["classic_arc"], 120)
            self.assertEqual(ledger["senior_arc"], 280)
            self.assertEqual(ledger["terminal"], 500)
            self.assertEqual(ledger["spent_pt_total"], 1040)
            self.assertEqual(ledger["current_pt"], 120)
            self.assertEqual(ledger["earned_pt_total"], 1160)

            # prepare_arc_skills: Year-2 stat_rescue skip when stats high vs run capped when low
            from core.auto_trainer.config import UGParentGoal

            y2_root = tmp / "run_y2"
            (y2_root / "session").mkdir(parents=True)
            goal_y2 = UGParentGoal.default().to_dict()
            goal_y2["scenario"] = "larc"
            goal_y2["skill_timing"]["arc_policy"] = {
                "enabled": True,
                "mode": "tiered_ladder",
                "preset_id": "kawasaki_2100_dirt_loh",
                "final_target": {"style": "front"},
                "stage_policy": {
                    "classic_arc": {"mode": "stat_rescue", "speed_threshold": 900, "stamina_threshold": 600, "max_skills": 2},
                    "senior_arc": {"mode": "t0_only", "max_skills": 3, "reserve_pt": 500},
                },
            }
            (y2_root / "goal.json").write_text(json.dumps(goal_y2), encoding="utf-8")
            (y2_root / "progress.json").write_text(json.dumps({
                "run_session_id": "run_y2", "device": endpoint, "next_turn": 42,
                "current_stats": {"speed": 950, "stamina": 650, "power": 800, "guts": 500, "wisdom": 700},
            }), encoding="utf-8")
            y2_ctx = SimpleNamespace(
                root=y2_root, run_id="run_y2", progress={"device": endpoint, "next_turn": 42,
                    "current_stats": {"speed": 950, "stamina": 650, "power": 800, "guts": 500, "wisdom": 700}},
                d1_skill_root=tmp / "d1", external_skill_db_path=tmp / "ext.json", timing_records=[],
            )
            race_ctx_y2 = {"status": "verified", "target_id": "larc_arc_participation",
                           "period": {"value": {"year_index": 2, "month": 10, "half": "first"}}}
            res_skip = arc_mod.prepare_arc_skills(y2_ctx, race_ctx_y2, None, y2_root / "arc2_skip")
            self.assertEqual(res_skip["status"], "skipped_stats_sufficient")

            # Low stamina -> triggers stat_rescue capped at max_skills=2
            y2_ctx.progress["current_stats"]["stamina"] = 520
            seen_reqs: list[dict] = []

            class FakeNav:
                def __init__(self, _ctx): pass
                def open(self, _src, _folder): return True
                def return_race_day(self, _folder, *, worker_returned=False): return "after.png"

            def fake_worker(_ctx, batch_dir, req):
                seen_reqs.append(dict(req))
                batch_dir.mkdir(parents=True, exist_ok=True)
                wr = {
                    **_worker_report("run_y2", endpoint, 700, 560, ["s1"]),
                    "turn_index": 42, "year": 2, "purpose": "prerace", "returned": True,
                }
                (batch_dir / "worker_report.json").write_text(json.dumps(wr), encoding="utf-8")
                return wr

            res_rescue = arc_mod.prepare_arc_skills(
                y2_ctx, race_ctx_y2, None, y2_root / "arc2_rescue",
                navigation_factory=FakeNav, worker=fake_worker,
            )
            self.assertEqual(res_rescue["status"], "complete")
            self.assertEqual(seen_reqs[0]["max_purchase_count"], 2)
            self.assertEqual(res_rescue["skill_pt_ledger"]["classic_arc"], 140)

            # Year-3 t0_only multi-batch cap & reserve_pt stop
            race_ctx_y3 = {"status": "verified", "target_id": "larc_final_arc_top5",
                           "period": {"value": {"year_index": 3, "month": 10, "half": "first"}}}
            y2_ctx.progress["next_turn"] = 66
            seen_y3: list[dict] = []

            def fake_y3_worker(_ctx, batch_dir, req):
                seen_y3.append(dict(req))
                batch_dir.mkdir(parents=True, exist_ok=True)
                # Batch 1 buys 2 skills leaving 600 Pt; Batch 2 sees max_purchase_count=1 and leaves 450 <= reserve_pt 500
                if len(seen_y3) == 1:
                    wr = {**_worker_report("run_y2", endpoint, 900, 600, ["t0_a", "t0_b"]),
                          "turn_index": 66, "year": 3, "purpose": "prerace", "returned": True}
                else:
                    wr = {**_worker_report("run_y2", endpoint, 600, 450, ["t0_c"]),
                          "turn_index": 66, "year": 3, "purpose": "prerace", "returned": True}
                (batch_dir / "worker_report.json").write_text(json.dumps(wr), encoding="utf-8")
                return wr

            res_y3 = arc_mod.prepare_arc_skills(
                y2_ctx, race_ctx_y3, None, y2_root / "arc3",
                navigation_factory=FakeNav, worker=fake_y3_worker,
            )
            self.assertEqual(res_y3["status"], "complete")
            self.assertEqual(len(seen_y3), 2)
            self.assertEqual(seen_y3[0]["max_purchase_count"], 3)
            self.assertEqual(seen_y3[1]["max_purchase_count"], 1)
            self.assertEqual(res_y3["skill_pt_ledger"]["senior_arc"], 450)

            # Verify apply_race_preference uses tiered_ladder even without classic_allowlist
            prep_in = y2_root / "prep_in"
            prep_in.mkdir(parents=True)
            manifest = {"items": []}
            (prep_in / "capture_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (prep_in / "inventory.json").write_text(json.dumps({
                "current_skill_points": 500, "owned_skill_ids": [], "candidates": [_inv_cand("cn-db:1005", 120)],
            }), encoding="utf-8")
            (prep_in / "planner_report.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
            planner_cat = tmp / "planner_cat.json"
            planner_cat.write_text(json.dumps(catalog_dict), encoding="utf-8")

            class FakeApi:
                @staticmethod
                def load_preference(path):
                    return json.loads(Path(path).read_text(encoding="utf-8"))

                @staticmethod
                def replan(**_kw):
                    return {"selected_action_ids": []}

                @staticmethod
                def publish(_output, _report, _manifest):
                    return {"batch_plan_path": str(y2_root / "prep_out/batch_plan.json")}

            with (
                patch.object(prerace_worker, "_target_policy", return_value=lts),
                patch.object(lts, "load_knowledge", return_value=knowledge),
            ):
                replan = prerace_worker.apply_race_preference(
                    FakeApi(),
                    {
                        "capture_manifest_path": str(prep_in / "capture_manifest.json"),
                        "inventory_path": str(prep_in / "inventory.json"),
                        "planner_report_path": str(prep_in / "planner_report.json"),
                    },
                    y2_root / "prep_out",
                    {
                        "turn_id": "t42", "target_mode": "race", "larc_year": 2, "terminal_build": False,
                        "planner_catalog": str(planner_cat), "external_skill_db_path": str(tmp / "ext.json"),
                        "policy": {"max_purchase_count": 2},
                        "current_stats": {"speed": 800, "stamina": 500},
                        "arc_policy": norm1,
                    },
                    None,
                )
            self.assertEqual(replan["target_diagnostic"]["mode"], "tiered_ladder")

            # Verify run_larc_terminal_skills treats tiered_ladder as cm_spenddown & attaches skill_pt_ledger
            from scripts import larc_autonomy_skills as term_mod

            term_root = tmp / "run_term"
            (term_root / "session").mkdir(parents=True)
            (term_root / "d1/data/skills/cn-20260916").mkdir(parents=True)
            (term_root / "goal.json").write_text(json.dumps(goal_y2), encoding="utf-8")
            (term_root / "progress.json").write_text(
                json.dumps({"run_session_id": "run_term", "device": endpoint, "next_turn": 72}), encoding="utf-8"
            )
            ext_db = tmp / "ext_db.json"
            ext_db.write_text("{}", encoding="utf-8")
            fake_tess = tmp / "tesseract.exe"
            fake_tess.write_bytes(b"")
            cat_dir = PACER_BACKEND / "data/skills/cn_larc_end"
            cat_dir_existed = cat_dir.exists()
            cat_dir.mkdir(parents=True, exist_ok=True)
            created_temp_cats: list[Path] = []
            for cname in ("cn_skill_resolver_catalog.json", "cn_skill_planner_catalog.json"):
                cpath = cat_dir / cname
                if not cpath.exists():
                    cpath.write_text(json.dumps(catalog_dict), encoding="utf-8")
                    created_temp_cats.append(cpath)
            try:
                calls: list[int] = []

                def fake_run_controlled(argv, **_kw):
                    calls.append(len(calls) + 1)
                    bdir = Path(argv[argv.index("--output-dir") + 1])
                    bdir.mkdir(parents=True, exist_ok=True)
                    proof = bdir / "return_proof.json"
                    proof.write_text("{}", encoding="utf-8")
                    turn_id = argv[argv.index("--turn-id") + 1]
                    if len(calls) == 1:
                        # Leaves 10 Pt (< 50): v0.6.18+ stops immediately with small_remainder_retained
                        wr = {
                            "ok": True, "stage": "complete", "reason": "race_skill_learning_verified",
                            "run_session_id": "run_term", "endpoint": endpoint, "turn_id": turn_id,
                            "output_dir": str(bdir), "return_proof_path": str(proof),
                            "before": {"current_skill_points": 110}, "after": {"current_skill_points": 10},
                            "remaining_skill_points": 10, "selected_action_ids": ["x1"],
                            "finish_identity": {"id": "1"},
                        }
                    else:
                        wr = {
                            "ok": True, "stage": "complete", "reason": "no_purchase_return_verified",
                            "run_session_id": "run_term", "endpoint": endpoint, "turn_id": turn_id,
                            "output_dir": str(bdir), "return_proof_path": str(proof),
                            "before": {"current_skill_points": 10}, "after": {"current_skill_points": 10},
                            "remaining_skill_points": 10, "selected_action_ids": [],
                            "finish_identity": {"id": "1"},
                        }
                    (bdir / "worker_report.json").write_text(json.dumps(wr), encoding="utf-8")
                    return SimpleNamespace(returncode=0, stdout="", stderr="")

                with (
                    patch.object(term_mod, "resolve_tesseract_runtime", return_value=SimpleNamespace(executable=fake_tess)),
                    patch.object(term_mod, "run_controlled_process", side_effect=fake_run_controlled),
                ):
                    t_report, t_code = term_mod.run_larc_terminal_skills(
                        run_root=term_root,
                        output_dir=term_root / "term_out",
                        d1_root=term_root / "d1",
                        external_skill_db_path=ext_db,
                        adb_port=16448,
                    )
                self.assertEqual(t_code, 0)
                self.assertEqual(len(calls), 1)
                self.assertEqual(t_report["ready_reason"], "small_remainder_retained")
                self.assertEqual(t_report["skill_pt_ledger"]["terminal"], 100)
                self.assertEqual(t_report["skill_pt_ledger"]["current_pt"], 10)
            finally:
                for p in created_temp_cats:
                    p.unlink(missing_ok=True)
                if not cat_dir_existed:
                    for d in (cat_dir, cat_dir.parent, cat_dir.parent.parent):
                        try:
                            d.rmdir()
                        except OSError:
                            break

    # --- Step 4: Attempt-1 active-career takeover & Surface aptitude OCR + Graduation ---
    def test_step4_attempt1_takeover_and_surface_graduation(self) -> None:
        from core.auto_trainer import ace_graduation as ag
        from core.auto_trainer.cut_farming import CutFarmingConfig, CutSession
        from scripts.career_sieve_cycle import SieveCycle
        from scripts.larc_continuation_inspection import (
            aptitude_tokens, read_aptitudes, read_surface_aptitudes, surface_aptitude_tokens,
        )

        # Part A: Surface aptitude OCR
        tokens = [
            {"text": "短距离A", "bbox": [10, 100, 90, 125]},
            {"text": "中距离S", "bbox": [10, 140, 90, 165]},
            {"text": "草地", "bbox": [10, 200, 70, 225]},
            {"text": "泥地A", "bbox": [10, 240, 80, 265]},
        ]
        # Verify distance parsers untouched & surface parsers handle both joined & crop fallback
        self.assertEqual(aptitude_tokens(tokens), {"short": "A", "medium": "S"})
        self.assertEqual(surface_aptitude_tokens(tokens), {"dirt": "A"})

        class FakeRankReader:
            def read_line(self, _crop):
                return ("B", 0.99)

        img = np.zeros((1280, 720, 3), dtype=np.uint8)
        self.assertEqual(read_aptitudes(img, tokens, rank_reader=FakeRankReader()), {"short": "A", "medium": "S"})
        self.assertEqual(read_surface_aptitudes(img, tokens, rank_reader=FakeRankReader()), {"dirt": "A", "turf": "B"})

        # Part B: ace_graduation surface validation, inspect & fail-closed evaluate
        norm = ag.normalize_ace_cycle({
            "target_graduates": 1,
            "graduation": {"enabled": True, "surfaces": {"dirt": "A"}},
        })
        self.assertEqual(norm["graduation"]["surfaces"], {"dirt": "A"})
        with self.assertRaises(ValueError):
            ag.normalize_ace_cycle({"graduation": {"enabled": True, "surfaces": {"sand": "A"}}})

        with TemporaryDirectory() as td:
            run_root = Path(td) / "ace_run"
            (run_root / "session").mkdir(parents=True)
            (run_root / "session/ace_final_facts.json").write_text(json.dumps({
                "run_session_id": "ace_run",
                "stats": {"speed": 1500, "stamina": 1000, "power": 1300, "guts": 900, "wisdom": 1100},
                "distances": {"middle": "S"},
                "surfaces": {"dirt": "A", "turf": "B"},
            }), encoding="utf-8")
            facts = ag.inspect_completed_career(run_root)
            self.assertEqual(facts["surfaces"], {"dirt": "A", "turf": "B"})
            self.assertEqual(ag.evaluate_graduation(norm, facts)["status"], "graduated")
            self.assertEqual(
                ag.evaluate_graduation({"graduation": {"enabled": True, "surfaces": {"dirt": "S"}}}, facts)["status"],
                "unqualified",
            )
            self.assertEqual(
                ag.evaluate_graduation({"graduation": {"enabled": True, "surfaces": {"dirt": "A"}}}, {**facts, "surfaces": {}})["status"],
                "unknown",
            )

        # Part C: SieveCycle Attempt-1 active-career takeover vs Attempt-2 strict ordinary start
        with TemporaryDirectory() as td:
            batch_root = Path(td) / "batch"
            batch_root.mkdir(parents=True)
            cfg = CutFarmingConfig(character_id="100101", device_port=16448, max_attempts=3)
            sess = CutSession(batch_root, cfg)
            sess.data["ss_sieve_cycle"] = {
                "phase": "ready",
                "career_options": {"setup": {"character_name": "测试马", "character_id": "100101"}},
            }
            sess.save()
            ctrl = SimpleNamespace(checkpoint=lambda *a, **k: None)

            def _make_attempt_dir(name: str) -> Path:
                r = batch_root / name
                (r / "session").mkdir(parents=True, exist_ok=True)
                (r / "progress.json").write_text(json.dumps({"run_session_id": name, "device": "127.0.0.1:16448", "next_turn": 1}), encoding="utf-8")
                return r

            # Subcase C1: Attempt 1 via takeover_observer sees main_menu -> takes over without ordinary_opening_owner
            sess.begin_attempt(_make_attempt_dir("att1"))
            cycle1 = SieveCycle(
                sess, ctrl,
                opening=lambda *a, **k: self.fail("opener must not run when takeover_observer finds active career"),
                takeover_observer=lambda _ctx, _out: {"owner": "common", "state": "main_menu"},
            )
            r1 = cycle1._open(batch_root / "att1", SimpleNamespace(d1_skill_root=None))
            self.assertTrue(r1["ok"])
            self.assertEqual(r1["reason"], "existing_career_takeover_attempt1")
            self.assertFalse(r1["new_career_started"])
            self.assertFalse((batch_root / "att1/session/ordinary_opening_owner.json").exists())

            # Attempt 2 must NOT use takeover_observer and requires strict ordinary_start_confirmed
            sess.begin_attempt(_make_attempt_dir("att2"))
            cycle1.opening = lambda *a, **k: {"ok": True, "reason": "existing_career_handoff"}
            r2_bad = cycle1._open(batch_root / "att2", SimpleNamespace(d1_skill_root=None))
            self.assertFalse(r2_bad["ok"])

            # Subcase C2: Attempt 1 via existing_career_handoff from opener -> accepted as takeover
            b2 = Path(td) / "batch2"
            b2.mkdir(exist_ok=True)
            sess2 = CutSession(b2, cfg)
            sess2.data["ss_sieve_cycle"] = sess.data["ss_sieve_cycle"]
            sess2.save()
            att_h = Path(td) / "batch2/att_h"
            (att_h / "session").mkdir(parents=True)
            (att_h / "progress.json").write_text(json.dumps({"run_session_id": "att_h", "device": "127.0.0.1:16448", "next_turn": 1}), encoding="utf-8")
            sess2.begin_attempt(att_h)
            cycle2 = SieveCycle(
                sess2, ctrl,
                opening=lambda *a, **k: {"ok": True, "reason": "existing_career_handoff"},
            )
            r_handoff = cycle2._open(att_h, SimpleNamespace(d1_skill_root=None))
            self.assertTrue(r_handoff["ok"])
            self.assertFalse(r_handoff["new_career_started"])
            self.assertFalse((att_h / "session/ordinary_opening_owner.json").exists())

    # --- Step 5: Per-date overrides & α-star* real catalog speed_effect ---
    def test_step5_mid_skill_date_overrides_and_alpha_star_speed_effect(self) -> None:
        from core.auto_trainer.larc_support_skill_startup import (
            due_reasons, execution_options, normalize_mid_skill_learning, speed_effect,
        )

        # Per-date override normalization & resolution
        mid_cfg = normalize_mid_skill_learning({
            "enabled": True,
            "mode": "allowlist_v1",
            "interval_enabled": False,
            "min_discount": 30.0,
            "max_cost": 180,
            "spend_percent": 60.0,
            "dates": [
                {"year_index": 2, "month": 1, "half": "second"},
                {"year_index": 2, "month": 6, "half": "second", "min_discount": 20.0, "max_cost": 260, "spend_percent": 100.0},
            ],
        })
        # Legacy date preserves only 3 keys; override date keeps override keys
        self.assertEqual(set(mid_cfg["dates"][0].keys()), {"year_index", "month", "half"})
        self.assertEqual(mid_cfg["dates"][1]["min_discount"], 20.0)
        self.assertEqual(mid_cfg["dates"][1]["max_cost"], 260)

        timing = SimpleNamespace(
            early_options={},
            mid_skill_learning=mid_cfg,
            min_pt=100,
            interval_turns=6,
            early_skill_allowlist=["地固"],
        )
        reasons = due_reasons(
            {"run_session_id": "r1"}, {"enabled": True, "mode": "allowlist_v1"},
            run_id="r1", turn=35, points=400, timing=timing,
            period={"year_index": 2, "month": 6, "half": "second"},
        )
        self.assertEqual(reasons, ["date:2-06-second"])

        opts_default = execution_options(timing, period={"year_index": 2, "month": 1, "half": "second"})
        self.assertEqual(opts_default["min_discount"], 30.0)
        self.assertEqual(opts_default["max_cost"], 180)
        self.assertAlmostEqual(opts_default["spend_fraction"], 0.6)

        opts_t35 = execution_options(timing, period={"year_index": 2, "month": 6, "half": "second"})
        self.assertEqual(opts_t35["min_discount"], 20.0)
        self.assertEqual(opts_t35["max_cost"], 260)
        self.assertAlmostEqual(opts_t35["spend_fraction"], 1.0)

        # Real cn_skill_master.json α-star* / cn-db:910461 + skills-rich.json check
        master_path = REPO_ROOT / "backend/data/skills/cn-20260916/cn_skill_master.json"
        master_data = json.loads(master_path.read_text(encoding="utf-8-sig"))
        rows = list(master_data.get("skills", {}).values()) if isinstance(master_data.get("skills"), dict) else master_data.get("skills", [])
        alpha_rows = [r for r in rows if r.get("skill_id") in {"cn:α-star*", "cn-db:910461"}]
        self.assertEqual(len(alpha_rows), 2)
        for row in alpha_rows:
            self.assertTrue(speed_effect(row), f"Expected speed_effect True for real row {row.get('skill_id')}")

        rich_path = REPO_ROOT / "knowledge-tools/data/knowledge/skills-rich.json"
        if rich_path.is_file():
            rich_data = json.loads(rich_path.read_text(encoding="utf-8-sig"))
            rich_rows = rich_data if isinstance(rich_data, list) else rich_data.get("skills", [])
            rich_alpha = [r for r in rich_rows if r.get("skill_id") == "cn:α-star*" or "α-star" in str(r.get("name_zh_cn", ""))]
            self.assertTrue(rich_alpha)
            for row in rich_alpha:
                self.assertTrue(speed_effect(row), f"Expected speed_effect True for skills-rich row {row.get('skill_id')}")

        # Negative checks must stay False
        self.assertIs(speed_effect({"type": "recovery", "type_raw": "持久力恢复", "effects": []}), False)
        self.assertIs(speed_effect({"type": "green", "type_raw": "被动（速度）", "effects": [{"kind": "speed", "type_raw": "被动", "value": 40}]}), False)
        self.assertIs(speed_effect({"type": "debuff", "type_raw": "目标速度降低", "effects": [{"kind": "target_speed", "type_raw": "目标速度降低", "value": -0.25}]}), False)

    # --- Step 6: Phase B Strategy Pack SPI tiered presets, stat greens, recovery gate, reliable groundwork & multi-evolution ---
    def test_step6_synthetic_pack_tiered_presets_and_multi_evolution(self) -> None:
        import zipfile
        import numpy as np
        from core.auto_trainer.advance.planner import SafeAdvancePlanner
        from core.auto_trainer.larc_strategy_extension import reload_extensions
        from core.auto_trainer.larc_target_skills import normalize_arc_policy
        from core.skill_conditions import evaluate_condition
        from core.skill_knowledge import SkillKnowledgeLibrary, race_inputs
        from core.skill_planner import (
            cm_skill_compiler as cm,
            cm_skill_search,
            tiered_ladder_presets as tlp,
            tiered_skill_compiler as tsc,
        )
        from core.skill_planner.candidate_set import build_skill_candidate_set
        from core.skill_planner.catalog import SkillCatalog
        from core.skill_planner.global_optimizer import optimize_final_skill_build
        from core.skill_planner.models import SkillPurchaseContext
        from core.skill_planner.preferences import parse_skill_preference_profile

        def _find_cn_larc_end_dir(tmp_dir: str) -> Path:
            for cand_dir in (
                PACER_BACKEND / "data/skills/cn_larc_end",
                REPO_ROOT / "backend/data/skills/cn_larc_end",
                REPO_ROOT.parent / "checks/merged-pacer-backend/data/skills/cn_larc_end",
            ):
                if (cand_dir / "cn_skill_master.json").is_file() and (cand_dir / "cn_skill_planner_catalog.json").is_file():
                    return cand_dir
            zip_path = REPO_ROOT.parents[1] / "maauma-release-066-unified/release/archives/maauma-base-0.6.6-windows-x64.zip"
            self.assertTrue(zip_path.is_file(), f"Missing cn_larc_end catalog files and archive fallback: {zip_path}")
            out_dir = Path(tmp_dir) / "cn_larc_end"
            out_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zip_path) as zf:
                for fn in ("cn_skill_master.json", "cn_skill_planner_catalog.json"):
                    (out_dir / fn).write_bytes(zf.read(f"pacer/backend/data/skills/cn_larc_end/{fn}"))
            return out_dir

        prev_pack_env = os.environ.get("MAAUMA_STRATEGY_PACK_DIR")
        try:
            with TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                larc_dir = _find_cn_larc_end_dir(tmp_dir)
                master_path = larc_dir / "cn_skill_master.json"
                catalog_path = larc_dir / "cn_skill_planner_catalog.json"
                norm_catalog_dict = json.loads(catalog_path.read_text(encoding="utf-8-sig"))
                rows_list = norm_catalog_dict.get("skills", [])
                knowledge = SkillKnowledgeLibrary.load(master_path)
                catalog = SkillCatalog.load(catalog_path)
                by_name = {
                    r["name_zh_cn"]: r
                    for r in rows_list
                    if r.get("name_zh_cn") and r.get("base_cost") and r.get("rarity") != "unique"
                }

                # 0. Public base alone must have 0 built-in skill ladder presets
                os.environ["MAAUMA_STRATEGY_PACK_DIR"] = "__NONE__"
                reload_extensions()
                self.assertEqual(tlp.list_tiered_ladder_presets(), [])
                with self.assertRaises(ValueError):
                    tlp.resolve_tiered_ladder_preset("synth_combo_ladder")

                # Stage a synthetic external strategy pack in TemporaryDirectory
                synth_pack = tmp_path / "synth_ladder_pack"
                ladders_dir = synth_pack / "skill_ladders"
                ladders_dir.mkdir(parents=True, exist_ok=True)
                combo_policy = {
                    "preset_id": "kawasaki_2100_dirt_loh",
                    "final_target": {"style": "leader"},
                    "mode": "tiered_ladder",
                    "ladder": {
                        "tiers": [
                            {"id": "T0", "label": "Core", "skills": ["热度飙升！", "逆时针○", "冬季优俊少女○", "抢先", "拔得头筹", "磐石之势", "擅长迈步"]},
                            {"id": "T1", "label": "Mid", "skills": ["中距离弯道○", "急弯○"]},
                            {"id": "T2", "label": "Extra", "skills": ["逆时针◎"]},
                            {"id": "T3", "label": "Fill", "skills": ["中期能手"]},
                        ],
                        "avoid": ["目标中心位！", "干劲十足"],
                        "unlisted_policy": "skip",
                        "within_tier_order": "discount_cost_first",
                        "allow_lower_fallback": True,
                        "groundwork_gate": {"enabled": True, "min_sources": 3, "reliable_active_pair": True},
                        "stamina_gap_boost": {
                            "enabled": True,
                            "min_stamina": 1150,
                            "use_hp_gate": True,
                            "promote_skills": ["直线恢复", "弯道恢复○", "喘口气", "圆弧艺术家"],
                        },
                        "combo_promotions": [
                            {
                                "combo_id": "test_combo_777_xmas",
                                "require_all": ["777", "圣诞夜的奇迹飞驰！"],
                                "promote_skills": ["777", "圣诞夜的奇迹飞驰！"],
                                "target_tier": "T1",
                            }
                        ],
                    },
                    "stage_policy": {
                        "classic_arc": {"mode": "stat_rescue", "allow_recovery": False},
                        "senior_arc": {"mode": "t0_only", "allow_recovery": False},
                        "terminal": {"mode": "full_ladder"},
                    },
                    "evolution_policy": {"enabled": True, "default_branch": "original", "overrides": {}},
                }
                green_policy = {
                    "preset_id": "kawasaki_2100_dirt_loh",
                    "final_target": {"style": "leader"},
                    "mode": "tiered_ladder",
                    "ladder": {
                        "tiers": [
                            {"id": "T0", "label": "Core", "skills": ["胜利的机运", "超群的迈步", "逆时针○", "冬季优俊少女○"]},
                            {"id": "T1", "label": "Mid", "skills": ["急弯○", "自信十足"]},
                        ],
                        "unlisted_policy": "skip",
                        "green_skill_quota": {
                            "enabled": True,
                            "min_count": 6,
                            "skills": [
                                "超群的迈步", "擅长迈步", "逆时针○", "冬季优俊少女○",
                                "急弯○", "自信十足", "交流重赏○", "晴天○",
                            ],
                            "last_resort_skills": ["良场地○"],
                        },
                    },
                    "evolution_policy": {"enabled": True, "default_branch": "original", "overrides": {}},
                }
                (ladders_dir / "combo.json").write_text(json.dumps(combo_policy, ensure_ascii=False), encoding="utf-8")
                (synth_pack / "pack.py").write_text(
                    "def register_strategy_pack(registry, *, manifest=None, pack_dir=None):\n    return None\n",
                    encoding="utf-8",
                )
                (synth_pack / "manifest.json").write_text(
                    json.dumps(
                        {
                            "schema_version": "maauma.strategy_pack.v1",
                            "pack_id": "synth_ladder_pack",
                            "display_name": "Synthetic Tiered Ladder Pack",
                            "version": "1.0.0",
                            "entry": "pack.py",
                            "skill_ladder_presets": [
                                {"id": "synth_combo_ladder", "name": "Synth Combo", "summary": "Test", "file": "skill_ladders/combo.json"},
                                {"id": "synth_green_quota_ladder", "name": "Synth Green", "summary": "Test", "policy": green_policy},
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                os.environ["MAAUMA_STRATEGY_PACK_DIR"] = str(synth_pack)
                reload_extensions()

                # 1. Verify synthetic pack presets normalize idempotently & ZERO skills fail resolution in cn_larc_end
                self.assertEqual(
                    [p["id"] for p in tlp.list_tiered_ladder_presets()],
                    ["synth_combo_ladder", "synth_green_quota_ladder"],
                )
                for preset_key in ("synth_combo_ladder", "synth_green_quota_ladder"):
                    pol = normalize_arc_policy({"enabled": True, "ladder_preset_id": preset_key})
                    self.assertEqual(normalize_arc_policy(pol), pol)
                    self.assertEqual(pol["preset_id"], "kawasaki_2100_dirt_loh")
                    self.assertEqual(pol["final_target"]["season"], 4)
                    self.assertTrue(pol["evolution_policy"]["enabled"])

                    compiled, diag = tsc.compile_tiered_skill_profile(
                        _base_profile(24),
                        {"current_skill_points": 2000, "owned_skill_ids": [], "candidates": []},
                        norm_catalog_dict,
                        policy=pol,
                        year=3,
                        terminal=True,
                        knowledge=knowledge,
                    )
                    self.assertTrue(cm_skill_search.is_cm_profile(parse_skill_preference_profile(compiled)))
                    for tier_res in diag["ladder_resolution"]["tiers"]:
                        unresolved = [r for r in tier_res["resolution"] if r["status"] != "resolved"]
                        self.assertEqual(unresolved, [], f"Unresolved tier skills in {preset_key}/{tier_res['id']}: {unresolved}")
                    unresolved_avoid = [r for r in diag["ladder_resolution"]["avoid"] if r["status"] != "resolved"]
                    self.assertEqual(unresolved_avoid, [], f"Unresolved avoid skills in {preset_key}: {unresolved_avoid}")

                def _real_cand(name: str, cost: int | None = None, discount: int = 0) -> dict:
                    row = by_name[name]
                    sid = row["skill_id"]
                    dcost = cost if cost is not None else int(row["base_cost"])
                    return _inv_cand(sid, dcost, discount=discount)

                # 2. Verify Kawasaki static & stat-aware passive greens at high vs low stats
                high_stats = {"speed": 1500, "stamina": 1200, "power": 1200, "guts": 1050, "wisdom": 1250}
                low_stats = {"speed": 900, "stamina": 900, "power": 900, "guts": 700, "wisdom": 800}
                kawa_target, _ = cm.resolve_cm_target({"style": "leader"}, "kawasaki_2100_dirt_loh")
                kawa_inputs_hi = cm._enrich_race_inputs(race_inputs(kawa_target), kawa_target, current_stats=high_stats)
                kawa_inputs_lo = cm._enrich_race_inputs(race_inputs(kawa_target), kawa_target, current_stats=low_stats)
                kawa_course = cm._course(kawa_target, "kawasaki_2100_dirt_loh")
                kawa_gate_hi = cm.stamina_recovery_gate(kawa_target, high_stats)
                kawa_gate_lo = cm.stamina_recovery_gate(kawa_target, low_stats)
                for green_name in (
                    "冬季优俊少女○", "交流重赏○", "非标准距离○", "逆时针○", "川崎赛场○", "急弯○",
                    "擅长迈步", "超群的迈步", "自信十足",
                ):
                    rm = cm._resolve_master(knowledge, by_name[green_name])
                    rating, exc = cm._rating(rm, kawa_target, kawa_course, kawa_gate_hi, kawa_inputs_hi, evaluate_condition)
                    self.assertIsNone(exc, f"Expected no exclusion for {green_name}")
                    self.assertEqual(rating.reason, "matched_green", f"Expected matched_green for {green_name}")
                    self.assertTrue(rating.start_source, f"Expected start_source for {green_name}")

                for stat_green in ("擅长迈步", "自信十足"):
                    rm = cm._resolve_master(knowledge, by_name[stat_green])
                    rating_lo, exc_lo = cm._rating(rm, kawa_target, kawa_course, kawa_gate_lo, kawa_inputs_lo, evaluate_condition)
                    self.assertIsNone(exc_lo)
                    self.assertEqual(rating_lo.reason, "conditional_green", f"Expected conditional_green at low stats for {stat_green}")
                    self.assertFalse(rating_lo.start_source, f"Expected start_source=False at low stats for {stat_green}")

                # 3. Avoid, combo_promotions, allow_recovery=False before terminal & stamina_gap_boost priority
                pol_combo = normalize_arc_policy({"enabled": True, "ladder_preset_id": "synth_combo_ladder"})
                cands_with_combo = [
                    _real_cand("目标中心位！", 200),
                    _real_cand("干劲十足", 100),
                    _real_cand("777", 120, discount=25),
                    _real_cand("圣诞夜的奇迹飞驰！", 160, discount=20),
                    _real_cand("热度飙升！", 260, discount=25),
                    _real_cand("逆时针○", 70, discount=20),
                    _real_cand("擅长迈步", 100),
                    _real_cand("中期能手", 120, discount=20),
                ]
                comp_f1, diag_f1 = tsc.compile_tiered_skill_profile(
                    _base_profile(24),
                    {"current_skill_points": 750, "owned_skill_ids": [], "candidates": cands_with_combo},
                    norm_catalog_dict,
                    policy=pol_combo,
                    current_stats=high_stats,
                    year=3,
                    terminal=True,
                    knowledge=knowledge,
                )
                prof_f1 = parse_skill_preference_profile(comp_f1)
                ctx_f1 = SkillPurchaseContext.from_dict({
                    "schema_version": "uma_skill_purchase_context.v1",
                    "run_session_id": "run_f1",
                    "turn_id": "turn_f1",
                    "target_mode": "race",
                    "trigger": "terminal",
                    "current_skill_pt": 750,
                    "owned_skills": [],
                    "policy": {"reserve_skill_pt": 0, "allow_gold_without_lower": True, "require_condition_match": False},
                    "available_skills": cands_with_combo,
                    "race_profile": kawa_target,
                })
                plan_f1 = optimize_final_skill_build(ctx_f1, catalog, prof_f1, build_skill_candidate_set(ctx_f1, profile_sha256=prof_f1.profile_sha256))
                picked_f1 = {p.skill_id for p in plan_f1.purchases}
                self.assertNotIn(by_name["目标中心位！"]["skill_id"], picked_f1)
                self.assertNotIn(by_name["干劲十足"]["skill_id"], picked_f1)
                self.assertIn(by_name["777"]["skill_id"], picked_f1)
                self.assertIn(by_name["圣诞夜的奇迹飞驰！"]["skill_id"], picked_f1)
                self.assertIn(by_name["热度飙升！"]["skill_id"], picked_f1)
                self.assertIn(by_name["逆时针○"]["skill_id"], picked_f1)
                self.assertNotIn(by_name["中期能手"]["skill_id"], picked_f1)
                dec_f1 = {d["skill_id"]: d for d in diag_f1["skills"]}
                self.assertEqual(dec_f1[by_name["热度飙升！"]["skill_id"]]["tier"], "T0")
                self.assertEqual(dec_f1[by_name["擅长迈步"]["skill_id"]]["tier"], "T0")
                self.assertEqual(dec_f1[by_name["777"]["skill_id"]]["tier"], "T1")
                self.assertEqual(dec_f1[by_name["圣诞夜的奇迹飞驰！"]["skill_id"]]["tier"], "T1")

                # Without 777, 圣诞夜的奇迹飞驰！ stays ineligible while 热度飙升！ remains in T0
                cands_no_777 = [c for c in cands_with_combo if c["skill_id"] != by_name["777"]["skill_id"]]
                _comp_f2, diag_f2 = tsc.compile_tiered_skill_profile(
                    _base_profile(24),
                    {"current_skill_points": 750, "owned_skill_ids": [], "candidates": cands_no_777},
                    norm_catalog_dict,
                    policy=pol_combo,
                    current_stats=high_stats,
                    year=3,
                    terminal=True,
                    knowledge=knowledge,
                )
                self.assertNotIn(by_name["圣诞夜的奇迹飞驰！"]["skill_id"], diag_f2["eligible_skill_ids"])
                self.assertEqual({d["skill_id"]: d for d in diag_f2["skills"]}[by_name["热度飙升！"]["skill_id"]]["tier"], "T0")

                # Pre-terminal senior check: block all recovery skills even when stamina is low
                cands_pre = [
                    _real_cand("热度飙升！", 260, discount=25),
                    _real_cand("直线恢复", 140),
                    _real_cand("777", 120, discount=25),
                    _real_cand("圣诞夜的奇迹飞驰！", 160, discount=20),
                ]
                _comp_pre, diag_pre = tsc.compile_tiered_skill_profile(
                    _base_profile(24),
                    {"current_skill_points": 1200, "owned_skill_ids": [], "candidates": cands_pre},
                    norm_catalog_dict,
                    policy=pol_combo,
                    current_stats={"speed": 1400, "stamina": 900, "power": 1100, "guts": 900, "wisdom": 1100},
                    year=3,
                    terminal=False,
                    knowledge=knowledge,
                )
                self.assertIn(by_name["热度飙升！"]["skill_id"], diag_pre["eligible_skill_ids"])
                dec_pre = {d["skill_id"]: d for d in diag_pre["skills"]}
                for rec_name in ("直线恢复", "777", "圣诞夜的奇迹飞驰！"):
                    rsid = by_name[rec_name]["skill_id"]
                    self.assertNotIn(rsid, diag_pre["eligible_skill_ids"])
                    self.assertEqual(dec_pre[rsid]["reason"], "recovery_blocked_before_terminal")

                # Terminal low stamina (true HP deficit): stamina_gap_boost promotes 直线恢复 first to T0;
                # HP surplus (stamina=1050) must NOT false-promote recovery.
                cands_sta_gap = [
                    _real_cand("直线恢复", 140),
                    _real_cand("弯道恢复○", 140),
                    _real_cand("喘口气", 280),
                    _real_cand("圆弧艺术家", 280),
                ]
                _comp_gap, diag_gap = tsc.compile_tiered_skill_profile(
                    _base_profile(24),
                    {"current_skill_points": 1200, "owned_skill_ids": [], "candidates": cands_sta_gap},
                    norm_catalog_dict,
                    policy=pol_combo,
                    current_stats={"speed": 1500, "stamina": 650, "power": 1200, "guts": 1050, "wisdom": 1250},
                    year=3,
                    terminal=True,
                    knowledge=knowledge,
                )
                dec_gap = {d["skill_id"]: d for d in diag_gap["skills"]}
                self.assertEqual(dec_gap[by_name["直线恢复"]["skill_id"]]["tier"], "T0")
                self.assertEqual(dec_gap[by_name["直线恢复"]["skill_id"]]["cm_reason"], "stamina_gap_boost")

                _comp_surplus, diag_surplus = tsc.compile_tiered_skill_profile(
                    _base_profile(24),
                    {"current_skill_points": 1200, "owned_skill_ids": [], "candidates": cands_sta_gap},
                    norm_catalog_dict,
                    policy=pol_combo,
                    current_stats={"speed": 1500, "stamina": 1050, "power": 1200, "guts": 1050, "wisdom": 1250},
                    year=3,
                    terminal=True,
                    knowledge=knowledge,
                )
                dec_surplus = {d["skill_id"]: d for d in diag_surplus["skills"]}
                self.assertIsNone(dec_surplus[by_name["直线恢复"]["skill_id"]].get("cm_reason"))

                # 4. Reliable groundwork pairing (2 greens + 2 actives vs promoting T1 green)
                gw_sid = by_name["磐石之势"]["skill_id"]
                cands_gw_2g2a = [
                    _real_cand("磐石之势", 240),
                    _real_cand("逆时针○", 80),
                    _real_cand("冬季优俊少女○", 80),
                    _real_cand("抢先", 100),
                    _real_cand("拔得头筹", 100),
                    _real_cand("急弯○", 60),
                ]
                _comp_gw1, diag_gw1 = tsc.compile_tiered_skill_profile(
                    _base_profile(24),
                    {"current_skill_points": 1200, "owned_skill_ids": [], "candidates": cands_gw_2g2a},
                    norm_catalog_dict,
                    policy=pol_combo,
                    current_stats=high_stats,
                    year=3,
                    terminal=True,
                    knowledge=knowledge,
                )
                self.assertIn(gw_sid, diag_gw1["eligible_skill_ids"])
                self.assertEqual(
                    set(diag_gw1["groundwork_bundles"][gw_sid]["required_skill_ids"]),
                    {
                        by_name["逆时针○"]["skill_id"],
                        by_name["冬季优俊少女○"]["skill_id"],
                        by_name["抢先"]["skill_id"],
                        by_name["拔得头筹"]["skill_id"],
                    },
                )
                self.assertEqual({d["skill_id"]: d for d in diag_gw1["skills"]}[by_name["急弯○"]["skill_id"]]["tier"], "T1")

                cands_gw_1g2a = [c for c in cands_gw_2g2a if c["skill_id"] != by_name["冬季优俊少女○"]["skill_id"]]
                _comp_gw2, diag_gw2 = tsc.compile_tiered_skill_profile(
                    _base_profile(24),
                    {"current_skill_points": 1200, "owned_skill_ids": [], "candidates": cands_gw_1g2a},
                    norm_catalog_dict,
                    policy=pol_combo,
                    current_stats=high_stats,
                    year=3,
                    terminal=True,
                    knowledge=knowledge,
                )
                self.assertIn(gw_sid, diag_gw2["eligible_skill_ids"])
                dec_gw2 = {d["skill_id"]: d for d in diag_gw2["skills"]}
                self.assertEqual(dec_gw2[by_name["急弯○"]["skill_id"]]["tier"], "T0")
                self.assertEqual(dec_gw2[by_name["急弯○"]["skill_id"]]["cm_reason"], "groundwork_start_source")
                self.assertEqual(
                    set(diag_gw2["groundwork_bundles"][gw_sid]["required_skill_ids"]),
                    {
                        by_name["逆时针○"]["skill_id"],
                        by_name["急弯○"]["skill_id"],
                        by_name["抢先"]["skill_id"],
                        by_name["拔得头筹"]["skill_id"],
                    },
                )

                # 5. Double circle (◎) placed in T2 stays behind T1 ○ skills under tight budget
                owned_ccw = [by_name["逆时针○"]["skill_id"]]
                cands_dc = [
                    _real_cand("逆时针◎", 110),
                    _real_cand("中距离弯道○", 100),
                ]
                for budget_dc, expect_ids in (
                    (150, {by_name["中距离弯道○"]["skill_id"]}),
                    (250, {by_name["中距离弯道○"]["skill_id"], by_name["逆时针◎"]["skill_id"]}),
                ):
                    comp_dc, _diag_dc = tsc.compile_tiered_skill_profile(
                        _base_profile(24),
                        {"current_skill_points": budget_dc, "owned_skill_ids": owned_ccw, "candidates": cands_dc},
                        norm_catalog_dict,
                        policy=pol_combo,
                        current_stats=high_stats,
                        year=3,
                        terminal=True,
                        knowledge=knowledge,
                    )
                    prof_dc = parse_skill_preference_profile(comp_dc)
                    ctx_dc = SkillPurchaseContext.from_dict({
                        "schema_version": "uma_skill_purchase_context.v1",
                        "run_session_id": f"run_dc_{budget_dc}",
                        "turn_id": f"turn_dc_{budget_dc}",
                        "target_mode": "race",
                        "trigger": "terminal",
                        "current_skill_pt": budget_dc,
                        "owned_skills": owned_ccw,
                        "policy": {"reserve_skill_pt": 0, "allow_gold_without_lower": True, "require_condition_match": False},
                        "available_skills": cands_dc,
                        "race_profile": kawa_target,
                    })
                    plan_dc = optimize_final_skill_build(ctx_dc, catalog, prof_dc, build_skill_candidate_set(ctx_dc, profile_sha256=prof_dc.profile_sha256))
                    self.assertEqual({p.skill_id for p in plan_dc.purchases}, expect_ids)

                # 6. Green skill quota (6 greens) keeps 良场地○ as last resort & deduplicates 超群/擅长迈步
                pol_green = normalize_arc_policy({"enabled": True, "ladder_preset_id": "synth_green_quota_ladder"})
                cands_6plus = [
                    _real_cand("胜利的机运", 260),
                    _real_cand("超群的迈步", 240),
                    _real_cand("逆时针○", 80),
                    _real_cand("冬季优俊少女○", 80),
                    _real_cand("急弯○", 70),
                    _real_cand("自信十足", 180),
                    _real_cand("交流重赏○", 70),
                    _real_cand("晴天○", 90),
                    _real_cand("良场地○", 70),
                ]
                _c_r1, diag_r1 = tsc.compile_tiered_skill_profile(
                    _base_profile(24),
                    {"current_skill_points": 1500, "owned_skill_ids": [], "candidates": cands_6plus},
                    norm_catalog_dict,
                    policy=pol_green,
                    current_stats=high_stats,
                    year=3,
                    terminal=True,
                    knowledge=knowledge,
                )
                dec_r1 = {d["skill_id"]: d for d in diag_r1["skills"]}
                self.assertEqual(dec_r1[by_name["胜利的机运"]["skill_id"]]["tier"], "T0")
                self.assertEqual(dec_r1[by_name["超群的迈步"]["skill_id"]]["tier"], "T0")
                self.assertEqual(dec_r1[by_name["急弯○"]["skill_id"]]["tier"], "T0")
                self.assertEqual(dec_r1[by_name["急弯○"]["skill_id"]]["cm_reason"], "green_skill_quota")
                self.assertEqual(dec_r1[by_name["自信十足"]["skill_id"]]["tier"], "T0")
                self.assertEqual(dec_r1[by_name["自信十足"]["skill_id"]]["cm_reason"], "green_skill_quota")
                self.assertEqual(dec_r1[by_name["晴天○"]["skill_id"]]["reason"], "not_in_ladder")
                self.assertEqual(dec_r1[by_name["良场地○"]["skill_id"]]["reason"], "not_in_ladder")

                # Include 擅长迈步 alongside 超群的迈步 (6 green cards, only 5 distinct primary families) -> 良场地○ promotes to T0
                keep_5_fam = {"胜利的机运", "超群的迈步", "逆时针○", "冬季优俊少女○", "急弯○", "自信十足", "良场地○"}
                cands_5_fam = [c for c in cands_6plus if c["skill_id"] in {by_name[n]["skill_id"] for n in keep_5_fam}] + [_real_cand("擅长迈步", 120)]
                _c_r2, diag_r2 = tsc.compile_tiered_skill_profile(
                    _base_profile(24),
                    {"current_skill_points": 1500, "owned_skill_ids": [], "candidates": cands_5_fam},
                    norm_catalog_dict,
                    policy=pol_green,
                    current_stats=high_stats,
                    year=3,
                    terminal=True,
                    knowledge=knowledge,
                )
                dec_r2 = {d["skill_id"]: d for d in diag_r2["skills"]}
                self.assertEqual(dec_r2[by_name["良场地○"]["skill_id"]]["tier"], "T0")
                self.assertEqual(dec_r2[by_name["良场地○"]["skill_id"]]["cm_reason"], "green_skill_quota")

                # 7. Consecutive multi-skill evolution state machine test
                planner = SafeAdvancePlanner(skill_evolution_choice="evolve")
                portrait_img = np.zeros((1280, 720, 3), dtype=np.uint8)
                sel_text = {"normalized_text": "技能进化选择 请选择进化技能"}
                cnf_text = {"normalized_text": "技能进化确认 是否确定进化技能"}
                for _evo_idx in range(2):
                    act1, _pt1 = planner._skill_evolution_action(portrait_img, sel_text)
                    act2, _pt2 = planner._skill_evolution_action(portrait_img, sel_text)
                    act3, _pt3 = planner._skill_evolution_action(portrait_img, cnf_text)
                    self.assertEqual((act1, act2, act3), ("select", "confirm_selection", "confirm_dialog"))
        finally:
            if prev_pack_env is None:
                os.environ.pop("MAAUMA_STRATEGY_PACK_DIR", None)
            else:
                os.environ["MAAUMA_STRATEGY_PACK_DIR"] = prev_pack_env
            reload_extensions()

    def test_step6_mid_skill_mode_preservation_and_skill_pipeline_fixes(self) -> None:
        from core.auto_trainer.larc_support_skill_startup import (
            GENERAL_MODE, LEGACY_MODE, due_reasons, learning_completion, new_mid_skill_learning,
        )
        from core.skill_planner import candidate_set as cs
        from core.skill_planner import tiered_skill_compiler as tsc

        # 1. new_mid_skill_learning preserves explicit legacy_speed_startup and still defaults to allowlist_v1
        default_cfg = new_mid_skill_learning({"enabled": True, "interval_turns": 6})
        self.assertEqual(default_cfg["mode"], GENERAL_MODE)
        explicit_legacy = new_mid_skill_learning({
            "mode": LEGACY_MODE, "enabled": True, "interval_turns": 6,
            "min_discount": 30.0, "max_cost": 150, "spend_percent": 60.0,
        })
        self.assertEqual(explicit_legacy["mode"], LEGACY_MODE)

        comp = learning_completion({"mode": LEGACY_MODE, "required": 5}, {"confirmed_count": 5, "possible_count": 5})
        self.assertTrue(comp["completed"])
        self.assertEqual(comp["safe_remaining"], 0)
        timing = SimpleNamespace(
            mid_skill_learning=explicit_legacy, min_pt=60, interval_turns=6,
            early_options={}, early_skill_allowlist=["翘尾巴"],
        )
        self.assertEqual(
            due_reasons(
                {"run_session_id": "r_stop", "completed": True, "last_checked_turn": 25},
                {"enabled": True, "mode": LEGACY_MODE, "required": 5},
                run_id="r_stop", turn=31, points=500, timing=timing,
            ),
            [],
        )

        # 2. Multi-evidence 5-dimension acceptance in candidate_set
        sample_item = {
            "skill_id": "cn:中距离弯道○",
            "display_cost": 60,
            "observed_discount_percent": 40,
            "recognition_confidence": 0.90,
            "binding_policy": "current_run_multi_evidence",
            "name_binding": {
                "status": "auto_confirmed",
                "binding_policy": "current_run_multi_evidence",
                "confidence": 0.90,
                "margin": 0.05,
                "evidence_dimensions": 5,
                "evidence": {"name": 0.92, "description": 0.95, "cost": 1.0, "rarity": 1.0, "icon_color": 1.0},
                "current_run_contract": {
                    "version": "current_run_multi_evidence.v1",
                    "local_db_id": "201051",
                    "identity_status": "exact_local_db_id_join",
                    "metadata_status": "external_cross_checked",
                    "description_anchor_exact": True,
                    "name_score_threshold_met": True,
                },
            },
        }
        self.assertTrue(cs._is_current_run_multi_evidence_binding(sample_item["name_binding"]))

        # 3. _lookup_observed_lower_hint resolves unowned lower discount even when OCR drops '！'
        import importlib.util
        snr_spec = importlib.util.spec_from_file_location(
            "_public_backend_skill_name_resolver",
            REPO_ROOT / "backend/core/skill_name_resolver.py",
        )
        assert snr_spec is not None and snr_spec.loader is not None
        b_snr = importlib.util.module_from_spec(snr_spec)
        snr_spec.loader.exec_module(b_snr)
        tier = {"name": "游戏到此为止！", "description": "位于中盘且落后较多时稍微容易赶超"}
        self.assertEqual(
            b_snr._lookup_observed_lower_hint(
                tier,
                [{"name": "游戏到此为止", "acquired": False, "discount_percent": 20, "unknown_reason": []}],
            ),
            20,
        )

        # 4. Whole-chain gold utility outweighs single T1 skill + T2 filler in compiler
        catalog_path = REPO_ROOT / "backend/data/skills/cn-20260916/cn_skill_planner_catalog.json"
        if catalog_path.is_file():
            norm_catalog_dict = json.loads(catalog_path.read_text(encoding="utf-8-sig"))
            prof, _diag = tsc.compile_tiered_skill_profile(
                {
                    **_base_profile(),
                    "race_profile": {"distance_type": "middle", "surface": "dirt", "style": "leader", "distance_m": 2100},
                    "stats": {"speed": 1500, "stamina": 1200, "power": 1200, "guts": 1200, "wisdom": 1200},
                },
                {
                    "current_skill_points": 300,
                    "owned_skill_ids": [],
                    "candidates": [
                        {"skill_id": "cn:先走一步！", "display_cost": 272, "observed_discount_percent": 20, "cost_basis": "observed_final", "recognition_confidence": 0.99, "risk_flags": []},
                        {"skill_id": "cn:游戏到此为止！", "display_cost": 128, "observed_discount_percent": 20, "cost_basis": "observed_final", "recognition_confidence": 0.99, "risk_flags": []},
                        {"skill_id": "cn:疾步", "display_cost": 120, "observed_discount_percent": 20, "cost_basis": "observed_final", "recognition_confidence": 0.99, "risk_flags": []},
                    ],
                },
                norm_catalog_dict,
                policy={
                    "enabled": True,
                    "mode": "tiered_ladder",
                    "ladder": {
                        "tiers": [
                            {"id": "T1", "skills": ["游戏到此为止！", "先走一步！"]},
                            {"id": "T2", "skills": ["疾步"]},
                        ],
                    },
                },
                year=3,
                terminal=True,
            )
            rule_weights = {r["selector"]["value"]: r["weight"] for r in prof["rules"]}
            self.assertGreater(
                rule_weights["cn:先走一步！"],
                rule_weights["cn:游戏到此为止！"] + rule_weights["cn:疾步"],
            )


if __name__ == "__main__":
    unittest.main()
