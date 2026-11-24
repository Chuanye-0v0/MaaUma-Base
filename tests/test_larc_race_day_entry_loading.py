"""Regression tests for transient loading / 连接中 retry handling in _observe_race_day_entry."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import types
import unittest

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
from core.auto_trainer.racing.executor import (  # noqa: E402
    RACE_ENTRY_LOADING_REOBSERVATION_DELAY_SECONDS,
    RaceRouteExecutor,
)
from core.auto_trainer.racing.models import (  # noqa: E402
    RACE_ENTRY_POST_OBSERVATION_RETRIES,
    RaceRoutePolicy,
    RaceRouteSession,
)
from core.auto_trainer.racing.planner import RaceRoutePlanner  # noqa: E402


def _make_png(path: Path, seed_val: int) -> None:
    img = np.full((1280, 720, 3), seed_val % 250, dtype=np.uint8)
    cv2.imwrite(str(path), img)


class _SequenceAdb:
    def __init__(self, frame_seeds: list[int]) -> None:
        self._seeds = list(frame_seeds)
        self.calls = 0

    def screenshot(self, out_path: str) -> str:
        idx = min(self.calls, len(self._seeds) - 1)
        self.calls += 1
        _make_png(Path(out_path), self._seeds[idx])
        return out_path


class _SequenceSafetyAnalyzer:
    def __init__(self, texts: list[str], *, dangerous_flags: list[bool] | None = None) -> None:
        self._texts = list(texts)
        self._dangerous = dangerous_flags or [False] * len(texts)
        self.calls = 0

    def analyze(self, path: Path) -> types.SimpleNamespace:
        idx = min(self.calls, len(self._texts) - 1)
        self.calls += 1
        sha = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        return types.SimpleNamespace(
            text_status="ok",
            text_source="mock_ocr",
            source_image_sha256=sha,
            dangerous=bool(self._dangerous[idx]),
            errors=[],
            visible_text=self._texts[idx],
        )


def _exchange_decision(pre_path: Path) -> dict:
    source_sha = hashlib.sha256(pre_path.read_bytes()).hexdigest()
    return {
        "action": "tap",
        "reason": "enter_race_from_race_day",
        "route_state": "race_day_main_menu",
        "tap": {
            "coordinate_name": "race_day.practice",
            "x": 360,
            "y": 1088,
            "scaled": False,
        },
        "locked_target_verification": {
            "source": "race_day_main_menu.locked_target",
            "action_id": "race_day.practice",
            "source_image_sha256": source_sha,
            "single_use": True,
            "race_kind": "larc_exchange",
        },
        "evidence": {
            "image": str(pre_path),
            "screen_text": {
                "normalized_text": "凯旋门代表交流赛 代表交流赛 实战 技能 海外适应性",
            },
        },
    }


class RaceDayEntryLoadingRetryTests(unittest.TestCase):
    def test_loading_frames_eventually_reach_exchange_detail(self) -> None:
        sleeps: list[float] = []
        with TemporaryDirectory() as tmpdir:
            pre_path = Path(tmpdir) / "pre.png"
            _make_png(pre_path, 99)
            pre_sha = hashlib.sha256(pre_path.read_bytes()).hexdigest()
            decision = _exchange_decision(pre_path)
            session = RaceRouteSession()
            session.stage_locked_target_race_entry(decision)

            # 4 transient loading/hub frames before reaching `赛事详情 — 凯旋门代表交流赛`
            texts = [
                "凯旋门代表交流赛 连接中 代表交流赛",
                "凯旋门代表交流赛 连接中 代表交流赛",
                "凯旋门代表交流赛 代表交流赛",
                "通信中",
                "赛事详情 凯旋门代表交流赛 是否参加比赛 取消 参赛",
            ]
            adb = _SequenceAdb([10, 20, 30, 40, 50])
            analyzer = _SequenceSafetyAnalyzer(texts)
            executor = RaceRouteExecutor(
                adb_factory=lambda: adb,
                safety_analyzer=analyzer,
                sleep_fn=sleeps.append,
            )

            orig_looks_like_list = RaceRoutePlanner._looks_like_race_list_postcondition
            orig_exchange = RaceRoutePlanner.larc_exchange_entry_postcondition
            try:
                RaceRoutePlanner._looks_like_race_list_postcondition = staticmethod(lambda img, visible_text=None: False)

                def _fake_exchange(self, post_path, *, entry_decision, session, visible_text=None):
                    if visible_text and "是否参加比赛" in visible_text:
                        return {"route_state": "race_detail", "action": "tap", "reason": "confirm_race_entry"}
                    return None

                RaceRoutePlanner.larc_exchange_entry_postcondition = _fake_exchange
                src = Path(tmpdir) / "race_001.png"
                final, obs = executor._observe_race_day_entry(
                    adb,
                    src,
                    pre_sha256=pre_sha,
                    decision=decision,
                    policy=RaceRoutePolicy(scenario="larc"),
                    session=session,
                )
            finally:
                RaceRoutePlanner._looks_like_race_list_postcondition = orig_looks_like_list
                RaceRoutePlanner.larc_exchange_entry_postcondition = orig_exchange

        self.assertEqual(final["screen"], "race_detail")
        self.assertEqual(final["reason"], "current_exchange_successor_verified")
        self.assertEqual(len(obs), 5)
        self.assertEqual(adb.calls, 5)
        self.assertTrue(all(d == RACE_ENTRY_LOADING_REOBSERVATION_DELAY_SECONDS for d in sleeps))
        self.assertEqual(len(sleeps), 4)

    def test_unrecognized_non_loading_screen_stops_within_normal_budget(self) -> None:
        sleeps: list[float] = []
        with TemporaryDirectory() as tmpdir:
            pre_path = Path(tmpdir) / "pre.png"
            _make_png(pre_path, 99)
            pre_sha = hashlib.sha256(pre_path.read_bytes()).hexdigest()
            decision = _exchange_decision(pre_path)
            session = RaceRouteSession()
            session.stage_locked_target_race_entry(decision)

            adb = _SequenceAdb([11, 22, 33, 44, 55])
            analyzer = _SequenceSafetyAnalyzer(["无关页面 未知界面"] * 5)
            executor = RaceRouteExecutor(
                adb_factory=lambda: adb,
                safety_analyzer=analyzer,
                sleep_fn=sleeps.append,
            )

            orig_looks_like_list = RaceRoutePlanner._looks_like_race_list_postcondition
            orig_exchange = RaceRoutePlanner.larc_exchange_entry_postcondition
            try:
                RaceRoutePlanner._looks_like_race_list_postcondition = staticmethod(lambda img, visible_text=None: False)
                RaceRoutePlanner.larc_exchange_entry_postcondition = lambda self, *a, **kw: None
                src = Path(tmpdir) / "race_001.png"
                final, obs = executor._observe_race_day_entry(
                    adb,
                    src,
                    pre_sha256=pre_sha,
                    decision=decision,
                    policy=RaceRoutePolicy(scenario="larc"),
                    session=session,
                )
            finally:
                RaceRoutePlanner._looks_like_race_list_postcondition = orig_looks_like_list
                RaceRoutePlanner.larc_exchange_entry_postcondition = orig_exchange

        self.assertEqual(final["screen"], "unknown")
        self.assertEqual(final["reason"], "post_page_unrecognized")
        self.assertEqual(len(obs), RACE_ENTRY_POST_OBSERVATION_RETRIES + 1)

    def test_account_conflict_breaks_immediately(self) -> None:
        sleeps: list[float] = []
        with TemporaryDirectory() as tmpdir:
            pre_path = Path(tmpdir) / "pre.png"
            _make_png(pre_path, 99)
            pre_sha = hashlib.sha256(pre_path.read_bytes()).hexdigest()
            decision = _exchange_decision(pre_path)
            session = RaceRouteSession()
            session.stage_locked_target_race_entry(decision)

            adb = _SequenceAdb([11, 22])
            analyzer = _SequenceSafetyAnalyzer(["该账号已在其他设备登录 请重新登录"])
            executor = RaceRouteExecutor(
                adb_factory=lambda: adb,
                safety_analyzer=analyzer,
                sleep_fn=sleeps.append,
            )
            src = Path(tmpdir) / "race_001.png"
            final, obs = executor._observe_race_day_entry(
                adb,
                src,
                pre_sha256=pre_sha,
                decision=decision,
                policy=RaceRoutePolicy(scenario="larc"),
                session=session,
            )
        self.assertEqual(final["screen"], "unknown")
        self.assertEqual(final["reason"], "unsafe_post_page")
        self.assertEqual(len(obs), 1)
        self.assertEqual(sleeps, [])


if __name__ == "__main__":
    unittest.main()
