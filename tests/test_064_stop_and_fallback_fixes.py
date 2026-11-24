"""Regression tests for 0.6.4 stop, fallback, skill recovery, capture failure, and race_list enter/exit fixes."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import types
import unittest
from unittest.mock import patch

import cv2
import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
PACER_BACKEND = Path(os.environ.get("MAAUMA_PACER_BACKEND_DIR") or (REPO_ROOT / "pacer" / "backend"))


def _ensure_pacer_backend_path() -> dict[str, object]:
    resolved = str(PACER_BACKEND.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)
    injected: dict[str, object] = {}
    if "config.settings" not in sys.modules:
        try:
            import config.settings  # noqa: F401
        except ImportError:
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
            sys.modules["config"] = cfg_mod
            sys.modules["config.settings"] = cfg_set
            injected = {"config": cfg_mod, "config.settings": cfg_set}
    return injected


def _write_png(path: Path, image: np.ndarray) -> str:
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise AssertionError("Failed to encode test PNG")
    data = encoded.tobytes()
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _synthetic_adaptation_grid_frame() -> np.ndarray:
    """720x1280 BGR frame with an active adaptation grid blue header and white body."""
    img = np.full((1280, 720, 3), 24, dtype=np.uint8)
    # Blue details header: hsv[655:682, 50:670] -> H in (100, 130), S > 100
    img[655:682, 50:670] = (230, 110, 20)
    # White body: hsv[775:965, 60:660] -> S < 65, V > 175
    img[775:965, 60:660] = (245, 245, 245)
    return img


def _synthetic_larc_home_frame() -> np.ndarray:
    """720x1280 BGR image matching L'Arc main menu HSV anchors."""
    img = np.full((1280, 720, 3), 24, dtype=np.uint8)
    img[42:126, 15:149] = (230, 120, 30)
    img[75:136, 158:711] = (245, 245, 245)
    img[825:906, 25:591] = (245, 245, 245)
    img[930:1045, 25:210] = (60, 210, 60)
    img[930:1045, 220:500] = (230, 120, 30)
    img[930:1045, 515:695] = (230, 120, 30)
    return img


def _synthetic_large_bottom_dialogue_frame() -> np.ndarray:
    img = np.full((1280, 720, 3), 30, dtype=np.uint8)
    img[896:1164, 15:705] = (245, 245, 245)
    img[905:935, 30:120] = (60, 110, 160)
    return img


def _synthetic_warning_modal_frame() -> np.ndarray:
    """720x1280 BGR image triggering _go_to_race_modal_button + green confirm button perception."""
    img = np.full((1280, 720, 3), 40, dtype=np.uint8)
    # Green header: height * [0.23, 0.31] = [294, 396] -> H in [38, 92], S > 70, V > 80
    img[295:395, 20:700] = (70, 205, 70)
    # White modal body
    img[395:1000, 25:695] = (245, 245, 245)
    # Warning red/orange strip: height * [0.31, 0.40] = [396, 512]
    img[410:470, 80:640] = (30, 120, 240)
    # Cancel white button (left) & Primary green confirm button (right): cy in [0.66, 0.76]*1280 = [845, 972], cx in [0.45, 0.85]*720
    img[865:945, 375:645] = (65, 210, 65)  # w=270 (0.375), h=80 (0.0625), cx=510 (0.708), cy=905 (0.707)
    return img


class Test064StopAndFallbackFixes(unittest.TestCase):
    def setUp(self) -> None:
        self._injected_cfg = _ensure_pacer_backend_path()
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
        for mod_name in self._injected_cfg:
            sys.modules.pop(mod_name, None)

    def test_fix1_adaptation_return_requires_leaving_grid_and_recovers(self) -> None:
        from core.auto_trainer.advance.executor import SafeAdvanceExecutor
        from core.auto_trainer.config import UGParentGoal
        from core.auto_trainer.larc_adaptation import LarcAdaptationSession
        from core.auto_trainer.larc_control_wait import adaptation_controls
        from core.auto_trainer.route_transition_session import RouteTransitionSession
        from core.auto_trainer.skip_session import RunSkipSession
        import scripts.larc_autonomy_growth as lag

        grid_frame = _synthetic_adaptation_grid_frame()
        self.assertTrue(adaptation_controls(grid_frame, allow_home=False, require_leave_grid=False))
        self.assertFalse(adaptation_controls(grid_frame, allow_home=True, require_leave_grid=True))

        home_frame = _synthetic_larc_home_frame()
        with (
            patch.object(SafeAdvanceExecutor, "_career_home_controls_ready", return_value=True),
            patch("core.auto_trainer.larc_pages.larc_race_day_controls", return_value=False),
        ):
            self.assertTrue(adaptation_controls(home_frame, allow_home=True, require_leave_grid=True))

        with TemporaryDirectory() as td:
            root = Path(td) / "run_fix1"
            (root / "session").mkdir(parents=True, exist_ok=True)
            route_path = root / "session" / "transition_session.json"
            skip_path = root / "session" / "skip_session.json"
            sess_path = root / "session" / "inspect.json"
            goal = UGParentGoal.default().to_dict()
            goal["scenario"] = "larc"
            (root / "goal.json").write_text(json.dumps(goal), encoding="utf-8")
            RouteTransitionSession.new("run_fix1").save(route_path)
            RunSkipSession.new("run_fix1").save(skip_path)
            sess = LarcAdaptationSession(
                "run_fix1",
                sess_path,
                phase="first_exchange",
                inspect_only=True,
                inspect_grid_only=True,
                stay_on_grid=True,
            )
            sess.save()

            ctx = SimpleNamespace(root=root, run_id="run_fix1", port=16384, route=route_path, skip=skip_path)
            adv_calls: list[object] = []

            def fake_advance(args_obj):
                adv_calls.append(args_obj)
                if len(adv_calls) == 1:
                    s = LarcAdaptationSession.load(sess_path, "run_fix1")
                    s.returned = True
                    s.save()
                    return {
                        "is_valid": False,
                        "stop_reason": "route_state_out_of_scope",
                        "step_reports": [{"result": {"decision": {"reason": "larc_adaptation_return"}}}],
                    }, 1
                return {
                    "is_valid": True,
                    "stop_reason": "ready_main_menu",
                    "source_screenshot_path": str(root / "menu.png"),
                    "step_reports": [],
                }, 0

            fake_adb = SimpleNamespace(connect=lambda: True, close=lambda: None)
            with (
                patch.object(lag, "native_instance_id", return_value=1),
                patch.object(lag, "advance", side_effect=fake_advance),
                patch.object(lag, "advance_args", side_effect=lambda argv: SimpleNamespace(argv=argv)),
                patch("core.native_capture_adb.adb_for_capture", return_value=fake_adb),
                patch("core.execution_frames.wait_for_frame", return_value=SimpleNamespace(value=True, frame=home_frame)),
            ):
                res = lag.return_from_growth_inspection(ctx, sess_path, root / "ret_out")

            self.assertTrue(res["ok"])
            self.assertEqual(res["reason"], "ready_main_menu")
            self.assertEqual(len(adv_calls), 2)
            self.assertIs(getattr(adv_calls[1], "_initial_frame", None), home_frame)

    def test_fix2_reliable_training_fallback_picks_highest_finite_score(self) -> None:
        from core.auto_trainer.live_loop import UGParentTrainingLoop

        state = SimpleNamespace()
        decision = {
            "scored_candidates": [
                {
                    "training_type": "stamina",
                    "action_id": "train:stamina",
                    "rejected": False,
                    "source_confidence": 0.92,
                    "score": 12.92,
                },
                {
                    "training_type": "power",
                    "action_id": "train:power",
                    "rejected": True,
                    "source_confidence": 0.99,
                    "score": 99.0,
                },
                {
                    "training_type": "guts",
                    "action_id": "train:guts",
                    "rejected": False,
                    "source_confidence": 0.90,
                    "score": math.nan,
                },
                {
                    "training_type": "wisdom",
                    "action_id": "train:wisdom",
                    "rejected": False,
                    "hard_rejected": False,
                    "eligible": True,
                    "source_confidence": 0.95,
                    "score": 44.52,
                },
            ]
        }
        with patch.object(UGParentTrainingLoop, "_selected_primary_gain_is_reliable", return_value=True):
            picked = UGParentTrainingLoop._reliable_training_fallback_candidate(state, decision)

        self.assertIsNotNone(picked)
        cand, conf = picked
        self.assertEqual(cand["training_type"], "wisdom")
        self.assertAlmostEqual(conf, 0.95, places=6)

    def test_fix3_skill_list_recovery_text_not_dangerous_and_adaptation_preserves_stop_reason(self) -> None:
        from core.auto_trainer.config import UGParentGoal
        from core.auto_trainer.route_transition_session import RouteTransitionSession
        from core.auto_trainer.skip_session import RunSkipSession
        from core.screen_text import analyze_screen_text, resource_terms_from_state
        import scripts.larc_autonomy_adaptation as laa

        skill_list_text = "\n".join([
            "α-star* Lv2",
            "最后冲刺时若处于领先集团则恢复体力",
            "启发等级 Lv3",
            "当前技能点数 580",
            "弧线大师",
            "弯道加速 10%OFF",
            "返回",
            "确定",
        ])
        analyzed = analyze_screen_text(skill_list_text)
        self.assertFalse(analyzed["dangerous"])
        self.assertEqual(analyzed["resource_keyword_policy"], "disabled_in_skill_list")
        self.assertEqual(resource_terms_from_state({"screen_text": analyzed}), [])

        real_prompt = analyze_screen_text("是否使用TP恢复体力？\n消耗 TP 30\n使用\n取消")
        self.assertTrue(real_prompt["dangerous"])
        self.assertEqual(real_prompt["resource_keyword_policy"], "enabled")

        with TemporaryDirectory() as td:
            root = Path(td) / "run_fix3"
            (root / "session").mkdir(parents=True, exist_ok=True)
            goal = UGParentGoal.default().to_dict()
            goal["scenario"] = "larc"
            (root / "goal.json").write_text(json.dumps(goal), encoding="utf-8")
            (root / "progress.json").write_text(
                json.dumps({"run_session_id": "run_fix3", "device": "127.0.0.1:16384", "next_turn": 20}),
                encoding="utf-8",
            )
            RouteTransitionSession.new("run_fix3").save(root / "session" / "transition_session.json")
            RunSkipSession.new("run_fix3").save(root / "session" / "skip_session.json")
            from core.auto_trainer.career_goal_ledger import CareerGoalLedger, CareerGoalLedgerStore
            CareerGoalLedgerStore().write_atomic(
                root / "session" / "career_goal_ledger.json",
                CareerGoalLedger.empty("run_fix3"),
            )

            with (
                patch.object(laa, "native_instance_id", return_value=None),
                patch.object(
                    laa,
                    "advance",
                    return_value=({"is_valid": False, "stop_reason": "resource_prompt", "performed_steps": 0}, 2),
                ),
            ):
                rep, code = laa.run_larc_adaptation(
                    run_root=root,
                    output_dir=root / "adapt_out",
                    adb_port=16384,
                    phase="first_exchange",
                )
            self.assertEqual(code, 2)
            self.assertEqual(rep["stop_reason"], "resource_prompt")

    def test_fix4_live_takeover_capture_failed_maps_to_capture_failed(self) -> None:
        from core.auto_trainer.config import UGParentGoal
        from core.auto_trainer.route_transition_session import RouteTransitionSession
        from core.auto_trainer.skip_session import RunSkipSession
        import scripts.larc_autonomous_run as lar

        with TemporaryDirectory() as td:
            root = Path(td) / "run_fix4"
            (root / "session").mkdir(parents=True, exist_ok=True)
            goal = UGParentGoal.default().to_dict()
            goal["scenario"] = "larc"
            goal["total_turns"] = 78
            (root / "goal.json").write_text(json.dumps(goal), encoding="utf-8")
            (root / "progress.json").write_text(
                json.dumps({"run_session_id": "run_fix4", "device": "127.0.0.1:59999", "next_turn": 1, "actions_completed": 0}),
                encoding="utf-8",
            )
            RouteTransitionSession.new("run_fix4").save(root / "session" / "transition_session.json")
            RunSkipSession.new("run_fix4").save(root / "session" / "skip_session.json")
            (root / "session" / "larc_member_roster.json").write_text(
                json.dumps({"run_session_id": "run_fix4", "schema_version": "larc_member_roster.v1", "supports": []}),
                encoding="utf-8",
            )

            def raise_capture_error(ctx, output, **_kwargs):
                err = RuntimeError("live_takeover_capture_failed")
                err.capture_failure = {
                    "schema_version": "uma.capture_failure.v1",
                    "backend": "mumu-ipc",
                    "stage": "capture",
                    "reason": "capture_failed",
                }
                raise err

            args = lar.parse_args([
                "--run-root", str(root),
                "--adb-port", "59999",
                "--max-actions", "1",
                "--execute",
                "--confirm-token", "LARC_AUTONOMY_TEST",
            ])
            with (
                patch.object(lar, "_make_transition_probe", return_value=None),
                patch.object(lar, "_terminal_finish_verified", return_value=False),
                patch("scripts.larc_live_takeover.observe_current", side_effect=raise_capture_error),
                patch("core.auto_trainer.cut_stop_diagnostic.save_ordinary_stop_diagnostic", return_value={"status": "unavailable"}),
                patch("core.auto_trainer.recognition_failure_archive.archive_abnormal_stop", return_value={}),
                patch("core.auto_trainer.recognition_failure_archive.archive_segment_failure", return_value={}),
            ):
                summary, code = lar.run(args)

            self.assertEqual(code, 2)
            self.assertEqual(summary["stop_reason"], "capture_failed")
            self.assertIn("capture_failure", summary)

    def test_fix5_dialogue_nudge_safe_coords_and_home_guards(self) -> None:
        from core.auto_trainer.advance.executor import SafeAdvanceExecutor
        from core.auto_trainer.advance.planner import SafeAdvancePlanner
        from core.auto_trainer.route_transition_session import RouteTransitionSession
        from core.auto_trainer.skip_session import RunSkipSession
        import scripts.larc_autonomous_run as lar

        dlg = _synthetic_large_bottom_dialogue_frame()
        home = _synthetic_larc_home_frame()

        def run_nudge_case(*, norm_text: str, fresh_frame: np.ndarray | None, home_ready_on_fresh: bool) -> list[tuple[int, int, bool]]:
            with TemporaryDirectory() as td:
                root = Path(td) / "run_5a1"
                (root / "session").mkdir(parents=True, exist_ok=True)
                img_path = root / "dlg.png"
                _write_png(img_path, dlg)
                run_id = root.name
                route_path = root / "session" / "transition_session.json"
                skip_path = root / "session" / "skip_session.json"
                ledger_path = root / "session" / "career_goal_ledger.json"
                RouteTransitionSession.new(run_id).save(route_path)
                RunSkipSession.new(run_id).save(skip_path)

                taps: list[tuple[int, int, bool]] = []
                clock = {"now": 100.0}
                adb_ns = SimpleNamespace(tap=lambda x, y, scaled=False: taps.append((x, y, scaled)))
                if fresh_frame is not None:
                    adb_ns.read_frame = lambda deadline=None: fresh_frame
                fake_probe = SimpleNamespace(
                    adb=adb_ns,
                    observe=lambda action_turn, skip_enabled: {
                        "surface": None,
                        "fallback": True,
                        "frame": dlg.copy(),
                        "progress_frame": None,
                        "progressed": False,
                    },
                )
                ctx = SimpleNamespace(
                    root=root,
                    run_id=run_id,
                    port=16384,
                    route=route_path,
                    skip=skip_path,
                    ledger=ledger_path,
                    progress={"device": "127.0.0.1:16384", "next_turn": 24},
                    allow_fixed_races=False,
                    allow_adaptations=False,
                    transition_probe=fake_probe,
                    screen_reader=SimpleNamespace(),
                )
                idx = {"n": 0}
                times = [100.0, 108.0, 109.0]

                def fake_adv(_args):
                    i = idx["n"]
                    idx["n"] += 1
                    clock["now"] = times[min(i, len(times) - 1)]
                    if i >= 2:
                        return {
                            "is_valid": True,
                            "stop_reason": "ready_main_menu",
                            "source_screenshot_path": str(img_path),
                            "step_reports": [{"result": {"decision": {"evidence": {"reader": "ok"}}}}],
                        }, 0
                    return {
                        "is_valid": True,
                        "stop_reason": "no_progress_timeout",
                        "step_reports": [{
                            "result": {
                                "performed": False,
                                "tap_performed": False,
                                "frame_safety": {"dangerous": False},
                                "decision": {
                                    "action": "wait",
                                    "reason": "bottom_skip_double_enabled",
                                    "route_state": "event_dialogue",
                                    "evidence": {
                                        "image": str(img_path),
                                        "screen_text": {"dangerous": False, "normalized_text": norm_text},
                                    },
                                },
                            }
                        }],
                    }, 1

                summary = {"route_reports": []}
                with (
                    patch.object(lar, "monotonic", side_effect=lambda: clock["now"]),
                    patch.object(lar, "sleep", lambda _s: None),
                    patch.object(lar, "advance", side_effect=fake_adv),
                    patch.object(lar, "observe_mei_unlock", lambda *a, **k: None),
                    patch.object(lar, "advance_args", lambda argv: SimpleNamespace()),
                    patch.object(
                        SafeAdvanceExecutor,
                        "_career_home_controls_ready",
                        side_effect=lambda im: True if idx["n"] >= 3 else (home_ready_on_fresh if fresh_frame is not None and im is fresh_frame else False),
                    ),
                    patch.object(SafeAdvancePlanner, "_looks_like_confirm_modal_panel", return_value=False),
                    patch("core.auto_trainer.event_options.event_option_rows_ready", return_value=False),
                    patch.object(SafeAdvancePlanner, "_looks_like_training_home_controls", return_value=False),
                    patch.object(SafeAdvancePlanner, "_looks_like_large_bottom_dialogue", return_value=True),
                ):
                    _src, reason = lar._route_to_menu(ctx, root / "out", summary, lambda: None)
                self.assertEqual(reason, "ready_main_menu")
                return taps

        # Case A: Genuine dialogue stall -> safe empty left-middle tap (14%, 45%), NOT inside main_menu.race
        taps_ok = run_nudge_case(norm_text="普通系米拉子今天也在散步。", fresh_frame=None, home_ready_on_fresh=False)
        self.assertEqual(taps_ok, [(int(720 * 0.14), int(1280 * 0.45), False)])

        # Case B: Home markers already present in OCR -> no tap
        taps_home_text = run_nudge_case(norm_text="参赛条件 剩余 826人\n海外适应性\n休息", fresh_frame=None, home_ready_on_fresh=False)
        self.assertEqual(taps_home_text, [])

        # Case C: Fresh frame shows home controls ready -> no tap
        taps_fresh_home = run_nudge_case(norm_text="普通对话文本", fresh_frame=home, home_ready_on_fresh=True)
        self.assertEqual(taps_fresh_home, [])

    def test_fix5_larc_fan_warning_cancels_unless_maiden_first_win(self) -> None:
        from core.auto_trainer.advance.planner import SafeAdvancePlanner

        modal_img = _synthetic_warning_modal_frame()
        planner = SafeAdvancePlanner()
        with TemporaryDirectory() as td:
            img_path = Path(td) / "modal.png"
            _write_png(img_path, modal_img)

            dec_fan = planner.plan(
                img_path,
                scenario="larc",
                visible_text="粉丝数不足 无法参加目标赛事\n取消\n前往赛事",
            )
            self.assertEqual(dec_fan["action"], "tap")
            self.assertEqual(dec_fan["reason"], "cancel_larc_fan_warning")
            self.assertEqual(dec_fan["tap"]["coordinate_name"], "safe_advance.larc_fan_warning_cancel")

            dec_maiden = planner.plan(
                img_path,
                scenario="larc",
                visible_text="无法参加目标赛事\n零胜段位\n取消\n前往赛事",
            )
            self.assertEqual(dec_maiden["action"], "tap")
            self.assertEqual(dec_maiden["reason"], "go_to_race_entry_requirement")
            self.assertEqual(dec_maiden["tap"]["coordinate_name"], "safe_advance.go_to_race")

    def test_fix5_screen_text_and_race_route_fading_home_and_unentered_g1_leave(self) -> None:
        from core.auto_trainer.advance.planner import SafeAdvancePlanner
        from core.auto_trainer.racing.executor import RaceRouteExecutor
        from core.auto_trainer.racing.models import RaceRoutePolicy, RaceRouteSession
        from core.auto_trainer.racing.planner import RaceRoutePlanner
        from core.auto_trainer.route_transition_session import RouteTransitionSession
        from core.auto_trainer.skip_session import RunSkipSession
        from core.screen_text import analyze_screen_text
        import scripts.larc_autonomous_run as lar

        # 5B-1: Compound exclusion keeps "参赛条件" off race_participant_list while preserving real participant screens.
        home_ocr = "养成\n经典级 1月上半月\n目标 全国德比 中进入前3\n参赛条件 剩余 826人\n体力\n休息\n训练"
        home_st = analyze_screen_text(home_ocr)
        self.assertFalse(any(h["screen"] == "race_participant_list" for h in home_st["screen_hints"]))

        real_part_st = analyze_screen_text("广播协会英里杯\n参赛!")
        self.assertEqual(real_part_st["primary_hint"]["screen"], "race_participant_list")

        # 5B-2a: Fading L'Arc main menu returns wait(loading), not race_list:no_primary_entry.
        planner = RaceRoutePlanner()
        with TemporaryDirectory() as td:
            fading_path = Path(td) / "fading_home.png"
            _write_png(fading_path, _synthetic_large_bottom_dialogue_frame())
            fading_home_ocr = "养成\n体力\n参赛条件 剩余 826人\n休息\n海外适应性"
            dec = planner.plan(
                fading_path,
                policy=RaceRoutePolicy(
                    scenario="larc",
                    visible_text=fading_home_ocr,
                    allow_enter_race=True,
                    allow_locked_race_entry=True,
                ),
                session=RaceRouteSession(),
            )
            self.assertEqual(dec["action"], "wait")
            self.assertEqual(dec["reason"], "loading")
            self.assertEqual(dec["route_state"], "loading")

            # 5B-2b: Unentered L'Arc race_list on non-target G1 leaves via race_list.back when allow_leave_race_list=True.
            blank_path = Path(td) / "race_list.png"
            _write_png(blank_path, np.full((1280, 720, 3), 240, dtype=np.uint8))
            list_policy = RaceRoutePolicy(
                scenario="larc",
                visible_text="赛事一览\n预测\n参赛\n参赛计划",
                allow_enter_race=True,
                allow_locked_race_entry=True,
                allow_leave_race_list=True,
            )
            with (
                patch.object(SafeAdvancePlanner, "_looks_like_race_list", return_value=True),
                patch.object(RaceRoutePlanner, "_selected_race_card_evidence", return_value={"grade": "G1"}),
                patch.object(RaceRoutePlanner, "_looks_like_non_goal_graded_selected_race", return_value=False),
                patch.object(RaceRoutePlanner, "_looks_like_target_race_list", return_value=False),
            ):
                leave_g1 = planner.plan(blank_path, policy=list_policy, session=RaceRouteSession())
            self.assertEqual(leave_g1["action"], "tap")
            self.assertEqual(leave_g1["reason"], "leave_unverified_g1_race_list")
            self.assertEqual(leave_g1["tap"]["coordinate_name"], "race_list.back")

            # Mandatory target G1 still enters instead of backing out.
            with (
                patch.object(SafeAdvancePlanner, "_looks_like_race_list", return_value=True),
                patch.object(RaceRoutePlanner, "_selected_race_card_evidence", return_value={"grade": "G1"}),
                patch.object(RaceRoutePlanner, "_looks_like_non_goal_graded_selected_race", return_value=False),
                patch.object(RaceRoutePlanner, "_looks_like_target_race_list", return_value=True),
                patch.object(RaceRoutePlanner, "_bottom_center_green_button", return_value=(360, 1100)),
            ):
                enter_g1 = planner.plan(blank_path, policy=list_policy, session=RaceRouteSession())
            self.assertEqual(enter_g1["action"], "tap")
            self.assertEqual(enter_g1["reason"], "enter_selected_target_race")

            # Executor clears pending intents on leave_unverified_g1_race_list.
            sess = RaceRouteSession(pending_locked_target_race={"x": 1}, pending_custom_scheduled_race={"y": 2})
            packet = SimpleNamespace(source_image_sha256="a" * 64, to_dict=lambda: {"source_image_sha256": "a" * 64})
            verdict = SimpleNamespace(allowed=True, to_dict=lambda: {"allowed": True})
            fake_adb = SimpleNamespace(
                connect=lambda: True,
                get_screen_size=lambda: (720, 1280),
                screenshot=lambda _p: None,
                tap=lambda *_a, **_k: True,
            )
            with (
                patch("core.auto_trainer.racing.executor.read_image_with_retry", return_value=np.zeros((1280, 720, 3), dtype=np.uint8)),
                patch("core.auto_trainer.racing.executor.FrameSafetyAnalyzer.analyze", return_value=packet),
                patch.object(RaceRoutePlanner, "plan", return_value={
                    "action": "tap",
                    "reason": "leave_unverified_g1_race_list",
                    "route_state": "race_list",
                    "tap": {"coordinate_name": "race_list.back", "x": 75, "y": 1232, "scaled": False},
                    "evidence": {"image": str(blank_path)},
                }),
                patch("core.auto_trainer.racing.executor.FormalActionSafetyGate.evaluate", return_value=verdict),
            ):
                ex_res = RaceRouteExecutor(adb_factory=lambda: fake_adb, sleep_fn=lambda _s: None).run_once(
                    mode="live",
                    execute=True,
                    confirm_token="UG_PARENT_RACE_ROUTE_SANDBOX",
                    policy=RaceRoutePolicy(scenario="larc", allow_leave_race_list=True),
                    session=sess,
                    screenshot_path=blank_path,
                    settle_seconds=0,
                )
            self.assertTrue(ex_res["performed"])
            self.assertEqual(ex_res["reason"], "leave_unverified_g1_race_list")
            self.assertIsNone(sess.pending_locked_target_race)
            self.assertIsNone(sess.pending_custom_scheduled_race)

        # 5B-3: _route_to_menu hands an accidental non-maiden race_list to _run_fixed_race with live_source(entered=False).
        with TemporaryDirectory() as td:
            root = Path(td) / "run_5b3"
            (root / "session").mkdir(parents=True, exist_ok=True)
            img_path = root / "rl.png"
            sha = _write_png(img_path, np.zeros((1280, 720, 3), dtype=np.uint8))
            route_path = root / "session" / "transition_session.json"
            skip_path = root / "session" / "skip_session.json"
            ledger_path = root / "session" / "career_goal_ledger.json"
            RouteTransitionSession.new(root.name).save(route_path)
            RunSkipSession.new(root.name).save(skip_path)

            ctx = SimpleNamespace(
                root=root,
                run_id=root.name,
                port=16384,
                route=route_path,
                skip=skip_path,
                ledger=ledger_path,
                progress={"device": "127.0.0.1:16384", "next_turn": 24},
                allow_fixed_races=True,
                allow_adaptations=False,
                transition_probe=None,
                screen_reader=SimpleNamespace(),
            )
            calls = {"adv": 0}
            fixed_calls: list[dict[str, object]] = []

            def fake_adv(_args):
                calls["adv"] += 1
                if calls["adv"] == 1:
                    return {
                        "is_valid": True,
                        "stop_reason": "race_list",
                        "source_screenshot_path": str(img_path),
                        "step_reports": [{
                            "screenshot_path": str(img_path),
                            "result": {
                                "performed": False,
                                "frame_safety": {"source_image_sha256": sha},
                                "decision": {
                                    "action": "stop",
                                    "reason": "race_list",
                                    "evidence": {
                                        "image": str(img_path),
                                        "screen_text": {"normalized_text": "赛事一览\n预测\n参赛\n参赛计划"},
                                    },
                                },
                            },
                        }],
                    }, 0
                return {
                    "is_valid": True,
                    "stop_reason": "ready_main_menu",
                    "source_screenshot_path": str(img_path),
                    "step_reports": [{"result": {"decision": {"evidence": {"reader": "ok"}}}}],
                }, 0

            def fake_fixed_race(_ctx, _race_root, _summary, _save_summary, **kwargs):
                fixed_calls.append(kwargs)
                return True, "ready_main_menu"

            from core.auto_trainer.advance.executor import SafeAdvanceExecutor

            summary = {"route_reports": []}
            with (
                patch.object(lar, "advance", side_effect=fake_adv),
                patch.object(lar, "advance_args", lambda argv: SimpleNamespace()),
                patch.object(lar, "observe_mei_unlock", lambda *a, **k: None),
                patch.object(lar, "_run_fixed_race", side_effect=fake_fixed_race),
                patch.object(SafeAdvanceExecutor, "_career_home_controls_ready", return_value=True),
            ):
                src, reason = lar._route_to_menu(ctx, root / "out", summary, lambda: None)

            self.assertEqual(reason, "ready_main_menu")
            self.assertEqual(src, str(img_path))
            self.assertEqual(len(fixed_calls), 1)
            ls = fixed_calls[0]["live_source"]
            self.assertEqual(ls["owner"], "race")
            self.assertEqual(ls["state"], "race_list")
            self.assertIs(ls["entered"], False)
            self.assertEqual(ls["source_sha256"], sha)


if __name__ == "__main__":
    unittest.main()
