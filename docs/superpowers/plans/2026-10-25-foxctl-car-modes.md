# foxctl 1.83.0 — Car Charge Modes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explicit car-charge modes to foxctl (`Auto`, `To coast`, `To full`, `+3/+5/+10 kWh`), selectable from Home Assistant through an MQTT `select`, with a live status sensor, persisted across restarts.

**Architecture:** New pure functions (`car_mode_*`) decide whether the car may draw, when a mode is finished, and what the status line says. A small stateful layer (`_CAR` + `car_mode_tick`) runs inside the existing `ev_divert_tick` ahead of the Auto paths. Commands arrive on the existing persistent paho client (new subscription). The handler only queues the mode and wakes the main loop, so all control runs on the loop thread.

**Tech Stack:** Python 3 (stdlib + paho-mqtt), unittest, Docker compose deploy on the HA Pi.

**Spec:** `~/projects/living-room-climate/docs/superpowers/specs/2026-10-25-wall-panel-design.md` § "3. foxctl car modes"

## Global Constraints

- Option strings are exact: `Auto`, `To coast`, `To full`, `+3 kWh`, `+5 kWh`, `+10 kWh`.
- MQTT: command `foxctl/car_charge/set`; state `foxctl/car_charge/state` (retained); status `foxctl/car_charge/status` (retained); attributes `foxctl/car_charge/attrs` (retained). Discovery object_ids `foxctl_car_charge` (select) and `foxctl_car_charge_status` (sensor), device `foxctl_foxess`.
- Never buy at shoulder or peak: outside the free window a mode only charges while `soc > dynamic.survival_soc` and without sustained import (> `predawn_import_stop_kw`, default 0.5, two checks running).
- The free window is the active tariff profile's `free` block (four4free 10:00–14:00). Never reason with ZeroHero.
- Explicit modes ignore the Auto daily cap (`session_cap_kwh`). They keep the safety holds: battery selling, stale/down telemetry, paid-power battery force-charge below target − 5 %.
- A mode expires after `ev_mode_expiry_h` (36) hours. To full's no-draw rule is `ev_mode_full_nodraw_min` (10) minutes.
- Latest wins: a mode cancels a manual force charge (`_EV["override_until"] = 0`). `/api/ev_charge` and `/api/ev_off` reset the mode to `Auto`.
- Repo conventions: trunk-based commits to `main` with the Co-Authored-By trailer; bump `VERSION` in `foxctl.py` and `version` in `config.yaml`; add a CHANGELOG entry. Deploy is the scoped `energy_tools/` rsync (never the whole-repo `--delete`).
- The test baseline has 62 known stale failures/errors. A change must add no new ones.

## Review Focus

1. **Restart mid-mode while `energy_totals.ev` is still `None`.** The first cycles can lack the EV meter total. The mode must neither finish nor show garbage; delivered shows `?` until the total arrives. → Task 1 test `test_done_unknown_delivered_is_not_done`, Task 2 test `test_restart_resumes_mode_from_disk`.
2. **The EV meter total goes backwards (sensor reset).** Delivered must never go negative; rebase the start. → Task 2 test `test_meter_reset_rebases_start`.
3. **A stale car SoC of 100 % from a sleeping WiCAN.** To full must not finish on it. → Task 1 test `test_full_ignores_stale_car_soc`.
4. **Junk or near-miss MQTT payloads** (`auto`, `+4 kWh`, empty, bytes). Ignore them, change nothing. → Task 1 test `test_parse_rejects_near_misses`.
5. **A new mode chosen while one is running** (+5 → +10, or +5 again). It restarts counting from now. → Task 2 test `test_reselect_restarts_count`.

---

### Task 1: Pure car-mode functions

**Files:**
- Modify: `energy_tools/foxctl.py`. Insert a new section directly after `ev_divert_decision` (before `def ev_divert_tick`). Add `Event` to the threading import (line 40: `from threading import Lock, Thread` → `from threading import Event, Lock, Thread`).
- Test: `tests/test_foxctl.py`. Add a `CarModePureTest` class after `EvSurplusAndKeepFloorTest`.

**Interfaces:**
- Consumes: `_in_window(free, h)`, `_gp_now(snap)`, `_car_draw_est(snap)`, `ev_solar_surplus_kw(snap, car_on)`, `_FILL`, `datetime` (patched by `_frozen_clock` in tests).
- Produces:
  - `CAR_MODES: tuple[str, ...]`
  - `car_mode_parse(payload) -> str | None`
  - `car_mode_target_kwh(mode: str) -> float | None`
  - `car_mode_allowed(snap: dict, ev: dict, charging: bool, import_hits: int) -> tuple[bool, str, str | None]`, returning (ok, reason, source); source ∈ `"free window"`, `"solar"`, `"battery"` when ok.
  - `car_mode_done(mode, *, delivered_kwh, soc, floor_soc, in_free, car_soc, car_soc_age_min, nodraw_min, free_window_ended, nodraw_limit_min=10) -> tuple[bool, str]`
  - `car_mode_status(mode, *, phase, delivered_kwh, source, reason, soc, floor_soc, car_soc, free_start_h) -> str`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_foxctl.py` (before `class EvOutlookBypassTest`):

```python
class CarModePureTest(unittest.TestCase):
    """Explicit car modes (1.83.0): parsing, when the car may draw, when a mode is done,
    and the status line the wall panel shows."""

    EV = {"supply_cap_kw": 14.5, "headroom_guard_kw": 0.8, "predawn_import_stop_kw": 0.5}

    def setUp(self):
        self._fill_plan = foxctl._FILL.get("plan")
        foxctl._FILL["plan"] = None            # no planner verdict leaking in from other tests

    def tearDown(self):
        foxctl._FILL["plan"] = self._fill_plan

    def _snap(self, **kw):
        s = {"soc": 70, "pv_kw": 0.0, "load_kw": 1.0, "ev_kw": 0.0, "feedin_power": 0.0,
             "battery_power": -1.0, "telemetry_source": "Modbus", "grid_power": 0.0,
             "dynamic": {"tariff": copy.deepcopy(FOUR4FREE), "survival_soc": 41, "target_soc": 100},
             "money": {"free_left_kwh": 40.0}, "car": {"sessions": [{"peak_kw": 1.9}]}}
        s.update(kw)
        return s

    def test_parse_accepts_exact_options(self):
        for m in foxctl.CAR_MODES:
            self.assertEqual(foxctl.car_mode_parse(m.encode()), m)

    def test_parse_rejects_near_misses(self):
        for junk in (b"auto", b"+4 kWh", b"", b"  ", None, b"To Full", "+5kWh"):
            self.assertIsNone(foxctl.car_mode_parse(junk))
        self.assertEqual(foxctl.car_mode_parse(b" +5 kWh \n"), "+5 kWh")

    def test_target_kwh(self):
        self.assertEqual(foxctl.car_mode_target_kwh("+3 kWh"), 3.0)
        self.assertEqual(foxctl.car_mode_target_kwh("+10 kWh"), 10.0)
        self.assertIsNone(foxctl.car_mode_target_kwh("To full"))
        self.assertIsNone(foxctl.car_mode_target_kwh("Auto"))

    def test_allowed_on_battery_above_coast_floor(self):
        with _frozen_clock(19):
            ok, why, src = foxctl.car_mode_allowed(self._snap(soc=64), self.EV, False, 0)
        self.assertTrue(ok, why)
        self.assertEqual(src, "battery")

    def test_paused_at_coast_floor(self):
        with _frozen_clock(19):
            ok, why, src = foxctl.car_mode_allowed(self._snap(soc=41), self.EV, True, 0)
        self.assertFalse(ok)
        self.assertIn("coast floor", why)

    def test_paused_on_sustained_import(self):
        with _frozen_clock(19):
            ok, why, _ = foxctl.car_mode_allowed(self._snap(soc=80), self.EV, True, 2)
        self.assertFalse(ok)
        self.assertIn("importing", why)

    def test_free_window_allowed_even_low_battery(self):
        with _frozen_clock(11):
            ok, why, src = foxctl.car_mode_allowed(self._snap(soc=20, grid_power=3.0), self.EV, False, 0)
        self.assertTrue(ok, why)
        self.assertEqual(src, "free window")

    def test_free_window_respects_supply_cap(self):
        with _frozen_clock(11):
            ok, why, _ = foxctl.car_mode_allowed(self._snap(grid_power=13.0), self.EV, False, 0)
        self.assertFalse(ok)
        self.assertIn("supply", why)

    def test_free_allowance_used_up(self):
        with _frozen_clock(11):
            ok, why, _ = foxctl.car_mode_allowed(self._snap(money={"free_left_kwh": 0.5}), self.EV, False, 0)
        self.assertFalse(ok)
        self.assertIn("free allowance", why)

    def test_safety_holds(self):
        with _frozen_clock(19):
            self.assertFalse(foxctl.car_mode_allowed(
                self._snap(recommendation={"force_discharge": True}), self.EV, False, 0)[0])
            self.assertFalse(foxctl.car_mode_allowed(
                self._snap(telemetry_source="Modbus (stale)"), self.EV, False, 0)[0])
            self.assertFalse(foxctl.car_mode_allowed(
                self._snap(soc=60, recommendation={"force_charge": True}), self.EV, False, 0)[0])

    def test_solar_source_when_sun_covers_most_of_the_car(self):
        with _frozen_clock(15):
            ok, _, src = foxctl.car_mode_allowed(
                self._snap(soc=95, feedin_power=0.0, ev_kw=1.9, battery_power=-0.3), self.EV, True, 0)
        self.assertTrue(ok)
        self.assertEqual(src, "solar")

    def _done(self, mode, **kw):
        base = dict(delivered_kwh=0.0, soc=70, floor_soc=41, in_free=False, car_soc=None,
                    car_soc_age_min=None, nodraw_min=0.0, free_window_ended=False)
        base.update(kw)
        return foxctl.car_mode_done(mode, **base)

    def test_kwh_mode_done_at_target(self):
        self.assertFalse(self._done("+5 kWh", delivered_kwh=4.9)[0])
        self.assertTrue(self._done("+5 kWh", delivered_kwh=4.96)[0])

    def test_done_unknown_delivered_is_not_done(self):
        self.assertFalse(self._done("+3 kWh", delivered_kwh=None)[0])

    def test_full_done_on_fresh_car_soc_or_no_draw(self):
        self.assertTrue(self._done("To full", car_soc=99.6, car_soc_age_min=12)[0])
        self.assertTrue(self._done("To full", nodraw_min=10.5)[0])
        self.assertFalse(self._done("To full", nodraw_min=6)[0])

    def test_full_ignores_stale_car_soc(self):
        self.assertFalse(self._done("To full", car_soc=100, car_soc_age_min=95)[0])
        self.assertFalse(self._done("To full", car_soc=100, car_soc_age_min=None)[0])

    def test_coast_done_at_floor_outside_free_window(self):
        self.assertTrue(self._done("To coast", soc=41)[0])
        self.assertFalse(self._done("To coast", soc=41, in_free=True)[0])
        self.assertTrue(self._done("To coast", free_window_ended=True)[0])
        self.assertTrue(self._done("To coast", nodraw_min=11)[0])

    def test_status_lines(self):
        s = foxctl.car_mode_status("+5 kWh", phase="charging", delivered_kwh=2.14, source="solar",
                                   reason="", soc=95, floor_soc=41, car_soc=None, free_start_h=10)
        self.assertEqual(s, "+5 kWh · 2.1 of 5 · on solar")
        s = foxctl.car_mode_status("+5 kWh", phase="paused", delivered_kwh=2.1, source=None,
                                   reason="battery at coast floor (41%)", soc=41, floor_soc=41,
                                   car_soc=None, free_start_h=10)
        self.assertEqual(s, "+5 kWh · 2.1 of 5 · paused · finishes free at 10:00")
        s = foxctl.car_mode_status("To coast", phase="charging", delivered_kwh=3.2, source="battery",
                                   reason="", soc=58, floor_soc=41, car_soc=None, free_start_h=10)
        self.assertEqual(s, "To coast · 3.2 kWh · battery 58% → floor 41%")
        s = foxctl.car_mode_status("To full", phase="charging", delivered_kwh=1.0, source="free window",
                                   reason="", soc=80, floor_soc=41, car_soc=74.2, free_start_h=10)
        self.assertEqual(s, "To full · car 74% · free window")
        s = foxctl.car_mode_status("+3 kWh", phase="charging", delivered_kwh=None, source="battery",
                                   reason="", soc=80, floor_soc=41, car_soc=None, free_start_h=10)
        self.assertEqual(s, "+3 kWh · ? of 3 · on battery")
        for st in (s,):
            self.assertLessEqual(len(st), 60)
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `cd ~/projects/ha-energy-tools && python3 -m unittest tests.test_foxctl.CarModePureTest 2>&1 | tail -3`
Expected: `FAILED (errors=…)` with `AttributeError: module 'foxctl' has no attribute 'CAR_MODES'`.

- [ ] **Step 3: Implement the pure functions**

In `energy_tools/foxctl.py`, change line 40 to `from threading import Event, Lock, Thread`, then insert before `def ev_divert_tick(cfg, snap):`:

```python
# ------------------------------------------------------------ car modes --
# Explicit car-charge modes (1.83.0, wall panel spec 2026-10-25): chosen from HA via the MQTT
# select foxctl_car_charge. Outside the free window they only ever spend spare battery above the
# coast floor (survival_soc) or sun — never shoulder/peak grid; the free window finishes the rest.
CAR_MODES = ("Auto", "To coast", "To full", "+3 kWh", "+5 kWh", "+10 kWh")


def car_mode_parse(payload):
    """MQTT payload (bytes/str) -> an exact CAR_MODES entry, else None. Pure."""
    if payload is None:
        return None
    s = payload.decode("utf-8", "replace") if isinstance(payload, (bytes, bytearray)) else str(payload)
    s = s.strip()
    return s if s in CAR_MODES else None


def car_mode_target_kwh(mode):
    """'+5 kWh' -> 5.0; every other mode -> None. Pure."""
    if mode.startswith("+") and mode.endswith(" kWh"):
        try:
            return float(mode[1:-4])
        except ValueError:
            return None
    return None


def car_mode_allowed(snap, ev, charging, import_hits):
    """May the car draw NOW under an explicit mode? Pure. Returns (ok, reason, source) with
    source in {"free window", "solar", "battery"} when ok. Order: safety holds, free window
    (supply cap + free allowance), then battery above the coast floor without sustained import."""
    rec = snap.get("recommendation") or {}
    active = (snap.get("scheduler") or {}).get("active") or {}
    if rec.get("force_discharge") or active.get("mode") == "ForceDischarge":
        return False, "battery is selling to grid", None
    ts = snap.get("telemetry_source") or ""
    if "stale" in ts or "down" in ts:
        return False, "telemetry stale", None
    dyn = snap.get("dynamic") or {}
    soc = snap.get("soc")
    free = (dyn.get("tariff") or {}).get("free") or {}
    now = datetime.now()
    in_free = bool(free) and _in_window(free, now.hour + now.minute / 60.0)
    target = dyn.get("target_soc")
    if (not in_free and (rec.get("force_charge") or active.get("mode") == "ForceCharge")
            and isinstance(soc, (int, float)) and isinstance(target, (int, float)) and soc < target - 5):
        return False, f"battery force-charging to {target:.0f}%", None
    if in_free:
        free_left = (snap.get("money") or {}).get("free_left_kwh")
        if isinstance(free_left, (int, float)) and free_left <= 1:
            return False, "free allowance used up", None
        plan = _FILL.get("plan")
        if plan and time.time() - (plan.get("ts") or 0) < 600 and not plan.get("car_ok") and not charging:
            return False, "no supply headroom while the battery fills", None
        cap_kw = float(ev.get("supply_cap_kw", 14.5) or 0.0)
        gp = _gp_now(snap)
        if cap_kw > 0 and isinstance(gp, (int, float)):
            guard = float(ev.get("headroom_guard_kw", 0.8) or 0.0)
            if charging and gp > cap_kw:
                return False, f"import {gp:.1f}kW over the {cap_kw:g}kW supply cap", None
            if not charging and gp + _car_draw_est(snap) + guard > cap_kw:
                return False, "no supply headroom while the battery fills", None
        return True, "free window", "free window"
    floor = dyn.get("survival_soc")
    if not isinstance(soc, (int, float)) or not isinstance(floor, (int, float)):
        return False, "battery level unknown", None
    if soc <= floor:
        return False, f"battery at coast floor ({floor:.0f}%)", None
    if import_hits >= 2:
        return False, "importing from grid", None
    surplus = ev_solar_surplus_kw(snap, charging)
    source = "solar" if surplus >= 0.5 * _car_draw_est(snap) else "battery"
    return True, "spare " + source, source


def car_mode_done(mode, *, delivered_kwh, soc, floor_soc, in_free, car_soc, car_soc_age_min,
                  nodraw_min, free_window_ended, nodraw_limit_min=10):
    """Has an explicit mode finished? Pure. Returns (done, why)."""
    target = car_mode_target_kwh(mode)
    if target is not None:
        if isinstance(delivered_kwh, (int, float)) and delivered_kwh >= target - 0.05:
            return True, f"{delivered_kwh:.1f} kWh delivered"
        return False, ""
    fresh_full = (isinstance(car_soc, (int, float)) and car_soc >= 99.5
                  and isinstance(car_soc_age_min, (int, float)) and car_soc_age_min <= 30)
    car_full = fresh_full or (nodraw_min or 0.0) >= nodraw_limit_min
    if mode == "To full":
        return (True, "car full") if car_full else (False, "")
    if mode == "To coast":
        if car_full:
            return True, "car full"
        if (not in_free and isinstance(soc, (int, float)) and isinstance(floor_soc, (int, float))
                and soc <= floor_soc):
            return True, f"battery reached the coast floor ({floor_soc:.0f}%)"
        if free_window_ended:
            return True, "free window over"
    return False, ""


def car_mode_status(mode, *, phase, delivered_kwh, source, reason, soc, floor_soc, car_soc,
                    free_start_h):
    """One-line status for the panel / HA (<= 60 chars). Pure."""
    d = f"{delivered_kwh:.1f}" if isinstance(delivered_kwh, (int, float)) else "?"
    target = car_mode_target_kwh(mode)
    if target is not None:
        head = f"{mode} · {d} of {target:g}"
    elif mode == "To coast":
        head = f"To coast · {d} kWh"
    elif mode == "To full":
        head = (f"To full · car {car_soc:.0f}%" if isinstance(car_soc, (int, float))
                else f"To full · {d} kWh")
    else:
        head = mode
    if phase == "paused":
        if "coast floor" in reason or "importing" in reason:
            tail = f"paused · finishes free at {int(free_start_h):02d}:00"
        else:
            tail = f"paused · {reason}"
        return f"{head} · {tail}"[:60]
    if mode == "To coast" and isinstance(soc, (int, float)) and isinstance(floor_soc, (int, float)):
        return f"{head} · battery {soc:.0f}% → floor {floor_soc:.0f}%"[:60]
    where = "free window" if source == "free window" else f"on {source}"
    return f"{head} · {where}"[:60]
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `python3 -m unittest tests.test_foxctl.CarModePureTest 2>&1 | tail -3`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add energy_tools/foxctl.py tests/test_foxctl.py
git commit -m "foxctl: pure car-mode functions (parse, allowed, done, status)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Car-mode state, persistence and tick integration

**Files:**
- Modify: `energy_tools/foxctl.py`:
  - add `_CAR`, `_CAR_LOCK`, `_WAKE` and the stateful functions after the Task 1 section;
  - in `ev_divert_tick`, call `car_mode_tick` and branch on it;
  - in `DEFAULT_CONFIG["ev_divert"]`, add `"mode_expiry_h": 36, "mode_full_nodraw_min": 10`.
- Modify: `energy_tools/build_config.py`: add the two options to `fc["ev_divert"]`.
- Modify: `energy_tools/config.yaml`: add options `ev_mode_expiry_h: 36` and `ev_mode_full_nodraw_min: 10`, plus schema `ev_mode_expiry_h: float` and `ev_mode_full_nodraw_min: float`.
- Test: `tests/test_foxctl.py`. Add a `CarModeTickTest` class after `CarModePureTest`.

**Interfaces:**
- Consumes: everything from Task 1; `_EV`, `_state_dir(cfg)`, `log_event`, `ha_call_service`.
- Produces:
  - `_CAR: dict` with keys `mode, start_kwh, started, pending, loaded, import_hits, nodraw_since, seen_free, status, attrs`
  - `_WAKE: threading.Event`
  - `car_mode_request(mode: str) -> None` (thread-safe; queues and wakes the loop)
  - `car_mode_set(cfg, mode, ev_cum, why, now=None) -> None` (applies and persists immediately; used by the HTTP handlers)
  - `car_mode_load(cfg) -> None`
  - `car_mode_tick(cfg, snap, ev, now=None) -> tuple[bool, str] | None` (`None` while the mode is Auto)

- [ ] **Step 1: Write the failing tests**

Append after `CarModePureTest`:

```python
class CarModeTickTest(unittest.TestCase):
    """Stateful car-mode layer: commands, persistence, finishing, and how it sits in ev_divert_tick."""

    def setUp(self):
        self._orig = foxctl.ha_call_service
        self.calls = []
        foxctl.ha_call_service = lambda cfg, d, s, e: self.calls.append(s)
        self._ev_state = dict(foxctl._EV)
        foxctl._EV.update({"on": None, "last_change": 0.0, "override_until": 0.0,
                           "session_day": None, "session_start_kwh": None, "capped": False,
                           "guard_cut_ts": 0.0, "lowdraw_since": 0.0, "import_hits": 0,
                           "predawn_parked_day": None})
        self._car_state = dict(foxctl._CAR)
        foxctl._CAR.update({"mode": "Auto", "start_kwh": None, "started": 0.0, "pending": None,
                            "loaded": True, "import_hits": 0, "nodraw_since": 0.0,
                            "seen_free": False, "status": "", "attrs": {}})
        self.tmp = tempfile.mkdtemp()
        self.cfg = {"control": {"allow_control": True}, "state_dir": self.tmp,
                    "ev_divert": {"switch": "switch.x", "min_export_kw": 1.0, "min_export_free_kw": 0.3,
                                  "min_dwell_min": 5, "battery_priority": False, "min_soc": 0,
                                  "session_cap_kwh": 30, "predawn_dump": False, "floor_guard": False}}

    def tearDown(self):
        foxctl.ha_call_service = self._orig
        foxctl._EV.clear(); foxctl._EV.update(self._ev_state)
        foxctl._CAR.clear(); foxctl._CAR.update(self._car_state)
        foxctl._WAKE.clear()

    def _snap(self, ev_cum=100.0, **kw):
        s = {"soc": 70, "pv_kw": 0.0, "load_kw": 1.0, "ev_kw": 0.0, "feedin_power": 0.0,
             "battery_power": -1.0, "telemetry_source": "Modbus", "grid_power": 0.0,
             "dynamic": {"tariff": copy.deepcopy(FOUR4FREE), "survival_soc": 41, "target_soc": 100},
             "money": {"free_left_kwh": 40.0}, "car": {"sessions": [{"peak_kw": 1.9}]},
             "energy_totals": {"ev": ev_cum}}
        s.update(kw)
        return s

    def test_request_then_tick_starts_mode_and_switches_on_now(self):
        foxctl.car_mode_request("+5 kWh")
        foxctl._EV["last_change"] = time.time()          # dwell would normally block a switch
        with _frozen_clock(19):
            msg = foxctl.ev_divert_tick(self.cfg, self._snap())
        self.assertEqual(foxctl._CAR["mode"], "+5 kWh")
        self.assertEqual(foxctl._CAR["start_kwh"], 100.0)
        self.assertIn("turn_on", self.calls)            # mode start bypasses the dwell
        self.assertIn("+5 kWh", msg)
        self.assertTrue(Path(self.tmp, "car_mode.json").exists())

    def test_mode_overrides_auto_and_daily_cap(self):
        foxctl._EV["capped"] = True
        foxctl.car_mode_set(self.cfg, "+3 kWh", 100.0, "test")
        with _frozen_clock(19):
            foxctl.ev_divert_tick(self.cfg, self._snap())
        self.assertTrue(foxctl._EV["on"])

    def test_kwh_mode_finishes_and_returns_to_auto(self):
        foxctl.car_mode_set(self.cfg, "+3 kWh", 100.0, "test")
        with _frozen_clock(19):
            foxctl.ev_divert_tick(self.cfg, self._snap(ev_cum=101.0))
            self.assertEqual(foxctl._CAR["mode"], "+3 kWh")
            foxctl.ev_divert_tick(self.cfg, self._snap(ev_cum=103.1))
        self.assertEqual(foxctl._CAR["mode"], "Auto")

    def test_pauses_at_coast_floor_and_reports_free_window(self):
        foxctl.car_mode_set(self.cfg, "+5 kWh", 100.0, "test")
        with _frozen_clock(19):
            foxctl.ev_divert_tick(self.cfg, self._snap(soc=41))
        self.assertFalse(foxctl._EV["on"])
        self.assertIn("finishes free at 10:00", foxctl._CAR["status"])
        self.assertEqual(foxctl._CAR["attrs"]["phase"], "paused")

    def test_import_guard_needs_two_checks(self):
        foxctl.car_mode_set(self.cfg, "+5 kWh", 100.0, "test")
        with _frozen_clock(19):
            foxctl.ev_divert_tick(self.cfg, self._snap())                  # on
            foxctl._EV["last_change"] = 0.0
            foxctl.ev_divert_tick(self.cfg, self._snap(grid_power=0.9))    # 1st import hit
            self.assertTrue(foxctl._EV["on"])
            foxctl._EV["last_change"] = 0.0
            foxctl.ev_divert_tick(self.cfg, self._snap(grid_power=0.9))    # 2nd → pause
        self.assertFalse(foxctl._EV["on"])

    def test_restart_resumes_mode_from_disk(self):
        foxctl.car_mode_set(self.cfg, "+10 kWh", 95.0, "test")
        foxctl._CAR.update({"mode": "Auto", "loaded": False, "start_kwh": None})   # fresh process
        foxctl.car_mode_load(self.cfg)
        self.assertEqual(foxctl._CAR["mode"], "+10 kWh")
        self.assertEqual(foxctl._CAR["start_kwh"], 95.0)
        with _frozen_clock(19):
            foxctl.ev_divert_tick(self.cfg, self._snap(ev_cum=None))       # totals not loaded yet
        self.assertEqual(foxctl._CAR["mode"], "+10 kWh")
        self.assertIn("? of 10", foxctl._CAR["status"])

    def test_meter_reset_rebases_start(self):
        foxctl.car_mode_set(self.cfg, "+5 kWh", 500.0, "test")
        with _frozen_clock(19):
            foxctl.ev_divert_tick(self.cfg, self._snap(ev_cum=2.0))         # counter went backwards
        self.assertEqual(foxctl._CAR["start_kwh"], 2.0)
        self.assertEqual(foxctl._CAR["attrs"]["delivered_kwh"], 0.0)

    def test_reselect_restarts_count(self):
        foxctl.car_mode_set(self.cfg, "+5 kWh", 100.0, "test")
        foxctl.car_mode_request("+10 kWh")
        with _frozen_clock(19):
            foxctl.ev_divert_tick(self.cfg, self._snap(ev_cum=103.0))
        self.assertEqual(foxctl._CAR["mode"], "+10 kWh")
        self.assertEqual(foxctl._CAR["start_kwh"], 103.0)

    def test_mode_cancels_force_and_expires(self):
        foxctl._EV["override_until"] = time.time() + 3600
        foxctl.car_mode_request("To full")
        with _frozen_clock(19):
            foxctl.ev_divert_tick(self.cfg, self._snap())
        self.assertEqual(foxctl._EV["override_until"], 0.0)
        foxctl._CAR["started"] = time.time() - 37 * 3600
        with _frozen_clock(19):
            foxctl.ev_divert_tick(self.cfg, self._snap())
        self.assertEqual(foxctl._CAR["mode"], "Auto")

    def test_auto_returns_none_from_car_mode_tick(self):
        self.assertIsNone(foxctl.car_mode_tick(self.cfg, self._snap(), self.cfg["ev_divert"]))
```

Also add `import time` to the test module imports if it isn't there yet (check the top of the file; `time` is used above).

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `python3 -m unittest tests.test_foxctl.CarModeTickTest 2>&1 | tail -3`
Expected: errors such as `AttributeError: module 'foxctl' has no attribute 'car_mode_request'`.

- [ ] **Step 3: Implement the state layer**

Insert after the Task 1 functions (still before `def ev_divert_tick`):

```python
_CAR = {"mode": "Auto", "start_kwh": None, "started": 0.0, "pending": None, "loaded": False,
        "import_hits": 0, "nodraw_since": 0.0, "seen_free": False, "status": "", "attrs": {}}
_CAR_LOCK = Lock()
_WAKE = Event()     # set by the MQTT command handler: cut the main loop's sleep short


def _car_path(cfg):
    return _state_dir(cfg) / "car_mode.json"


def _car_save(cfg):
    try:
        p = _car_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: _CAR[k] for k in ("mode", "start_kwh", "started")}))
        tmp.replace(p)
    except Exception as e:
        print(f"car mode save failed: {e}", file=sys.stderr)


def car_mode_load(cfg):
    """Restore a running mode after a restart (once). Unknown/corrupt -> Auto."""
    if _CAR.get("loaded"):
        return
    _CAR["loaded"] = True
    try:
        d = json.loads(_car_path(cfg).read_text())
        if d.get("mode") in CAR_MODES:
            _CAR.update({"mode": d["mode"], "start_kwh": d.get("start_kwh"),
                         "started": float(d.get("started") or 0.0)})
    except Exception:
        pass


def car_mode_request(mode):
    """Thread-safe: queue a mode chosen over MQTT and wake the main loop to apply it."""
    with _CAR_LOCK:
        _CAR["pending"] = mode
    _WAKE.set()


def car_mode_set(cfg, mode, ev_cum, why, now=None):
    """Apply a mode now (loop thread or HTTP handler). Starting a mode cancels a manual force
    charge and lets the next switch call skip the dwell."""
    now = time.time() if now is None else now
    with _CAR_LOCK:
        _CAR.update({"mode": mode, "start_kwh": ev_cum if mode != "Auto" else None,
                     "started": now if mode != "Auto" else 0.0, "import_hits": 0,
                     "nodraw_since": 0.0, "seen_free": False, "loaded": True})
    if mode != "Auto":
        _EV["override_until"] = 0.0
    _EV["last_change"] = 0.0
    _car_save(cfg)
    log_event("ev_divert", f"car mode → {mode} ({why})")


def car_mode_tick(cfg, snap, ev, now=None):
    """Run the explicit mode for this cycle. Returns (want, why) while a mode is active,
    or None when the mode is Auto (ev_divert_tick then runs the Auto paths)."""
    now = time.time() if now is None else now
    car_mode_load(cfg)
    ev_cum = (snap.get("energy_totals") or {}).get("ev")
    ev_cum = float(ev_cum) if isinstance(ev_cum, (int, float)) else None
    with _CAR_LOCK:
        pending, _CAR["pending"] = _CAR["pending"], None
    if pending:
        car_mode_set(cfg, pending, ev_cum, "selected in HA", now)
    mode = _CAR["mode"]
    dyn = snap.get("dynamic") or {}
    free = (dyn.get("tariff") or {}).get("free") or {}
    free_start_h = free.get("start", 10)
    if mode == "Auto":
        _CAR["attrs"] = {"mode": "Auto", "phase": "idle"}
        return None
    expiry_h = float(ev.get("mode_expiry_h", 36) or 36)
    if now - (_CAR.get("started") or now) > expiry_h * 3600:
        car_mode_set(cfg, "Auto", ev_cum, f"expired after {expiry_h:g}h", now)
        return None
    if _CAR.get("start_kwh") is None and ev_cum is not None:
        _CAR["start_kwh"] = ev_cum
        _car_save(cfg)
    if ev_cum is not None and _CAR["start_kwh"] is not None and ev_cum < _CAR["start_kwh"]:
        _CAR["start_kwh"] = ev_cum          # meter went backwards (reset): rebase, never negative
        _car_save(cfg)
    delivered = (round(ev_cum - _CAR["start_kwh"], 2)
                 if ev_cum is not None and _CAR["start_kwh"] is not None else None)
    charging = bool(_EV.get("on"))
    nowd = datetime.now()
    in_free = bool(free) and _in_window(free, nowd.hour + nowd.minute / 60.0)
    if in_free:
        _CAR["seen_free"] = True
    gp = _gp_now(snap)
    stop_kw = float(ev.get("predawn_import_stop_kw", 0.5) or 0.0)
    if charging and not in_free and isinstance(gp, (int, float)) and gp > stop_kw:
        _CAR["import_hits"] = _CAR.get("import_hits", 0) + 1
    else:
        _CAR["import_hits"] = 0
    ok, reason, source = car_mode_allowed(snap, ev, charging, _CAR["import_hits"])
    ev_kw = snap.get("ev_kw")
    if charging and ok and isinstance(ev_kw, (int, float)) and ev_kw < 0.1:
        _CAR["nodraw_since"] = _CAR.get("nodraw_since") or now
    else:
        _CAR["nodraw_since"] = 0.0
    nodraw_min = (now - _CAR["nodraw_since"]) / 60.0 if _CAR["nodraw_since"] else 0.0
    ct = ((snap.get("car") or {}).get("target") or {})
    car_soc = (snap.get("car") or {}).get("soc")
    done, done_why = car_mode_done(
        mode, delivered_kwh=delivered, soc=snap.get("soc"), floor_soc=dyn.get("survival_soc"),
        in_free=in_free, car_soc=car_soc, car_soc_age_min=ct.get("soc_age_min"),
        nodraw_min=nodraw_min, free_window_ended=_CAR["seen_free"] and not in_free,
        nodraw_limit_min=float(ev.get("mode_full_nodraw_min", 10) or 10))
    if done:
        car_mode_set(cfg, "Auto", ev_cum, f"{mode} done: {done_why}", now)
        return None
    phase = "charging" if ok else "paused"
    _CAR["status"] = car_mode_status(mode, phase=phase, delivered_kwh=delivered, source=source,
                                     reason=reason, soc=snap.get("soc"),
                                     floor_soc=dyn.get("survival_soc"), car_soc=car_soc,
                                     free_start_h=free_start_h)
    _CAR["attrs"] = {"mode": mode, "phase": phase, "delivered_kwh": delivered,
                     "target_kwh": car_mode_target_kwh(mode), "car_soc": car_soc,
                     "floor_soc": dyn.get("survival_soc"), "reason": reason,
                     "started": datetime.fromtimestamp(_CAR["started"]).isoformat(timespec="seconds")}
    return ok, _CAR["status"]
```

- [ ] **Step 4: Wire it into `ev_divert_tick`**

In `ev_divert_tick`, directly after the `day = …` / session-cap bookkeeping and before `if now < _EV.get("override_until", 0):`, add:

```python
    car = car_mode_tick(cfg, snap, ev, now)           # explicit car mode (None while Auto)
```

Then change the override/else structure from:

```python
    if now < _EV.get("override_until", 0):        # manual force-charge: ignore economics, dwell, battery gate, cap
        want, why = True, f"manual force-charge ({int((_EV['override_until'] - now) / 60)}min left)"
    else:
        want, why = ev_divert_decision(snap, ev)
```

to:

```python
    if now < _EV.get("override_until", 0):        # manual force-charge: ignore economics, dwell, battery gate, cap
        want, why = True, f"manual force-charge ({int((_EV['override_until'] - now) / 60)}min left)"
    elif car is not None:                         # explicit car mode owns the charger
        want, why = car
    else:
        want, why = ev_divert_decision(snap, ev)
```

(The rest of the `else:` block stays as is, indented under `else:`.) In Auto mode the status line mirrors the Auto reason: at the end of `ev_divert_tick`, just before `return f"car charger …"`, add:

```python
    if car is None:
        _CAR["status"] = ("Auto · " + why)[:60]
```

- [ ] **Step 5: Add the config defaults and options**

In `DEFAULT_CONFIG["ev_divert"]` (next to `"keep_soc_mild": 90, "keep_soc_hvac": 95,`), add:

```python
                  # Explicit car modes (1.83.0): backstop expiry, and To full's "car stopped drawing" rule.
                  "mode_expiry_h": 36, "mode_full_nodraw_min": 10,
```

In `energy_tools/build_config.py`, after the `keep_soc_hvac` line:

```python
    "mode_expiry_h": float(opt.get("ev_mode_expiry_h", 36)),
    "mode_full_nodraw_min": float(opt.get("ev_mode_full_nodraw_min", 10)),
```

In `energy_tools/config.yaml`, after `ev_keep_soc_hvac: 95` in options add `ev_mode_expiry_h: 36` and `ev_mode_full_nodraw_min: 10`. In the schema, after `ev_keep_soc_hvac: float`, add `ev_mode_expiry_h: float` and `ev_mode_full_nodraw_min: float`.

- [ ] **Step 6: Run the tests and confirm they pass, then run the EV regression set**

Run: `python3 -m unittest tests.test_foxctl.CarModeTickTest tests.test_foxctl.CarModePureTest tests.test_foxctl.EvDivertTest tests.test_foxctl.EvRunningSolarSessionTest tests.test_foxctl.EvSurplusAndKeepFloorTest tests.test_foxctl.EvOutlookBypassTest tests.test_foxctl.EvDailyCapTest 2>&1 | tail -3`
Expected: `OK`.

- [ ] **Step 7: Commit**

```bash
git add energy_tools/foxctl.py energy_tools/build_config.py energy_tools/config.yaml tests/test_foxctl.py
git commit -m "foxctl: car-mode state, persistence and ev_divert_tick integration

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: MQTT select + status sensor, loop wake, HTTP force interplay

**Files:**
- Modify: `energy_tools/foxctl.py`:
  - `mqtt_publish`: callbacks, discovery, per-cycle car publish;
  - `loop`: wake on `_WAKE`;
  - the `/api/ev_charge` and `/api/ev_off` handlers: reset the mode to Auto.
- Test: `tests/test_foxctl.py`. Add a `CarModeMqttTest` class.

**Interfaces:**
- Consumes: `car_mode_parse`, `car_mode_request`, `car_mode_set`, `_CAR`, `_WAKE`, `_ENERGY`.
- Produces: `_car_on_message(client, userdata, msg)`, `_car_on_connect(client, *args)`, `car_mode_discovery() -> list[tuple[str, dict]]` returning (topic, config) pairs.

- [ ] **Step 1: Write the failing tests**

```python
class CarModeMqttTest(unittest.TestCase):
    """MQTT glue: discovery payloads, command handling, and that commands only queue + wake."""

    def setUp(self):
        self._car_state = dict(foxctl._CAR)
        foxctl._CAR["pending"] = None
        foxctl._WAKE.clear()

    def tearDown(self):
        foxctl._CAR.clear(); foxctl._CAR.update(self._car_state)
        foxctl._WAKE.clear()

    def test_discovery_has_select_and_status_sensor(self):
        disc = dict(foxctl.car_mode_discovery())
        sel = disc["homeassistant/select/foxctl_car_charge/config"]
        self.assertEqual(sel["command_topic"], "foxctl/car_charge/set")
        self.assertEqual(sel["state_topic"], "foxctl/car_charge/state")
        self.assertEqual(sel["options"], list(foxctl.CAR_MODES))
        sen = disc["homeassistant/sensor/foxctl_car_charge_status/config"]
        self.assertEqual(sen["state_topic"], "foxctl/car_charge/status")
        self.assertEqual(sen["json_attributes_topic"], "foxctl/car_charge/attrs")

    def test_valid_command_queues_and_wakes(self):
        msg = mock.Mock(payload=b"+5 kWh", topic="foxctl/car_charge/set")
        foxctl._car_on_message(None, None, msg)
        self.assertEqual(foxctl._CAR["pending"], "+5 kWh")
        self.assertTrue(foxctl._WAKE.is_set())

    def test_junk_command_is_ignored(self):
        msg = mock.Mock(payload=b"+4 kWh", topic="foxctl/car_charge/set")
        foxctl._car_on_message(None, None, msg)
        self.assertIsNone(foxctl._CAR["pending"])
        self.assertFalse(foxctl._WAKE.is_set())

    def test_on_connect_subscribes(self):
        cli = mock.Mock()
        foxctl._car_on_connect(cli, None, None, 0, None)
        cli.subscribe.assert_called_with("foxctl/car_charge/set", qos=1)
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `python3 -m unittest tests.test_foxctl.CarModeMqttTest 2>&1 | tail -3`
Expected: `AttributeError: … 'car_mode_discovery'`.

- [ ] **Step 3: Implement the MQTT glue**

Add right after `_MQTT_SENSORS = [...]` (before `def mqtt_publish`):

```python
CAR_SET_TOPIC = "foxctl/car_charge/set"


def car_mode_discovery():
    """(topic, config) pairs for the car-mode select + status sensor. Pure."""
    dev = {"identifiers": ["foxctl_foxess"], "name": "FoxESS (foxctl)",
           "manufacturer": "FoxESS", "model": "foxctl single-poller"}
    sel = {"name": "Car charge", "unique_id": "foxctl_car_charge", "object_id": "foxctl_car_charge",
           "command_topic": CAR_SET_TOPIC, "state_topic": "foxctl/car_charge/state",
           "options": list(CAR_MODES), "availability_topic": "foxctl/availability",
           "icon": "mdi:ev-station", "device": dev}
    sen = {"name": "Car charge status", "unique_id": "foxctl_car_charge_status",
           "object_id": "foxctl_car_charge_status", "state_topic": "foxctl/car_charge/status",
           "json_attributes_topic": "foxctl/car_charge/attrs",
           "availability_topic": "foxctl/availability", "icon": "mdi:car-electric", "device": dev}
    return [(f"{MQTT_DISCOVERY}/select/foxctl_car_charge/config", sel),
            (f"{MQTT_DISCOVERY}/sensor/foxctl_car_charge_status/config", sen)]


def _car_on_connect(client, *args):
    """(Re)subscribe on every connect — paho drops subscriptions across reconnects."""
    client.subscribe(CAR_SET_TOPIC, qos=1)


def _car_on_message(client, userdata, msg):
    """paho network thread: validate, queue, wake the loop. Never touches the charger here."""
    mode = car_mode_parse(msg.payload)
    if mode is None:
        print(f"car mode: ignored payload {msg.payload!r}", file=sys.stderr)
        return
    car_mode_request(mode)
```

In `mqtt_publish`, set the callbacks before `cli.connect(...)`:

```python
            cli.on_connect = _car_on_connect
            cli.on_message = _car_on_message
```

Inside the `if not _MQTT["disc"]:` block, after the sensor loop, publish the car discovery:

```python
            for topic, conf in car_mode_discovery():
                cli.publish(topic, json.dumps(conf), qos=1, retain=True)
```

At the end of the try block, after `cli.publish("foxctl/telemetry", …)`:

```python
        cli.publish("foxctl/car_charge/state", _CAR.get("mode") or "Auto", qos=1, retain=True)
        cli.publish("foxctl/car_charge/status", (_CAR.get("status") or _CAR.get("mode") or "Auto")[:255],
                    qos=1, retain=True)
        cli.publish("foxctl/car_charge/attrs", json.dumps(_CAR.get("attrs") or {}), qos=1, retain=True)
```

- [ ] **Step 4: Wake the loop on a command**

In `loop(cfg)`, replace:

```python
        end = time.monotonic() + sleep
        while time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))
```

with:

```python
        end = time.monotonic() + sleep
        while time.monotonic() < end:
            if _WAKE.wait(min(1.0, max(0.0, end - time.monotonic()))):
                _WAKE.clear()          # a car-mode command arrived: act on it now
                break
```

- [ ] **Step 5: Force charge / force off reset the mode (latest wins)**

In the HTTP handler branch `elif self.path.startswith("/api/ev_charge") or self.path.startswith("/api/ev_off"):`, add as the first statement inside the `elif self.path.startswith("/api/ev_off"):` branch and the force `else:` branch (both, right after the `sw`/`allow_control` checks pass):

```python
                        car_mode_set(cfg, "Auto", (_ENERGY.get("totals") or {}).get("ev"),
                                     "manual force/stop from the web UI")
```

- [ ] **Step 6: Run the MQTT tests and the full suite**

Run: `python3 -m unittest tests.test_foxctl.CarModeMqttTest 2>&1 | tail -2`
Expected: `OK`.

Run: `python3 -m unittest discover -s tests 2>&1 | grep -E "^(ERROR|FAIL):" | sort > /tmp/claude-ev/after-183.txt; comm -13 /tmp/claude-ev/baseline.txt /tmp/claude-ev/after-183.txt`
Expected: no output (no new failures against the 62-item baseline). If `/tmp/claude-ev/baseline.txt` is gone, regenerate it from the 1.82.0 commit:
`git worktree add /tmp/claude-ev/base d31679c && (cd /tmp/claude-ev/base && python3 -m unittest discover -s tests 2>&1 | grep -E "^(ERROR|FAIL):" | sort > /tmp/claude-ev/baseline.txt); git worktree remove --force /tmp/claude-ev/base`

- [ ] **Step 7: Commit**

```bash
git add energy_tools/foxctl.py tests/test_foxctl.py
git commit -m "foxctl: car-mode MQTT select + status sensor; wake loop on command; force resets mode

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Release 1.83.0 and deploy

**Files:**
- Modify: `energy_tools/foxctl.py` (`VERSION = "1.83.0"`), `energy_tools/config.yaml` (`version: "1.83.0"`), `energy_tools/CHANGELOG.md` (new top entry).

- [ ] **Step 1: Bump the version and write the CHANGELOG**

Add at the top of `energy_tools/CHANGELOG.md`:

```markdown
## 1.83.0 — car charge modes (wall panel)

Explicit car modes chosen from HA (`select.foxess_foxctl_car_charge`) for the new wall panel
and the phone: **To coast** (spare home battery → car down to the coast floor), **To full**,
**+3 / +5 / +10 kWh**, and **Auto** (the existing behaviour).

- Outside the 10:00–14:00 free window a mode only spends battery above `survival_soc` (the
  coast floor) or sun, pausing on sustained import; the free window finishes the rest. Never
  buys shoulder or peak.
- Finishing: +N on delivered kWh (EV meter); To full on a fresh WiCAN 99.5 % or 10 min of no
  draw; To coast at the floor, when the car is full, or when the next free window ends.
- `sensor.foxess_foxctl_car_charge_status` gives a one-line status plus attributes (`phase`,
  `delivered_kwh`, `target_kwh`, `floor_soc`, `reason`).
- Persisted in `car_mode.json`: a container restart no longer cancels a charge. Commands
  wake the loop at once. Force charge / force off from the web UI reset the mode to Auto
  (latest wins). Modes skip the Auto daily cap and keep the safety holds.
- New options `ev_mode_expiry_h` (36) and `ev_mode_full_nodraw_min` (10).
```

Then run `sed -i '' 's/^VERSION = "1.82.0"/VERSION = "1.83.0"/' energy_tools/foxctl.py` and `sed -i '' 's/^version: "1.82.0"/version: "1.83.0"/' energy_tools/config.yaml`.

- [ ] **Step 2: Commit and push**

```bash
git add energy_tools/foxctl.py energy_tools/config.yaml energy_tools/CHANGELOG.md
git commit -m "foxctl 1.83.0: car charge modes (to coast / to full / +3/5/10 kWh) for the wall panel

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push origin main
```

- [ ] **Step 3: Pre-deploy checks on the Pi**

```bash
ssh robwil@192.168.1.52 'curl -s localhost:8770/api/state | python3 -c "import json,sys;s=json.load(sys.stdin);print(s.get(\"version\"), \"|\", s.get(\"ev_divert\"))"'
```

Expected: `1.82.0 | …`. If it shows `manual force-charge (Nmin left)`, note N: the restart drops in-memory overrides, so re-apply with `curl -X POST "localhost:8770/api/ev_charge?h=<ceil(N/60)>"` right after the restart.

- [ ] **Step 4: Back up, sync, add options, bump the image tag, rebuild**

```bash
ssh robwil@192.168.1.52 'cd /opt/stack/energy_tools && tar czf src.bak-pre-1.83.0.tgz src && cp -p data/options.json data/options.json.bak-pre-1.83.0 && cp -p /opt/stack/docker-compose.yml /opt/stack/docker-compose.yml.bak-pre-energy-1.83.0'
cd ~/projects/ha-energy-tools && rsync -a --exclude '__pycache__' energy_tools/ robwil@192.168.1.52:/opt/stack/energy_tools/src/energy_tools/
ssh robwil@192.168.1.52 'cd /opt/stack && python3 - <<"EOF"
import json
p="/opt/stack/energy_tools/data/options.json"; d=json.load(open(p))
d.setdefault("ev_mode_expiry_h", 36); d.setdefault("ev_mode_full_nodraw_min", 10)
json.dump(d, open(p,"w"), indent=2); print({k: d[k] for k in ("ev_mode_expiry_h","ev_mode_full_nodraw_min","tariff_mode")})
EOF
sed -i "s|image: energy-tools:1.82.0|image: energy-tools:1.83.0|" docker-compose.yml && grep -n "image: energy-tools" docker-compose.yml
docker compose build energy-tools 2>&1 | tail -2 && docker compose up -d energy-tools 2>&1 | tail -1'
```

Expected: options printed with `tariff_mode: four4free`; `image: energy-tools:1.83.0`; `Started`.

- [ ] **Step 5: Verify in HA**

```bash
ssh robwil@192.168.1.52 'sleep 70; curl -s localhost:8770/api/state | python3 -c "import json,sys;s=json.load(sys.stdin);print(s.get(\"version\"), \"|\", s.get(\"ev_divert\"))"; docker logs --since 2m energy-tools 2>&1 | grep -iE "error|traceback|car mode" | head'
```

Expected: `1.83.0 | car charger … (… Auto …)` or the Auto reason, with no tracebacks. In HA (Settings → Entities), `select.foxess_foxctl_car_charge` and `sensor.foxess_foxctl_car_charge_status` exist under "FoxESS (foxctl)".

- [ ] **Step 6: Exercise a mode end to end from HA**

Select `+3 kWh` on `select.foxess_foxctl_car_charge` in the HA UI (or ask Rob to). Within ~10 s, `tail -n 3 /opt/stack/energy_tools/data/foxctl/events.jsonl` shows `car mode → +3 kWh (selected in HA)` and the status sensor reads `+3 kWh · 0.0 of 3 · …`. Then select `Auto`; the events show `car mode → Auto`.

- [ ] **Step 7: Update project memory**

Update `~/.claude/projects/-Users-robwil-projects-ha-energy-tools/memory/ev-solar-keep-floor.md` (or add `car-charge-modes.md` and index it in `MEMORY.md`): the 1.83.0 modes, the entity IDs, persistence in `car_mode.json`, and latest-wins with force charge.
