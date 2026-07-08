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
            {"stale_count": 0, "stale_notified": False, "last_selling": False}
        )

    def tearDown(self):
        foxctl.ha_notify = self._orig

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
