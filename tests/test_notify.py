"""Tests for maybe_notify stale debouncing.

A single failed poll cycle self-heals (control already holds for that cycle),
so the stale notification must only fire after `notify.stale_cycles`
consecutive stale cycles (default 3), once per outage.

    python3 -m unittest tests.test_notify -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "energy_tools"))
import foxctl  # noqa: E402


def cfg(**notify_overrides):
    n = {"enabled": True, "on_stale": True, "on_sell": False}
    n.update(notify_overrides)
    return {"notify": n}


def snap(source):
    return {"telemetry_source": source, "recommendation": {}}


class StaleDebounceTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self._orig = foxctl.ha_notify
        foxctl.ha_notify = lambda cfg, t, m: self.sent.append(t)
        foxctl._NOTIFY.update(
            {"stale_count": 0, "stale_notified": False, "last_selling": False,
             "sent": {}, "loaded": True}      # loaded=True: never read the real state file
        )
        self._save = foxctl._notify_save
        foxctl._notify_save = lambda cfg: None

    def tearDown(self):
        foxctl.ha_notify = self._orig
        foxctl._notify_save = self._save

    def cycles(self, c, sources):
        for s in sources:
            foxctl.maybe_notify(c, snap(s))

    def test_single_stale_cycle_is_silent(self):
        self.cycles(cfg(), ["FoxESS(down)", "FoxESS", "FoxESS"])
        self.assertEqual(self.sent, [])

    def test_two_stale_cycles_still_silent(self):
        self.cycles(cfg(), ["FoxESS(stale)", "FoxESS(down)", "FoxESS"])
        self.assertEqual(self.sent, [])

    def test_three_consecutive_stale_notifies_once(self):
        self.cycles(cfg(), ["FoxESS(down)"] * 5)
        self.assertEqual(len(self.sent), 1)

    def test_recovery_resets_the_counter(self):
        self.cycles(cfg(), ["FoxESS(down)", "FoxESS(down)", "FoxESS",
                            "FoxESS(down)", "FoxESS(down)", "FoxESS"])
        self.assertEqual(self.sent, [])

    def test_new_outage_after_recovery_notifies_again(self):
        self.cycles(cfg(), ["FoxESS(down)"] * 3 + ["FoxESS"] + ["FoxESS(down)"] * 3)
        self.assertEqual(len(self.sent), 2)

    def test_stale_cycles_option_of_one_keeps_old_behaviour(self):
        self.cycles(cfg(stale_cycles=1), ["FoxESS(down)"])
        self.assertEqual(len(self.sent), 1)

    def test_disabled_on_stale_never_notifies(self):
        self.cycles(cfg(on_stale=False), ["FoxESS(down)"] * 10)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()


class NotifyGapTest(unittest.TestCase):
    """notify.min_gap_min was configured (180) but never read — every caller could page
    on every 5-minute cycle. Notifications are now rate limited per dedupe key."""

    def setUp(self):
        self.sent = []
        self._orig = foxctl.ha_notify
        foxctl.ha_notify = lambda cfg, t, m: self.sent.append(t)
        foxctl._NOTIFY.update({"stale_count": 0, "stale_notified": False,
                               "last_selling": False, "sent": {}, "loaded": True})
        self._save = foxctl._notify_save
        foxctl._notify_save = lambda cfg: None

    def tearDown(self):
        foxctl.ha_notify = self._orig
        foxctl._notify_save = self._save

    def test_due_when_never_sent(self):
        self.assertTrue(foxctl.notify_due({}, "k", now=1000.0, gap_s=600))

    def test_not_due_inside_the_gap(self):
        self.assertFalse(foxctl.notify_due({"k": 900.0}, "k", now=1000.0, gap_s=600))

    def test_due_again_after_the_gap(self):
        self.assertTrue(foxctl.notify_due({"k": 100.0}, "k", now=1000.0, gap_s=600))

    def test_separate_keys_do_not_block_each_other(self):
        sent = {"a": 990.0}
        self.assertFalse(foxctl.notify_due(sent, "a", now=1000.0, gap_s=600))
        self.assertTrue(foxctl.notify_due(sent, "b", now=1000.0, gap_s=600))

    def test_zero_gap_disables_rate_limiting(self):
        self.assertTrue(foxctl.notify_due({"k": 999.0}, "k", now=1000.0, gap_s=0))


def car_snap(on_track=False, soc=59.22, deadline="2026-08-01"):
    return {"telemetry_source": "FoxESS", "recommendation": {},
            "car": {"target": {"on_track": on_track, "soc": soc, "kwh_needed": 8.7,
                               "hours_needed": 2.7, "chargeable_hours": 2.4,
                               "target_soc": 80.0, "deadline_date": deadline}}}


class CarTargetNotifyTest(unittest.TestCase):
    """The car-behind notice must fire once per deadline, not once per poll — and an
    unreadable car SoC must not look like 'on track' and re-arm it."""

    def setUp(self):
        self.sent = []
        self._orig = foxctl.ha_notify
        foxctl.ha_notify = lambda cfg, t, m: self.sent.append(t)
        foxctl._NOTIFY.update({"stale_count": 0, "stale_notified": False,
                               "last_selling": False, "sent": {}, "loaded": True})
        self._save = foxctl._notify_save
        foxctl._notify_save = lambda cfg: None

    def tearDown(self):
        foxctl.ha_notify = self._orig
        foxctl._notify_save = self._save

    def _cfg(self):
        return {"notify": {"enabled": True, "on_stale": False, "on_sell": False,
                           "on_car_target": True, "min_gap_min": 180}}

    def test_notifies_once_not_every_cycle(self):
        for _ in range(6):
            foxctl.maybe_notify(self._cfg(), car_snap())
        self.assertEqual(len(self.sent), 1)

    def test_unknown_soc_does_not_rearm_the_notice(self):
        # WiCAN only reports when the car's ECU is awake: on_track goes None, not True.
        # Treating None as "recovered" is what let it re-page when the reading came back.
        foxctl.maybe_notify(self._cfg(), car_snap())
        foxctl.maybe_notify(self._cfg(), car_snap(on_track=None))
        foxctl.maybe_notify(self._cfg(), car_snap())
        self.assertEqual(len(self.sent), 1)

    def test_a_new_deadline_may_notify_again(self):
        foxctl.maybe_notify(self._cfg(), car_snap(deadline="2026-08-01"))
        foxctl.maybe_notify(self._cfg(), car_snap(deadline="2026-08-08"))
        self.assertEqual(len(self.sent), 2)

    def test_on_track_never_notifies(self):
        for _ in range(4):
            foxctl.maybe_notify(self._cfg(), car_snap(on_track=True))
        self.assertEqual(self.sent, [])

    def test_unknown_soc_alone_never_notifies(self):
        for _ in range(4):
            foxctl.maybe_notify(self._cfg(), car_snap(on_track=None))
        self.assertEqual(self.sent, [])

    def test_can_be_switched_off(self):
        c = self._cfg()
        c["notify"]["on_car_target"] = False
        foxctl.maybe_notify(c, car_snap())
        self.assertEqual(self.sent, [])

    def test_state_survives_a_restart(self):
        # _NOTIFY is module state; a container restart re-armed every notice. The sent map
        # is persisted, so a reload must not re-page.
        foxctl.maybe_notify(self._cfg(), car_snap())
        persisted = dict(foxctl._NOTIFY["sent"])
        foxctl._NOTIFY.update({"sent": {}, "stale_count": 0, "stale_notified": False,
                               "last_selling": False, "loaded": True})
        foxctl._NOTIFY["sent"] = persisted          # what _notify_load would restore
        foxctl.maybe_notify(self._cfg(), car_snap())
        self.assertEqual(len(self.sent), 1)
