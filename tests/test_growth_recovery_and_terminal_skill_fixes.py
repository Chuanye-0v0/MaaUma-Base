from contextlib import contextmanager
import importlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PACER_BACKEND = _REPO_ROOT / "pacer" / "backend"
_SKILLS_BACKEND = _PACER_BACKEND / "bridges" / "skills" / "backend"
for _p in (str(_PACER_BACKEND), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@contextmanager
def _skills_bridge_modules():
    """Temporarily bind `core` & `desktop` packages to bridges/skills/backend just like D1 worker."""
    saved_path = list(sys.path)
    saved_endpoint = os.environ.get("UMA_PUBLIC_ENDPOINT")
    os.environ["UMA_PUBLIC_ENDPOINT"] = "127.0.0.1:16448"
    saved_mods = {
        k: v
        for k, v in sys.modules.items()
        if k in {"core", "desktop"} or k.startswith(("core.", "desktop."))
    }
    for k in list(saved_mods):
        sys.modules.pop(k, None)
    sys.path[:] = [str(_SKILLS_BACKEND)] + [p for p in saved_path if p != str(_SKILLS_BACKEND)]
    try:
        yield importlib.import_module
    finally:
        for k in [
            k
            for k in list(sys.modules)
            if k in {"core", "desktop"} or k.startswith(("core.", "desktop."))
        ]:
            sys.modules.pop(k, None)
        sys.modules.update(saved_mods)
        sys.path[:] = saved_path
        if saved_endpoint is None:
            os.environ.pop("UMA_PUBLIC_ENDPOINT", None)
        else:
            os.environ["UMA_PUBLIC_ENDPOINT"] = saved_endpoint


class GrowthRecoveryAndTerminalSkillFixTests(unittest.TestCase):
    def test_exit_only_adaptation_ignores_unrelated_errors_when_pending_zero(self) -> None:
        from core.auto_trainer import larc_adaptation as la

        class _Reader:
            def __init__(self, grid):
                self._grid = grid

            def read_grid(self, _img):
                return dict(self._grid)

        img = np.zeros((1280, 720, 3), dtype=np.uint8)
        # Render white return button at (43..112, 1212..1265) for plan_larc_adaptation_return
        img[1212:1265, 43:112] = 255
        evidence = {"screen_text": {"normalized_text": "海外适应性 获得条件一览 消耗积分"}}
        sess = la.LarcAdaptationSession(
            run_session_id="run1", inspect_only=True, inspect_grid_only=True, stay_on_grid=False
        )
        grid = {
            "kind": "grid",
            "levels": [2, 2, 1, None, 1, 1, 1, 1, 1, 0],
            "selected_slot": 0,
            "pending_cost": 0,
            "points": 310,
            "errors": ["slot_4_unreadable", "points_unreadable"],
        }
        decision = la.plan_larc_adaptation(img, dict(evidence), session=sess, reader=_Reader(grid))
        self.assertEqual(decision["reason"], "larc_adaptation_inspection_complete")
        self.assertEqual(
            decision["evidence"]["larc_adaptation"]["inspection_exit_ignored_errors"],
            ["slot_4_unreadable", "points_unreadable"],
        )
        res = la.plan_larc_adaptation_return(img, decision)
        self.assertEqual(res["action"], "tap")
        self.assertEqual(res["reason"], "larc_adaptation_return")

        # Must remain fail-closed if pending_cost != 0
        busy_grid = {**grid, "pending_cost": 200, "errors": ["slot_4_unreadable"]}
        res_busy = la.plan_larc_adaptation(img, dict(evidence), session=sess, reader=_Reader(busy_grid))
        self.assertNotEqual(res_busy.get("reason"), "larc_adaptation_inspection_complete")
        self.assertNotEqual(la.plan_larc_adaptation_return(img, res_busy).get("reason"), "larc_adaptation_return")

    def test_optional_growth_recovery_degrades_safely_and_blocks_inflight_submission(self) -> None:
        from scripts import larc_autonomous_run as lar
        from scripts import larc_autonomy_growth as lag

        self.assertIn('"--max-steps", "6"', Path(lag.__file__).read_text(encoding="utf-8"))

        with TemporaryDirectory() as td:
            root = Path(td) / "run_g"
            (root / "session").mkdir(parents=True)
            ctx = SimpleNamespace(root=root, run_id="run_g", growth_checks=set())
            summary = {}
            saved_calls = []

            orig_return = lar._return_unowned_adaptation_grid
            lar._return_unowned_adaptation_grid = lambda _ctx, _out: {
                "ok": True,
                "reason": "ready_main_menu",
                "menu_source": str(root / "home.png"),
                "inspection_only": True,
            }
            try:
                degraded = lar._safe_recover_optional_growth(
                    ctx,
                    {"ok": False, "reason": "larc_adaptation_unreadable"},
                    root / "g1",
                    46,
                    ("k46",),
                    "speed_expedition",
                    False,
                    True,
                    summary,
                    lambda: saved_calls.append(True),
                )
                self.assertTrue(degraded["ok"])
                self.assertTrue(degraded["degraded_growth_skipped"])
                self.assertTrue(degraded["requires_recollection"])
                self.assertEqual(degraded["reason"], "growth_degraded_returned_home")
                self.assertIn(("k46",), ctx.growth_checks)
                self.assertEqual(len(summary["growth_adaptations"]), 1)

                # In-flight submission must stay strictly fail-closed
                (root / "session/larc_adaptation_speed_expedition.json").write_text(
                    json.dumps({"run_session_id": "run_g", "stage": "submission_pending"}),
                    encoding="utf-8",
                )
                summary_blocked = {}
                blocked = lar._safe_recover_optional_growth(
                    ctx,
                    {"ok": False, "reason": "larc_adaptation_submission_outcome_unknown"},
                    root / "g2",
                    46,
                    ("k46b",),
                    "speed_expedition",
                    False,
                    True,
                    summary_blocked,
                    lambda: None,
                )
                self.assertFalse(blocked["ok"])
                self.assertEqual(blocked["recovery_blocked"], "submission_outcome_unknown")
            finally:
                lar._return_unowned_adaptation_grid = orig_return

    def test_evolution_badge_stripping_and_semantic_review_match_punct_and_discount(self) -> None:
        with _skills_bridge_modules() as imp:
            sap = imp("core.skill_acquisition_parser")
            spl = imp("core.skill_purchase_live_adapter")

            self.assertEqual(sap._strip_evolution_badge_noise("达成进化条件热度飙升"), "热度飙升")
            self.assertEqual(sap._normalize_ocr_text("达成进化条件 热度飙升！"), "热度飙升")

            sha = "a" * 64
            review = {
                "item_id": "cn:热度飙升！",
                "observed_name": "热度飙升！",
                "observed_name_aliases": [{"text": "热度飙升", "confidence": 0.91}],
                "observed_description": "在赛事中期进行名次争夺时速度提升＜泥地＞",
                "required_description_token": "速度提升＜泥地＞",
                "observed_final_cost": 252,
                "rarity_or_icon_type": "gold",
                "discount_percent": 30,
                "name_binding_source": "direct_parser_name",
                "name_resolution_confidence": 0.96,
                "name_resolution_margin": 0.6,
                "name_resolution_evidence_dimensions": 4,
                "name_resolution_evidence": {"name": 0.96, "rarity": 0.99, "icon_color": 0.99, "cost": 0.99},
                "current_run_scope_only": True,
                "source_report_sha256": sha,
            }
            skill = {
                "name": "热度飙升",
                "description": "在赛事中期进行名次争夺时速度提升＜泥地＞",
                "cost": 252,
                "rarity": "gold",
                "discount_percent": None,  # Orange hint bubble flickered to None while price stayed 252
                "acquired": False,
                "plus_available": True,
                "field_confidence": {"cost": 0.98},
                "unknown_reason": ["description_low_confidence_requires_review"],
                "ocr_raw": {"name_candidates": [{"text": "热度飙升", "confidence": 0.93}]},
            }
            conf = spl._semantic_review_match_confidence(skill, review, allow_partial=False)
            self.assertIsNotNone(conf)
            self.assertGreaterEqual(conf, 0.9)

            bad_price = {**skill, "cost": 360}
            self.assertIsNone(spl._semantic_review_match_confidence(bad_price, review, allow_partial=False))

    def test_executor_retries_transient_row_mismatch_once(self) -> None:
        with _skills_bridge_modules() as imp:
            sbpe = imp("core.skill_batch_purchase_executor")
            SKILL_BATCH_PURCHASE_SAFETY_TOKEN = sbpe.SKILL_BATCH_PURCHASE_SAFETY_TOKEN
            SkillBatchPurchaseExecutor = sbpe.SkillBatchPurchaseExecutor

            page_0_missing = {
                "fresh": True,
                "frame_sha256": "1" * 64,
                "page_kind": "skill_acquisition",
                "screen_kind": "skill_acquisition",
                "currency_scope": "skill_points_only",
                "dangerous_resource_flags": [],
                "current_skill_points": 500,
                "rows": [],
            }
            page_0_ready = {
                "fresh": True,
                "frame_sha256": "2" * 64,
                "page_kind": "skill_acquisition",
                "screen_kind": "skill_acquisition",
                "currency_scope": "skill_points_only",
                "dangerous_resource_flags": [],
                "current_skill_points": 500,
                "rows": [
                    {
                        "item_id": "cn:热度飙升！",
                        "observed_final_cost": 252,
                        "observed_discount_percent": None,
                        "selected": False,
                        "plus_available": True,
                        "plus_control": {"x": 650, "y": 550},
                        "row_index": 0,
                    }
                ],
            }
            page_0_after = {
                "fresh": True,
                "frame_sha256": "3" * 64,
                "page_kind": "skill_acquisition",
                "screen_kind": "skill_acquisition",
                "currency_scope": "skill_points_only",
                "dangerous_resource_flags": [],
                "current_skill_points": 248,
                "selected_item_ids": ["cn:热度飙升！"],
                "selected_total": 252,
                "remaining_skill_points": 248,
                "remaining_points_semantics": "post_selection",
                "green_confirm_control": {"x": 360, "y": 1080},
                "rows": [{"item_id": "cn:热度飙升！", "selected": True}],
            }

            class _Adapter:
                def __init__(self):
                    self.captures = [page_0_missing, page_0_ready, page_0_after]

                def rewind_to_top(self, _max):
                    return True

                def capture_page(self, _active):
                    return self.captures.pop(0)

                def tap_plus(self, _ctrl, _aid):
                    pass

                def open_confirmation(self, _ctrl):
                    pass

                def persist_checkpoint(self, _cp):
                    pass

            orig_vi, orig_vp, orig_groups = (
                sbpe.validate_skill_inventory_packet,
                sbpe.validate_skill_batch_plan,
                sbpe.skill_selection_groups,
            )
            orig_sleep = sbpe.time.sleep
            sbpe.validate_skill_inventory_packet = lambda _inv: (True, "ok")
            sbpe.validate_skill_batch_plan = lambda _plan, _inv: (True, "ok")
            sbpe.skill_selection_groups = lambda p: p["page_groups"]
            sbpe.time.sleep = lambda _s: None
            try:
                inv = {"packet_sha256": "i" * 64, "captured_at_unix_ms": 0, "expires_at_unix_ms": 10**14}
                plan = {
                    "plan_sha256": "p" * 64,
                    "initial_skill_points": 500,
                    "selected_action_ids": ["cn:热度飙升！"],
                    "page_groups": [
                        {"page_index": 0, "items": [{"action_id": "cn:热度飙升！", "display_cost": 252, "observed_discount_percent": 30}]}
                    ],
                }
                ex = SkillBatchPurchaseExecutor(
                    inventory=inv,
                    plan=plan,
                    adapter=_Adapter(),
                    mode="live",
                    execute=True,
                    safety_token=SKILL_BATCH_PURCHASE_SAFETY_TOKEN,
                    expected_inventory_sha256="i" * 64,
                    expected_plan_sha256="p" * 64,
                    now_unix_ms=1000,
                )
                res = ex.run_selection()
                self.assertTrue(res.confirmation_opened)
                self.assertEqual(res.stop_reason, "confirmation_opened")
            finally:
                sbpe.validate_skill_inventory_packet = orig_vi
                sbpe.validate_skill_batch_plan = orig_vp
                sbpe.skill_selection_groups = orig_groups
                sbpe.time.sleep = orig_sleep

    def test_issue_final_confirmation_retries_up_to_three_frames(self) -> None:
        with _skills_bridge_modules() as imp:
            import cv2
            hs = imp("core.autonomous_training.harvest_skills")
            with TemporaryDirectory() as td:
                root = Path(td)
                sel = root / "selection"
                sel.mkdir()
                (sel / "promotion_plan.json").write_text(json.dumps({"purchases": [{"item_id": "s1"}]}), encoding="utf-8")
                (sel / "promotion_review.json").write_text(
                    json.dumps({"reviewed_rows": {"fp": {"item_id": "s1", "observed_name": "A", "observed_description": "D"}}}),
                    encoding="utf-8",
                )
                frame = root / "f.png"
                cv2.imwrite(str(frame), np.zeros((1280, 720, 3), dtype=np.uint8))

                class _Dev:
                    def __init__(self):
                        self.calls = 0

                    def capture(self):
                        self.calls += 1
                        return {
                            "page": "skill_confirmation" if self.calls == 3 else "skills",
                            "screenshot_path": str(frame),
                            "captured_at_unix_ms": 1000,
                        }

                orig_inspect = hs.inspect_verified_selection_confirmation
                orig_issue = hs.issue_final_confirmation_artifacts
                orig_sleep = hs.time.sleep
                dev = _Dev()
                hs.inspect_verified_selection_confirmation = lambda _im: {"accepted": dev.calls == 3}
                hs.issue_final_confirmation_artifacts = lambda **_kw: {"stop_reason": "final_authorization_issued", "child_plan_path": str(sel / "child.json")}
                hs.time.sleep = lambda _s: None
                try:
                    res = hs._issue_final_confirmation(dev, sel, {"selection_ledger_path": "l", "selection_ledger_frame_path": "lf"})
                    self.assertEqual(res["stop_reason"], "final_authorization_issued")
                    self.assertEqual(res["confirmation_capture_count"], 3)
                finally:
                    hs.inspect_verified_selection_confirmation = orig_inspect
                    hs.issue_final_confirmation_artifacts = orig_issue
                    hs.time.sleep = orig_sleep

    def test_unconfirmed_safe_return_and_autonomy_exclude_retry(self) -> None:
        from scripts import larc_autonomy_skills as las
        from scripts import larc_d1_skill_worker as ldw

        commands = []

        class _FakeDevice:
            def __init__(self):
                self.frames = [
                    {
                        "page": "skills",
                        "current_skill_points": 1800,
                        "screenshot_path": "shot_skills.png",
                        "skill_cart_disabled": False,
                        "actions": {
                            "reset_skill_selection": {"point": [620, 1085]},
                            "open_skill_confirmation": {"point": [360, 1080]},
                            "return_harvest_skills": {"point": [80, 1180]},
                        },
                    },
                    {
                        "page": "skills",
                        "current_skill_points": 2080,
                        "screenshot_path": "shot_reset.png",
                        "skill_cart_disabled": True,
                        "actions": {"return_harvest_skills": {"point": [80, 1180]}},
                    },
                    {"page": "finish_ready", "current_skill_points": 2080},
                ]

            def capture(self):
                return self.frames.pop(0) if self.frames else {"page": "finish_ready", "current_skill_points": 2080}

            def step(self, _aid):
                raise AssertionError("Must not call device.step('return_harvest_skills')")

            def command(self, *args):
                commands.append(args)
                return b""

        sink = {}
        orig_sleep = ldw.time.sleep
        ldw.time.sleep = lambda _s: None
        try:
            ldw._safe_return_after_unconfirmed_selection(_FakeDevice(), sink, "127.0.0.1:5555")
        finally:
            ldw.time.sleep = orig_sleep

        self.assertTrue(sink["returned_after_unconfirmed_selection"])
        self.assertEqual(sink["after_return_points"], 2080)
        self.assertTrue(sink["reset_before_unconfirmed_return"])
        self.assertEqual(len(commands), 2)

        with TemporaryDirectory() as td:
            root = Path(td) / "run_t"
            d1 = Path(td) / "d1"
            (d1 / "data/skills/cn-20260916").mkdir(parents=True)
            for fn in ("cn_skill_resolver_catalog.json", "cn_skill_planner_catalog.json"):
                (d1 / "data/skills/cn-20260916" / fn).write_text("{}", encoding="utf-8")
            db = Path(td) / "ext.json"
            db.write_text("{}", encoding="utf-8")
            (root / "session").mkdir(parents=True)
            (root / "progress.json").write_text(
                json.dumps({"run_session_id": root.name, "device": "127.0.0.1:16448", "next_turn": 61}),
                encoding="utf-8",
            )
            (root / "goal.json").write_text(json.dumps({"scenario": "larc"}), encoding="utf-8")

            invoked_argvs = []
            orig_run_proc = las.run_controlled_process
            orig_tess = las.resolve_tesseract_runtime
            orig_env = las.skill_ocr_environment

            def fake_run(argv, **_kw):
                invoked_argvs.append(list(argv))
                batch_dir = Path(argv[argv.index("--output-dir") + 1])
                batch_dir.mkdir(parents=True, exist_ok=True)
                turn_id = argv[argv.index("--turn-id") + 1]
                if len(invoked_argvs) == 1:
                    payload = {
                        "ok": False,
                        "stage": "learning_pending",
                        "purchase_executed": False,
                        "confirmation_opened": False,
                        "returned_after_unconfirmed_selection": True,
                        "after_return_points": 2080,
                        "reason": "ValueError:race_skill_confirmation_not_opened:planned_row_identity_mismatch:cn:热度飙升！",
                        "selection_result": {"stop_reason": "planned_row_identity_mismatch:cn:热度飙升！"},
                    }
                    rc = 2
                else:
                    ret_proof = batch_dir / "proof.json"
                    ret_proof.write_text("{}", encoding="utf-8")
                    payload = {
                        "ok": True,
                        "stage": "complete",
                        "reason": "race_skill_learning_verified",
                        "run_session_id": root.name,
                        "endpoint": "127.0.0.1:16448",
                        "turn_id": turn_id,
                        "output_dir": str(batch_dir),
                        "return_proof_path": str(ret_proof),
                        "before": {"current_skill_points": 2080},
                        "after": {"current_skill_points": 12},
                        "finish_identity": {"name": "小林历奇", "run_styles": {"领跑": "A", "跟前": "B", "居中": "C", "后追": "G"}},
                    }
                    rc = 0
                (batch_dir / "worker_report.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                return SimpleNamespace(returncode=rc, stdout="", stderr="")

            las.run_controlled_process = fake_run
            las.resolve_tesseract_runtime = lambda: SimpleNamespace(executable="tesseract.exe")
            las.skill_ocr_environment = lambda env, **_kw: dict(env)
            try:
                report, code = las.run_larc_terminal_skills(
                    run_root=root,
                    output_dir=root / "term_out",
                    d1_root=d1,
                    external_skill_db_path=db,
                    adb_port=16448,
                )
                self.assertEqual(code, 0)
                self.assertTrue(report["ready_to_finish"])
                self.assertEqual(len(invoked_argvs), 2)
                self.assertIn("--exclude-skill-id", invoked_argvs[1])
                idx = invoked_argvs[1].index("--exclude-skill-id")
                self.assertEqual(invoked_argvs[1][idx + 1], "cn:热度飙升！")
            finally:
                las.run_controlled_process = orig_run_proc
                las.resolve_tesseract_runtime = orig_tess
                las.skill_ocr_environment = orig_env


if __name__ == "__main__":
    unittest.main()
