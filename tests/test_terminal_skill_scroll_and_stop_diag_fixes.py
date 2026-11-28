"""Regression tests for terminal skill OCR truncation, scroll clip overlap, and stop-diagnostic skill frames."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

import cv2
import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
PACER_BACKEND = REPO_ROOT / "pacer" / "backend"
PACER_BRIDGE_CORE = PACER_BACKEND / "bridges" / "skills" / "backend" / "core"
MAIN_CORE = REPO_ROOT / "backend" / "core"


def _load_isolated_module(name: str, path: Path, *, package_root: Path):
    saved_modules = sys.modules.copy()
    saved_path = list(sys.path)
    for mod_name in list(sys.modules):
        if mod_name == "core" or mod_name.startswith("core.") or mod_name == "desktop" or mod_name.startswith("desktop."):
            sys.modules.pop(mod_name, None)
    sys.path.insert(0, str(package_root))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        for mod_name in list(sys.modules):
            if mod_name not in saved_modules:
                sys.modules.pop(mod_name, None)
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path


def _load_parser_with_perf():
    saved_modules = sys.modules.copy()
    saved_path = list(sys.path)
    for mod_name in list(sys.modules):
        if mod_name == "core" or mod_name.startswith("core."):
            sys.modules.pop(mod_name, None)
    sys.path.insert(0, str(PACER_BRIDGE_CORE.parent))
    try:
        perf_spec = importlib.util.spec_from_file_location(
            "core.skill_ocr_performance", PACER_BRIDGE_CORE / "skill_ocr_performance.py"
        )
        assert perf_spec and perf_spec.loader
        perf_mod = importlib.util.module_from_spec(perf_spec)
        sys.modules["core.skill_ocr_performance"] = perf_mod
        perf_spec.loader.exec_module(perf_mod)

        parser_spec = importlib.util.spec_from_file_location(
            "test_pacer_skill_acq_parser", PACER_BRIDGE_CORE / "skill_acquisition_parser.py"
        )
        assert parser_spec and parser_spec.loader
        parser_mod = importlib.util.module_from_spec(parser_spec)
        sys.modules["test_pacer_skill_acq_parser"] = parser_mod
        parser_spec.loader.exec_module(parser_mod)
        return perf_mod, parser_mod
    finally:
        for mod_name in list(sys.modules):
            if mod_name not in saved_modules:
                sys.modules.pop(mod_name, None)
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path


def _make_scrolled_pair_with_bottom_clip_noise(shift: int = 370) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(42)
    tall = np.full((1400, 720, 3), 245, dtype=np.uint8)
    for band_y in range(80, 1180, 68):
        for dx in range(160, 440, 18):
            h = int(rng.integers(10, 18))
            w = int(rng.integers(8, 14))
            tall[band_y : band_y + h, dx : dx + w] = (50, 70, 95)

    before = np.full((1280, 720, 3), 245, dtype=np.uint8)
    after = np.full((1280, 720, 3), 245, dtype=np.uint8)
    # Copy overlapping list band y=468..1029 (height 561).
    before[468:1029] = tall[100 : 100 + 561]
    after[468:1029] = tall[100 + shift : 100 + shift + 561]
    # Simulate 1-px card bottom clip at before y=1028 -> visible at after y=1028 - shift.
    clip_y_after = 1028 - shift
    after[clip_y_after : clip_y_after + 2, 160:460] = (90, 95, 100)
    before[1028, 160:460] = (245, 245, 245)
    return before, after


class TestTerminalSkillScrollAndStopDiagFixes(unittest.TestCase):
    def test_read_name_rapidocr_never_chops_first_char_without_badge_noise(self) -> None:
        _perf, parser = _load_parser_with_perf()
        crop = np.full((52, 370, 3), 240, dtype=np.uint8)
        # Synthetic orange-brown glyph ink boxes at x=12..140 in inset [6:46] so connectedComponents finds >=2 glyphs.
        inset_bgr = (35, 110, 190)  # HSV H~15, S>70, V<210
        for gx in (12, 48, 84, 120):
            crop[10:42, gx : gx + 28] = inset_bgr

        saw_left_slice = []
        saw_glyphs = []

        def fake_rapid(img, *, upscale=2, force_detection=False, full_resolution=False):
            h, w = img.shape[:2]
            if w == 370 - 32:
                saw_left_slice.append((h, w))
                return [{"text": "跑诀窍", "confidence": 0.945, "box": None}]
            if full_resolution:
                saw_glyphs.append((h, w))
                return [{"text": "领跑诀窍", "confidence": 0.9804, "box": None}]
            if h == 34:  # name_inset crop[6:40]
                return [{"text": "领跑诀窍", "confidence": 0.88, "box": None}]
            return [{"text": "领跑诀窍", "confidence": 0.86, "box": None}]

        with mock.patch.object(parser, "_rapidocr_ocr_data", side_effect=fake_rapid):
            res = parser._read_name_rapidocr(crop)

        self.assertEqual(saw_left_slice, [], "badge_cleared left-slice must not run without badge noise")
        self.assertTrue(saw_glyphs, "rapidocr_name_glyphs recovery must still run when full-width conf < 0.9")
        self.assertEqual(res["text"], "领跑诀窍")
        self.assertGreaterEqual(res["confidence"], 0.90)

    def test_read_name_rapidocr_still_strips_real_evolution_badge(self) -> None:
        _perf, parser = _load_parser_with_perf()
        crop = np.full((52, 370, 3), 240, dtype=np.uint8)

        def fake_rapid(img, *, upscale=2, force_detection=False, full_resolution=False):
            _, w = img.shape[:2]
            if w == 370 - 32:
                return [{"text": "弧线大师", "confidence": 0.95, "box": None}]
            return [{"text": "可进化弧线大师", "confidence": 0.84, "box": None}]

        with mock.patch.object(parser, "_rapidocr_ocr_data", side_effect=fake_rapid):
            res = parser._read_name_rapidocr(crop)

        self.assertEqual(res["text"], "弧线大师")
        self.assertGreaterEqual(res["confidence"], 0.90)

    def test_measure_skill_overlap_tolerates_viewport_clip_border_and_rejects_mismatch(self) -> None:
        before, after = _make_scrolled_pair_with_bottom_clip_noise(shift=370)
        mismatched = np.full((1280, 720, 3), 245, dtype=np.uint8)
        mismatched[500:550, 200:400] = (40, 40, 40)

        for tag, mod_path, pkg_root in (
            ("pacer", PACER_BRIDGE_CORE / "skill_scroll.py", PACER_BRIDGE_CORE.parent),
            ("main", MAIN_CORE / "skill_scroll.py", MAIN_CORE.parent),
        ):
            mod = _load_isolated_module(f"test_skill_scroll_{tag}", mod_path, package_root=pkg_root)
            ok_res = mod.measure_skill_overlap(before, after)
            self.assertTrue(ok_res.get("verified"), f"{tag}: {ok_res}")
            self.assertLessEqual(abs(ok_res["shift"] - 370.0), 1.0, f"{tag}: {ok_res}")
            self.assertGreaterEqual(ok_res["text_agreement"], 0.95, f"{tag}: {ok_res}")

            bad_res = mod.measure_skill_overlap(before, mismatched)
            self.assertFalse(bad_res.get("verified"), f"{tag}: expected mismatch failure, got {bad_res}")

    def test_stop_diagnostic_prefers_newer_terminal_skill_capture_and_enforces_scope(self) -> None:
        saved_modules = sys.modules.copy()
        saved_path = list(sys.path)
        for mod_name in list(sys.modules):
            if mod_name == "core" or mod_name.startswith("core."):
                sys.modules.pop(mod_name, None)
        sys.path.insert(0, str(PACER_BACKEND))
        try:
            from core.auto_trainer import cut_stop_diagnostic as csd

            with tempfile.TemporaryDirectory() as tmp:
                run = Path(tmp) / "larc_panel_test_1b1a"
                seg = run / "autonomous_261128005317_6539"
                batch_dir = run / "s" / "batch_002"
                cap_dir = batch_dir / "capture"
                cap_dir.mkdir(parents=True, exist_ok=True)
                seg.mkdir(parents=True, exist_ok=True)
                outside = Path(tmp) / "outside"
                outside.mkdir(parents=True, exist_ok=True)

                seg_ctime = seg.stat().st_ctime
                now = seg_ctime + 200.0
                old_frame = np.full((1280, 720, 3), 60, dtype=np.uint8)
                skill_frame = np.full((1280, 720, 3), 210, dtype=np.uint8)
                skill_frame[100:200, 100:300] = (180, 50, 120)
                foreign_frame = np.full((1280, 720, 3), 10, dtype=np.uint8)

                skill_png = cap_dir / "1795800702161574100.png"
                cv2.imwrite(str(skill_png), skill_frame)
                os.utime(skill_png, (now - 0.5, now - 0.5))

                foreign_png = outside / "1795800702999999999.png"
                cv2.imwrite(str(foreign_png), foreign_frame)
                os.utime(foreign_png, (now - 0.1, now - 0.1))

                worker_report = batch_dir / "worker_report.json"
                worker_report.write_text(
                    json.dumps({"run_session_id": run.name, "output_dir": str(batch_dir)}),
                    encoding="utf-8",
                )

                bad_worker = batch_dir / "bad_worker_report.json"
                bad_worker.write_text(
                    json.dumps({"run_session_id": "wrong_run_id"}),
                    encoding="utf-8",
                )

                memory = {
                    "frame": old_frame,
                    "run_session_id": run.name,
                    "segment_id": seg.name,
                    "captured_at_unix": now - 136.0,
                    "capture_time_source": "capture_return_time",
                    "observation": {
                        "schema_version": "uma.stop_frame_observation.v1",
                        "capture_method": "screenshot",
                        "sequence": 10549,
                        "owner_turn_index": 61,
                        "stop_owner_turn_index": 61,
                    },
                }

                summary = {
                    "run_session_id": run.name,
                    "output": str(seg),
                    "status": "stopped",
                    "stop_reason": "terminal_skill_stopped:ValueError:race_skill_prepare_failed",
                    "terminal_skills": {
                        "run_session_id": run.name,
                        "batches": [str(worker_report)],
                    },
                }

                with (
                    mock.patch("core.cut_portable_runtime.screenshot_output_allowed", return_value=True),
                    mock.patch("core.auto_trainer.run_support_profile.evidence", return_value={"status": "none"}),
                ):
                    meta = csd.save_ordinary_stop_diagnostic(
                        run, 16384, summary, last_game_frame=memory, now=now
                    )
                    self.assertEqual(meta["status"], "ready", meta)
                    self.assertIs(meta["capture_performed"], False)
                    self.assertEqual(meta["capture_time_source"], "same_segment_saved_game_frame_mtime")
                    self.assertLess(meta["frame_age_seconds"], 5.0)
                    saved_jpg = cv2.imread(str(run / "session/stop_diagnostic/stop.jpg"))
                    self.assertIsNotNone(saved_jpg)
                    self.assertGreater(float(saved_jpg[150, 150].mean()), 100.0)

                    # Security check: wrong run_session_id or path outside run must fall back to memory frame.
                    bad_summary = {
                        **summary,
                        "terminal_skills": {
                            "run_session_id": run.name,
                            "batches": [str(bad_worker), str(outside / "capture" / "worker_report.json")],
                        },
                    }
                    bad_meta = csd.save_ordinary_stop_diagnostic(
                        run, 16384, bad_summary, last_game_frame=memory, now=now
                    )
                    self.assertEqual(bad_meta["status"], "ready", bad_meta)
                    self.assertAlmostEqual(bad_meta["frame_age_seconds"], 136.0, delta=1.0)
        finally:
            for mod_name in list(sys.modules):
                if mod_name not in saved_modules:
                    sys.modules.pop(mod_name, None)
            sys.modules.update(saved_modules)
            sys.path[:] = saved_path


class TestSkillScrollOverlapHardeningAndDegradation(unittest.TestCase):
    _REPO = Path(__file__).resolve().parents[1]
    _PACER_BACKEND = _REPO / "pacer/backend"
    _PACER_SKILLS_BRIDGE = _PACER_BACKEND / "bridges/skills"

    @staticmethod
    def _make_distinct_skill_frames(shift: int) -> tuple[np.ndarray, np.ndarray]:
        rng = np.random.default_rng(12345)
        canvas_h = 1200
        strip = np.full((canvas_h, 720, 3), 245, dtype=np.uint8)
        for y in range(24, canvas_h - 40, 26):
            x_start = 155 + int(rng.integers(0, 60))
            width = 180 + int(rng.integers(0, 140))
            strip[y:y + 12, x_start:x_start + width] = (30, 30, 30)
            for seg_x in range(x_start + 15, x_start + width - 10, 28):
                strip[y:y + 12, seg_x:seg_x + 7] = (245, 245, 245)
        before = np.full((1280, 720, 3), 245, dtype=np.uint8)
        after = np.full((1280, 720, 3), 245, dtype=np.uint8)
        before[468:1029, :] = strip[100:100 + 561, :]
        after[468:1029, :] = strip[100 + shift:100 + shift + 561, :]
        return before, after

    def _run_with_root(self, root: Path, fn):
        saved_modules = sys.modules.copy()
        saved_path = list(sys.path)
        for mod_name in list(sys.modules):
            if (
                mod_name in {"core", "scripts", "desktop"}
                or mod_name.startswith(("core.", "scripts.", "desktop."))
            ):
                sys.modules.pop(mod_name, None)
        sys.path.insert(0, str(root))
        try:
            with mock.patch.dict(os.environ, {"UMA_PUBLIC_ENDPOINT": "127.0.0.1:16384"}):
                return fn()
        finally:
            for mod_name in list(sys.modules):
                if mod_name not in saved_modules:
                    sys.modules.pop(mod_name, None)
            sys.modules.update(saved_modules)
            sys.path[:] = saved_path

    def test_measure_skill_overlap_accepts_shift_420_and_rejects_470_both_copies(self) -> None:
        before_420, after_420 = self._make_distinct_skill_frames(420)
        before_470, after_470 = self._make_distinct_skill_frames(470)
        for tag, root in (("main", self._REPO / "backend"), ("pacer", self._PACER_SKILLS_BRIDGE / "backend")):
            def check():
                import core.skill_scroll as ss
                self.assertEqual(ss.MAX_SKILL_OVERLAP_SHIFT, 450, tag)
                ok = ss.measure_skill_overlap(before_420, after_420)
                self.assertTrue(ok.get("verified"), f"{tag}: shift=420 failed: {ok}")
                bad = ss.measure_skill_overlap(before_470, after_470)
                self.assertFalse(bad.get("verified"), f"{tag}: shift=470 should fail: {bad}")
            self._run_with_root(root, check)

    def _make_obs(self, path: Path, *, page="skills", at_top=True, at_bottom=False, pts=55,
                  actions=None) -> dict:
        import hashlib
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        return {
            "page": page,
            "skill_list_at_top": at_top,
            "skill_list_at_bottom": at_bottom,
            "skill_list_navigation_verified": True,
            "current_skill_points": pts,
            "screenshot_path": str(path),
            "frame_sha256": sha,
            "captured_at_unix_ms": 1_700_000_000_000,
            "expires_at_unix_ms": 1_700_000_600_000,
            "actions": dict(actions or {}),
        }

    def test_prepare_skills_settle_retry_bottom_bounce_and_rewind_fallback_both_copies(self) -> None:
        before_img, after_img = self._make_distinct_skill_frames(320)
        _, bad_img = self._make_distinct_skill_frames(490)
        _, tail_img = self._make_distinct_skill_frames(520)

        for tag, root in (("main", self._REPO / "backend"), ("pacer", self._PACER_SKILLS_BRIDGE / "backend")):
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                res_cat = tmp_path / "resolver.json"
                pln_cat = tmp_path / "planner.json"
                res_cat.write_text("{}", encoding="utf-8")
                pln_cat.write_text("{}", encoding="utf-8")
                p0 = tmp_path / "f0.png"
                p_bad = tmp_path / "f_bad.png"
                p_good = tmp_path / "f_good.png"
                p_tail = tmp_path / "f_tail.png"
                cv2.imwrite(str(p0), before_img)
                cv2.imwrite(str(p_bad), bad_img)
                cv2.imwrite(str(p_good), after_img)
                cv2.imwrite(str(p_tail), tail_img)

                ctx_build = {
                    "turn_id": "t1",
                    "target_mode": "race",
                    "race_profile": {"distance_type": "medium", "style": "pace"},
                    "resolver_catalog": str(res_cat),
                    "planner_catalog": str(pln_cat),
                }
                dry_ok = {
                    "status": "ready",
                    "planner_dry_run": {"status": "ready"},
                    "parser_manifest": {"current_skill_points": 55},
                    "resolver_summary": {},
                    "verdict": {"planner_verdict": "PLANNER_READY"},
                    "inventory_packet": {"execution_allowed": False},
                    "batch_plan": {"execution_allowed": False, "selected_action_ids": []},
                    "planner_observation": {"available_skills": []},
                }

                # Scenario 1: settle re-capture recovers from unsettled first overlap frame.
                def run_settle():
                    import core.autonomous_training.skills as sk
                    obs0 = self._make_obs(p0, at_top=True, at_bottom=False,
                                          actions={"scroll_skill_overlap": {}, "scroll_skill_up": {}})
                    obs1_bad = self._make_obs(p_bad, at_top=False, at_bottom=False,
                                              actions={"scroll_skill_overlap": {}, "scroll_skill_up": {}})
                    obs1_good = self._make_obs(p_good, at_top=False, at_bottom=True,
                                               actions={"scroll_skill_overlap": {}, "scroll_skill_up": {}})
                    captures = [obs0, obs1_good]

                    class Dev:
                        endpoint = sk._ALLOWED_ENDPOINT
                        context = {}
                        def capture_skill_scroll(self):
                            return captures.pop(0)
                        def capture(self):
                            return self.capture_skill_scroll()
                        def step(self, action):
                            self.steps.append(action)
                            return {"observation": obs1_bad}
                    dev = Dev()
                    dev.steps = []
                    with mock.patch.object(sk.time, "sleep") as m_sleep, \
                         mock.patch.object(sk, "run_fresh_dry_run", return_value=dry_ok):
                        res = sk.prepare_skills(dev, tmp_path / f"out_settle_{tag}", ctx_build)
                    self.assertEqual(res.get("status"), "ready", f"{tag}: {res}")
                    m_sleep.assert_called()
                    self.assertEqual(dev.steps, ["scroll_skill_overlap"])

                # Scenario 2: overlap still fails after settle -> saves forensic PNG -> rewinds -> reveal_bottom succeeds.
                def run_fallback():
                    import core.autonomous_training.skills as sk
                    obs_top = self._make_obs(p0, at_top=True, at_bottom=False,
                                             actions={"scroll_skill_overlap": {}, "scroll_skill_reveal_bottom": {}, "scroll_skill_up": {}})
                    obs_bad = self._make_obs(p_bad, at_top=False, at_bottom=False,
                                             actions={"scroll_skill_overlap": {}, "scroll_skill_reveal_bottom": {}, "scroll_skill_up": {}})
                    obs_tail = self._make_obs(p_tail, at_top=False, at_bottom=True,
                                              actions={"scroll_skill_up": {}})
                    captures = [obs_top, obs_bad]

                    class Dev:
                        endpoint = sk._ALLOWED_ENDPOINT
                        context = {}
                        steps = []
                        def capture_skill_scroll(self):
                            return captures.pop(0)
                        def capture(self):
                            return self.capture_skill_scroll()
                        def step(self, action):
                            self.steps.append(action)
                            if action == "scroll_skill_overlap":
                                return {"observation": obs_bad}
                            if action == "scroll_skill_up":
                                return {"observation": obs_top}
                            if action == "scroll_skill_reveal_bottom":
                                return {"observation": obs_tail}
                            raise AssertionError(action)
                    dev = Dev()
                    dev.steps = []
                    out_dir = tmp_path / f"out_fb_{tag}"
                    with mock.patch.object(sk.time, "sleep"), \
                         mock.patch.object(sk, "run_fresh_dry_run", return_value=dry_ok):
                        res = sk.prepare_skills(dev, out_dir, ctx_build)
                    self.assertEqual(res.get("status"), "ready", f"{tag}: {res}")
                    self.assertEqual(dev.steps, ["scroll_skill_overlap", "scroll_skill_up", "scroll_skill_reveal_bottom"])
                    manifest = json.loads((out_dir / "capture_manifest.json").read_text(encoding="utf-8"))
                    self.assertTrue(manifest.get("overlap_fallback_used"))
                    self.assertTrue(manifest.get("failed_overlap_attempts"))
                    self.assertTrue((out_dir / "capture/overlap_fail_page_001.png").is_file())

                # Scenario 3: bottom bounce (at_bottom=True with identical pixels) tolerates end of list.
                def run_bottom_bounce():
                    import core.autonomous_training.skills as sk
                    obs_top = self._make_obs(p0, at_top=True, at_bottom=False,
                                             actions={"scroll_skill_overlap": {}, "scroll_skill_up": {}})
                    obs_bot_same = self._make_obs(p0, at_top=False, at_bottom=True,
                                                  actions={"scroll_skill_overlap": {}, "scroll_skill_up": {}})
                    captures = [obs_top, obs_bot_same]

                    class Dev:
                        endpoint = sk._ALLOWED_ENDPOINT
                        context = {}
                        def capture_skill_scroll(self):
                            return captures.pop(0)
                        def capture(self):
                            return self.capture_skill_scroll()
                        def step(self, action):
                            return {"observation": obs_bot_same}
                    out_dir = tmp_path / f"out_bot_{tag}"
                    with mock.patch.object(sk.time, "sleep"), \
                         mock.patch.object(sk, "run_fresh_dry_run", return_value=dry_ok):
                        res = sk.prepare_skills(Dev(), out_dir, ctx_build)
                    self.assertEqual(res.get("status"), "ready", f"{tag}: {res}")
                    manifest = json.loads((out_dir / "capture_manifest.json").read_text(encoding="utf-8"))
                    self.assertTrue(manifest["overlap_evidence"][-1].get("bottom_identical_list_tolerated"))

                self._run_with_root(root, run_settle)
                self._run_with_root(root, run_fallback)
                self._run_with_root(root, run_bottom_bounce)

    def test_learn_race_skills_safely_returns_on_prepare_blocked(self) -> None:
        def check():
            from scripts import larc_d1_skill_worker as w
            sink = {}
            api = mock.Mock()
            api.prepare.return_value = {
                "status": "blocked",
                "reason": "skill_scroll_overlap_not_verified",
                "purchase_executed": False,
                "tap_count": 0,
            }
            device = mock.Mock()
            device.capture.side_effect = [
                {
                    "page": "skills",
                    "current_skill_points": 55,
                    "actions": {"return_harvest_skills": {"point": [68, 1180]}},
                },
                {"page": "finish_ready", "current_skill_points": 55},
            ]
            with tempfile.TemporaryDirectory() as tmp, mock.patch.object(w.time, "sleep"):
                with self.assertRaisesRegex(ValueError, "race_skill_prepare_failed:skill_scroll_overlap_not_verified"):
                    w._learn_race_skills(
                        api,
                        device,
                        Path(tmp),
                        {"turn_id": "t1"},
                        {},
                        55,
                        expected_endpoint=w.ENDPOINT,
                        report_sink=sink,
                    )
            self.assertFalse(sink.get("purchase_executed"))
            self.assertFalse(sink.get("confirmation_opened"))
            self.assertTrue(sink.get("returned_after_unconfirmed_selection"))
            self.assertEqual(sink.get("after_return_points"), 55)
            device.command.assert_called_once()

        self._run_with_root(self._PACER_BACKEND, check)

    def test_terminal_skills_55pt_stop_and_low_pt_prepare_retry_and_degradation(self) -> None:
        def check():
            from scripts import larc_autonomy_skills as las

            def make_run(tmp: Path, next_turn: int = 77) -> tuple[Path, Path, Path, Path]:
                run = tmp / "cut_pacer_test_ab77"
                (run / "session").mkdir(parents=True, exist_ok=True)
                d1 = tmp / "d1"
                cat = d1 / "data/skills/cn-20260916"
                cat.mkdir(parents=True, exist_ok=True)
                (cat / "cn_skill_resolver_catalog.json").write_text("{}", encoding="utf-8")
                (cat / "cn_skill_planner_catalog.json").write_text("{}", encoding="utf-8")
                (run / "progress.json").write_text(
                    json.dumps({"run_session_id": run.name, "device": "127.0.0.1:16384", "next_turn": next_turn}),
                    encoding="utf-8",
                )
                ext_db = tmp / "ext.json"
                ext_db.write_text("{}", encoding="utf-8")
                catalog_dir = Path(las.__file__).resolve().parents[1] / "data/skills/cn_larc_end"
                return run, run / "s", d1, ext_db

            class FakeProc:
                def __init__(self, rc=0):
                    self.returncode = rc
                    self.stdout = ""
                    self.stderr = ""
                    self.startup_stalled = False
                    self.timed_out = False
                    self.diagnostics_path = None

            def write_worker(argv, payload):
                out_dir = Path(argv[argv.index("--output-dir") + 1])
                out_dir.mkdir(parents=True, exist_ok=True)
                turn_id = argv[argv.index("--turn-id") + 1]
                proof = out_dir / "proof.json"
                proof.write_text("{}", encoding="utf-8")
                run_name = Path(argv[argv.index("--run-root") + 1]).name
                full = {
                    "run_session_id": run_name,
                    "endpoint": "127.0.0.1:16384",
                    "turn_id": turn_id,
                    "output_dir": str(out_dir),
                    "return_proof_path": str(proof),
                    **payload,
                }
                (out_dir / "worker_report.json").write_text(json.dumps(full), encoding="utf-8")

            policy = {"enabled": False, "auto_cm": False, "mode": "legacy"}
            fake_rt = mock.Mock(executable="tesseract.exe")

            with (
                mock.patch.object(las, "resolve_tesseract_runtime", return_value=fake_rt),
                mock.patch.object(las, "skill_ocr_environment", side_effect=lambda env, **kw: dict(env)),
                mock.patch("core.auto_trainer.larc_target_skills.policy_from_run", return_value=policy),
                mock.patch("core.auto_trainer.larc_target_skills.summarize_run_skill_pt", return_value={"ok": True}),
            ):
                # 1) 55 Pt remainder after batch_001 stops cleanly via after < 60 (small_remainder_retained).
                with tempfile.TemporaryDirectory() as t1:
                    run, out, d1, ext = make_run(Path(t1))
                    def side1(argv, **kwargs):
                        write_worker(argv, {
                            "ok": True, "stage": "complete", "reason": "race_skill_learning_verified",
                            "before": {"current_skill_points": 2790}, "after": {"current_skill_points": 55},
                            "finish_identity": {"name": "Oguri"},
                        })
                        return FakeProc(0)
                    with mock.patch.object(las, "run_controlled_process", side_effect=side1) as m_run:
                        rep, rc = las.run_larc_terminal_skills(
                            run_root=run, output_dir=out, d1_root=d1, external_skill_db_path=ext, adb_port=16384
                        )
                    self.assertEqual(rc, 0, rep)
                    self.assertEqual(rep.get("ready_reason"), "small_remainder_retained")
                    self.assertEqual(len(rep.get("batches", [])), 1)
                    self.assertEqual(m_run.call_count, 1)

                # 2) batch_001 -> 75 Pt, batch_002 safe_readonly_prepare -> ADB retry batch_003 succeeds.
                with tempfile.TemporaryDirectory() as t2:
                    run, out, d1, ext = make_run(Path(t2))
                    seen_backends = []
                    def side2(argv, **kwargs):
                        seen_backends.append(kwargs.get("env", {}).get("UMA_CUT_CAPTURE_BACKEND"))
                        n = len(seen_backends)
                        if n == 1:
                            write_worker(argv, {
                                "ok": True, "stage": "complete", "reason": "race_skill_learning_verified",
                                "before": {"current_skill_points": 2000}, "after": {"current_skill_points": 75},
                                "finish_identity": {"name": "Oguri"},
                            })
                            return FakeProc(0)
                        if n == 2:
                            write_worker(argv, {
                                "ok": False, "stage": "learn",
                                "reason": "ValueError:race_skill_prepare_failed:skill_scroll_overlap_not_verified",
                                "purchase_executed": False, "confirmation_opened": False,
                                "returned_after_unconfirmed_selection": True, "after_return_points": 75,
                                "prepare_result": {"status": "blocked", "purchase_executed": False, "tap_count": 0},
                            })
                            return FakeProc(2)
                        write_worker(argv, {
                            "ok": True, "stage": "complete", "reason": "no_purchase_return_verified",
                            "before": {"current_skill_points": 75}, "after": {"current_skill_points": 75},
                            "finish_identity": {"name": "Oguri"},
                        })
                        return FakeProc(0)
                    with mock.patch.object(las, "run_controlled_process", side_effect=side2):
                        rep, rc = las.run_larc_terminal_skills(
                            run_root=run, output_dir=out, d1_root=d1, external_skill_db_path=ext, adb_port=16384
                        )
                    self.assertEqual(rc, 0, rep)
                    self.assertEqual(seen_backends[-1], "adb")
                    self.assertEqual(len(rep.get("prepare_retry_batches", [])), 1)

                # 3) batch_001 -> 75 Pt, batch_002 & ADB retry batch_003 both fail read-only prepare -> low-Pt degrade.
                with tempfile.TemporaryDirectory() as t3:
                    run, out, d1, ext = make_run(Path(t3))
                    calls = [0]
                    def side3(argv, **kwargs):
                        calls[0] += 1
                        if calls[0] == 1:
                            write_worker(argv, {
                                "ok": True, "stage": "complete", "reason": "race_skill_learning_verified",
                                "before": {"current_skill_points": 2000}, "after": {"current_skill_points": 75},
                                "finish_identity": {"name": "Oguri"},
                            })
                            return FakeProc(0)
                        write_worker(argv, {
                            "ok": False, "stage": "learn",
                            "reason": "ValueError:race_skill_prepare_failed:skill_scroll_overlap_not_verified",
                            "purchase_executed": False, "confirmation_opened": False,
                            "returned_after_unconfirmed_selection": True, "after_return_points": 75,
                            "prepare_result": {"status": "blocked", "purchase_executed": False, "tap_count": 0},
                        })
                        return FakeProc(2)
                    with mock.patch.object(las, "run_controlled_process", side_effect=side3):
                        rep, rc = las.run_larc_terminal_skills(
                            run_root=run, output_dir=out, d1_root=d1, external_skill_db_path=ext, adb_port=16384
                        )
                    self.assertEqual(rc, 0, rep)
                    self.assertEqual(rep.get("ready_reason"), "degraded_terminal_skill_prepare_low_pt_skipped")
                    self.assertEqual(rep.get("remaining_skill_points"), 75)

                # 4) batch_001 at 2000 Pt fails read-only prepare twice -> strict stop (rc == 2).
                with tempfile.TemporaryDirectory() as t4:
                    run, out, d1, ext = make_run(Path(t4))
                    def side4(argv, **kwargs):
                        write_worker(argv, {
                            "ok": False, "stage": "learn",
                            "reason": "ValueError:race_skill_prepare_failed:skill_scroll_overlap_not_verified",
                            "purchase_executed": False, "confirmation_opened": False,
                            "returned_after_unconfirmed_selection": True, "after_return_points": 2000,
                            "prepare_result": {"status": "blocked", "purchase_executed": False, "tap_count": 0},
                        })
                        return FakeProc(2)
                    with mock.patch.object(las, "run_controlled_process", side_effect=side4):
                        rep, rc = las.run_larc_terminal_skills(
                            run_root=run, output_dir=out, d1_root=d1, external_skill_db_path=ext, adb_port=16384
                        )
                    self.assertEqual(rc, 2, rep)
                    self.assertIn("terminal_skill_worker_failed", rep.get("stop_reason", ""))

        self._run_with_root(self._PACER_BACKEND, check)


if __name__ == "__main__":
    unittest.main()
