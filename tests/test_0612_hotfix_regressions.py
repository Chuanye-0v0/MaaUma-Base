"""Regression tests for MaaUma 0.6.12 hotfixes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
PACER_BACKEND = Path(os.environ.get("MAAUMA_PACER_BACKEND_DIR") or (REPO_ROOT / "pacer" / "backend"))


def _ensure_pacer_backend_path() -> None:
    resolved = str(PACER_BACKEND.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)
    if "config.settings" not in sys.modules:
        try:
            import config.settings  # noqa: F401
        except ImportError:
            cfg_mod = types.ModuleType("config")
            cfg_set = types.ModuleType("config.settings")
            cfg_mod.settings = cfg_set
            sys.modules["config"] = cfg_mod
            sys.modules["config.settings"] = cfg_set
    cfg_set = sys.modules["config.settings"]
    defaults = {
        "ADB_HOST": "127.0.0.1",
        "ADB_ENDPOINT": "127.0.0.1:16384",
        "ADB_PORT": 16384,
        "ADB_PATH": "",
        "SWIPE_DELAY": 0.2,
        "TAP_DELAY": 0.2,
        "ASSETS_DIR": PACER_BACKEND / "assets",
    }
    for attr, val in defaults.items():
        if not hasattr(cfg_set, attr):
            setattr(cfg_set, attr, val)


def _make_full_page_clock_image() -> np.ndarray:
    img = np.full((1280, 720, 3), 240, dtype=np.uint8)
    # Green header & retry button, white text & cancel button, pink clock area.
    img[35:90, 20:700] = (0, 200, 0)
    img[490:595, 35:690] = (240, 240, 240)
    img[850:935, 180:267] = (200, 100, 220)
    img[1150:1210, 65:337] = (240, 240, 240)
    img[1150:1210, 385:650] = (0, 200, 0)
    return img


def _crop_reader(before_reading: tuple[str, float], after_reading: tuple[str, float]):
    def _read(crop: np.ndarray) -> tuple[str, float]:
        width = crop.shape[1]
        if width == 73:
            return before_reading
        if width == 76:
            return after_reading
        return ("", 0.0)

    return _read


def _create_minimal_direct_career_run(runs_dir: Path) -> Path:
    from core.auto_trainer.route_transition_session import RouteTransitionSession
    from core.auto_trainer.skip_session import RunSkipSession

    run_id = "larc_panel_261126091500_ab12"
    run_root = runs_dir / run_id
    (run_root / "session").mkdir(parents=True, exist_ok=True)
    goal = {
        "schema_version": "ug_parent_goal.v1",
        "scenario": "larc",
        "goal": "ug_parent",
        "total_turns": 72,
        "target_rank": "UG",
        "target_stats": {"speed": 1550, "stamina": 1200, "power": 1200, "guts": 1100, "wisdom": 1200},
        "risk_tolerance": "low",
        "training_failure_rate_limit": 25.0,
        "larc_policy": {"variant": "baseline", "revision": "2.3"},
    }
    (run_root / "goal.json").write_text(json.dumps(goal), encoding="utf-8")
    (run_root / "progress.json").write_text(
        json.dumps(
            {
                "run_session_id": run_id,
                "device": "127.0.0.1:16384",
                "actions_completed": 0,
                "next_turn": 1,
            }
        ),
        encoding="utf-8",
    )
    RouteTransitionSession.new(run_id).save(run_root / "session/transition_session.json")
    RunSkipSession.new(run_id).save(run_root / "session/skip_session.json")
    (run_root / "session/larc_member_roster.json").write_text(
        json.dumps(
            {
                "schema_version": "larc_member_roster.v1",
                "run_session_id": run_id,
                "last_observed_turn": -1,
                "members": [],
                "information_scan": {"status": "never_scanned"},
                "ss_executions": [],
                "last_ss_preview": None,
                "ss_execution_participants": [],
                "complete_roster_verified": False,
                "decision_safe": False,
            }
        ),
        encoding="utf-8",
    )
    return run_root


class Test0612HotfixRegressions(unittest.TestCase):
    def setUp(self) -> None:
        _ensure_pacer_backend_path()
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)
        self._tmp.cleanup()

    def test_direct_career_turn_start_without_ace_cycle_binding(self) -> None:
        from scripts import larc_autonomous_run as lar

        runs_dir = self.tmp / "pacer/backend/runs"
        run_root = _create_minimal_direct_career_run(runs_dir)
        self.assertFalse((run_root / "session/ace_cycle_binding.json").exists())

        source_path = run_root / "entry_source.json"
        source_path.write_text("{}", encoding="utf-8")
        provider_doc = {
            "valid_for_training": True,
            "larc_period": {
                "status": "verified",
                "value": {"stage": "classic", "year_index": 2, "month": 1, "half": "first"},
            },
            "state_context": {
                "turn": 1,
                "current_stats": {"speed": 200, "stamina": 200, "power": 200, "guts": 200, "wisdom": 200},
                "remaining_turns": 10,
                "target_text": "全国德比",
                "vitality": 100.0,
                "scenario_state": {
                    "scenario": "larc",
                    "larc_period": {
                        "status": "verified",
                        "value": {"stage": "classic", "year_index": 2, "month": 1, "half": "first"},
                    },
                },
            },
        }
        args = lar.parse_args(
            [
                "--run-root",
                str(run_root),
                "--adb-port",
                "16384",
                "--max-actions",
                "5",
                "--stop-after-year",
                "1",
                "--execute",
                "--confirm-token",
                lar.CONFIRM_TOKEN,
            ]
        )

        with (
            patch("scripts.larc_live_takeover.enabled", return_value=False),
            patch.object(lar, "_make_transition_probe", return_value=None),
            patch.object(lar, "_route_to_menu", return_value=(source_path, "ready_main_menu")),
            patch.object(lar, "provide_main_menu_state", return_value=provider_doc),
            patch("core.auto_trainer.larc_mei_unlock_policy.observe_mei_gamble_menu", return_value=None),
            patch("core.auto_trainer.cut_stop_diagnostic.save_ordinary_stop_diagnostic", return_value={"status": "unavailable"}),
            patch("core.auto_trainer.recognition_failure_archive.archive_abnormal_stop", return_value={}),
            patch("core.auto_trainer.recognition_failure_archive.archive_segment_failure", return_value={}),
        ):
            summary, code = lar._run(args)

        self.assertEqual(code, 0, summary)
        self.assertEqual(summary["status"], "year_checkpoint", summary)
        self.assertEqual(summary["stop_reason"], "year_checkpoint", summary)

    def test_execution_exception_persists_runner_error_and_preserves_relative_filename(self) -> None:
        from desktop import native_stop_exporter, pacer_stop_context
        from scripts import larc_autonomous_run as lar

        pacer_root = self.tmp / "pacer/backend"
        run_root = _create_minimal_direct_career_run(pacer_root / "runs")
        missing_target = run_root / "session/ace_cycle_binding.json"
        source_path = run_root / "entry_source.json"
        source_path.write_text("{}", encoding="utf-8")

        args = lar.parse_args(
            [
                "--run-root",
                str(run_root),
                "--adb-port",
                "16384",
                "--max-actions",
                "5",
                "--execute",
                "--confirm-token",
                lar.CONFIRM_TOKEN,
            ]
        )

        def boom(*_a, **_kw):
            raise FileNotFoundError(2, "No such file or directory", str(missing_target))

        with (
            patch("scripts.larc_live_takeover.enabled", return_value=False),
            patch.object(lar, "_make_transition_probe", return_value=None),
            patch.object(lar, "_route_to_menu", return_value=(source_path, "ready_main_menu")),
            patch.object(lar, "provide_main_menu_state", side_effect=boom),
            patch("core.auto_trainer.cut_stop_diagnostic.save_ordinary_stop_diagnostic", return_value={"status": "unavailable"}),
            patch("core.auto_trainer.recognition_failure_archive.archive_abnormal_stop", return_value={}),
            patch("core.auto_trainer.recognition_failure_archive.archive_segment_failure", return_value={}),
        ):
            summary, code = lar._run(args)

        self.assertEqual(code, 2)
        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(summary["stop_reason"], "execution_exception")
        self.assertTrue(summary["error"].startswith("FileNotFoundError@"), summary["error"])
        self.assertIn("[RUN]/session/ace_cycle_binding.json", summary["error"])

        exported = native_stop_exporter._text(summary["error"], run_root, None)
        self.assertIn("[RUN]/session/ace_cycle_binding.json", exported)
        self.assertNotIn(str(self.tmp), exported)

        err_file = run_root / "runner_error.json"
        self.assertTrue(err_file.is_file())
        err_doc = json.loads(err_file.read_text(encoding="utf-8"))
        self.assertEqual(err_doc["reason"], "execution_exception")
        self.assertEqual(err_doc["error_type"], "FileNotFoundError")
        self.assertGreater(err_doc["line"], 0)

        outer = {
            "mode": "career",
            "endpoint": "127.0.0.1:16384",
            "run_root": str(run_root),
            "control": {
                "state": "stopped",
                "last_error": "execution_exception",
                "run_id": run_root.name,
                "runtime": {"step": "turn_start"},
                "last_result": {"outcome": "stopped", "reason": "execution_exception"},
            },
            "cut": {},
            "record": {
                "run_session_id": run_root.name,
                "device": "127.0.0.1:16384",
                "summary_status": "stopped",
                "stop_reason": "execution_exception",
                "error": summary["error"],
            },
        }
        pacer_stop_context.annotate(outer, pacer_root)
        ctx = outer["current_stop_context"]
        self.assertEqual(ctx["reason"], "execution_exception")
        self.assertIn("runner_error", ctx)
        self.assertEqual(ctx["runner_error"]["error_type"], "FileNotFoundError")
        self.assertIn("FileNotFoundError", ctx["message"])

    def test_race_clock_retry_read_retry_confidence_guards(self) -> None:
        from core.auto_trainer import race_clock_retry as rcr

        image = _make_full_page_clock_image()
        self.assertTrue(rcr._full_page(image))
        base_text = "重新挑战 第2名 是否使用闹钟进行重新挑战 重新挑战次数还剩2次"
        free_text = base_text + " 每天1次免费机会"

        with patch("core.auto_trainer.safe_advance.SafeAdvancePlanner._final_loss_recovery_decision", return_value=None):
            # 1. Paid retry: 251 (0.83645) -> 250 (0.992) must be accepted.
            paid = rcr.read_retry(image, base_text, reader=_crop_reader(("251", 0.83645), ("250", 0.992)))
            self.assertIsNotNone(paid)
            self.assertEqual(paid["before"], 251)
            self.assertEqual(paid["after"], 250)
            self.assertFalse(paid["free"])

            # 2. Explicit daily free: 12 (0.80) -> 12 (0.96) must be accepted.
            explicit = rcr.read_retry(image, free_text, reader=_crop_reader(("12", 0.80), ("12", 0.96)))
            self.assertIsNotNone(explicit)
            self.assertEqual(explicit["before"], 12)
            self.assertEqual(explicit["after"], 12)
            self.assertTrue(explicit["free"])

            # 3. Inferred zero-cost (no free banner): 12 (0.85) -> 12 (0.99) must still be rejected.
            inferred_low = rcr.read_retry(image, base_text, reader=_crop_reader(("12", 0.85), ("12", 0.99)))
            self.assertIsNone(inferred_low)

            # 4. Inferred zero-cost with >= 0.90 on both sides remains accepted.
            inferred_ok = rcr.read_retry(image, base_text, reader=_crop_reader(("12", 0.91), ("12", 0.99)))
            self.assertIsNotNone(inferred_ok)
            self.assertTrue(inferred_ok["free"])

            # 5. Reject min_conf < 0.75 even on 251 -> 250.
            too_low = rcr.read_retry(image, base_text, reader=_crop_reader(("251", 0.74), ("250", 0.99)))
            self.assertIsNone(too_low)

            # 6. Reject max_conf < 0.90 even on 251 -> 250.
            both_low = rcr.read_retry(image, base_text, reader=_crop_reader(("251", 0.85), ("250", 0.89)))
            self.assertIsNone(both_low)

            # 7. Reject invalid stock delta 251 -> 249.
            bad_delta = rcr.read_retry(image, base_text, reader=_crop_reader(("251", 0.85), ("249", 0.99)))
            self.assertIsNone(bad_delta)


if __name__ == "__main__":
    unittest.main()
