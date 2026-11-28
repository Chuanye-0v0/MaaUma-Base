"""Regression tests for the 6 overnight dual-emulator L'Arc cycle stop bugs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import types
import unittest
from unittest.mock import patch

import cv2
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

from core.auto_trainer import cut_farming, cut_restart_route, larc_adaptation, larc_manual_handoff, larc_ss_information_probe, race_clock_retry
from core.auto_trainer.safe_advance import SafeAdvancePlanner
from scripts import career_sieve_cycle, career_workflow_runtime, larc_ordinary_start, larc_same_account_reconnect, larc_umaa_host, ug_parent_one_turn_pipeline


def _make_full_page_clock_image(*, with_pink_banner: bool = True) -> np.ndarray:
    img = np.full((1280, 720, 3), 245, dtype=np.uint8)
    # Green header bar (BGR for ~H=55, S=180, V=180)
    img[35:90, 20:700] = (70, 180, 50)
    # Shifted/regular clock pink badge in [842..935, 180..267] (BGR pink: H~165)
    img[845:935, 180:267] = (170, 70, 230)
    if with_pink_banner:
        # Activity pink banner '每天3次免费机会！' across y=817..870, x=290..541
        img[817:870, 290:541] = (165, 55, 240)
    # Bottom left white cancel, bottom right green retry
    img[1150:1210, 65:337] = (240, 240, 240)
    img[1150:1210, 385:650] = (70, 180, 50)
    return img


def _make_pass_renewal_modal_image() -> np.ndarray:
    img = np.full((1280, 720, 3), 240, dtype=np.uint8)
    # Green modal header at 0.30..0.35
    img[384:448, 30:690] = (70, 180, 50)
    # Left white button [0.62..0.69, 0.08..0.48] with brown text pixels
    img[794:883, 60:345] = (245, 245, 245)
    img[825:850, 170:230] = (50, 90, 140)
    # Right green confirm/buy button [0.62..0.69, 0.52..0.92]
    img[794:883, 375:660] = (70, 180, 50)
    return img


class OvernightCycleSixFixesTests(unittest.TestCase):
    # ------------------------------------------------------------------
    # Bug 1: Activity pink banner '每天3次免费机会！' in race_clock_retry
    # ------------------------------------------------------------------
    def test_bug1_retry_text_accepts_arbitrary_daily_free_counts(self) -> None:
        for phrase in ("每天1次免费机会", "每天3次免费机会！", "每天5次免费", "今日免费2次", "今日1次免费"):
            parsed = race_clock_retry.retry_text(
                f"挑战结果 第4名 是否使用闹钟重新挑战？ 重新挑战次数还剩5次 {phrase}"
            )
            self.assertIsNotNone(parsed, phrase)
            self.assertTrue(parsed["free"], phrase)
            self.assertEqual(parsed["rank"], 4)
            self.assertEqual(parsed["remaining"], 5)

    def test_bug1_detect_pink_banner_and_read_shifted_cost_rois(self) -> None:
        img = _make_full_page_clock_image(with_pink_banner=True)
        bottom = race_clock_retry._detect_daily_free_banner_bottom(img, layout="full_page")
        self.assertIsNotNone(bottom)
        self.assertGreaterEqual(bottom, 860)

        seen_rois: list[tuple[int, int, int, int]] = []

        def fake_line(_im, roi, _reader=None):
            seen_rois.append(tuple(roi))
            # Fail if legacy banner-intersecting ROI (y1 <= 868) is queried first,
            # succeed cleanly on banner-below ROI (y1 >= 870).
            if roi[1] <= 868:
                return ("196", 0.84)
            return ("196", 0.997)

        with patch.object(race_clock_retry, "_line", side_effect=fake_line):
            proof = race_clock_retry.read_retry(
                img,
                "挑战结果 第5名 是否使用闹钟重新挑战？ 重新挑战次数还剩5次 每天3次免费机会！",
                reader=object(),
            )
        self.assertIsNotNone(proof)
        self.assertEqual(proof["before"], 196)
        self.assertEqual(proof["after"], 196)
        self.assertTrue(proof["free"])
        self.assertEqual(proof["layout"], "full_page")
        self.assertTrue(any(r[1] >= 870 for r in seen_rois))

    # ------------------------------------------------------------------
    # Bug 2: Windows concurrent progress.json read & atomic_json retries
    # ------------------------------------------------------------------
    def test_bug2_larc_manual_handoff_read_retries_transient_errors(self) -> None:
        with TemporaryDirectory() as td:
            p = Path(td) / "progress.json"
            p.write_text('{"next_turn": 1}', encoding="utf-8")
            calls = {"n": 0}
            orig = Path.read_text

            def flaky_read(self_path, *args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise PermissionError(13, "Permission denied", str(self_path))
                if calls["n"] == 2:
                    return "{partial"
                return orig(self_path, *args, **kwargs)

            with (
                patch.object(Path, "read_text", flaky_read),
                patch.object(larc_manual_handoff.time, "sleep"),
            ):
                data = larc_manual_handoff._read(p)
            self.assertEqual(data, {"next_turn": 1})
            self.assertEqual(calls["n"], 3)

            with (
                patch.object(Path, "read_text", side_effect=PermissionError(13, "locked")),
                patch.object(larc_manual_handoff.time, "sleep"),
            ):
                with self.assertRaises(PermissionError):
                    larc_manual_handoff._read(p)

    def test_bug2_atomic_json_unique_temp_and_workflow_runtime_read_retry(self) -> None:
        with TemporaryDirectory() as td:
            target = Path(td) / "progress.json"
            seen_tmps: list[str] = []
            orig_replace = os.replace
            attempts = {"n": 0}

            def flaky_replace(src, dst):
                seen_tmps.append(Path(src).name)
                attempts["n"] += 1
                if attempts["n"] < 3:
                    raise PermissionError(5, "Access is denied", str(dst))
                return orig_replace(src, dst)

            with (
                patch.object(cut_farming.os, "replace", side_effect=flaky_replace),
                patch.object(cut_farming, "sleep"),
            ):
                cut_farming.atomic_json(target, {"ok": True})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"ok": True})
            self.assertEqual(attempts["n"], 3)
            self.assertTrue(seen_tmps[0] != "progress.json.tmp" and seen_tmps[0].endswith(".tmp"))
            self.assertEqual(list(Path(td).glob("*.tmp")), [])

            # Also verify career_workflow_runtime.read & larc_umaa_host.read_json retry cleanly
            orig_read_text = Path.read_text
            c_calls = {"n": 0}

            def flaky_wf_read(self_path, *args, **kwargs):
                c_calls["n"] += 1
                if c_calls["n"] == 1:
                    raise PermissionError(13, "busy")
                return orig_read_text(self_path, *args, **kwargs)

            with (
                patch.object(Path, "read_text", flaky_wf_read),
                patch.object(career_workflow_runtime.time, "sleep"),
            ):
                self.assertEqual(career_workflow_runtime.read(target), {"ok": True})

            h_calls = {"n": 0}

            def flaky_host_read(self_path, *args, **kwargs):
                h_calls["n"] += 1
                if h_calls["n"] == 1:
                    raise PermissionError(13, "busy")
                return orig_read_text(self_path, *args, **kwargs)

            larc_umaa_host._JSON_CACHE.clear()
            with (
                patch.object(Path, "read_text", flaky_host_read),
                patch.object(larc_umaa_host.time, "sleep"),
            ):
                self.assertEqual(larc_umaa_host.read_json(target), {"ok": True})

    # ------------------------------------------------------------------
    # Bug 3: TP refill popup at career start vs genuine Continue modal
    # ------------------------------------------------------------------
    def test_bug3_ordinary_start_does_not_misclassify_tp_refill_modal(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            img_path = root / "tp_modal.png"
            cv2.imwrite(str(img_path), np.full((1280, 720, 3), 240, dtype=np.uint8))
            captured = {}

            class CapturingRoute:
                def __init__(self, session, perceptor, **_kwargs):
                    captured["perceptor"] = perceptor

                def __call__(self, _ctx, _pages, **_kwargs):
                    return {"ok": False, "reason": "captured_only"}

            ctx = types.SimpleNamespace(port=16384)
            with patch("scripts.larc_ordinary_setup.deck_profile_observation_enabled", return_value=False):
                larc_ordinary_start.start_preselected_ordinary_career(
                    ctx,
                    root,
                    trainee_name="艾尼斯风神",
                    trainee_variant="103102",
                    setup={"support_deck": 0, "borrow_card_id": 0, "borrow_min_level": 1, "borrow_min_break": 0},
                    route_factory=CapturingRoute,
                )
            perceptor = captured["perceptor"]

            # Case 1: Double-stacked TP refill modal where _continue_training_modal_button false-triggers
            tp_tokens = [
                {"text": "回复训练值", "bbox": [240, 390, 480, 430]},
                {"text": "关闭", "bbox": [310, 815, 410, 855]},
            ]
            with (
                patch.object(SafeAdvancePlanner, "_continue_training_modal_button", return_value=(500, 900)),
                patch("core.auto_trainer.cut_condition_probe._default_ocr_reader") as mock_reader,
                patch("core.auto_trainer.cut_condition_probe.ocr_tokens", return_value=tp_tokens),
                patch.object(
                    larc_ordinary_start,
                    "analyze_restart_frame",
                    return_value={"state": "close_tp_result", "reason": "close_tp_result", "center": [360, 835]},
                ),
            ):
                mock_reader.return_value.read_regions = lambda _im: []
                res = perceptor(img_path, phase="setup")
            self.assertEqual(res["state"], "close_tp_result")

            # Case 2: Genuine Continue Training modal -> still hands off as existing_career_handoff
            cont_tokens = [
                {"text": "继续养成", "bbox": [260, 390, 460, 430]},
                {"text": "删除数据", "bbox": [120, 760, 240, 795]},
                {"text": "取消", "bbox": [150, 880, 230, 920]},
                {"text": "继续", "bbox": [480, 880, 560, 920]},
            ]
            with (
                patch.object(SafeAdvancePlanner, "_continue_training_modal_button", return_value=(520, 900)),
                patch("core.auto_trainer.cut_condition_probe._default_ocr_reader") as mock_reader,
                patch("core.auto_trainer.cut_condition_probe.ocr_tokens", return_value=cont_tokens),
                patch.object(
                    larc_ordinary_start,
                    "analyze_restart_frame",
                    return_value={"state": "unknown", "reason": "restart_page_unrecognized"},
                ),
            ):
                mock_reader.return_value.read_regions = lambda _im: []
                res = perceptor(img_path, phase="setup")
            self.assertEqual(res, {"state": "existing_career_handoff", "reason": "existing_career_visible"})

    # ------------------------------------------------------------------
    # Bug 4: Transient unreadable adaptation frames wait up to 3 observations
    # ------------------------------------------------------------------
    def test_bug4_adaptation_grid_unreadable_retries_then_recovers_or_stops(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            img_path = root / "adapt.png"
            img = np.full((1280, 720, 3), 240, dtype=np.uint8)
            cv2.imwrite(str(img_path), img)
            sess_path = root / "adapt_sess.json"
            plan = larc_adaptation.build_adaptation_plan(
                {"levels": [1, 1, 1, 1, 0, 0, 0, 0, 0, 0], "points": 300, "pending_cost": 0},
                phase="first_exchange",
            )
            session = larc_adaptation.LarcAdaptationSession(
                "run-bug4",
                sess_path,
                plan=plan,
                source_sha256="b" * 64,
                stage="selecting",
                control_waits={},
            )
            bad_reader = types.SimpleNamespace(
                read_grid=lambda _im: {
                    "levels": [None, 1, 1, 1, 0, 0, 0, 0, 0, 0],
                    "pending_cost": 0,
                    "points": 300,
                    "errors": ["selected_slot_unreadable"],
                    "selected_slot": 1,
                }
            )
            ev = {"image": str(img_path), "screen_text": {"normalized_text": "海外适应性 获得条件一览 消耗积分"}}
            d1 = larc_adaptation.plan_larc_adaptation(img, ev, session=session, reader=bad_reader)
            d2 = larc_adaptation.plan_larc_adaptation(img, ev, session=session, reader=bad_reader)
            d3 = larc_adaptation.plan_larc_adaptation(img, ev, session=session, reader=bad_reader)
            self.assertEqual((d1["action"], d1["reason"]), ("wait", "larc_adaptation_grid_loading"))
            self.assertEqual((d2["action"], d2["reason"]), ("wait", "larc_adaptation_grid_loading"))
            self.assertEqual((d3["action"], d3["reason"]), ("wait", "larc_adaptation_grid_loading"))
            self.assertEqual(session.control_waits["grid_unreadable"], 3)

            # Recovery before 4th failure clears counter
            clean_reader = types.SimpleNamespace(
                read_grid=lambda _im: {
                    "levels": [1, 1, 1, 1, 0, 0, 0, 0, 0, 0],
                    "pending_cost": 0,
                    "points": 300,
                    "errors": [],
                    "selected_slot": 1,
                }
            )
            with patch.object(larc_adaptation, "_adaptation_control", return_value={"status": "enabled", "kind": "level_up"}):
                d_ok = larc_adaptation.plan_larc_adaptation(img, ev, session=session, reader=clean_reader)
            self.assertEqual(d_ok["action"], "tap")
            self.assertNotIn("grid_unreadable", session.control_waits)

            # 4th consecutive unreadable stops cleanly
            for _ in range(3):
                d = larc_adaptation.plan_larc_adaptation(img, ev, session=session, reader=bad_reader)
                self.assertEqual(d["action"], "wait")
            d4 = larc_adaptation.plan_larc_adaptation(img, ev, session=session, reader=bad_reader)
            self.assertEqual((d4["action"], d4["reason"]), ("stop", "larc_adaptation_unreadable"))

    # ------------------------------------------------------------------
    # Bug 5: Overseas expedition (Classic/Senior Jul-Sep) skips SS info cleanly
    # ------------------------------------------------------------------
    def test_bug5_overseas_ss_unavailable_detection_and_probe_return(self) -> None:
        img = np.full((1280, 720, 3), 240, dtype=np.uint8)
        # Dimmed blue 6th SS slot at [1035:1115, 610:698]: BGR -> H=105, S=90, V=140
        img[1035:1115, 610:698] = (140, 115, 90)
        with patch("core.auto_trainer.larc_preview_text._reader", return_value=lambda _crop: ("经典级 7月上半月", 0.99)):
            overseas = larc_ss_information_probe._read_overseas_ss_unavailable(img, 720, 1280)
        self.assertIsNotNone(overseas)
        self.assertEqual(overseas["reason"], "overseas_expedition_ss_unavailable")
        self.assertIn("经典级7月上半月", overseas["calendar_text"])

        with TemporaryDirectory() as td:
            root = Path(td)
            run_root = root / "run_overseas"
            out_dir = run_root / "ss_information"
            (run_root / "session").mkdir(parents=True, exist_ok=True)
            route_path = run_root / "session/transition_session.json"
            from core.auto_trainer.route_transition_session import RouteTransitionSession, RouteTransitionStatus
            sess = RouteTransitionSession.new("run_overseas")
            sess.status = RouteTransitionStatus.TRACKING
            sess.current_state = "training_menu"
            sess.observation_sha256 = "0" * 64
            sess.save(route_path)
            roster = types.SimpleNamespace(
                needs_ss_information=True,
                reward_cycles_initialized=False,
                _check_source=lambda _ctx: None,
                save=lambda _p: None,
            )
            roster_path = run_root / "session/larc_roster.json"

            class DummyAdb:
                def connect(self):
                    return True

                def screenshot(self, path):
                    cv2.imwrite(str(path), img)
                    return img

            probe = larc_ss_information_probe.LarcSSInformationProbe(
                adb=DummyAdb(),
                safety_analyzer=types.SimpleNamespace(analyze=lambda _p: types.SimpleNamespace(
                    source_image_sha256="s" * 64, visible_text="海外远征中", dangerous=False,
                    screen="training_menu", stop_reason=None, resource_terms=())),
                sleep_fn=lambda _s: None,
            )
            fake_nav = {
                "page_verified": False,
                "open": None,
                "close": None,
                "scroll": None,
                "information": {"page_verified": False, "rows": []},
                "ss_locked": None,
                "ss_overseas_unavailable": overseas,
            }
            from dataclasses import dataclass

            @dataclass
            class _Precheck:
                accepted: bool = True
                selected_tab: str = "speed"

            precheck = _Precheck()
            with (
                patch.object(larc_ss_information_probe, "read_navigation", return_value=fake_nav),
                patch.object(probe.prechecker, "check", return_value=precheck),
                patch("core.auto_trainer.larc_ss_information_probe.RouteTransitionSession.observe", return_value="a" * 64),
            ):
                rep = probe.run(
                    run_session_id="run_overseas",
                    turn_index=1,
                    roster=roster,
                    roster_path=roster_path,
                    transition_session_path=route_path,
                    output_dir=out_dir,
                    execute=True,
                    confirm_token=larc_ss_information_probe.PROBE_CONFIRM_TOKEN,
                )
            self.assertEqual(rep["status"], "optional_unavailable")
            self.assertEqual(rep["reason"], "overseas_expedition_no_ss_information")
            self.assertTrue(rep["returned_to_training"])
            self.assertFalse(rep["turn_consumed"])
            self.assertFalse(rep["turn_consuming_action_executed"])

    # ------------------------------------------------------------------
    # Bug 6: Terminal restart takeover, 05:00 popups, multi-device conflict
    # ------------------------------------------------------------------
    def test_bug6_active_career_end_confirmation_and_cut_restart_popups(self) -> None:
        self.assertTrue(
            career_sieve_cycle._is_active_career_scene(
                {"owner": "common", "state": "modal", "safe_reason": "career_end_confirmation"}
            )
        )
        with TemporaryDirectory() as td:
            frame_path = Path(td) / "frame.png"
            cv2.imwrite(str(frame_path), _make_pass_renewal_modal_image())
            dummy_cfg = types.SimpleNamespace(max_tp_items=1, max_carrots=0, discard_found_for_validation=False)

            def run_restart(tokens, boxes, *, skip_pt=None):
                with (
                    patch.object(cut_restart_route, "connecting_overlay_visible", return_value=False),
                    patch.object(cut_restart_route, "career_comic_loading_visible", return_value=False),
                    patch.object(cut_restart_route, "ocr_tokens", return_value=tokens),
                    patch.object(cut_restart_route, "button_boxes", return_value=boxes),
                    patch.object(cut_restart_route.SafeAdvancePlanner, "_looks_like_white_loading_tips", return_value=False),
                    patch.object(cut_restart_route.SafeAdvancePlanner, "_received_login_reward_skip_point", return_value=skip_pt),
                ):
                    return cut_restart_route.analyze_restart_frame(
                        frame_path, config=dummy_cfg, phase="setup", check_start_tp=False
                    )

            # 6A: Terminal career_end_confirmation routes to existing_career_handoff
            term_tokens = [
                {"text": "养成结束确认", "bbox": [260, 390, 460, 430]},
                {"text": "养成结束", "bbox": [450, 815, 580, 855]},
            ]
            self.assertEqual(run_restart(term_tokens, [])["state"], "existing_career_handoff")

            # 6B: Pass renewal modal dismisses ONLY via 取消, never buy
            pass_tokens = [
                {"text": "因子通行证", "bbox": [260, 390, 460, 430]},
                {"text": "因子通行证即将到期，购买高级通行证", "bbox": [120, 560, 600, 610]},
                {"text": "取消", "bbox": [170, 815, 240, 855]},
                {"text": "购买", "bbox": [480, 815, 560, 855]},
            ]
            pass_res = run_restart(pass_tokens, [(60, 794, 285, 883), (375, 794, 285, 883)])
            self.assertEqual(pass_res["state"], "dismiss_home_popup")
            self.assertEqual(pass_res["popup_kind"], "pass_renewal_cancel")
            self.assertLess(pass_res["center"][0], 360)

            # 6B: 05:00 Login reward skip in setup phase
            reward_tokens = [{"text": "获得了", "bbox": [280, 450, 420, 490]}, {"text": "宝石", "bbox": [310, 510, 390, 550]}]
            rw_res = run_restart(reward_tokens, [], skip_pt=(655, 1216))
            self.assertEqual(rw_res["state"], "dismiss_home_popup")
            self.assertEqual(rw_res["popup_kind"], "login_reward_skip")
            self.assertEqual(rw_res["center"], [655, 1216])

            # 6C: Multi-device conflict stops immediately
            conflict_tokens = [
                {"text": "连接冲突错误", "bbox": [240, 390, 480, 430]},
                {"text": "已在其他设备登录，即将返回标题界面", "bbox": [120, 560, 600, 610]},
            ]
            cf_res = run_restart(conflict_tokens, [])
            self.assertEqual(cf_res["state"], "unknown")
            self.assertEqual(cf_res["reason"], "account_login_conflict")

    def test_bug6_login_reward_white_banner_and_pass_renewal_cancel(self) -> None:
        # White reward banner on kimono blurred background (62c1 style)
        reward_img = np.full((1280, 720, 3), 110, dtype=np.uint8)
        reward_img[640:768, 60:660] = (245, 245, 245)  # centered white reward banner
        with patch.object(SafeAdvancePlanner, "_bottom_right_skip_button", return_value=(655, 1216)):
            pt = SafeAdvancePlanner._received_login_reward_skip_point(
                reward_img,
                {"buttons": []},
                {"normalized_text": "获得了\n宝石", "resource_terms": ["宝石"]},
            )
        self.assertEqual(pt, (655, 1216))

        # Factor pass renewal cancel button chooses ONLY left white button
        modal_img = _make_pass_renewal_modal_image()
        cancel_pt = SafeAdvancePlanner._pass_renewal_cancel_button(
            {"normalized_text": "因子通行证\n因子通行证已到期\n取消\n购买"},
            modal_img,
        )
        self.assertIsNotNone(cancel_pt)
        self.assertLess(cancel_pt[0], 360)

        # Full planner path with scenario="larc" and dangerous=True clicks ONLY cancel
        with TemporaryDirectory() as td:
            mp = Path(td) / "pass_modal.png"
            cv2.imwrite(str(mp), modal_img)
            with (
                patch("core.auto_trainer.advance.planner.Perception.analyze_image", return_value={"screen": "unknown", "buttons": []}),
                patch(
                    "core.auto_trainer.advance.planner.analyze_screen_text",
                    return_value={
                        "normalized_text": "因子通行证\n因子通行证已到期\n取消\n购买",
                        "dangerous": True,
                        "resource_terms": ["购买"],
                        "primary_hint": {},
                    },
                ),
            ):
                dec = SafeAdvancePlanner().plan(mp, scenario="larc", visible_text="因子通行证 取消 购买")
            self.assertEqual((dec["action"], dec["reason"]), ("tap", "close_pass_renewal_popup"))
            self.assertEqual(dec["tap"]["coordinate_name"], "safe_advance.pass_renewal_cancel")
            self.assertLess(dec["tap"]["x"], 360)

    def test_bug6_multi_device_conflict_never_auto_kicks_across_planners_and_reconnect(self) -> None:
        planner = SafeAdvancePlanner()
        with TemporaryDirectory() as td:
            p = Path(td) / "conflict.png"
            img = np.full((1280, 720, 3), 240, dtype=np.uint8)
            cv2.imwrite(str(p), img)

            # Multi-device conflict -> account_login_conflict (both dangerous=True and False)
            for dangerous in (False, True):
                with (
                    patch.object(SafeAdvancePlanner, "_connection_conflict_return_title_button", return_value=(360, 835)),
                    patch.object(SafeAdvancePlanner, "_looks_like_training_home_controls", return_value=False),
                    patch("core.auto_trainer.advance.planner.Perception.analyze_image", return_value={"screen": "unknown", "buttons": []}),
                    patch(
                        "core.auto_trainer.advance.planner.analyze_screen_text",
                        return_value={
                            "normalized_text": "连接冲突错误\n已在其他设备登录\n即将返回标题界面\n返回标题界面",
                            "dangerous": dangerous,
                            "resource_terms": [],
                            "primary_hint": {},
                        },
                    ),
                ):
                    dec = planner.plan(p, scenario="larc", visible_text="连接冲突错误 已在其他设备登录")
                self.assertEqual((dec["action"], dec["reason"]), ("stop", "account_login_conflict"))

            # Idle timeout disconnect -> STILL returns return_title_from_connection_conflict
            with (
                patch.object(SafeAdvancePlanner, "_connection_conflict_return_title_button", return_value=(360, 835)),
                patch.object(SafeAdvancePlanner, "_looks_like_training_home_controls", return_value=False),
                patch("core.auto_trainer.advance.planner.Perception.analyze_image", return_value={"screen": "unknown", "buttons": []}),
                patch(
                    "core.auto_trainer.advance.planner.analyze_screen_text",
                    return_value={
                        "normalized_text": "连接已断开\n由于长时间内未进行操作与服务器的连接发生中断\n返回标题页面",
                        "dangerous": False,
                        "resource_terms": [],
                        "primary_hint": {},
                    },
                ),
            ):
                idle_dec = planner.plan(p, scenario="larc", visible_text="连接已断开 返回标题页面 长时间内未进行操作")
            self.assertEqual((idle_dec["action"], idle_dec["reason"]), ("tap", "return_title_from_connection_conflict"))

            # ReconnectPlanner also refuses to return_title on multi-device conflict
            rec_planner = larc_same_account_reconnect.ReconnectPlanner()
            with (
                patch.object(
                    SafeAdvancePlanner,
                    "plan",
                    return_value={
                        "action": "stop",
                        "reason": "connection_conflict",
                        "evidence": {"screen_text": {"normalized_text": "连接冲突错误\n已在其他设备登录"}},
                    },
                ),
                patch.object(SafeAdvancePlanner, "_connection_conflict_return_title_button", return_value=(360, 835)),
            ):
                r_dec = rec_planner.plan(p, scenario="larc")
            self.assertEqual((r_dec["action"], r_dec["reason"]), ("stop", "account_login_conflict"))


if __name__ == "__main__":
    unittest.main()
