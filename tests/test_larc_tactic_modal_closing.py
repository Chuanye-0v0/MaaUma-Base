"""Regression tests for tactics-modal closing transition waiting (prevents race_confirm:target_verification_required)."""
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

from core.auto_trainer.larc_race_tactics import STYLE_CENTERS
from core.auto_trainer.racing.models import RaceRoutePolicy, RaceRouteSession
from core.auto_trainer.racing.planner import RaceRoutePlanner


_BGR_GREEN = (50, 210, 70)
_BGR_WHITE = (240, 240, 240)


def _synth_modal_image(selected_style: str = "front") -> np.ndarray:
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


class TacticModalClosingTests(unittest.TestCase):
    def test_tactic_modal_closing_frame_waits_instead_of_race_confirm_stop(self) -> None:
        planner = RaceRoutePlanner()
        with TemporaryDirectory() as tmpdir:
            modal_front_path = Path(tmpdir) / "modal_front.png"
            occluded_path = Path(tmpdir) / "modal_front_confetti.png"
            modal_im = _synth_modal_image("front")
            cv2.imwrite(str(modal_front_path), modal_im)
            confetti_im = modal_im.copy()
            confetti_im[897:940, 400:635] = _BGR_WHITE
            cv2.imwrite(str(occluded_path), confetti_im)

            occluded_text = (
                "910 744 战术 跟前 尽量往前冲 咬紧领头的选手 "
                "后追G 居中G 跟前D 领跑A 已选择：2人 已选择：4人 已选择：1人 取消"
            )
            modal_text = "战术 跟前 后追G 居中G 跟前D 领跑A 已选择：2人 已选择：4人 已选择：1人 取消 确定"

            d_solo_closing = planner.plan(
                modal_front_path,
                session=RaceRouteSession(pending_tactic_target=None, tactic_modal_closing=True),
                policy=RaceRoutePolicy(
                    scenario="larc",
                    desired_run_style=None,
                    solo_leader_to_front=True,
                    allow_confirm_race=True,
                    allow_quick_result=True,
                    post_entry_only=True,
                    visible_text=occluded_text,
                ),
            )
            self.assertEqual(d_solo_closing["action"], "wait")
            self.assertEqual(d_solo_closing["reason"], "race_tactic_modal_closing")

            d_explicit_closing = planner.plan(
                modal_front_path,
                session=RaceRouteSession(pending_tactic_target="front", tactic_modal_closing=True),
                policy=RaceRoutePolicy(
                    scenario="larc",
                    desired_run_style="front",
                    allow_confirm_race=True,
                    allow_quick_result=True,
                    visible_text=modal_text,
                ),
            )
            self.assertEqual(d_explicit_closing["action"], "wait")
            self.assertEqual(d_explicit_closing["reason"], "race_tactic_modal_closing")

            d_confetti = planner.plan(
                occluded_path,
                session=RaceRouteSession(pending_tactic_target="front", tactic_modal_closing=False),
                policy=RaceRoutePolicy(
                    scenario="larc",
                    desired_run_style="front",
                    allow_confirm_race=True,
                    allow_quick_result=True,
                    visible_text=occluded_text,
                ),
            )
            self.assertEqual(d_confetti["action"], "wait")
            self.assertEqual(d_confetti["reason"], "race_tactic_modal_closing")


if __name__ == "__main__":
    unittest.main()
