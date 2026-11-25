"""Regression tests for same-tier gold skill preference and PacerHost summary.json snapshot cache."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
PACER_BACKEND = Path(os.environ.get("MAAUMA_PACER_BACKEND_DIR") or (REPO_ROOT / "pacer" / "backend"))
CORE_BACKEND = REPO_ROOT / "backend"


def _ensure_paths() -> None:
    for path in (PACER_BACKEND, CORE_BACKEND):
        resolved = str(path.resolve())
        if resolved in sys.path:
            sys.path.remove(resolved)
    sys.path.insert(0, str(CORE_BACKEND.resolve()))
    sys.path.insert(0, str(PACER_BACKEND.resolve()))
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


_ensure_paths()

from core.skill_knowledge import SkillKnowledgeLibrary  # noqa: E402
from core.skill_planner.tiered_skill_compiler import compile_tiered_skill_profile  # noqa: E402
from scripts.larc_umaa_host import PacerHost  # noqa: E402


def _row(
    sid: str,
    name: str,
    *,
    rarity: str = "normal",
    base_cost: int = 180,
    group_id: int = 1000,
    group_rate: int = 1,
    relations: dict | None = None,
) -> dict:
    effs = [{"kind": "speed", "value": 0.35 if rarity == "gold" else 0.15, "target_type": 1}]
    branches = [
        {
            "condition_ast": {"constant": True},
            "precondition_ast": {"constant": True},
            "duration": {"base_seconds": 2.4},
            "effects": effs,
        }
    ]
    return {
        "skill_id": sid,
        "name_zh_cn": name,
        "type": "speed",
        "rarity": rarity,
        "is_verified": True,
        "requires_manual_verification": False,
        "manual_status": "manual_verified",
        "identity_status": "manual_verified",
        "metadata_status": "manual_verified",
        "acquisition": {"kind": "pt_purchase"},
        "evaluation_score": 263 if rarity == "gold" else 174,
        "base_cost": base_cost,
        "upgrade_cost": base_cost,
        "total_cost": base_cost,
        "type_raw": "速度",
        "value_raw": "0.15",
        "duration_raw": "2.40",
        "trigger_code": "distance_rate>=50",
        "precondition": "",
        "conditions": {"distance": ["any"], "style": ["any"]},
        "group_id": group_id,
        "group_rate": group_rate,
        "db_id": sid.split(":")[-1],
        "local_db_id": sid.split(":")[-1],
        "relations": relations or {},
        "cost": {"chain": [sid], "base_pt": base_cost},
        "effects": effs,
        "activation_branches": branches,
        "knowledge": {
            "trigger_code": "distance_rate>=50",
            "precondition": "",
            "type_raw": "速度",
            "value_raw": "0.15",
            "duration_raw": "2.40",
            "evaluation_score": 263 if rarity == "gold" else 174,
            "upgrade_cost": base_cost,
            "effects": effs,
            "activation_branches": branches,
        },
    }


def _base_profile() -> dict:
    return {
        "schema_version": "uma.skill_preference_profile.v1",
        "profile_id": "test_gold_pref",
        "base_profile": "race",
        "reserve_skill_pt": 0,
        "filler_policy": "safe_useful_only",
        "max_batch_size": 24,
        "rules": [],
    }


class GoldPreferenceAndSnapshotCacheTests(unittest.TestCase):
    def _compile_sample(self, tmp: Path, *, year: int, terminal: bool, speed: int = 1500) -> dict[str, int]:
        rows = [
            _row("cn:弯道能手○", "弯道能手○", rarity="normal", base_cost=180, group_id=101, group_rate=1, relations={"upper": ["cn:弧线大师"]}),
            _row("cn:弧线大师", "弧线大师", rarity="gold", base_cost=180, group_id=101, group_rate=2, relations={"lower": ["cn:弯道能手○"]}),
            _row("cn:阳之庇佑", "阳之庇佑", rarity="normal", base_cost=200, group_id=102, group_rate=1),
            _row("cn:孤高金技", "孤高金技", rarity="gold", base_cost=300, group_id=103, group_rate=2),
        ]
        catalog_dict = {"schema_version": "uma.cn_larc_skill_library.v1", "skills": rows}
        master_path = tmp / "skill_master.json"
        master_path.write_text(json.dumps(catalog_dict, ensure_ascii=False), encoding="utf-8")
        knowledge = SkillKnowledgeLibrary.load(master_path)
        inventory = {
            "current_skill_points": 1000,
            "owned_skill_ids": [],
            "candidates": [
                {"skill_id": "cn:弯道能手○", "display_name": "弯道能手○", "display_cost": 108, "cost_basis": "observed_final", "observed_hint_level": 4, "observed_discount_percent": 40, "recognition_confidence": 0.95},
                {"skill_id": "cn:弧线大师", "display_name": "弧线大师", "display_cost": 234, "cost_basis": "observed_final", "observed_hint_level": 3, "observed_discount_percent": 30, "recognition_confidence": 0.95},
                {"skill_id": "cn:阳之庇佑", "display_name": "阳之庇佑", "display_cost": 130, "cost_basis": "observed_final", "observed_hint_level": 3, "observed_discount_percent": 35, "recognition_confidence": 0.95},
                {"skill_id": "cn:孤高金技", "display_name": "孤高金技", "display_cost": 240, "cost_basis": "observed_final", "observed_hint_level": 2, "observed_discount_percent": 20, "recognition_confidence": 0.95},
            ],
        }
        arc_policy = {
            "enabled": True,
            "mode": "tiered_ladder",
            "preset_id": "kawasaki_2100_dirt_loh",
            "final_target": {"style": "leader"},
            "ladder": {
                "tiers": [
                    {
                        "id": "T1",
                        "label": "核心",
                        "skills": ["弯道能手○", "弧线大师", "阳之庇佑", "孤高金技"],
                        "within_tier_order": "discount_cost_first",
                    }
                ],
                "avoid": [],
                "allow_lower_fallback": True,
                "green_skill_quota": {"enabled": False, "min_count": 6, "skills": [], "last_resort_skills": []},
                "groundwork_gate": {"enabled": False, "min_sources": 3, "reliable_active_pair": True},
                "stamina_gap_boost": {"enabled": False, "min_stamina": 1150, "use_hp_gate": True, "promote_skills": []},
                "accel_group_caps": [],
                "combo_promotions": [],
            },
            "stage_policy": {
                "classic_arc": {"mode": "stat_rescue", "speed_threshold": 900, "stamina_threshold": 600, "prefer_white": True},
                "senior_arc": {"mode": "t0_only", "allowed_tiers": ["T1"]},
                "terminal": {"mode": "full_ladder"},
            },
        }
        _, diag = compile_tiered_skill_profile(
            _base_profile(),
            inventory,
            catalog_dict,
            policy=arc_policy,
            current_stats={"speed": speed, "stamina": 1200},
            year=year,
            terminal=terminal,
            knowledge=knowledge,
        )
        return {entry["skill_id"]: entry["cm_utility"] for entry in diag["skills"] if entry.get("cm_utility") is not None}

    def test_terminal_gold_outranks_same_tier_whites_classic_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            term_utils = self._compile_sample(Path(tmp), year=3, terminal=True)
            classic_utils = self._compile_sample(Path(tmp), year=2, terminal=False, speed=500)
            # Terminal 2-step gold beats lower white + sibling white.
            self.assertGreater(term_utils["cn:弧线大师"], term_utils["cn:弯道能手○"] + term_utils["cn:阳之庇佑"])
            # Terminal 1-step gold (+10_000 gold bonus) beats cheaper same-tier white despite lower discount.
            self.assertGreater(term_utils["cn:孤高金技"], term_utils["cn:弯道能手○"])
            # Classic prefer_white mode applies white_bonus (+120) and omits gold_bonus, so white keeps edge.
            self.assertGreater(classic_utils["cn:弯道能手○"], term_utils["cn:弯道能手○"])

    def test_cached_summary_fields_hits_cache_until_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "summary.json"
            p.write_text(json.dumps({"status": "running", "stop_reason": None, "turns": [{"turn_index": 1}]}), encoding="utf-8")
            host = object.__new__(PacerHost)
            with patch("scripts.pacer_stop_diagnostics.extract_stop_diagnostics", return_value={"cached": 1}) as mock_diag:
                first = host._cached_summary_fields(p)
                second = host._cached_summary_fields(p)
                self.assertEqual(mock_diag.call_count, 1)
                self.assertEqual(first["last_turn"]["turn_index"], 1)
                self.assertEqual(second["last_turn"]["turn_index"], 1)

                p.write_text(json.dumps({"status": "stopped", "stop_reason": "user_stop", "turns": [{"turn_index": 2}]}), encoding="utf-8")
                third = host._cached_summary_fields(p)
                self.assertEqual(mock_diag.call_count, 2)
                self.assertEqual(third["last_turn"]["turn_index"], 2)
                self.assertEqual(third["summary_status"], "stopped")


if __name__ == "__main__":
    unittest.main()
