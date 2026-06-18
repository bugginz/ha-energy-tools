"""Tests for foxctl decision + apply plumbing.

Stdlib unittest only (no pytest) so this runs in the HA add-on image with zero extra deps:

    python3 -m unittest discover -s tests -v

These cover two latent bugs fixed alongside them:
  * /api/apply never persisted its result into LAST -> dashboard header stayed "applied: None"
    even after a successful apply  (see apply_and_record).
  * the loop captured the auto-apply flag once at startup, so toggling allow_control / auto_apply
    in the config needed a full restart  (see refresh_control).
"""

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "energy_tools"))
import foxctl  # noqa: E402


def base_strat():
    return copy.deepcopy(foxctl.DEFAULT_CONFIG["strategy"])


class RefreshControlTest(unittest.TestCase):
    """refresh_control() reloads control flags from disk so a config edit takes effect without a restart."""

    def setUp(self):
        self._orig_path = foxctl.CONFIG_PATH
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.path = Path(path)
        foxctl.CONFIG_PATH = self.path

    def tearDown(self):
        foxctl.CONFIG_PATH = self._orig_path
        self.path.unlink(missing_ok=True)

    def _write(self, control):
        self.path.write_text(json.dumps({"control": control}))

    def test_picks_up_toggled_flags_from_disk(self):
        # process started with auto-apply OFF...
        cfg = {"control": {"allow_control": False, "auto_apply": False, "set_force_charge": False}}
        # ...user edits the config on disk to turn it on
        self._write({"allow_control": True, "auto_apply": True, "set_force_charge": True})
        auto = foxctl.refresh_control(cfg)
        self.assertTrue(auto)
        self.assertTrue(cfg["control"]["allow_control"])
        self.assertTrue(cfg["control"]["set_force_charge"])

    def test_auto_requires_both_flags(self):
        cfg = {"control": {"allow_control": True, "auto_apply": True}}
        self._write({"allow_control": True, "auto_apply": False})  # auto_apply off -> no auto
        self.assertFalse(foxctl.refresh_control(cfg))
        self._write({"allow_control": False, "auto_apply": True})  # master switch off -> no auto
        self.assertFalse(foxctl.refresh_control(cfg))

    def test_only_known_keys_updated(self):
        # a stray key on disk must not be injected into the in-memory control block
        cfg = {"control": {"allow_control": False, "auto_apply": False}}
        self._write({"allow_control": True, "auto_apply": True, "bogus": 123})
        foxctl.refresh_control(cfg)
        self.assertNotIn("bogus", cfg["control"])

    def test_bad_file_keeps_in_memory_values(self):
        cfg = {"control": {"allow_control": True, "auto_apply": True}}
        self.path.write_text("{ not valid json")  # mid-write / corrupt
        auto = foxctl.refresh_control(cfg)
        self.assertTrue(auto)  # falls back to current in-memory flags, does not crash
        self.assertTrue(cfg["control"]["allow_control"])

    def test_missing_file_keeps_in_memory_values(self):
        cfg = {"control": {"allow_control": True, "auto_apply": True}}
        self.path.unlink()
        self.assertTrue(foxctl.refresh_control(cfg))


class ApplyAndRecordTest(unittest.TestCase):
    """apply_and_record() must persist the apply outcome into LAST so the dashboard reflects it."""

    def setUp(self):
        self._orig_apply = foxctl.apply_recommendation
        self._orig_last = foxctl.LAST

    def tearDown(self):
        foxctl.apply_recommendation = self._orig_apply
        foxctl.LAST = self._orig_last

    def test_writes_result_into_last(self):
        foxctl.apply_recommendation = lambda cfg, snap: "force-charge START until ~23:45"
        foxctl.LAST = {"applied": None, "soc": 50}
        msg = foxctl.apply_and_record({}, {"recommendation": {}})
        self.assertEqual(msg, "force-charge START until ~23:45")
        self.assertEqual(foxctl.LAST["applied"], "force-charge START until ~23:45")  # the bug: stayed None

    def test_empty_last_does_not_crash(self):
        foxctl.apply_recommendation = lambda cfg, snap: "nothing to do"
        foxctl.LAST = {}
        msg = foxctl.apply_and_record({}, {"recommendation": {}})
        self.assertEqual(msg, "nothing to do")


class ApplyRecommendationGateTest(unittest.TestCase):
    """The safety gates in apply_recommendation must short-circuit BEFORE any inverter write."""

    def test_blocks_when_control_disabled(self):
        cfg = {"control": {"allow_control": False}}
        msg = foxctl.apply_recommendation(cfg, {"recommendation": {"force_charge": True}})
        self.assertIn("control disabled", msg)

    def test_holds_on_stale_telemetry(self):
        cfg = {"control": {"allow_control": True}, "foxess": {"token": "x", "sn": "y"}}
        snap = {"recommendation": {"force_charge": True}, "telemetry_source": "FoxESS (HA stale)"}
        msg = foxctl.apply_recommendation(cfg, snap)
        self.assertIn("STALE", msg)


class DecideTest(unittest.TestCase):
    """Core recommendation logic — the FORCE_CHARGE branch from the bug report (price <= start)."""

    def _prices(self, price, **kw):
        return {"price": price, "forecast": [], "aemo_forecast": [], "feedin": None, **kw}

    def test_force_charge_when_price_at_or_below_start(self):
        strat = base_strat()  # charge_start_price = 0.12
        rec = foxctl.decide(self._prices(0.11), soc=50, pv_kw=0.0, work_mode="SelfUse", strat=strat)
        self.assertTrue(rec["force_charge"])
        self.assertEqual(rec["action"], "FORCE_CHARGE")

    def test_no_charge_when_battery_full(self):
        strat = base_strat()
        rec = foxctl.decide(self._prices(0.11), soc=100, pv_kw=0.0, work_mode="SelfUse", strat=strat)
        self.assertFalse(rec["force_charge"])
        self.assertEqual(rec["target_mode"], "SelfUse")

    def test_foundation_ceiling_blocks_charge_above_price_ceiling(self):
        # price above the absolute ceiling must never grid-charge, even if a branch proposed it.
        strat = base_strat()
        strat["charge_start_price"] = 0.50  # tempt the price<=start branch into firing at a high price
        strat["price_ceiling"] = 0.20
        rec = foxctl.decide(self._prices(0.30), soc=50, pv_kw=0.0, work_mode="SelfUse", strat=strat)
        self.assertFalse(rec["force_charge"])
        self.assertIn("FOUNDATION", rec["reason"])


if __name__ == "__main__":
    unittest.main()
