"""Regression tests for P0 continuous-career speedups and 720p native precheck."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import time
import types
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
    # Remove cached 'core*' and 'desktop*' so package_root resolves cleanly without polluting caller imports.
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


def _load_parser_and_perf(tag: str, parser_path: Path, perf_path: Path, pkg_root: Path):
    saved_modules = sys.modules.copy()
    saved_path = list(sys.path)
    for mod_name in list(sys.modules):
        if mod_name == "core" or mod_name.startswith("core."):
            sys.modules.pop(mod_name, None)
    sys.path.insert(0, str(pkg_root))
    try:
        perf_spec = importlib.util.spec_from_file_location("core.skill_ocr_performance", perf_path)
        assert perf_spec and perf_spec.loader
        perf_mod = importlib.util.module_from_spec(perf_spec)
        sys.modules["core.skill_ocr_performance"] = perf_mod
        perf_spec.loader.exec_module(perf_mod)

        parser_spec = importlib.util.spec_from_file_location(f"test_parser_{tag}", parser_path)
        assert parser_spec and parser_spec.loader
        parser_mod = importlib.util.module_from_spec(parser_spec)
        sys.modules[f"test_parser_{tag}"] = parser_mod
        parser_spec.loader.exec_module(parser_mod)
        return perf_mod, parser_mod
    finally:
        for mod_name in list(sys.modules):
            if mod_name not in saved_modules:
                sys.modules.pop(mod_name, None)
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path


def _load_live_module(live_path: Path, tag: str):
    saved_modules = sys.modules.copy()
    saved_path = list(sys.path)
    for mod_name in list(sys.modules):
        if mod_name == "core" or mod_name.startswith("core.") or mod_name == "desktop" or mod_name.startswith("desktop."):
            sys.modules.pop(mod_name, None)
    desktop_pkg = types.ModuleType("desktop")
    ctx_mod = types.ModuleType("desktop.umaa_public_context")
    ctx_mod.endpoint = lambda: "127.0.0.1:16384"
    adb_mod = types.ModuleType("desktop.adb_probe")
    adb_mod.AdbProbe = object
    sys.modules["desktop"] = desktop_pkg
    sys.modules["desktop.umaa_public_context"] = ctx_mod
    sys.modules["desktop.adb_probe"] = adb_mod
    sys.path.insert(0, str(live_path.parents[2]))
    try:
        spec = importlib.util.spec_from_file_location(f"test_live_{tag}", live_path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"test_live_{tag}"] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        for mod_name in list(sys.modules):
            if mod_name not in saved_modules:
                sys.modules.pop(mod_name, None)
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path


def _make_synthetic_skill_page() -> np.ndarray:
    img = np.full((1280, 720, 3), 245, dtype=np.uint8)
    # Purple header banner: HSV H in [115, 160], S > 40, V > 60 and white title pixels (>= 35% each).
    img[0:45, 0:180] = (180, 60, 140)
    img[8:38, 18:135] = (255, 255, 255)
    # Two skill rows separated by standard row background/dividers at y=470..610 and 615..755.
    for row_top in (470, 615):
        img[row_top:row_top + 135, 30:690] = (232, 240, 248)
        # Icon box
        img[row_top + 35:row_top + 115, 42:120] = (90, 150, 220)
        # Name ink & description text bars
        img[row_top + 18:row_top + 36, 140:320] = (40, 60, 120)
        img[row_top + 56:row_top + 96, 140:420] = (60, 75, 110)
        # Green '+' button on right
        img[row_top + 72:row_top + 112, 620:670] = (70, 190, 80)
        # Cost digits strip
        img[row_top + 80:row_top + 108, 555:605] = (45, 55, 130)
    return img


def _make_synthetic_larc_training_frame() -> np.ndarray:
    img = np.full((1280, 720, 3), 240, dtype=np.uint8)
    # Top-left L'Arc blue header + white text [42:126, 15:149] (blue>=0.22, white>=0.30)
    img[42:126, 15:149] = (210, 110, 20)
    img[58:96, 30:132] = (252, 252, 252)
    # Attribute label band (y=740..890 -> 820..850 header, 850..890 white cells with brown ink)
    img[820:850, 22:703] = (210, 130, 30)
    img[828:842, 60:640] = (252, 252, 252)
    img[850:890, 22:703] = (252, 252, 252)
    edges = (22, 135, 253, 371, 490, 598, 703)
    for idx in range(6):
        left = edges[idx] + (35 if idx < 5 else 0)
        right = edges[idx + 1] - 2
        img[862:878, left + 14:right - 14] = (40, 75, 140)
    # Failure rate region [830:930, 0:180]
    img[845:905, 20:140] = (30, 140, 235)
    img[855:895, 45:115] = (252, 252, 252)
    # 5 training tabs (centers: 67, 184, 302, 420, 538) + locked/ss 6th slot at 654
    centers = (67, 184, 302, 420, 538, 654)
    for idx, cx in enumerate(centers[:5]):
        img[1005:1135, cx - 42:cx + 42] = (60, 185, 80) if idx == 0 else (200, 140, 40)
        img[1030:1080, cx - 22:cx + 22] = (252, 252, 252)
    # Decide double gold chevrons on tab 0 ('speed', cx=67) inside y=1090..1175
    cx = 67
    for top_y in (1098, 1142):
        for dy in range(18):
            y = top_y + dy
            for dx in range(-32, 33):
                wing_drop = abs(dx) // 5
                if dy >= wing_drop and dy <= wing_drop + 10:
                    img[y, cx + dx] = (30, 200, 240)
    return img


class TestP0SpeedOptimizations(unittest.TestCase):
    def test_p0_1_evolution_transition_pending_and_repeat_limits(self) -> None:
        for tag, live_path, harvest_path in (
            ("pacer", PACER_BRIDGE_CORE / "autonomous_training" / "live.py", PACER_BRIDGE_CORE / "autonomous_training" / "harvest.py"),
            ("main", MAIN_CORE / "autonomous_training" / "live.py", MAIN_CORE / "autonomous_training" / "harvest.py"),
        ):
            live = _load_live_module(live_path, tag)
            before_select = {
                "page": "skill_evolution_select",
                "actions": {"select_skill_evolution": {"point": [64, 468]}},
            }
            self.assertTrue(
                live.transition_pending("select_skill_evolution", before_select, before_select),
                f"{tag}: identical radio point should remain pending",
            )
            after_card2 = {
                "page": "skill_evolution_select",
                "actions": {"select_skill_evolution": {"point": [64, 1019]}},
            }
            self.assertFalse(
                live.transition_pending("select_skill_evolution", before_select, after_card2),
                f"{tag}: next evolution skill should not trigger 20s wait",
            )
            after_scroll = {
                "page": "skill_evolution_select",
                "actions": {"scroll_skill_evolution": {"gesture": "swipe", "point": [360, 880], "end": [360, 420]}},
            }
            self.assertFalse(
                live.transition_pending("select_skill_evolution", before_select, after_scroll)
            )
            self.assertFalse(
                live.transition_pending("scroll_skill_evolution", after_scroll, after_scroll),
                f"{tag}: scroll_skill_evolution must not stall 20s",
            )
            before_anim = {
                "page": "skill_evolution_animation",
                "actions": {"advance_skill_evolution": {"point": [360, 1180]}},
            }
            self.assertFalse(
                live.transition_pending("advance_skill_evolution", before_anim, before_anim),
                f"{tag}: advance_skill_evolution must not stall 20s",
            )
            self.assertTrue(
                live.transition_pending("advance_skill_evolution", before_anim, {"page": "loading", "actions": {}}),
            )
            harvest_txt = harvest_path.read_text(encoding="utf-8")
            self.assertIn("else 20 if action == 'advance_skill_evolution'", harvest_txt)
            self.assertIn("else 8 if action in ('select_skill_evolution', 'scroll_skill_evolution')", harvest_txt)

    def test_p0_2_inspect_skill_page_avoids_tesseract_when_rapidocr_ready(self) -> None:
        img = _make_synthetic_skill_page()
        for tag, parser_path, perf_path, pkg_root in (
            ("pacer", PACER_BRIDGE_CORE / "skill_acquisition_parser.py", PACER_BRIDGE_CORE / "skill_ocr_performance.py", PACER_BRIDGE_CORE.parent),
            ("main", MAIN_CORE / "skill_acquisition_parser.py", MAIN_CORE / "skill_ocr_performance.py", MAIN_CORE.parent),
        ):
            _perf, parser = _load_parser_and_perf(f"ins_{tag}", parser_path, perf_path, pkg_root)
            tess_calls = []

            def fake_rapid(crop, *, upscale=2, force_detection=False, full_resolution=False):
                if crop.shape[0] <= 40 and crop.shape[1] <= 125:
                    return [{"text": "技能获取", "confidence": 0.99, "box": None}]
                return [{"text": "250", "confidence": 0.99, "box": None}]

            def spy_tesseract(*args, **kwargs):
                tess_calls.append((args, kwargs))
                return ("", 0.0)

            with (
                mock.patch.object(parser, "rapidocr_available", return_value=True),
                mock.patch.object(parser, "_rapidocr_ocr_data", side_effect=fake_rapid),
                mock.patch.object(parser, "_ocr_data", side_effect=spy_tesseract),
                mock.patch.object(parser, "detect_skill_row_boxes", return_value=[(30, 470, 690, 605)]),
            ):
                evidence = parser.inspect_skill_acquisition_page(img)
            self.assertTrue(evidence["accepted"], f"{tag}: {evidence}")
            self.assertTrue(evidence["title_matches"])
            self.assertEqual(evidence["current_skill_points"], 250)
            self.assertEqual(len(tess_calls), 0, f"{tag}: expected 0 tesseract calls, got {len(tess_calls)}")

            with (
                mock.patch.object(parser, "rapidocr_available", return_value=True),
                mock.patch.object(parser, "_rapidocr_ocr_data", return_value=[]),
                mock.patch.object(parser, "read_current_skill_points", side_effect=lambda *a, **kw: 180 if kw.get("backend") == "tesseract" else None) as sp_mock,
                mock.patch.object(parser, "detect_skill_row_boxes", return_value=[(30, 470, 690, 605)]),
            ):
                fallback = parser.inspect_skill_acquisition_page(img)
            self.assertTrue(fallback["accepted"])
            self.assertEqual(fallback["current_skill_points"], 180)
            self.assertEqual([call.kwargs.get("backend") for call in sp_mock.call_args_list], ["rapidocr", "tesseract"])

    def test_p0_2_parse_row_row_cache_hit_and_invalidation(self) -> None:
        img = _make_synthetic_skill_page()
        for tag, parser_path, perf_path, pkg_root in (
            ("pacer", PACER_BRIDGE_CORE / "skill_acquisition_parser.py", PACER_BRIDGE_CORE / "skill_ocr_performance.py", PACER_BRIDGE_CORE.parent),
            ("main", MAIN_CORE / "skill_acquisition_parser.py", MAIN_CORE / "skill_ocr_performance.py", MAIN_CORE.parent),
        ):
            perf, parser = _load_parser_and_perf(f"row_{tag}", parser_path, perf_path, pkg_root)
            ocr_calls = 0

            def counting_name(crop, ocr_backend, runtime):
                nonlocal ocr_calls
                ocr_calls += 1
                return {"text": "弯道回复", "confidence": 0.96, "accepted": True, "candidates": []}

            def counting_desc(crop, ocr_backend, runtime):
                nonlocal ocr_calls
                ocr_calls += 1
                return {"text": "在弯道略微恢复持久力", "confidence": 0.95, "raw": "在弯道略微恢复持久力"}

            def counting_cost(row, *, top_partial=False, runtime=None, backend="tesseract"):
                nonlocal ocr_calls
                ocr_calls += 1
                return {"value": 140, "source": "rapidocr", "confidence": 0.97, "rapidocr_confidence": 0.97, "tesseract_confidence": None}

            def counting_hint(row, *, runtime=None, backend="tesseract"):
                nonlocal ocr_calls
                ocr_calls += 1
                return (None, None, "")

            with (
                mock.patch.object(parser, "_read_name", side_effect=counting_name),
                mock.patch.object(parser, "_read_description", side_effect=counting_desc),
                mock.patch.object(parser, "_read_cost_evidence", side_effect=counting_cost),
                mock.patch.object(parser, "_read_hint", side_effect=counting_hint),
            ):
                with perf.harvest_skill_ocr() as state:
                    r1 = parser._parse_row(
                        img, (30, 470, 690, 605),
                        source_page=Path("page_00.png"), page_index=0, row_index=0,
                        source_scale=(1.0, 1.0), output_dir=None, ocr_backend="rapidocr", runtime=None,
                    )
                    first_calls = ocr_calls
                    r2 = parser._parse_row(
                        img, (30, 470, 690, 605),
                        source_page=Path("page_01.png"), page_index=1, row_index=2,
                        source_scale=(2.0, 2.0), output_dir=None, ocr_backend="rapidocr", runtime=None,
                    )
                    second_calls = ocr_calls - first_calls
                    mutated = img.copy()
                    mutated[510, 640] = (0, 0, 255)
                    r3 = parser._parse_row(
                        mutated, (30, 470, 690, 605),
                        source_page=Path("page_02.png"), page_index=2, row_index=0,
                        source_scale=(1.0, 1.0), output_dir=None, ocr_backend="rapidocr", runtime=None,
                    )
                    third_calls = ocr_calls - first_calls - second_calls

            self.assertGreater(first_calls, 0)
            self.assertEqual(second_calls, 0, f"{tag}: second parse must hit row cache")
            self.assertGreater(third_calls, 0, f"{tag}: pixel mutation must miss row cache")
            self.assertEqual(state["row_cache_hits"], 1)
            self.assertEqual(r1["name"], r2["name"])
            self.assertEqual(r2["source_pages"], ["page_01.png"])
            self.assertEqual(r2["source_evidence"][0]["page_index"], 1)
            self.assertEqual(r2["source_evidence"][0]["row_index"], 2)
            self.assertEqual(r2["source_evidence"][0]["row_bbox"], [60, 940, 1380, 1210])
            self.assertEqual(r3["source_pages"], ["page_02.png"])

    def test_p0_2_measure_skill_overlap_fast_path_and_failures(self) -> None:
        rng = np.random.default_rng(42)
        base_canvas = np.full((1800, 720, 3), 245, dtype=np.uint8)
        for y in range(470, 1450, 26):
            for _ in range(14):
                x = int(rng.integers(155, 505))
                w = int(rng.integers(6, 18))
                h = int(rng.integers(10, 18))
                base_canvas[y:y + h, x:x + w] = int(rng.integers(30, 90))
        before = base_canvas[:1280].copy()

        for tag, scroll_path, pkg_root in (
            ("pacer", PACER_BRIDGE_CORE / "skill_scroll.py", PACER_BRIDGE_CORE.parent),
            ("main", MAIN_CORE / "skill_scroll.py", MAIN_CORE.parent),
        ):
            scroll = _load_isolated_module(f"test_scroll_{tag}", scroll_path, package_root=pkg_root)
            for shift in (120, 240, 350):
                after = base_canvas[shift:shift + 1280].copy()
                t0 = time.perf_counter()
                result = scroll.measure_skill_overlap(before, after)
                dt = time.perf_counter() - t0
                self.assertTrue(result["verified"], f"{tag} shift={shift}: {result}")
                self.assertLessEqual(abs(result["shift"] - shift), 1.0, f"{tag} shift={shift}: {result}")
                self.assertGreaterEqual(result["text_agreement"], 0.95)
                self.assertGreaterEqual(result["inliers"], 10)
                self.assertLess(dt, 0.15, f"{tag} fast path unexpectedly slow: {dt:.3f}s")

            self.assertFalse(scroll.measure_skill_overlap(before, before)["verified"])
            self.assertFalse(scroll.measure_skill_overlap(before, base_canvas[470:1750].copy())["verified"])
            self.assertFalse(scroll.measure_skill_overlap(None, before)["verified"])

    def test_p0_3_precheck_no_1440p_disk_write_and_720p_parity(self) -> None:
        saved_modules = sys.modules.copy()
        saved_path = list(sys.path)
        for mod_name in list(sys.modules):
            if mod_name == "core" or mod_name.startswith("core."):
                sys.modules.pop(mod_name, None)
        sys.path.insert(0, str(PACER_BACKEND))

        def restore_imports():
            for mod_name in list(sys.modules):
                if mod_name not in saved_modules:
                    sys.modules.pop(mod_name, None)
            sys.modules.update(saved_modules)
            sys.path[:] = saved_path

        self.addCleanup(restore_imports)
        from core.auto_trainer.executor import DefaultTrainingExecutionVerifier
        from core.training_menu_precheck import TrainingMenuPrechecker

        frame_720 = _make_synthetic_larc_training_frame()
        frame_1440 = cv2.resize(frame_720, (1440, 2560))

        settings_720 = frame_720.copy()
        settings_720[int(1280 * 0.02):int(1280 * 0.095), int(720 * 0.02):int(720 * 0.98)] = (60, 190, 70)
        settings_720[int(1280 * 0.895):int(1280 * 0.965), int(720 * 0.08):int(720 * 0.48)] = (252, 252, 252)
        settings_720[int(1280 * 0.895):int(1280 * 0.965), int(720 * 0.52):int(720 * 0.92)] = (60, 190, 70)
        settings_720[int(1280 * 0.915):int(1280 * 0.945), int(720 * 0.65):int(720 * 0.78)] = (252, 252, 252)
        settings_720[int(1280 * 0.10):int(1280 * 0.88), int(720 * 0.05):int(720 * 0.95)] = (252, 252, 252)
        settings_720[int(1280 * 0.10):int(1280 * 0.88), int(720 * 0.965):int(720 * 0.985)] = (190, 110, 140)
        settings_1440 = cv2.resize(settings_720, (1440, 2560))

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            p_720 = tmp_dir / "menu_720.png"
            p_1440 = tmp_dir / "menu_1440.png"
            s_720 = tmp_dir / "settings_720.png"
            s_1440 = tmp_dir / "settings_1440.png"
            assert cv2.imwrite(str(p_720), frame_720)
            assert cv2.imwrite(str(p_1440), frame_1440)
            assert cv2.imwrite(str(s_720), settings_720)
            assert cv2.imwrite(str(s_1440), settings_1440)

            verifier = DefaultTrainingExecutionVerifier(scenario="larc")
            res_verifier = verifier.precheck(p_720)
            self.assertEqual(list(tmp_dir.glob("*.precheck_1440x2560.png")), [])
            self.assertTrue(res_verifier.accepted, res_verifier)
            self.assertEqual(res_verifier.selected_tab, "speed")
            self.assertEqual(res_verifier.evidence["layout"]["normalized_from"], [720, 1280])

            checker = TrainingMenuPrechecker(scenario="larc")
            res_720 = checker.check(p_720, expected_tab="speed")
            res_1440 = checker.check(p_1440, expected_tab="speed")
            self.assertEqual(res_720.accepted, res_1440.accepted)
            self.assertEqual(res_720.selected_tab, res_1440.selected_tab)
            self.assertEqual(res_720.failures, res_1440.failures)

            set_720 = checker._check_settings_overlay(
                settings_720, hsv=cv2.cvtColor(settings_720, cv2.COLOR_BGR2HSV)
            )
            set_1440 = checker._check_settings_overlay(settings_1440)
            self.assertTrue(set_720["present"], set_720)
            self.assertEqual(set_720["present"], set_1440["present"])

            # Verify precheck(settings_720) with L'Arc blue header still visible -> fails with 'settings_overlay'
            settings_under_header_720 = settings_720.copy()
            settings_under_header_720[42:126, 15:149] = frame_720[42:126, 15:149]
            settings_under_header_1440 = cv2.resize(settings_under_header_720, (1440, 2560))
            sh_720 = tmp_dir / "settings_hdr_720.png"
            sh_1440 = tmp_dir / "settings_hdr_1440.png"
            assert cv2.imwrite(str(sh_720), settings_under_header_720)
            assert cv2.imwrite(str(sh_1440), settings_under_header_1440)
            res_sh_720 = checker.check(sh_720, expected_tab="speed")
            res_sh_1440 = checker.check(sh_1440, expected_tab="speed")
            self.assertFalse(res_sh_720.accepted)
            self.assertEqual(res_sh_720.failures, res_sh_1440.failures)
            self.assertIn("settings_overlay", res_sh_720.failures)


class TestTier1Tier2SkillCaptureSpeedups(unittest.TestCase):
    _ROOTS = (
        ("main", REPO_ROOT / "backend"),
        ("pacer", PACER_BACKEND / "bridges/skills/backend"),
    )

    def _run_with_root(self, root: Path, fn):
        import os
        saved_modules = sys.modules.copy()
        saved_path = list(sys.path)
        for mod_name in list(sys.modules):
            if (
                mod_name in {"core", "scripts", "desktop", "config"}
                or mod_name.startswith(("core.", "scripts.", "desktop.", "config."))
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

    def test_tier1_tier2_swipe_profile_and_tap_plus_and_reverse_shift_limit(self) -> None:
        for tag, root in self._ROOTS:
            def check():
                import core.skill_batch_purchase as sbp
                import core.skill_scroll as ss
                from scripts import ug_parent_skill_purchase_batch as ug

                self.assertEqual(ss.SKILL_OVERLAP_SWIPE["duration_ms"], 120, tag)
                self.assertEqual(ss.SKILL_OVERLAP_SWIPE["hold_ms"], 240, tag)
                self.assertEqual(ss.SKILL_OVERLAP_SWIPE["release_settle_ms"], 60, tag)
                shell_cmd = ss.skill_overlap_shell()
                self.assertIn("sleep 0.240", shell_cmd, tag)
                self.assertIn("sleep 0.060", shell_cmd, tag)
                # 1 nudge + 6 ease-out drag steps + 1 zero-delta hold -> 8 MOVE events.
                self.assertEqual(shell_cmd.count("input touchscreen motionevent MOVE"), 8, tag)

                rc = {"uses_skill_points_only": True, "uses_carrots": False, "uses_clocks": False, "uses_paid_currency": False}
                inv = {
                    "packet_sha256": "a" * 64,
                    "captured_at_unix_ms": 1,
                    "expires_at_unix_ms": 2,
                    "allowed_action_ids": ["1001"],
                    "current_skill_points": 300,
                    "capture_binding": {"page_hashes": ["p0", "p1"]},
                    "resource_checks": rc,
                }
                base_item = {"action_id": "1001", "display_cost": 100, "cumulative_cost": 100, "expected_remaining_skill_points": 200, "page_index": 0}
                for shift_val, expect_ok in ((420.0, True), (470.0, False)):
                    plan = {
                        "schema_version": "uma.skill_batch_plan.v1",
                        "inventory_packet_sha256": "a" * 64,
                        "decision_source": "local_auto",
                        "currency_scope": "skill_points_only",
                        "captured_at_unix_ms": 1,
                        "expires_at_unix_ms": 2,
                        "execution_allowed": False,
                        "selected_action_ids": ["1001"],
                        "items": [dict(base_item)],
                        "page_groups": [{"page_index": 0, "items": [dict(base_item)]}],
                        "initial_skill_points": 300,
                        "spent_skill_points": 100,
                        "expected_remaining_skill_points": 200,
                        "reverse_scroll_shifts": [shift_val],
                        "resource_checks": rc,
                    }
                    plan["plan_sha256"] = sbp._canonical_plan_sha256(plan)
                    with mock.patch.object(sbp, "validate_skill_inventory_packet", return_value=(True, "ok")):
                        ok, reason = sbp.validate_skill_batch_plan(plan, inv)
                    if expect_ok:
                        self.assertTrue(ok, f"{tag}: shift={shift_val} rejected: {reason}")
                    else:
                        self.assertFalse(ok, f"{tag}: shift={shift_val} should be rejected")
                        self.assertEqual(reason, "reverse_scroll_shifts_invalid", tag)

                adb = mock.Mock()
                adb.tap.return_value = True
                adb.swipe_skill_overlap.return_value = True
                session = mock.Mock()
                session.capture.return_value = {
                    "fresh": True,
                    "page_kind": "skill_acquisition",
                    "current_skill_points": 300,
                    "list_at_bottom": True,
                    "rows": [],
                    "selected_item_ids": [],
                    "parser_manifest": {},
                }
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    adapter = ug._LiveBatchAdapter(
                        adb=adb,
                        session=session,
                        review={"reviewed_rows": {}},
                        screenshot_dir=tmp_path,
                        checkpoint=tmp_path / "cp.json",
                        swipe_profile=ss.SKILL_OVERLAP_SWIPE["name"],
                    )
                    with mock.patch.object(ug.time, "sleep") as m_sleep:
                        adapter.tap_plus({"x": 640, "y": 540}, "1001")
                    self.assertLessEqual(m_sleep.call_args.args[0], 0.3, tag)

                    # Mock frame PNG for _top_probe_sha via capture_page returning bottom=True.
                    with (
                        mock.patch.object(adapter, "capture_page", return_value={
                            "current_skill_points": 300,
                            "list_at_bottom": True,
                        }),
                        mock.patch.object(adapter, "_top_probe_sha", return_value="a" * 64),
                    ):
                        self.assertTrue(adapter.begin_reverse_selection([420.0], 4), tag)
                        self.assertFalse(adapter.begin_reverse_selection([470.0], 4), tag)

            self._run_with_root(root, check)

    def test_tier2_scroll_anchor_fast_path_skips_ocr_and_fails_closed(self) -> None:
        img = _make_synthetic_skill_page()
        img2 = img.copy()
        # Mutate list viewport row pixels (outside title/Pt anchor areas): cache MUST still hit!
        img2[500:540, 200:400] = (10, 20, 30)
        img_pt_changed = img.copy()
        img_pt_changed[405:440, 560:640] = (0, 0, 250)

        for tag, root in self._ROOTS:
            def check():
                from core import skill_ocr_performance as perf
                from core.autonomous_training import observer as obs
                from core import skill_acquisition_parser as parser

                rapid_calls = 0

                def fake_rapid(crop, *, upscale=2, force_detection=False, full_resolution=False):
                    nonlocal rapid_calls
                    rapid_calls += 1
                    if crop.shape[0] <= 60 and crop.shape[1] <= 200:
                        return [{"text": "技能获取", "confidence": 0.99, "box": [[20, 10], [160, 10], [160, 50], [20, 50]]}]
                    return [
                        {"text": "当前技能点数", "confidence": 0.98, "box": [[20, 10], [200, 10], [200, 50], [20, 50]]},
                        {"text": "320", "confidence": 0.99, "box": [[520, 10], [640, 10], [640, 50], [520, 50]]},
                    ]

                with (
                    mock.patch.object(parser, "_rapidocr_ocr_data", side_effect=fake_rapid),
                    perf.harvest_skill_ocr(),
                ):
                    t1 = obs.read_skill_scroll_tokens(img, ocr=None)
                    calls_after_first = rapid_calls
                    self.assertGreater(calls_after_first, 0, tag)
                    t2 = obs.read_skill_scroll_tokens(img2, ocr=None)
                    self.assertEqual(rapid_calls, calls_after_first, f"{tag}: expected 0 OCR calls on anchor cache hit")
                    self.assertEqual(t1, t2, tag)
                    # Changing Pt pixels > 8 must invalidate fast path and re-run RapidOCR.
                    obs.read_skill_scroll_tokens(img_pt_changed, ocr=None)
                    self.assertGreater(rapid_calls, calls_after_first, f"{tag}: expected fail-closed OCR refresh on Pt change")

            self._run_with_root(root, check)

    def test_tier1_live_step_reuses_fresh_scroll_observation_and_clears_on_dispatch(self) -> None:
        img = _make_synthetic_skill_page()
        ok, raw_png = cv2.imencode(".png", img)
        self.assertTrue(ok)
        raw_bytes = raw_png.tobytes()

        for tag, live_path in (
            ("pacer", PACER_BRIDGE_CORE / "autonomous_training/live.py"),
            ("main", MAIN_CORE / "autonomous_training/live.py"),
        ):
            live_mod = _load_live_module(live_path, f"reuse_{tag}")
            with tempfile.TemporaryDirectory() as tmp:
                dev = object.__new__(live_mod.AutonomousDevice)
                dev.endpoint = "127.0.0.1:16384"
                dev.adb = "adb"
                dev.output_dir = Path(tmp)
                dev.ocr = mock.Mock()
                dev.context = {"mode": "harvest", "harvest_active": True}
                dev._last_scroll_observation = None

                commands = []

                def fake_command(*args):
                    commands.append(args)
                    if args == ("-s", dev.endpoint, "exec-out", "screencap", "-p"):
                        return raw_bytes
                    # Dispatch must never leave _last_scroll_observation intact while command executes.
                    self.assertIsNone(dev._last_scroll_observation, tag)
                    return b""

                dev.command = fake_command
                perf_mod = _load_isolated_module(
                    f"perf_{tag}",
                    live_path.parents[2] / "core/skill_ocr_performance.py",
                    package_root=live_path.parents[2],
                )
                capture_calls = 0
                obs_template = {
                    "page": "skills",
                    "actions": {
                        "scroll_skill_overlap": {"gesture": "swipe", "point": [360, 980], "end": [360, 490]},
                    },
                    "skill_list_at_bottom": False,
                    "skill_list_at_top": True,
                    "skill_list_navigation_verified": True,
                    "current_skill_points": 320,
                    "frame_sha256": "b" * 64,
                }

                def fake_observe(raw, tokens, *args, image=None):
                    nonlocal capture_calls
                    capture_calls += 1
                    res = dict(obs_template)
                    res["actions"] = {k: dict(v) for k, v in obs_template["actions"].items()}
                    return res

                with (
                    perf_mod.harvest_skill_ocr(),
                    mock.patch.object(live_mod.time, "sleep"),
                    mock.patch("core.autonomous_training.observer.read_skill_scroll_tokens", create=True, return_value=[]),
                    mock.patch("core.autonomous_training.observer.observe", create=True, side_effect=fake_observe),
                ):
                    # Inject fake observer module because live_mod imports from core.autonomous_training.observer at runtime.
                    saved_mods = sys.modules.copy()
                    saved_sys_path = list(sys.path)
                    sys.path.insert(0, str(live_path.parents[2]))
                    try:
                        import types as _types
                        obs_stub = _types.ModuleType("core.autonomous_training.observer")
                        obs_stub.read_tokens = lambda im, ocr: []
                        obs_stub.read_skill_scroll_tokens = lambda im, ocr: []
                        obs_stub.read_start_notice_tokens = lambda im, ocr: []
                        obs_stub.observe = fake_observe
                        sys.modules["core.autonomous_training.observer"] = obs_stub
                        # Also patch core.skill_ocr_performance in sys.modules to share active_harvest_ocr
                        sys.modules["core.skill_ocr_performance"] = perf_mod
                        with perf_mod.harvest_skill_ocr():
                            dev.capture_skill_scroll()
                            self.assertEqual(capture_calls, 1)
                            dev.step("scroll_skill_overlap")
                            # Reusing cached before means step('scroll_skill_overlap') only calls capture() once (for after).
                            self.assertEqual(capture_calls, 2, f"{tag}: expected 2 captures total, got {capture_calls}")
                    finally:
                        for mn in list(sys.modules):
                            if mn not in saved_mods:
                                sys.modules.pop(mn, None)
                        sys.modules.update(saved_mods)
                        sys.path[:] = saved_sys_path

    def test_tier2_shift_overlap_row_reuse_and_pt_header_reuse(self) -> None:
        # Construct 2 normalized 720x1280 pages with 2 full 165px rows shifted cleanly by 185px.
        canvas = np.full((1500, 720, 3), 255, dtype=np.uint8)
        canvas[0:468, :] = 240
        # Distinct green '+' buttons + dark name/cost text inside rows at canvas y=488..653 and y=673..838.
        for ry in (488, 673, 858):
            canvas[ry:ry + 165, 30:690] = (232, 238, 245)
            canvas[ry + 18:ry + 40, 140:320] = (40, 50, 90)
            canvas[ry + 55:ry + 90, 140:420] = (60, 70, 100)
            canvas[ry + 80:ry + 110, 535:585] = (40, 40, 120)
            canvas[ry + 65:ry + 115, 600:645] = (70, 190, 80)  # green plus
        page0 = np.full((1280, 720, 3), 240, dtype=np.uint8)
        page0[468:1029, :] = canvas[468:1029, :]
        page1 = page0.copy()
        # Scroll canvas down by 185px inside viewport [468:1029].
        page1[468:1029, :] = canvas[468 + 185:1029 + 185, :]
        # Page 1 mutated row (page 2): mark previously reused row as acquired (pink), forcing re-OCR!
        page2 = page1.copy()
        page2[488 + 50:488 + 120, 500:640] = (180, 80, 220)

        for tag, root in self._ROOTS:
            def check():
                from core import skill_ocr_performance as perf
                from core import skill_acquisition_parser as parser

                row_ocr_calls = 0
                pt_reads = 0

                def count_name(crop, ocr_backend, runtime):
                    nonlocal row_ocr_calls
                    row_ocr_calls += 1
                    return {"text": f"技能{row_ocr_calls}", "confidence": 0.97, "accepted": True, "candidates": []}

                def count_desc(crop, ocr_backend, runtime):
                    return {"text": "描述文本", "confidence": 0.96, "raw": "描述文本"}

                def count_cost(row, *, top_partial=False, runtime=None, backend="tesseract"):
                    return {"value": 120, "source": "rapidocr", "confidence": 0.98, "rapidocr_confidence": 0.98, "tesseract_confidence": None}

                def count_pt(image, *, runtime=None, backend="tesseract"):
                    nonlocal pt_reads
                    pt_reads += 1
                    return 300

                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    p0 = tmp_path / "page_00.png"
                    p1 = tmp_path / "page_01.png"
                    p2 = tmp_path / "page_02.png"
                    cv2.imwrite(str(p0), page0)
                    cv2.imwrite(str(p1), page1)
                    cv2.imwrite(str(p2), page2)

                    with (
                        mock.patch.object(parser, "resolve_ocr_backend", return_value=("rapidocr", True)),
                        mock.patch.object(parser, "_read_name", side_effect=count_name),
                        mock.patch.object(parser, "_read_description", side_effect=count_desc),
                        mock.patch.object(parser, "_read_cost_evidence", side_effect=count_cost),
                        mock.patch.object(parser, "_read_hint", return_value=(None, None, "")),
                        mock.patch.object(parser, "read_current_skill_points", side_effect=count_pt),
                        perf.harvest_skill_ocr() as state,
                    ):
                        manifest = parser.parse_skill_acquisition_pages(
                            [p0, p1, p2],
                            output_dir=tmp_path / "out",
                            ocr_backend="rapidocr",
                            overlap_evidence=[
                                {"verified": True, "page_index": 1, "shift": 185.0},
                                {"verified": True, "page_index": 2, "shift": 0.0},
                            ],
                        )
                    self.assertEqual(pt_reads, 1, f"{tag}: identical Pt header should be OCR'd once across 3 pages")
                    self.assertGreaterEqual(state["shift_row_cache_hits"], 1, f"{tag}: expected shifted row reuse")
                    self.assertEqual(len(manifest["pages"]), 3, tag)
                    # Page 0: 2 full rows + 1 bottom partial? (488..653, 673..838, 858..1023 -> 3 full rows on p0)
                    # Page 1: bottom row is new -> 1 new OCR; top 2 shifted rows reused -> total row_ocr_calls < 6.
                    self.assertLessEqual(row_ocr_calls, 5, f"{tag}: expected reused overlap rows to skip OCR, got {row_ocr_calls}")

            self._run_with_root(root, check)

    def test_tier1_prepare_skills_bg_preparse_and_fallback_reset(self) -> None:
        helper_module = _load_isolated_module(
            "release_terminal_scroll_test_helpers",
            Path(__file__).with_name("test_terminal_skill_scroll_and_stop_diag_fixes.py"),
            package_root=PACER_BACKEND,
        )
        _Helper = helper_module.TestSkillScrollOverlapHardeningAndDegradation
        before_img, after_img = _Helper._make_distinct_skill_frames(320)
        _, bad_img = _Helper._make_distinct_skill_frames(490)
        _, tail_img = _Helper._make_distinct_skill_frames(520)

        for tag, root in self._ROOTS:
            def check():
                import hashlib
                from core import skill_ocr_performance as perf
                from core import skill_acquisition_parser as parser
                from core.autonomous_training import skills as sk
                from scripts import plan_fresh_skill_acquisition as plan_api

                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    res_cat = tmp_path / "resolver.json"
                    pln_cat = tmp_path / "planner.json"
                    res_cat.write_text('{"skills": []}', encoding="utf-8")
                    pln_cat.write_text('{"skills": []}', encoding="utf-8")

                    p_top = tmp_path / "top.png"
                    p_bad = tmp_path / "bad.png"
                    p_tail = tmp_path / "tail.png"
                    p_good = tmp_path / "good.png"
                    cv2.imwrite(str(p_top), before_img)
                    cv2.imwrite(str(p_bad), bad_img)
                    cv2.imwrite(str(p_tail), tail_img)
                    cv2.imwrite(str(p_good), after_img)

                    def mkobs(p, *, top, bot, acts):
                        return {
                            "page": "skills",
                            "skill_list_at_top": top,
                            "skill_list_at_bottom": bot,
                            "skill_list_navigation_verified": True,
                            "current_skill_points": 200,
                            "screenshot_path": str(p),
                            "frame_sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                            "captured_at_unix_ms": 1_700_000_000_000,
                            "expires_at_unix_ms": 1_700_000_600_000,
                            "actions": acts,
                        }

                    # Run prepare_skills with fallback reset triggered, ensuring clean ThreadPool reset & run_fresh_dry_run invocation.
                    obs_top = mkobs(p_top, top=True, bot=False, acts={"scroll_skill_overlap": {}, "scroll_skill_reveal_bottom": {}, "scroll_skill_up": {}})
                    obs_bad = mkobs(p_bad, top=False, bot=False, acts={"scroll_skill_overlap": {}, "scroll_skill_reveal_bottom": {}, "scroll_skill_up": {}})
                    obs_tail = mkobs(p_tail, top=False, bot=True, acts={"scroll_skill_up": {}})
                    captures = [obs_top, obs_bad]

                    class Dev:
                        endpoint = sk._ALLOWED_ENDPOINT
                        context = {}
                        def capture_skill_scroll(self):
                            return captures.pop(0)
                        def capture(self):
                            return self.capture_skill_scroll()
                        def step(self, action):
                            if action == "scroll_skill_overlap":
                                return {"observation": obs_bad}
                            if action == "scroll_skill_up":
                                return {"observation": obs_top}
                            return {"observation": obs_tail}

                    single_calls = []

                    def fake_single_page(path, **kw):
                        single_calls.append(Path(path).name)
                        return {
                            "page_entries": [],
                            "entry_by_bbox": {},
                            "continuation_page": {"path": Path(path), "page_index": kw["page_index"], "scale": (1.0, 1.0), "top_fragment": None, "bottom_fragment": None, "top_entry": None, "bottom_entry": None},
                            "page_result": {"source_page": str(path), "width": 720, "height": 1280, "current_skill_points": 200, "detected_usable_rows": 0, "row_bboxes": [], "continuation_fragments": {"top_fragment": None, "bottom_fragment": None}},
                            "current_points": 200,
                            "warning": None,
                            "normalized": np.zeros((1280, 720, 3), dtype=np.uint8),
                            "pt_crop": np.zeros((65, 180, 3), dtype=np.uint8),
                        }

                    dry_stub = {
                        "status": "ready",
                        "planner_dry_run": {"status": "ready"},
                        "parser_manifest": {"current_skill_points": 200},
                        "resolver_summary": {},
                        "verdict": {"planner_verdict": "PLANNER_READY"},
                        "inventory_packet": {"execution_allowed": False},
                        "batch_plan": {"execution_allowed": False, "selected_action_ids": []},
                        "planner_observation": {"available_skills": []},
                    }
                    # Keep module name scripts.plan_fresh_skill_acquisition so bg_enabled is True!
                    def wrapped_dry_run(**kwargs):
                        parser.parse_skill_acquisition_pages(
                            kwargs["pages"],
                            output_dir=kwargs["output_dir"] / "parser_output",
                            assume_continuous_scroll=True,
                            overlap_evidence=kwargs.get("capture_manifest", {}).get("overlap_evidence"),
                        )
                        return dry_stub
                    wrapped_dry_run.__module__ = plan_api.run_fresh_dry_run.__module__

                    out_dir = tmp_path / "prep_bg"
                    with (
                        mock.patch.object(sk.time, "sleep"),
                        mock.patch.object(sk, "resolve_ocr_backend", return_value=("rapidocr", True)),
                        mock.patch.object(parser, "resolve_ocr_backend", return_value=("rapidocr", True)),
                        mock.patch.object(sk, "parse_single_skill_page", side_effect=fake_single_page),
                        mock.patch.object(parser, "parse_single_skill_page", side_effect=fake_single_page),
                        mock.patch.object(sk, "run_fresh_dry_run", wrapped_dry_run),
                        perf.harvest_skill_ocr() as st,
                    ):
                        res = sk.prepare_skills(
                            Dev(),
                            out_dir,
                            {
                                "turn_id": "t1",
                                "target_mode": "race",
                                "race_profile": {"distance_type": "medium", "style": "pace"},
                                "resolver_catalog": str(res_cat),
                                "planner_catalog": str(pln_cat),
                            },
                        )
                    self.assertEqual(res.get("status"), "ready", f"{tag}: {res}")
                    # Background thread pre-parsed the 2 fallback pages cleanly; synchronous parse_skill_acquisition_pages reused them (still 2 + initial 1 before reset = 3 total calls, not 5).
                    self.assertGreaterEqual(len(st.get("preparsed_pages", {})), 2, f"{tag}: expected preparsed_pages populated")
                    self.assertLessEqual(len(single_calls), 3, f"{tag}: expected preparsed pages to avoid duplicate parsing: {single_calls}")

            self._run_with_root(root, check)


if __name__ == "__main__":
    unittest.main()
