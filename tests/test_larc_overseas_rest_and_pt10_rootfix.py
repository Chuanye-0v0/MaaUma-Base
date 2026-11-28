"""Regression tests for Y2 overseas rest vs 0-person wisdom and early Pt+10 reserve."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACER_ROOT = ROOT / "pacer"
PACER_BACKEND = PACER_ROOT / "backend"
for p in (PACER_BACKEND, PACER_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.auto_trainer import larc_recovery_routes as lrr  # noqa: E402
from core.auto_trainer.config import UGParentGoal, validate_goal_config  # noqa: E402
from core.auto_trainer.larc_calendar import period_at, period_ordinal  # noqa: E402
from core.auto_trainer.larc_joint_career import cycle_reserve as cr  # noqa: E402
from core.auto_trainer.larc_opening import LarcOpeningStrategy  # noqa: E402
from core.auto_trainer.state import TrainingCandidate, TrainingTurnState  # noqa: E402


def _make_public_goal() -> UGParentGoal:
    raw = UGParentGoal.default().to_dict()
    raw["scenario"] = "larc"
    raw["total_turns"] = 78
    raw["risk_tolerance"] = "low"
    raw["target_stats"] = {
        "speed": 1500,
        "stamina": 1200,
        "power": 1300,
        "guts": 1100,
        "wisdom": 1200,
    }
    raw["failure_limit"] = 30.0
    raw["larc_policy"] = {
        "variant": "baseline",
        "revision": "2.3",
        "strategy_refinement": True,
        "training_balance_v2": True,
        "overseas_purchase_v2": True,
        "overseas_purchase_v3": True,
        "strategy_v4": True,
        "attribute_weights": {
            "speed": 1.1,
            "stamina": 1.2,
            "power": 1.0,
            "guts": 0.9,
            "wisdom": 0.8,
        },
        "after_target_credit": 0.45,
        "strategy22_params": {},
    }
    goal = UGParentGoal.from_dict(raw)
    validate_goal_config(goal)
    return goal


def _overseas_state(
    *,
    ordinal: int,
    vitality: float,
    wisdom_gain: dict[str, int] | None = None,
    wisdom_friendship: bool = False,
    wisdom_supports: int = 0,
    speed_failure: float = 19.0,
) -> TrainingTurnState:
    wis_gain = wisdom_gain or {"speed": 11, "wisdom": 28, "skill_pt": 33}
    candidates = [
        TrainingCandidate(
            training_type="speed",
            stats_gain={"speed": 46, "power": 19, "skill_pt": 51},
            failure_rate=speed_failure,
            support_count=4,
            friendship=False,
            vitality_delta=-21,
            raw_data={"capture": {"run_session_id": "test_overseas_fix"}},
        ),
        TrainingCandidate(
            training_type="stamina",
            stats_gain={"stamina": 36, "guts": 18, "skill_pt": 36},
            failure_rate=23.0,
            support_count=2,
            friendship=False,
            vitality_delta=-21,
            raw_data={"capture": {"run_session_id": "test_overseas_fix"}},
        ),
        TrainingCandidate(
            training_type="power",
            stats_gain={"stamina": 27, "power": 54, "skill_pt": 43},
            failure_rate=24.0,
            support_count=2,
            friendship=False,
            vitality_delta=-24,
            raw_data={"capture": {"run_session_id": "test_overseas_fix"}},
        ),
        TrainingCandidate(
            training_type="guts",
            stats_gain={"speed": 20, "power": 17, "guts": 44, "skill_pt": 41},
            failure_rate=27.0,
            support_count=2,
            friendship=False,
            vitality_delta=-25,
            raw_data={"capture": {"run_session_id": "test_overseas_fix"}},
        ),
        TrainingCandidate(
            training_type="wisdom",
            stats_gain=wis_gain,
            failure_rate=0.0,
            support_count=wisdom_supports,
            friendship=wisdom_friendship,
            vitality_delta=5,
            raw_data={"capture": {"run_session_id": "test_overseas_fix"}},
        ),
    ]
    per = dict(period_at(ordinal))
    year = per.get("year_index")
    if year in (2, 3):
        per["stage"] = {2: "classic", 3: "senior"}[year]
    return TrainingTurnState(
        screen="training_menu",
        turn=ordinal,
        vitality=int(round(vitality)),
        current_stats={
            "speed": 1004,
            "stamina": 426,
            "power": 689,
            "guts": 525,
            "wisdom": 701,
            "skill_pt": 1280,
        },
        training_options=candidates,
        scenario_state={
            "scenario": "larc",
            "mood": "excellent",
            "larc_period": {"status": "verified", "value": per},
            "larc_growth_observation": {"status": "accepted", "turn_index": ordinal, "expectation": 66.4},
            "larc_training_history": [
                {
                    "turn": ordinal - 1,
                    "training_type": "speed",
                    "stats_gain": {"speed": 95, "power": 42, "skill_pt": 60},
                    "vitality_delta": -21,
                    "failure_rate": 0.0,
                    "friendship": True,
                }
            ],
        },
    )


class TestLarcOverseasRestAndPt10RootFix(unittest.TestCase):
    def test_recovery_window_and_terminal_energy_weight_y2_vs_y3(self) -> None:
        goal = _make_public_goal()

        for ordinal in (38, 39):  # Y2M8a, Y2M8b
            st = _overseas_state(ordinal=ordinal, vitality=38.04)
            w = lrr.effective_recovery_window(st, goal)
            self.assertIsNotNone(w)
            self.assertEqual(w["kind"], "expedition_continuity")
            self.assertGreaterEqual(w["steps"], 4)
            self.assertEqual(w["action_calendar"][-1]["kind"], "domestic")
            self.assertGreater(period_ordinal(w["action_calendar"][-1]["period"]), 42)
            self.assertEqual(lrr.terminal_energy_weight(st, goal, w), 0.7)

        # Year 3 overseas still terminates at Final Arc (ordinal 66) with 0 terminal energy weight.
        st_y3 = _overseas_state(ordinal=63, vitality=38.04)
        w_y3 = lrr.effective_recovery_window(st_y3, goal)
        self.assertIsNotNone(w_y3)
        self.assertEqual(w_y3["kind"], "expedition_continuity")
        self.assertEqual(w_y3["steps"], 1)
        self.assertEqual([period_ordinal(x["period"]) for x in w_y3["action_calendar"]], [65])
        self.assertEqual(lrr.terminal_energy_weight(st_y3, goal, w_y3), 0.0)

    def test_overseas_low_hp_weak_wisdom_vs_rainbow_and_high_hp(self) -> None:
        goal = _make_public_goal()

        # 1) Y2M8b (ordinal 39), HP=38%, 0-person weak wisdom -> rest.normal must win cleanly.
        st_weak = _overseas_state(ordinal=39, vitality=38.04)
        ev_weak = LarcOpeningStrategy(goal).evaluate(st_weak)
        self.assertEqual(ev_weak.action, "rest")
        rest_row = next(r for r in ev_weak.scored_candidates if r.get("action_id") == "rest.normal")
        rt_weak = rest_row["planning"]["recovery_timing"]
        self.assertTrue(rt_weak["overseas_low_hp_wisdom"])
        self.assertGreater(rt_weak["score"], 0.0)

        # 2) Same Y2M8b (ordinal 39), HP=38%, 3-person rainbow wisdom -> train:wisdom still wins.
        st_rainbow = _overseas_state(
            ordinal=39,
            vitality=38.04,
            wisdom_gain={"speed": 20, "wisdom": 60, "skill_pt": 44},
            wisdom_friendship=True,
            wisdom_supports=3,
        )
        ev_rb = LarcOpeningStrategy(goal).evaluate(st_rainbow)
        self.assertEqual(ev_rb.action, "train")
        self.assertEqual(ev_rb.target, "wisdom")

        # 3) High-HP Y2M7b (ordinal 37), HP=81.5%, speed 0% risk -> normal speed training wins & flag is False.
        st_high = _overseas_state(ordinal=37, vitality=81.52, speed_failure=0.0)
        ev_high = LarcOpeningStrategy(goal).evaluate(st_high)
        self.assertEqual(ev_high.action, "train")
        self.assertEqual(ev_high.target, "speed")
        rest_high = next(r for r in ev_high.scored_candidates if r.get("action_id") == "rest.normal")
        rt_high = rest_high["planning"].get("recovery_timing") or {}
        self.assertFalse(bool(rt_high.get("overseas_low_hp_wisdom")))

    def test_cycle_reserve_budget_overseas_floor_solvency(self) -> None:
        tgt = [2, 2, 1, 3, 1, 3, 0, 0, 0, 0]

        # T35 (now=36, cash=650, bundle_3_5 cost=600, inc=150, share=1.0): funded!
        res_t35 = cr._budget(
            {"targets": tgt, "cost": 600},
            now=36,
            cash=650,
            share=1.0,
            defer_language=True,
            current_income=150,
            defer_niel_power=True,
        )
        self.assertEqual(res_t35["preparation_shortfall"], 0)
        self.assertGreater(res_t35["friendship_cash"], 0)

        # Genuinely insolvent T35 (cash=600, inc=0): blocked!
        res_insolvent = cr._budget(
            {"targets": tgt, "cost": 600},
            now=36,
            cash=600,
            share=1.0,
            defer_language=True,
            current_income=0,
            defer_niel_power=True,
        )
        self.assertGreater(res_insolvent["preparation_shortfall"], 0)

        # T36 (now=37, cash=500, pt10 cost=300, inc=90, share=0.75): funded!
        res_t36 = cr._budget(
            {"targets": tgt, "cost": 300},
            now=37,
            cash=500,
            share=0.75,
            defer_language=True,
            current_income=90,
            defer_niel_power=True,
        )
        self.assertEqual(res_t36["preparation_shortfall"], 0)
        self.assertGreater(res_t36["friendship_cash"], 0)

        # Low-cash T36 (cash=350, pt10 cost=300, inc=90, share=1.0): still blocked!
        res_low = cr._budget(
            {"targets": tgt, "cost": 300},
            now=37,
            cash=350,
            share=1.0,
            defer_language=True,
            current_income=90,
            defer_niel_power=True,
        )
        self.assertGreater(res_low["preparation_shortfall"], 0)


if __name__ == "__main__":
    unittest.main()
