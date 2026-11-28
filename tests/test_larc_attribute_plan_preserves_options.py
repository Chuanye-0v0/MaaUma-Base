"""Regression coverage for partial strategy extension overrides."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACER_BACKEND = ROOT / "pacer" / "backend"
if str(PACER_BACKEND) not in sys.path:
    sys.path.insert(0, str(PACER_BACKEND))

from core.auto_trainer import larc_attribute_plan as attribute_plan
from core.auto_trainer import larc_strategy_extension, larc_strategy_params
from core.auto_trainer.larc_calendar import period_at
from core.auto_trainer.larc_opportunity_forecast import marginal_gains


class AttributeOptionsRegressionTest(unittest.TestCase):
    def setUp(self):
        self.options = {
            "attribute_weights": {
                "speed": 1.10,
                "stamina": 1.20,
                "power": 1.0,
                "guts": 0.85,
                "wisdom": 0.80,
            },
            "after_target_credit": 0.45,
            "high_stat_scarcity": False,
            "restore_high_stat_value": True,
        }
        self.extension_options = {
            "far_surplus_credit": 0.15,
            "far_surplus_buffer": 25.0,
            "far_surplus_exact_buffer": True,
        }
        self.params = SimpleNamespace(
            default_after_target_credit=0.5,
            late_far_surplus_ordinals=(63, 65),
            late_far_surplus_credit=0.2,
            late_far_surplus_buffer=40,
        )

    def resolve(self, extension_options, *, options=None, day=52):
        provider = SimpleNamespace(
            late_surplus_options=lambda params, value, ordinal: extension_options
        )
        with (
            patch.object(larc_strategy_extension, "active_provider", return_value=provider),
            patch.object(larc_strategy_params, "params_for_goal", return_value=self.params),
            patch.object(larc_strategy_params, "unified_larc_enabled", return_value=True),
        ):
            return attribute_plan.late_surplus_options(
                self.options if options is None else options, period_at(day), None
            )

    def test_partial_extension_preserves_base_fields(self):
        self.assertEqual(
            self.resolve(self.extension_options),
            {**self.options, **self.extension_options},
        )
        self.assertNotIn("far_surplus_credit", self.options)

    def test_partial_extension_retains_real_attribute_scoring(self):
        utility = self.resolve(self.extension_options)
        target = dict(speed=1500, stamina=1200, power=1300, guts=1100, wisdom=1200)
        current = dict(speed=1310, stamina=810, power=1000, guts=850, wisdom=1290)
        gaps = {
            key: {
                "target": cap,
                "active_gap": max(0, cap - current[key]),
                "gap_premium": 0.0,
            }
            for key, cap in target.items()
        }
        stamina = marginal_gains(current, {"stamina": 69, "guts": 32}, gaps, **utility)
        self.assertAlmostEqual(stamina["base"], 69 * 1.20 + 32 * 0.85)
        wisdom = marginal_gains(current, {"wisdom": 20}, gaps, **utility)
        self.assertAlmostEqual(wisdom["base"], 20 * 0.80 * 0.15)
        speed = marginal_gains(current, {"speed": 10}, gaps, **utility)
        self.assertAlmostEqual(speed["base"], 10 * 1.10)
        self.assertAlmostEqual(speed["scarcity"], 10 * 1.10)

    def test_no_extension_retains_base_fields_and_clears_old_surplus(self):
        self.assertEqual(
            self.resolve(None, options={**self.options, **self.extension_options}),
            self.options,
        )

    def test_builtin_late_surplus_still_applies(self):
        utility = {**self.options, "after_target_credit": 0.5}
        self.assertEqual(
            self.resolve(None, options=utility, day=63),
            {**utility, "far_surplus_credit": 0.2, "far_surplus_buffer": 40},
        )


if __name__ == "__main__":
    unittest.main()
