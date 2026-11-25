"""Regression tests for 0.6.7 fixes: B+A expedition solvency, Duramente 30178 aliases, and 24s success_close."""
from __future__ import annotations

import inspect
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PACER_BACKEND = Path(os.environ.get("MAAUMA_PACER_BACKEND_DIR") or (REPO_ROOT / "pacer" / "backend"))


def _ensure_paths() -> dict[str, object]:
    resolved = str(PACER_BACKEND.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)
    injected: dict[str, object] = {}
    if "config.settings" not in sys.modules:
        try:
            import config.settings  # noqa: F401
        except ImportError:
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


def _fake_duramente_library():
    return types.SimpleNamespace(
        cards={
            "30178": {
                "card_id": "30178",
                "character_id": "1108",
                "name_zh_cn": "[血脉初现]鸣声雷动",
                "character_name_zh_cn": "鸣声雷动",
            }
        },
        events={
            "support:30178:story:801108001": {
                "event_id": "support:30178:story:801108001",
                "story_id": "801108001",
                "support_card_id": "30178",
                "owner_character_id": "1108",
                "next_event_ids": [],
                "titles": {"ja": "流すべきこの汗", "zh_cn": "应该流的这滴汗"},
                "options": [
                    {
                        "index": 1,
                        "branches": [
                            {
                                "probability": 1.0,
                                "skills": [],
                                "unknown_rewards": [],
                                "effects": [
                                    {"kind": "speed", "op": "add", "value": 10},
                                    {"kind": "guts", "op": "add", "value": 10},
                                    {"kind": "bond", "op": "add", "value": 5, "owner_id": "30178"},
                                ],
                            }
                        ],
                    },
                    {
                        "index": 2,
                        "branches": [
                            {
                                "probability": 1.0,
                                "skills": [],
                                "unknown_rewards": [],
                                "effects": [
                                    {"kind": "energy", "op": "add", "value": 25},
                                    {"kind": "bond", "op": "add", "value": 5, "owner_id": "30178"},
                                ],
                            }
                        ],
                    },
                ],
            },
            "support:30178:story:801108002": {
                "event_id": "support:30178:story:801108002",
                "story_id": "801108002",
                "support_card_id": "30178",
                "owner_character_id": "1108",
                "next_event_ids": [],
                "titles": {"ja": "追うべき背中", "zh_cn": "应该追逐的背影"},
                "options": [
                    {"index": 1, "branches": [{"probability": 1.0, "skills": [], "unknown_rewards": [], "effects": [{"kind": "stamina", "op": "add", "value": 15}]}]},
                    {"index": 2, "branches": [{"probability": 1.0, "skills": [], "unknown_rewards": [], "effects": []}]},
                ],
            },
            "support:30178:story:830178002": {
                "event_id": "support:30178:story:830178002",
                "story_id": "830178002",
                "support_card_id": "30178",
                "owner_character_id": "1108",
                "next_event_ids": [],
                "titles": {"ja": "Vol.2 『脈々と』", "zh_cn": "Vol.2 《连绵不绝》"},
                "options": [
                    {"index": 1, "branches": [{"probability": 1.0, "skills": [], "unknown_rewards": [], "effects": [{"kind": "energy", "op": "add", "value": 15}, {"kind": "speed", "op": "add", "value": 15}]}]},
                    {"index": 2, "branches": [{"probability": 1.0, "skills": [], "unknown_rewards": [], "effects": [{"kind": "speed", "op": "add", "value": 7}, {"kind": "power", "op": "add", "value": 7}]}]},
                ],
            },
        },
        option_consequences=lambda *_a, **_kw: None,
    )


class Test067Fixes(unittest.TestCase):
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

    def test_duramente_30178_cn_aliases_and_vol_prefix(self) -> None:
        from core.auto_trainer import larc_event_library as lel
        from core.auto_trainer.config import UGParentGoal
        from core.auto_trainer.state import TrainingTurnState

        fake_lib = _fake_duramente_library()
        ctx = {"support_profile": {"cards": [{"slot": 0, "card_id": "30178"}]}, "trainee_id": "1046"}

        with (
            patch.object(lel, "default_library", return_value=fake_lib),
            patch("core.auto_trainer.larc_event_matching.default_library", return_value=fake_lib, create=True),
            patch("core.auto_trainer.larc_event_matching.title_resolution", return_value={"status": "unmatched"}),
        ):
            lel._support_index.cache_clear()

            res_sweat = lel.resolve_support("协助卡事件\n理应挥洒的汗水\n选项效果\n鸣声雷动", 2, ctx)
            self.assertIsNotNone(res_sweat)
            assert res_sweat is not None
            self.assertEqual(res_sweat["event"]["story_id"], "801108001")

            goal = UGParentGoal.default()
            state = TrainingTurnState(
                screen="training",
                turn=45,
                vitality=55,
                current_stats={"speed": 1000, "stamina": 600, "power": 700, "guts": 550, "wisdom": 650, "skill_pt": 500},
                training_options=[],
                scenario_state={"scenario": "larc", "mood": "good"},
            )
            scored = lel.score_library_event(res_sweat, state, goal, ctx, "good")
            self.assertEqual(scored["choice_index"], 2)

            res_back = lel.resolve_support("协助卡事件\n理应追逐的背影\n选项效果\n鸣声雷动", 2, ctx)
            self.assertIsNotNone(res_back)
            assert res_back is not None
            self.assertEqual(res_back["event"]["story_id"], "801108002")

            res_vol2 = lel.resolve_support("协助卡事件\nVol.2 《薪火相传》\n选项效果\n鸣声雷动", 2, ctx)
            self.assertIsNotNone(res_vol2)
            assert res_vol2 is not None
            self.assertEqual(res_vol2["event"]["story_id"], "830178002")
            self.assertEqual(res_vol2["identity_basis"], "known_deck_card_vol_prefix_unique")

            lel._support_index.cache_clear()

    def test_ba_expedition_route_solvency_650_vs_600(self) -> None:
        from core.auto_trainer import larc_purchase_v3 as v3

        days = tuple(v3.free_action_days(37))
        levels = [2, 2, 1, 1, 1, 1, 0, 0, 0, 0]

        def utility(_index: int, owned: tuple[int, ...]) -> float:
            return 100.0 + 40.0 * sum(1 for s in range(6) if owned[s] == 3)

        common = dict(
            now=36,
            original=levels,
            days=days,
            utility=utility,
            share=1.0,
            branch="conservative",
            initial=[2, 2, 1, 3, 1, 3, 0, 0, 0, 0],
            initial_cost=600,
            phases=["speed_expedition", "expedition_skill_pt"],
            allow_language_deferral=True,
            balance_resources=True,
            focused_first_cycle=True,
            same_day_purchases=True,
            allow_power_deferral=True,
            keep_first_arc_cash_route=False,
        )

        feasible_650 = v3._route(points=650, current_income=170, **common)
        self.assertTrue(feasible_650["continuation_feasible"])
        self.assertIsNone(feasible_650["halted_at"])

        halted_600 = v3._route(points=600, current_income=0, **common)
        self.assertFalse(halted_600["continuation_feasible"])
        self.assertEqual(halted_600["halted_at"]["ordinal"], 42)
        self.assertGreaterEqual(halted_600["halted_at"]["shortfall"], 50.0)

    def test_success_close_uses_range_48(self) -> None:
        sc_path = REPO_ROOT / "backend" / "core" / "autonomous_training" / "success_close.py"
        self.assertIn("range(48)", sc_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
