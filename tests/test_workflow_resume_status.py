"""Unit tests for read-only workflow snapshot can_resume and resume_reason."""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACER_BACKEND = Path(os.environ.get("MAAUMA_PACER_BACKEND_DIR") or (REPO_ROOT / "pacer" / "backend"))
ENDPOINT = "127.0.0.1:16384"


def _ensure_paths() -> None:
    resolved = str(PACER_BACKEND.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)
    if "config.settings" not in sys.modules:
        try:
            import config.settings  # noqa: F401
        except ImportError:
            cfg_mod = types.ModuleType("config")
            cfg_set = types.ModuleType("config.settings")
            cfg_set.ADB_HOST = "127.0.0.1"
            cfg_set.ADB_ENDPOINT = ENDPOINT
            cfg_set.ADB_PORT = 16384
            cfg_set.ADB_PATH = ""
            cfg_set.SWIPE_DELAY = 0.2
            cfg_set.TAP_DELAY = 0.2
            cfg_set.ASSETS_DIR = PACER_BACKEND / "assets"
            cfg_mod.settings = cfg_set
            sys.modules["config"] = cfg_mod
            sys.modules["config.settings"] = cfg_set


_ensure_paths()

from core.auto_trainer.cut_farming import atomic_json  # noqa: E402
from core.career_panel_presets import RUN_MARKER, RUN_SCHEMA  # noqa: E402
from core.career_workflow import templates  # noqa: E402
from scripts import career_workflow_host  # noqa: E402
from scripts.career_workflow_runtime import DOCUMENT, WorkflowState  # noqa: E402


def _template_by_id(template_id: str) -> dict:
    doc = next(row for row in templates() if row.get("template_id") == template_id)
    doc["policies"]["default"].setdefault("career_options", {})["setup"] = {
        "character_id": "1098",
        "costume_id": "109801",
    }
    return doc


def _write_pointer(root: Path, workflow_root: Path) -> None:
    pointer = career_workflow_host._endpoint_pointer(root, ENDPOINT)
    atomic_json(pointer, {"root": str(workflow_root), "endpoint": ENDPOINT})


def _write_attempt_markers(attempt_root: Path) -> None:
    atomic_json(
        attempt_root / RUN_MARKER,
        {
            "schema_version": RUN_SCHEMA,
            "source": "private_pacer_center",
            "mode": "career",
            "run_id": attempt_root.name,
            "endpoint": ENDPOINT,
            "started_at": "2026-11-25 17:02:11",
        },
    )
    atomic_json(
        attempt_root / "progress.json",
        {
            "run_session_id": attempt_root.name,
            "device": ENDPOINT,
            "actions_completed": 5,
            "next_turn": 6,
        },
    )


def _write_ace_batch(
    host_root: Path,
    batch_name: str,
    *,
    phase: str,
    outcome: str | None,
    attempt_status: str = "running",
    batch_status: str = "stopped",
) -> tuple[Path, Path]:
    batch_root = host_root / "runs" / batch_name
    attempt_root = batch_root / "attempts" / "larc_panel_20261125_170211_a1"
    _write_attempt_markers(attempt_root)
    doc = _template_by_id("ace_career_cycle")
    atomic_json(batch_root / DOCUMENT, doc)
    store = WorkflowState(batch_root, doc, endpoint=ENDPOINT)
    store.data["status"] = "stopped"
    store.bind(attempt_root)
    cut_data = {
        "schema_version": "uma.cut_farming_session.v1",
        "status": batch_status,
        "config": {
            "device_port": 16384,
            "strategy": "baseline",
            "character_id": "validation",
            "validation_mode": True,
            "validation_character_name": "小林历奇",
            "max_attempts": 10,
            "max_tp_items": 0,
            "max_carrots": 0,
        },
        "pending_action": None,
        "tp_refill_pending": None,
        "ss_sieve_cycle": {"phase": phase},
        "attempts": [
            {
                "attempt_index": 1,
                "run_id": attempt_root.name,
                "run_root": str(attempt_root),
                "status": attempt_status,
                **({"ss_sieve_outcome": outcome} if outcome is not None else {}),
            }
        ],
    }
    cut_path = batch_root / "cut_farming.json"
    atomic_json(cut_path, cut_data)
    _write_pointer(host_root, batch_root)
    return batch_root, cut_path


class WorkflowResumeStatusTests(unittest.TestCase):
    def test_ace_cycle_career_failed_reports_not_resumable_and_keeps_file_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host_root = Path(tmp)
            _, cut_path = _write_ace_batch(
                host_root,
                "cut_pacer_20261125_170211_6f019e",
                phase="career_failed",
                outcome="failed",
                attempt_status="failed",
            )
            before_hash = hashlib.sha256(cut_path.read_bytes()).hexdigest()

            host = types.SimpleNamespace(root=host_root, endpoint=ENDPOINT)
            snap = career_workflow_host.snapshot(host)

            self.assertIsNotNone(snap)
            self.assertIs(snap["can_resume"], False)
            self.assertTrue(snap["resume_reason"])
            self.assertEqual(hashlib.sha256(cut_path.read_bytes()).hexdigest(), before_hash)

    def test_ace_cycle_growing_resumable_reports_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host_root = Path(tmp)
            _, cut_path = _write_ace_batch(
                host_root,
                "cut_pacer_20261125_174500_ok0001",
                phase="growing",
                outcome=None,
                attempt_status="running",
            )
            before_hash = hashlib.sha256(cut_path.read_bytes()).hexdigest()

            host = types.SimpleNamespace(root=host_root, endpoint=ENDPOINT)
            snap = career_workflow_host.snapshot(host)

            self.assertIsNotNone(snap)
            self.assertIs(snap["can_resume"], True, msg=f"resume_reason={snap.get('resume_reason')!r}")
            self.assertIsNone(snap["resume_reason"])
            self.assertEqual(hashlib.sha256(cut_path.read_bytes()).hexdigest(), before_hash)

    def test_ordinary_career_completed_reports_not_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host_root = Path(tmp)
            run_root = host_root / "runs" / "larc_panel_20261125_120000_done"
            _write_attempt_markers(run_root)
            doc = _template_by_id("direct_career")
            atomic_json(run_root / DOCUMENT, doc)
            store = WorkflowState(run_root, doc, endpoint=ENDPOINT)
            store.bind(run_root)
            store.choose("completed")
            store.data["status"] = "completed"
            store.save()
            _write_pointer(host_root, run_root)

            host = types.SimpleNamespace(root=host_root, endpoint=ENDPOINT)
            snap = career_workflow_host.snapshot(host)

            self.assertIsNotNone(snap)
            self.assertIs(snap["can_resume"], False)
            self.assertIn("已完成", snap["resume_reason"])

    def test_ordinary_career_mid_run_stopped_reports_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host_root = Path(tmp)
            run_root = host_root / "runs" / "larc_panel_20261125_171000_mid"
            _write_attempt_markers(run_root)
            doc = _template_by_id("direct_career")
            atomic_json(run_root / DOCUMENT, doc)
            store = WorkflowState(run_root, doc, endpoint=ENDPOINT)
            store.bind(run_root)
            store.data["status"] = "stopped"
            store.save()
            _write_pointer(host_root, run_root)

            host = types.SimpleNamespace(root=host_root, endpoint=ENDPOINT)
            snap = career_workflow_host.snapshot(host)

            self.assertIsNotNone(snap)
            self.assertIs(snap["can_resume"], True, msg=f"resume_reason={snap.get('resume_reason')!r}")
            self.assertIsNone(snap["resume_reason"])


if __name__ == "__main__":
    unittest.main()
