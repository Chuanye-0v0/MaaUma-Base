"""Self-contained public tests for Strategy Extension SPI and open RunControl."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
PACER_BACKEND = REPO_ROOT / "pacer" / "backend"


def _ensure_pacer_backend_path() -> None:
    resolved = str(PACER_BACKEND.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)


class TestStrategyExtensionSPI(unittest.TestCase):
    def setUp(self) -> None:
        _ensure_pacer_backend_path()
        self._prev_pack_env = os.environ.get("MAAUMA_STRATEGY_PACK_DIR")
        os.environ["MAAUMA_STRATEGY_PACK_DIR"] = "__NONE__"
        from core.auto_trainer.larc_strategy_extension import reload_extensions

        reload_extensions()

    def tearDown(self) -> None:
        if self._prev_pack_env is None:
            os.environ.pop("MAAUMA_STRATEGY_PACK_DIR", None)
        else:
            os.environ["MAAUMA_STRATEGY_PACK_DIR"] = self._prev_pack_env
        from core.auto_trainer.larc_strategy_extension import reload_extensions

        reload_extensions()

    def test_public_baseline_only_and_open_run_control(self) -> None:
        from core.auto_trainer import larc_strategy_params as sp
        from core.auto_trainer import larc_strategy_versions as sv
        from core.auto_trainer.cut_runtime_license import LicensedRunControl
        from core.auto_trainer.larc_strategy_extension import strategy_pack_status

        self.assertEqual(sv.CURRENT_REVISION, "2.3")
        self.assertEqual(
            set(sp.PRESET_REGISTRY.keys()),
            {
                "2.1",
                "2.2",
                "2.3",
                "2.3_pro",
                "unified_v2_1",
                "unified_v2_2",
                "unified_v2_3",
                "unified_v2_3_pro",
            },
        )
        with self.assertRaises(ValueError):
            sv.validate_revision("9.9.9")

        status = strategy_pack_status()
        self.assertFalse(status["has_external_pack"])
        self.assertEqual(status["current_revision"], "2.3")

        ctrl = LicensedRunControl(REPO_ROOT)
        snap = ctrl.snapshot()
        self.assertEqual(snap["runtime_license"]["status"], "open")
        self.assertEqual(snap["runtime_license"]["permission_scope"], "open_source")
        ctrl.checkpoint("spi_test_step")
        self.assertNotEqual(ctrl.snapshot()["state"], "paused")

    def test_synthetic_external_pack_registers_and_reloads(self) -> None:
        from dataclasses import replace
        from core.auto_trainer import larc_strategy_params as sp
        from core.auto_trainer import larc_strategy_rules as sr
        from core.auto_trainer import larc_strategy_versions as sv
        from core.auto_trainer.larc_strategy_extension import reload_extensions, strategy_pack_status

        with TemporaryDirectory() as tmp:
            pack_dir = Path(tmp) / "synthetic_pack"
            pack_dir.mkdir(parents=True, exist_ok=True)
            manifest = {
                "schema_version": "maauma.strategy_pack.v1",
                "pack_id": "synthetic_test_pack",
                "display_name": "Synthetic Test Pack",
                "version": "9.9.0",
                "entry": "pack.py",
                "default_revision": "9.9",
                "ui_revisions": [
                    {
                        "revision": "9.9",
                        "label": "9.9 Synthetic Preset",
                        "notice": "Synthetic test revision.",
                        "default_failure_limit": 30,
                        "attribute_weights": {"speed": 1.1, "stamina": 1.0, "power": 1.0, "guts": 1.0, "wisdom": 1.0},
                        "after_target_credit": 0.4,
                    }
                ],
            }
            (pack_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (pack_dir / "pack.py").write_text(
                (
                    "from dataclasses import replace\n"
                    "from core.auto_trainer.larc_strategy_extension import StrategyExtensionProvider\n"
                    "from core.auto_trainer.larc_strategy_params import PRESET_2_3_PRO, with_ext\n"
                    "class _SyntheticProvider(StrategyExtensionProvider):\n"
                    "    def get_param_attr(self, params, name):\n"
                    "        d = dict(params.ext)\n"
                    "        if name in d: return d[name]\n"
                    "        raise AttributeError(name)\n"
                    "    def first_arc_win_ap_expectation(self, state, goal=None, target_levels=None):\n"
                    "        return {'win_ap': 99, 'synthetic': True}\n"
                    "def register_strategy_pack(registry, *, manifest=None, pack_dir=None):\n"
                    "    preset = with_ext(replace(PRESET_2_3_PRO, revision='9.9', preset_id='unified_v9_9', policy_version='larc_unified_v9_9_test'), synthetic_flag=True)\n"
                    "    registry.register_preset('9.9', preset)\n"
                    "    registry.register_policy_version('9.9', 'larc_unified_v9_9_test')\n"
                    "    registry.set_default_current_revision('9.9')\n"
                    "    registry.set_provider(_SyntheticProvider())\n"
                ),
                encoding="utf-8",
            )
            os.environ["MAAUMA_STRATEGY_PACK_DIR"] = str(pack_dir)
            reload_extensions()

            self.assertEqual(sv.CURRENT_REVISION, "9.9")
            self.assertEqual(sv.validate_revision("9.9"), "9.9")
            self.assertIn("9.9", sp.PRESET_REGISTRY)
            self.assertTrue(sp.PRESET_REGISTRY["9.9"].synthetic_flag)
            self.assertEqual(sr.first_arc_win_ap_expectation(None).get("win_ap"), 99)
            self.assertTrue(strategy_pack_status()["has_external_pack"])

            os.environ["MAAUMA_STRATEGY_PACK_DIR"] = "__NONE__"
            reload_extensions()
            self.assertEqual(sv.CURRENT_REVISION, "2.3")
            self.assertNotIn("9.9", sp.PRESET_REGISTRY)


if __name__ == "__main__":
    unittest.main()
