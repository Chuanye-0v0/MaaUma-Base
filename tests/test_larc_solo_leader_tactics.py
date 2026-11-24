"""Regression tests for solo-leader (`领跑` 0 opponents) -> `跟前` auto-switch and cross-race restore."""
from __future__ import annotations

import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import types
import unittest

import cv2
import numpy as np


_HERE = Path(__file__).resolve()
_BASE_BACKEND = _HERE.parents[1] / "pacer" / "backend"
_PACER_BACKEND = Path(
    os.environ.get("MAAUMA_PACER_BACKEND_DIR")
    or (_BASE_BACKEND if (_BASE_BACKEND / "core").is_dir() else _HERE.parents[1])
)


def _ensure_pacer_backend_path() -> None:
    resolved = str(_PACER_BACKEND.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)
    if "config.settings" not in sys.modules:
        try:
            import config.settings  # noqa: F401
        except ImportError:
            cfg_mod = types.ModuleType("config")
            cfg_set = types.ModuleType("config.settings")
            cfg_set.ADB_HOST = "127.0.0.1"
            cfg_set.ADB_ENDPOINT = "127.0.0.1:16384"
            cfg_set.ADB_PORT = 16384
            cfg_mod.settings = cfg_set
            sys.modules["config"] = cfg_mod
            sys.modules["config.settings"] = cfg_set


_ensure_pacer_backend_path()

from core.auto_trainer.larc_preview_text import race_preview_text
from core.auto_trainer.larc_race_tactics import (
    STYLE_CENTERS,
    parse_modal_style_aptitudes,
    preview_is_solo_leader,
    race_tactic_control,
)
from core.auto_trainer.racing.models import RaceRoutePolicy, RaceRouteSession
from core.auto_trainer.racing.planner import RaceRoutePlanner


_BGR_GREEN = (50, 210, 70)
_BGR_WHITE = (240, 240, 240)
_BGR_BROWN_TEXT = (60, 85, 130)
_BGR_GRAY_BG = (195, 195, 200)


def _synth_preview_image(*, solo_leader: bool = True, counts_loaded: bool = True) -> np.ndarray:
    im = np.full((1280, 720, 3), _BGR_GRAY_BG, dtype=np.uint8)
    im[724:765, 515:635] = _BGR_GREEN
    im[690:723, 450:700] = _BGR_WHITE
    if counts_loaded:
        for style, dx in (("back", -94), ("middle", -36), ("front", 23)):
            x0 = int(round(575 + dx - 16))
            im[700:716, x0:x0 + 30] = _BGR_BROWN_TEXT
        if not solo_leader:
            x0 = int(round(575 + 84 - 16))
            im[700:716, x0:x0 + 30] = _BGR_BROWN_TEXT
    im[1120:1220, 130:355] = _BGR_WHITE
    im[1155:1185, 190:300] = _BGR_BROWN_TEXT
    im[1120:1220, 365:590] = (40, 220, 80)
    return im


def _synth_modal_image(selected_style: str = "leader") -> np.ndarray:
    im = np.full((1280, 720, 3), (120, 120, 125), dtype=np.uint8)
    im[310:348, 30:690] = _BGR_GREEN
    im[897:940, 85:320] = _BGR_WHITE
    im[897:940, 400:635] = _BGR_GREEN
    for style, x in STYLE_CENTERS.items():
        im[745:805, x - 48:x + 48] = _BGR_WHITE
        if style == selected_style:
            for y in (732, 813):
                for left in (x - 61, x + 46):
                    im[y:y + 10, left:left + 12] = _BGR_GREEN
    return im


def _preview_text(race_title: str = "凯旋门奖", current_style_label: str = "领跑") -> str:
    return (
        f"GI {race_title} 人气第1 属性 技能 基础能力 "
        f"适应性 草地A 中距离A {current_style_label}A 干劲 极佳 "
        f"战术 后追 6人 居中 9人 跟前 4人 领跑 更改战术 查看结果 前往赛事"
    )


def _modal_text(front_apt: str = "D") -> str:
    return f"战术 跟前 后追G 居中F 跟前{front_apt} 领跑A 已选择：6人 已选择：9人 已选择：4人 已选择：0人 取消 确定"


class SoloLeaderTacticTests(unittest.TestCase):
    def test_solo_leader_switches_to_front_for_d_or_e_and_skips_debut(self) -> None:
        prev_solo = _synth_preview_image(solo_leader=True)
        solo, inks, _ = preview_is_solo_leader(prev_solo)
        self.assertTrue(solo)
        self.assertLess(inks["leader"], 0.015)

        with TemporaryDirectory() as tmpdir:
            prev_path = Path(tmpdir) / "preview_solo.png"
            modal_leader_path = Path(tmpdir) / "modal_leader.png"
            modal_front_path = Path(tmpdir) / "modal_front.png"
            cv2.imwrite(str(prev_path), prev_solo)
            cv2.imwrite(str(modal_leader_path), _synth_modal_image("leader"))
            cv2.imwrite(str(modal_front_path), _synth_modal_image("front"))

            session = RaceRouteSession()
            planner = RaceRoutePlanner()
            policy = RaceRoutePolicy(
                scenario="larc",
                desired_run_style=None,
                solo_leader_to_front=True,
                allow_quick_result=True,
                allow_skip_race_scene=True,
                visible_text=_preview_text("凯旋门奖", "领跑"),
            )
            d_open = planner.plan(prev_path, policy=policy)
            self.assertEqual(d_open["reason"], "race_tactic_open")
            ev_open = d_open["evidence"]["race_tactic"]
            self.assertEqual(ev_open["target_style"], "front")
            self.assertTrue(ev_open["solo_leader_detected"])
            self.assertTrue(ev_open["solo_leader_fallback"])

            session.pending_tactic_target = ev_open["target_style"]
            session.pending_tactic_solo_fallback = bool(ev_open["solo_leader_fallback"])

            d_select = planner.plan(
                modal_leader_path,
                policy=RaceRoutePolicy(**{**policy.__dict__, "visible_text": _modal_text("D")}),
                session=session,
            )
            self.assertEqual(d_select["reason"], "race_tactic_select")
            self.assertEqual(d_select["evidence"]["race_tactic"]["front_aptitude"], "D")

            d_confirm = planner.plan(
                modal_front_path,
                policy=RaceRoutePolicy(**{**policy.__dict__, "visible_text": _modal_text("E")}),
                session=session,
            )
            self.assertEqual(d_confirm["reason"], "race_tactic_confirm")
            self.assertEqual(d_confirm["evidence"]["race_tactic"]["front_aptitude"], "E")
            self.assertTrue(d_confirm["evidence"]["race_tactic"]["solo_leader_fallback"])

            d_debut = planner.plan(
                prev_path,
                policy=RaceRoutePolicy(**{**policy.__dict__, "visible_text": _preview_text("初级新人战", "领跑")}),
            )
            self.assertEqual(d_debut["reason"], "view_race_result")
            self.assertNotIn("race_tactic", d_debut["evidence"])

    def test_front_aptitude_f_or_g_cancels_and_suppresses_reopen(self) -> None:
        with TemporaryDirectory() as tmpdir:
            prev_path = Path(tmpdir) / "preview_solo.png"
            modal_leader_path = Path(tmpdir) / "modal_leader.png"
            cv2.imwrite(str(prev_path), _synth_preview_image(solo_leader=True))
            cv2.imwrite(str(modal_leader_path), _synth_modal_image("leader"))

            planner = RaceRoutePlanner()
            session = RaceRouteSession(pending_tactic_target="front", pending_tactic_solo_fallback=True)
            for bad_apt in ("F", "G"):
                d_cancel = planner.plan(
                    modal_leader_path,
                    policy=RaceRoutePolicy(
                        scenario="larc",
                        solo_leader_to_front=True,
                        allow_quick_result=True,
                        allow_skip_race_scene=True,
                        visible_text=_modal_text(bad_apt),
                    ),
                    session=session,
                )
                self.assertEqual(d_cancel["reason"], "race_tactic_cancel")
                self.assertEqual(d_cancel["evidence"]["race_tactic"]["reason"], "solo_leader_front_aptitude_blocked")
                self.assertEqual(d_cancel["evidence"]["race_tactic"]["front_aptitude"], bad_apt)

            session_rejected = RaceRouteSession(solo_leader_front_aptitude_rejected=True)
            d_skip = planner.plan(
                prev_path,
                policy=RaceRoutePolicy(
                    scenario="larc",
                    solo_leader_to_front=True,
                    allow_quick_result=True,
                    allow_skip_race_scene=True,
                    visible_text=_preview_text("凯旋门奖", "领跑"),
                ),
                session=session_rejected,
            )
            self.assertEqual(d_skip["reason"], "view_race_result")

    def test_non_leader_untouched_and_cross_race_restores_leader_when_opponents_exist(self) -> None:
        with TemporaryDirectory() as tmpdir:
            prev_solo = Path(tmpdir) / "prev_solo.png"
            prev_multi = Path(tmpdir) / "prev_multi.png"
            cv2.imwrite(str(prev_solo), _synth_preview_image(solo_leader=True))
            cv2.imwrite(str(prev_multi), _synth_preview_image(solo_leader=False))

            planner = RaceRoutePlanner()
            d_middle = planner.plan(
                prev_solo,
                policy=RaceRoutePolicy(
                    scenario="larc",
                    solo_leader_to_front=True,
                    allow_quick_result=True,
                    visible_text=_preview_text("凯旋门奖", "居中"),
                ),
            )
            self.assertEqual(d_middle["reason"], "view_race_result")
            self.assertNotIn("race_tactic", d_middle["evidence"])

            d_restore = planner.plan(
                prev_multi,
                policy=RaceRoutePolicy(
                    scenario="larc",
                    solo_leader_to_front=True,
                    solo_leader_restore_style="leader",
                    allow_quick_result=True,
                    visible_text=_preview_text("宝冢纪念", "跟前"),
                ),
            )
            self.assertEqual(d_restore["reason"], "race_tactic_open")
            self.assertEqual(d_restore["evidence"]["race_tactic"]["target_style"], "leader")
            self.assertFalse(d_restore["evidence"]["race_tactic"]["solo_leader_fallback"])

            d_keep_front = planner.plan(
                prev_solo,
                policy=RaceRoutePolicy(
                    scenario="larc",
                    solo_leader_to_front=True,
                    solo_leader_restore_style="leader",
                    allow_quick_result=True,
                    visible_text=_preview_text("宝冢纪念", "跟前"),
                ),
            )
            self.assertEqual(d_keep_front["reason"], "view_race_result")
            self.assertEqual(d_keep_front["evidence"]["race_tactic"]["kind"], "ready")
            self.assertEqual(d_keep_front["evidence"]["race_tactic"]["target_style"], "front")

    def test_race_preview_text_reads_style_when_solo_leader_to_front_enabled(self) -> None:
        img = _synth_preview_image(solo_leader=True)
        boxes = {
            (480, 50, 708, 128): ("人气第1", 0.99),
            (33, 22, 652, 70): ("凯旋门奖", 0.99),
            (168, 1140, 322, 1208): ("查看结果", 0.99),
            (382, 1140, 578, 1208): ("前往赛事", 0.99),
            (353, 178, 490, 217): ("基础能力", 0.99),
            (505, 479, 622, 515): ("中距离A", 0.99),
            (505, 516, 622, 554): ("领跑A", 0.99),
            (515, 721, 632, 773): ("更改战术", 0.99),
        }

        def fake_reader(crop):
            h, w = crop.shape[:2]
            for (x, y, r, b), val in boxes.items():
                if (b - y, r - x) == (h, w):
                    return val
            return ("", 0.0)

        text = race_preview_text(
            img,
            RaceRoutePolicy(scenario="larc", desired_run_style=None, solo_leader_to_front=True, allow_quick_result=True),
            reader=fake_reader,
        )
        self.assertIsNotNone(text)
        self.assertIn("领跑A", text)
        self.assertIn("更改战术", text)
        self.assertEqual(parse_modal_style_aptitudes(_modal_text("D")), {"back": "G", "middle": "F", "front": "D", "leader": "A"})


if __name__ == "__main__":
    unittest.main()
