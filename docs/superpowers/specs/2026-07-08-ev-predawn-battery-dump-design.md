# EV pre-dawn battery→car dump + universal SoC floor-guard

**Date:** 2026-07-08 · **Target:** ship before the four4free flip on 2026-07-10.

## Goal

Never pay for power unless unavoidable. The best achievable day: battery at 100% at the
**end** of the free tariff window, appliances run inside the window, and the car charged
only from excess. Excess, in priority order:

1. **Spare solar** (existing v1.62/v1.64 diversion — unchanged).
2. **Free window** grid (existing: battery fills first, appliances run, car last — unchanged).
3. **NEW: overnight battery buffer** that the house didn't need — dump it into the car
   pre-dawn, because the free window refills the battery for free a few hours later.

Grounding numbers (observed): battery refills ~25%/h at 10.5 kW force-charge (75% in 3 h,
100% in 4 h), so even a 20% start refills inside the 4 h window — refill capacity is never
the binding constraint. Grid import tops out ~14.5 kW (≈60 A supply). The car draws
~2.4 kW measured (portable charger), not the configured 7 kW.

## Feature 1 — pre-dawn battery→car dump

A new branch in the EV divert logic (`ev_divert_decision` / `ev_divert_tick`):

- **Active window:** from `ev_predawn_start_hour` (default 4) until the active tariff's
  free-window start (11:00 zerohero, 10:00 four4free — read from config so the Friday
  flip needs no code change).
- **Arming:** budget positive; "plugged in" is discovered, not sensed — the branch turns
  the switch on, and the existing no-draw detection (`ev_kw < 0.1` for 5 min) turns it
  back off and parks the branch until the next night if the car isn't there. No departure
  schedule — the user plugs in most evenings, and misjudged dumps cost ≈ nothing (refill
  is free; worst case nudges the 50 kWh/day free cap, whose excess rate 26.4c is still
  below shoulder 37.51c).
- **Pre-dawn budget (kWh)** — energy-based, so the car's actual draw rate is irrelevant
  to correctness:

  ```
  predawn_budget = (soc − ev_predawn_floor_soc)/100 × battery_capacity_kwh
                   − forecast_house_load(now → free_window_start)
  ```

  The load forecast reuses the existing hour-of-day profile + temperature night factor
  (same model as `_load_to_sunrise` / survival SoC), with the horizon extended from
  sunrise to the free-window start. No solar term — conservative; dawn solar is bonus.
- **Start/stop with deadband** (same pattern as the v1.64 outlook gate): start when
  `budget > ev_start_margin_kwh` (existing option, default 1.0); stop when `budget ≤ 0`.
  Events log reason includes the maths, e.g.
  `car charger ON (pre-dawn surplus +2.8 kWh above 30% floor · refills free at 10:00)`.
- **Floor choice:** 30% (~12.4 kWh) is the hedge for "the free window doesn't happen"
  (outage, foxctl down) — enough to limp to evening. It is a *planning* floor; the
  inverter's constant `inverter_min_soc` (10%) is untouched.

## Feature 2 — universal floor-guard (covers manual switch-ons)

**Requirement (user):** if the car charge switch was turned on manually — at any hour —
and the projection says the battery won't hold the floor through to the free-window
start, foxctl must switch it off.

**Today's gap:** `ev_divert_tick` is edge-triggered on its own `_EV["on"]` bookkeeping;
a switch flipped on in HA while foxctl wants "off" produces no state change, so foxctl
never intervenes (confirmed live 2026-07-08 23:50: manual session running, tick blind).

**New behaviour:** every tick, independent of `_EV` bookkeeping, if the car is actually
drawing (`ev_kw ≥ 0.3`) and `predawn_budget ≤ 0` (same formula, evaluated at any hour —
horizon = next free-window start, wrapping past midnight), switch OFF and log:
`car charger OFF (floor-guard: battery would land below 30% before the 10:00 free window
— short X.X kWh)`.

**Exemption:** an active UI "Force car charge" override (`_EV["override_until"]`) is the
one documented escape hatch — "I need the car charged regardless of cost" — and is NOT
guarded. Plain HA switch flips are guarded. Disabling `ev_predawn_dump` disables the
dump but NOT the floor-guard; the guard has its own flag `ev_floor_guard` (default on).

**Re-trigger etiquette:** after a guard cut, do not re-cut-vs-restart flap: the guard
sets `_EV["on"] = False` so normal deadband rules apply; if the user flips the switch on
again within the same night, guard it again only after `guard_grace_min` (default 10 min)
so a deliberate second flip gets a visible grace window and a fresh log line rather than
an instant silent kill.

## Feature 3 — "never pay" import guard while dumping

While a pre-dawn dump session is active (outside the free window, battery-sourced): if
sustained grid import > `predawn_import_stop_kw` (default 0.5 kW for 2 consecutive polls)
appears — house spike pushing house+car past inverter discharge — stop the car. These
kWh must come from the battery, never the meter. (Normal case is safe: house ~1.5 kW +
car ~2.4 kW ≪ 10.5 kW inverter discharge; this guards the kettle-and-heater morning.)

## Config (config.yaml option → build_config → runtime `ev_divert{}`)

| Option | Default | Meaning |
|---|---|---|
| `ev_predawn_dump` | `true` | enable the pre-dawn battery→car branch |
| `ev_predawn_floor_soc` | `30` | planning floor (%) the dump/guard protects |
| `ev_predawn_start_hour` | `4` | earliest hour the dump may start |
| `ev_floor_guard` | `true` | cut ANY non-override charge session that breaches the floor projection |
| `predawn_import_stop_kw` | `0.5` | sustained grid import that aborts a dump session |
| `guard_grace_min` | `10` | grace before the guard re-cuts a deliberately re-flipped switch |

Reuses existing `ev_start_margin_kwh` (1.0) for the start deadband. Remember the config
gotcha: runtime values come from saved options/env via `build_config.py`, not the repo
`foxctl_config.json`.

## Surfacing

- `snap["predawn_budget"]` block: `{kwh, parts{usable_above_floor, load_to_window},
  floor_soc, window_start_hour, active, guard_armed}` — mirrors `car_budget`'s shape.
- Car card: show the pre-dawn budget line when relevant (night hours), plus
  floor-guard state.
- Events log: every ON/OFF with the budget maths (audit trail: "why did it charge
  5.2 kWh at 4 am / why did it cut my manual charge").
- **`/api/state` gains `version`** (the config.yaml version string) so deploy checks are
  one curl.
- Planning overlays that assumed `ev_charge_kw: 7.0` should prefer the measured session
  draw (~2.4 kW) when sessions exist — affects the dashboard free-window car overlay
  only, not the budget maths.

## Interactions (unchanged behaviour, stated for confidence)

- Free-window sequencing (battery→100% first, car last) untouched; the dump ends at
  window start by construction.
- Winter charge-to-100% and the 14–16 h shoulder top-up are unaffected: the dump's
  budget subtracts forecast load to window start, so the battery lands ≥30% at 10:00 and
  the window takes it to 100% by 14:00 with ~1.2 h margin.
- v1.64 spare-solar outlook gate unchanged (different floor/horizon; both can be active
  at different hours without conflict — pre-dawn branch only runs before window start,
  spare-solar only with sun).
- Daily session cap (`session_cap_kwh`) still applies to dump sessions.

## Verification

- Unit tests for the new pure functions: `predawn_budget(...)` and the floor-guard
  decision (soc/load-profile fixtures; window wrap past midnight; margin deadband;
  override exemption). Legacy `tests/test_foxctl.py` is stale — don't touch; run new
  tests standalone (`python3 -m unittest`).
- Live test plan (car is plugged in tonight): deploy, watch events — expected with
  tonight's numbers (SoC 56% @ 23:50): floor-guard cuts the manual session ~01:00 at
  ~50% SoC; battery coasts to ~30% by 11:00; free window refills to 100% by ~14:00.
- Post-flip check Friday: pre-dawn branch reads 10:00 window start; first evaluation
  sanity-checked.

## Later (explicitly out of scope now)

- **MeatPi/WiCAN car SoC (~2026-07-15):** upgrade dump + free-window logic from "dump
  surplus" to "charge to car target SoC by departure"; departure awareness becomes real.
- **powermon** (18-circuit Meross incoming) — parked mid-design; its residual/HVAC data
  later sharpens the load forecast this feature leans on.
