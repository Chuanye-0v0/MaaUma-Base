"""Regression tests for continuation string-number coercion, startup validation, and node-specific errors."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import types
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


_ensure_pacer_backend_path()

from core.auto_trainer.larc_continuation import defaults as cont_defaults, normalize as norm_cont
from core.career_workflow import validate_start_document
from core.career_workflow_library_v2 import SCHEMA_V2, compile_v2_document, normalize_library_v2


def _string_continuation() -> dict:
    return {
        "schema_version": "larc_continuation.v1",
        "enabled": True,
        "notify": True,
        "junior": {
            "mode": "filter",
            "combine": "all",
            "min_pass": "1",
            "conditions": [
                {"kind": "realtime_luck_min", "minimum": " -3 "},
                {"kind": "stat_sum_min", "minimum": "1700"},
                {"kind": "bond_ready_min", "minimum": "2", "bond_threshold": "80"},
            ],
            "on_fail": "restart",
        },
        "pre_expedition": {
            "mode": "filter",
            "combine": "all",
            "min_pass": "2",
            "conditions": [
                {"kind": "realtime_luck_min", "minimum": "-2.5"},
                {"kind": "stat_sum_min", "minimum": "2500"},
            ],
            "on_fail": "restart",
        },
        "inheritance": {
            "mode": "observe",
            "skills": [],
            "min_skills": "1",
            "distances": {},
            "on_fail": "pause",
        },
        "inheritance2": {
            "mode": "filter",
            "combine": "all",
            "min_pass": "2",
            "conditions": [{"kind": "stat_sum_min", "minimum": "4300"}],
            "skills": [],
            "min_skills": "1",
            "distances": {},
            "on_fail": "restart",
        },
        "custom_dates": [
            {
                "id": "date_2_12_second",
                "label": "经典12月下",
                "year": "2",
                "month": "12",
                "half": "second",
                "mode": "filter",
                "combine": "all",
                "min_pass": "2",
                "conditions": [{"kind": "stat_sum_min", "minimum": "3600"}],
                "on_fail": "restart",
            }
        ],
    }


def _sample_v2_library_with_strings() -> dict:
    return {
        "schema_version": SCHEMA_V2,
        "revision": 40,
        "selected_build_id": "build_1",
        "selected_mode_id": "ace_career_cycle",
        "builds": [
            {
                "id": "build_1",
                "name": "小林测试",
                "recipe": {
                    "setup": {"character_id": "109801", "character_name": "小林历奇"},
                },
                "mode_settings": {
                    "cut_preserve": {
                        "cut_params": {"max_attempts": "20", "max_tp_items": "0", "max_carrots": "0"},
                        "continuation": _string_continuation(),
                    },
                    "ss_sieve_cycle": {
                        "ss_sieve": {"pre_debut": False, "year1_min": "4", "allow_ready_five": False},
                        "cycle_budget": {"max_attempts": "20", "max_tp_items": "0", "max_carrots": "0"},
                    },
                    "ace_career_cycle": {
                        "ace_cycle": {
                            "target_graduates": "5",
                            "graduation": {"enabled": True, "min_stats": {"speed": "1550"}, "min_stat_sum": "4500"},
                            "ss_sieve": None,
                            "restart_after_training_failure": True,
                        },
                        "cycle_budget": {"max_attempts": "100", "max_tp_items": "100", "max_carrots": "0"},
                        "continuation": _string_continuation(),
                    },
                },
            }
        ],
        "custom_modes": [],
    }


class ContinuationCoercionTests(unittest.TestCase):
    def test_legacy_string_numbers_coerce_cleanly(self) -> None:
        out = norm_cont(_string_continuation())
        self.assertIs(type(out["junior"]["min_pass"]), int)
        self.assertEqual(out["junior"]["min_pass"], 1)
        self.assertEqual(out["junior"]["conditions"][0]["minimum"], -3)
        self.assertIs(type(out["junior"]["conditions"][1]["minimum"]), int)
        self.assertEqual(out["junior"]["conditions"][1]["minimum"], 1700)
        self.assertEqual(out["junior"]["conditions"][2]["minimum"], 2)
        self.assertEqual(out["junior"]["conditions"][2]["bond_threshold"], 80)
        self.assertEqual(out["pre_expedition"]["conditions"][0]["minimum"], -2.5)
        self.assertIs(type(out["inheritance"]["min_skills"]), int)
        self.assertIs(type(out["inheritance2"]["min_pass"]), int)
        cd = out["custom_dates"][0]
        self.assertIs(type(cd["year"]), int)
        self.assertEqual((cd["year"], cd["month"], cd["min_pass"]), (2, 12, 2))
        self.assertEqual(cd["conditions"][0]["minimum"], 3600)

    def test_node_specific_errors_for_empty_and_out_of_range(self) -> None:
        bad_junior = _string_continuation()
        bad_junior["junior"]["min_pass"] = ""
        with self.assertRaises(ValueError) as ctx1:
            norm_cont(bad_junior)
        self.assertIn("首年·最低达标项数", str(ctx1.exception))

        bad_pre = _string_continuation()
        bad_pre["pre_expedition"]["min_pass"] = None
        with self.assertRaises(ValueError) as ctx2:
            norm_cont(bad_pre)
        self.assertIn("首次远征前·最低达标项数", str(ctx2.exception))

        bad_inh2 = _string_continuation()
        bad_inh2["inheritance2"]["min_pass"] = "101"
        with self.assertRaises(ValueError) as ctx3:
            norm_cont(bad_inh2)
        self.assertIn("第二次继承后·最低达标项数", str(ctx3.exception))

        bad_luck = _string_continuation()
        bad_luck["junior"]["conditions"][0]["minimum"] = "-40"
        with self.assertRaises(ValueError) as ctx4:
            norm_cont(bad_luck)
        self.assertIn("首年·胡度值最低门槛", str(ctx4.exception))

    def test_disabled_skips_dirty_numbers_but_still_rejects_structural_errors(self) -> None:
        cfg = cont_defaults()
        cfg["enabled"] = False
        cfg["junior"]["min_pass"] = "x"
        cfg["junior"]["conditions"] = [{"kind": "stat_sum_min", "minimum": ""}]
        cfg["pre_expedition"]["min_pass"] = 0
        cfg["inheritance"]["min_skills"] = ""
        cfg["custom_dates"] = [
            {
                "id": "date_3_6_second",
                "label": "高级6月下",
                "year": 3,
                "month": 6,
                "half": "second",
                "mode": "filter",
                "combine": "all",
                "min_pass": "bad",
                "conditions": [{"kind": "stat_sum_min", "minimum": ""}],
                "on_fail": "pause",
            }
        ]
        res = norm_cont(cfg)
        self.assertFalse(res["enabled"])

        bad_struct = deepcopy(cfg)
        bad_struct["junior"]["mode"] = "invalid_mode"
        with self.assertRaises(ValueError):
            norm_cont(bad_struct)

        bad_key = deepcopy(cfg)
        bad_key["unknown_field"] = 1
        with self.assertRaises(ValueError):
            norm_cont(bad_key)

    def test_validate_start_document_catches_continuation_and_budget_before_run(self) -> None:
        lib = normalize_library_v2(_sample_v2_library_with_strings())
        doc = compile_v2_document(lib["builds"][0], "ace_career_cycle")
        self.assertEqual(validate_start_document(doc), {"ok": True, "errors": [], "warnings": []})

        bad_cont_doc = deepcopy(doc)
        bad_cont_doc["policies"]["default"]["career_options"]["continuation"]["pre_expedition"]["min_pass"] = ""
        chk = validate_start_document(bad_cont_doc)
        self.assertFalse(chk["ok"])
        self.assertTrue(any("首次远征前·最低达标项数" in err for err in chk["errors"]), chk)

        bad_budget_doc = deepcopy(doc)
        bad_budget_doc["policies"]["default"]["cut_options"]["cut"]["max_tp_items"] = "abc"
        chk2 = validate_start_document(bad_budget_doc)
        self.assertFalse(chk2["ok"])
        self.assertTrue(any("TP药最多用几瓶" in err for err in chk2["errors"]), chk2)

        disabled_dirty_doc = deepcopy(doc)
        cont = disabled_dirty_doc["policies"]["default"]["career_options"]["continuation"]
        cont["enabled"] = False
        cont["junior"]["min_pass"] = "dirty"
        self.assertEqual(validate_start_document(disabled_dirty_doc)["ok"], True)

    def test_normalize_library_v2_self_heals_strings_on_load(self) -> None:
        lib = normalize_library_v2(_sample_v2_library_with_strings())
        ms = lib["builds"][0]["mode_settings"]["ace_career_cycle"]
        self.assertEqual(ms["cycle_budget"]["max_attempts"], 100)
        self.assertIs(type(ms["cycle_budget"]["max_attempts"]), int)
        self.assertEqual(ms["cycle_budget"]["max_tp_items"], 100)
        self.assertIs(type(ms["ace_cycle"]["target_graduates"]), int)
        self.assertEqual(ms["ace_cycle"]["graduation"]["min_stats"]["speed"], 1550)
        self.assertIs(type(ms["continuation"]["junior"]["min_pass"]), int)
        self.assertEqual(ms["continuation"]["junior"]["conditions"][1]["minimum"], 1700)
        self.assertEqual(ms["continuation"]["custom_dates"][0]["year"], 2)
        cut_cp = lib["builds"][0]["mode_settings"]["cut_preserve"]["cut_params"]
        self.assertIs(type(cut_cp["max_attempts"]), int)
        self.assertEqual(cut_cp["max_attempts"], 20)

    def test_larc_umaa_host_career_coerces_and_enforces_restart_guard(self) -> None:
        from scripts.larc_umaa_host import PacerHost

        captured = {}

        def fake_service_factory(directory, profiles, **kwargs):
            captured["profiles"] = profiles
            return types.SimpleNamespace(
                snapshot=lambda: {"state": "stopped"},
                wait=lambda *a, **k: None,
                close=lambda: None,
            )

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            umaa_root = root / "umaa"
            (umaa_root / "runs/device-occupancy").mkdir(parents=True, exist_ok=True)
            (root / "scripts/larc_continuation_collect.py").parent.mkdir(parents=True, exist_ok=True)
            (root / "scripts/larc_continuation_collect.py").write_text("# stub\n", encoding="utf-8")
            (root / "scripts/larc_continuation_inspection.py").write_text("# stub\n", encoding="utf-8")
            host = PacerHost(
                umaa_root,
                root=root,
                service_factory=fake_service_factory,
                lease=types.SimpleNamespace(probe=lambda *a, **k: None),
            )
            host._claim = lambda ep: True  # noqa: SLF001
            from contextlib import nullcontext
            host._occupy = lambda ep: nullcontext()  # noqa: SLF001

            # Career mode forbids restart on custom_dates/pre_expedition/etc.
            bad_cont = _string_continuation()
            with self.assertRaises(ValueError) as cm:
                host.start({
                    "action": "start",
                    "mode": "career",
                    "endpoint": "127.0.0.1:16448",
                    "request_id": "req-restart-check",
                    "options": {"continuation": bad_cont},
                })
            self.assertIn("自动回刷切", str(cm.exception))

            # Career mode with pause-only string continuation coerces cleanly and passes normalized ints.
            ok_cont = _string_continuation()
            for k in ("junior", "pre_expedition", "inheritance", "inheritance2"):
                ok_cont[k]["on_fail"] = "pause"
            ok_cont["custom_dates"][0]["on_fail"] = "pause"

            captured_opts = {}
            import scripts.larc_umaa_host as luh
            import scripts.larc_ordinary_setup as los
            import scripts.larc_continuation_runtime as lcr

            old_run_career = luh.create_panel_run
            old_submit = luh.submit_button
            orig_parse_setup = los.parse_ordinary_setup
            orig_save_setup = los.save_ordinary_setup
            orig_upd = luh.update_goal
            orig_smb = luh.save_mei_breaks
            orig_srp = luh.save_ramonu_present
            orig_save_cont = lcr.save_options

            def fake_create_panel_run(**kwargs):
                rr = Path(kwargs["runs_dir"]) / "test_run"
                (rr / "session").mkdir(parents=True, exist_ok=True)
                (rr / "goal.json").write_text(json.dumps({"larc_policy": {"variant": "alpha"}}), encoding="utf-8")
                (rr / "progress.json").write_text(
                    json.dumps({"run_session_id": rr.name, "device": f"127.0.0.1:{kwargs['device_port']}"}),
                    encoding="utf-8",
                )
                for rel in ("session/transition_session.json", "session/skip_session.json", "session/larc_member_roster.json"):
                    (rr / rel).write_text("{}", encoding="utf-8")
                return rr

            def spy_save_cont(path, continuation):
                captured_opts["continuation"] = continuation
                return orig_save_cont(path, continuation)

            luh.create_panel_run = fake_create_panel_run
            luh.submit_button = lambda *a, **k: None
            los.parse_ordinary_setup = lambda *a, **k: {"character_id": "109801"}
            los.save_ordinary_setup = lambda *a, **k: None
            luh.update_goal = lambda *a, **k: None
            luh.save_mei_breaks = lambda *a, **k: None
            luh.save_ramonu_present = lambda *a, **k: None
            lcr.save_options = spy_save_cont
            try:
                res = host.start({
                    "action": "start",
                    "mode": "career",
                    "endpoint": "127.0.0.1:16448",
                    "request_id": "req-coerce-career-1",
                    "options": {
                        "continuation": ok_cont,
                        "targets": {"speed": 1200, "stamina": 1000, "power": 1000, "guts": 900, "wisdom": 1000},
                    },
                })
                self.assertIn("control", res)
                self.assertIs(type(captured_opts["continuation"]["junior"]["min_pass"]), int)
                self.assertEqual(captured_opts["continuation"]["junior"]["min_pass"], 1)
                self.assertEqual(captured_opts["continuation"]["junior"]["conditions"][0]["minimum"], -3)
                self.assertEqual(captured_opts["continuation"]["junior"]["conditions"][1]["minimum"], 1700)
                self.assertEqual(captured_opts["continuation"]["junior"]["conditions"][2]["bond_threshold"], 80)
            finally:
                luh.create_panel_run = old_run_career
                luh.submit_button = old_submit
                los.parse_ordinary_setup = orig_parse_setup
                los.save_ordinary_setup = orig_save_setup
                luh.update_goal = orig_upd
                luh.save_mei_breaks = orig_smb
                luh.save_ramonu_present = orig_srp
                lcr.save_options = orig_save_cont


if __name__ == "__main__":
    unittest.main()
