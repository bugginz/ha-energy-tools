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
import urllib.error
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
    """Core recommendation logic — Phase 2 need-based, RELATIVE buying (no absolute charge_start_price)."""

    def _prices(self, price, forecast=None, **kw):
        return {"price": price, "forecast": forecast or [], "aemo_forecast": [], "feedin": None, **kw}

    def _fwd(self, *prices):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        return [{"t": (now + timedelta(hours=i + 1)).isoformat(), "price": p} for i, p in enumerate(prices)]

    def test_charges_when_now_is_cheapest_slot_covering_deficit(self):
        strat = base_strat()
        strat["import_deficit_kwh"] = 5.0          # we must import
        strat["price_ceiling"] = 0.30
        rec = foxctl.decide(self._prices(0.10, self._fwd(0.25, 0.25, 0.25)),
                            soc=50, pv_kw=0.0, work_mode="SelfUse", strat=strat)
        self.assertTrue(rec["force_charge"])
        self.assertIn("NEED-BASED", rec["reason"])

    def test_defers_when_a_cheaper_future_slot_covers_the_need(self):
        strat = base_strat()
        strat["import_deficit_kwh"] = 2.0          # ~1 slot needed
        strat["price_ceiling"] = 0.30
        rec = foxctl.decide(self._prices(0.20, self._fwd(0.08)),
                            soc=50, pv_kw=0.0, work_mode="SelfUse", strat=strat)
        self.assertFalse(rec["force_charge"])      # now 0.20 > bar 0.08 → wait

    def test_no_charge_without_a_forward_deficit(self):
        strat = base_strat()
        strat["import_deficit_kwh"] = 0.0          # battery+solar already cover the day
        rec = foxctl.decide(self._prices(0.05), soc=50, pv_kw=0.0, work_mode="SelfUse", strat=strat)
        self.assertFalse(rec["force_charge"])

    def test_floor_always_charges_below_it(self):
        strat = base_strat()
        strat["charge_start_floor"] = 0.12         # always-OK at/below this
        strat["import_deficit_kwh"] = 0.0
        rec = foxctl.decide(self._prices(0.10), soc=50, pv_kw=0.0, work_mode="SelfUse", strat=strat)
        self.assertTrue(rec["force_charge"])

    def test_no_charge_when_battery_full(self):
        strat = base_strat()
        strat["import_deficit_kwh"] = 10.0
        rec = foxctl.decide(self._prices(0.05), soc=100, pv_kw=0.0, work_mode="SelfUse", strat=strat)
        self.assertFalse(rec["force_charge"])
        self.assertEqual(rec["target_mode"], "SelfUse")

    def test_foundation_ceiling_blocks_charge_above_price_ceiling(self):
        # price above the absolute ceiling must never grid-charge, however big the deficit.
        strat = base_strat()
        strat["import_deficit_kwh"] = 20.0
        strat["price_ceiling"] = 0.20
        rec = foxctl.decide(self._prices(0.30), soc=50, pv_kw=0.0, work_mode="SelfUse", strat=strat)
        self.assertFalse(rec["force_charge"])
        self.assertIn("FOUNDATION", rec["reason"])


class ChartUsesRelativeBarTest(unittest.TestCase):
    """The charts must visualise the need-based relative bar (rec['buy_bar']), not the vestigial
    charge_start_price — so the chosen cheap buy slots shade and the SoC projection fills."""

    def _snap(self, buy_bar):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        fc = [{"t": (now + timedelta(hours=i * 0.5)).isoformat(),
               "price": 0.10 if i == 4 else 0.25} for i in range(12)]
        return {"forecast_h": fc, "feedin_forecast_h": [], "aemo_forecast_h": [], "solar_bells": [],
                "recommendation": {"buy_bar": buy_bar, "force_charge": False},
                "dynamic": {"charge_start_price": 0.05, "price_ceiling": 0.30, "sell_price": 0.5,
                            "survival_soc": 30, "target_soc": 90, "max_soc": 90},
                "plan": {"soc_line": [(0, 55), (2, 90)], "floor_line": [(0, 30), (2, 30)]},
                "soc": 55.0}

    def test_forecast_chart_draws_relative_bar(self):
        svg = foxctl.render_forecast_svg(self._snap(0.18), None, 6, "cw6")
        self.assertIn("buy ≤ $0.18 (relative)", svg)   # the bar, not the 0.05 charge_start_price

    def test_falls_back_to_charge_start_when_no_bar(self):
        svg = foxctl.render_forecast_svg(self._snap(None), None, 6, "cw6")
        self.assertIn("buy ≤ $0.05", svg)              # graceful fallback


class ZeroHeroTest(unittest.TestCase):
    """GloBird ZeroHero ToU: no import before 11:00, fill to max in the 11–14 free window, export 18–21,
    and ZERO grid import through the 16–23 peak."""

    def setUp(self):
        self._orig = foxctl.datetime

    def tearDown(self):
        foxctl.datetime = self._orig

    def _rec(self, hour, soc, survival=30, sell_enabled=True):
        import datetime as _dt

        class FDT(_dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return _dt.datetime(2026, 6, 24, hour, 0, 0, tzinfo=tz)
        foxctl.datetime = FDT
        strat = copy.deepcopy(foxctl.DEFAULT_CONFIG["strategy"])
        strat["max_soc"] = 100
        strat["reserve_soc"] = 20
        strat["sell_enabled"] = sell_enabled
        return foxctl.decide_zerohero(soc, "SelfUse", strat, survival)

    def test_free_window_grid_charges_to_max(self):
        r = self._rec(12, 50)
        self.assertTrue(r["force_charge"])
        self.assertIn("FREE", r["reason"])

    def test_free_window_full_holds(self):
        self.assertFalse(self._rec(12, 100)["force_charge"])

    def test_before_11_no_import(self):
        r = self._rec(8, 60)
        self.assertFalse(r["force_charge"])
        self.assertFalse(r["force_discharge"])

    def test_peak_zero_import_no_charge(self):
        # 17:00 is peak (16–23) but not the 18–21 export window → hold, never grid-charge, even if low
        r = self._rec(17, 45)
        self.assertFalse(r["force_charge"])
        self.assertIn("PEAK", r["reason"])

    def test_late_peak_after_export_no_import(self):
        r = self._rec(22, 35)            # 22:00 still peak, past export window
        self.assertFalse(r["force_charge"])
        self.assertFalse(r["force_discharge"])

    def test_evening_window_exports_when_enabled(self):
        r = self._rec(19, 80, survival=30, sell_enabled=True)
        self.assertTrue(r["force_discharge"])

    def test_evening_holds_at_survival(self):
        r = self._rec(19, 30, survival=30, sell_enabled=True)   # at survival → don't export below it
        self.assertFalse(r["force_discharge"])

    def test_no_export_when_feedin_disabled(self):
        # feed-in is bad → auto_sell off → 18–21 behaves like peak: cover from battery, no export
        r = self._rec(19, 90, survival=30, sell_enabled=False)
        self.assertFalse(r["force_discharge"])
        self.assertFalse(r["force_charge"])
        self.assertIn("PEAK", r["reason"])


class BuyTargetKwhTest(unittest.TestCase):
    """NEED-BASED vs TOP-UP buy sizing. Top-up fills headroom to target (less solar) for spike readiness."""

    def test_need_based_is_just_survival(self):
        # topup off → only the survival deficit (+ buffer)
        self.assertEqual(foxctl.buy_target_kwh(50, 30, 100, 5.0, 0.0, topup=False), 5.0)

    def test_topup_fills_headroom_to_target(self):
        # 50% of 30kWh, target 100 → 15 kWh headroom; survival only 2 → top-up dominates
        self.assertEqual(foxctl.buy_target_kwh(50, 30, 100, 2.0, 0.0, topup=True), 15.0)

    def test_topup_subtracts_remaining_solar(self):
        # 15 kWh headroom less 4 kWh remaining solar → buy 11 from grid
        self.assertEqual(foxctl.buy_target_kwh(50, 30, 100, 0.0, 4.0, topup=True), 11.0)

    def test_topup_never_below_survival(self):
        # near-full but a real survival deficit → survival wins
        self.assertEqual(foxctl.buy_target_kwh(95, 30, 100, 6.0, 0.0, topup=True), 6.0)

    def test_buffer_adds_on_top(self):
        self.assertEqual(foxctl.buy_target_kwh(90, 30, 100, 1.0, 0.0, topup=True, buffer=3.0), 6.0)  # 3 headroom +3


class DynamicLeversTest(unittest.TestCase):
    """Phase 3: the strategist nudges target_soc + spike-sell buffer + bar cap, all hard-clamped."""

    def _foundation(self):
        return {"price_ceiling": 0.30, "max_soc": 90, "charge_start_floor": 0.10, "note_active": False}

    def _strat(self):
        s = copy.deepcopy(foxctl.DEFAULT_CONFIG["strategy"])
        s["battery_capacity_kwh"] = 30
        return s

    def test_buffer_clamped_to_half_pack(self):
        out = foxctl.apply_dynamic_params(self._strat(), {"spike_sell_buffer_kwh": 999}, self._foundation())
        self.assertEqual(out["spike_sell_buffer_kwh"], 15.0)      # ≤ 0.5 * 30 kWh

    def test_bar_cap_clamped_to_floor_ceiling(self):
        out = foxctl.apply_dynamic_params(self._strat(), {"buy_bar_cap": 0.99}, self._foundation())
        self.assertEqual(out["buy_bar_cap"], 0.30)               # capped at ceiling
        out2 = foxctl.apply_dynamic_params(self._strat(), {"buy_bar_cap": 0.01}, self._foundation())
        self.assertEqual(out2["buy_bar_cap"], 0.10)              # floored at the floor

    def test_target_soc_clamped_to_max(self):
        out = foxctl.apply_dynamic_params(self._strat(), {"target_soc": 150}, self._foundation())
        self.assertEqual(out["target_soc"], 90)

    def test_bar_cap_tightens_buy_ceiling_in_decide(self):
        # a bar cap below the ceiling must stop buying above it even with a big deficit
        strat = self._strat()
        strat["import_deficit_kwh"] = 20.0
        strat["price_ceiling"] = 0.30
        strat["buy_bar_cap"] = 0.12                              # refuse to buy above 12c
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        fwd = [{"t": (now + timedelta(hours=i + 1)).isoformat(), "price": 0.20} for i in range(4)]
        rec = foxctl.decide({"price": 0.20, "forecast": fwd, "aemo_forecast": [], "feedin": None},
                            soc=50, pv_kw=0.0, work_mode="SelfUse", strat=strat)
        self.assertFalse(rec["force_charge"])                    # 0.20 > bar cap 0.12 → hold

    def test_buffer_adds_to_import_deficit_concept(self):
        # the buffer is added to the forward deficit (unit-level: bigger deficit → bar can rise to buy now)
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        fwd = [{"t": (now + timedelta(hours=i + 1)).isoformat(), "price": p}
               for i, p in enumerate([0.08, 0.15, 0.25])]
        small = foxctl.plan_buy_slots(fwd, 0.20, now, 2.0, 10.5, 0.30, 0.05)    # tiny need → bar low
        big = foxctl.plan_buy_slots(fwd, 0.20, now, 20.0, 10.5, 0.30, 0.05)     # +buffer → bar rises
        self.assertFalse(small["should_charge"])
        self.assertTrue(big["should_charge"])


class PlanBuySlotsTest(unittest.TestCase):
    """The relative, need-based buy planner in isolation."""

    def _now_fc(self, *prices):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        return now, [{"t": (now + timedelta(hours=i + 1)).isoformat(), "price": p} for i, p in enumerate(prices)]

    def test_floor_is_always_ok(self):
        now, _ = self._now_fc()
        r = foxctl.plan_buy_slots([], 0.05, now, 0.0, 10.5, 0.30, 0.10)
        self.assertTrue(r["should_charge"])
        self.assertEqual(r["bar"], 0.10)

    def test_no_deficit_no_buy(self):
        now, _ = self._now_fc()
        self.assertFalse(foxctl.plan_buy_slots([], 0.05, now, 0.0, 10.5, 0.30, 0.0)["should_charge"])

    def test_ceiling_blocks(self):
        now, _ = self._now_fc()
        self.assertFalse(foxctl.plan_buy_slots([], 0.40, now, 5.0, 10.5, 0.30, 0.10)["should_charge"])

    def test_picks_cheapest_covering_slot(self):
        now, fc = self._now_fc(0.25, 0.25, 0.25)
        r = foxctl.plan_buy_slots(fc, 0.10, now, 5.0, 10.5, 0.30, 0.05)
        self.assertTrue(r["should_charge"])
        self.assertEqual(r["slots_needed"], 1)

    def test_defers_to_cheaper_future(self):
        now, fc = self._now_fc(0.08)
        self.assertFalse(foxctl.plan_buy_slots(fc, 0.20, now, 2.0, 10.5, 0.30, 0.05)["should_charge"])

    def test_large_deficit_widens_the_bar(self):
        now, fc = self._now_fc(0.08, 0.15, 0.25)
        r = foxctl.plan_buy_slots(fc, 0.20, now, 20.0, 10.5, 0.30, 0.05)
        self.assertTrue(r["should_charge"])        # need ~4 slots → bar rises to accept now
        self.assertGreaterEqual(r["slots_needed"], 4)


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
            self.assertEqual(fp["solar_profile"][12], 1.0)  # the zero-solar day is excluded → mean(1)
            # daily totals: day sums are 48 and 96 → avg 72, min 48, max 96
            self.assertEqual(fp["daily_total"], {"avg": 72.0, "min": 48.0, "max": 96.0})
        finally:
            foxctl._FCAST["days"] = orig

    def test_zero_data_days_excluded_from_averages(self):
        orig = foxctl._FCAST["days"]
        try:
            foxctl._FCAST["days"] = {
                "2026-06-01": {"load": [0.0] * 24, "solar": [0.0] * 24},   # pre-install: must be ignored
                "2026-06-02": {"load": [0.0] * 24, "solar": [0.0] * 24},   # pre-install: must be ignored
                "2026-06-18": {"load": [2.0] * 24, "solar": [0.0] * 24},   # real load, but still no panels
                "2026-06-19": {"load": [2.0] * 24, "solar": [1.0] * 24},   # real load + real solar
            }
            fp = foxctl.forecast_profiles()
            self.assertEqual(fp["days"], 2)         # 2 valid LOAD days (zero-load days dropped)
            self.assertEqual(fp["days_solar"], 1)   # only 1 valid SOLAR day
            self.assertEqual(fp["load_profile"][0], 2.0)          # not dragged toward 0 by empty days
            self.assertEqual(fp["solar_profile"][12], 1.0)        # avg over the single generating day
            self.assertEqual(fp["daily_total"], {"avg": 48.0, "min": 48.0, "max": 48.0})
        finally:
            foxctl._FCAST["days"] = orig


class SolarCalibrationTest(unittest.TestCase):
    """Phase 3: forecast-vs-actual solar bias — no-op until enough samples, then clamped."""

    def setUp(self):
        self._orig_cal = dict(foxctl._SOLAR_CAL)
        self._orig_days = foxctl._FCAST["days"]
        self._orig_loaded = foxctl._FCAST["loaded"]
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.path = Path(path)
        self.cfg = {"state_dir": str(self.path.parent)}
        foxctl._SOLAR_CAL.update({"path": None, "fc": {}, "samples": [], "loaded": True})
        self._orig_save = foxctl.save_scal
        foxctl.save_scal = lambda cfg: None   # don't touch disk
        foxctl._FCAST["loaded"] = True

    def tearDown(self):
        foxctl.save_scal = self._orig_save
        foxctl._SOLAR_CAL.clear(); foxctl._SOLAR_CAL.update(self._orig_cal)
        foxctl._FCAST["days"] = self._orig_days
        foxctl._FCAST["loaded"] = self._orig_loaded
        self.path.unlink(missing_ok=True)

    def _seed_pairs(self, fc, act, n):
        # n completed days, each with forecast `fc` and actual `act`
        foxctl._SOLAR_CAL["samples"] = [{"d": f"2026-05-{i+1:02d}", "fc": fc, "act": act} for i in range(n)]

    def test_no_op_until_min_samples(self):
        self._seed_pairs(10.0, 5.0, foxctl.SOLAR_CAL_MIN - 1)   # forecast 2x too high, but too few days
        res = foxctl.update_solar_cal(self.cfg, 8.0)
        self.assertFalse(res["applied"])
        self.assertEqual(res["bias"], 1.0)

    def test_bias_learned_and_clamped(self):
        self._seed_pairs(10.0, 5.0, foxctl.SOLAR_CAL_MIN)       # actual is half the forecast → bias 0.5
        res = foxctl.update_solar_cal(self.cfg, 8.0)
        self.assertTrue(res["applied"])
        self.assertEqual(res["bias"], 0.5)                      # at the clamp floor
        # an extreme over-forecast cannot push bias below the clamp
        self._seed_pairs(10.0, 1.0, foxctl.SOLAR_CAL_MIN)
        self.assertEqual(foxctl.update_solar_cal(self.cfg, 8.0)["bias"], foxctl.SOLAR_CAL_CLAMP[0])

    def test_pairs_forecast_with_actual_from_store(self):
        foxctl._SOLAR_CAL["fc"] = {"2026-06-10": 12.0}
        foxctl._FCAST["days"] = {"2026-06-10": {"load": [0.0] * 24, "solar": [0.5] * 24}}  # actual 12.0
        foxctl.update_solar_cal(self.cfg, 9.0)
        sample = [s for s in foxctl._SOLAR_CAL["samples"] if s["d"] == "2026-06-10"]
        self.assertEqual(len(sample), 1)
        self.assertEqual(sample[0]["act"], 12.0)


class StatBackfillTest(unittest.TestCase):
    """HA statistics backfill: hourly cumulative-sum series from the forecast store (no network)."""

    def setUp(self):
        self._orig = foxctl._FCAST["days"]

    def tearDown(self):
        foxctl._FCAST["days"] = self._orig

    def test_cumulative_series_and_zero_day_skipped(self):
        foxctl._FCAST["days"] = {
            "2026-06-19": {"load": [1.0] * 24, "solar": [0.0] * 24},   # solar zero → no solar points
            "2026-06-20": {"load": [2.0] * 24, "solar": [0.5] * 24},
        }
        series = foxctl.build_stat_series(7)
        _, _, load_pts = series["foxctl:load_energy"]
        self.assertEqual(len(load_pts), 48)            # 2 days × 24h
        self.assertEqual(load_pts[-1][1], 72.0)        # cumulative: 24*1 + 24*2
        _, _, solar_pts = series["foxctl:solar_energy"]
        self.assertEqual(len(solar_pts), 24)           # only the generating day
        self.assertEqual(solar_pts[-1][1], 12.0)       # 24 * 0.5
        self.assertTrue(load_pts[0][0].isoformat())    # start is a tz-aware datetime


class RenderSmokeTest(unittest.TestCase):
    """render() the full dashboard with a realistic snapshot so a page-crashing bug can't ship."""

    def test_page_renders_with_numeric_ev_power(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        fc = [{"t": (now + timedelta(hours=i * 0.5)).isoformat(), "price": 0.15, "descriptor": "x"}
              for i in range(12)]
        snap = {"ts": "2026-06-22T11:00", "price": 0.2, "aemo_price": 0.07, "feedin": 0.06, "soc": 91.0,
                "pv_kw": 0.0, "ev_kw": 2.53, "load_kw": 3.1, "grid_power": 1.0, "feedin_power": 0.0,
                "battery_power": -1.0, "ev_divert": "car charger ON (export …)", "work_mode": "SelfUse",
                "telemetry_source": "FoxESS", "data_age_s": 30, "forecast_next": fc, "forecast_h": fc,
                "aemo_forecast_h": [], "recommendation": {"action": "SET_MODE", "target_mode": "SelfUse",
                "reason": "full", "band": "normal", "force_charge": False},
                "dynamic": {"charge_start_price": 0.12, "price_ceiling": 0.2, "target_soc": 90,
                "max_soc": 90, "survival_soc": 30, "sell_price": 0.5, "sell_enabled": True,
                "source": "LLM", "mode": "amber"}, "battery": {"capacity_kwh": 30.0, "stored_kwh": 27.3},
                "consumption": {"avg_daily_total_kwh": 33.0, "days_sampled": 5,
                "hour_profile": {h: 1.0 for h in range(24)}}, "forecast_profiles": {"days": 5, "days_solar": 5},
                "solar_forecast": {"today_total": 7, "remaining_today": 1, "tomorrow": 15},
                "solar_cal": {"bias": 1.0, "applied": False, "samples": 2}, "solar_bells": [],
                "plan": {"action_now": "hold", "target_now": 91.0, "soc_line": [], "floor_line": []},
                "scheduler": {"enabled": False, "active": None}, "applied": "work mode already SelfUse",
                "llm": None}
        cfg = {"control": {"allow_control": True, "auto_apply": True, "set_force_charge": True},
               "strategy": {"force_charge_power_kw": 10.5, "target_soc": 90},
               "ev_divert": {"switch": "switch.x"}}
        html = foxctl.render(snap, cfg)
        self.assertIn("EV charger", html)
        self.assertIn("🔌 2.53", html)        # numeric ev_kw must format, not raise
        self.assertGreater(len(html), 5000)
        # grid-flow card shows export when feeding in
        exporting = dict(snap, feedin=0.67, feedin_power=3.2)
        self.assertIn("EXPORTING @ $0.67", foxctl.render(exporting, cfg))


class EvDivertTest(unittest.TestCase):
    """Solar-diversion policy: divert to the car when export is cheap/grid is cheap, but yield to the
    house battery while it's charging toward the planner target before a sell."""

    EV = {"feedin_max": 0.10, "allow_grid": True, "min_export_kw": 1.0,
          "min_soc": 0, "battery_priority": True, "min_dwell_min": 10}

    def _snap(self, **kw):
        s = {"feedin": 0.30, "feedin_power": 0.0, "price": 0.25, "soc": 98,
             "dynamic": {"charge_start_price": 0.12}, "plan": {"target_now": 95}}
        s.update(kw)
        return s

    def test_diverts_on_cheap_export_surplus(self):
        want, _ = foxctl.ev_divert_decision(self._snap(feedin=0.05, feedin_power=3.0), self.EV)
        self.assertTrue(want)

    def test_diverts_on_cheap_grid(self):
        want, _ = foxctl.ev_divert_decision(self._snap(price=0.10), self.EV)   # buy ≤ charge_start
        self.assertTrue(want)

    def test_battery_priority_blocks_solar_surplus_below_target(self):
        # SOLAR surplus + planner wants 100% and SoC is 80% → battery gets the spare solar first, car off
        want, why = foxctl.ev_divert_decision(
            self._snap(feedin=0.05, feedin_power=3.0, price=0.25, soc=80, plan={"target_now": 100}), self.EV)
        self.assertFalse(want)
        self.assertIn("solar to battery first", why)

    def test_cheap_grid_charges_alongside_battery(self):
        # CHEAP GRID (buy ≤ charge_start) does NOT yield — car charges while the battery tops off too
        want, why = foxctl.ev_divert_decision(
            self._snap(price=0.10, soc=80, plan={"target_now": 100}), self.EV)
        self.assertTrue(want)
        self.assertIn("car + battery", why)

    def test_held_off_while_selling(self):
        s = self._snap(price=0.10, recommendation={"force_discharge": True})   # cheap grid, but we're selling
        want, why = foxctl.ev_divert_decision(s, self.EV)
        self.assertFalse(want)
        self.assertIn("selling", why)

    def test_held_off_while_force_charging_below_target(self):
        s = self._snap(price=0.10, soc=80, recommendation={"force_charge": True},
                       dynamic={"charge_start_price": 0.12, "target_soc": 100})
        want, why = foxctl.ev_divert_decision(s, self.EV)
        self.assertFalse(want)
        self.assertIn("force-charging", why)

    def test_charges_when_force_charge_near_target(self):
        # 98% with target 100 → within 5% of target, battery top-off nearly done → car may charge too
        s = self._snap(price=0.10, soc=98, recommendation={"force_charge": True},
                       dynamic={"charge_start_price": 0.12, "target_soc": 100})
        self.assertTrue(foxctl.ev_divert_decision(s, self.EV)[0])

    def test_no_divert_when_nothing_cheap(self):
        want, why = foxctl.ev_divert_decision(self._snap(), self.EV)   # dear export, dear grid
        self.assertFalse(want)
        self.assertIn("not cheap", why)


class EvDailyCapTest(unittest.TestCase):
    """Interim daily car cap: auto-divert charges up to N kWh/day then holds off (no car SoC needed)."""

    def setUp(self):
        self._orig = foxctl.ha_call_service
        self.calls = []
        foxctl.ha_call_service = lambda cfg, d, s, e: self.calls.append((d, s, e))
        foxctl._EV.update({"on": None, "last_change": 0.0, "override_until": 0.0,
                           "session_day": None, "session_start_kwh": None, "capped": False})
        self.cfg = {"control": {"allow_control": True},
                    "ev_divert": {"switch": "switch.x", "feedin_max": 0.10, "allow_grid": True,
                                  "min_export_kw": 1.0, "min_dwell_min": 0, "battery_priority": False,
                                  "session_cap_kwh": 30}}

    def tearDown(self):
        foxctl.ha_call_service = self._orig
        foxctl._EV.update({"on": None, "last_change": 0.0, "override_until": 0.0,
                           "session_day": None, "session_start_kwh": None, "capped": False})

    def _snap(self, ev_cum):
        return {"feedin": 0.06, "feedin_power": 3.0, "price": 0.10, "soc": 98,
                "dynamic": {"charge_start_price": 0.12}, "plan": {"target_now": 95},
                "energy_totals": {"ev": ev_cum}}

    def test_charges_then_caps_at_kwh(self):
        first = foxctl.ev_divert_tick(self.cfg, self._snap(0.0))   # session starts, car on
        self.assertIn("ON", first)
        self.assertTrue(foxctl._EV["on"])
        capped = foxctl.ev_divert_tick(self.cfg, self._snap(31.0))  # 31 kWh delivered → over the 30 cap
        self.assertIn("daily cap", capped)
        self.assertFalse(foxctl._EV["on"])

    def test_manual_force_overrides_cap(self):
        foxctl._EV["capped"] = True
        foxctl._EV["session_start_kwh"] = 0.0
        foxctl._EV["override_until"] = 1e18      # active manual force
        msg = foxctl.ev_divert_tick(self.cfg, self._snap(50.0))
        self.assertIn("manual force-charge", msg)
        self.assertTrue(foxctl._EV["on"])


class PlannerTest(unittest.TestCase):
    """Phase 4 shadow planner: requirement-aware ideal SoC trajectory (no control side-effects)."""

    def _params(self, **kw):
        p = {"reserve": 20, "max_soc": 90, "survival": 30, "charge_start": 0.12,
             "sell_thr": 0.50, "sell_on": True, "charge_kw": 10.0, "eff": 1.0}
        p.update(kw)
        return p

    def test_charges_in_cheap_slot_when_below_requirement(self):
        # SoC (30%≈9kWh) is below what the coming expensive 5kWh deficit needs → charge in the cheap slot
        slots = [{"h": 0.0, "price": 0.10, "dt": 1.0, "load": 1.0, "solar": 0.0},
                 {"h": 1.0, "price": 0.40, "dt": 1.0, "load": 5.0, "solar": 0.0}]
        plan = foxctl.plan_soc_trajectory(slots, 30.0, 30.0, self._params())
        self.assertEqual(plan["action_now"], "charge")
        self.assertGreater(plan["target_now"], 30.0)                  # SoC rose in the cheap slot
        self.assertEqual(len(plan["soc_line"]), 2)

    def test_no_charge_when_already_above_requirement(self):
        # plenty of SoC for the coming demand → the planner should NOT buy energy it doesn't need
        slots = [{"h": 0.0, "price": 0.10, "dt": 1.0, "load": 1.0, "solar": 0.0},
                 {"h": 1.0, "price": 0.40, "dt": 1.0, "load": 5.0, "solar": 0.0}]
        plan = foxctl.plan_soc_trajectory(slots, 80.0, 30.0, self._params())
        self.assertNotEqual(plan["action_now"], "charge")

    def test_floor_envelope_rises_before_expensive_demand(self):
        # the min-SoC envelope ENTERING the dear slot must reflect its net-load (above bare reserve)
        slots = [{"h": 0.0, "price": 0.10, "dt": 1.0, "load": 0.0, "solar": 0.0},
                 {"h": 1.0, "price": 0.40, "dt": 1.0, "load": 6.0, "solar": 0.0}]
        plan = foxctl.plan_soc_trajectory(slots, 60.0, 30.0, self._params())
        self.assertEqual(plan["floor_line"][1][1], 40.0)   # reserve 20% + 6kWh/30kWh = 40% entering dear slot
        self.assertGreaterEqual(plan["floor_line"][0][1], 20.0)

    def test_never_below_reserve_or_above_max(self):
        slots = [{"h": i * 0.5, "price": 0.45, "dt": 0.5, "load": 4.0, "solar": 0.0} for i in range(12)]
        plan = foxctl.plan_soc_trajectory(slots, 35.0, 10.0, self._params())
        socs = [s for _, s in plan["soc_line"]]
        self.assertGreaterEqual(min(socs), 20.0 - 1e-6)   # reserve
        self.assertLessEqual(max(socs), 90.0 + 1e-6)      # max_soc

    def test_arbitrage_fills_for_future_sell(self):
        # cheap now (0.10), a sell-window later (0.60 ≥ sell_thr 0.50); SoC already covers all *load*
        # requirements → requirement-only would hold, but arbitrage should charge to sell into the spike.
        slots = [{"h": 0.0, "price": 0.10, "dt": 1.0, "load": 0.0, "solar": 0.0},
                 {"h": 1.0, "price": 0.60, "dt": 1.0, "load": 0.0, "solar": 0.0}]
        no_arb = foxctl.plan_soc_trajectory(slots, 50.0, 30.0, self._params(arbitrage=False))
        self.assertEqual(no_arb["action_now"], "hold")            # no load requirement → no charge
        arb = foxctl.plan_soc_trajectory(slots, 50.0, 30.0, self._params(arbitrage=True))
        self.assertEqual(arb["action_now"], "charge")            # buys cheap to sell into the spike
        self.assertGreater(arb["target_now"], 50.0)

    def test_uses_per_slot_feed_in_forecast_for_sell(self):
        # buy price is low everywhere (no buy-proxy sell), but the feed-in forecast spikes in slot 1 →
        # the planner should still see a sell opportunity from the real export price.
        slots = [{"h": 0.0, "price": 0.10, "dt": 1.0, "load": 0.0, "solar": 0.0, "sell_price": 0.05},
                 {"h": 1.0, "price": 0.10, "dt": 1.0, "load": 0.0, "solar": 0.0, "sell_price": 0.60}]
        plan = foxctl.plan_soc_trajectory(slots, 50.0, 30.0, self._params())   # sell_thr 0.50
        # arb target should pull the cheap first slot to charge toward the future export window
        self.assertEqual(plan["action_now"], "charge")

    def test_no_arbitrage_when_spread_unprofitable(self):
        # future "sell" price below buy-after-efficiency → not worth pre-buying
        slots = [{"h": 0.0, "price": 0.10, "dt": 1.0, "load": 0.0, "solar": 0.0},
                 {"h": 1.0, "price": 0.60, "dt": 1.0, "load": 0.0, "solar": 0.0}]
        # sell threshold above the spike price → no sellable window ahead → no arb fill
        plan = foxctl.plan_soc_trajectory(slots, 50.0, 30.0, self._params(sell_thr=0.80))
        self.assertEqual(plan["action_now"], "hold")

    def test_empty_horizon_safe(self):
        plan = foxctl.plan_soc_trajectory([], 55.0, 30.0, self._params())
        self.assertEqual(plan["soc_line"], [])
        self.assertEqual(plan["action_now"], "hold")
        self.assertEqual(plan["target_now"], 55.0)


class PersistentChatTest(unittest.TestCase):
    """The mission-anchored strategist conversation: history hygiene, pruning, persistence, fallback."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = {"state_dir": self.tmp, "llm": {"enabled": True, "api_key": "k",
                    "model": "claude-opus-4-8", "fallback_model": "claude-haiku-4-5"}}
        foxctl._CHAT["loaded"] = False
        foxctl._CHAT["msgs"] = []

    def test_api_messages_merges_and_strips_leading_assistant(self):
        msgs = [{"role": "assistant", "content": "stale"},   # dropped (must start with user)
                {"role": "user", "content": "a"},
                {"role": "user", "content": "b"},             # merged into one user turn
                {"role": "assistant", "content": "c"}]
        out = foxctl._api_messages(msgs)
        self.assertEqual([m["role"] for m in out], ["user", "assistant"])
        self.assertEqual(out[0]["content"], "a\n\nb")

    def test_supports_adaptive(self):
        self.assertTrue(foxctl._supports_adaptive("claude-opus-4-8"))
        self.assertTrue(foxctl._supports_adaptive("claude-sonnet-4-6"))
        self.assertFalse(foxctl._supports_adaptive("claude-haiku-4-5"))

    def test_retries_primary_on_529_then_succeeds(self):
        # a 529 (Overloaded) is transient — retry the SAME model, don't immediately drop to fallback.
        calls = {"n": 0}

        def flaky(api_key, model, mission, messages, max_tokens, timeout=60):
            calls["n"] += 1
            if model == "claude-opus-4-8" and calls["n"] == 1:
                raise urllib.error.HTTPError("u", 529, "Overloaded", {}, None)
            return "ok", {}
        orig, slept = foxctl._llm_post, []
        foxctl._llm_post = flaky
        _sleep = foxctl.time.sleep
        foxctl.time.sleep = lambda s: slept.append(s)
        try:
            text, used, _ = foxctl._llm_call("k", "claude-opus-4-8", "claude-haiku-4-5", "m", [], retries=3)
        finally:
            foxctl._llm_post = orig
            foxctl.time.sleep = _sleep
        self.assertEqual((text, used), ("ok", "claude-opus-4-8"))   # primary succeeded on retry
        self.assertTrue(slept)                                       # backoff happened

    def test_falls_back_after_exhausting_retries(self):
        def always529(api_key, model, mission, messages, max_tokens, timeout=60):
            if model == "claude-opus-4-8":
                raise urllib.error.HTTPError("u", 529, "Overloaded", {}, None)
            return "fallback-answer", {}
        orig, _sleep = foxctl._llm_post, foxctl.time.sleep
        foxctl._llm_post = always529
        foxctl.time.sleep = lambda s: None
        try:
            text, used, _ = foxctl._llm_call("k", "claude-opus-4-8", "claude-haiku-4-5", "m", [], retries=2)
        finally:
            foxctl._llm_post = orig
            foxctl.time.sleep = _sleep
        self.assertEqual((text, used), ("fallback-answer", "claude-haiku-4-5"))

    def test_permanent_error_does_not_retry(self):
        # a 404 (bad model / no access) is permanent — straight to fallback, no backoff sleeps.
        calls = {"opus": 0}

        def bad(api_key, model, mission, messages, max_tokens, timeout=60):
            if model == "claude-opus-4-8":
                calls["opus"] += 1
                raise urllib.error.HTTPError("u", 404, "not found", {}, None)
            return "fb", {}
        orig, _sleep, slept = foxctl._llm_post, foxctl.time.sleep, []
        foxctl._llm_post = bad
        foxctl.time.sleep = lambda s: slept.append(s)
        try:
            _, used, _ = foxctl._llm_call("k", "claude-opus-4-8", "claude-haiku-4-5", "m", [], retries=3)
        finally:
            foxctl._llm_post = orig
            foxctl.time.sleep = _sleep
        self.assertEqual(used, "claude-haiku-4-5")
        self.assertEqual(calls["opus"], 1)     # tried Opus once, no retries
        self.assertEqual(slept, [])            # no backoff for a permanent error

    def test_prune_keeps_chat_tail_and_latest_policy_only(self):
        foxctl._CHAT["loaded"] = True
        msgs = []
        # two old policy exchanges + a chat exchange; only the most recent policy pair should survive
        for i in range(2):
            msgs.append({"role": "user", "content": f"POLICY {i}", "kind": "policy", "ts": f"t{i}u"})
            msgs.append({"role": "assistant", "content": f"{{}} {i}", "kind": "policy", "ts": f"t{i}a"})
        msgs.append({"role": "user", "content": "hi", "kind": "chat", "ts": "tcu"})
        msgs.append({"role": "assistant", "content": "hello", "kind": "chat", "ts": "tca"})
        foxctl._CHAT["msgs"] = msgs
        foxctl._prune_chat()
        kept = foxctl._CHAT["msgs"]
        policy = [m for m in kept if m["kind"] == "policy"]
        chat = [m for m in kept if m["kind"] == "chat"]
        self.assertEqual(len(policy), foxctl.CHAT_KEEP_POLICY)
        self.assertEqual(policy[-1]["content"], "{} 1")        # newest policy exchange retained
        self.assertEqual(len(chat), 2)                          # chat dialogue retained
        # insertion order preserved: the surviving policy pair, then the chat pair
        self.assertEqual([m["content"] for m in kept], ["POLICY 1", "{} 1", "hi", "hello"])

    def test_persistence_roundtrip(self):
        foxctl._CHAT["loaded"] = True
        foxctl._chat_add("user", "remember the mission", "chat")
        foxctl._chat_add("assistant", "noted", "chat")
        foxctl.save_chat(self.cfg)
        foxctl._CHAT["loaded"] = False
        foxctl._CHAT["msgs"] = []
        loaded = foxctl.load_chat(self.cfg)
        self.assertEqual([m["content"] for m in loaded], ["remember the mission", "noted"])

    def test_dynamic_records_exchange_and_uses_fallback(self):
        calls = {"n": 0, "models": []}

        def fake_post(api_key, model, mission, messages, max_tokens, timeout=60):
            calls["n"] += 1
            calls["models"].append(model)
            if model == "claude-opus-4-8":
                raise RuntimeError("overloaded")               # primary fails → fallback
            return ('{"target_soc": 70, "spike_sell_buffer_kwh": 4, "buy_bar_cap": 0.18, '
                    '"rating": "REFINE", "reason": "buffer for tonight\'s spike", '
                    '"operator_action": "", "base_floor": null}'), {}

        orig = foxctl._llm_post
        foxctl._llm_post = fake_post
        try:
            v = foxctl._llm_dynamic(self.cfg, "k", "claude-opus-4-8", "claude-haiku-4-5",
                                    {"soc": 50})
        finally:
            foxctl._llm_post = orig
        self.assertEqual(calls["models"], ["claude-opus-4-8", "claude-haiku-4-5"])
        self.assertEqual(v["model"], "claude-haiku-4-5")        # reports the model that actually answered
        self.assertEqual(v["params"]["target_soc"], 70)
        self.assertEqual(v["params"]["spike_sell_buffer_kwh"], 4.0)
        self.assertEqual(v["params"]["buy_bar_cap"], 0.18)
        # the exchange is now in the persistent history (collapsed policy user + assistant reply)
        kinds = [(m["role"], m["kind"]) for m in foxctl._CHAT["msgs"]]
        self.assertIn(("user", "policy"), kinds)
        self.assertIn(("assistant", "policy"), kinds)

    def test_failed_call_leaves_history_untouched(self):
        foxctl._CHAT["loaded"] = True

        def boom(*a, **k):
            raise RuntimeError("down")
        orig = foxctl._llm_post
        foxctl._llm_post = boom
        try:
            with self.assertRaises(RuntimeError):
                # same model as fallback → no second attempt, error propagates
                foxctl._llm_dynamic(self.cfg, "k", "claude-haiku-4-5", "claude-haiku-4-5", {"soc": 1})
        finally:
            foxctl._llm_post = orig
        self.assertEqual(foxctl._CHAT["msgs"], [])              # nothing committed on failure

    def test_chat_reply_records_and_returns(self):
        def fake_post(api_key, model, mission, messages, max_tokens, timeout=60):
            return "I'll hold the battery until the midday trough.", {}
        orig = foxctl._llm_post
        foxctl._llm_post = fake_post
        try:
            res = foxctl.llm_chat_reply(self.cfg, "why aren't you charging now?")
        finally:
            foxctl._llm_post = orig
        self.assertIn("midday trough", res["reply"])
        self.assertEqual([(m["role"], m["kind"]) for m in foxctl._CHAT["msgs"]],
                         [("user", "chat"), ("assistant", "chat")])

    def test_chat_reply_disabled_without_key(self):
        res = foxctl.llm_chat_reply({"llm": {"enabled": False}}, "hi")
        self.assertIn("error", res)

    def test_clear_chat_wipes_history(self):
        foxctl._CHAT["loaded"] = True
        foxctl._chat_add("user", "old confused thread", "chat")
        foxctl.save_chat(self.cfg)
        res = foxctl.clear_chat(self.cfg)
        self.assertTrue(res["cleared"])
        self.assertEqual(foxctl._CHAT["msgs"], [])
        self.assertEqual(foxctl.load_chat(self.cfg), [])      # persisted wipe survives a reload


class StrategistMissionContextTest(unittest.TestCase):
    """The mission + context must tell the LLM the controller auto-sells, so it stops recommending
    manual exporting during spikes."""

    def test_mission_describes_auto_sell_and_boundary(self):
        m = foxctl.MISSION.lower()
        self.assertIn("auto-sell", m)
        self.assertIn("never tell the operator", m)   # forbids the exact field-report mistake
        self.assertIn("charge_start_price", m)
        self.assertIn("target_soc", m)


class InverterMinSocSafetyTest(unittest.TestCase):
    """Phase 1 safety: foxctl must NEVER push a computed survival level onto the inverter as its
    min-SoG (the 66% import bug). The only min-SoC ever written is the constant inverter_min_soc."""

    class _FakeFox:
        """Captures every scheduler write so tests can assert the min-SoC argument."""
        instances = []

        def __init__(self, *a, **k):
            self.fc = []      # force-charge calls
            self.fd = []      # force-discharge calls
            self.disabled = 0
            self.wm = []
            InverterMinSocSafetyTest._FakeFox.instances.append(self)

        def enable_force_charge(self, s, e, min_soc, cap, pwr):
            self.fc.append({"min_soc": min_soc, "cap": cap})

        def enable_force_discharge(self, s, e, min_soc, pwr):
            self.fd.append({"min_soc": min_soc})

        def disable_scheduler(self):
            self.disabled += 1

        def set_work_mode(self, m):
            self.wm.append(m)

    def setUp(self):
        self._orig = foxctl.FoxESS
        foxctl.FoxESS = self._FakeFox
        self._FakeFox.instances = []
        self.cfg = {"control": {"allow_control": True, "auto_apply": True, "set_work_mode": True,
                                "set_force_charge": True},
                    "foxess": {"token": "t", "sn": "s"},
                    "strategy": copy.deepcopy(foxctl.DEFAULT_CONFIG["strategy"])}
        self.cfg["strategy"]["inverter_min_soc"] = 10

    def tearDown(self):
        foxctl.FoxESS = self._orig

    def _snap(self, rec):
        return {"recommendation": rec, "telemetry_source": "FoxESS", "work_mode": "SelfUse",
                "scheduler": {"enabled": False, "active": None}, "dynamic": {"target_soc": 90},
                "soc": 80, "feedin": 0.9}

    def test_auto_sell_writes_constant_floor_not_survival(self):
        # survival floor of 66% must NOT reach the inverter — only inverter_min_soc (10) does.
        rec = {"action": "SELL", "force_charge": False, "force_discharge": True, "sell_floor": 66,
               "target_mode": "SelfUse", "band": "spike"}
        foxctl.apply_recommendation(self.cfg, self._snap(rec))
        fd = self._FakeFox.instances[0].fd
        self.assertEqual(len(fd), 1)
        self.assertEqual(fd[0]["min_soc"], 10)        # constant floor, NOT 66

    def test_force_charge_writes_constant_floor(self):
        rec = {"action": "FORCE_CHARGE", "force_charge": True, "force_discharge": False,
               "target_mode": "SelfUse", "band": "ludicrous"}
        foxctl.apply_recommendation(self.cfg, self._snap(rec))
        fc = self._FakeFox.instances[0].fc
        self.assertEqual(fc[0]["min_soc"], 10)

    def test_force_charge_test_uses_constant_floor(self):
        foxctl.force_charge_test(self.cfg, 10)
        self.assertEqual(self._FakeFox.instances[0].fc[0]["min_soc"], 10)

    def test_get_min_soc_parses_value(self):
        fox = self._orig("t", "s")
        fox.call = lambda path, body=None: {"result": {"value": "66"}}
        self.assertEqual(fox.get_min_soc(), 66)

    def test_get_min_soc_none_on_error(self):
        fox = self._orig("t", "s")
        def boom(path, body=None):
            raise RuntimeError("no such key")
        fox.call = boom
        self.assertIsNone(fox.get_min_soc())


class ManualSellSoftwareFloorTest(unittest.TestCase):
    """Manual SELL stops in software at the requested floor, instead of pushing it to the device."""

    def setUp(self):
        self._orig = foxctl.FoxESS
        foxctl.FoxESS = InverterMinSocSafetyTest._FakeFox
        InverterMinSocSafetyTest._FakeFox.instances = []
        foxctl._OV["manual"] = {"mode": "sell", "until": __import__("time").time() + 3600,
                                "power": 10.5, "min_soc": 20, "cap": None}
        foxctl._OV["loaded"] = True
        self.cfg = {"control": {"allow_control": True}, "foxess": {"token": "t", "sn": "s"},
                    "strategy": {"inverter_min_soc": 10}, "state_dir": tempfile.mkdtemp()}

    def tearDown(self):
        foxctl.FoxESS = self._orig
        foxctl._OV["manual"] = None

    def test_stops_when_floor_reached(self):
        snap = {"soc": 20, "scheduler": {"active": None}}     # at the 20% manual floor
        out = foxctl.manual_tick(self.cfg, snap)
        self.assertIsNone(out)                                # reverted to auto
        self.assertIsNone(foxctl._OV["manual"])               # override cleared
        self.assertEqual(InverterMinSocSafetyTest._FakeFox.instances[0].disabled, 1)

    def test_runs_while_above_floor_with_constant_device_floor(self):
        snap = {"soc": 50, "scheduler": {"active": None}}     # well above floor → discharge
        foxctl.manual_tick(self.cfg, snap)
        fd = InverterMinSocSafetyTest._FakeFox.instances[0].fd
        self.assertEqual(fd[0]["min_soc"], 10)                # device floor is the constant, not 20


class SellOverridePersistenceTest(unittest.TestCase):
    """The sell threshold must round-trip AND appear in the snapshot so the form shows the saved value."""

    def setUp(self):
        self.cfg = {"state_dir": tempfile.mkdtemp(), "strategy": {"price_ceiling": 0.20, "sell_price": 0.50}}
        foxctl._OV.update({"floor": None, "sell": None, "manual": None, "loaded": True})

    def test_set_baseline_persists_sell(self):
        foxctl.set_baseline(self.cfg, None, 0.65, 0.20)
        self.assertEqual(foxctl._OV["sell"], 0.65)
        foxctl._OV["loaded"] = False                          # force a reload from disk
        foxctl.load_ov(self.cfg)
        self.assertEqual(foxctl._OV["sell"], 0.65)

    def test_render_form_shows_sell_override(self):
        # the baseline form reads snap["override"]["sell"]; with a saved override of 0.65 the form
        # input must show 0.65, not the config default 0.50 (the "sell value not sticking" bug).
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        fc = [{"t": (now + timedelta(hours=i * 0.5)).isoformat(), "price": 0.15, "descriptor": "x"}
              for i in range(12)]
        snap = {"ts": "t", "price": 0.2, "aemo_price": 0.07, "feedin": 0.06, "soc": 50.0, "pv_kw": 0.0,
                "ev_kw": None, "load_kw": 3.1, "forecast_next": fc, "forecast_h": fc, "aemo_forecast_h": [],
                "recommendation": {"action": "SET_MODE", "target_mode": "SelfUse", "reason": "x",
                                   "band": "normal", "force_charge": False},
                "dynamic": {"charge_start_price": 0.12, "price_ceiling": 0.2, "target_soc": 90, "max_soc": 90,
                            "survival_soc": 30, "sell_price": 0.65, "sell_enabled": True, "source": "LLM",
                            "mode": "amber"},
                "battery": {"capacity_kwh": 30.0, "stored_kwh": 15.0},
                "consumption": {"avg_daily_total_kwh": 33.0, "days_sampled": 5,
                                "hour_profile": {h: 1.0 for h in range(24)}},
                "forecast_profiles": {"days": 5, "days_solar": 5},
                "solar_forecast": {"today_total": 7, "remaining_today": 1, "tomorrow": 15},
                "solar_cal": {"bias": 1.0, "applied": False, "samples": 2}, "solar_bells": [],
                "plan": {"action_now": "hold", "target_now": 50.0, "soc_line": [], "floor_line": []},
                "scheduler": {"enabled": False, "active": None}, "applied": "x", "llm": None,
                "override": {"floor": None, "sell": 0.65, "manual": None}}
        cfg = {"control": {"allow_control": True, "auto_apply": True, "set_force_charge": True},
               "strategy": {"force_charge_power_kw": 10.5, "target_soc": 90, "charge_start_floor": 0.10,
                            "sell_price": 0.50}, "ev_divert": {"switch": ""}}
        html = foxctl.render(snap, cfg)
        self.assertIn('value="0.65"', html)         # the saved override, not the 0.50 default


if __name__ == "__main__":
    unittest.main()
