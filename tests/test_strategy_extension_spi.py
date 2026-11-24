"""Self-contained public tests for Strategy Extension SPI and open RunControl."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
PACER_BACKEND = REPO_ROOT / "pacer" / "backend"


def _ensure_pacer_backend_path() -> None:
    resolved = str(PACER_BACKEND.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)


class TestStrategyExtensionSPI(unittest.TestCase):
    def setUp(self) -> None:
        _ensure_pacer_backend_path()
        self._prev_pack_env = os.environ.get("MAAUMA_STRATEGY_PACK_DIR")
        os.environ["MAAUMA_STRATEGY_PACK_DIR"] = "__NONE__"
        from core.auto_trainer.larc_strategy_extension import reload_extensions

        reload_extensions()

    def tearDown(self) -> None:
        if self._prev_pack_env is None:
            os.environ.pop("MAAUMA_STRATEGY_PACK_DIR", None)
        else:
            os.environ["MAAUMA_STRATEGY_PACK_DIR"] = self._prev_pack_env
        from core.auto_trainer.larc_strategy_extension import reload_extensions

        reload_extensions()

    def test_public_baseline_only_and_open_run_control(self) -> None:
        from core.auto_trainer import larc_strategy_params as sp
        from core.auto_trainer import larc_strategy_versions as sv
        from core.auto_trainer.cut_runtime_license import LicensedRunControl
        from core.auto_trainer.larc_strategy_extension import strategy_pack_status
        from core.skill_planner import tiered_ladder_presets

        self.assertEqual(sv.CURRENT_REVISION, "2.3")
        self.assertEqual(
            set(sp.PRESET_REGISTRY.keys()),
            {
                "2.1",
                "2.2",
                "2.3",
                "2.3_pro",
                "unified_v2_1",
                "unified_v2_2",
                "unified_v2_3",
                "unified_v2_3_pro",
            },
        )
        with self.assertRaises(ValueError):
            sv.validate_revision("9.9.9")

        status = strategy_pack_status()
        self.assertFalse(status["has_external_pack"])
        self.assertEqual(status["current_revision"], "2.3")
        self.assertEqual(status["unlocked_workflow_modes"], ["direct_career"])
        self.assertFalse(status["allow_custom_workflows"])
        self.assertEqual(status["weight_preset_count"], 0)
        self.assertEqual(status["skill_ladder_preset_count"], 0)
        self.assertEqual(status["skill_ladder_presets"], [])
        self.assertEqual(tiered_ladder_presets.list_tiered_ladder_presets(), [])
        with self.assertRaises(ValueError):
            tiered_ladder_presets.resolve_tiered_ladder_preset("nonexistent")

        ctrl = LicensedRunControl(REPO_ROOT)
        snap = ctrl.snapshot()
        self.assertEqual(snap["runtime_license"]["status"], "open")
        self.assertEqual(snap["runtime_license"]["permission_scope"], "open_source")
        ctrl.checkpoint("spi_test_step")
        self.assertNotEqual(ctrl.snapshot()["state"], "paused")

    def test_synthetic_external_pack_registers_and_reloads(self) -> None:
        from dataclasses import replace
        from core.auto_trainer import larc_strategy_params as sp
        from core.auto_trainer import larc_strategy_rules as sr
        from core.auto_trainer import larc_strategy_versions as sv
        from core.auto_trainer.larc_strategy_extension import reload_extensions, strategy_pack_status
        from core.auto_trainer.larc_target_skills import normalize_arc_policy
        from core.skill_planner import tiered_ladder_presets

        with TemporaryDirectory() as tmp:
            outside_file = Path(tmp) / "outside.json"
            outside_file.write_text(json.dumps({"preset_id": "longchamp_2400_turf", "mode": "tiered_ladder", "ladder": {"tiers": []}}), encoding="utf-8")
            pack_dir = Path(tmp) / "synthetic_pack"
            weights_dir = pack_dir / "weights"
            ladders_dir = pack_dir / "skill_ladders"
            weights_dir.mkdir(parents=True, exist_ok=True)
            ladders_dir.mkdir(parents=True, exist_ok=True)
            sample_profile = {
                "schema_version": "uma.scoring_profile.v2",
                "scoring_semantics": {"model": "uma.larc.final_score", "version": 1},
                "preferences": {
                    "numeric": 1.0,
                    "bond": 1.0,
                    "charge": 1.2,
                    "gold_ship_bonus": 0.0,
                    "mei_bonus": 0.0,
                    "tuning": {"mode": "default", "factors": {}, "formula": "base_score"},
                },
            }
            (weights_dir / "sample_weight.json").write_text(json.dumps(sample_profile), encoding="utf-8")
            sample_ladder = {
                "preset_id": "longchamp_2400_turf",
                "final_target": {"style": "leader"},
                "mode": "tiered_ladder",
                "ladder": {"tiers": [{"id": "T0", "label": "Core", "skills": ["逆时针○"]}]},
            }
            (ladders_dir / "synth_ladder.json").write_text(json.dumps(sample_ladder, ensure_ascii=False), encoding="utf-8")
            manifest = {
                "schema_version": "maauma.strategy_pack.v1",
                "pack_id": "synthetic_test_pack",
                "display_name": "Synthetic Test Pack",
                "version": "9.9.0",
                "entry": "pack.py",
                "default_revision": "9.9",
                "workflow_modes": ["direct_career", "cut_preserve", "cut_then_career"],
                "allow_custom_workflows": True,
                "weight_presets": [
                    {
                        "id": "synth_file_weight",
                        "name": "Synthetic File Weight",
                        "summary": "File-backed weight preset",
                        "recommended_revision": "9.9",
                        "file": "weights/sample_weight.json",
                    },
                    {
                        "id": "synth_inline_weight",
                        "name": "Synthetic Inline Weight",
                        "profile": sample_profile,
                    },
                ],
                "skill_ladder_presets": [
                    {
                        "id": "synth_file_ladder",
                        "name": "Synthetic File Ladder",
                        "summary": "File-backed skill ladder",
                        "file": "skill_ladders/synth_ladder.json",
                    },
                    {
                        "id": "synth_inline_ladder",
                        "name": "Synthetic Inline Ladder",
                        "policy": sample_ladder,
                    },
                    {
                        "id": "synth_escape_ladder",
                        "name": "Traversal Should Be Ignored",
                        "file": "../outside.json",
                    },
                ],
                "ui_revisions": [
                    {
                        "revision": "9.9",
                        "label": "9.9 Synthetic Preset",
                        "notice": "Synthetic test revision.",
                        "default_failure_limit": 30,
                        "attribute_weights": {"speed": 1.1, "stamina": 1.0, "power": 1.0, "guts": 1.0, "wisdom": 1.0},
                        "after_target_credit": 0.4,
                    }
                ],
            }
            (pack_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (pack_dir / "pack.py").write_text(
                (
                    "from dataclasses import replace\n"
                    "from core.auto_trainer.larc_strategy_extension import StrategyExtensionProvider\n"
                    "from core.auto_trainer.larc_strategy_params import PRESET_2_3_PRO, with_ext\n"
                    "class _SyntheticProvider(StrategyExtensionProvider):\n"
                    "    def get_param_attr(self, params, name):\n"
                    "        d = dict(params.ext)\n"
                    "        if name in d: return d[name]\n"
                    "        raise AttributeError(name)\n"
                    "    def first_arc_win_ap_expectation(self, state, goal=None, target_levels=None):\n"
                    "        return {'win_ap': 99, 'synthetic': True}\n"
                    "def register_strategy_pack(registry, *, manifest=None, pack_dir=None):\n"
                    "    preset = with_ext(replace(PRESET_2_3_PRO, revision='9.9', preset_id='unified_v9_9', policy_version='larc_unified_v9_9_test'), synthetic_flag=True)\n"
                    "    registry.register_preset('9.9', preset)\n"
                    "    registry.register_policy_version('9.9', 'larc_unified_v9_9_test')\n"
                    "    registry.set_default_current_revision('9.9')\n"
                    "    registry.set_provider(_SyntheticProvider())\n"
                ),
                encoding="utf-8",
            )
            os.environ["MAAUMA_STRATEGY_PACK_DIR"] = str(pack_dir)
            reload_extensions()

            self.assertEqual(sv.CURRENT_REVISION, "9.9")
            self.assertEqual(sv.validate_revision("9.9"), "9.9")
            self.assertIn("9.9", sp.PRESET_REGISTRY)
            self.assertTrue(sp.PRESET_REGISTRY["9.9"].synthetic_flag)
            self.assertEqual(sr.first_arc_win_ap_expectation(None).get("win_ap"), 99)
            st = strategy_pack_status()
            self.assertTrue(st["has_external_pack"])
            self.assertEqual(st["unlocked_workflow_modes"], ["direct_career", "cut_preserve", "cut_then_career"])
            self.assertTrue(st["allow_custom_workflows"])
            self.assertEqual(st["weight_preset_count"], 2)
            self.assertEqual(st["skill_ladder_preset_count"], 2)
            listed = tiered_ladder_presets.list_tiered_ladder_presets()
            self.assertEqual([x["id"] for x in listed], ["synth_file_ladder", "synth_inline_ladder"])
            self.assertEqual(tiered_ladder_presets.resolve_tiered_ladder_preset("synth_file_ladder")["preset_id"], "longchamp_2400_turf")
            self.assertEqual(tiered_ladder_presets.resolve_tiered_ladder_preset("synth_inline_ladder")["mode"], "tiered_ladder")
            with self.assertRaises(ValueError):
                tiered_ladder_presets.resolve_tiered_ladder_preset("synth_escape_ladder")
            pol = normalize_arc_policy({"enabled": True, "ladder_preset_id": "synth_file_ladder"})
            self.assertEqual(pol["mode"], "tiered_ladder")
            self.assertEqual(pol["ladder"]["tiers"][0]["skills"], ["逆时针○"])

            os.environ["MAAUMA_STRATEGY_PACK_DIR"] = "__NONE__"
            reload_extensions()
            self.assertEqual(sv.CURRENT_REVISION, "2.3")
            self.assertNotIn("9.9", sp.PRESET_REGISTRY)
            st_reset = strategy_pack_status()
            self.assertEqual(st_reset["unlocked_workflow_modes"], ["direct_career"])
            self.assertFalse(st_reset["allow_custom_workflows"])
            self.assertEqual(st_reset["weight_preset_count"], 0)
            self.assertEqual(st_reset["skill_ladder_preset_count"], 0)
            self.assertEqual(tiered_ladder_presets.list_tiered_ladder_presets(), [])

    def test_mei_unlock_gamble_enabled_in_ordinary_auto_career(self) -> None:
        from core.auto_trainer.larc_mei_unlock_policy import gamble_selection, select_mei_unlock
        from core.auto_trainer.state import TrainingTurnState

        mei = {
            "event_id": "jp.single_mode_story:809043004",
            "story_id": "809043004",
            "options": [
                {"index": 1, "effects": []},
                {"index": 2, "effects": [], "conditional_branches": [{"effects": []}, {"effects": []}]},
                {"index": 3, "effects": []},
            ],
        }
        prefs = {"mei_unlock_policy": {"mode": "gamble_when_low", "below_vitality_percent": 50.0}}
        low_hp = TrainingTurnState(screen="event_choice", turn=5, vitality=30.0)
        high_hp = TrainingTurnState(screen="event_choice", turn=5, vitality=60.0)

        sel_low = select_mei_unlock(
            mei,
            ("有时也要忘记工作", "o1", "o2", "o3"),
            low_hp,
            {"cut_farming_active": False, "mei_gamble_authority": True},
            prefs,
        )
        self.assertEqual(sel_low["choice_index"], 2)
        self.assertEqual(sel_low["reason"], "mei_unlock_low_vitality_gamble")
        self.assertTrue(gamble_selection(sel_low))

        sel_high = select_mei_unlock(
            mei,
            ("有时也要忘记工作", "o1", "o2", "o3"),
            high_hp,
            {"cut_farming_active": False, "mei_gamble_authority": True},
            prefs,
        )
        self.assertEqual(sel_high["choice_index"], 1)
        self.assertFalse(gamble_selection(sel_high))

        sel_blocked = select_mei_unlock(
            mei,
            ("有时也要忘记工作", "o1", "o2", "o3"),
            low_hp,
            {"cut_farming_active": True, "cut_restart_authority": False},
            prefs,
        )
        self.assertEqual(sel_blocked["choice_index"], 1)
        self.assertFalse(gamble_selection(sel_blocked))


if __name__ == "__main__":
    unittest.main()
