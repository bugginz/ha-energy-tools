# Outlook-driven EV auto stop/start — design

**Date:** 2026-07-05
**Component:** `energy_tools/foxctl.py`
**Status:** approved design, pre-implementation

## Problem

The car charger is a metered smart plug that foxctl can measure but does not
control. Charging is started/stopped by hand, so it is easy to leave the car
drawing power when the forward outlook says the battery is better spent
elsewhere — e.g. plugging in mid-afternoon and forgetting to stop it, when the
sun is about to set and a cold night means the battery will be needed for
heating.

We want foxctl to **automatically stop and start the car** based on whether the
battery + remaining solar can prove a surplus over tonight's expected needs
(heating included) — not merely react to live solar export.

## Goals

- foxctl owns the car-charger switch, so "forgot to stop it" cannot happen.
- The stop/start decision is **anticipatory**: it accounts for the rest of
  today's solar and tonight's expected load (including heating), so the car
  stops *before* the battery is at risk, not after.
- Every stop/start is explainable — a plain-language reason and a single budget
  number surfaced in the dashboard and event log.
- Reuse the existing `ev_divert` control loop, `survival_soc`, calibrated solar,
  and v1.63 Faikin/AC awareness rather than build parallel machinery.

## Non-goals

- Backtesting forecast accuracy (separate, paused effort).
- Per-circuit clamp ingestion (future hardware, separate effort).
- Full decision-engine replay or car-side (API) control — this uses the plug
  switch only.
- Price-forecast optimisation. Decisions remain deterministic on the active
  tariff, consistent with the rest of foxctl.

## Existing machinery this builds on

- `ev_divert_decision(snap, ev)` — pure policy returning `(want, why)`. Today it
  says the car may be ON only for (1) FREE window once the battery is full, or
  (2) spare solar export ≥ `min_export_kw` once the battery has passed a
  survival-based gate. Yields to force-charge / selling.
- `ev_divert_tick(cfg, snap)` — edge-triggered driver with dwell, a 4am-anchored
  daily kWh cap, a manual force-charge override window, and no-draw detection.
  No-op unless `ev_divert.switch` is set. Calls `ha_call_service(... switch,
  turn_on/turn_off ...)`.
- `dynamic.survival_soc` — battery % estimated as needed to get through the
  night; already the basis of `battery_priority`.
- Calibrated `solar_remaining` (Phase 3 bias applied) — forward remaining solar
  kWh for today.
- `forecast_periods(ha, cfg)` + v1.63 Faikin/AC awareness — forward temperature
  picture and current AC state, the basis of the heating uplift.

## Hardware / entities

The charger is the metered smart plug device `6294ha_series_2`, which exposes:

- `sensor.6294ha_series_2_power` — real power (W). **Becomes the EV metering
  source** (`ev_power_entity`), replacing the current×240V estimate from
  `sensor.6294ha_series_2_current`.
- `switch.6294ha_series_2` — controllable relay. **Becomes
  `ev_divert.switch`.** Exact entity id to be confirmed against live HA during
  implementation (the device may name it with a suffix); the design assumes a
  single `switch.*` entity on that device.

## Architecture

Five small, well-bounded pieces, mostly reusing existing units.

### 1. Control wiring (config + metering)

- Set `ev_power_entity = sensor.6294ha_series_2_power`.
- Set `ev_divert.switch = switch.6294ha_series_2` (confirm id).
- With `control.allow_control` already true, `ev_divert_tick` begins driving the
  plug. No new control path — the existing dwell / cap / override / no-draw
  behaviour applies unchanged.

### 2. Forward surplus budget (new pure function)

`ev_car_budget(snap, cfg, now) -> (budget_kwh, parts)`

```
car_budget_kwh = usable_battery_now
               + solar_remaining_today
               - expected_load_to_sunrise
               - comfort_reserve_kwh
```

- **`usable_battery_now`** = `max(0, (soc - inverter_min_soc) / 100) *
  battery_capacity_kwh` (capacity 41.44 kWh, `inverter_min_soc` from config).
- **`solar_remaining_today`** = the already-calibrated `solar_remaining` figure
  from the snapshot (0 after sunset).
- **`expected_load_to_sunrise`** = integral of the learned hour-of-day load
  profile from `now` to tomorrow's sunrise, **plus a heating uplift**:
  - Base: sum the learned per-hour `loads` profile across the remaining hours to
    sunrise (reuse the same profile the dashboard forecast uses).
  - Heating uplift: from `forecast_periods` temperatures over that window and
    the existing AC per-°C sensitivity (v1.62/1.63 `per_c` / `mild_c`), add the
    expected extra heating kWh. If Faikin AC is currently active/available, use
    it to sanity-bound the uplift. This is the piece that makes cold nights
    reserve more battery.
  - The base load profile already contains *typical* evening heating; the uplift
    is the *marginal* adjustment for how cold tonight is versus the profile's
    baseline. Care is needed not to double-count — the uplift is relative to
    `mild_c`, matching how the existing AC nudge is defined.
- **`comfort_reserve_kwh`** = new config buffer so we never plan the battery to
  the inverter floor (default e.g. 2.0 kWh; expressed in kWh, converted from/to
  SoC as needed).

`parts` returns the four components so the reason string and dashboard can show
the breakdown. The function is pure (takes `now`), unit-testable with fixed
inputs.

### 3. Decision integration

Extend `ev_divert_decision` (or wrap it) so the budget is a **gate on top of**
the existing yes-reasons — it can only ever *withhold* the car, never force it
on beyond current safety rules:

- Compute `budget = ev_car_budget(...)`.
- If an existing yes-reason holds (spare solar now, or free-window + battery
  full) **and** `budget > start_margin_kwh` → `want = True`, reason includes the
  budget, e.g. `"spare solar 2.1kW → car · outlook +3.4kWh surplus"`.
- If `budget < 0` → `want = False`, reason:
  `"outlook: battery + remaining solar won't cover tonight's heating (−1.2kWh) — car held to protect reserve"`.
- Between `0` and `start_margin_kwh` → hold current state (deadband; see 4).
- FREE-window charging from 0c grid is exempt from the budget gate **only while
  the battery itself is full** (soaking free grid energy into the car does not
  spend battery reserve). Spare-solar diversion is fully budget-gated.

### 4. Anti-flap

- Keep the existing `min_dwell_min` edge-trigger in `ev_divert_tick`.
- Add a deadband in the budget gate: start requires `budget > start_margin_kwh`
  (e.g. +1.0 kWh); stop triggers at `budget < 0`. Between the two the prior
  on/off state is held. This prevents chatter as the budget crosses zero near
  sunset.

### 5. Visibility

- `ev_divert_tick`'s returned status string already appears in the dashboard EV
  line; append the budget and its dominant reason.
- On each actual switch change, `log_event("ev_divert", ...)` records the budget
  breakdown (`bat`, `solar`, `load`, `reserve`) so post-hoc "why did it stop?"
  is answerable.
- Optional: expose `car_budget_kwh` as a metric alongside the existing
  `foxctl_ev_*` sensors so it is graphable/automatable in HA.

## Config additions

Under the top-level `ev_divert` block (currently absent from
`foxctl_config.json`, so it falls back to `DEFAULTS`) and top-level `ha`:

```jsonc
"ha": {
  "ev_power_entity": "sensor.6294ha_series_2_power"   // was ..._current
},
"ev_divert": {
  "switch": "switch.6294ha_series_2",   // confirm id in HA
  "comfort_reserve_kwh": 2.0,           // battery kWh never planned away
  "start_margin_kwh": 1.0,              // deadband: surplus needed to (re)start
  "outlook_gate": true                  // master on/off for the budget gate
}
```

Existing `ev_divert` keys (`feedin_max`, `min_export_kw`, `battery_priority`,
`min_soc`, `session_cap_kwh` default 30, `min_dwell_min`, `free_window_charge`,
`allow_grid`) are unchanged. `outlook_gate: false` falls back to today's behaviour (spare-solar /
free-window only), useful for A/B comparison and rollback.

## Edge cases

- **Sunrise unknown / no sun data** → treat `solar_remaining_today` as 0 and use
  a conservative fixed window (now→07:00) for load, so the budget errs toward
  holding the car.
- **Load profile not yet matured** → fall back to `typical_daily_load_kwh`
  pro-rated over the remaining hours, same fallback the dashboard uses.
- **Manual force-charge** → existing override wins; budget ignored for its
  window (user intent trumps outlook).
- **No temperature forecast** → heating uplift = 0 (base profile still carries
  typical heating); log that the uplift was skipped.
- **Battery selling / force-charging** → unchanged; those safety returns fire
  before the budget gate.
- **SoC ≤ inverter_min_soc** → `usable_battery_now` clamps to 0, budget almost
  certainly negative → car held. Correct.

## Testing

Pure-function tests for `ev_car_budget` with fixed `now`:

- Sunny afternoon, warm night, high SoC → large positive budget → car allowed.
- Late afternoon, cold forecast, mid SoC, low remaining solar → negative budget
  → car held (the motivating scenario).
- Sunset with battery at survival floor → budget ≤ 0 → held.
- Heating uplift responds monotonically to colder forecast temps.
- Deadband: budget just above 0 does not start a stopped car; must exceed
  `start_margin_kwh`.

Decision-integration tests for `ev_divert_decision`:

- Budget gate never forces the car on when no existing yes-reason holds.
- FREE-window + battery-full path bypasses the gate; FREE-window + battery not
  full still defers to battery first (existing behaviour).
- `outlook_gate: false` reproduces pre-feature decisions exactly.

Live verification via the HA frontend WS after deploy: confirm the switch entity
id, watch the car stop as sunset/cold-night conditions bring the budget
negative, and confirm the dashboard reason string reads sensibly.

## Rollout

1. Land config + metering swap + budget function + gate behind
   `outlook_gate` (default true) and `ev_divert.switch`.
2. Commit + push (git-based add-on; HA reloads).
3. Live-verify switch id and behaviour via HA WS; tune `comfort_reserve_kwh` /
   `start_margin_kwh` from observed nights.
