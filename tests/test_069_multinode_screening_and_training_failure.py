"""Unit tests for MaaUma 0.6.9 multi-node screening, training-failure auto-restart, cross-career resume guard, and expedition low-vit wisdom lock."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import types
from types import SimpleNamespace
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
PACER_BACKEND = Path(os.environ.get("MAAUMA_PACER_BACKEND_DIR") or (REPO_ROOT / "pacer" / "backend"))


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


def _find_external_pack_dir() -> Path | None:
    pack_root = REPO_ROOT.parent / "private-strategy-pack"
    if not pack_root.is_dir():
        return None
    for child in sorted(pack_root.iterdir()):
        if child.is_dir() and (child / "manifest.json").is_file() and (child / "screening_presets").is_dir():
            return child
    return None


def _period(year_index: int, month: int, half: str, *, pre_debut: bool = False) -> dict:
    stage = {1: "junior", 2: "classic", 3: "senior"}[year_index]
    value: dict = {"stage": stage, "year_index": year_index, "month": month, "half": half}
    if pre_debut:
        value["pre_debut"] = True
    return value


class Test069MultinodeScreeningAndFixes(unittest.TestCase):
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

    def test_continuation_multinode_realtime_luck_custom_dates_and_pause_only(self) -> None:
        from core.auto_trainer.larc_continuation import (
            any_restart_requested,
            custom_date_entry,
            enforce_pause_only,
            evaluate_custom_date,
            evaluate_inheritance2,
            evaluate_pre_expedition,
            normalize,
        )

        raw = {
            "schema_version": "larc_continuation.v1",
            "enabled": True,
            "notify": True,
            "junior": {
                "mode": "filter",
                "combine": "all",
                "min_pass": 1,
                "conditions": [
                    {"kind": "realtime_luck_min", "minimum": -3},
                    {"kind": "stat_sum_min", "minimum": 1700},
                ],
                "on_fail": "restart",
            },
            "pre_expedition": {
                "mode": "filter",
                "combine": "all",
                "min_pass": 1,
                "conditions": [
                    {"kind": "realtime_luck_min", "minimum": -2.5},
                    {"kind": "stat_sum_min", "minimum": 2500},
                ],
                "on_fail": "restart",
            },
            "inheritance": {
                "mode": "observe",
                "skills": [],
                "min_skills": 1,
                "distances": {},
                "on_fail": "pause",
            },
            "inheritance2": {
                "mode": "filter",
                "combine": "all",
                "min_pass": 1,
                "conditions": [{"kind": "stat_sum_min", "minimum": 4300}],
                "skills": [],
                "min_skills": 1,
                "distances": {},
                "on_fail": "restart",
            },
            "custom_dates": [
                {
                    "id": "date_2_12_second",
                    "label": "经典12月下",
                    "year": 2,
                    "month": 12,
                    "half": "second",
                    "mode": "filter",
                    "combine": "all",
                    "min_pass": 1,
                    "conditions": [{"kind": "realtime_luck_min", "minimum": 0}],
                    "on_fail": "restart",
                }
            ],
        }
        cfg = normalize(raw)
        self.assertTrue(any_restart_requested(cfg))
        paused = enforce_pause_only(cfg)
        self.assertFalse(any_restart_requested(paused))
        for key in ("junior", "pre_expedition", "inheritance", "inheritance2"):
            self.assertEqual(paused[key]["on_fail"], "pause")
        self.assertEqual(paused["custom_dates"][0]["on_fail"], "pause")

        facts_pass = {
            "realtime_luck": -1.5,
            "stats": {"speed": 650, "stamina": 500, "power": 500, "guts": 450, "wisdom": 500},
        }
        facts_fail = {
            "realtime_luck": -4.0,
            "stats": {"speed": 600, "stamina": 500, "power": 450, "guts": 450, "wisdom": 450},
        }
        self.assertEqual(evaluate_pre_expedition(cfg, facts_pass)["status"], "passed")
        self.assertEqual(evaluate_pre_expedition(cfg, facts_fail)["status"], "failed")

        facts_inh2_pass = {
            "stats": {"speed": 1000, "stamina": 850, "power": 900, "guts": 750, "wisdom": 850},
            "skills": [],
            "distances": {},
        }
        facts_inh2_fail = {
            "stats": {"speed": 900, "stamina": 800, "power": 800, "guts": 700, "wisdom": 800},
            "skills": [],
            "distances": {},
        }
        self.assertEqual(evaluate_inheritance2(cfg, facts_inh2_pass)["status"], "passed")
        self.assertEqual(evaluate_inheritance2(cfg, facts_inh2_fail)["status"], "failed")

        entry = custom_date_entry(cfg, "date_2_12_second")
        self.assertIsNotNone(entry)
        self.assertEqual(evaluate_custom_date(entry, {"realtime_luck": 1.0})["status"], "passed")
        self.assertEqual(evaluate_custom_date(entry, {"realtime_luck": -0.5})["status"], "failed")

        with self.assertRaises(ValueError):
            normalize({**raw, "custom_dates": [raw["custom_dates"][0], raw["custom_dates"][0]]})
        with self.assertRaises(ValueError):
            normalize({**raw, "pre_expedition": {**raw["pre_expedition"], "conditions": [{"kind": "realtime_luck_min", "minimum": -35}]}})

    def test_training_failure_should_restart_and_runtime_authorization(self) -> None:
        from core.auto_trainer.ace_graduation import normalize_ace_cycle
        from core.auto_trainer.cut_training_failure import training_failure_should_restart
        from scripts.career_workflow_runtime import (
            DOCUMENT,
            TRAINING_FAILURE_RESTART,
            authorized_training_failure_restart,
        )

        ace = normalize_ace_cycle(
            {
                "target_graduates": 2,
                "graduation": {"enabled": True, "surfaces": {"dirt": "A"}, "min_stat_sum": 6000},
                "ss_sieve": {"pre_debut": True, "year1_min": 4, "allow_ready_five": True},
                "restart_after_training_failure": True,
                "training_failure_ignore_wisdom": True,
                "training_failure_stop_at_senior_expedition": True,
            }
        )
        self.assertTrue(ace["restart_after_training_failure"])
        self.assertEqual(ace["graduation"]["surfaces"], {"dirt": "A"})
        self.assertIsNotNone(ace["ss_sieve"])

        # Wisdom tolerated when ignore_wisdom=True; speed restarts before senior July expedition.
        wis_fail = {"run_id": "run1", "training_type": "wisdom"}
        spd_fail = {"run_id": "run1", "training_type": "speed"}
        self.assertFalse(
            training_failure_should_restart(
                wis_fail, ignore_wisdom=True, stop_at_senior_expedition=True, period=_period(2, 5, "first")
            )
        )
        self.assertTrue(
            training_failure_should_restart(
                wis_fail, ignore_wisdom=False, stop_at_senior_expedition=True, period=_period(2, 5, "first")
            )
        )
        self.assertTrue(
            training_failure_should_restart(
                spd_fail, ignore_wisdom=True, stop_at_senior_expedition=True, period=_period(3, 6, "second")
            )
        )
        self.assertFalse(
            training_failure_should_restart(
                spd_fail, ignore_wisdom=True, stop_at_senior_expedition=True, period=_period(3, 7, "first")
            )
        )
        self.assertTrue(
            training_failure_should_restart(
                spd_fail, ignore_wisdom=True, stop_at_senior_expedition=False, period=_period(3, 7, "first")
            )
        )

        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "run_tf"
            (root / "session").mkdir(parents=True)
            endpoint = "127.0.0.1:16384"
            doc = {
                "template_id": "ace_career_cycle",
                "run_graph": {
                    "nodes": [{"id": "grow", "kind": "career", "ace_cycle": ace}],
                },
            }
            (root / "progress.json").write_text(
                json.dumps({"run_session_id": root.name, "device": endpoint}), encoding="utf-8"
            )
            (root / DOCUMENT).write_text(json.dumps(doc), encoding="utf-8")
            (root / "session/ace_cycle_binding.json").write_text(
                json.dumps(
                    {
                        "run_id": root.name,
                        "training_failure_restart": {
                            "enabled": True,
                            "ignore_wisdom": True,
                            "stop_at_senior_expedition": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (root / "session/ace_training_failure.json").write_text(
                json.dumps(
                    {
                        "run_id": root.name,
                        "endpoint": endpoint,
                        "period": _period(2, 8, "first"),
                        "failure": {"run_id": root.name, "training_type": "speed"},
                    }
                ),
                encoding="utf-8",
            )
            store = SimpleNamespace(
                document=doc,
                data={"run_id": root.name, "attempt_root": str(root.resolve()), "endpoint": endpoint},
            )
            report = {"stop_reason": TRAINING_FAILURE_RESTART, "run_session_id": root.name}
            self.assertTrue(authorized_training_failure_restart(store, root, report))

            # Wisdom failure packet must NOT authorize restart when ignore_wisdom=True
            (root / "session/ace_training_failure.json").write_text(
                json.dumps(
                    {
                        "run_id": root.name,
                        "endpoint": endpoint,
                        "period": _period(2, 8, "first"),
                        "failure": {"run_id": root.name, "training_type": "wisdom"},
                    }
                ),
                encoding="utf-8",
            )
            self.assertFalse(authorized_training_failure_restart(store, root, report))

    def test_cross_career_resume_clears_stale_adaptations_and_mei_outing(self) -> None:
        from core.auto_trainer.larc_adaptation import LarcAdaptationSession, build_adaptation_plan
        from scripts.larc_autonomous_run import (
            _reconcile_live_takeover_career_continuity,
            _record_career_fingerprint,
        )

        sha_a = "a" * 64
        sha_b = "b" * 64
        device = "127.0.0.1:16384"

        def make_provider(year: int, month: int, half: str, stats: dict[str, int], *, pre_debut: bool = False) -> dict:
            per = _period(year, month, half, pre_debut=pre_debut)
            return {
                "valid_for_training": True,
                "larc_period": {"status": "verified", "value": per},
                "state_context": {
                    "current_stats": stats,
                    "scenario_state": {"scenario": "larc", "larc_period": {"status": "verified", "value": per}},
                },
            }

        # Case 1: Old Senior Year (ordinal 60) -> swapped save at Classic T43 (ordinal 38)
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "run_cross_career"
            session_dir = root / "session"
            session_dir.mkdir(parents=True)
            exp_plan = build_adaptation_plan(
                {"levels": [2, 2, 2, 2, 2, 1, 0, 0, 0, 0], "points": 400, "pending_cost": 0},
                phase="expedition_skill_pt",
            )
            LarcAdaptationSession(
                run_session_id=root.name,
                path=session_dir / "larc_adaptation_expedition_skill_pt.json",
                plan=exp_plan,
                source_sha256=sha_a,
                stage="complete",
                submitted_source_sha256=sha_a,
                returned=True,
                phase="expedition_skill_pt",
            ).save()
            (session_dir / "larc_adaptation_ready.json").write_text(
                json.dumps(
                    {
                        "run_session_id": root.name,
                        "device": device,
                        "phases": {"expedition_skill_pt": str(root / "old/report.json")},
                    }
                ),
                encoding="utf-8",
            )
            (session_dir / "larc_friend_outing.json").write_text(
                json.dumps(
                    {
                        "schema_version": "larc_outing_session.v1",
                        "run_session_id": root.name,
                        "unlocked": True,
                        "completed": 3,
                        "source": {"run_session_id": root.name, "turn_index": 20, "path": str(root / "old/menu.png"), "sha256": sha_a},
                        "pending": None,
                        "receipts": [],
                    }
                ),
                encoding="utf-8",
            )
            (root / "progress.json").write_text(json.dumps({"next_turn": 43, "device": device}), encoding="utf-8")

            ctx_prev = SimpleNamespace(root=root, run_id=root.name, progress={"next_turn": 62, "device": device})
            _record_career_fingerprint(
                ctx_prev,
                make_provider(3, 7, "first", {"speed": 1100, "stamina": 850, "power": 900, "guts": 750, "wisdom": 850}),
            )

            ctx_new = SimpleNamespace(
                root=root,
                run_id=root.name,
                progress={"next_turn": 43, "device": device},
                ready_adaptations={"expedition_skill_pt": "larc_adaptation_verified"},
                growth_checks={("k1",)},
                live_takeover_year1_adapt_recheck=True,
            )
            summary: dict = {}
            reset = _reconcile_live_takeover_career_continuity(
                ctx_new,
                make_provider(2, 8, "first", {"speed": 700, "stamina": 520, "power": 560, "guts": 480, "wisdom": 520}),
                summary,
                lambda: None,
            )
            self.assertIsNotNone(reset)
            self.assertIn("date_regressed", reset["reasons"])
            self.assertIn("stats_discontinuity", reset["reasons"])
            self.assertTrue(reset["cleared_mei_outing"])
            self.assertFalse((session_dir / "larc_adaptation_expedition_skill_pt.json").exists())
            self.assertFalse((session_dir / "larc_friend_outing.json").exists())
            self.assertEqual(ctx_new.ready_adaptations, {})
            self.assertEqual(ctx_new.growth_checks, set())
            ready_doc = json.loads((session_dir / "larc_adaptation_ready.json").read_text(encoding="utf-8"))
            self.assertEqual(ready_doc["phases"], {})

        # Case 2: Monotonic resume in same career keeps valid expedition_skill_pt and mei_outing
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "run_same_career"
            session_dir = root / "session"
            session_dir.mkdir(parents=True)
            exp_plan = build_adaptation_plan(
                {"levels": [2, 2, 2, 2, 2, 1, 0, 0, 0, 0], "points": 400, "pending_cost": 0},
                phase="expedition_skill_pt",
            )
            LarcAdaptationSession(
                run_session_id=root.name,
                path=session_dir / "larc_adaptation_expedition_skill_pt.json",
                plan=exp_plan,
                source_sha256=sha_b,
                stage="complete",
                submitted_source_sha256=sha_b,
                returned=True,
                phase="expedition_skill_pt",
            ).save()
            (session_dir / "larc_adaptation_ready.json").write_text(
                json.dumps(
                    {
                        "run_session_id": root.name,
                        "device": device,
                        "phases": {"expedition_skill_pt": str(root / "ok/report.json")},
                    }
                ),
                encoding="utf-8",
            )
            (session_dir / "larc_friend_outing.json").write_text(
                json.dumps(
                    {
                        "schema_version": "larc_outing_session.v1",
                        "run_session_id": root.name,
                        "unlocked": True,
                        "completed": 2,
                        "pending": None,
                        "receipts": [],
                    }
                ),
                encoding="utf-8",
            )
            (root / "progress.json").write_text(json.dumps({"next_turn": 44, "device": device}), encoding="utf-8")

            ctx_prev = SimpleNamespace(root=root, run_id=root.name, progress={"next_turn": 43, "device": device})
            _record_career_fingerprint(
                ctx_prev,
                make_provider(2, 8, "first", {"speed": 700, "stamina": 520, "power": 560, "guts": 480, "wisdom": 520}),
            )

            ctx_same = SimpleNamespace(
                root=root,
                run_id=root.name,
                progress={"next_turn": 44, "device": device},
                ready_adaptations={"expedition_skill_pt": "larc_adaptation_verified"},
                growth_checks={("k1",)},
                live_takeover_year1_adapt_recheck=True,
            )
            res = _reconcile_live_takeover_career_continuity(
                ctx_same,
                make_provider(2, 8, "second", {"speed": 725, "stamina": 530, "power": 575, "guts": 485, "wisdom": 530}),
                {},
                lambda: None,
            )
            self.assertIsNone(res)
            self.assertFalse(ctx_same.live_takeover_year1_adapt_recheck)
            self.assertTrue((session_dir / "larc_adaptation_expedition_skill_pt.json").is_file())
            self.assertTrue((session_dir / "larc_friend_outing.json").is_file())

        # Case 3: No larc_career_fingerprint.json (upgrading from 0.6.8 run dir) -> falls back to summary.json & ahead-of-date ready index
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "run_068_legacy"
            session_dir = root / "session"
            old_seg = root / "autonomous_20261125_120000_000001"
            session_dir.mkdir(parents=True)
            old_seg.mkdir(parents=True)
            (old_seg / "summary.json").write_text(
                json.dumps(
                    {
                        "run_session_id": root.name,
                        "turns": [
                            {
                                "turn_index": 38,
                                "before": {
                                    "period": _period(2, 8, "first"),
                                    "current_stats": {"speed": 900, "stamina": 700, "power": 800, "guts": 600, "wisdom": 700},
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (session_dir / "larc_adaptation_ready.json").write_text(
                json.dumps(
                    {
                        "run_session_id": root.name,
                        "device": device,
                        "phases": {"expedition_skill_pt": str(root / "old/report.json")},
                    }
                ),
                encoding="utf-8",
            )
            (session_dir / "larc_friend_outing.json").write_text(
                json.dumps({"schema_version": "larc_outing_session.v1", "run_session_id": root.name, "unlocked": True, "completed": 3, "pending": None, "receipts": []}),
                encoding="utf-8",
            )
            (root / "progress.json").write_text(json.dumps({"next_turn": 20, "device": device}), encoding="utf-8")
            ctx_legacy = SimpleNamespace(
                root=root,
                run_id=root.name,
                progress={"next_turn": 20, "device": device},
                ready_adaptations={"expedition_skill_pt": "larc_adaptation_verified"},
                growth_checks=set(),
                live_takeover_year1_adapt_recheck=True,
            )
            res = _reconcile_live_takeover_career_continuity(
                ctx_legacy,
                make_provider(2, 5, "first", {"speed": 500, "stamina": 400, "power": 500, "guts": 350, "wisdom": 450}),
                {},
                lambda: None,
            )
            self.assertIsNotNone(res)
            self.assertIn("date_regressed", res["reasons"])
            self.assertIn("cached_adaptation_ahead_of_date", res["reasons"])
            self.assertTrue(res["cleared_mei_outing"])
            self.assertEqual(ctx_legacy.ready_adaptations, {})

    def test_external_pack_screening_presets_and_low_vit_non_rainbow_french_guard(self) -> None:
        pack_dir = _find_external_pack_dir()
        if pack_dir is None:
            self.skipTest("External strategy pack directory not present")

        from core.auto_trainer.larc_continuation import normalize
        from core.auto_trainer.larc_strategy_extension import active_provider, reload_extensions
        from core.auto_trainer.larc_strategy_params import PRESET_REGISTRY

        manifest = json.loads((pack_dir / "manifest.json").read_text(encoding="utf-8-sig"))
        presets = manifest.get("screening_presets") or []
        self.assertGreaterEqual(len(presets), 3)
        for item in presets:
            cfg_path = pack_dir / item["file"]
            self.assertTrue(cfg_path.is_file(), f"Missing preset file {cfg_path}")
            raw = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            raw.pop("training_failure_restart", None)
            normalized = normalize(raw)
            self.assertEqual(normalized["schema_version"], "larc_continuation.v1")

        os.environ["MAAUMA_STRATEGY_PACK_DIR"] = str(pack_dir)
        reload_extensions()
        provider = active_provider()

        guarded_params = next(
            (
                p
                for p in PRESET_REGISTRY.values()
                if any("non_rainbow" in k and v is True for k, v in getattr(p, "ext", ()))
            ),
            None,
        )
        self.assertIsNotNone(guarded_params, "Expected an external extension preset in PRESET_REGISTRY")

        def make_built(*, speed_eligible: bool = False):
            offers = [
                {"offer_id": "train:speed|NoBuy", "action_id": "train:speed", "phases": [], "eligible": speed_eligible, "direct_score": 80.0 if speed_eligible else None},
                {"offer_id": "train:stamina|NoBuy", "action_id": "train:stamina", "phases": [], "eligible": False},
                {"offer_id": "train:power|NoBuy", "action_id": "train:power", "phases": [], "eligible": False},
                {"offer_id": "train:guts|NoBuy", "action_id": "train:guts", "phases": [], "eligible": False},
                {"offer_id": "train:wisdom|NoBuy", "action_id": "train:wisdom", "phases": [], "eligible": True, "direct_score": 55.0},
                {"offer_id": "train:wisdom|french_three", "action_id": "train:wisdom", "phases": ["french_three"], "eligible": True, "direct_score": 114.26},
                {"offer_id": "rest.normal|NoBuy", "action_id": "rest.normal", "phases": [], "eligible": True, "direct_score": 71.17},
            ]
            return {"offers": offers}

        def check_gate(*, now: int, vit: float, rainbow: bool, speed_eligible: bool = False):
            built = make_built(speed_eligible=speed_eligible)
            m = SimpleNamespace(LEVEL_TWO_COSTS=[50] * 10, _cost=lambda s, lv: 100)
            ctx = provider.quote_wisdom_gate_prepare(
                params=guarded_params,
                unified=True,
                now=now,
                levels=[1, 1, 1, 1, 0, 1, 0, 0, 0, 0],
                grid={"points": 400},
                share=1.0,
                built=built,
                base={"action_id": "train:wisdom"},
                income=lambda _o: {"amount": 50},
                m=m,
                income_at=lambda _d, _s, _sh: 0,
                race_completion_ap={40: 100},
            )
            self.assertIsNotNone(ctx)
            return provider.quote_wisdom_gate_check(
                ctx,
                {
                    "offer_id": "train:wisdom|french_three",
                    "action_id": "train:wisdom",
                    "phase": "french_three",
                    "targets": [1, 1, 1, 1, 3, 1, 0, 0, 0, 0],
                    "cost": 150,
                    "gains": {"wisdom": 30},
                    "current_risk": 0,
                    "direct_score": 114.26,
                },
                {"amount": 50},
                levels=[1, 1, 1, 1, 0, 1, 0, 0, 0, 0],
                candidates={
                    "train:wisdom": SimpleNamespace(
                        training_type="wisdom",
                        friendship=rainbow,
                        stats_gain={"wisdom": 10},
                    )
                },
                state=SimpleNamespace(vitality=vit),
                goal=None,
                grid={"points": 400},
                now=now,
                m=m,
            )

        # All 4 spending trainings rejected + vit=30 + non-rainbow wisdom at now=36 (T48) -> blocked
        wg_blocked = check_gate(now=36, vit=30.0, rainbow=False, speed_eligible=False)
        self.assertIsNotNone(wg_blocked)
        self.assertFalse(wg_blocked["allowed"])
        self.assertIn("french_three_non_rainbow_low_vit_lock_blocked", wg_blocked["reason"])

        # Same at now=37 also blocked
        wg_blocked_37 = check_gate(now=37, vit=35.0, rainbow=False, speed_eligible=False)
        self.assertIsNotNone(wg_blocked_37)
        self.assertFalse(wg_blocked_37["allowed"])

        # Rainbow wisdom -> NOT blocked by low-vit lock
        wg_rainbow = check_gate(now=36, vit=30.0, rainbow=True, speed_eligible=False)
        self.assertIsNone(wg_rainbow)

        # Speed training eligible -> NOT blocked by low-vit lock
        wg_speed_ok = check_gate(now=36, vit=30.0, rainbow=False, speed_eligible=True)
        self.assertIsNone(wg_speed_ok)

        # Vit 50 -> NOT blocked by low-vit lock
        wg_high_vit = check_gate(now=36, vit=50.0, rainbow=False, speed_eligible=False)
        self.assertIsNone(wg_high_vit)

    def test_plant_roster_104602_yellow_falcon_and_109101_aliases(self) -> None:
        import importlib
        import numpy as np

        full_roster = json.loads(
            (REPO_ROOT / "backend/core/autonomous_training/plant_roster.json").read_text(encoding="utf-8-sig")
        )
        full_map = {str(r["id"]): r for r in full_roster["records"]}
        self.assertEqual(full_map["104602"]["outfit"], "黄昏凯旋")
        self.assertIn("黄昏三人组", full_map["104602"].get("outfit_aliases", []))
        self.assertIn("强击", full_map["109101"].get("aliases", []))

        slim_roster = json.loads(
            (REPO_ROOT / "pacer/backend/core/autonomous_training/plant_roster.json").read_text(encoding="utf-8-sig")
        )
        slim_map = {str(r["id"]): r for r in slim_roster["records"]}
        self.assertIn("强击", slim_map["109101"].get("aliases", []))

        backend_dir = str((REPO_ROOT / "backend").resolve())
        added_backend = False
        if backend_dir not in sys.path:
            sys.path.append(backend_dir)
            added_backend = True
        try:
            trainee = importlib.import_module("core.autonomous_training.trainee")
            img = np.zeros((1280, 720, 3), dtype=np.uint8)
            obs = trainee.variant_observation(
                img,
                [{"text": "[黄昏凯旋]醒目飞鹰", "confidence": 0.99, "bbox": [10, 400, 220, 435]}],
                "104602",
            )
            self.assertEqual(obs["selected_variant"], "104602")
            self.assertEqual(obs["selected_name"], "醒目飞鹰")
            self.assertEqual(obs["identity_basis"], "selected_name_and_outfit")
        finally:
            if added_backend and backend_dir in sys.path:
                sys.path.remove(backend_dir)


if __name__ == "__main__":
    unittest.main()
