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
from datetime import datetime
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


class FoxESSReadEndpointsTest(unittest.TestCase):
    """report()/history() must hit the right paths with the right body and parse 'result' — no network."""

    def setUp(self):
        self.fox = foxctl.FoxESS("tok", "SN123")
        self.calls = []
        self.fox.call = lambda path, body=None: (self.calls.append((path, body)) or self._resp)

    def test_report_request_shape_and_parse(self):
        self._resp = {"errno": 0, "result": [{"variable": "loads", "unit": "kWh", "values": list(range(24))}]}
        when = datetime(2026, 6, 19, 13, 0)
        res = self.fox.report(["loads", "generation"], "day", when)
        path, body = self.calls[0]
        self.assertEqual(path, "/op/v0/device/report/query")
        self.assertEqual(body, {"sn": "SN123", "dimension": "day",
                                "variables": ["loads", "generation"],
                                "year": 2026, "month": 6, "day": 19})
        self.assertEqual(len(res[0]["values"]), 24)   # hourly array

    def test_history_request_shape(self):
        self._resp = {"errno": 0, "result": [{"datas": []}]}
        self.fox.history(["loadsPower"], 1000.7, 2000.9)
        path, body = self.calls[0]
        self.assertEqual(path, "/op/v0/device/history/query")
        self.assertEqual(body, {"sn": "SN123", "variables": ["loadsPower"], "begin": 1000, "end": 2000})

    def test_empty_result_is_safe(self):
        self._resp = {"errno": 0}   # no "result" key
        self.assertEqual(self.fox.report(["loads"]), [])
        self.assertEqual(self.fox.history(["loadsPower"], 0, 1), [])


class ForecastStoreTest(unittest.TestCase):
    """Phase 2: hourly integration + profile averaging from FoxESS history (no network)."""

    def test_integrate_hourly_trapezoidal(self):
        # 2 kW held across 12:00→12:30 → (2+2)/2 * 0.5h = 1.0 kWh in hour 12; gaps >0.5h skipped
        pts = [{"time": "2026-06-20 12:00:00 AEST+1000", "value": 2.0},
               {"time": "2026-06-20 12:30:00 AEST+1000", "value": 2.0},
               {"time": "2026-06-20 18:00:00 AEST+1000", "value": 9.0}]  # 5.5h gap → not counted
        hourly = foxctl._integrate_hourly(pts)
        self.assertEqual(len(hourly), 24)
        self.assertAlmostEqual(hourly[12], 1.0, places=3)
        self.assertEqual(sum(hourly), 1.0)   # the lone post-gap sample contributes nothing

    def test_fetch_forecast_day_shapes(self):
        class FakeFox:
            def report(self, vars, dim, when):
                return [{"variable": "loads", "unit": "kWh", "values": [1.0] * 24}]
            def history(self, vars, b, e):
                return [{"datas": [{"variable": "pvPower", "data": [
                    {"time": "2026-06-20 11:00:00 AEST+1000", "value": 3.0},
                    {"time": "2026-06-20 11:30:00 AEST+1000", "value": 3.0}]}]}]
        load, solar = foxctl.fetch_forecast_day(FakeFox(), datetime(2026, 6, 20))
        self.assertEqual(load, [1.0] * 24)
        self.assertAlmostEqual(solar[11], 1.5, places=3)   # (3+3)/2*0.5
        self.assertEqual(len(solar), 24)

    def test_forecast_profiles_averages_days(self):
        orig = foxctl._FCAST["days"]
        try:
            foxctl._FCAST["days"] = {
                "2026-06-19": {"load": [2.0] * 24, "solar": [0.0] * 24},
                "2026-06-20": {"load": [4.0] * 24, "solar": [1.0] * 24},
            }
            fp = foxctl.forecast_profiles()
            self.assertEqual(fp["days"], 2)
            self.assertEqual(fp["load_profile"][0], 3.0)    # mean(2,4)
            self.assertEqual(fp["solar_profile"][12], 0.5)  # mean(0,1)
        finally:
            foxctl._FCAST["days"] = orig


if __name__ == "__main__":
    unittest.main()
