"""Regression tests for read-only loading retries on Mei outing post-selection wait."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import types
import unittest
from unittest.mock import patch

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

import cv2  # noqa: E402
from core.auto_trainer.executor import REQUIRED_EXECUTION_TOKEN  # noqa: E402
from core.auto_trainer.larc_friend_outing import CHARACTER  # noqa: E402
from core.auto_trainer.larc_outing_route import (  # noqa: E402
    MEI_OUTING_LOADING_EXTRA_WAITS,
    MEI_OUTING_LOADING_WAIT_SECONDS,
    MeiOutingRoute,
)
from core.auto_trainer.larc_outing_session import MeiOutingSession  # noqa: E402
from core.auto_trainer.route_transition_session import RouteTransitionSession  # noqa: E402
from core.training_menu_precheck import TrainingMenuPrecheckResult  # noqa: E402


def _make_png(path: Path, seed: int) -> None:
    frame = np.full((1280, 720, 3), (seed * 17) % 240, dtype=np.uint8)
    cv2.imwrite(str(path), frame)


def _verified_source(path: Path, run_id: str, turn: int) -> dict:
    _make_png(path, turn + 7)
    return {
        "run_session_id": run_id,
        "turn_index": turn,
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


class _FakeAdb:
    supports_in_memory_capture = False

    def __init__(self) -> None:
        self._frame_idx = 0
        self.taps: list[tuple[int, int]] = []

    def connect(self) -> bool:
        return True

    def screenshot(self, path: str) -> str:
        self._frame_idx += 1
        _make_png(Path(path), self._frame_idx)
        return path

    def tap(self, x: int, y: int, *, scaled: bool = False) -> bool:
        self.taps.append((x, y))
        return True


class _FakeReader:
    def __init__(self) -> None:
        self.post_select = False

    def read(self, image) -> dict:
        if self.post_select:
            return {"status": "unknown", "screen": "unknown", "reason": "outing_transition_not_settled"}
        return {
            "status": "accepted",
            "screen": "main_menu",
            "open": (280, 1125),
            "friend_outing_hint": {"status": "accepted", "available": True},
        }


class _ChooserSequenceReader(_FakeReader):
    def __init__(self) -> None:
        super().__init__()
        self._calls = 0

    def read(self, image) -> dict:
        self._calls += 1
        if self._calls == 1:
            return {
                "status": "accepted",
                "screen": "main_menu",
                "open": (280, 1125),
                "friend_outing_hint": {"status": "accepted", "available": True},
            }
        if self._calls == 2:
            self.post_select = True
            return {
                "status": "accepted",
                "screen": "mei_outing_chooser",
                "character": CHARACTER,
                "completed": 0,
                "select": (350, 446),
                "cancel": (180, 1180),
            }
        return {"status": "unknown", "screen": "unknown", "reason": "outing_transition_not_settled"}


class _FakeSafety:
    def analyze(self, path: Path, supplied_text: str | None = None):
        sha = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        return types.SimpleNamespace(
            source_image_sha256=sha,
            text_status="ok",
            visible_text=supplied_text or "",
            dangerous=False,
            errors=[],
        )


class _FakeGate:
    def evaluate(self, safety, decision):
        return types.SimpleNamespace(
            allowed=True,
            reason="ok",
            to_dict=lambda: {"allowed": True, "reason": "ok"},
        )


class _FakePrechecker:
    def check(self, path: Path) -> TrainingMenuPrecheckResult:
        return TrainingMenuPrecheckResult(
            accepted=True,
            reason="training_menu_precheck_passed",
            selected_tab="wisdom",
            evidence={},
            failures=[],
        )


def _menu_snapshot() -> dict:
    return {
        "current_stats": {"speed": 426, "stamina": 239, "power": 344, "guts": 310, "wisdom": 372, "skill_pt": 589},
        "remaining_turns": 3,
        "target_text": "获得7000点协助者积分",
        "vitality": 32,
        "period": {"stage": "junior", "year_index": 1, "month": 11, "half": "second"},
        "mood": "excellent",
    }


def _fake_menu_reader(_path: Path, state_path: Path, *, turn_index: int, scenario: str) -> dict:
    snap = _menu_snapshot()
    state = {
        "turn": turn_index,
        "current_stats": snap["current_stats"],
        "remaining_turns": snap["remaining_turns"],
        "target_text": snap["target_text"],
        "vitality": snap["vitality"],
        "scenario_state": {
            "larc_period": {"status": "verified", "value": snap["period"]},
            "mood": snap["mood"],
        },
    }
    Path(state_path).write_text(json.dumps({"state_context": state, "valid_for_training": True}), encoding="utf-8")
    return {"state_context": state, "valid_for_training": True}


class MeiOutingConnectingWaitTests(unittest.TestCase):
    def _run_route(self, tmp: Path, *, connecting_frames: int) -> tuple[dict, _FakeAdb, list[float]]:
        run_id = "larc_panel_20261125_170211_157c57"
        turn = 19
        seed_source = _verified_source(tmp / "seed.png", run_id, turn)
        session_path = tmp / "session" / "larc_outing_session.json"
        transition_path = tmp / "session" / "route_transition.json"

        MeiOutingSession(
            run_id,
            {
                "schema_version": "larc_outing_session.v1",
                "run_session_id": run_id,
                "unlocked": True,
                "unlock_source": seed_source,
                "completed": 0,
                "source": seed_source,
                "pending": None,
                "receipts": [],
            },
        ).save(session_path)
        route = RouteTransitionSession.new(run_id)
        route.observe("training_menu", source_sha256=seed_source["sha256"])
        route.save(transition_path)

        sleeps: list[float] = []
        adb = _FakeAdb()
        router = MeiOutingRoute(
            adb=adb,
            reader=_ChooserSequenceReader(),
            safety=_FakeSafety(),
            gate=_FakeGate(),
            menu_reader=_fake_menu_reader,
            training_prechecker=_FakePrechecker(),
            sleep_fn=sleeps.append,
        )
        candidate = {
            "completed": 0,
            "source": seed_source,
            "menu_snapshot": _menu_snapshot(),
        }

        probe_calls = 0

        def fake_connecting(_image) -> bool:
            nonlocal probe_calls
            probe_calls += 1
            return probe_calls <= connecting_frames

        def fake_direct_start(_path, *, before_path=None, before_menu=None):
            if probe_calls > connecting_frames:
                return {"screen": "dialogue"}
            return None

        with (
            patch(
                "core.auto_trainer.larc_outing_route.CoordinateConfig",
                return_value=types.SimpleNamespace(point=lambda *_a: (160, 2442)),
            ),
            patch("core.auto_trainer.transition_wait.connecting_overlay_visible", side_effect=fake_connecting),
            patch("core.auto_trainer.transition_wait.career_comic_loading_visible", return_value=False),
            patch("core.auto_trainer.optional_confirmation.direct_action_started", side_effect=fake_direct_start),
        ):
            report = router.run(
                run_id=run_id,
                turn=turn,
                session_path=session_path,
                transition_path=transition_path,
                output_dir=tmp / "outing_out",
                mode="live",
                execute_training_confirm=True,
                confirm_token=REQUIRED_EXECUTION_TOKEN,
                max_turns=1,
                candidate=candidate,
            )
        return report, adb, sleeps

    def test_connecting_overlay_extends_read_only_wait_until_dialogue_starts(self) -> None:
        with TemporaryDirectory() as tmpdir:
            report, adb, sleeps = self._run_route(Path(tmpdir), connecting_frames=10)

        self.assertTrue(report["performed"])
        self.assertTrue(report["is_valid"])
        self.assertEqual(report["reason"], "executed_mei_outing")
        self.assertEqual(report["confirmation_mode"], "disabled")
        self.assertEqual(report["confirmation_loading_extra_waits"], 10)
        self.assertEqual(len(adb.taps), 3)  # back_to_menu, open, select only (never retried)
        self.assertGreaterEqual(sleeps.count(MEI_OUTING_LOADING_WAIT_SECONDS), 10)

    def test_connecting_overlay_exceeding_extra_budget_fails_bounded(self) -> None:
        with TemporaryDirectory() as tmpdir:
            report, adb, _sleeps = self._run_route(Path(tmpdir), connecting_frames=100)

        self.assertFalse(report["performed"])
        self.assertFalse(report["is_valid"])
        self.assertEqual(report["reason"], "mei_outing_confirmation_unverified")
        self.assertEqual(report["confirmation_loading_extra_waits"], MEI_OUTING_LOADING_EXTRA_WAITS)
        self.assertEqual(len(adb.taps), 3)


if __name__ == "__main__":
    unittest.main()
