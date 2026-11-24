"""Regression tests for MaaUma Base 0.6.3 bugfixes (L'Arc menu, event knowledge, 30173 aliases, skip-stall, debounce)."""
from __future__ import annotations

import json
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


def _synthetic_larc_home_frame() -> np.ndarray:
    """Create a 1280x720 BGR image matching L'Arc main menu HSV color anchors."""
    img = np.full((1280, 720, 3), 24, dtype=np.uint8)
    # remaining_panel (15, 42, 149, 126): blue (H ~ 105 in OpenCV BGR)
    img[42:126, 15:149] = (230, 120, 30)
    # objective_panel (158, 75, 711, 136): white
    img[75:136, 158:711] = (245, 245, 245)
    # stats_panel (25, 825, 591, 906): white in L'Arc
    img[825:906, 25:591] = (245, 245, 245)
    # bottom_actions (28, 939, 686, 1155) + specific controls:
    # rest_action (25, 930, 210, 1045): green
    img[930:1045, 25:210] = (60, 210, 60)
    # training_action (220, 930, 500, 1045): blue
    img[930:1045, 220:500] = (230, 120, 30)
    # skill_action (515, 930, 695, 1045): blue
    img[930:1045, 515:695] = (230, 120, 30)
    return img


def _synthetic_large_bottom_dialogue_frame() -> np.ndarray:
    """Create a 1280x720 BGR image matching SafeAdvancePlanner._looks_like_large_bottom_dialogue."""
    img = np.full((1280, 720, 3), 30, dtype=np.uint8)
    # Dialogue region: y in [0.70*1280=896, 0.91*1280=1164], mostly white + warm brown border accent
    img[896:1164, 15:705] = (245, 245, 245)
    img[905:935, 30:120] = (60, 110, 160)
    return img


class Test063Fixes(unittest.TestCase):
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
        for name in self._injected_cfg:
            sys.modules.pop(name, None)

    def test_fix1_larc_main_menu_detection_and_rejection(self) -> None:
        from core.main_menu_state import _detect_main_menu

        home = _synthetic_larc_home_frame()
        res_larc = _detect_main_menu(home, scenario="larc")
        self.assertEqual(res_larc["value"], "main_menu")

        # Same L'Arc white-stats frame should NOT match grand_masters red/blue criteria
        res_gm = _detect_main_menu(home, scenario="grand_masters")
        self.assertIsNone(res_gm["value"])

        # Obscured bottom buttons (dialogue overlay / non-home) must abstain in L'Arc
        non_home = _synthetic_large_bottom_dialogue_frame()
        res_dlg = _detect_main_menu(non_home, scenario="larc")
        self.assertIsNone(res_dlg["value"])

    def test_fix2_event_knowledge_packaged_paths_without_cache(self) -> None:
        from core import event_knowledge_lookup as ekl
        from core.auto_trainer import cut_farming

        with TemporaryDirectory() as td:
            tmp = Path(td)
            pkg_personal = tmp / "data/event_knowledge/umaassistant_personal_v1.json"
            pkg_titles = tmp / "data/event_knowledge/bwiki_cn_titles_v1.json"
            legacy_personal = tmp / ".cache/event_knowledge/umaassistant_personal_v1.json"
            legacy_titles = tmp / ".cache/event_knowledge/bwiki_cn_titles_v1.json"
            overlay_path = tmp / "data/event_knowledge/character_cn_title_overlay_v1.json"

            pkg_personal.parent.mkdir(parents=True, exist_ok=True)
            pkg_personal.write_text(
                json.dumps(
                    {
                        "format": "uma.event_knowledge.personal_candidates.v1",
                        "events": [
                            {
                                "source_id": "src:30173:1",
                                "owner_type": "support",
                                "owner_name_ja": "test_owner",
                                "owner_name_zh_tw": "test_owner_tw",
                                "titles": {"ja": "test_ja", "zh_tw": "test_tw"},
                                "options": [
                                    {"index": 1, "texts": {"ja": "o1"}, "effect_text": "eff1"},
                                    {"index": 2, "texts": {"ja": "o2"}, "effect_text": "eff2"},
                                ],
                                "source_url": "https://example.invalid/ev",
                                "cn_release_status": "cn_released",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            pkg_titles.write_text(
                json.dumps(
                    {
                        "format": "uma.event_knowledge.cn_titles.v1",
                        "title_id_count": 1,
                        "source_url": "https://example.invalid/cat",
                        "by_title": {"你是节奏恰到好处的行板": ["830173001"]},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            overlay_path.write_text(
                json.dumps(
                    {
                        "format": "uma.event_knowledge.character_cn_title_overlay.v1",
                        "extra_characters": {},
                        "events": {
                            "src:30173:1": {
                                "zh_cn": "叠层标题测试",
                                "candidate_story_ids": ["830173001"],
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            ekl._personal_cn_title_index.cache_clear()
            ekl._cn_title_id_index.cache_clear()
            ekl._character_cn_overlay_cached.cache_clear()
            self.assertFalse(legacy_personal.exists())
            with (
                patch.object(ekl, "PACKAGED_PERSONAL_PATH", pkg_personal),
                patch.object(ekl, "LEGACY_PERSONAL_PATH", legacy_personal),
                patch.object(ekl, "PERSONAL_PATH", pkg_personal),
                patch.object(ekl, "PACKAGED_CN_TITLE_CATALOG_PATH", pkg_titles),
                patch.object(ekl, "LEGACY_CN_TITLE_CATALOG_PATH", legacy_titles),
                patch.object(ekl, "CN_TITLE_CATALOG_PATH", pkg_titles),
                patch.object(ekl, "CHARACTER_CN_OVERLAY_PATH", overlay_path),
            ):
                ref = ekl.personal_candidate_reference(
                    "协助卡事件\n叠层标题测试\n选项1\n选项2",
                    2,
                    personal_path=pkg_personal,
                    title_catalog_path=pkg_titles,
                )
                self.assertEqual(ref["status"], "source_candidate")
                self.assertEqual(ref["candidates"][0]["source_id"], "src:30173:1")

                t_only = ekl.personal_candidate_reference(
                    "协助卡事件\n你是节奏恰到好处的行板\n选项1\n选项2",
                    2,
                    personal_path=pkg_personal,
                    title_catalog_path=pkg_titles,
                )
                self.assertEqual(t_only["status"], "title_only")
                self.assertEqual(t_only["candidate_story_ids"], ["830173001"])

        self.assertEqual(cut_farming.EVENT_CACHE, str(ekl.PERSONAL_PATH))

    def test_fix4_support_aliases_for_30173(self) -> None:
        from core.auto_trainer import larc_event_library as lel
        from core.event_titles import title_key

        aliases = lel._CN_SUPPORT_TITLE_ALIASES
        self.assertEqual(aliases.get(title_key("你是速度精确的行板")), title_key("你是节奏恰到好处的行板"))
        self.assertEqual(aliases.get(title_key("从高十五度恢复原位但是")), title_key("从十五度音程转回原位......但是"))
        self.assertEqual(aliases.get(title_key("从高十五度恢复原位但是一")), title_key("从十五度音程转回原位......但是"))

        fake_events = {
            "ev_1": {
                "event_id": "ev_1",
                "story_id": "830173001",
                "support_card_id": "30173",
                "owner_character_id": "1099",
                "next_event_ids": [],
                "titles": {"zh_cn": "你是节奏恰到好处的行板", "zh_tw": "你是節奏恰到好處的行板"},
                "options": [
                    {"index": 1, "branches": [{"skills": [], "probability": 1.0, "effects": []}]},
                    {"index": 2, "branches": [{"skills": [], "probability": 1.0, "effects": []}]},
                ],
            },
            "ev_2": {
                "event_id": "ev_2",
                "story_id": "830173002",
                "support_card_id": "30173",
                "owner_character_id": "1099",
                "next_event_ids": [],
                "titles": {"zh_cn": "从十五度音程转回原位......但是", "zh_tw": "從十五度音程轉回原位……但是"},
                "options": [
                    {"index": 1, "branches": [{"skills": [], "probability": 1.0, "effects": []}]},
                    {"index": 2, "branches": [{"skills": [], "probability": 1.0, "effects": []}]},
                ],
            },
        }
        fake_lib = SimpleNamespace(
            events=fake_events,
            cards={"30173": {"card_id": "30173", "character_id": "1099"}},
        )
        ctx = {"support_profile": {"cards": [{"slot": 0, "card_id": "30173"}]}, "trainee_id": "1046"}
        with patch.object(lel, "default_library", return_value=fake_lib):
            lel._support_index.cache_clear()
            for noisy, expected_story in (
                ("你是速度精确的行板", "830173001"),
                ("从高十五度恢复原位但是", "830173002"),
                ("从高十五度恢复原位但是一", "830173002"),
            ):
                resolved = lel.resolve_support(f"协助卡事件\n{noisy}\n选项效果", 2, ctx)
                self.assertIsNotNone(resolved, noisy)
                assert resolved is not None
                self.assertEqual(resolved["event"]["story_id"], expected_story)
            lel._support_index.cache_clear()

    def test_fix5_route_to_menu_skip_stall_nudges_and_guards(self) -> None:
        from scripts import larc_autonomous_run as lar
        from core.auto_trainer.route_transition_session import RouteTransitionSession
        from core.auto_trainer.skip_session import RunSkipSession, SkipSessionState
        from core.auto_trainer.advance.executor import SafeAdvanceExecutor
        from core.auto_trainer.safe_advance import SafeAdvancePlanner

        dlg = _synthetic_large_bottom_dialogue_frame()
        with TemporaryDirectory() as td:
            root = Path(td)
            session_dir = root / "session"
            session_dir.mkdir(parents=True, exist_ok=True)
            img_path = root / "stall.png"
            cv2.imwrite(str(img_path), dlg)

            run_id = "run-fix5"
            route_path = session_dir / "route.json"
            skip_path = session_dir / "skip.json"
            ledger_path = session_dir / "ledger.json"
            ledger_path.write_text("{}", encoding="utf-8")
            RouteTransitionSession.new(run_id).save(route_path)
            skip = RunSkipSession.new(run_id)
            skip.state = SkipSessionState.ENABLED
            skip.last_observed_level = "double"
            skip.last_source_sha256 = "a" * 64
            skip.confirmed_source_sha256 = "a" * 64
            skip.save(skip_path)

            taps: list[tuple[int, int, bool]] = []
            clock = {"now": 100.0}

            def fake_monotonic() -> float:
                return clock["now"]

            fake_probe = SimpleNamespace(
                adb=SimpleNamespace(tap=lambda x, y, scaled=False: taps.append((x, y, scaled))),
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
                progress={"device": "127.0.0.1:16384", "next_turn": 5},
                allow_fixed_races=False,
                allow_adaptations=False,
                transition_probe=fake_probe,
                screen_reader=SimpleNamespace(),
            )

            # Sequence of iterations (progressed=True on nudge resets idle timer):
            # 0: t=100.0 (init wait_budget, idle=0 < 6s -> no nudge)
            # 1: t=107.0 (idle=7s >= 6s -> nudge #1, resets idle at 107.0)
            # 2: t=110.0 (3s since nudge #1 < 4s cooldown & < 6s idle -> no nudge)
            # 3: t=114.0 (7s since nudge #1 >= 6s -> nudge #2, resets idle at 114.0)
            # 4: t=121.0 (dangerous frame -> blocked)
            # 5: t=122.0 (confirm modal -> blocked)
            # 6: t=123.0 (event options -> blocked)
            # 7: t=124.0 (safe stalled, 10s >= 6s -> nudge #3)
            # 8: t=131.0 (skip_stall_nudges == 3 max reached -> blocked)
            # 9: t=132.0 -> ready_main_menu exit
            iter_idx = {"i": 0}
            adv_times = [100.0, 107.0, 110.0, 114.0, 121.0, 122.0, 123.0, 124.0, 131.0, 132.0]

            def fake_advance(route_args):
                i = iter_idx["i"]
                iter_idx["i"] += 1
                clock["now"] = adv_times[i]
                if i == 9:
                    return {
                        "is_valid": True,
                        "stop_reason": "ready_main_menu",
                        "source_screenshot_path": str(img_path),
                        "step_reports": [{"result": {"decision": {"evidence": {"reader": "ok"}}}}],
                    }, 0
                dangerous = (i == 4)
                return {
                    "is_valid": True,
                    "stop_reason": "no_progress_timeout",
                    "step_reports": [
                        {
                            "result": {
                                "performed": False,
                                "tap_performed": False,
                                "frame_safety": {"dangerous": dangerous},
                                "decision": {
                                    "action": "wait",
                                    "reason": "bottom_skip_double_enabled",
                                    "route_state": "event_dialogue",
                                    "evidence": {
                                        "image": str(img_path),
                                        "screen_text": {"dangerous": False, "normalized_text": ""},
                                    },
                                },
                            }
                        }
                    ],
                }, 1

            summary = {"route_reports": []}
            with (
                patch.object(lar, "monotonic", side_effect=fake_monotonic),
                patch.object(lar, "sleep", lambda _s: None),
                patch.object(lar, "advance", side_effect=fake_advance),
                patch.object(lar, "observe_mei_unlock", lambda *a, **k: None),
                patch.object(lar, "advance_args", lambda argv: SimpleNamespace()),
                patch.object(SafeAdvanceExecutor, "_career_home_controls_ready", return_value=True),
                patch.object(
                    SafeAdvancePlanner,
                    "_looks_like_confirm_modal_panel",
                    side_effect=lambda _f: iter_idx["i"] - 1 == 5,
                ),
                patch(
                    "core.auto_trainer.event_options.event_option_rows_ready",
                    side_effect=lambda _f: iter_idx["i"] - 1 == 6,
                ),
                patch.object(SafeAdvancePlanner, "_looks_like_training_home_controls", return_value=False),
                patch.object(SafeAdvancePlanner, "_looks_like_large_bottom_dialogue", return_value=True),
            ):
                src, reason = lar._route_to_menu(ctx, root / "out", summary, lambda: None)

            self.assertEqual(reason, "ready_main_menu")
            self.assertEqual(src, str(img_path))
            self.assertEqual(len(taps), 3)
            self.assertEqual(taps[0], (int(720 * 0.14), int(1280 * 0.45), False))
            nudged_counts = [
                r["skip_stall_dialogue_nudge"]
                for r in summary["route_reports"]
                if "skip_stall_dialogue_nudge" in r
            ]
            self.assertEqual(nudged_counts, [1, 2, 3])

    def test_fix6_adaptation_untracked_change_session_and_debounce(self) -> None:
        from core.auto_trainer import larc_adaptation as la

        with TemporaryDirectory() as td:
            tmp = Path(td)
            sess_path = tmp / "sess.json"
            img_path = tmp / "frame.png"
            img = np.zeros((1280, 720, 3), dtype=np.uint8)
            cv2.imwrite(str(img_path), img)

            plan = la.build_adaptation_plan(
                {"levels": [1, 1, 1, 1, 0, 0, 0, 0, 0, 0], "points": 300, "pending_cost": 0},
                phase="first_exchange",
            )
            saved = la.LarcAdaptationSession(
                "run-fix6",
                sess_path,
                plan=plan,
                source_sha256="a" * 64,
                stage="selecting",
                control_waits={"untracked_change": 2},
            )
            saved.save()
            loaded = la.LarcAdaptationSession.load(sess_path, "run-fix6")
            self.assertEqual(loaded.control_waits, {"untracked_change": 2})

            deb = la.LarcAdaptationSession(
                "run-fix6",
                sess_path,
                plan=plan,
                source_sha256="a" * 64,
                stage="selecting",
                control_waits={},
            )
            drifted_reader = SimpleNamespace(
                read_grid=lambda _im: {
                    "levels": [1, 2, 1, 1, 0, 0, 0, 0, 0, 0],
                    "pending_cost": 0,
                    "points": 300,
                    "errors": [],
                    "selected_slot": 1,
                }
            )
            ev = {"image": str(img_path), "screen_text": {"normalized_text": "海外适应性 获得条件一览 消耗积分"}}
            d1 = la.plan_larc_adaptation(img, ev, session=deb, reader=drifted_reader)
            d2 = la.plan_larc_adaptation(img, ev, session=deb, reader=drifted_reader)
            d3 = la.plan_larc_adaptation(img, ev, session=deb, reader=drifted_reader)
            self.assertEqual((d1["action"], d1["reason"]), ("wait", "larc_adaptation_grid_loading"))
            self.assertEqual((d2["action"], d2["reason"]), ("wait", "larc_adaptation_grid_loading"))
            self.assertEqual((d3["action"], d3["reason"]), ("stop", "larc_adaptation_untracked_change"))

            # Recovery clears counter before 3rd observation
            deb_rec = la.LarcAdaptationSession(
                "run-fix6",
                sess_path,
                plan=plan,
                source_sha256="a" * 64,
                stage="selecting",
                control_waits={"untracked_change": 1},
            )
            clean_reader = SimpleNamespace(
                read_grid=lambda _im: {
                    "levels": [1, 1, 1, 1, 0, 0, 0, 0, 0, 0],
                    "pending_cost": 0,
                    "points": 300,
                    "errors": [],
                    "selected_slot": 1,
                }
            )
            with patch.object(la, "_adaptation_control", return_value={"status": "enabled", "kind": "level_up"}):
                d_ok = la.plan_larc_adaptation(img, ev, session=deb_rec, reader=clean_reader)
            self.assertEqual(d_ok["action"], "tap")
            self.assertNotIn("untracked_change", deb_rec.control_waits)

    def test_fix7_unregistered_revision_readonly_fallback_and_seed_tolerance(self) -> None:
        from core.auto_trainer import larc_strategy_extension as ext
        from core.auto_trainer import larc_strategy_versions as sv
        from core.career_workflow_library_v2 import BuildLibraryStore, LIBRARY_V2, SCHEMA_V2, default_mode_settings

        with TemporaryDirectory() as td:
            root = Path(td)
            v2_path = root / LIBRARY_V2
            v2_path.parent.mkdir(parents=True, exist_ok=True)
            v2_obj = {
                "schema_version": SCHEMA_V2,
                "revision": 1,
                "selected_build_id": "b1",
                "selected_mode_id": "direct_career",
                "builds": [
                    {
                        "id": "b1",
                        "name": "测试方案",
                        "recipe": {
                            "setup": {},
                            "larc_policy": {"variant": "baseline", "revision": "9.9.9_unregistered"},
                        },
                        "mode_settings": default_mode_settings(),
                    }
                ],
                "custom_modes": [],
            }
            raw_before = json.dumps(v2_obj, ensure_ascii=False, indent=2)
            v2_path.write_text(raw_before, encoding="utf-8")

            store = BuildLibraryStore(root)
            payload = store.payload()
            self.assertEqual(len(payload["library"]["builds"]), 1)
            loaded_rev = payload["library"]["builds"][0]["recipe"]["larc_policy"]["revision"]
            self.assertEqual(loaded_rev, sv.CURRENT_REVISION)
            self.assertTrue(payload.get("warnings"))
            self.assertIn("9.9.9_unregistered", payload["warnings"][0])
            # Read-only load must leave the disk file byte-for-byte untouched
            self.assertEqual(v2_path.read_text(encoding="utf-8"), raw_before)

        # Bad initial_policies seed on fresh root must fall back cleanly instead of raising
        with TemporaryDirectory() as td:
            store_fresh = BuildLibraryStore(Path(td))
            bad_seed = {
                "default": {
                    "career_options": {"targets": {"speed": "", "stamina": "", "power": "", "guts": "", "wisdom": ""}},
                    "cut_options": {"cut": {"max_attempts": "20"}},
                }
            }
            payload_fresh = store_fresh.payload(initial_policies=bad_seed)
            self.assertEqual(len(payload_fresh["library"]["builds"]), 1)

        # Broken external strategy pack must be isolated into load_errors without aborting bootstrap
        with TemporaryDirectory() as td:
            bad_pack = Path(td) / "broken_pack"
            bad_pack.mkdir(parents=True, exist_ok=True)
            (bad_pack / "manifest.json").write_text("{not valid json", encoding="utf-8")
            with patch.dict(os.environ, {"MAAUMA_STRATEGY_PACK_DIR": str(bad_pack)}):
                reg = ext.reload_extensions()
                status = ext.strategy_pack_status()
                self.assertEqual(len(reg.load_errors), 1)
                self.assertEqual(len(status["load_errors"]), 1)
                self.assertIn("JSONDecodeError", status["load_errors"][0]["error"])


if __name__ == "__main__":
    unittest.main()
