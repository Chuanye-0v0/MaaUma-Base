"""Tests for realtime luck v3.2 (SS sawtooth removal, per-card specialty, correlated norms)
and domestic dark-trough adb-png charge recovery via effective_domestic_charge.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import statistics
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
PACER_BACKEND = Path(os.environ.get("MAAUMA_PACER_BACKEND_DIR") or (REPO_ROOT / "pacer" / "backend"))
CORE_BACKEND = REPO_ROOT / "backend"
SCRATCH_REVIEWS_DIR = Path(
    r"C:\Users\Administrator\.gemini\antigravity\brain\02a0b5f7-982a-4c0b-9302-7ceea6145cac\scratch\luck_audit_reviews"
)


def _ensure_paths() -> None:
    for path in (PACER_BACKEND, CORE_BACKEND):
        resolved = str(path.resolve())
        if resolved in sys.path:
            sys.path.remove(resolved)
    sys.path.insert(0, str(CORE_BACKEND.resolve()))
    sys.path.insert(0, str(PACER_BACKEND.resolve()))


_ensure_paths()

from core.pacer_realtime_luck import (  # noqa: E402
    LUCK_BASIS,
    _excess_stack_moments_poisson_binomial,
    build_pacer_realtime_luck,
)
from core.auto_trainer.larc_participant_evidence import (  # noqa: E402
    domestic_charge_unlocked,
    effective_domestic_charge,
)
from core.auto_trainer.larc_charge_value import battery_inventory  # noqa: E402
from core.auto_trainer.larc_cycle_value import training_cycle_value  # noqa: E402
from core.auto_trainer.larc_mei_charge_forecast import _current_forecast  # noqa: E402
from core.auto_trainer.larc_ss_matched_stock import structural_status  # noqa: E402
from core.auto_trainer.larc_strategy181_asset import earned_qualification  # noqa: E402
from core.auto_trainer.larc_training_value import score_training_progress  # noqa: E402
from core.larc_reward_cycle import observe_cycle  # noqa: E402
from scripts.larc_cut_standalone_host import runtime_capture_info  # noqa: E402


STAT_TABS = ("speed", "stamina", "power", "guts", "wisdom")


def _default_deck_supports(specialties: tuple[float, float] = (80.0, 80.0)) -> list[dict]:
    return [
        {"slot": 0, "type": "speed", "specialty_priority": specialties[0]},
        {"slot": 1, "type": "speed", "specialty_priority": specialties[1]},
        {"slot": 2, "type": "stamina", "specialty_priority": 50.0},
        {"slot": 3, "type": "wisdom", "specialty_priority": 50.0},
        {"slot": 4, "type": "wisdom", "specialty_priority": 35.0},
        {"slot": 5, "type": "friend", "specialty_priority": 0.0},
    ]


def _make_luck_participant(
    slot: int,
    *,
    kind: str = "support",
    icon_type: str | None = "speed",
    bond_ready: bool = True,
    bond_accepted: bool = True,
    filled: int = 1,
    preview: int = 1,
    character_id: str | None = None,
) -> dict:
    bond = (
        {
            "status": "accepted",
            "fill_ratio_estimate": 0.90 if bond_ready else 0.40,
            "color": "orange" if bond_ready else "cyan",
            "is_max": bond_ready,
        }
        if bond_accepted
        else {"status": "not_visible", "fill_ratio_estimate": None, "color": None, "is_max": None}
    )
    identity = (
        {"status": "known", "character_id": character_id}
        if character_id
        else {"status": "unknown", "character_id": "unknown"}
    )
    role = (
        {"status": "accepted", "kind": kind, "icon_type": icon_type}
        if kind is not None
        else {"status": "unknown", "kind": None, "icon_type": None}
    )
    return {
        "slot": slot,
        "identity": identity,
        "role": role,
        "bond": bond,
        "charge": {
            "status": "accepted",
            "capacity": 3,
            "filled_cells": filled,
            "preview_cells": preview,
            "is_full": filled == 3,
        },
        "hint": {"present": False},
    }


def _period_for_turn(turn: int) -> dict:
    # Turns 1-10 pre_debut junior, turns 11-24 domestic junior/classic (all non-overseas)
    if turn <= 10:
        return {"stage": "junior", "year_index": 1, "month": 1 + (turn - 1) // 2, "half": "first" if turn % 2 else "second", "pre_debut": True}
    month = 6 + ((turn - 11) // 2) % 6
    return {"stage": "junior" if turn <= 16 else "classic", "year_index": 1 if turn <= 16 else 2, "month": month, "half": "first" if turn % 2 else "second", "pre_debut": False}


def _make_turn_obs(
    turn: int,
    *,
    kind: str = "train",
    ss_preview: dict | None = None,
    preview_cells: int = 1,
) -> dict:
    speed_parts = [
        _make_turn_part_rainbow(0, "speed", filled=1, preview=preview_cells, turn=turn),
        _make_turn_part_rainbow(1, "wisdom", filled=0, preview=preview_cells, turn=turn),
    ]
    stamina_parts = [_make_luck_participant(0, kind="npc", icon_type=None, bond_accepted=False, filled=1, preview=preview_cells)]
    wisdom_parts = [_make_turn_part_rainbow(0, "wisdom", filled=2, preview=min(1, preview_cells), turn=turn)]
    by_tab = {
        "speed": speed_parts if turn % 2 == 0 else [],
        "stamina": stamina_parts,
        "power": [],
        "guts": [],
        "wisdom": wisdom_parts if turn % 3 == 0 else [],
    }
    trainings = []
    for tab in STAT_TABS:
        parts = by_tab[tab]
        friendship = tab == "speed" and bool(parts) and _period_for_turn(turn)["pre_debut"] is False
        trainings.append(
            {
                "training_type": tab,
                "friendship": friendship,
                "hint_count": 1 if (tab == "speed" and turn in (5, 11, 17)) else 0,
                "participants": parts,
            }
        )
    return {
        "turn_index": turn,
        "consumed": True,
        "kind": kind,
        "state_before": {"turn": turn, "period": _period_for_turn(turn)},
        "trainings": trainings,
        "ss_comparison": {"preview": ss_preview} if ss_preview is not None else {},
    }


def _make_turn_part_rainbow(slot: int, icon_type: str, *, filled: int, preview: int, turn: int) -> dict:
    return _make_luck_participant(
        slot,
        kind="support",
        icon_type=icon_type,
        bond_ready=(turn >= 11),
        bond_accepted=True,
        filled=filled,
        preview=preview,
    )


def _source(turn: int = 10, run_id: str = "run_v32_test") -> dict:
    return {
        "source_kind": "simulation",
        "run_session_id": run_id,
        "turn_index": turn,
        "observation_id": f"obs_{turn}",
        "step_count": 0,
        "path": f"frames/turn_{turn}.png",
        "absolute_path": f"E:/fake/frames/turn_{turn}.png",
        "sha256": f"sha_{turn}_abcdef",
    }


def _make_consumer_participant(
    slot: int,
    member_id: str,
    *,
    kind: str | None = "support",
    icon_type: str | None = "speed",
    bond_ready: bool = False,
    bond_accepted: bool = True,
    filled: int = 0,
    preview: int = 0,
    character_id: str = "unknown",
    reward_kind: str = "skill_hint",
) -> dict:
    if bond_accepted:
        bond = {
            "status": "accepted",
            "fill_ratio_estimate": 0.90 if bond_ready else 0.35,
            "color": "orange" if bond_ready else "cyan",
            "is_max": bond_ready,
        }
    else:
        bond = {"status": "not_visible", "fill_ratio_estimate": None, "color": None, "is_max": None}
    role = (
        {"status": "accepted", "kind": kind, "icon_type": icon_type}
        if kind is not None
        else {"status": "unknown", "kind": None, "icon_type": None}
    )
    identity = {
        "status": "known" if character_id != "unknown" else "unknown",
        "character_id": character_id,
    }
    reward = {"status": "accepted", "kind": reward_kind, "amount": None, "earned": False}
    return {
        "slot": slot,
        "member_ref": {"status": "accepted", "run_session_id": "run_v32_test", "member_id": member_id},
        "identity": identity,
        "role": role,
        "bond": bond,
        "charge": {
            "status": "accepted",
            "capacity": 3,
            "filled_cells": filled,
            "preview_cells": preview,
            "is_full": filled == 3,
        },
        "training_hint": {"status": "accepted", "present": False},
        "ss_reward": reward,
    }


def _make_training_option(
    training_type: str,
    participants: list[dict],
    *,
    turn: int = 10,
    friendship: bool = False,
    run_id: str = "run_v32_test",
) -> SimpleNamespace:
    src = _source(turn=turn, run_id=run_id)
    packet = {
        "schema_version": "larc_training_participants.v1",
        "status": "accepted",
        "training_type": training_type,
        "source": dict(src),
        "participants": participants,
    }
    return SimpleNamespace(
        training_type=training_type,
        friendship=friendship,
        failure_rate=0,
        vitality_delta=-20,
        stats_gain={k: 12 for k in ("speed", "stamina", "power", "guts", "wisdom", "skill_pt")},
        raw_data={"capture": dict(src)},
        scenario_mechanics={"larc": {"training_participants": packet}},
    )


def _make_state(
    options: list[SimpleNamespace],
    *,
    turn: int = 10,
    year_index: int = 1,
    month: int = 7,
    pre_debut: bool = False,
    period_status: str = "accepted",
    run_id: str = "run_v32_test",
    ready_count: int = 2,
    entry_extra: dict | None = None,
) -> SimpleNamespace:
    src = _source(turn=turn, run_id=run_id)
    scenario_state = {
        "scenario": "larc",
        "larc_period": {
            "status": period_status,
            "value": {"year_index": year_index, "month": month, "half": "first", "pre_debut": pre_debut},
        },
        "larc_ss_entry": {
            "status": "accepted",
            "stage": "entry",
            "ready_count": ready_count,
            "source": dict(src),
        },
        "larc_supporter_goal": {"status": "not_due"},
    }
    if entry_extra:
        scenario_state.update(entry_extra)
    return SimpleNamespace(
        turn=turn,
        screen="training_menu",
        energy=70,
        max_energy=100,
        vitality=70,
        max_vitality=100,
        mood="excellent",
        current_stats={k: 350 for k in ("speed", "stamina", "power", "guts", "wisdom")},
        scenario_state=scenario_state,
        training_options=options,
    )


class TestPacerLuckV32AndDarkTroughCharge(unittest.TestCase):
    def test_counterfactual_1_ss_action_invariance_and_sss_dedup(self) -> None:
        base_payload = {
            "supports": _default_deck_supports(),
            "observations": [_make_turn_obs(t, kind="train", ss_preview=None) for t in range(1, 21)],
        }
        ss_payload = deepcopy(base_payload)
        # Hold full SSS at turn 12 (train), execute at turn 13 (ss), and execute SSS at turn 15
        ss_prev_1 = {"status": "accepted", "kind": "SSS", "ready_count": 5}
        ss_prev_2 = {"status": "accepted", "kind": "SS", "ready_count": 5}
        ss_payload["observations"][11]["ss_comparison"] = {"preview": deepcopy(ss_prev_1)}
        ss_payload["observations"][12]["kind"] = "ss"
        ss_payload["observations"][12]["ss_comparison"] = {"preview": deepcopy(ss_prev_1)}
        ss_payload["observations"][14]["kind"] = "ss"
        ss_payload["observations"][14]["ss_comparison"] = {"preview": deepcopy(ss_prev_2)}

        r_train = build_pacer_realtime_luck(base_payload)
        r_ss = build_pacer_realtime_luck(ss_payload)

        self.assertEqual(r_train["basis"], LUCK_BASIS)
        self.assertEqual(LUCK_BASIS, "deck_rng_expectation_v3_2")
        self.assertEqual(r_ss["score"], r_train["score"])
        self.assertEqual(r_ss["recent_delta"], r_train["recent_delta"])
        self.assertEqual(r_ss["components"]["board_z"], r_train["components"]["board_z"])
        self.assertEqual(r_ss["components"]["charge_z"], 0.0)
        self.assertEqual(r_ss["components"]["sss_z"], 0.0)
        self.assertEqual(r_ss["components"]["ss_charge_z"], 0.0)
        self.assertEqual(r_ss["components"]["ss_pace_z"], 0.0)
        # Held turn 12 + executed turn 13 count as ONE SSS opportunity; turn 15 counts as second SS (non-SSS)
        self.assertEqual(r_ss["components"]["ss_count"], 2)
        self.assertEqual(r_ss["components"]["sss_count"], 1)

    def test_counterfactual_2_bright_vs_dark_trough_luck_invariance(self) -> None:
        bright_payload = {
            "supports": _default_deck_supports(),
            "observations": [_make_turn_obs(t, preview_cells=2 if t % 2 else 1) for t in range(1, 21)],
        }
        dark_payload = {
            "supports": _default_deck_supports(),
            "observations": [_make_turn_obs(t, preview_cells=0) for t in range(1, 21)],
        }
        r_bright = build_pacer_realtime_luck(bright_payload)
        r_dark = build_pacer_realtime_luck(dark_payload)

        self.assertEqual(r_dark["score"], r_bright["score"])
        self.assertEqual(r_dark["recent_delta"], r_bright["recent_delta"])
        self.assertEqual(r_dark["components"]["charge_z"], 0.0)
        self.assertEqual(r_dark["components"]["ss_charge_z"], 0.0)

    def test_counterfactual_2_training_ai_recovers_dark_trough_across_consumers(self) -> None:
        # 4 non-full domestic stamina members in dark trough (preview_cells=0, 1 confirmed rainbow -> +2 each = 8 net charge)
        stamina_dark_parts = [
            _make_consumer_participant(0, "sim_person_0", kind="support", icon_type="stamina", bond_ready=True, filled=0, preview=0, reward_kind="stat_gain"),
            _make_consumer_participant(1, "sim_person_1", kind="support", icon_type="power", bond_ready=False, filled=1, preview=0),
            _make_consumer_participant(2, "sim_person_2", kind=None, icon_type=None, bond_accepted=False, filled=1, preview=0),
            _make_consumer_participant(3, "sim_person_3", kind="npc", icon_type=None, bond_accepted=False, filled=0, preview=0),
        ]
        stamina_dark_parts[0]["ss_reward_cycle"] = {
            **observe_cycle(
                {"status": "accepted", "kind": "stat_gain", "amount": 10, "earned": False},
                [
                    {"status": "accepted", "kind": "max_vitality_and_recovery", "amount": 20, "earned": False},
                    {"status": "accepted", "kind": "skill_hint", "amount": None, "earned": False},
                ],
                _source(10),
            ),
            "association": {"status": "accepted"},
        }
        # 4th option has unknown support bond on guts -> must fall back conservatively to lower bound 1 per member
        guts_unknown_bond_parts = [
            _make_consumer_participant(0, "sim_person_4", kind="support", icon_type="guts", bond_accepted=False, filled=0, preview=0),
            _make_consumer_participant(1, "sim_person_5", kind=None, icon_type=None, bond_accepted=False, filled=1, preview=0),
        ]
        # Bright 2-member speed option (preview_cells=1 each -> 2 total net charge)
        speed_bright_parts = [
            _make_consumer_participant(0, "sim_person_6", kind="support", icon_type="speed", bond_ready=False, filled=0, preview=1),
            _make_consumer_participant(1, "sim_person_7", kind=None, icon_type=None, bond_accepted=False, filled=1, preview=1),
        ]
        # Asset check: member at filled=2 with dark trough preview_cells=0 becomes newly full (+1)
        power_near_full_parts = [
            _make_consumer_participant(0, "sim_person_8", kind="support", icon_type="wisdom", bond_ready=False, filled=2, preview=0),
        ]

        stamina_opt = _make_training_option("stamina", stamina_dark_parts, turn=10, friendship=True)
        speed_opt = _make_training_option("speed", speed_bright_parts, turn=10, friendship=False)
        guts_opt = _make_training_option("guts", guts_unknown_bond_parts, turn=10, friendship=False)
        power_opt = _make_training_option("power", power_near_full_parts, turn=10, friendship=False)
        state = _make_state([stamina_opt, speed_opt, guts_opt, power_opt], turn=10, year_index=1, month=7, pre_debut=False, period_status="verified", ready_count=4)

        weights = {
            "effective_bond": 1.0,
            "net_charge": 5.0,
            "newly_full": 8.0,
            "ss_preparation": 12.0,
            "vitality": 0.5,
            "charge_preference": 1.0,
            "unscaled_net_charge": 5.0,
        }
        prog_stamina = score_training_progress(state, stamina_opt, weights)
        prog_speed = score_training_progress(state, speed_opt, weights)
        prog_guts = score_training_progress(state, guts_opt, weights)

        # Raw vision payload MUST NOT be mutated
        for p in stamina_dark_parts:
            self.assertEqual(p["charge"]["preview_cells"], 0)

        # 4 dark-trough members (with 1 rainbow -> +2 each = 8 net) strictly beat 2 bright members (2 net)
        self.assertEqual(prog_stamina["features"]["net_charge"]["value"], 8)
        self.assertEqual(prog_speed["features"]["net_charge"]["value"], 2)
        self.assertGreater(prog_stamina["components"]["net_charge"], prog_speed["components"]["net_charge"])
        self.assertTrue(all(d["charge_preview_basis"] == "derived_from_rainbow" for d in prog_stamina["participants"]))

        # Unknown support bond on dark frame falls back safely to +1 lower bound
        self.assertEqual(prog_guts["features"]["net_charge"]["value"], 2)
        self.assertTrue(all(d["charge_preview_basis"] == "derived_lower_bound" for d in prog_guts["participants"]))

        # battery_inventory consistency
        inv = battery_inventory(state, "run_v32_test")
        self.assertEqual(inv["sim_person_0"]["previews"]["stamina"], 2)
        self.assertEqual(inv["sim_person_1"]["previews"]["stamina"], 2)
        self.assertEqual(inv["sim_person_2"]["previews"]["stamina"], 2)
        self.assertEqual(inv["sim_person_3"]["previews"]["stamina"], 2)
        self.assertEqual(inv["sim_person_4"]["previews"]["guts"], 1)

        # training_cycle_value consistency (stamina member 0 has recovery cycle reward)
        cycle = training_cycle_value(state, stamina_opt, _source(10), weights, ready=4, newly_full=2)
        self.assertGreater(cycle["score"], 0.0)
        self.assertTrue(any(r["member_id"] == "sim_person_0" for r in cycle["members"]))

        # larc_strategy181_asset earned_qualification counts dark-trough filled==2 member becoming full
        qual = earned_qualification(
            state,
            power_opt,
            {"evidence": {"training_source": _source(10)}},
            calendar=45,
        )
        self.assertTrue(qual["eligible"])
        self.assertIn("sim_person_8", qual["newly_full_member_ids"])

        # _current_forecast (larc_mei_charge_forecast) consistency under verified domestic turn (dark vs bright match!)
        members_15 = [{"member_id": f"sim_person_{i}", "filled_cells": 0 if i < 14 else 3} for i in range(15)]
        members_15[1]["filled_cells"] = 2
        forecast_rule = {
            "schema_version": "larc_mei_charge_rule.v1",
            "status": "accepted",
            "selection_rule": "nonfull_current_training_first",
            "model_rule_cn_verified": False,
            "maximum_members": 5,
            "trigger_probability": 0.4,
            "source": _source(10),
            "members": members_15,
        }
        forecast_parts_dark = [
            _make_consumer_participant(0, "sim_person_14", kind="friend", icon_type=None, bond_accepted=False, filled=3, preview=0, character_id="satake_mei"),
            _make_consumer_participant(1, "sim_person_0", kind="support", icon_type="speed", bond_ready=False, filled=0, preview=0),
            _make_consumer_participant(2, "sim_person_1", kind=None, icon_type=None, bond_accepted=False, filled=2, preview=0),
        ]
        forecast_parts_dark[0]["member_ref"]["member_id"] = "mei_helper"
        forecast_parts_dark[0]["charge"]["applicable"] = False
        forecast_parts_bright = deepcopy(forecast_parts_dark)
        forecast_parts_bright[1]["charge"]["preview_cells"] = 1
        forecast_parts_bright[2]["charge"]["preview_cells"] = 1

        f_opt_dark = _make_training_option("speed", forecast_parts_dark, turn=10)
        f_opt_bright = _make_training_option("speed", forecast_parts_bright, turn=10)
        f_state = _make_state(
            [f_opt_dark],
            turn=10,
            year_index=1,
            month=7,
            pre_debut=False,
            period_status="verified",
            entry_extra={"larc_mei_charge_rule": forecast_rule},
        )
        goal_obj = SimpleNamespace(scenario="larc", larc_policy={"variant": "baseline"})
        fc_dark = _current_forecast(f_state, f_opt_dark, _source(10), goal_obj, 0.4)
        fc_bright = _current_forecast(f_state, f_opt_bright, _source(10), goal_obj, 0.4)
        self.assertIsNotNone(fc_dark)
        self.assertEqual(fc_dark, fc_bright)
        self.assertEqual(fc_dark["ready_after_base_exact"], 2)
        self.assertEqual(fc_dark["nonfull_after_base"], 13)
        self.assertEqual(fc_dark["priority_member_ids"], ["sim_person_0"])

        self.assertEqual(effective_domestic_charge(power_near_full_parts[0], state=state, tab_participants=power_near_full_parts, candidate=power_opt), (2, 1, "derived_from_rainbow"))

    def test_scope_guards_and_tab_intensity_derivation(self) -> None:
        dark_support = _make_consumer_participant(0, "m0", kind="support", icon_type="speed", bond_ready=False, filled=1, preview=0)
        dark_npc = _make_consumer_participant(1, "m1", kind="npc", icon_type=None, bond_accepted=False, filled=1, preview=0)
        dark_none_role = _make_consumer_participant(2, "m2", kind=None, icon_type=None, bond_accepted=False, filled=0, preview=0)
        bright_sibling = _make_consumer_participant(3, "m3", kind="support", icon_type="speed", bond_ready=True, filled=0, preview=3)

        dom_state = _make_state([], turn=10, year_index=1, month=7, pre_debut=False)
        tab = [dark_support, dark_npc, dark_none_role]

        # Domestic Turn 10 unlocks both npc and kind=None teammates
        self.assertTrue(domestic_charge_unlocked(dom_state))
        self.assertEqual(
            effective_domestic_charge(dark_npc, state=dom_state, tab_participants=tab, training_type="speed"),
            (1, 1, "derived_from_rainbow"),
        )
        self.assertEqual(
            effective_domestic_charge(dark_none_role, state=dom_state, tab_participants=tab, training_type="speed"),
            (0, 1, "derived_from_rainbow"),
        )
        # Sibling intensity propagation (+3 clipped to 3 - 1 = 2)
        self.assertEqual(
            effective_domestic_charge(dark_support, state=dom_state, tab_participants=[dark_support, bright_sibling], training_type="speed"),
            (1, 2, "derived_from_tab_intensity"),
        )

        # Overseas blocks (Y2M07, Y2M09, Y3M08, both accepted and verified)
        for y, m, st in ((2, 7, "accepted"), (2, 9, "accepted"), (3, 8, "verified")):
            abroad_state = _make_state([], turn=40, year_index=y, month=m, pre_debut=False, period_status=st)
            self.assertFalse(domestic_charge_unlocked(abroad_state))
            self.assertEqual(effective_domestic_charge(dark_support, state=abroad_state, tab_participants=tab, training_type="speed"), (1, 0, "observed"))

        # Pre-debut Turn 1 & Turn 2 blocked; Turn 3 unlocked
        for t in (1, 2):
            early = _make_state([], turn=t, year_index=1, month=1, pre_debut=True)
            self.assertFalse(domestic_charge_unlocked(early))
            self.assertEqual(effective_domestic_charge(dark_support, state=early, tab_participants=tab, training_type="speed"), (1, 0, "observed"))
        self.assertTrue(domestic_charge_unlocked(_make_state([], turn=3, year_index=1, month=2, pre_debut=True)))

        # allow_derived=False, full bar, Mei/friend/director/reporter, non-L'Arc scenario
        self.assertEqual(effective_domestic_charge(dark_support, state=dom_state, tab_participants=tab, training_type="speed", allow_derived=False), (1, 0, "observed"))
        full_p = _make_consumer_participant(0, "full", filled=3, preview=0)
        self.assertEqual(effective_domestic_charge(full_p, state=dom_state, tab_participants=[full_p], training_type="speed"), (3, 0, "observed"))
        for rk, cid in (("friend", "satake_mei"), ("friend", "other_friend"), ("director", "akikawa"), ("reporter", "otonashi")):
            non_charge = _make_consumer_participant(0, "nc", kind=rk, icon_type=None, bond_accepted=False, filled=0, preview=0, character_id=cid)
            self.assertEqual(effective_domestic_charge(non_charge, state=dom_state, tab_participants=[non_charge], training_type="speed"), (0, 0, "observed"))

        ura_state = SimpleNamespace(turn=10, scenario_state={"scenario": "ura"})
        self.assertFalse(domestic_charge_unlocked(ura_state))
        self.assertEqual(effective_domestic_charge(dark_support, state=ura_state, tab_participants=tab, training_type="speed"), (1, 0, "observed"))

    def test_heterogeneous_specialty_per_card_order_invariance(self) -> None:
        m1, v1 = _excess_stack_moments_poisson_binomial([0.5, 0.15])
        m2, v2 = _excess_stack_moments_poisson_binomial([0.15, 0.5])
        self.assertAlmostEqual(m1, m2, places=12)
        self.assertAlmostEqual(v1, v2, places=12)
        self.assertAlmostEqual(m1, 0.5 * 0.15, places=12)

        obs = [_make_turn_obs(t, preview_cells=1) for t in range(1, 21)]
        r_a = build_pacer_realtime_luck({"supports": _default_deck_supports((100.0, 20.0)), "observations": deepcopy(obs)})
        r_b = build_pacer_realtime_luck({"supports": _default_deck_supports((20.0, 100.0)), "observations": deepcopy(obs)})

        self.assertEqual(r_a["score"], r_b["score"])
        self.assertEqual(r_a["recent_delta"], r_b["recent_delta"])
        self.assertEqual(r_a["components"]["board_z"], r_b["components"]["board_z"])

    def test_overlap_norm_five_turns_and_judgment_shock(self) -> None:
        # At n == 5, cum_z == rec_z and rho_cr == 1.0 -> comp_norm == 1.0 -> score == recent_delta
        obs5 = [_make_turn_obs(t, preview_cells=1) for t in range(1, 6)]
        r5 = build_pacer_realtime_luck({"supports": _default_deck_supports(), "observations": obs5})
        self.assertEqual(r5["score"], r5["recent_delta"])

        obs20 = [_make_turn_obs(t, preview_cells=1) for t in range(1, 21)]
        payload = {"supports": _default_deck_supports(), "observations": obs20}
        r_succ = build_pacer_realtime_luck(
            payload,
            judgment={"status": "ok", "events": [{"turn_index": 18, "failure_rate_percent": 30, "outcome": "success"}]},
        )
        r_fail = build_pacer_realtime_luck(
            payload,
            judgment={"status": "ok", "events": [{"turn_index": 18, "failure_rate_percent": 30, "outcome": "failure"}]},
        )
        self.assertGreater(r_succ["components"]["judgment_z"], 0.0)
        self.assertLess(r_fail["components"]["judgment_z"], 0.0)
        self.assertGreater(r_succ["score"], r_fail["score"])
        self.assertGreater(r_succ["recent_delta"], r_fail["recent_delta"])

    def test_real_cached_cloud_reviews_if_present(self) -> None:
        mf_path = SCRATCH_REVIEWS_DIR / "MF-3259aafc.json"
        if not mf_path.exists():
            self.skipTest("Cached cloud reviews directory not present")
        mf_data = json.loads(mf_path.read_text(encoding="utf-8"))
        res = build_pacer_realtime_luck(mf_data["review"])
        self.assertEqual(res["basis"], "deck_rng_expectation_v3_2")
        self.assertGreaterEqual(res["score"], -14.0)
        self.assertLessEqual(res["score"], -10.0)
        for zero_key in ("charge_z", "sss_z", "ss_charge_z", "ss_pace_z"):
            self.assertEqual(res["components"][zero_key], 0.0)

        ss_deltas = []
        for path in sorted(SCRATCH_REVIEWS_DIR.glob("MF-*.json")):
            rev = json.loads(path.read_text(encoding="utf-8"))["review"]
            by_turn = {}
            for idx, obs in enumerate(rev.get("observations") or (), 1):
                if isinstance(obs, dict) and isinstance(obs.get("trainings"), list) and len(obs["trainings"]) == 5:
                    by_turn.setdefault(int(obs.get("turn_index", idx)), []).append(obs)
            chosen = []
            prev_score = None
            for turn in sorted(by_turn):
                ents = by_turn[turn]
                row = ([r for r in ents if r.get("consumed") is True] or ents)[-1]
                chosen.append(row)
                score = build_pacer_realtime_luck(dict(rev, observations=chosen))["score"]
                if prev_score is not None and score is not None and row.get("kind") == "ss":
                    ss_deltas.append(score - prev_score)
                prev_score = score
        if ss_deltas:
            mean_delta = statistics.mean(ss_deltas)
            self.assertGreaterEqual(mean_delta, -1.5)
            self.assertLessEqual(mean_delta, 1.0)

    def test_runtime_capture_info_detects_adb_fallback_and_fast_native(self) -> None:
        with TemporaryDirectory() as tmp_str:
            root = Path(tmp_str)
            turn_dir = root / "runs" / "attempt_1" / "turn_012"
            turn_dir.mkdir(parents=True, exist_ok=True)
            snap = {
                "mode": "career",
                "cut": {"current_attempt_root": str(root / "runs" / "attempt_1")},
                "record": {"last_turn": {"turn_index": 12, "output": str(turn_dir)}},
            }

            # Case 1: MF-3259aafc style adb-png with native_available=False and empty fallback_reasons
            adb_report = {
                "report": {
                    "stages": {
                        "capture": {
                            "capture_transport": "adb-png",
                            "capture_backend_details": {
                                "backend": "adb-png",
                                "native_available": False,
                                "fallback_reasons": [],
                                "dimensions": {"output_size": [720, 1280]},
                            },
                            "capture_timings": [
                                {"tab": "speed", "frames": 3, "capture_seconds": 1.282},
                                {"tab": "stamina", "frames": 3, "capture_seconds": 1.329},
                            ],
                        }
                    }
                }
            }
            (turn_dir / "pipeline_report.json").write_text(json.dumps(adb_report), encoding="utf-8")
            info_slow = runtime_capture_info(snap, root)
            self.assertIsNotNone(info_slow)
            self.assertEqual(info_slow["backend"], "adb-png")
            self.assertTrue(info_slow["fallback"])
            self.assertTrue(info_slow["is_slow"])
            self.assertAlmostEqual(info_slow["mean_frame_ms"], 435.2, delta=0.5)
            self.assertIn("原生高速通道未启用", info_slow["message"])

            # Case 2: Fast native mumu-ipc (~20ms/frame)
            ipc_report = {
                "report": {
                    "stages": {
                        "capture": {
                            "capture_transport": "mumu-ipc",
                            "capture_backend_details": {
                                "backend": "mumu-ipc",
                                "native_available": True,
                                "fallback_reasons": [],
                                "dimensions": {"output_size": [720, 1280]},
                            },
                            "capture_timings": [
                                {"tab": "speed", "frames": 3, "capture_seconds": 0.06},
                                {"tab": "stamina", "frames": 3, "capture_seconds": 0.06},
                            ],
                        }
                    }
                }
            }
            (turn_dir / "pipeline_report.json").write_text(json.dumps(ipc_report), encoding="utf-8")
            info_fast = runtime_capture_info(snap, root)
            self.assertIsNotNone(info_fast)
            self.assertEqual(info_fast["backend"], "mumu-ipc")
            self.assertFalse(info_fast["fallback"])
            self.assertFalse(info_fast["is_slow"])
            self.assertAlmostEqual(info_fast["mean_frame_ms"], 20.0, delta=0.2)
            self.assertIn("MuMu高速截图", info_fast["message"])

    def test_card_luck_calibration_and_missing_library_fallback(self) -> None:
        from core import pacer_luck_records as plr
        from core import pacer_realtime_luck as prl

        # 1. _card_bond80_prob equals _stage_bond80_prob when initial_bond is None and shifts earlier for higher initial_bond
        for ord_val in (9, 14, 20, 28, 36):
            self.assertAlmostEqual(
                prl._card_bond80_prob(ord_val, None),
                prl._stage_bond80_prob(ord_val),
                places=9,
            )
        self.assertGreater(prl._card_bond80_prob(15, 50.0), prl._card_bond80_prob(15, 0.0))

        # 2. Specialty priority clamp raised to 150.0 (> 120 produces strictly higher home prob)
        p_120, _ = prl._card_tab_probs("speed", 120.0)
        p_140, _ = prl._card_tab_probs("speed", 140.0)
        self.assertGreater(p_140["speed"], p_120["speed"])

        # 3. _rainbow_sidekick_moments supports card_qs and accepts per-card hint_frequency in build_pacer_realtime_luck
        obs = [_make_turn_obs(t) for t in range(12, 18)]
        res = build_pacer_realtime_luck({
            "supports": [
                {"slot": 0, "type": "speed", "specialty_priority": 116.0, "initial_bond": 35, "hint_frequency": 50},
                {"slot": 1, "type": "speed", "specialty_priority": 80.0, "initial_bond": 20, "hint_frequency": 0},
                {"slot": 2, "type": "stamina", "specialty_priority": 50.0},
                {"slot": 3, "type": "wisdom", "specialty_priority": 60.0},
                {"slot": 4, "type": "wisdom", "specialty_priority": 40.0},
                {"slot": 5, "type": "friend"},
            ],
            "observations": obs,
        })
        self.assertTrue(res["available"])
        self.assertEqual(res["basis"], "deck_rng_expectation_v3_2")
        self.assertEqual(res["components"]["card_params_resolved"], 5)

        # 4. Graceful empty fallback when support_library.json is absent in source-only checkout
        plr._support_library_index.cache_clear()
        self.assertIsInstance(plr._support_library_index(), dict)
        self.assertIsInstance(plr._lookup_card_luck_params("30028", level=50), dict)


if __name__ == "__main__":
    unittest.main()
