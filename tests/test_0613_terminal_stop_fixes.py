"""Regression tests for MaaUma 0.6.13 terminal settlement and 59T/60T stop fixes."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cv2
import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
PACER_BACKEND = Path(os.environ.get("MAAUMA_PACER_BACKEND_DIR") or (REPO_ROOT / "pacer" / "backend"))
ROOT_BACKEND = REPO_ROOT / "backend"
BRIDGE_TRAIN_DIR = PACER_BACKEND / "bridges" / "skills" / "backend" / "core" / "autonomous_training"
LIVE_CAPTURE_DIR = (
    REPO_ROOT.parents[1]
    / "apps"
    / "maauma-two-emulator-0.5.0"
    / "pacer"
    / "backend"
    / "runs"
)


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
    for attr, val in (
        ("ADB_HOST", "127.0.0.1"),
        ("ADB_ENDPOINT", "127.0.0.1:16384"),
        ("ADB_PORT", 16384),
        ("ADB_PATH", ""),
        ("SWIPE_DELAY", 0.2),
        ("TAP_DELAY", 0.2),
        ("ASSETS_DIR", PACER_BACKEND / "assets"),
    ):
        if not hasattr(cfg_set, attr):
            setattr(cfg_set, attr, val)


def _load_module_by_path(name: str, path: Path, stubs: dict[str, object] | None = None):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    if not stubs:
        spec.loader.exec_module(mod)
        return mod
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(mod)
    return mod


def _load_harvest_observer():
    core_pkg = types.ModuleType("core")
    pi_mod = types.ModuleType("core.page_identity")

    class _StubPageIdentity:
        def __init__(self, image):
            self.image = image

        def find(self, *_args, **_kwargs):
            return False

    pi_mod.PageIdentity = _StubPageIdentity
    core_pkg.page_identity = pi_mod
    return _load_module_by_path(
        "cand_bridge_harvest_observer",
        BRIDGE_TRAIN_DIR / "harvest_observer.py",
        stubs={"core": core_pkg, "core.page_identity": pi_mod},
    )


def _evo_tokens(*, with_confirm: bool = True) -> list[dict]:
    tokens = [
        {"text": "技能进化选择", "confidence": 0.99, "bbox": [250, 30, 470, 90]},
        {"text": "请选择进化技能", "confidence": 0.99, "bbox": [180, 1040, 540, 1100]},
    ]
    if with_confirm:
        tokens.append({"text": "确定", "confidence": 0.99, "bbox": [460, 1150, 580, 1205]})
    return tokens


def _make_synthetic_evolution_image(
    *,
    card1_selected: bool = False,
    card2_selected: bool = False,
    card2_bottom_clipped: bool = True,
    confirm_bright: bool = False,
) -> bytes:
    img = np.full((1280, 720, 3), 245, dtype=np.uint8)
    # PinkBGR: HSV H in 140..179, S >= 40, V >= 150 -> e.g. BGR (200, 140, 240)
    pink_bgr = (200, 140, 240)
    green_bgr = (60, 210, 60)

    # Card 0: y=385..551 (height=166)
    img[385:551, 105:688] = pink_bgr
    if card1_selected:
        img[452:484, 48:80] = green_bgr

    # Card 1: bottom-clipped (y=936..1040, h=104) or full height (y=680..846, h=166)
    if card2_bottom_clipped:
        y0, y1, ry = 936, 1040, 1016
    else:
        y0, y1, ry = 680, 846, 763
    img[y0:y1, 105:688] = pink_bgr
    if card2_selected:
        img[ry - 16 : ry + 16, 48:80] = green_bgr

    if confirm_bright:
        img[1142:1214, 376:658] = green_bgr
    else:
        img[1142:1214, 376:658] = (170, 170, 170)

    ok, enc = cv2.imencode(".png", img)
    assert ok
    return enc.tobytes()


class Test0613TerminalStopFixes(unittest.TestCase):
    def setUp(self) -> None:
        _ensure_pacer_backend_path()

    def test_fix1_skill_evolution_selects_unselected_second_card_and_scroll(self) -> None:
        ho = _load_harvest_observer()
        sw = _load_module_by_path(
            "cand_larc_d1_settlement_worker",
            PACER_BACKEND / "scripts" / "larc_d1_settlement_worker.py",
        )
        tokens = _evo_tokens()

        # 1. Card 0 selected (green), Card 1 unselected (bottom-clipped y=936..1040) -> picks Card 1 radio [64, 1019]
        raw_one = _make_synthetic_evolution_image(
            card1_selected=True, card2_selected=False, card2_bottom_clipped=True, confirm_bright=False
        )
        obs_one = ho.observe_harvest(raw_one, tokens)
        self.assertIsNotNone(obs_one)
        self.assertEqual(obs_one["page"], "skill_evolution_select")
        self.assertNotIn("confirm_skill_evolution_selection", obs_one["actions"])
        self.assertEqual(obs_one["actions"]["select_skill_evolution"], {"point": [64, 1019]})
        self.assertEqual(sw._settlement_action(obs_one, {}), "select_skill_evolution")

        # 2. Both visible cards selected but confirm still grey -> emits scroll_skill_evolution
        raw_both = _make_synthetic_evolution_image(
            card1_selected=True, card2_selected=True, card2_bottom_clipped=True, confirm_bright=False
        )
        obs_both = ho.observe_harvest(raw_both, tokens)
        self.assertIsNotNone(obs_both)
        self.assertNotIn("select_skill_evolution", obs_both["actions"])
        self.assertEqual(
            obs_both["actions"]["scroll_skill_evolution"],
            {"gesture": "swipe", "point": [360, 880], "end": [360, 420]},
        )
        self.assertIn("scroll_skill_evolution", sw.SETTLEMENT_ACTIONS)
        self.assertEqual(sw._settlement_action(obs_both, {}), "scroll_skill_evolution")

        # 3. Confirm button bright green -> confirms selection
        raw_ready = _make_synthetic_evolution_image(
            card1_selected=True, card2_selected=True, card2_bottom_clipped=True, confirm_bright=True
        )
        obs_ready = ho.observe_harvest(raw_ready, tokens)
        self.assertIsNotNone(obs_ready)
        self.assertIn("confirm_skill_evolution_selection", obs_ready["actions"])
        self.assertEqual(sw._settlement_action(obs_ready, {}), "confirm_skill_evolution_selection")

        # 4. Real 07:46 Ricky captures if present in local workspace
        before_pngs = list(LIVE_CAPTURE_DIR.glob("cut_pacer_20261126_0746*/**/z/capture/1795653020629490800.png"))
        after_pngs = list(LIVE_CAPTURE_DIR.glob("cut_pacer_20261126_0746*/**/z/capture/1795653022457501900.png"))
        if before_pngs and after_pngs:
            obs_real_before = ho.observe_harvest(before_pngs[0].read_bytes(), tokens)
            obs_real_after = ho.observe_harvest(after_pngs[0].read_bytes(), tokens)
            self.assertEqual(obs_real_before["actions"]["select_skill_evolution"]["point"][0], 64)
            self.assertEqual(obs_real_before["actions"]["select_skill_evolution"]["point"][1], 468)
            pt_after = obs_real_after["actions"]["select_skill_evolution"]["point"]
            self.assertEqual(pt_after[0], 64)
            self.assertTrue(994 <= pt_after[1] <= 1039, pt_after)

        # 5. Harvest / takeover whitelists include scroll_skill_evolution
        harvest_txt = (BRIDGE_TRAIN_DIR / "harvest.py").read_text(encoding="utf-8")
        takeover_txt = (BRIDGE_TRAIN_DIR / "takeover.py").read_text(encoding="utf-8")
        self.assertIn("'scroll_skill_evolution': 'skill_evolution_select'", harvest_txt)
        self.assertIn("'scroll_skill_evolution': {'skill_evolution_select'}", harvest_txt)
        self.assertIn("'scroll_skill_evolution'", takeover_txt)

    def test_fix2_terminal_retry_and_prior_identity_fallback(self) -> None:
        skw = _load_module_by_path(
            "cand_larc_d1_skill_worker",
            PACER_BACKEND / "scripts" / "larc_d1_skill_worker.py",
        )
        desktop_pkg = types.ModuleType("desktop")
        ctx_mod = types.ModuleType("desktop.umaa_public_context")
        ctx_mod.endpoint = lambda: skw.ENDPOINT
        core_pkg = types.ModuleType("core")
        exec_mod = types.ModuleType("core.skill_purchase_executor")
        exec_mod.SKILL_PURCHASE_SAFETY_TOKEN = "test-token"
        at_pkg = types.ModuleType("core.autonomous_training")
        sc_mod = types.ModuleType("core.autonomous_training.success_close")
        sc_mod._child_purchase_ids = lambda _child: []
        finish_mod = _load_module_by_path(
            "cand_root_finish",
            ROOT_BACKEND / "core" / "autonomous_training" / "finish.py",
            stubs={
                "desktop": desktop_pkg,
                "desktop.umaa_public_context": ctx_mod,
                "core": core_pkg,
                "core.skill_purchase_executor": exec_mod,
                "core.autonomous_training": at_pkg,
                "core.autonomous_training.success_close": sc_mod,
            },
        )
        api = SimpleNamespace(canonical_finish_evidence=finish_mod.canonical_finish_evidence)

        observer_txt = (BRIDGE_TRAIN_DIR / "observer.py").read_text(encoding="utf-8")
        self.assertIn("('居中', (460, 755, 580, 795))", observer_txt)
        autonomy_skills_txt = (PACER_BACKEND / "scripts" / "larc_autonomy_skills.py").read_text(encoding="utf-8")
        self.assertIn("--prior-identity-json", autonomy_skills_txt)

        prior_identity = {
            "name": "小林历奇",
            "run_styles": {"领跑": "A", "跟前": "A", "居中": "C", "后追": "G"},
        }

        with tempfile.TemporaryDirectory() as td:
            png_path = Path(td) / "frame.png"
            raw = b"fake-png-frame-bytes"
            png_path.write_bytes(raw)
            fsha = hashlib.sha256(raw).hexdigest()

            def make_obs(*, page="finish_ready", sp=9, styles=None, name="小林历奇", enabled=True, with_finish=True):
                actions = {"open_skills": {"point": [208, 1060]}}
                if with_finish:
                    actions["finish_training"] = {
                        "point": [508, 1079],
                        "text": "养成结束！",
                        "bbox": [439, 1062, 577, 1096],
                        "enabled": enabled,
                    }
                return {
                    "endpoint": skw.ENDPOINT,
                    "page": page,
                    "current_skill_points": sp,
                    "screenshot_path": str(png_path),
                    "frame_sha256": fsha,
                    "captured_at_unix_ms": int(time.time() * 1000),
                    "actions": actions,
                    "finish_summary": {"name": name, "run_styles": styles},
                }

            # 1. Retry recovers when capture 1 misses '居中' and capture 2 reads '居中': 'C'
            seq1 = iter([
                make_obs(styles={"领跑": "A", "跟前": "A", "居中": None, "后追": "G"}),
                make_obs(styles={"领跑": "A", "跟前": "A", "居中": "C", "后追": "G"}),
            ])
            before1, ident1 = skw._terminal(
                SimpleNamespace(capture=lambda: next(seq1)),
                api,
                max_attempts=3,
                retry_delay=0.0,
            )
            self.assertEqual(ident1, prior_identity)
            self.assertEqual(before1["terminal_capture_attempts"], 2)
            self.assertNotIn("identity_fallback", before1)

            # 2. Retry recovers from transient unknown/None frame
            seq2 = iter([
                make_obs(page="unknown", sp=None, styles=None),
                make_obs(styles={"领跑": "A", "跟前": "A", "居中": "C", "后追": "G"}),
            ])
            before2, ident2 = skw._terminal(
                SimpleNamespace(capture=lambda: next(seq2)),
                api,
                max_attempts=3,
                retry_delay=0.0,
            )
            self.assertEqual(ident2, prior_identity)
            self.assertEqual(before2["terminal_capture_attempts"], 2)

            # 3. Persistent single-style OCR miss ('居中': None) falls back cleanly to agreeing prior_identity
            obs_partial = lambda: make_obs(styles={"领跑": "A", "跟前": "A", "居中": None, "后追": "G"})
            before3, ident3 = skw._terminal(
                SimpleNamespace(capture=obs_partial),
                api,
                prior_identity=prior_identity,
                max_attempts=3,
                retry_delay=0.0,
            )
            self.assertEqual(ident3, prior_identity)
            self.assertEqual(before3["identity_fallback"], "prior_batch_verified_partial_match")

            # 4. Negative fallback guards must still reject and raise terminal_identity_unverified
            bad_cases = [
                make_obs(name="神鹰", styles={"领跑": "A", "跟前": "A", "居中": None, "后追": "G"}),
                make_obs(styles={"领跑": "B", "跟前": "A", "居中": None, "后追": "G"}),
                make_obs(styles={"领跑": "A", "跟前": "A", "居中": None, "后追": None}),
                make_obs(styles={"领跑": "A", "跟前": "A", "居中": None, "后追": "G"}, enabled=False),
                make_obs(styles={"领跑": "A", "跟前": "A", "居中": None, "后追": "G"}, with_finish=False),
            ]
            for bad_obs in bad_cases:
                with self.assertRaises(ValueError) as ctx:
                    skw._terminal(
                        SimpleNamespace(capture=lambda o=bad_obs: o),
                        api,
                        prior_identity=prior_identity,
                        max_attempts=2,
                        retry_delay=0.0,
                    )
                self.assertEqual(str(ctx.exception), "terminal_identity_unverified")

    def test_fix3_capped_stat_accepted_zero_gold_row_is_reliable(self) -> None:
        from core.auto_trainer.live_loop import UGParentTrainingLoop

        def make_state(*, wis_stat: int = 1300, rows=None, col_status="abstained", col_reason="gain_rows_not_found", primary_total=0, sp_reliable=True, sp_total=52):
            if rows is None:
                rows = [
                    {"row_index": 0, "occupancy": "absent", "status": "absent", "value": None},
                    {"row_index": 1, "occupancy": "present", "status": "accepted", "value": 0, "raw_text": "+0", "style": "gold"},
                ]
            raw_data = {
                "gain_breakdown": {
                    "wisdom": {
                        "base_gain": 0,
                        "fragment_or_scenario_gain": 0,
                        "total_gain": primary_total,
                        "reliable": False,
                    },
                    "skill_pt": {
                        "total_gain": sp_total,
                        "reliable": sp_reliable,
                    },
                },
                "stats_gain_ocr": {
                    "columns": [
                        {
                            "attribute_id": "wisdom",
                            "status": col_status,
                            "reason": col_reason,
                            "rows": rows,
                        }
                    ]
                },
            }
            opt = SimpleNamespace(training_type="wisdom", raw_data=raw_data)
            return SimpleNamespace(
                current_stats={"speed": 1337, "stamina": 914, "power": 1054, "guts": 949, "wisdom": wis_stat},
                training_options=[opt],
            )

        # Positive case: exact 06:32 turn_060 structure
        ev_ok = UGParentTrainingLoop._selected_primary_gain_evidence(make_state(), "wisdom")
        self.assertTrue(ev_ok["present"])
        self.assertTrue(ev_ok["reliable"])
        self.assertEqual(ev_ok["total_gain"], 0)
        self.assertTrue(ev_ok["capped_zero_gain"])
        self.assertTrue(UGParentTrainingLoop._selected_primary_gain_is_reliable(make_state(), "wisdom"))

        # Negative cases that must stay unreliable
        self.assertFalse(UGParentTrainingLoop._selected_primary_gain_is_reliable(make_state(wis_stat=1199), "wisdom"))
        self.assertFalse(UGParentTrainingLoop._selected_primary_gain_is_reliable(make_state(sp_reliable=False), "wisdom"))
        self.assertFalse(UGParentTrainingLoop._selected_primary_gain_is_reliable(make_state(sp_total=0), "wisdom"))
        self.assertFalse(
            UGParentTrainingLoop._selected_primary_gain_is_reliable(
                make_state(rows=[
                    {"row_index": 0, "occupancy": "absent", "status": "absent"},
                    {"row_index": 1, "occupancy": "present", "status": "abstained", "value": None},
                ]),
                "wisdom",
            )
        )
        self.assertFalse(
            UGParentTrainingLoop._selected_primary_gain_is_reliable(
                make_state(rows=[
                    {"row_index": 0, "occupancy": "absent", "status": "absent"},
                    {"row_index": 1, "occupancy": "present", "status": "accepted", "value": 5},
                ]),
                "wisdom",
            )
        )


if __name__ == "__main__":
    unittest.main()
