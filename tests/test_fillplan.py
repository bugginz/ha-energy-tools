"""Tests for the free-window fill planner (spec 2026-07-31).

Stdlib unittest only (no pytest), same as tests/test_foxctl.py:

    python3 -m unittest tests.test_fillplan -v
"""

import os
import sys
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "energy_tools"))
import fillplan  # noqa: E402


class SolarBetweenTest(unittest.TestCase):
    """Half-sine daylight model integrated over a sub-interval."""

    def setUp(self):
        # Sunset 17:00, 10h daylight window => 07:00-17:00, 20 kWh forecast.
        self.sunset = datetime(2026, 7, 31, 17, 0)
        self.kwh = 20.0

    def test_whole_window_equals_forecast(self):
        got = fillplan.solar_kwh_between(self.sunset, self.kwh,
                                         self.sunset - timedelta(hours=10), self.sunset)
        self.assertAlmostEqual(got, 20.0, places=6)

    def test_halves_are_symmetric(self):
        noon = self.sunset - timedelta(hours=5)
        first = fillplan.solar_kwh_between(self.sunset, self.kwh,
                                           self.sunset - timedelta(hours=10), noon)
        second = fillplan.solar_kwh_between(self.sunset, self.kwh, noon, self.sunset)
        self.assertAlmostEqual(first, second, places=6)
        self.assertAlmostEqual(first, 10.0, places=6)

    def test_after_deadline_slice_is_less_than_half(self):
        # 14:00 -> sunset is the last 3h of a 10h day: the tail of the bell.
        got = fillplan.solar_kwh_between(self.sunset, self.kwh,
                                         datetime(2026, 7, 31, 14, 0), self.sunset)
        self.assertLess(got, 10.0)
        self.assertGreater(got, 0.0)

    def test_interval_after_sunset_is_zero(self):
        got = fillplan.solar_kwh_between(self.sunset, self.kwh,
                                         self.sunset, self.sunset + timedelta(hours=2))
        self.assertEqual(got, 0.0)

    def test_interval_before_sunrise_is_zero(self):
        start = self.sunset - timedelta(hours=14)
        got = fillplan.solar_kwh_between(self.sunset, self.kwh, start,
                                         start + timedelta(hours=2))
        self.assertEqual(got, 0.0)

    def test_clamps_to_window_edges(self):
        # Asking for a span wider than the daylight window still yields the total, not more.
        got = fillplan.solar_kwh_between(self.sunset, self.kwh,
                                         self.sunset - timedelta(hours=24),
                                         self.sunset + timedelta(hours=5))
        self.assertAlmostEqual(got, 20.0, places=6)

    def test_no_forecast_is_zero(self):
        self.assertEqual(fillplan.solar_kwh_between(self.sunset, None,
                                                    self.sunset - timedelta(hours=3),
                                                    self.sunset), 0.0)

    def test_missing_sunset_is_zero(self):
        self.assertEqual(fillplan.solar_kwh_between(None, 20.0,
                                                    datetime(2026, 7, 31, 12, 0),
                                                    datetime(2026, 7, 31, 14, 0)), 0.0)


def plan(**kw):
    """plan_fill with the live 2026-07-31 site defaults; override per test."""
    args = dict(soc=49.0, capacity_kwh=41.44, now_h=11.083, deadline_h=14.0,
                deadline_soc=90.0, max_soc_cap=100.0, solar_after_deadline_kwh=6.0,
                house_kw=2.2, car_kw_est=3.4, supply_cap_kw=14.5, max_fill_kw=10.5,
                min_fill_kw=1.0, guard_kw=0.8, trim_kw=0.0, charge_eff=0.95, margin=1.1)
    args.update(kw)
    return fillplan.plan_fill(**args)


class PlanFillTest(unittest.TestCase):

    def test_live_case_leaves_room_for_the_car(self):
        # The 2026-07-31 11:05 situation that flapped: 49% SoC, A/C running, car wants 3.4 kW.
        p = plan()
        self.assertTrue(p["car_ok"])
        self.assertAlmostEqual(p["target_soc"], 90.0, places=6)
        # Battery + house + car + guard must fit under the supply cap.
        self.assertLessEqual(p["fill_kw"] + 2.2 + 3.4, 14.5 - 0.8 + 1e-9)

    def test_live_case_still_meets_the_deadline(self):
        p = plan()
        delivered = p["fill_kw"] * (14.0 - 11.083) * 0.95
        self.assertGreaterEqual(delivered, p["deficit_kwh"])

    def test_fill_never_below_the_deadline_need(self):
        p = plan()
        self.assertGreaterEqual(p["fill_kw"], p["must_kw"] - 1e-9)

    def test_battery_far_behind_locks_the_car_out(self):
        # 20% at 13:00 with one hour left: the battery needs everything.
        p = plan(soc=20.0, now_h=13.0)
        self.assertFalse(p["car_ok"])
        self.assertAlmostEqual(p["fill_kw"], 10.5, places=6)

    def test_battery_at_target_frees_the_whole_remainder(self):
        p = plan(soc=95.0)
        self.assertEqual(p["must_kw"], 0.0)
        self.assertTrue(p["car_ok"])
        # Battery absorbs what the car and house do not take — free energy is not wasted.
        self.assertGreater(p["fill_kw"], 0.0)
        self.assertLessEqual(p["fill_kw"] + 2.2 + 3.4, 14.5 - 0.8 + 1e-9)

    def test_poor_afternoon_solar_raises_the_deadline_target(self):
        p = plan(solar_after_deadline_kwh=1.0)
        self.assertGreater(p["target_soc"], 90.0)
        self.assertLessEqual(p["target_soc"], 100.0)

    def test_good_afternoon_solar_keeps_the_target_at_90(self):
        p = plan(solar_after_deadline_kwh=12.0)
        self.assertAlmostEqual(p["target_soc"], 90.0, places=6)

    def test_unknown_solar_leaves_the_target_alone(self):
        # None means "no sunset/forecast reading", NOT "zero solar". Treating a hiccup in
        # sun.sun as zero would raise the target to 100% and lock the car out all window.
        unknown = plan(solar_after_deadline_kwh=None)
        known_zero = plan(solar_after_deadline_kwh=0.0)
        self.assertAlmostEqual(unknown["target_soc"], 90.0, places=6)
        self.assertGreater(known_zero["target_soc"], 90.0)
        self.assertTrue(unknown["car_ok"])

    def test_target_never_exceeds_the_soc_cap(self):
        p = plan(solar_after_deadline_kwh=0.0, max_soc_cap=95.0)
        self.assertLessEqual(p["target_soc"], 95.0)

    def test_fill_is_clamped_to_the_inverter_maximum(self):
        p = plan(soc=5.0, now_h=13.9, house_kw=0.0)
        self.assertLessEqual(p["fill_kw"], 10.5)

    def test_fill_has_a_floor(self):
        # Huge house load: spare goes negative, but a ForceCharge group below min_fill is pointless.
        p = plan(soc=95.0, house_kw=14.0)
        self.assertGreaterEqual(p["fill_kw"], 1.0)

    def test_trim_reduces_the_planned_total_draw(self):
        # Trim backs off the whole plan, not the fill specifically: excluding the car and
        # letting the battery take the remainder is a legitimate way to get under the cap.
        base = plan()
        trimmed = plan(trim_kw=2.0)
        self.assertLess(trimmed["total_kw"], base["total_kw"])

    def test_planned_total_always_respects_cap_guard_and_trim(self):
        for kw in (0.0, 1.0, 2.0, 4.0):
            for house in (0.2, 2.2, 5.0):
                p = plan(house_kw=house, trim_kw=kw)
                if p["must_kw"] < p["spare_kw"]:      # not forced up by the deadline need
                    self.assertLessEqual(p["total_kw"], 14.5 - 0.8 - kw + 1e-9,
                                         f"trim={kw} house={house} -> {p}")

    def test_bigger_house_load_shrinks_the_fill_not_the_car(self):
        quiet = plan(house_kw=0.5)
        busy = plan(house_kw=2.5)
        self.assertLess(busy["fill_kw"], quiet["fill_kw"])
        self.assertTrue(busy["car_ok"])

    def test_past_the_deadline_does_not_divide_by_zero(self):
        p = plan(now_h=14.0)
        self.assertGreater(p["hours_left"], 0.0)
        self.assertLessEqual(p["fill_kw"], 10.5)

    def test_no_soc_reading_falls_back_to_full_power(self):
        p = plan(soc=None)
        self.assertAlmostEqual(p["fill_kw"], 10.5, places=6)
        self.assertFalse(p["car_ok"])


class FlapRegressionTest(unittest.TestCase):
    """The 2026-07-31 failure: a house-load spike between planner cycles must not
    produce car on -> fast-guard off -> car on."""

    def test_house_spike_does_not_flip_the_car_off(self):
        before = plan(house_kw=0.2)          # 10:01, A/C idle
        self.assertTrue(before["car_ok"])
        after = plan(house_kw=2.2)           # 10:51, A/C running
        self.assertTrue(after["car_ok"])     # still allowed...
        # ...because the fill dropped to absorb the spike, keeping the total under the cap.
        self.assertLess(after["fill_kw"], before["fill_kw"])
        self.assertLessEqual(after["fill_kw"] + 2.2 + 3.4, 14.5 - 0.8 + 1e-9)

    def test_the_old_fixed_105kw_fill_would_have_breached_the_cap(self):
        # Documents the bug: 10.5 + house 2.2 + car 3.4 = 16.1 kW against a 14.5 kW cap.
        self.assertGreater(10.5 + 2.2 + 3.4, 14.5)


class WeeklyDeadlineTest(unittest.TestCase):

    def test_hours_until_next_saturday_morning(self):
        friday_11am = datetime(2026, 7, 31, 11, 0)      # Friday
        self.assertAlmostEqual(fillplan.hours_until_weekly(friday_11am, 5, 8), 21.0, places=6)

    def test_same_day_before_the_hour(self):
        saturday_6am = datetime(2026, 8, 1, 6, 0)
        self.assertAlmostEqual(fillplan.hours_until_weekly(saturday_6am, 5, 8), 2.0, places=6)

    def test_same_day_after_the_hour_rolls_a_week(self):
        saturday_9am = datetime(2026, 8, 1, 9, 0)
        self.assertAlmostEqual(fillplan.hours_until_weekly(saturday_9am, 5, 8), 167.0, places=6)


class FreeWindowHoursTest(unittest.TestCase):

    def test_remaining_window_today_only(self):
        # Friday 11:05, deadline Saturday 08:00: only today's 11:05-14:00 counts.
        now = datetime(2026, 7, 31, 11, 5)
        deadline = datetime(2026, 8, 1, 8, 0)
        got = fillplan.free_window_hours_before(now, deadline, 10.0, 14.0)
        self.assertAlmostEqual(got, 2.9167, places=3)

    def test_two_windows_when_the_deadline_is_further_out(self):
        now = datetime(2026, 7, 31, 11, 5)
        deadline = datetime(2026, 8, 2, 8, 0)          # Sunday morning: today's tail + Saturday
        got = fillplan.free_window_hours_before(now, deadline, 10.0, 14.0)
        self.assertAlmostEqual(got, 2.9167 + 4.0, places=3)

    def test_outside_the_window_counts_nothing_today(self):
        now = datetime(2026, 7, 31, 15, 0)
        deadline = datetime(2026, 8, 1, 8, 0)
        self.assertEqual(fillplan.free_window_hours_before(now, deadline, 10.0, 14.0), 0.0)

    def test_deadline_in_the_past_is_zero(self):
        now = datetime(2026, 7, 31, 11, 5)
        self.assertEqual(fillplan.free_window_hours_before(now, datetime(2026, 7, 30, 8, 0),
                                                           10.0, 14.0), 0.0)


class CarDeadlineStatusTest(unittest.TestCase):

    def test_behind_when_there_is_not_enough_window_left(self):
        # 58.4% -> 80% of a 42 kWh pack is ~9.1 kWh; at 3.4 kW that needs ~3h.
        s = fillplan.car_deadline_status(58.4, 42.0, 80.0, chargeable_hours=2.0,
                                         charge_kw=3.4, eff=0.9)
        self.assertFalse(s["on_track"])
        self.assertAlmostEqual(s["kwh_needed"], 9.072, places=3)
        self.assertGreater(s["hours_needed"], 2.0)

    def test_on_track_with_a_full_window(self):
        s = fillplan.car_deadline_status(58.4, 42.0, 80.0, chargeable_hours=4.0,
                                         charge_kw=3.4, eff=0.9)
        self.assertTrue(s["on_track"])

    def test_already_at_target_needs_nothing(self):
        s = fillplan.car_deadline_status(85.0, 42.0, 80.0, chargeable_hours=0.0,
                                         charge_kw=3.4, eff=0.9)
        self.assertEqual(s["kwh_needed"], 0.0)
        self.assertTrue(s["on_track"])

    def test_unknown_soc_is_unknown_not_false(self):
        s = fillplan.car_deadline_status(None, 42.0, 80.0, chargeable_hours=4.0,
                                         charge_kw=3.4, eff=0.9)
        self.assertIsNone(s["on_track"])


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------- integration --
# The pure planner above is only half the fix; these exercise the foxctl wiring that
# actually writes the scheduler and gates the car.

from unittest import mock  # noqa: E402

import foxctl  # noqa: E402


class FakeFox:
    """Records scheduler writes instead of calling the FoxESS cloud."""

    sn = "TESTSN"

    def __init__(self):
        self.writes = []

    def call(self, path, body):
        self.writes.append((path, body))
        return {"errno": 0}


def tick_cfg():
    return {"strategy": {"fill_planner": True, "fill_deadline_soc": 90, "fill_deadline_hour": 14,
                         "battery_capacity_kwh": 41.44, "force_charge_power_kw": 10.5,
                         "fill_min_kw": 1.0, "fill_margin": 1.1, "fill_charge_eff": 0.95,
                         "fill_power_step_kw": 1.0, "fill_rewrite_gap_s": 240,
                         "charge_target_soc": 100, "max_soc": 100},
            "ev_divert": {"supply_cap_kw": 14.5, "headroom_guard_kw": 0.8, "guard_cooloff_min": 3},
            "control": {"allow_control": True, "set_force_charge": True}}


def tick_snap(soc=49.0, load_kw=2.2, ev_kw=0.0, fd_pwr=10500, read_ok=True, grid_live=None):
    base = {"startHour": 10, "startMinute": 0, "endHour": 14, "endMinute": 0,
            "workMode": "ForceCharge", "minSocOnGrid": 10, "fdSoc": 100,
            "fdPwr": fd_pwr, "enable": 1}
    return {"soc": soc, "load_kw": load_kw, "ev_kw": ev_kw, "grid_power_live": grid_live,
            "dynamic": {"tariff": {"free": {"start": 10, "end": 14}}},
            "solar_forecast": {"remaining_today": 12.0},
            "sun": {"set": "2026-07-31T17:00:00+10:00"},
            "car": {"sessions": [{"peak_kw": 3.4}]},
            "scheduler": {"read_ok": read_ok, "groups": [base]}}


def _frozen(hour, minute=5):
    """Freeze foxctl's clock inside the free window."""
    real = foxctl.datetime

    class FrozenDT(real):
        @classmethod
        def now(cls, tz=None):
            return real(2026, 7, 31, hour, minute, tzinfo=tz)
    return mock.patch.object(foxctl, "datetime", FrozenDT)


class FillTickTest(unittest.TestCase):

    def setUp(self):
        foxctl._FILL.update({"plan": None, "last_write": 0.0, "written_kw": None, "trim_kw": 0.0})
        foxctl._SCHED.update({"loaded": True, "mine_key": None, "user_groups": []})
        self._save, self._log = foxctl._sched_save, foxctl.log_event
        foxctl._sched_save = lambda cfg: None
        foxctl.log_event = lambda *a, **k: None

    def tearDown(self):
        foxctl._sched_save, foxctl.log_event = self._save, self._log

    def test_steps_the_base_group_power_down_to_make_room_for_the_car(self):
        fox = FakeFox()
        with _frozen(11):
            msg = foxctl.free_window_fill_tick(tick_cfg(), fox, tick_snap())
        self.assertIsNotNone(msg)
        self.assertEqual(len(fox.writes), 1)
        groups = fox.writes[0][1]["groups"]
        self.assertEqual(len(groups), 1)
        written_kw = groups[0]["fdPwr"] / 1000.0
        self.assertLess(written_kw, 10.5)
        self.assertLessEqual(written_kw + 2.2 + 3.4, 14.5 - 0.8 + 1e-9)
        # The window, mode and SoC cap belong to the user — only power may change.
        self.assertEqual(groups[0]["startHour"], 10)
        self.assertEqual(groups[0]["endHour"], 14)
        self.assertEqual(groups[0]["workMode"], "ForceCharge")
        self.assertEqual(groups[0]["fdSoc"], 100)
        self.assertEqual(groups[0]["minSocOnGrid"], 10)

    def test_no_write_on_a_flaky_scheduler_read(self):
        fox = FakeFox()
        with _frozen(11):
            foxctl.free_window_fill_tick(tick_cfg(), fox, tick_snap(read_ok=False))
        self.assertEqual(fox.writes, [])

    def test_no_write_when_the_change_is_below_the_step(self):
        fox = FakeFox()
        with _frozen(11):
            foxctl.free_window_fill_tick(tick_cfg(), fox, tick_snap(fd_pwr=8100))
        self.assertEqual(fox.writes, [])

    def test_rate_limited_to_one_write_per_gap(self):
        fox = FakeFox()
        with _frozen(11):
            foxctl.free_window_fill_tick(tick_cfg(), fox, tick_snap())
            foxctl.free_window_fill_tick(tick_cfg(), fox, tick_snap(load_kw=6.0))
        self.assertEqual(len(fox.writes), 1)

    def test_planner_disabled_writes_nothing(self):
        cfg = tick_cfg()
        cfg["strategy"]["fill_planner"] = False
        fox = FakeFox()
        with _frozen(11):
            foxctl.free_window_fill_tick(cfg, fox, tick_snap())
        self.assertEqual(fox.writes, [])
        self.assertIsNone(foxctl._FILL["plan"])

    def test_outside_the_window_clears_the_plan(self):
        fox = FakeFox()
        with _frozen(15):
            foxctl.free_window_fill_tick(tick_cfg(), fox, tick_snap())
        self.assertEqual(fox.writes, [])
        self.assertIsNone(foxctl._FILL["plan"])

    def test_stale_cloud_load_does_not_produce_an_over_cap_fill(self):
        # The 2026-07-31 11:32 regression: cloud load == car draw (apparent 0 kW house) while
        # the live clamp showed 14.1 kW against a 7.5 kW fill. The old estimate planned 10.1 kW.
        fox = FakeFox()
        with _frozen(11, 32):
            foxctl.free_window_fill_tick(tick_cfg(), fox,
                                         tick_snap(soc=65.0, load_kw=3.6, ev_kw=3.6,
                                                   fd_pwr=7500, grid_live=14.1))
        plan = foxctl._FILL["plan"]
        self.assertGreater(plan["fill_kw"], 0.0)
        # House is really ~3.0 kW, so the fill plus house plus car must still clear the cap.
        self.assertLessEqual(plan["fill_kw"] + 3.0 + 3.6, 14.5 + 1e-9)
        self.assertLess(plan["fill_kw"], 10.0)

    def test_plan_is_published_for_the_car_decision(self):
        fox = FakeFox()
        with _frozen(11):
            foxctl.free_window_fill_tick(tick_cfg(), fox, tick_snap())
        self.assertTrue(foxctl._FILL["plan"]["car_ok"])
        self.assertIn("ts", foxctl._FILL["plan"])


class CarGateTest(unittest.TestCase):
    """ev_divert_decision's free-window branch: cooloff, planner verdict, guard margin."""

    def setUp(self):
        foxctl._FILL.update({"plan": None, "last_write": 0.0, "written_kw": None, "trim_kw": 0.0})
        foxctl._EV.update({"on": False, "guard_cut_ts": 0.0})

    def _snap(self, gp=11.0):
        return {"soc": 49.0, "ev_kw": 0.0, "grid_power": gp, "grid_power_live": gp,
                "feedin_power": 0.0, "recommendation": {}, "scheduler": {"active": {}},
                "dynamic": {"tariff": {"free": {"start": 10, "end": 14}}, "target_soc": 100},
                "money": {"free_left_kwh": 50}, "car": {"sessions": [{"peak_kw": 3.4}]}}

    def _ev(self, **kw):
        ev = {"switch": "switch.car", "free_window_charge": True, "allow_grid": True,
              "supply_cap_kw": 14.5, "headroom_guard_kw": 0.8, "guard_cooloff_min": 3,
              "min_dwell_min": 10, "session_cap_kwh": 0, "outlook_gate": False}
        ev.update(kw)
        return ev

    def test_cooloff_blocks_a_restart_right_after_a_guard_cut(self):
        foxctl._EV["guard_cut_ts"] = foxctl.time.time()
        with _frozen(11):
            want, why = foxctl.ev_divert_decision(self._snap(), self._ev())
        self.assertFalse(want)
        self.assertIn("cooling off", why)

    def test_cooloff_expires(self):
        foxctl._EV["guard_cut_ts"] = foxctl.time.time() - 600
        foxctl._FILL["plan"] = {"car_ok": True, "must_kw": 6.8, "target_soc": 90.0,
                                "fill_kw": 8.1, "ts": foxctl.time.time()}
        with _frozen(11):
            want, _ = foxctl.ev_divert_decision(self._snap(), self._ev())
        self.assertTrue(want)

    def test_planner_verdict_holds_the_car_off_when_the_fill_needs_everything(self):
        foxctl._FILL["plan"] = {"car_ok": False, "must_kw": 10.5, "target_soc": 90.0,
                                "fill_kw": 10.5, "ts": foxctl.time.time()}
        with _frozen(11):
            want, why = foxctl.ev_divert_decision(self._snap(gp=8.0), self._ev())
        self.assertFalse(want)
        self.assertIn("no headroom", why)

    def test_planner_verdict_lets_the_car_run(self):
        foxctl._FILL["plan"] = {"car_ok": True, "must_kw": 6.8, "target_soc": 90.0,
                                "fill_kw": 8.1, "ts": foxctl.time.time()}
        with _frozen(11):
            want, why = foxctl.ev_divert_decision(self._snap(gp=13.0), self._ev())
        self.assertTrue(want)
        self.assertIn("free window", why)

    def test_a_stale_plan_is_ignored_in_favour_of_the_live_reading(self):
        foxctl._FILL["plan"] = {"car_ok": True, "must_kw": 0.0, "target_soc": 90.0,
                                "fill_kw": 8.1, "ts": foxctl.time.time() - 3600}
        with _frozen(11):
            want, why = foxctl.ev_divert_decision(self._snap(gp=13.0), self._ev())
        self.assertFalse(want)          # 13.0 + 3.4 + 0.8 guard > 14.5
        self.assertIn("no headroom", why)

    def test_fallback_start_test_now_requires_the_guard_margin(self):
        # 11.0 + 3.4 = 14.4 fits under 14.5 bare, but not with the 0.8 kW guard: this is
        # exactly the marginal start that flapped on 2026-07-31.
        with _frozen(11):
            want, _ = foxctl.ev_divert_decision(self._snap(gp=11.0), self._ev())
        self.assertFalse(want)


class SolarAfterDeadlineTest(unittest.TestCase):
    """The deadline hour is a LOCAL clock hour; sun.sun arrives in UTC."""

    def setUp(self):
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = "Australia/Sydney"
        time.tzset()

    def tearDown(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()

    def _snap(self, sunset="2026-07-31T07:14:44+00:00", remaining=17.06):
        return {"sun": {"set": sunset}, "solar_forecast": {"remaining_today": remaining}}

    def test_deadline_is_local_time_not_utc(self):
        # Live 2026-07-31 values: sunset 17:14 AEST (07:14Z). Building "14:00" from a UTC
        # `now` put the deadline at midnight AEST, past sunset, so this returned 0.0 and the
        # planner raised its target to 100% and locked the car out.
        with _frozen(11, 26):
            got = foxctl._solar_after_deadline_kwh(self._snap(), 14)
        self.assertIsNotNone(got)
        self.assertGreater(got, 1.0)
        self.assertLess(got, 17.06)

    def test_missing_forecast_is_unknown_not_zero(self):
        with _frozen(11, 26):
            self.assertIsNone(foxctl._solar_after_deadline_kwh(self._snap(remaining=None), 14))
            self.assertIsNone(foxctl._solar_after_deadline_kwh(self._snap(sunset=None), 14))

    def test_after_sunset_is_a_known_zero(self):
        # next_setting has rolled to tomorrow: there is genuinely no solar left today.
        with _frozen(19, 0):
            got = foxctl._solar_after_deadline_kwh(self._snap(sunset="2026-08-01T07:14:00+00:00"), 14)
        self.assertEqual(got, 0.0)


class HouseLoadEstimateTest(unittest.TestCase):
    """The FoxESS cloud load reading lags ~5 min; the local grid clamp is seconds-fresh.
    Underestimating the house is what breaches the supply cap, so take the higher estimate."""

    def test_live_grid_wins_when_the_cloud_load_is_stale_low(self):
        # Observed 2026-07-31 11:32: cloud load == the car draw, implying a 0 kW house, while
        # the live clamp showed grid 14.1 kW against a 7.5 kW fill and a 3.4 kW car.
        snap = {"load_kw": 3.6, "ev_kw": 3.6, "grid_power_live": 14.1}
        self.assertAlmostEqual(foxctl._fill_house_kw(snap, fill_now_kw=7.5), 3.0, places=6)

    def test_cloud_load_used_when_it_is_the_higher_estimate(self):
        snap = {"load_kw": 5.0, "ev_kw": 0.0, "grid_power_live": 8.0}
        self.assertAlmostEqual(foxctl._fill_house_kw(snap, fill_now_kw=7.5), 5.0, places=6)

    def test_grid_estimate_ignored_when_the_current_fill_is_unknown(self):
        # Without knowing the battery's draw, grid − car would count the fill as house load.
        snap = {"load_kw": 2.2, "ev_kw": 0.0, "grid_power_live": 13.0}
        self.assertAlmostEqual(foxctl._fill_house_kw(snap, fill_now_kw=None), 2.2, places=6)

    def test_falls_back_high_when_nothing_is_readable(self):
        self.assertEqual(foxctl._fill_house_kw({}, fill_now_kw=None), 2.0)

    def test_never_returns_a_zero_house(self):
        snap = {"load_kw": 3.6, "ev_kw": 3.6, "grid_power_live": 11.0}
        self.assertGreaterEqual(foxctl._fill_house_kw(snap, fill_now_kw=11.0), 0.3)
