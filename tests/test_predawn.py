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


def tick_cfg(**ev_overrides):
    ev = {"switch": "switch.car", "min_dwell_min": 10, "start_margin_kwh": 1.0,
          "session_cap_kwh": 0, "outlook_gate": False}
    ev.update(ev_overrides)
    return {"ev_divert": ev, "control": {"allow_control": True}}


def tick_snap(predawn=None, ev_kw=0.0, grid_power=0.0, telemetry_source="FoxESS"):
    return {"predawn_budget": predawn or {}, "ev_kw": ev_kw, "grid_power": grid_power,
            "energy_totals": {}, "feedin_power": 0.0, "soc": 56.0,
            "dynamic": {}, "recommendation": {}, "scheduler": {}, "money": {},
            "telemetry_source": telemetry_source}


def predawn_block(kwh, active=True, guard_kwh=None, in_free=False):
    return {"kwh": kwh, "guard_kwh": kwh if guard_kwh is None else guard_kwh,
            "parts": {}, "floor_soc": 30.0, "window_start_hour": 10,
            "hrs_to_window": 5.0, "in_free_window": in_free, "active": active,
            "dump_enabled": True, "guard_enabled": True}


class PredawnTickTest(unittest.TestCase):
    """Pre-dawn dump start/stop + import abort inside ev_divert_tick."""

    def setUp(self):
        self._ev = dict(foxctl._EV)
        foxctl._EV.update({"on": False, "last_change": 0.0, "override_until": 0.0,
                           "lowdraw_since": 0.0, "session_day": None,
                           "session_start_kwh": None, "capped": False,
                           "import_hits": 0, "guard_cut_ts": 0.0, "predawn_parked_day": None})

    def tearDown(self):
        foxctl._EV.clear()
        foxctl._EV.update(self._ev)

    def test_dump_starts_when_budget_above_margin(self):
        with mock.patch.object(foxctl, "ha_call_service") as svc, \
             mock.patch.object(foxctl, "log_event"):
            out = foxctl.ev_divert_tick(tick_cfg(), tick_snap(predawn_block(2.8)))
        svc.assert_called_once_with(mock.ANY, "switch", "turn_on", "switch.car")
        self.assertIn("pre-dawn surplus +2.8kWh", out)

    def test_dump_needs_start_margin_from_off(self):
        with mock.patch.object(foxctl, "ha_call_service") as svc, \
             mock.patch.object(foxctl, "log_event"):
            out = foxctl.ev_divert_tick(tick_cfg(), tick_snap(predawn_block(0.5)))
        svc.assert_not_called()          # 0.5 < start_margin 1.0 and car is off
        self.assertIn("off", out)

    def test_dump_keeps_running_below_margin_above_zero(self):
        foxctl._EV["on"] = True
        with mock.patch.object(foxctl, "ha_call_service") as svc, \
             mock.patch.object(foxctl, "log_event"):
            out = foxctl.ev_divert_tick(tick_cfg(), tick_snap(predawn_block(0.5), ev_kw=2.4))
        svc.assert_not_called()          # deadband: no switch flap between 0 and margin
        self.assertIn("pre-dawn surplus", out)

    def test_dump_stops_at_zero_budget(self):
        foxctl._EV["on"] = True
        with mock.patch.object(foxctl, "ha_call_service") as svc, \
             mock.patch.object(foxctl, "log_event"):
            out = foxctl.ev_divert_tick(tick_cfg(), tick_snap(predawn_block(-0.3), ev_kw=2.4))
        svc.assert_called_once_with(mock.ANY, "switch", "turn_off", "switch.car")
        self.assertIn("pre-dawn done", out)

    def test_inactive_window_no_dump(self):
        with mock.patch.object(foxctl, "ha_call_service") as svc, \
             mock.patch.object(foxctl, "log_event"):
            foxctl.ev_divert_tick(tick_cfg(), tick_snap(predawn_block(5.0, active=False)))
        svc.assert_not_called()

    def test_import_abort_after_two_hits(self):
        foxctl._EV["on"] = True
        cfg = tick_cfg()
        snap = tick_snap(predawn_block(3.0), ev_kw=2.4, grid_power=1.2)
        with mock.patch.object(foxctl, "ha_call_service") as svc, \
             mock.patch.object(foxctl, "log_event"):
            out1 = foxctl.ev_divert_tick(cfg, snap)      # hit 1 — stays on
            self.assertIn("pre-dawn surplus", out1)
            svc.assert_not_called()
            out2 = foxctl.ev_divert_tick(cfg, snap)      # hit 2 — abort
        svc.assert_called_once_with(mock.ANY, "switch", "turn_off", "switch.car")
        self.assertIn("pre-dawn abort", out2)

    def test_import_abort_uses_local_clamp_when_present(self):
        # Cloud grid_power says clean (0.0, stale) but the local clamp shows import — abort.
        foxctl._EV["on"] = True
        cfg = tick_cfg()
        snap = tick_snap(predawn_block(3.0), ev_kw=2.4, grid_power=0.0)
        snap["grid_power_live"] = 1.1
        with mock.patch.object(foxctl, "ha_call_service") as svc, \
             mock.patch.object(foxctl, "log_event"):
            foxctl.ev_divert_tick(cfg, snap)
            out2 = foxctl.ev_divert_tick(cfg, snap)
        svc.assert_called_once_with(mock.ANY, "switch", "turn_off", "switch.car")
        self.assertIn("pre-dawn abort", out2)

    def test_import_hits_reset_on_clean_poll(self):
        foxctl._EV["on"] = True
        cfg = tick_cfg()
        with mock.patch.object(foxctl, "ha_call_service"), mock.patch.object(foxctl, "log_event"):
            foxctl.ev_divert_tick(cfg, tick_snap(predawn_block(3.0), ev_kw=2.4, grid_power=1.2))
            foxctl.ev_divert_tick(cfg, tick_snap(predawn_block(3.0), ev_kw=2.4, grid_power=0.0))
            self.assertEqual(foxctl._EV["import_hits"], 0)

    def test_dump_parks_when_socket_shows_no_draw(self):
        # Socket on for 10+ min with ~0 draw → car full/unplugged. Park until the 4am day roll,
        # otherwise the branch would cycle an empty socket every dwell period until window-open.
        import time as _t
        foxctl._EV["on"] = True
        foxctl._EV["lowdraw_since"] = _t.time() - 700
        cfg = tick_cfg()
        with mock.patch.object(foxctl, "ha_call_service") as svc, \
             mock.patch.object(foxctl, "log_event"):
            out = foxctl.ev_divert_tick(cfg, tick_snap(predawn_block(3.0), ev_kw=0.02))
        svc.assert_called_once_with(mock.ANY, "switch", "turn_off", "switch.car")
        self.assertIn("pre-dawn parked", out)
        self.assertIsNotNone(foxctl._EV["predawn_parked_day"])
        # …and a later tick the same (4am-anchored) day must NOT restart the dump.
        with mock.patch.object(foxctl, "ha_call_service") as svc2, \
             mock.patch.object(foxctl, "log_event"):
            foxctl.ev_divert_tick(cfg, tick_snap(predawn_block(3.0), ev_kw=0.0))
        svc2.assert_not_called()

    def test_no_park_inside_wake_grace(self):
        # A sleeping car can take ~5½ min to start drawing (seen live 2026-07-10) — the park
        # threshold (600s) must ride out a low-draw spell the generic 300s note would flag.
        import time as _t
        foxctl._EV["on"] = True
        foxctl._EV["lowdraw_since"] = _t.time() - 400
        with mock.patch.object(foxctl, "ha_call_service") as svc, \
             mock.patch.object(foxctl, "log_event"):
            out = foxctl.ev_divert_tick(tick_cfg(), tick_snap(predawn_block(3.0), ev_kw=0.02))
        svc.assert_not_called()
        self.assertIn("pre-dawn surplus", out)
        self.assertIsNone(foxctl._EV["predawn_parked_day"])

    def test_draw_unparks_a_raced_park(self):
        # Parked, but the switch is still on (dwell) and the car starts pulling real power —
        # the park raced a slow wake-up; clear it and let the dump continue.
        foxctl._EV["on"] = True
        foxctl._EV["predawn_parked_day"] = (
            foxctl.datetime.now() - foxctl.timedelta(hours=4)).strftime("%Y-%m-%d")
        with mock.patch.object(foxctl, "ha_call_service") as svc, \
             mock.patch.object(foxctl, "log_event"):
            out = foxctl.ev_divert_tick(tick_cfg(), tick_snap(predawn_block(3.0), ev_kw=2.4))
        svc.assert_not_called()          # already on — no switch flip
        self.assertIn("pre-dawn surplus", out)
        self.assertIsNone(foxctl._EV["predawn_parked_day"])


class FloorGuardTest(unittest.TestCase):
    """Manual switch-ons are invisible to the edge-trigger; the guard acts on ACTUAL draw."""

    def setUp(self):
        self._ev = dict(foxctl._EV)
        foxctl._EV.update({"on": False, "last_change": 0.0, "override_until": 0.0,
                           "lowdraw_since": 0.0, "session_day": None,
                           "session_start_kwh": None, "capped": False,
                           "import_hits": 0, "guard_cut_ts": 0.0, "predawn_parked_day": None})

    def tearDown(self):
        foxctl._EV.clear()
        foxctl._EV.update(self._ev)

    def _run(self, snap, **ev_overrides):
        with mock.patch.object(foxctl, "ha_call_service") as svc, \
             mock.patch.object(foxctl, "log_event") as log:
            out = foxctl.ev_divert_tick(tick_cfg(**ev_overrides), snap)
        return out, svc, log

    def test_guard_cuts_manual_session(self):
        # foxctl believes the car is off (_EV.on False) but it IS drawing → manual flip. Budget short.
        snap = tick_snap(predawn_block(-2.5, active=False), ev_kw=2.43)
        out, svc, log = self._run(snap)
        svc.assert_called_once_with(mock.ANY, "switch", "turn_off", "switch.car")
        self.assertIn("floor-guard", out)
        self.assertTrue(any("floor-guard" in str(c) for c in log.call_args_list))
        self.assertEqual(foxctl._EV["on"], False)
        self.assertGreater(foxctl._EV["guard_cut_ts"], 0)

    def test_guard_respects_ui_force_charge_override(self):
        import time as _t
        foxctl._EV["override_until"] = _t.time() + 3600
        snap = tick_snap(predawn_block(-2.5, active=False), ev_kw=2.43)
        out, svc, log = self._run(snap)
        # override path wants ON; the only switch call allowed is turn_on, never a guard turn_off
        for c in svc.call_args_list:
            self.assertNotEqual(c.args[2], "turn_off")
        self.assertNotIn("floor-guard", out)

    def test_guard_skipped_inside_free_window(self):
        snap = tick_snap(predawn_block(-2.5, active=False, in_free=True), ev_kw=2.43)
        out, svc, log = self._run(snap)
        svc.assert_not_called()

    def test_guard_grace_blocks_immediate_recut(self):
        import time as _t
        foxctl._EV["guard_cut_ts"] = _t.time() - 60          # cut 1 min ago; grace is 10 min
        snap = tick_snap(predawn_block(-2.5, active=False), ev_kw=2.43)
        out, svc, log = self._run(snap)
        svc.assert_not_called()

    def test_guard_uses_guard_kwh_not_kwh(self):
        # Daytime solar-fed session: raw budget negative but guard_kwh (incl. solar) positive → no cut.
        snap = tick_snap(predawn_block(-3.0, active=False, guard_kwh=4.0), ev_kw=2.43)
        out, svc, log = self._run(snap)
        svc.assert_not_called()

    def test_guard_disabled_by_config(self):
        snap = tick_snap(predawn_block(-2.5, active=False), ev_kw=2.43)
        out, svc, log = self._run(snap, floor_guard=False)
        svc.assert_not_called()

    def test_no_cut_when_not_drawing(self):
        snap = tick_snap(predawn_block(-2.5, active=False), ev_kw=0.0)
        out, svc, log = self._run(snap)
        svc.assert_not_called()


class StaleTelemetryTest(unittest.TestCase):
    """Frozen soc/grid_power during a FoxESS outage must not drive the pre-dawn dump or floor-guard."""

    def setUp(self):
        self._ev = dict(foxctl._EV)
        foxctl._EV.update({"on": False, "last_change": 0.0, "override_until": 0.0,
                           "lowdraw_since": 0.0, "session_day": None,
                           "session_start_kwh": None, "capped": False,
                           "import_hits": 0, "guard_cut_ts": 0.0, "predawn_parked_day": None})

    def tearDown(self):
        foxctl._EV.clear()
        foxctl._EV.update(self._ev)

    def test_dump_does_not_start_when_stale(self):
        snap = tick_snap(predawn_block(5.0), telemetry_source="FoxESS(stale)")
        with mock.patch.object(foxctl, "ha_call_service") as svc, \
             mock.patch.object(foxctl, "log_event"):
            foxctl.ev_divert_tick(tick_cfg(), snap)
        svc.assert_not_called()          # want stays False == on False → no switch call

    def test_running_dump_stops_when_stale(self):
        foxctl._EV["on"] = True
        snap = tick_snap(predawn_block(5.0), ev_kw=2.4, telemetry_source="FoxESS(stale)")
        with mock.patch.object(foxctl, "ha_call_service") as svc, \
             mock.patch.object(foxctl, "log_event"):
            out = foxctl.ev_divert_tick(tick_cfg(), snap)
        svc.assert_called_once_with(mock.ANY, "switch", "turn_off", "switch.car")
        self.assertIn("pre-dawn hold", out)

    def test_floor_guard_does_not_cut_when_stale(self):
        # Manual-session fixture that would normally trip the floor-guard.
        snap = tick_snap(predawn_block(-2.5, active=False), ev_kw=2.43, telemetry_source="FoxESS(stale)")
        with mock.patch.object(foxctl, "ha_call_service") as svc, \
             mock.patch.object(foxctl, "log_event"):
            foxctl.ev_divert_tick(tick_cfg(), snap)
        svc.assert_not_called()

    def test_dump_does_not_start_when_telemetry_down(self):
        snap = tick_snap(predawn_block(5.0), telemetry_source="down")
        with mock.patch.object(foxctl, "ha_call_service") as svc, \
             mock.patch.object(foxctl, "log_event"):
            foxctl.ev_divert_tick(tick_cfg(), snap)
        svc.assert_not_called()


class SafetyHoldTest(unittest.TestCase):
    """The pre-dawn dump must never override ev_divert_decision's SAFETY holds."""

    def setUp(self):
        self._ev = dict(foxctl._EV)
        foxctl._EV.update({"on": False, "last_change": 0.0, "override_until": 0.0,
                           "lowdraw_since": 0.0, "session_day": None,
                           "session_start_kwh": None, "capped": False,
                           "import_hits": 0, "guard_cut_ts": 0.0, "predawn_parked_day": None})

    def tearDown(self):
        foxctl._EV.clear()
        foxctl._EV.update(self._ev)

    def test_dump_does_not_arm_during_force_charge_hold(self):
        snap = tick_snap(predawn_block(5.0))
        snap.update(recommendation={"force_charge": True}, dynamic={"target_soc": 90}, soc=50)
        with mock.patch.object(foxctl, "ha_call_service") as svc, \
             mock.patch.object(foxctl, "log_event"):
            out = foxctl.ev_divert_tick(tick_cfg(), snap)
        svc.assert_not_called()
        self.assertIn("car held off", out)


class _FakeDT:
    """Stand-in for foxctl.datetime with a fixed clock."""
    def __init__(self, dt):
        self._dt = dt
    def now(self):
        return self._dt
    def __getattr__(self, name):
        import datetime as _d
        return getattr(_d.datetime, name)


class SimultaneousFreeWindowTest(unittest.TestCase):
    """Free window: the car charges ALONGSIDE the battery fill (2026-07-10), bounded by the
    supply cap — start needs import + expected car draw under the cap; a running car stays
    until import exceeds it. The force-charge hold only applies OUTSIDE the free window."""

    def setUp(self):
        self._ev = dict(foxctl._EV)
        foxctl._EV.update({"on": False, "last_change": 0.0, "override_until": 0.0,
                           "lowdraw_since": 0.0, "session_day": None,
                           "session_start_kwh": None, "capped": False,
                           "import_hits": 0, "guard_cut_ts": 0.0, "predawn_parked_day": None})

    def tearDown(self):
        foxctl._EV.clear(); foxctl._EV.update(self._ev)

    def snap(self, grid_kw, free_left=40.0, soc=50):
        return {"soc": soc, "grid_power": grid_kw, "feedin_power": 0.0,
                "recommendation": {"force_charge": True},
                "scheduler": {"enabled": True,
                              "active": {"mode": "ForceCharge", "window": "11:00-15:00"}},
                "dynamic": {"target_soc": 100,
                            "tariff": {"free": {"start": 11, "end": 15,
                                                "free_kwh": 50, "excess_c": 26.4}}},
                "money": {"free_left_kwh": free_left},
                "car": {"sessions": [{"peak_kw": 2.4}]}}

    EV = {"free_window_charge": True, "allow_grid": True, "supply_cap_kw": 14.5,
          "min_export_kw": 1.0}

    def decide(self, snap, hour=12):
        import datetime as _d
        with mock.patch.object(foxctl, "datetime",
                               _FakeDT(_d.datetime(2026, 7, 10, hour, 0))):
            return foxctl.ev_divert_decision(snap, dict(self.EV))

    def test_car_joins_battery_fill_with_headroom(self):
        want, why = self.decide(self.snap(grid_kw=11.0))       # 11.0 + 2.4 < 14.5
        self.assertTrue(want)
        self.assertIn("car + battery together", why)

    def test_no_start_without_headroom(self):
        want, why = self.decide(self.snap(grid_kw=12.5))       # 12.5 + 2.4 > 14.5
        self.assertFalse(want)
        self.assertIn("no headroom", why)

    def test_running_car_survives_to_the_cap(self):
        foxctl._EV["on"] = True
        want, why = self.decide(self.snap(grid_kw=14.0))       # includes car draw, <= cap
        self.assertTrue(want)

    def test_running_car_pauses_over_the_cap(self):
        foxctl._EV["on"] = True
        want, why = self.decide(self.snap(grid_kw=15.2))
        self.assertFalse(want)
        self.assertIn("supply cap", why)

    def test_free_cap_exhausted_still_blocks(self):
        want, why = self.decide(self.snap(grid_kw=11.0, free_left=0.5))
        self.assertFalse(want)
        self.assertIn("cap used up", why)

    def test_gp_now_prefers_local_clamp(self):
        self.assertEqual(foxctl._gp_now({"grid_power": 0.2, "grid_power_live": 13.4}), 13.4)
        self.assertEqual(foxctl._gp_now({"grid_power": 0.2, "grid_power_live": None}), 0.2)
        self.assertEqual(foxctl._gp_now({"grid_power": 0.2}), 0.2)

    def test_local_clamp_drives_headroom_check(self):
        # Cloud value says 0.2kW (stale), the local clamp says 14.9kW — the running car must pause.
        foxctl._EV["on"] = True
        s = self.snap(grid_kw=0.2)
        s["grid_power_live"] = 14.9
        want, why = self.decide(s)
        self.assertFalse(want)
        self.assertIn("supply cap", why)

    def test_actual_draw_counts_as_running_no_double_count(self):
        # Manual session: belief off, car drawing 2.3kW already inside grid_power — the
        # start-check must not add the estimate on top (12.7 alone is under the cap).
        s = self.snap(grid_kw=12.7)
        s["ev_kw"] = 2.3
        want, why = self.decide(s)
        self.assertTrue(want)
        self.assertIn("car + battery together", why)

    def test_force_charge_hold_still_applies_outside_window(self):
        want, why = self.decide(self.snap(grid_kw=2.0), hour=16)   # pre-peak top-up hours
        self.assertFalse(want)
        self.assertIn("car held off", why)


class SchedulerGroupPreservationTest(unittest.TestCase):
    """USER DIRECTIVE 2026-07-12: the hand-programmed schedule group is untouchable —
    foxctl only adds/removes its OWN group, and never blind-disables on a flaky read."""

    USER = {"startHour": 11, "startMinute": 0, "endHour": 15, "endMinute": 0,
            "workMode": "ForceCharge", "minSocOnGrid": 10, "fdSoc": 100, "fdPwr": 10500, "enable": 1}

    class FakeFox:
        sn = "SN"
        def __init__(self, raw):
            self.raw, self.calls = raw, []
        def scheduler(self):
            return self.raw
        def call(self, path, body):
            self.calls.append((path, body))
            return {"ok": 1}

    def setUp(self):
        import tempfile
        self.cfg = {"state_dir": tempfile.mkdtemp()}
        foxctl._SCHED.update({"loaded": False, "mine_key": None, "user_groups": []})

    def tearDown(self):
        foxctl._SCHED.update({"loaded": False, "mine_key": None, "user_groups": []})

    def mine(self):
        return foxctl._sched_group((15, 3), (16, 0), "ForceCharge", 10, 100, 10.5)

    def test_write_preserves_user_groups(self):
        fox = self.FakeFox({"enable": 1, "groups": [dict(self.USER)]})
        g = self.mine()
        foxctl.scheduler_write_own(self.cfg, fox, g)
        path, body = fox.calls[-1]
        self.assertIn("scheduler/enable", path)
        self.assertEqual(body["groups"], [self.USER, g])
        self.assertEqual(foxctl._SCHED["mine_key"], foxctl._group_key(g))

    def test_clear_removes_only_ours_and_keeps_master_flag(self):
        g = self.mine()
        foxctl._SCHED.update({"loaded": True, "mine_key": foxctl._group_key(g)})
        fox = self.FakeFox({"enable": 1, "groups": [dict(self.USER), g]})
        foxctl.scheduler_clear_own(self.cfg, fox)
        path, body = fox.calls[-1]
        self.assertIn("scheduler/enable", path)          # NOT set/flag
        self.assertEqual(body["groups"], [self.USER])

    def test_clear_disables_master_only_when_no_user_groups(self):
        g = self.mine()
        foxctl._SCHED.update({"loaded": True, "mine_key": foxctl._group_key(g)})
        fox = self.FakeFox({"enable": 1, "groups": [g]})
        foxctl.scheduler_clear_own(self.cfg, fox)
        path, body = fox.calls[-1]
        self.assertIn("set/flag", path)
        self.assertEqual(body["enable"], 0)

    def test_flaky_read_defers_the_write(self):
        # Writing from the cached list clobbered a just-programmed user group (2026-07-13) —
        # a flake now defers the write entirely.
        foxctl._SCHED.update({"loaded": True, "user_groups": [dict(self.USER)]})
        fox = self.FakeFox(None)                          # scheduler/get flake
        self.assertIsNone(foxctl.scheduler_write_own(self.cfg, fox, self.mine()))
        self.assertEqual(fox.calls, [])

    def test_clear_refuses_blind_disable_on_flake(self):
        foxctl._SCHED.update({"loaded": True, "mine_key": foxctl._group_key(self.mine())})
        fox = self.FakeFox(None)                          # flake + empty cache
        self.assertIsNone(foxctl.scheduler_clear_own(self.cfg, fox))
        self.assertEqual(fox.calls, [])

    def test_clear_noop_when_nothing_is_ours(self):
        foxctl._SCHED.update({"loaded": True})
        fox = self.FakeFox({"enable": 1, "groups": [dict(self.USER)]})
        self.assertIsNone(foxctl.scheduler_clear_own(self.cfg, fox))
        self.assertEqual(fox.calls, [])


class BaseScheduleGuardTest(unittest.TestCase):
    """User-authorized (2026-07-13): re-add the free-window base fill group when a HEALTHY
    read shows it missing; never act on a flake; registered as a USER group."""

    BASE = {"startHour": 10, "startMinute": 0, "endHour": 14, "endMinute": 0,
            "workMode": "ForceCharge", "minSocOnGrid": 10, "fdSoc": 100, "fdPwr": 10500, "enable": 1}

    class FakeFox:
        sn = "SN"
        def __init__(self):
            self.calls = []
        def call(self, path, body):
            self.calls.append((path, body))
            return {"ok": 1}

    def setUp(self):
        import tempfile
        self.cfg = {"state_dir": tempfile.mkdtemp(),
                    "strategy": {"base_schedule_guard": True, "inverter_min_soc": 10,
                                 "charge_target_soc": 100, "max_soc": 100,
                                 "force_charge_power_kw": 10.5}}
        foxctl._SCHED.update({"loaded": True, "mine_key": None, "user_groups": []})

    def tearDown(self):
        foxctl._SCHED.update({"loaded": False, "mine_key": None, "user_groups": []})

    def snap(self, groups, read_ok=True):
        return {"dynamic": {"tariff": {"free": {"start": 10, "end": 14}}},
                "scheduler": {"enabled": bool(groups), "groups": groups, "read_ok": read_ok}}

    def test_restores_missing_base_group(self):
        fox = self.FakeFox()
        msg = foxctl.ensure_base_schedule(self.cfg, fox, self.snap([]))
        self.assertIn("restored 10:00–14:00", msg)
        _, body = fox.calls[-1]
        self.assertEqual(body["groups"][-1]["startHour"], 10)
        self.assertEqual(body["groups"][-1]["fdSoc"], 100)
        # registered as a USER group, so scheduler_clear_own will never remove it
        self.assertIn(foxctl._group_key(body["groups"][-1]),
                      [foxctl._group_key(g) for g in foxctl._SCHED["user_groups"]])

    def test_present_base_group_untouched(self):
        fox = self.FakeFox()
        self.assertIsNone(foxctl.ensure_base_schedule(self.cfg, fox, self.snap([dict(self.BASE)])))
        self.assertEqual(fox.calls, [])

    def test_never_acts_on_flaky_read(self):
        fox = self.FakeFox()
        self.assertIsNone(foxctl.ensure_base_schedule(self.cfg, fox, self.snap([], read_ok=False)))
        self.assertEqual(fox.calls, [])

    def test_preserves_other_groups_when_restoring(self):
        other = {"startHour": 15, "startMinute": 3, "endHour": 16, "endMinute": 0,
                 "workMode": "ForceCharge", "minSocOnGrid": 10, "fdSoc": 100, "fdPwr": 10500, "enable": 1}
        fox = self.FakeFox()
        foxctl.ensure_base_schedule(self.cfg, fox, self.snap([dict(other)]))
        _, body = fox.calls[-1]
        self.assertEqual(len(body["groups"]), 2)
        self.assertEqual(body["groups"][0], other)

    def test_guard_can_be_disabled(self):
        self.cfg["strategy"]["base_schedule_guard"] = False
        fox = self.FakeFox()
        self.assertIsNone(foxctl.ensure_base_schedule(self.cfg, fox, self.snap([])))
        self.assertEqual(fox.calls, [])


class AttributeEvKwTest(unittest.TestCase):
    """Draw on the shared plug only counts as the car while the car's relay is ON
    (2026-07-11: outdoor heater on the spare socket tripped the floor-guard all evening)."""

    def test_relay_off_zeroes_draw(self):
        kw, src = foxctl._attribute_ev_kw(1.5, "off", "entity x")
        self.assertEqual(kw, 0.0)
        self.assertIn("ignoring 1.50kW", src)

    def test_relay_on_keeps_draw(self):
        self.assertEqual(foxctl._attribute_ev_kw(2.4, "on", "s"), (2.4, "s"))

    def test_unavailable_fails_toward_guarding(self):
        self.assertEqual(foxctl._attribute_ev_kw(2.4, "unavailable", "s"), (2.4, "s"))
        self.assertEqual(foxctl._attribute_ev_kw(2.4, None, "s"), (2.4, "s"))

    def test_relay_off_no_draw_untouched(self):
        self.assertEqual(foxctl._attribute_ev_kw(0.0, "off", "s"), (0.0, "s"))


class SchedulerViewTest(unittest.TestCase):
    """_scheduler_view: an enabled group is only ACTIVE while the clock is inside its
    window (2026-07-10: a persisted 10:00-14:00 group read as 'force-charging' at 2am,
    holding the car off all night and blocking the pre-dawn dump)."""

    RAW = {"enable": 1, "groups": [
        {"enable": 1, "workMode": "ForceCharge", "startHour": 10, "startMinute": 0,
         "endHour": 14, "endMinute": 0, "fdSoc": 100, "fdPwr": 10500},
    ]}

    def view(self, minutes, raw=None):
        return foxctl.FoxESS._scheduler_view(raw if raw is not None else self.RAW, minutes)

    def test_outside_window_is_not_active_but_segment_visible(self):
        v = self.view(2 * 60)                                  # 02:00
        self.assertTrue(v["enabled"])
        self.assertIsNone(v["active"])
        self.assertEqual(v["segment"]["window"], "10:00-14:00")

    def test_inside_window_is_active(self):
        for minutes in (10 * 60, 12 * 60 + 30, 13 * 60 + 59):
            self.assertEqual(self.view(minutes)["active"]["mode"], "ForceCharge")

    def test_window_end_is_exclusive(self):
        self.assertIsNone(self.view(14 * 60)["active"])

    def test_scheduler_disabled(self):
        v = self.view(12 * 60, raw={"enable": 0, "groups": self.RAW["groups"]})
        self.assertFalse(v["enabled"])
        self.assertIsNone(v["active"])
        self.assertIsNone(v["segment"])

    def test_disabled_and_invalid_groups_skipped(self):
        raw = {"enable": 1, "groups": [
            {"enable": 0, "workMode": "ForceCharge", "startHour": 0, "startMinute": 0,
             "endHour": 23, "endMinute": 59},
            {"enable": 1, "workMode": "Invalid", "startHour": 0, "startMinute": 0,
             "endHour": 23, "endMinute": 59},
        ]}
        v = self.view(12 * 60, raw=raw)
        self.assertIsNone(v["active"])
        self.assertIsNone(v["segment"])

    def test_second_group_active_first_reported_as_segment(self):
        raw = {"enable": 1, "groups": [
            dict(self.RAW["groups"][0]),
            {"enable": 1, "workMode": "ForceDischarge", "startHour": 18, "startMinute": 0,
             "endHour": 21, "endMinute": 0, "fdSoc": 15, "fdPwr": 8000},
        ]}
        v = self.view(19 * 60, raw=raw)
        self.assertEqual(v["active"]["mode"], "ForceDischarge")
        self.assertEqual(v["segment"]["mode"], "ForceCharge")


if __name__ == "__main__":
    unittest.main()
