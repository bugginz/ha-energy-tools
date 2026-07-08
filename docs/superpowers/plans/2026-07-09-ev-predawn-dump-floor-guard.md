# EV Pre-Dawn Dump + Floor-Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dump overnight battery surplus (above a 30% floor) into the car pre-dawn, and cut ANY charge session — including manual HA switch flips — that would drag the battery below that floor before the free tariff window refills it.

**Architecture:** All logic lives in `energy_tools/foxctl.py` (single-module pattern, ~3550 lines — follow it, don't restructure). A new pure function `ev_predawn_budget` computes the surplus; `gather_and_decide` publishes it as `snap["predawn_budget"]`; `ev_divert_tick` grows a pre-dawn start/stop branch, a grid-import abort, and a floor-guard that acts on *actual draw* rather than foxctl's own switch bookkeeping. Config flows options → `build_config.py` → runtime `ev_divert{}` dict.

**Tech Stack:** Python 3 stdlib only (no pip deps). Tests: stdlib `unittest` + `unittest.mock` (NO pytest — not installed).

**Spec:** `docs/superpowers/specs/2026-07-08-ev-predawn-battery-dump-design.md`

## Global Constraints

- Python stdlib only; runs in an Alpine container. No new dependencies.
- Tests: `python3 -m unittest tests.test_predawn -v` (new file). The legacy `tests/test_foxctl.py` is STALE (9 failures / 55 errors on clean HEAD) — never run it as a gate, never "fix" it.
- Commit + push after every task (user's standing instruction). Deploy = push, then `docker compose pull/up` on the Pi (HA Container host — there is NO add-on store anymore).
- Config layering: `config.yaml` options + schema → `build_config.py` → runtime config. The repo's `foxctl_config.json` is dev-only. Runtime keys inside `ev_divert{}` have no `ev_` prefix; option names DO (e.g. option `ev_predawn_dump` → runtime `predawn_dump`).
- Line numbers below are from commit `a0a5840`; re-locate with the quoted anchor text if drifted.
- Version bumps to **1.67.0** (config.yaml + new `VERSION` constant + CHANGELOG).

---

### Task 1: Pure budget function `ev_predawn_budget`

**Files:**
- Modify: `energy_tools/foxctl.py` (insert directly after `ev_car_budget`, which ends ~line 1245 with `"load_to_sunrise_kwh": round(load, 2), "reserve_kwh": round(reserve, 2)}`)
- Test: `tests/test_predawn.py` (create)

**Interfaces:**
- Consumes: nothing new.
- Produces: `ev_predawn_budget(soc, cap_kwh, floor_soc, load_to_window_kwh) -> (budget_kwh: float, parts: dict)` where `parts = {"usable_above_floor_kwh": float, "load_to_window_kwh": float}`. Tasks 2–4 rely on this exact signature.

- [ ] **Step 1: Write the failing test**

Create `tests/test_predawn.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/robwil/projects/ha-energy-tools && python3 -m unittest tests.test_predawn -v`
Expected: 3 ERRORS — `AttributeError: module 'foxctl' has no attribute 'ev_predawn_budget'`

- [ ] **Step 3: Write minimal implementation**

In `energy_tools/foxctl.py`, immediately after the `ev_car_budget` function body, add:

```python
def ev_predawn_budget(soc, cap_kwh, floor_soc, load_to_window_kwh):
    """kWh of battery above the PLANNING floor that the house provably won't need before the next
    free-window start (where the battery refills for ~free). Positive => the car may take it
    pre-dawn; <= 0 => any charge session is eating what the house needs to hold the floor.
    No solar term — conservative at night; the floor-guard adds remaining solar separately.
    Pure. Returns (budget_kwh, parts)."""
    usable = max(0.0, (float(soc) - float(floor_soc)) / 100.0) * float(cap_kwh)
    load = float(load_to_window_kwh or 0.0)
    return round(usable - load, 2), {"usable_above_floor_kwh": round(usable, 2),
                                     "load_to_window_kwh": round(load, 2)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_predawn -v`
Expected: `OK` (3 tests)

- [ ] **Step 5: Commit**

```bash
git add tests/test_predawn.py energy_tools/foxctl.py
git commit -m "feat: ev_predawn_budget — battery surplus above the planning floor" && git push
```

---

### Task 2: Config plumbing (defaults, build_config, config.yaml)

**Files:**
- Modify: `energy_tools/foxctl.py` — `DEFAULT_CONFIG["ev_divert"]` (~line 132, anchor `"session_cap_kwh": 30}`)
- Modify: `energy_tools/build_config.py` — `fc["ev_divert"] = {` block (lines 36–50)
- Modify: `energy_tools/config.yaml` — options (after `ev_start_margin_kwh: 1.0`, line 46) and schema (after `ev_start_margin_kwh: float`, line 100), plus `version: "1.67.0"` (line 2)

**Interfaces:**
- Produces runtime `ev_divert{}` keys read by Tasks 3–4: `predawn_dump` (bool), `predawn_floor_soc` (float), `predawn_start_hour` (int), `floor_guard` (bool), `predawn_import_stop_kw` (float), `guard_grace_min` (int).

- [ ] **Step 1: Extend DEFAULT_CONFIG**

In `DEFAULT_CONFIG["ev_divert"]`, replace the line `"session_cap_kwh": 30},` with:

```python
                  "session_cap_kwh": 30,
                  # Pre-dawn dump: from predawn_start_hour until the free-window start, put battery
                  # surplus above predawn_floor_soc into the car — the window refills it for ~free.
                  # floor_guard also cuts MANUAL switch-on sessions that would breach the floor
                  # (UI force-charge override is the one exemption). predawn_import_stop_kw aborts a
                  # dump that starts pulling from the meter; guard_grace_min spaces repeat guard cuts.
                  "predawn_dump": True, "predawn_floor_soc": 30, "predawn_start_hour": 4,
                  "floor_guard": True, "predawn_import_stop_kw": 0.5, "guard_grace_min": 10},
```

- [ ] **Step 2: Extend build_config.py**

In the `fc["ev_divert"] = {` dict, after the `"start_margin_kwh": ...` line, add:

```python
    "predawn_dump": bool(opt.get("ev_predawn_dump", True)),
    "predawn_floor_soc": float(opt.get("ev_predawn_floor_soc", 30)),
    "predawn_start_hour": int(opt.get("ev_predawn_start_hour", 4)),
    "floor_guard": bool(opt.get("ev_floor_guard", True)),
    "predawn_import_stop_kw": float(opt.get("ev_predawn_import_stop_kw", 0.5)),
    "guard_grace_min": int(opt.get("ev_guard_grace_min", 10)),
```

- [ ] **Step 3: Extend config.yaml**

Set `version: "1.67.0"`. In `options:` after `ev_start_margin_kwh: 1.0` add:

```yaml
  ev_predawn_dump: true
  ev_predawn_floor_soc: 30
  ev_predawn_start_hour: 4
  ev_floor_guard: true
  ev_predawn_import_stop_kw: 0.5
  ev_guard_grace_min: 10
```

In `schema:` after `ev_start_margin_kwh: float` add:

```yaml
  ev_predawn_dump: bool
  ev_predawn_floor_soc: float
  ev_predawn_start_hour: int
  ev_floor_guard: bool
  ev_predawn_import_stop_kw: float
  ev_guard_grace_min: int
```

- [ ] **Step 4: Verify**

Run: `python3 -m py_compile energy_tools/foxctl.py energy_tools/build_config.py && python3 -c "import sys; sys.path.insert(0,'energy_tools'); import foxctl; ev=foxctl.DEFAULT_CONFIG['ev_divert']; assert ev['predawn_floor_soc']==30 and ev['floor_guard'] is True and ev['predawn_start_hour']==4; print('defaults OK')"`
Expected: `defaults OK`

- [ ] **Step 5: Commit**

```bash
git add energy_tools/foxctl.py energy_tools/build_config.py energy_tools/config.yaml
git commit -m "feat: config plumbing for pre-dawn dump + floor-guard options (v1.67.0 bump)" && git push
```

---

### Task 3: Publish `snap["predawn_budget"]` + `VERSION` in gather_and_decide

**Files:**
- Modify: `energy_tools/foxctl.py` — (a) top of file after the import block (anchor: line 26 `from __future__ import annotations`, imports end ~line 45); (b) the car-budget wiring in `gather_and_decide` (anchor ~line 2272: `car_budget_kwh, car_budget_parts = ev_car_budget(`); (c) the snap-injection block (anchor ~line 2351: `snap["car_budget"] = {`).

**Interfaces:**
- Consumes: `ev_predawn_budget` (Task 1); in-scope locals `hh`, `free_start`, `hrs_to_free`, `profile`, `consumption`, `typical_load`, `night_factor`, `soc`, `cap_kwh`, `solar_remaining`, `ev_cfg` (all already defined ~lines 2244–2272).
- Produces: `snap["predawn_budget"]` dict with keys `kwh, parts, guard_kwh, floor_soc, window_start_hour, hrs_to_window, in_free_window, active, dump_enabled, guard_enabled` — Tasks 4–5 read exactly these; `snap["version"]` and module constant `VERSION`.

- [ ] **Step 1: Add the VERSION constant**

Directly after the import block (before the first `def`/constant section), add:

```python
VERSION = "1.67.0"   # keep in step with config.yaml `version` + CHANGELOG on every release
```

- [ ] **Step 2: Compute the pre-dawn budget beside the car budget**

Immediately after the two `ev_car_budget(...)` wiring lines (anchor: `car_budget_kwh, car_budget_parts = ev_car_budget(soc, cap_kwh, inv_floor, solar_remaining,` / `load_to_sunrise, ev_cfg.get("comfort_reserve_kwh", 2.0))`), add:

```python
    # Pre-dawn dump + floor-guard budget: battery above the PLANNING floor vs expected load until the
    # next free-window start (hrs_to_free, computed above for survival_soc — same horizon, same profile
    # + night factor). guard_kwh adds remaining solar so a daytime solar-fed session is never cut.
    pd_floor = float(ev_cfg.get("predawn_floor_soc", 30) or 30)
    pd_start = int(ev_cfg.get("predawn_start_hour", 4))
    load_to_window = _load_to_sunrise(consumption.get("hour_profile"), hrs_to_free, typical_load, night_factor)
    predawn_kwh, predawn_parts = ev_predawn_budget(soc, cap_kwh, pd_floor, load_to_window)
    _free_win = profile.get("free") or {}
    in_free = bool(_free_win) and _in_window(_free_win, hh)
    predawn_snap = {
        "kwh": predawn_kwh, "parts": predawn_parts,
        "guard_kwh": round(predawn_kwh + float(solar_remaining or 0.0), 2),
        "floor_soc": pd_floor, "window_start_hour": free_start,
        "hrs_to_window": round(hrs_to_free, 1), "in_free_window": in_free,
        "active": bool(_free_win) and not in_free and pd_start <= hh < float(free_start),
        "dump_enabled": bool(ev_cfg.get("predawn_dump", True)),
        "guard_enabled": bool(ev_cfg.get("floor_guard", True)),
    }
```

- [ ] **Step 3: Inject into snap**

Directly after the `snap["car_budget"] = {...}` block (ends with `"start_margin_kwh": float(ev_cfg.get("start_margin_kwh", 1.0) or 0.0)}`), add:

```python
    snap["predawn_budget"] = predawn_snap
    snap["version"] = VERSION
```

- [ ] **Step 4: Verify**

Run: `python3 -m py_compile energy_tools/foxctl.py && python3 -m unittest tests.test_predawn -v`
Expected: compile clean, `OK`. (gather_and_decide needs live HA/FoxESS — the snap shape is exercised live in Task 7.)

- [ ] **Step 5: Commit**

```bash
git add energy_tools/foxctl.py
git commit -m "feat: publish snap predawn_budget + version field" && git push
```

---

### Task 4: Pre-dawn dump branch + grid-import abort in ev_divert_tick

**Files:**
- Modify: `energy_tools/foxctl.py` — `_EV` initializer (~line 1211) and `ev_divert_tick` (~lines 1296–1362)
- Test: `tests/test_predawn.py` (extend)

**Interfaces:**
- Consumes: `snap["predawn_budget"]` (Task 3 shape), `ev_divert{}` keys (Task 2), existing `_EV`, `ha_call_service`, `log_event`, `snap["grid_power"]` (grid IMPORT kW, ≥ 0 — from `gridConsumptionPower`).
- Produces: tick status strings containing `pre-dawn surplus` / `pre-dawn done` / `pre-dawn abort` (Task 6's card shows the tick string verbatim).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_predawn.py`:

```python
def tick_cfg(**ev_overrides):
    ev = {"switch": "switch.car", "min_dwell_min": 10, "start_margin_kwh": 1.0,
          "session_cap_kwh": 0, "outlook_gate": False}
    ev.update(ev_overrides)
    return {"ev_divert": ev, "control": {"allow_control": True}}


def tick_snap(predawn=None, ev_kw=0.0, grid_power=0.0):
    return {"predawn_budget": predawn or {}, "ev_kw": ev_kw, "grid_power": grid_power,
            "energy_totals": {}, "feedin_power": 0.0, "soc": 56.0,
            "dynamic": {}, "recommendation": {}, "scheduler": {}, "money": {}}


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_predawn -v`
Expected: PredawnBudgetTest still OK; PredawnTickTest cases FAIL (no pre-dawn branch yet; `_EV` lacks `import_hits`).

- [ ] **Step 3: Implement**

(a) Replace the `_EV = {...}` initializer (~line 1211) with:

```python
_EV = {"on": None, "last_change": 0.0, "override_until": 0.0, "lowdraw_since": 0.0,
       "session_day": None, "session_start_kwh": None, "capped": False,
       "import_hits": 0, "guard_cut_ts": 0.0, "predawn_parked_day": None}   # + pre-dawn dump/guard
```

(b) In `ev_divert_tick`, between the outlook-gate block (ends `why += f" · outlook +{budget:.1f}kWh"`) and the daily-cap block (`if cap > 0 and ev_cum is not None:`), insert:

```python
        # 3) PRE-DAWN dump: put overnight battery surplus above the planning floor into the car —
        #    the free window refills the battery for ~free a few hours later (spec 2026-07-08).
        #    Deadband mirrors the outlook gate: need budget > start_margin to START, > 0 to KEEP GOING.
        pdb = snap.get("predawn_budget") or {}
        if (not want and pdb.get("dump_enabled") and pdb.get("active")
                and isinstance(pdb.get("kwh"), (int, float))
                and _EV.get("predawn_parked_day") != day):
            b = float(pdb["kwh"])
            margin = float(ev.get("start_margin_kwh", 1.0) or 0.0)
            if b > 0 and (_EV.get("on") or b > margin):
                want, why = True, (f"pre-dawn surplus +{b:.1f}kWh above {pdb.get('floor_soc', 30):.0f}% floor"
                                   f" · refills free at {int(pdb.get('window_start_hour', 10)):02d}:00")
            elif b <= 0 and _EV.get("on"):
                want, why = False, (f"pre-dawn done: surplus exhausted (budget {b:+.1f}kWh at "
                                    f"{pdb.get('floor_soc', 30):.0f}% floor)")
        # Park a dump whose socket shows sustained no-draw (car full or unplugged) until the 4am day
        # roll — otherwise the branch would cycle an empty socket every dwell period to window-open.
        if want and why.startswith("pre-dawn") and _EV.get("on") and _EV.get("lowdraw_since") \
                and (now - _EV["lowdraw_since"]) > 300:
            _EV["predawn_parked_day"] = day
            want, why = False, "pre-dawn parked: no draw — car full or unplugged (resets ~4am)"
        # Import abort: a dump must be battery-only — if the meter shows sustained import (house spike
        # pushing house+car past inverter discharge), buying at shoulder rates defeats the point.
        if want and why.startswith("pre-dawn"):
            gp = snap.get("grid_power")
            stop_kw = float(ev.get("predawn_import_stop_kw", 0.5) or 0.0)
            if isinstance(gp, (int, float)) and gp > stop_kw:
                _EV["import_hits"] = _EV.get("import_hits", 0) + 1
            else:
                _EV["import_hits"] = 0
            if _EV["import_hits"] >= 2:
                want, why = False, f"pre-dawn abort: importing {gp:.1f}kW from grid — dump must be battery-only"
        else:
            _EV["import_hits"] = 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_predawn -v`
Expected: `OK` (all PredawnBudgetTest + PredawnTickTest cases)

- [ ] **Step 5: Commit**

```bash
git add tests/test_predawn.py energy_tools/foxctl.py
git commit -m "feat: pre-dawn battery→car dump branch with import abort" && git push
```

---

### Task 5: Universal floor-guard (cuts manual sessions)

**Files:**
- Modify: `energy_tools/foxctl.py` — `ev_divert_tick`, immediately before the dwell line `due = (now - _EV["last_change"]) >= ev.get("min_dwell_min", 10) * 60`
- Test: `tests/test_predawn.py` (extend)

**Interfaces:**
- Consumes: `snap["predawn_budget"]["guard_kwh"/"guard_enabled"/"in_free_window"]`, `snap["ev_kw"]`, `_EV["override_until"/"guard_cut_ts"]`, `ev["guard_grace_min"]`.
- Produces: guard cut path returns early with a string containing `floor-guard`; sets `_EV["on"]=False`, `_EV["guard_cut_ts"]=now`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_predawn.py` (inside the module, new class):

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_predawn -v`
Expected: FloorGuardTest cases FAIL (`test_guard_cuts_manual_session` etc. — no guard code yet). Earlier classes still pass.

- [ ] **Step 3: Implement**

In `ev_divert_tick`, immediately BEFORE the line `due = (now - _EV["last_change"]) >= ev.get("min_dwell_min", 10) * 60`, insert:

```python
    # FLOOR-GUARD (spec 2026-07-08 feature 2): a session started by hand in HA is invisible to the
    # edge-trigger below (_EV["on"] tracks foxctl's own belief — want==belief → no switch call). So:
    # if the car is ACTUALLY drawing, nothing above wants it on, no UI force-charge override is
    # active, we're outside the free window (import there is 0c), and the guard budget (incl.
    # remaining solar, so a solar-fed daytime session is never cut) says the battery lands below the
    # floor before the window opens → cut it. guard_grace_min spaces repeat cuts so a deliberate
    # re-flip gets a visible grace window instead of an instant silent kill.
    pdb_g = snap.get("predawn_budget") or {}
    gb = pdb_g.get("guard_kwh")
    ev_kw_now = snap.get("ev_kw")
    if (ev.get("floor_guard", True) and pdb_g.get("guard_enabled", True) and not want
            and now >= _EV.get("override_until", 0) and not pdb_g.get("in_free_window")
            and isinstance(ev_kw_now, (int, float)) and ev_kw_now >= 0.3
            and isinstance(gb, (int, float)) and gb <= 0
            and (now - _EV.get("guard_cut_ts", 0.0)) >= float(ev.get("guard_grace_min", 10)) * 60):
        try:
            ha_call_service(cfg, "switch", "turn_off", sw)
        except Exception as e:
            print(f"floor-guard switch failed: {e}", file=sys.stderr)
            return f"ev divert error: {e}"
        log_event("ev_divert", (f"car charger OFF (floor-guard: battery would land below "
                                f"{pdb_g.get('floor_soc', 30):.0f}% before the "
                                f"{int(pdb_g.get('window_start_hour', 10)):02d}:00 free window — "
                                f"short {abs(gb):.1f}kWh)"))
        _EV["on"], _EV["last_change"], _EV["guard_cut_ts"] = False, now, now
        return f"car charger off (floor-guard cut manual session · short {abs(gb):.1f}kWh)"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_predawn -v`
Expected: `OK` — all classes.

- [ ] **Step 5: Commit**

```bash
git add tests/test_predawn.py energy_tools/foxctl.py
git commit -m "feat: floor-guard cuts manual charge sessions that would breach the floor" && git push
```

---

### Task 6: Dashboard surfacing + CHANGELOG

**Files:**
- Modify: `energy_tools/foxctl.py` — `render()` (~line 3091: car card block, anchors below) and the `<h1>` title line (~line 3160)
- Modify: `energy_tools/CHANGELOG.md` (prepend entry)

**Interfaces:**
- Consumes: `snap["predawn_budget"]` (Task 3), `VERSION` (Task 3). Note: the free-window car overlay ALREADY prefers measured session kWh (`meas = car.get("measured_kwh")` ~line 2789) — the spec's "prefer measured draw" item is satisfied; no change there.

- [ ] **Step 1: Add the pre-dawn line to the car card**

In `render()`, just above the `_ac = {"good": ...}` dict (anchor: `# Charge advisor — is now a good time`), add:

```python
    pdb = snap.get("predawn_budget") or {}
    pd_txt = ""
    if isinstance(pdb.get("kwh"), (int, float)) and (pdb.get("active") or pdb.get("in_free_window") is False and pdb.get("kwh") > 0):
        pd_txt = (f' · pre-dawn {pdb["kwh"]:+.1f}kWh vs {pdb.get("floor_soc", 30):.0f}% floor'
                  f' (window {int(pdb.get("window_start_hour", 10)):02d}:00)')
```

Then append `{pd_txt}` inside BOTH car-card variants' status `<small>` line:
- advisor card: change `meter: {snap.get("ev_power_source") or "—"}</small>` → `meter: {snap.get("ev_power_source") or "—"}{pd_txt}</small>`
- fallback card: change `<small>{ev_status}</small>` → `<small>{ev_status}{pd_txt}</small>`

- [ ] **Step 2: Show the version in the page header**

Change `<h1>foxctl <small id=refr ...></small></h1>` to include the constant:

```python
<h1>foxctl <small style="color:#bbb;font-weight:400">v{VERSION}</small> <small id=refr style="color:#888;font-weight:400"></small></h1>
```

(The render template is an f-string — `{VERSION}` interpolates like its neighbours.)

- [ ] **Step 3: CHANGELOG entry**

Prepend to `energy_tools/CHANGELOG.md` under `# Changelog`:

```markdown
## 1.67.0 — pre-dawn battery→car dump + universal SoC floor-guard

The free window refills the battery for ~free, so overnight surplus above a 30% planning
floor now goes into the car pre-dawn instead of sitting unused. And a charge session
started BY HAND in HA — previously invisible to foxctl's edge-triggered switch logic —
gets cut when the projection says the battery won't hold the floor to window-open.

- **Pre-dawn dump** (`ev_predawn_dump`, default on): from `ev_predawn_start_hour` (4) to
  the free-window start, car ON while `(soc − 30%)×capacity − forecast load to window` is
  positive (start needs > `ev_start_margin_kwh`, stop at 0 — same deadband as the outlook
  gate). Battery-only: sustained grid import > `ev_predawn_import_stop_kw` (0.5 kW, 2
  polls) aborts the session.
- **Floor-guard** (`ev_floor_guard`, default on): any hour, any session origin — actual
  draw + guard budget (incl. remaining solar) ≤ 0 outside the free window → switch OFF,
  reason in the events log. UI "Force car charge" is the one exemption;
  `ev_guard_grace_min` (10) spaces repeat cuts after a deliberate re-flip.
- `snap["predawn_budget"]` in `/api/state`, pre-dawn line on the car card, `version` in
  `/api/state` + the page header. New `tests/test_predawn.py` (stdlib unittest).
```

- [ ] **Step 4: Verify**

Run: `python3 -m unittest tests.test_predawn -v && python3 -m py_compile energy_tools/foxctl.py && python3 -c "
import sys; sys.path.insert(0,'energy_tools'); import foxctl
snap={'predawn_budget':{'kwh':2.8,'active':True,'floor_soc':30.0,'window_start_hour':10,'in_free_window':False},'ts':'x'}
html=foxctl.render(snap, foxctl.DEFAULT_CONFIG)
assert 'pre-dawn +2.8kWh' in html and 'v1.67.0' in html, 'card/header missing'
print('render OK')"`
Expected: tests `OK`, then `render OK`. (If `render` requires more snap keys and raises, add the minimal keys it demands — it is defensive `_n()`/`or {}` style throughout, so an empty-ish snap should render.)

- [ ] **Step 5: Commit**

```bash
git add energy_tools/foxctl.py energy_tools/CHANGELOG.md
git commit -m "v1.67.0: pre-dawn dump + floor-guard — dashboard surfacing + changelog" && git push
```

---

### Task 7: Deploy to the Pi 5 + live verification

**Files:** none (operational).

- [ ] **Step 1: Confirm everything is pushed**

Run: `git status --short && git log --oneline -3 origin/main..HEAD`
Expected: clean tree, no unpushed commits (empty range).

- [ ] **Step 2: User deploys on the Pi** (HA Container host — compose, not add-on)

Ask the user to run their compose update on the Pi (typically `docker compose build && docker compose up -d energy_tools` in the compose dir, or their equivalent). If they've given shell access before, check memory/global notes for the exact invocation rather than guessing.

- [ ] **Step 3: Verify the deploy — one curl now**

Run: `curl -s -m 8 http://homeassistant.local:8770/api/state | python3 -c "import json,sys; d=json.load(sys.stdin); print('version:', d.get('version')); print('predawn:', json.dumps(d.get('predawn_budget')))"`
Expected: `version: 1.67.0` and a populated `predawn_budget` block whose `parts` roughly match hand-maths from the live SoC (usable = (soc−30)/100×41.44).

- [ ] **Step 4: Live behaviour checks (spread over the next cycles)**

- **Floor-guard**: with the car plugged in and manually switched on at night while `guard_kwh ≤ 0`, the events log (`/api/events`) shows `car charger OFF (floor-guard: ...)` within one poll (≤5 min) and the switch goes off in HA.
- **Pre-dawn dump**: next morning ≥ 04:00 with `kwh > 1`, events show `car charger ON (pre-dawn surplus +X.XkWh ...)`; it stops with `pre-dawn done` before the window and SoC lands ≈ the floor at window-open.
- **Friday flip**: after `tariff_profile: four4free` goes live, `predawn_budget.window_start_hour` reads `10` with no code change.

- [ ] **Step 5: Record outcome**

Note verification results (what fired, budgets vs actual SoC landing) in the session/memory for tuning — the floor and margins are config, not code, if adjustments are needed.
