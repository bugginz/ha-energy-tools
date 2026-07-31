# Free-window fill planner — design

**Date:** 2026-07-31
**Status:** approved for implementation (user request 2026-07-31)

## Problem

On 2026-07-31 the car charger was cut twice inside the free window:

```
10:01:03  car charger ON (free window · car + battery together (50kWh free left))
10:47:22  fast-guard: importing 14.7kW > 14.5kW supply cap → car off
11:01:03  fast-guard: importing 14.6kW > 14.5kW supply cap → car off
```

The base 10:00–14:00 ForceCharge group fills the battery at a **fixed**
`force_charge_power_kw` (10.5 kW). House load plus a 3.4 kW car does not fit
under the 14.5 kW supply cap at that fill rate, so the 20-second fast-guard
clamp drops the car. Ten minutes later `ev_divert_decision` sees headroom
(measured without the car), starts it again, and it re-trips — a flap loop that
persists for as long as the battery is force-charging.

Two root causes:

1. **The fill rate is fixed and greedy.** It is set to the inverter maximum
   regardless of how much charge the battery actually needs before the window
   closes, so it consumes headroom the car needs.
2. **The car start test has no margin.** It compares instantaneous grid power
   plus an estimated car draw against the cap with zero slack, so any house-load
   wobble (the A/C compressor starting at 10:51 added 2.2 kW) pushes the total
   over.

## Goals

1. Battery reaches **≥90% SoC by 14:00** — a hard requirement, so afternoon
   solar can top up the remainder instead of being curtailed by ForceCharge.
2. The free-window fill rate adapts to what is actually needed, using the solar
   forecast, so leftover supply headroom goes to the car.
3. The car reaches **≥80% SoC by Saturday morning** when it is plugged in.
4. No flapping.

## Non-goals

- Changing the base group's window (10:00–14:00), mode, or SoC cap. Only its
  **power** is managed.
- Overriding the 90%-by-14:00 requirement for the car's benefit under any
  circumstances.

## Key constraint discovered during exploration

**PV is curtailed while the base ForceCharge group is active** — the events log
records `pv 0.03kW` with 11–14 kWh still forecast on multiple days. Solar
therefore contributes essentially nothing *during* the fill. This is why the
90%-by-14:00 target matters: grid (free) buys the bulk, and solar finishes the
last stretch after the window, once the group is parked or expired.

Consequence for the design: the solar forecast is **not** subtracted from the
energy the fill must deliver before 14:00. It is used only to decide whether
90% is a sufficient deadline target, or whether a poor afternoon means the
window should buy more than that.

## Design

### New module: `energy_tools/fillplan.py`

Pure functions, no I/O, unit-tested in isolation. `foxctl.py` is already 4200
lines; the planner is self-contained arithmetic and does not belong in it.

#### `solar_kwh_between(sunset_dt, kwh_remaining, from_dt, to_dt, daylen_h=10.0)`

Integrates the same half-sine daylight model `_solar_bells` already uses
(daylight window `[sunset − daylen, sunset]`, area = `kwh_remaining`) over an
arbitrary sub-interval. Closed form:

```
kwh · [cos(π(a−start)/L) − cos(π(b−start)/L)] / 2
```

Used to estimate how much solar lands **after** the 14:00 deadline.

#### `plan_fill(...) -> dict`

Inputs: `soc`, `capacity_kwh`, `now_h`, `deadline_h`, `deadline_soc`,
`max_soc_cap`, `solar_after_deadline_kwh`, `house_kw`, `car_kw_est`,
`supply_cap_kw`, `max_fill_kw`, `min_fill_kw`, `guard_kw`, `trim_kw`,
`charge_eff`, `margin`.

```
# 1. Deadline target — 90%, raised toward the cap when the afternoon looks poor
top_up_kwh = (max_soc_cap − deadline_soc)/100 × capacity
shortfall  = max(0, top_up_kwh − solar_after_deadline_kwh)
target     = min(max_soc_cap, deadline_soc + shortfall/capacity × 100)

# 2. Power the battery MUST take to hit target by the deadline
deficit_kwh = max(0, (target − soc)/100 × capacity)
hours_left  = max(0.1, deadline_h − now_h)
need_kw     = deficit_kwh / hours_left / charge_eff
must_kw     = min(max_fill_kw, need_kw × margin)

# 3. Split the supply cap
spare    = supply_cap_kw − house_kw − guard_kw − trim_kw
with_car = spare − car_kw_est
if with_car >= must_kw:  car_ok = True;  fill = clamp(with_car, must_kw, max_fill_kw)
else:                    car_ok = False; fill = clamp(spare,    must_kw, max_fill_kw)
```

The battery's deadline need is the floor of the fill rate and the car never
takes precedence over it: when the car does not fit, the only way to make room
would be to fill below `must_kw`, which goal 1 forbids. When the battery is
already at or above target, `must_kw` is 0 and the car simply gets its slice
while the battery absorbs whatever is left — free energy is never wasted.

`trim_kw` is a backoff term (see "Flap fix" below).

#### `car_deadline_status(...) -> dict`

Given car SoC, pack size, target SoC, and hours to the next deadline occurrence
(weekday + hour, recurring weekly), returns `kwh_needed`, `hours_to_deadline`,
`hours_of_charge_needed`, and `on_track`. Informational only — see below.

### Why car urgency does not change the power split

Considered and rejected. `with_car >= must_kw` is algebraically identical to
`spare − must_kw >= car_kw_est`, so there is no additional headroom for an
"urgent" car to claim short of filling the battery below its deadline need.
Car urgency therefore drives **reporting and a notification** only, never the
split. If the car cannot make 80% by Saturday morning from free-window energy,
the existing pre-dawn dump remains the fallback and the user gets told.

### Wiring into `foxctl.py`

**`free_window_fill_tick(cfg, fox, snap)`** — new, called from the same place
`smart_fill_tick` is called, before it:

- Runs only inside the free window, with `strategy.fill_planner` enabled,
  `control.set_force_charge` true, and a healthy scheduler read
  (`sch["read_ok"]`) — never acts on a flaky read, matching the existing
  guardian's discipline.
- Locates the base ForceCharge group intersecting the window.
- Rewrites **only** that group's power when
  `abs(current_kw − plan.fill_kw) >= strategy.fill_power_step_kw` (default
  1.0 kW), preserving start, end, mode, min-SoC and SoC cap verbatim. Rate
  limited to one rewrite per `fill_rewrite_gap_s` (default 240 s).
- Publishes the plan to `_FILL` and into the snapshot as `snap["fill_plan"]`
  for the dashboard and for `ev_divert_decision`.
- `log_event("fill_plan", …)` on every actual rewrite.

This deliberately writes to a group the user owns. That risk is real — the
schedule has been clobbered before by flaky reads — so the rewrite is gated on
a healthy read, changes one field, is hysteretic, rate-limited, and can be
disabled with `strategy.fill_planner=false`.

**`ev_divert_decision`** free-window branch: when a fresh plan is present, use
`plan["car_ok"]` instead of the zero-margin instantaneous comparison. The
existing cap check stays as a backstop for the case where no plan exists.

### Flap fix

Four independent layers:

1. **Root cause** — the planner sizes the fill so that battery + house + car +
   `guard_kw` fits under the cap. The car is no longer squeezing into 0.2 kW.
2. **Margin** — the start test requires `guard_kw` (default 0.8 kW) of slack
   rather than comparing against the cap exactly.
3. **Cooloff** — after a fast-guard trip, the car will not restart for
   `ev_divert.guard_cooloff_min` (default 3 minutes), long enough for a
   re-planned fill power to take effect.
4. **Backoff trim** — a fast-guard trip inside the free window adds the observed
   overshoot plus 0.3 kW to `_FILL["trim_kw"]`, which lowers the next planned
   fill. It decays 0.5 kW per clean cycle and is clamped to [0, 4] kW. This
   absorbs house-load spikes that land between the planner's 5-minute cycles,
   which the 20-second fast-guard would otherwise keep catching.

### Car SoC plumbing

`sensor.wican_soc_real` is live (58.4% at the time of writing), so the car's
state of charge is readable. New config: `ev_soc_entity`, `ev_battery_kwh`
(42.0 for the Fiat/Abarth 500e), `ev_target_soc` (80), `ev_target_weekday`
(5 = Saturday), `ev_target_hour` (8). Surfaced on `snap["car"]` as `soc`,
`target_soc`, `kwh_needed`, `hours_to_deadline`, `on_track`, and pushed as a
notification (respecting `notify_min_gap_min`) when the car is not on track.

## Testing

`tests/test_fillplan.py`, stdlib unittest to match the existing suite:

- Half-sine integral: full-window integral equals the forecast total; a
  sub-interval after solar noon is less than half; interval entirely after
  sunset is 0.
- Today's live case: SoC 49%, 41.44 kWh, 11:05, 14:00 deadline, house 2.2 kW,
  car 3.4 kW, cap 14.5 → fill leaves room for the car and still hits 90%.
- Battery so far behind that the car cannot fit → `car_ok` false, fill pinned
  to `must_kw` or above.
- Battery already at/above target → `must_kw` 0, car runs, battery takes the
  remainder.
- Deadline target rises above 90% when post-deadline solar is poor, stays at
  90% when it is good, never exceeds `max_soc_cap`.
- Clamping: `fill_kw` never exceeds `max_fill_kw`, never below `min_fill_kw`
  unless `must_kw` is higher, and `trim_kw` reduces the plan.
- Car deadline status: on-track and behind cases, weekday rollover.

Plus a regression test for the flap: two consecutive planner runs with a house
spike between them must not produce car on → off → on.

## Rollback

`strategy.fill_planner=false` restores the previous fixed-power behaviour
exactly; `ev_divert.guard_cooloff_min=0` disables the cooloff. Neither requires
a code change.
