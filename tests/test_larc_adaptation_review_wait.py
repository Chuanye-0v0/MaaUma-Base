"""Regression tests for larc_adaptation_review grid wait and 4-card confirmation modal controls."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import cv2
import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
PACER_BACKEND = REPO_ROOT / "pacer" / "backend"


def _with_pacer_path(fn):
    def wrapper(*args, **kwargs):
        saved_modules = sys.modules.copy()
        saved_path = list(sys.path)
        for mod_name in list(sys.modules):
            if mod_name == "core" or mod_name.startswith("core."):
                sys.modules.pop(mod_name, None)
        sys.path.insert(0, str(PACER_BACKEND))
        try:
            return fn(*args, **kwargs)
        finally:
            for mod_name in list(sys.modules):
                if mod_name not in saved_modules:
                    sys.modules.pop(mod_name, None)
            sys.modules.update(saved_modules)
            sys.path[:] = saved_path

    return wrapper


def _make_grid_frame(*, ripple: bool = False) -> np.ndarray:
    img = np.full((1280, 720, 3), 235, dtype=np.uint8)
    # Adaptation blue detail heading [655:682, 50:670] (HSV H ~ 110, S > 100)
    img[650:690, 40:680] = (210, 120, 25)
    # White detail panel [775:965, 60:660]
    img[770:975, 50:670] = (250, 250, 250)
    # Green Review button around (360, 1078)
    img[1038:1118, 210:510] = (45, 190, 95)
    if ripple:
        img[700:760, 260:460] = (255, 255, 255)
        img[1050:1105, 250:470] = (220, 255, 220)
    return img


def _make_pending_confirm_modal_shell() -> np.ndarray:
    img = np.full((1280, 720, 3), 248, dtype=np.uint8)
    # Green top modal header band y=32..92
    img[32:92, 20:700] = (45, 190, 95)
    # Blank modal body (no dark text yet) -> _modal_content_pending == True
    img[1135:1215, 70:340] = (250, 250, 250)
    img[1135:1215, 385:655] = (45, 190, 95)
    return img


def _make_4card_confirm_modal_frame() -> np.ndarray:
    img = np.full((1280, 720, 3), 250, dtype=np.uint8)
    # Green top modal header band y=32..92 (BGR with HSV H ~ 50, S > 90, V > 120)
    img[32:92, 20:700] = (45, 190, 95)
    # 4 blue card header bars inside y=129..750 (500..750 covers y=550..770 so white_550_770 < 0.55)
    for top in (130, 313, 497, 575, 681):
        img[top : top + 68, 40:680] = (220, 120, 25)
    # Dark text lines below the header band so _modal_content_pending is False (>= 0.5% dark pixels in [112:312, 120:600])
    img[220:260, 140:480] = (60, 70, 90)
    # Bottom Cancel (white) & Upgrade (green)
    img[1135:1215, 70:340] = (250, 250, 250)
    img[1135:1215, 385:655] = (45, 190, 95)
    return img


class _FakeInMemoryAdb:
    supports_in_memory_capture = True
    address = "127.0.0.1:16416"

    def __init__(self, frames: list[np.ndarray]) -> None:
        self._frames = list(frames)
        self.taps: list[tuple[int, int, bool]] = []
        self.saved_frames: list[Path] = []

    def tap_no_wait(self, x: int, y: int, *, scaled: bool = False) -> bool:
        self.taps.append((x, y, scaled))
        return True

    def read_frame(self, *, deadline: float | None = None) -> np.ndarray:
        if len(self._frames) > 1:
            return self._frames.pop(0)
        return self._frames[0]

    def save_frame(self, path: Path | str, frame: np.ndarray) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(p), frame)
        self.saved_frames.append(p)


class TestLarcAdaptationReviewWait(unittest.TestCase):
    @_with_pacer_path
    def test_adaptation_controls_rejects_grid_when_leave_required_and_accepts_4card_modal(self) -> None:
        from core.auto_trainer.larc_control_wait import adaptation_controls, _modal_controls

        grid = _make_grid_frame()
        pending_modal = _make_pending_confirm_modal_shell()
        four_card_modal = _make_4card_confirm_modal_frame()

        self.assertTrue(adaptation_controls(grid, allow_home=False, require_leave_grid=False))
        self.assertFalse(adaptation_controls(grid, allow_home=False, require_leave_grid=True))
        self.assertFalse(adaptation_controls(pending_modal, allow_home=False, require_leave_grid=True))

        # Verify our synthetic 4-card modal exercises the new helper (old _modal_controls rejects it).
        hsv_4card = cv2.cvtColor(four_card_modal, cv2.COLOR_BGR2HSV)
        self.assertFalse(_modal_controls(hsv_4card))
        self.assertTrue(adaptation_controls(four_card_modal, allow_home=False, require_leave_grid=True))

    @_with_pacer_path
    def test_executor_review_tap_waits_past_grid_ripple_until_4card_modal_ready(self) -> None:
        from core.auto_trainer.advance.constants import ADVANCE_CONFIRM_TOKEN
        from core.auto_trainer.advance.executor import SafeAdvanceExecutor

        grid_before = _make_grid_frame(ripple=False)
        grid_ripple = _make_grid_frame(ripple=True)
        pending_modal = _make_pending_confirm_modal_shell()
        four_card_modal = _make_4card_confirm_modal_frame()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            screenshot_path = tmp_dir / "advance_step_010.png"
            post_tap_path = tmp_dir / "advance_step_010.post_tap.png"
            cv2.imwrite(str(screenshot_path), grid_before)
            src_sha = hashlib.sha256(screenshot_path.read_bytes()).hexdigest()

            adb = _FakeInMemoryAdb([grid_ripple, pending_modal, four_card_modal])
            clock_now = [100.0]

            def fake_clock() -> float:
                return clock_now[0]

            def fake_sleep(secs: float) -> None:
                clock_now[0] += secs

            executor = SafeAdvanceExecutor(monotonic_fn=fake_clock, sleep_fn=fake_sleep)
            packet = SimpleNamespace(source_image_sha256=src_sha, to_dict=lambda: {"sha256": src_sha})
            session_mock = mock.MagicMock()

            ctx = SimpleNamespace(
                mode="live",
                execute=True,
                confirm_token=ADVANCE_CONFIRM_TOKEN,
                scenario="larc",
                screenshot_path=screenshot_path,
                post_tap_screenshot_path=post_tap_path,
                wait_seconds=0.8,
                settle_seconds=0.3,
                post_wait_freezes=True,
                post_wait_freeze_poll_seconds=0.1,
                post_wait_freeze_timeout_seconds=4.0,
                post_wait_freeze_stable_frames=2,
                post_wait_freeze_max_frame_diff=0.004,
                larc_adaptation_session=session_mock,
                larc_member_roster=None,
                larc_member_roster_path=None,
                larc_observed_turn=None,
                skill_handoff_authorization=None,
                skill_handoff_session=None,
                skip_session=None,
                transition_session=None,
                loading_wait_resume_report=None,
                loading_wait_resume_report_sha256=None,
                loading_wait_resume_session_path=None,
                capture_observation_report_sha256=None,
                capture_observation_session_path=None,
                control_driver=None,
                allow_unbound_goal_summary_continue=False,
                adb=adb,
                packet=packet,
                decision={
                    "action": "tap",
                    "reason": "larc_adaptation_review",
                    "route_state": "modal",
                    "tap": {
                        "coordinate_name": "safe_advance.larc_adaptation_review",
                        "x": 360,
                        "y": 1078,
                        "scaled": False,
                    },
                },
                resume_requested=False,
                capture_handoff=None,
                loading_contract_amendment=None,
                global_route_interrupt=False,
                larc_read_only_wait=False,
                transition_view=None,
                transition_expected_states=None,
                record_global_transition=False,
                staged_live_tap=False,
                safety_verdict=SimpleNamespace(to_dict=lambda: {}),
                safety_evidence={},
            )

            result = executor._run_stage_execute_and_post_freeze(ctx)

        self.assertTrue(result["performed"], result)
        self.assertTrue(result["tap_performed"], result)
        self.assertEqual(adb.taps, [(360, 1078, False)])
        pw = result["post_wait_freeze"]
        self.assertEqual(pw["status"], "stable", pw)
        self.assertEqual(pw["readiness_basis"], "larc_controls", pw)
        self.assertEqual(pw["poll_count"], 3, pw)


if __name__ == "__main__":
    unittest.main()
