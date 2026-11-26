"""Regression tests for deferring L'Arc SS information probe during overseas expedition turns."""
from __future__ import annotations

import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import types
import unittest
from unittest.mock import patch


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

from scripts.ug_parent_one_turn_pipeline import (
    _collect_larc_ss_information,
    _is_larc_overseas_expedition_period,
)


class _DummyRoster:
    def __init__(self) -> None:
        self.needs_ss_information = True
        self.reward_cycles_initialized = False
        self.saved_paths: list[str] = []

    def mark_cycle_initialization_attempted(self) -> None:
        self.reward_cycles_initialized = True

    def save(self, path: str) -> None:
        self.saved_paths.append(path)


class _DummyTrainingParser:
    def __init__(self, roster: _DummyRoster, roster_path: str) -> None:
        self._roster = roster
        self.larc_roster_path = roster_path

    def get_larc_roster(self, _run_id: str) -> _DummyRoster:
        return self._roster


class LarcSSInfoOverseasDeferTests(unittest.TestCase):
    def test_overseas_expedition_period_detection(self) -> None:
        for yr, m, h in (
            (2, 7, "second"), (2, 8, "first"), (2, 8, "second"), (2, 9, "first"), (2, 9, "second"),
            (3, 7, "first"), (3, 7, "second"), (3, 8, "first"), (3, 8, "second"), (3, 9, "first"), (3, 9, "second"),
        ):
                args_top = types.SimpleNamespace(
                    _main_menu_provider_report={"larc_period": {"status": "verified", "value": {"year_index": yr, "month": m, "half": h}}}
                )
                self.assertTrue(_is_larc_overseas_expedition_period(args_top), (yr, m, h))
                args_nested = types.SimpleNamespace(
                    _main_menu_provider_report={
                        "state_context": {"scenario_state": {"larc_period": {"year_index": yr, "month": m, "half": h}}}
                    }
                )
                self.assertTrue(_is_larc_overseas_expedition_period(args_nested), (yr, m, h))

        for yr, m, h, status in (
            (1, 8, "first", "verified"),
            (2, 7, "first", "verified"),
            (2, 10, "first", "verified"),
            (3, 6, "second", "verified"),
            (3, 10, "second", "verified"),
            (2, 9, "second", "unverified"),
        ):
            args = types.SimpleNamespace(
                _main_menu_provider_report={"larc_period": {"status": status, "value": {"year_index": yr, "month": m, "half": h}}}
            )
            self.assertFalse(_is_larc_overseas_expedition_period(args), (yr, m, h, status))

    def test_collect_larc_ss_information_defers_during_classic_sept_second_half(self) -> None:
        with TemporaryDirectory() as tmpdir:
            roster = _DummyRoster()
            parser = _DummyTrainingParser(roster, str(Path(tmpdir) / "roster.json"))
            args = types.SimpleNamespace(
                run_session_id="run_test_overseas",
                turn_index=1,
                adb_port=16384,
                transition_session_json=str(Path(tmpdir) / "transition.json"),
                initialize_larc_reward_cycles=True,
                collect_larc_ss_information=False,
                _main_menu_provider_report={
                    "larc_period": {"status": "verified", "value": {"year_index": 2, "month": 9, "half": "second"}}
                },
            )
            capture_report = {"output_root": tmpdir}
            with (
                patch("core.native_capture_adb.adb_for_capture", side_effect=AssertionError("must not touch adb")),
                patch("core.auto_trainer.larc_ss_information_probe.LarcSSInformationProbe.run", side_effect=AssertionError("must not run probe")),
            ):
                res = _collect_larc_ss_information(args, parser, capture_report)
            self.assertEqual(res["status"], "optional_unavailable")
            self.assertEqual(res["reason"], "overseas_expedition_no_ss_information")
            self.assertTrue(res["returned_to_training"])
            self.assertFalse(res["turn_consumed"])
            self.assertFalse(roster.reward_cycles_initialized)
            self.assertEqual(roster.saved_paths, [])


if __name__ == "__main__":
    unittest.main()
