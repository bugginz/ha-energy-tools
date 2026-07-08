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

    def test_import_hits_reset_on_clean_poll(self):
        foxctl._EV["on"] = True
        cfg = tick_cfg()
        with mock.patch.object(foxctl, "ha_call_service"), mock.patch.object(foxctl, "log_event"):
            foxctl.ev_divert_tick(cfg, tick_snap(predawn_block(3.0), ev_kw=2.4, grid_power=1.2))
            foxctl.ev_divert_tick(cfg, tick_snap(predawn_block(3.0), ev_kw=2.4, grid_power=0.0))
            self.assertEqual(foxctl._EV["import_hits"], 0)

    def test_dump_parks_when_socket_shows_no_draw(self):
        # Socket on for 6+ min with ~0 draw → car full/unplugged. Park until the 4am day roll,
        # otherwise the branch would cycle an empty socket every dwell period until window-open.
        import time as _t
        foxctl._EV["on"] = True
        foxctl._EV["lowdraw_since"] = _t.time() - 400
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


if __name__ == "__main__":
    unittest.main()
