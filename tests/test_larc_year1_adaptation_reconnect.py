"""Unit tests for Year-1 adaptation fallback and live-takeover cache recheck."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cv2
import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
PACER_BACKEND = Path(os.environ.get("MAAUMA_PACER_BACKEND_DIR") or (REPO_ROOT / "pacer" / "backend"))


def _ensure_pacer_backend_path() -> None:
    resolved = str(PACER_BACKEND.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)


def _period(year_index: int, month: int, half: str, *, pre_debut: bool = False) -> dict:
    stage = {1: "junior", 2: "classic", 3: "senior"}[year_index]
    value: dict = {"stage": stage, "year_index": year_index, "month": month, "half": half}
    if pre_debut:
        value["pre_debut"] = True
    return {"status": "verified", "value": value}


class TestLarcYear1AdaptationReconnect(unittest.TestCase):
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

    def test_select_larc_adaptation_phase_year1_december_and_classic_races(self) -> None:
        from scripts.larc_autonomy_adaptation import select_larc_adaptation_phase

        self.assertEqual(select_larc_adaptation_phase(_period(1, 12, "first")), "first_exchange")
        self.assertEqual(select_larc_adaptation_phase(_period(1, 12, "second")), "first_exchange")
        self.assertIsNone(select_larc_adaptation_phase(_period(1, 11, "second")))
        self.assertIsNone(select_larc_adaptation_phase(_period(1, 12, "first", pre_debut=True)))
        self.assertEqual(select_larc_adaptation_phase(_period(2, 6, "second")), "first_expedition")

        v3_goal = SimpleNamespace(
            scenario="larc",
            larc_policy={
                "variant": "baseline",
                "revision": "2.3",
                "overseas_purchase_v2": True,
                "overseas_purchase_v3": True,
                "strategy_v4": True,
                "strategy_refinement": True,
                "training_balance_v2": True,
            },
        )
        self.assertIsNone(select_larc_adaptation_phase(_period(2, 6, "second"), goal=v3_goal))
        self.assertEqual(select_larc_adaptation_phase(_period(1, 12, "second"), goal=v3_goal), "first_exchange")

        self.assertEqual(
            select_larc_adaptation_phase(_period(2, 9, "first"), race_target_id="larc_niel_participation", race_day=True),
            "first_overseas_race",
        )
        self.assertEqual(
            select_larc_adaptation_phase(_period(2, 10, "first"), race_target_id="larc_arc_participation", race_day=True),
            "first_arc",
        )
        self.assertEqual(
            select_larc_adaptation_phase(_period(3, 9, "first"), race_target_id="larc_foy_participation", race_day=True),
            "foy_prelude",
        )
        self.assertEqual(
            select_larc_adaptation_phase(_period(3, 10, "first"), race_target_id="larc_final_arc_top5", race_day=True),
            "final_arc",
        )

    def test_is_larc_exchange_hub_and_race_day_context_tolerate_leading_noise(self) -> None:
        from core.auto_trainer import larc_race_day_context as rdc
        from scripts.larc_autonomous_run import _is_larc_exchange_hub

        noisy_hub_text = "养成\n凯旋门代表交流赛\n能力\n初学者\n海外适应性\n300"
        debut_race_text = "养成\n初级出道前\n参加初级新人战\n比赛日\n距离代表交流赛还剩12回合\n海外适应性\n100"

        self.assertTrue(
            _is_larc_exchange_hub({"larc_race_day": {"verified": True}, "screen_text": {"normalized_text": noisy_hub_text}})
        )
        self.assertFalse(
            _is_larc_exchange_hub({"larc_race_day": {"verified": True}, "screen_text": {"normalized_text": debut_race_text}})
        )
        self.assertFalse(
            _is_larc_exchange_hub({"larc_race_day": {"verified": False}, "screen_text": {"normalized_text": noisy_hub_text}})
        )

        with TemporaryDirectory() as tmp:
            frame_path = Path(tmp) / "race_day.png"
            blank = np.zeros((1280, 720, 3), dtype=np.uint8)
            self.assertTrue(cv2.imwrite(str(frame_path), blank))
            sha = hashlib.sha256(frame_path.read_bytes()).hexdigest()

            with (
                patch.object(rdc, "is_larc_race_day", return_value=True),
                patch.object(rdc._CALENDAR_REGION_READER, "read_line", return_value=("", 0.0)),
            ):
                hub_ctx = rdc.read_larc_race_day_context(frame_path, source_sha256=sha, visible_text=noisy_hub_text)
                ordinary_ctx = rdc.read_larc_race_day_context(frame_path, source_sha256=sha, visible_text=debut_race_text)

            self.assertEqual(hub_ctx["period"]["status"], "verified")
            self.assertEqual(
                hub_ctx["period"]["value"],
                {"stage": "junior", "year_index": 1, "month": 12, "half": "second"},
            )
            self.assertEqual(hub_ctx["period"]["evidence"]["milestone"], "first_exchange")
            self.assertNotEqual(ordinary_ctx["period"]["status"], "verified")

    def test_clear_stale_adaptations_for_year1_takeover_archives_completed_and_preserves_pending(self) -> None:
        from core.auto_trainer.larc_adaptation import LarcAdaptationSession, build_adaptation_plan
        from scripts.larc_autonomous_run import _clear_stale_adaptations_for_year1_takeover

        sha = "a" * 64
        run_id = "larc_panel_test_run"
        device = "127.0.0.1:16384"
        exchange_plan = build_adaptation_plan(
            {"levels": [1, 1, 1, 1, 0, 0, 0, 0, 0, 0], "points": 300, "pending_cost": 0},
            phase="first_exchange",
        )
        arc_plan = build_adaptation_plan(
            {"levels": [2, 2, 1, 1, 1, 1, 1, 1, 0, 0], "points": 800, "pending_cost": 0},
            phase="first_arc",
        )

        # Case 1: completed/returned stale sessions + ready index are archived/cleared once.
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / run_id
            session_dir = root / "session"
            session_dir.mkdir(parents=True)

            LarcAdaptationSession(
                run_session_id=run_id,
                path=session_dir / "larc_adaptation_first_exchange.json",
                plan=exchange_plan,
                source_sha256=sha,
                stage="complete",
                submitted_source_sha256=sha,
                returned=True,
                phase="first_exchange",
            ).save()
            LarcAdaptationSession(
                run_session_id=run_id,
                path=session_dir / "larc_adaptation_first_arc.json",
                plan=arc_plan,
                source_sha256=sha,
                stage="complete",
                submitted_source_sha256=sha,
                returned=True,
                phase="first_arc",
            ).save()

            index_path = session_dir / "larc_adaptation_ready.json"
            index_path.write_text(
                json.dumps(
                    {
                        "run_session_id": run_id,
                        "device": device,
                        "phases": {"first_exchange": str(root / "old/report.json")},
                        "partial_phases": {"first_arc": str(root / "old/arc_report.json")},
                    }
                ),
                encoding="utf-8",
            )

            ctx = SimpleNamespace(
                root=root,
                run_id=run_id,
                progress={"device": device},
                ready_adaptations={"first_exchange": "larc_adaptation_verified"},
                live_takeover_year1_adapt_recheck=True,
            )
            summary: dict = {}
            saves: list[bool] = []

            cleared = _clear_stale_adaptations_for_year1_takeover(ctx, summary, lambda: saves.append(True))

            self.assertEqual(cleared, ["first_arc", "first_exchange"])
            self.assertFalse(ctx.live_takeover_year1_adapt_recheck)
            self.assertEqual(ctx.ready_adaptations, {})
            self.assertFalse((session_dir / "larc_adaptation_first_exchange.json").exists())
            self.assertFalse((session_dir / "larc_adaptation_first_arc.json").exists())

            archive_dir = Path(summary["live_takeover_year1_adaptation_recheck"]["archive"])
            self.assertTrue((archive_dir / "larc_adaptation_first_exchange.json").is_file())
            self.assertTrue((archive_dir / "larc_adaptation_first_arc.json").is_file())
            self.assertEqual(
                summary["live_takeover_year1_adaptation_recheck"]["cleared_phases"],
                ["first_arc", "first_exchange"],
            )
            self.assertEqual(len(saves), 1)

            updated_index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(updated_index["phases"], {})
            self.assertEqual(updated_index["partial_phases"], {})

        # Case 2: unresolved in-flight transactions must NEVER be discarded.
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / run_id
            session_dir = root / "session"
            session_dir.mkdir(parents=True)

            LarcAdaptationSession(
                run_session_id=run_id,
                path=session_dir / "larc_adaptation_first_exchange.json",
                plan=exchange_plan,
                source_sha256=sha,
                stage="submission_pending",
                submitted_source_sha256=sha,
                returned=False,
                phase="first_exchange",
            ).save()
            expedition_plan = build_adaptation_plan(
                {"levels": [1, 1, 1, 1, 0, 0, 0, 0, 0, 0], "points": 300, "pending_cost": 0},
                phase="first_expedition",
            )
            LarcAdaptationSession(
                run_session_id=run_id,
                path=session_dir / "larc_adaptation_first_expedition.json",
                plan=expedition_plan,
                source_sha256=sha,
                stage="awaiting_result",
                submitted_source_sha256=sha,
                returned=False,
                phase="first_expedition",
            ).save()

            index_path = session_dir / "larc_adaptation_ready.json"
            original_index = {
                "run_session_id": run_id,
                "device": device,
                "phases": {"first_exchange": str(root / "old/report.json")},
            }
            index_path.write_text(json.dumps(original_index), encoding="utf-8")

            ctx = SimpleNamespace(
                root=root,
                run_id=run_id,
                progress={"device": device},
                ready_adaptations={"first_exchange": "larc_adaptation_verified"},
                live_takeover_year1_adapt_recheck=True,
            )
            summary = {}
            saves = []

            cleared = _clear_stale_adaptations_for_year1_takeover(ctx, summary, lambda: saves.append(True))

            self.assertEqual(cleared, [])
            self.assertFalse(ctx.live_takeover_year1_adapt_recheck)
            self.assertTrue((session_dir / "larc_adaptation_first_exchange.json").is_file())
            self.assertTrue((session_dir / "larc_adaptation_first_expedition.json").is_file())
            self.assertEqual(json.loads(index_path.read_text(encoding="utf-8")), original_index)
            self.assertNotIn("live_takeover_year1_adaptation_recheck", summary)
            self.assertEqual(saves, [])


if __name__ == "__main__":
    unittest.main()
