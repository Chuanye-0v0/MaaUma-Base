"""Regression tests for SS dialogue race-overlay exclusion, goal OCR handoff, and Windows long paths."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

import cv2
import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
PACER_BACKEND = Path(os.environ.get("MAAUMA_PACER_BACKEND_DIR") or (REPO_ROOT / "pacer" / "backend"))


def _ensure_pacer_backend_path() -> None:
    resolved = str(PACER_BACKEND.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)


def _synthetic_overlay_frame() -> np.ndarray:
    """Create a 720x1280 BGR frame whose ROIs match the L'Arc rank/points overlay thresholds."""
    img = np.full((1280, 720, 3), 24, dtype=np.uint8)
    # Rank region hsv[710:890, 205:500]: white pixels (S < 75, V > 185).
    img[710:890, 205:500] = (245, 245, 245)
    # Pt badge hsv[875:952, 235:309]: saturated blue (H ~ 106 in OpenCV) + white.
    img[875:930, 235:309] = (240, 120, 20)
    img[930:952, 235:309] = (245, 245, 245)
    # Award row hsv[882:947, 315:575]: warm orange/gold (H ~ 15 in OpenCV) + white.
    img[882:915, 315:575] = (20, 130, 240)
    img[915:947, 315:575] = (245, 245, 245)
    return img


class TestStopFixesOverlayOcrLongpath(unittest.TestCase):
    def setUp(self) -> None:
        _ensure_pacer_backend_path()
        self._prev_pack_env = os.environ.get("MAAUMA_STRATEGY_PACK_DIR")
        os.environ["MAAUMA_STRATEGY_PACK_DIR"] = "__NONE__"
        try:
            from core.auto_trainer.larc_strategy_extension import reload_extensions

            reload_extensions()
        except ImportError:
            pass

    def tearDown(self) -> None:
        if self._prev_pack_env is None:
            os.environ.pop("MAAUMA_STRATEGY_PACK_DIR", None)
        else:
            os.environ["MAAUMA_STRATEGY_PACK_DIR"] = self._prev_pack_env
        try:
            from core.auto_trainer.larc_strategy_extension import reload_extensions

            reload_extensions()
        except ImportError:
            pass

    def test_larc_rank_points_overlay_excludes_ss_career_dialogue(self) -> None:
        from core.auto_trainer.racing.models import RaceRoutePolicy
        from core.auto_trainer.racing.planner import RaceRoutePlanner

        img = _synthetic_overlay_frame()
        real_overlay = RaceRoutePlanner._larc_rank_points_overlay(img, "第1名 42,000积分 点击继续")
        self.assertIsNotNone(real_overlay)
        self.assertEqual(real_overlay.get("rank"), 1)
        self.assertEqual(real_overlay.get("displayed_points"), 42000)

        dialogue_text = "群星赛 体力 缩短版 记录 启发等级提升！ 适应性积分提升了100"
        self.assertIsNone(RaceRoutePlanner._larc_rank_points_overlay(img, dialogue_text))

        with TemporaryDirectory() as tmp:
            png_path = Path(tmp) / "ss_dialogue.png"
            ok, encoded = cv2.imencode(".png", img)
            self.assertTrue(ok)
            png_path.write_bytes(encoded.tobytes())

            decision = RaceRoutePlanner().plan(
                png_path,
                policy=RaceRoutePolicy(
                    scenario="larc",
                    visible_text=dialogue_text,
                    allow_result_continue=True,
                ),
            )
            self.assertNotIn(
                decision.get("reason"),
                {"race_result_continue", "race_result_overlay_unverified"},
            )
            self.assertNotIn("larc_rank_points_overlay", decision.get("evidence", {}))

    def test_goal_achievement_terminal_text_and_live_overlay_handoff(self) -> None:
        from scripts.larc_autonomy_race import (
            _canonical_sha256,
            _completed_live_result_handoff,
            _goal_achievement_terminal_text,
        )

        self.assertTrue(_goal_achievement_terminal_text("目标达成！\n继续"))
        self.assertTrue(_goal_achievement_terminal_text("自标达成！\n获得42000点协助者积分\n继续"))
        self.assertTrue(_goal_achievement_terminal_text("日标达成！\n粉丝增加"))
        self.assertFalse(_goal_achievement_terminal_text("适应性积分提升了100"))
        self.assertFalse(_goal_achievement_terminal_text("标达成"))

        with TemporaryDirectory() as tmp:
            run_root = Path(tmp).resolve()
            step_dir = run_root / "turn_050" / "after" / "race_result_00"
            step_dir.mkdir(parents=True, exist_ok=True)
            terminal_png = step_dir / "step_001.png"
            ok, encoded = cv2.imencode(".png", np.zeros((1280, 720, 3), dtype=np.uint8))
            self.assertTrue(ok)
            png_bytes = encoded.tobytes()
            terminal_png.write_bytes(png_bytes)
            terminal_sha = hashlib.sha256(png_bytes).hexdigest()

            stop_reason = "goal_achievement_result"
            step_rows = [
                {
                    "result": {
                        "performed": False,
                        "reason": stop_reason,
                        "tap_outcome": None,
                        "frame_safety": {
                            "dangerous": False,
                            "resource_terms": [],
                            "source_image_sha256": terminal_sha,
                            "visible_text": "自标达成！\n获得42000点协助者积分\n继续",
                        },
                        "decision": {
                            "action": "stop",
                            "reason": stop_reason,
                            "evidence": {"image": str(terminal_png)},
                        },
                    }
                }
            ]
            report = {
                "is_valid": True,
                "live_takeover": True,
                "run_session_id": "run_test_goal_ocr",
                "device": "127.0.0.1:16384",
                "turn_index": 50,
                "stop_reason": stop_reason,
                "live_source": {
                    "owner": "race",
                    "state": "race_result",
                    "entered": True,
                    "race_reason": "race_result_continue",
                    "current_result_proof": {
                        "kind": "larc_rank_points_overlay",
                    },
                },
                "step_reports": step_rows,
            }
            owner_before = {"owner": "race", "state": "race_result"}
            owner_handoff = {
                "before": owner_before,
                "report_sha256": _canonical_sha256(report),
                "terminal_source_sha256": terminal_sha,
            }
            (step_dir / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (step_dir / "owner_handoff.json").write_text(
                json.dumps(owner_handoff, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            saved = {
                "status": "handed_off",
                "stop_reason": stop_reason,
                "run_session_id": "run_test_goal_ocr",
                "device": "127.0.0.1:16384",
                "turn_index": 50,
                "owner_before": owner_before,
                "current_input_receipts": [],
                "session": {"result_transition_pending": False},
                "steps": step_rows,
            }
            handoff = _completed_live_result_handoff(run_root, saved)
            self.assertIsNotNone(handoff)
            self.assertEqual(handoff["basis"], "completed_live_overlay_result")

    def test_windows_longpath_image_and_adaptation_session_roundtrip(self) -> None:
        from core.auto_trainer.frame_safety import FrameSafetyAnalyzer
        from core.auto_trainer.larc_adaptation import LarcAdaptationSession
        from core.frame_io import io_path, read_image_compat, read_image_with_retry
        from core.nemu_ipc_capture import NemuIpcCapture
        from core.perception import Perception

        with TemporaryDirectory() as tmp:
            deep_dir = Path(tmp)
            for idx in range(5):
                deep_dir = deep_dir / f"adaptation_first_overseas_race_defer_segment_{idx}_{'x' * 28}"
            long_png = deep_dir / "advance" / "advance_step_001.post_tap.png"
            long_json = deep_dir / "session" / "larc_adaptation_session.json"
            self.assertGreater(len(str(long_png)), 280)
            self.assertGreater(len(str(long_json)), 280)

            synthetic = np.full((1280, 720, 3), 90, dtype=np.uint8)
            NemuIpcCapture.save_image(str(long_png), synthetic)
            self.assertTrue(io_path(long_png).is_file())

            decoded_compat = read_image_compat(long_png)
            decoded_retry = read_image_with_retry(long_png)
            self.assertIsNotNone(decoded_compat)
            self.assertIsNotNone(decoded_retry)
            self.assertEqual(tuple(decoded_compat.shape), (1280, 720, 3))
            self.assertEqual(tuple(decoded_retry.shape), (1280, 720, 3))

            perception_result = Perception().analyze_file(long_png)
            self.assertIsInstance(perception_result, dict)

            packet = FrameSafetyAnalyzer(text_reader=lambda _: "ok").analyze(long_png)
            expected_sha = hashlib.sha256(io_path(long_png).read_bytes()).hexdigest()
            self.assertEqual(packet.source_image_sha256, expected_sha)

            session = LarcAdaptationSession(
                run_session_id="test_longpath_run",
                path=long_json,
                phase="first_overseas_race",
            )
            session.save()
            loaded = LarcAdaptationSession.load(long_json, "test_longpath_run")
            self.assertEqual(loaded.run_session_id, "test_longpath_run")
            self.assertEqual(loaded.phase, "first_overseas_race")


if __name__ == "__main__":
    unittest.main()
