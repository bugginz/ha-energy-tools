"""Tests for the pre-dawn battery→car dump + universal floor-guard (spec 2026-07-08).

Stdlib unittest only (no pytest), same as tests/test_foxctl.py:

    python3 -m unittest tests.test_predawn -v
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "energy_tools"))
import foxctl  # noqa: E402


class PredawnBudgetTest(unittest.TestCase):
    """ev_predawn_budget: usable battery above the planning floor minus expected load to window."""

    def test_live_worked_example(self):
        # 2026-07-08 23:50 live numbers: SoC 56%, 41.44 kWh battery, 30% floor, ~8 kWh load to 11:00.
        budget, parts = foxctl.ev_predawn_budget(56.0, 41.44, 30, 8.0)
        self.assertAlmostEqual(parts["usable_above_floor_kwh"], 10.77, places=2)
        self.assertAlmostEqual(budget, 2.77, places=2)
        self.assertEqual(parts["load_to_window_kwh"], 8.0)

    def test_below_floor_clamps_to_zero_usable(self):
        budget, parts = foxctl.ev_predawn_budget(25.0, 41.44, 30, 2.0)
        self.assertEqual(parts["usable_above_floor_kwh"], 0.0)
        self.assertEqual(budget, -2.0)

    def test_none_load_treated_as_zero(self):
        budget, parts = foxctl.ev_predawn_budget(50.0, 41.44, 30, None)
        self.assertAlmostEqual(budget, 8.29, places=2)


if __name__ == "__main__":
    unittest.main()
