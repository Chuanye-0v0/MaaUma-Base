"""Regression tests for 0.6.11 ACL fallbacks, runner_error diagnostics, long-path I/O, and compact run layout."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
PACER_BACKEND = ROOT / "pacer/backend"
if str(PACER_BACKEND) not in sys.path:
    sys.path.insert(0, str(PACER_BACKEND))


class _DummyParser:
    def parse_for_scenario(self, image_path, *, scenario=None):
        tab = Path(image_path).stem
        return {
            "training_type": tab if tab in {"speed", "stamina", "power", "guts", "wisdom"} else "speed",
            "stats_gain": {tab: 15, "skill_pt": 5} if tab in {"speed", "stamina", "power", "guts", "wisdom"} else {"speed": 15},
            "base_gain": {tab: 15} if tab in {"speed", "stamina", "power", "guts", "wisdom"} else {},
            "bonus_gain": {},
            "total_gain": {tab: 15, "skill_pt": 5} if tab in {"speed", "stamina", "power", "guts", "wisdom"} else {"speed": 15},
            "stats_gain_total": {tab: 15, "skill_pt": 5} if tab in {"speed", "stamina", "power", "guts", "wisdom"} else {"speed": 15},
            "stats_gain_ocr": {
                tab if tab in {"speed", "stamina", "power", "guts", "wisdom"} else "speed": {
                    "status": "accepted",
                    "value": 15,
                    "components": [15],
                }
            },
            "failure_rate": 0,
            "confidence": 0.9,
            "unknown_fields": [],
        }


def _stat_reader(image):
    values = {"speed": 500, "stamina": 400, "power": 450, "guts": 350, "wisdom": 420, "skill_pt": 120}
    return {
        "status": "ok",
        "reason": "test_ok",
        "values": values,
        "fields": {k: {"status": "accepted", "value": v} for k, v in values.items()},
    }


def _rmdir_long(path: Path) -> None:
    if not path.exists():
        return
    lp = str(path.resolve())
    if os.name == "nt" and not lp.startswith("\\\\?\\"):
        lp = "\\\\?\\" + lp
    shutil.rmtree(lp, ignore_errors=True)


class Test0611LongPathAclAndRunnerError(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        _rmdir_long(self.tmp)
        self._tmp.cleanup()

    def test_unicode_and_ascii_long_path_opencv_roundtrip(self) -> None:
        from core.cut_portable_runtime import enable_unicode_image_paths
        from core.frame_io import io_path

        enable_unicode_image_paths(force=True)
        sample = np.full((64, 64, 3), 42, dtype=np.uint8)
        sample[10:20, 10:20] = (10, 120, 220)

        for label, seg in (
            ("unicode", "赛马娘脚本/maauma-base-0.6.11-windows-x64/" + "子目录_" * 55),
            ("ascii", "DeepAsciiFolder/maauma-base-0.6.11-windows-x64/" + "segment_" * 30),
        ):
            deep_dir = self.tmp / seg
            io_path(deep_dir).mkdir(parents=True, exist_ok=True)
            img_path = deep_dir / "stamina_tab.png"
            self.assertGreater(len(str(img_path)), 268, label)

            ok = cv2.imwrite(str(img_path), sample)
            self.assertTrue(ok, f"cv2.imwrite failed on {label} long path")
            self.assertTrue(io_path(img_path).is_file())

            loaded = cv2.imread(str(img_path))
            self.assertIsNotNone(loaded, f"cv2.imread failed on {label} long path")
            self.assertTrue(np.array_equal(loaded, sample))

    def test_live_capture_refresh_under_long_chinese_path_and_compact_budget(self) -> None:
        from core.auto_trainer.capture_adapter import load_capture_group_raws
        from core.auto_trainer.live_capture_package import build_live_capture_package
        from core.auto_trainer.parser_sidecar import build_training_options_sidecar
        from core.auto_trainer.training_page_observation import build_training_page_observation
        from core.frame_io import io_path

        # 1. Verify typical compact layout stays <= 150 chars below runs/
        compact_rel = (
            Path("cut_pacer_261126022501_ab12")
            / "attempts"
            / "larc_panel_261126022501_cd34"
            / "autonomous_261126022501_ef56"
            / "turn_007"
            / "capture"
            / "r"
            / "t007_123456"
            / "ga1b2c"
            / "stamina.png"
        )
        self.assertLessEqual(len(str(compact_rel)), 150)

        # 2. Verify ultra-deep >=275 char Chinese capture + candidate refresh + observation + sidecar
        deep_capture = (
            self.tmp
            / "赛马娘脚本/maauma-base-0.6.11-windows-x64"
            / ("nested_capture_dir_" * 10)
            / "turn_007"
            / "capture"
        )
        src_dir = self.tmp / "sources"
        src_dir.mkdir(parents=True, exist_ok=True)
        sources = {}
        for idx, tab in enumerate(("speed", "stamina", "power", "guts", "wisdom")):
            tab_img = np.full((1280, 720, 3), 240 - idx * 15, dtype=np.uint8)
            tab_img[10:30, 10:30] = (idx * 40, 80, 200)
            p = src_dir / f"{tab}.png"
            ok, enc = cv2.imencode(".png", tab_img)
            self.assertTrue(ok)
            p.write_bytes(enc.tobytes())
            sources[tab] = p

        state_ctx = {
            "turn": 7,
            "current_stats": {"speed": 500, "stamina": 400, "power": 450, "guts": 350, "wisdom": 420, "skill_pt": 120},
            "scenario_state": {"scenario": "larc"},
        }
        pkg1 = build_live_capture_package(
            output_root=deep_capture,
            actual_group_id="t007_a1b2c3",
            run_session_id="larc_panel_261126022501_cd34",
            turn_index=7,
            source_state_key="k7",
            operator_id="op",
            source_images=sources,
            state_context=state_ctx,
            source_resolution=(720, 1280),
            normalized_resolution=(1440, 2560),
        )
        self.assertTrue(pkg1["is_valid"])

        pkg2 = build_live_capture_package(
            output_root=deep_capture,
            actual_group_id="t007_a1b2c3",
            run_session_id="larc_panel_261126022501_cd34",
            turn_index=7,
            source_state_key="k7",
            operator_id="op",
            source_images=sources,
            state_context=state_ctx,
            source_resolution=(720, 1280),
            normalized_resolution=(1440, 2560),
            prior_group={"raw_files": []},
            refreshed_tabs={"stamina"},
        )
        stamina_norm = Path(pkg2["normalized_dir"]) / "stamina.png"
        self.assertGreater(len(str(stamina_norm)), 275)
        self.assertTrue(io_path(stamina_norm).is_file())

        raws = load_capture_group_raws(
            manifest_path=pkg2["manifest_path"],
            execution_root=deep_capture,
            actual_group_id="t007_a1b2c3",
            formal_capture=True,
        )
        self.assertEqual(set(raws), {"speed", "stamina", "power", "guts", "wisdom"})

        obs_path = deep_capture / "training_page_observation.json"
        obs = build_training_page_observation(
            manifest_path=pkg2["manifest_path"],
            execution_root=deep_capture,
            actual_group_id="t007_a1b2c3",
            output_path=obs_path,
            reader=_stat_reader,
            main_menu_context=state_ctx,
            overwrite=True,
        )
        self.assertTrue(obs["is_valid"], obs)
        self.assertTrue(obs["consensus_complete"], obs)

        sidecar_path = deep_capture / "training_options.generated.json"
        sidecar = build_training_options_sidecar(
            manifest_path=pkg2["manifest_path"],
            execution_root=deep_capture,
            actual_group_id="t007_a1b2c3",
            output_path=sidecar_path,
            parser=_DummyParser(),
            state_context=state_ctx,
            overwrite=True,
        )
        self.assertTrue(sidecar["is_valid"], sidecar)

    def test_cut_portable_lease_acl_fallback_and_clear_error(self) -> None:
        from core import cut_portable_lease, cut_portable_runtime

        app_dir = self.tmp / "app"
        pacer_backend = app_dir / "pacer/backend"
        (app_dir / "pacer").mkdir(parents=True, exist_ok=True)
        pacer_backend.mkdir(parents=True, exist_ok=True)
        blocked_local = self.tmp / "blocked_localappdata"

        saved_env = {k: os.environ.get(k) for k in ("UMA_CUT_PORTABLE_ROOT", "UMA_ADB_PORT", "UMA_PUBLIC_LOCK_ROOT", "LOCALAPPDATA")}
        try:
            os.environ["UMA_CUT_PORTABLE_ROOT"] = str(pacer_backend)
            os.environ["UMA_ADB_PORT"] = "16384"
            os.environ.pop("UMA_PUBLIC_LOCK_ROOT", None)
            os.environ["LOCALAPPDATA"] = str(blocked_local)
            cut_portable_runtime._LEASE_CHECKED.clear()

            orig_mkdir = Path.mkdir

            def deny_blocked_local(self_path, *args, **kwargs):
                if str(blocked_local) in str(self_path):
                    raise PermissionError(13, "Access is denied", str(self_path))
                return orig_mkdir(self_path, *args, **kwargs)

            with patch.object(Path, "mkdir", deny_blocked_local):
                with cut_portable_lease.hold(pacer_backend / "runs/device-occupancy", "127.0.0.1:16384", "pacer"):
                    lease = cut_portable_lease.current_native_lease(
                        pacer_backend / "runs/device-occupancy", "127.0.0.1:16384"
                    )
                    self.assertIsNotNone(lease)
                    self.assertTrue(lease.valid())
                    self.assertEqual(Path(os.environ["UMA_PUBLIC_LOCK_ROOT"]), (app_dir / "temp").resolve())

            # When UMA_PUBLIC_LOCK_ROOT is explicitly set and still fails PermissionError, message must be explicit
            os.environ["UMA_PUBLIC_LOCK_ROOT"] = str(blocked_local)
            with patch.object(Path, "mkdir", deny_blocked_local):
                with self.assertRaises(PermissionError) as ctx:
                    with cut_portable_lease.hold(pacer_backend / "runs/device-occupancy", "127.0.0.1:16384", "pacer"):
                        pass
                self.assertIn("device_lock_permission_denied", str(ctx.exception))
        finally:
            cut_portable_runtime._LEASE_CHECKED.clear()
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_runner_error_persisted_and_surfaced_in_stop_context(self) -> None:
        from core.auto_trainer.cut_farming import CutFarmingConfig
        from desktop import pacer_stop_context
        from scripts.larc_panel_runner import create_larc_control_service, submit_button
        from scripts.larc_umaa_cut import create_cut_control_service

        cfg_mod = types.ModuleType("config")
        cfg_set = types.ModuleType("config.settings")
        cfg_set.ADB_HOST = "127.0.0.1"
        cfg_set.ADB_ENDPOINT = "127.0.0.1:16384"
        cfg_set.ADB_PORT = 16384
        cfg_set.ADB_PATH = ""
        cfg_set.SWIPE_DELAY = 0.2
        cfg_set.TAP_DELAY = 0.2
        cfg_set.ASSETS_DIR = PACER_BACKEND / "assets"
        cfg_mod.settings = cfg_set

        umaa_root = self.tmp / "backend"
        pacer_root = self.tmp / "pacer/backend"
        (umaa_root / "runs").mkdir(parents=True, exist_ok=True)
        (pacer_root / "runs").mkdir(parents=True, exist_ok=True)
        batch_root = pacer_root / "runs/cut_pacer_261126022501_ab12"
        batch_root.mkdir(parents=True, exist_ok=True)
        ctrl_dir = pacer_root / "runs/umaa-panel-control/16384"

        class _NoopLease:
            @staticmethod
            def hold(*args, **kwargs):
                from contextlib import nullcontext
                return nullcontext()

        def failing_cut_controller(*args, **kwargs):
            raise PermissionError(13, r"Access denied C:\Users\feiyu\secret token=demo_secret_value_123")

        cut_cfg_obj = CutFarmingConfig(character_id="101001", device_port=16384, validation_mode=True, validation_character_name="大树快车")
        cut_cfg = cut_cfg_obj.public_dict()
        batch_data = {"schema_version": "uma.cut_farming_session.v1", "config": cut_cfg, "attempts": []}
        (batch_root / "cut_farming.json").write_text(
            json.dumps(batch_data),
            encoding="utf-8",
        )

        with patch("scripts.larc_umaa_cut.checked_batch", return_value=(batch_root, batch_data, cut_cfg_obj)):
            svc = create_cut_control_service(
                ctrl_dir,
                {"current": {"batch_root": str(batch_root), "config": cut_cfg}},
                umaa_root=umaa_root,
                lease=_NoopLease(),
                controller_factory=failing_cut_controller,
            )
            submit_button(svc, "start", request_id="req-cut-err-1", profile_id="current")
            for _ in range(100):
                if svc.snapshot()["state"] == "stopped":
                    break
                time.sleep(0.02)
            snap = svc.snapshot()
        self.assertEqual(snap["last_result"]["reason"], "runner_error")
        err_file = batch_root / "runner_error.json"
        self.assertTrue(err_file.is_file())
        err_data = json.loads(err_file.read_text(encoding="utf-8"))
        self.assertEqual(err_data["error_type"], "PermissionError")
        self.assertGreater(err_data["line"], 0)
        self.assertNotIn("feiyu", err_data["error_message"])
        self.assertNotIn("demo_secret_value_123", err_data["error_message"])
        self.assertIn("<USER_HOME>", err_data["error_message"])
        self.assertIn("token=<REDACTED>", err_data["error_message"])

        outer = {
            "mode": "cut",
            "endpoint": "127.0.0.1:16384",
            "run_root": str(batch_root),
            "control": snap,
            "cut": {},
            "record": {},
        }
        pacer_stop_context.annotate(outer, pacer_root)
        ctx = outer["current_stop_context"]
        self.assertEqual(ctx["reason"], "runner_error")
        self.assertIn("runner_error", ctx)
        self.assertEqual(ctx["runner_error"]["error_type"], "PermissionError")
        self.assertIn("PermissionError", ctx["message"])

        # Career runner_error path
        run_root2 = pacer_root / "runs/larc_panel_261126022502_cd34"
        run_root2.mkdir(parents=True, exist_ok=True)
        ctrl_dir2 = pacer_root / "runs/umaa-panel-control/16416"

        def failing_run(args):
            raise RuntimeError("career_boom_for_test")

        with patch.dict(sys.modules, {"config": cfg_mod, "config.settings": cfg_set}):
            svc2 = create_larc_control_service(
                ctrl_dir2,
                {"current": ["--run-root", str(run_root2), "--adb-port", "16416", "--max-actions", "5", "--execute", "--confirm-token", "LARC_AUTONOMY_TEST"]},
                run=failing_run,
                umaa_root=umaa_root,
                lease=_NoopLease(),
            )
            submit_button(svc2, "start", request_id="req-career-err-1", profile_id="current")
            for _ in range(100):
                if svc2.snapshot()["state"] == "stopped":
                    break
                time.sleep(0.02)
        self.assertTrue((run_root2 / "runner_error.json").is_file())
        self.assertTrue((ctrl_dir2 / "last_runner_error.json").is_file())

    def test_compact_and_legacy_run_id_compatibility(self) -> None:
        from core.auto_trainer import career_review_export, pacer_run_retention
        from desktop import native_stop_exporter
        from scripts.larc_umaa_host import PacerHost

        # native_stop_exporter accepts both legacy and compact larc_panel IDs
        self.assertIsNotNone(native_stop_exporter.RUN_ID.fullmatch("larc_panel_20261017_221528_ef7987"))
        self.assertIsNotNone(native_stop_exporter.RUN_ID.fullmatch("larc_panel_261126022501_ab12"))

        # career_review_export parses both legacy and compact timestamps
        run_legacy = self.tmp / "runs/larc_panel_20261126_011500_abcdef"
        run_compact = self.tmp / "runs/larc_panel_261126021500_1234"
        r1 = career_review_export._review_record(self.tmp / "runs", run_legacy, {"status": "stopped"}, {}, {}, mode="career")
        r2 = career_review_export._review_record(self.tmp / "runs", run_compact, {"status": "stopped"}, {}, {}, mode="career")
        self.assertEqual(r1["started_at"], "2026-11-26T01:15:00")
        self.assertEqual(r2["started_at"], "2026-11-26T02:15:00")

        # pacer_run_retention._created_order sorts chronologically across old and new formats
        o1 = pacer_run_retention._created_order(Path("larc_panel_20261126_011500_abcdef"))
        o2 = pacer_run_retention._created_order(Path("larc_panel_261126021500_1234"))
        self.assertEqual(o1, "20261126_011500")
        self.assertEqual(o2, "20261126_021500")
        self.assertLess(o1, o2)

        # PacerHost.records() lists both formats cleanly
        host_pacer = self.tmp / "host_pacer"
        host_umaa = self.tmp / "host_umaa"
        (host_pacer / "runs").mkdir(parents=True, exist_ok=True)
        host_umaa.mkdir(parents=True, exist_ok=True)
        for idx, run_dir in enumerate((host_pacer / "runs/larc_panel_20261126_011500_abcdef", host_pacer / "runs/larc_panel_261126021500_1234"), 1):
            (run_dir / "session").mkdir(parents=True, exist_ok=True)
            (run_dir / "goal.json").write_text(json.dumps({"target_stats": {"speed": 1200, "stamina": 800, "power": 1000, "guts": 600, "wisdom": 800}, "larc_policy": {"variant": "baseline", "revision": "2.3"}}), encoding="utf-8")
            (run_dir / "progress.json").write_text(json.dumps({"run_session_id": run_dir.name, "device": "127.0.0.1:16384", "actions_completed": idx}), encoding="utf-8")
            (run_dir / "session/umaa_panel_options.json").write_text(json.dumps({"setup": {"character_id": "109801", "character_name": "小林历奇"}}), encoding="utf-8")

        class _MockLease:
            @staticmethod
            def probe(*args, **kwargs):
                return None

        host = PacerHost(host_umaa, root=host_pacer, lease=_MockLease())
        recs = host.records("127.0.0.1:16384", mode="career")
        self.assertEqual(len(recs), 2)
        labels = [r["label"] for r in recs]
        self.assertTrue(any("20261126_011500" in lbl for lbl in labels), labels)
        self.assertTrue(any("20261126_021500" in lbl for lbl in labels), labels)


if __name__ == "__main__":
    unittest.main()
