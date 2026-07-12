# Changelog

## 1.70.1 — four4free free window back to 10:00–14:00

GloBird confirmed 10:00–14:00 (2026-07-13) — the 11:00–15:00 advice of 07-10 was wrong,
so this reverts 1.67.3's window change. The user's hand-programmed inverter schedule
needs the matching change in the FoxESS app (foxctl won't touch it, per the 1.70.0
directive).

## 1.70.0 — foxctl never touches the user's inverter schedule

USER DIRECTIVE: the hand-programmed schedule (11:00–15:00 fill) is untouchable; foxctl
may only add/remove/enable/disable its OWN additional group. The FoxESS scheduler API
replaces the whole group list and the master flag disables everything, so every write
now carries the user's groups along (`scheduler_write_own`), and every stop rewrites the
list without foxctl's group instead of flipping the master flag (`scheduler_clear_own` —
flag only goes off when NO user groups exist). foxctl's group is fingerprinted and
persisted (`sched_own.json`); a flaky scheduler read (seen live: errno 0, result null)
falls back to the last-good cache and a clear NEVER blind-disables on a flake.
Also: AUTO force-charge windows are clamped to peak start (a 14:30 write no longer ends
16:30 — the 120-min cap is for foxctl dying, not buying at 59.95c) and a top-up won't
start within 10 min of peak.

## 1.69.1 — plug draw only counts as the car while the car's relay is ON

The 6294HA's power sensor spans both its sockets; the outdoor heater on the spare socket
read as a 1.5 kW "manual car session" and the floor-guard no-op-cut the (already off) car
relay every grace interval all evening. `_attribute_ev_kw` zeroes the measured draw when
the car's relay reads firmly "off" (unavailable/unknown keeps it — fail toward guarding),
which also keeps heater watts out of the car session log and EV energy counters.
`snap.ev_switch_state` exposed. Proper per-socket attribution arrives with the MeatPi.

## 1.69.0 — grid decisions read the local CT clamp

The A1 Meross clamp on the grid main (seconds-fresh, +import/−export, verified by
force-sell test) now feeds foxctl's grid-power decisions, with the cloud FoxESS value
(up to ~5 min stale) as fallback: new `ha.grid_power_entity` (default
`sensor.grid_main_power_local`) → `snap.grid_power_live` → `_gp_now()` used by the
free-window supply-cap headroom check and the pre-dawn import abort. New **fast-guard
thread** polls the clamp every 20 s while a charge session runs and cuts the car within
~40 s of sustained import over the supply cap (any hour, override included — it protects
the service fuse) or over the pre-dawn battery-only threshold (override exempt) — the
5-minute main loop alone could ride a kettle-on-top-of-car spike for minutes.

## 1.68.2 — phone-readable dashboard

Charts no longer shrink below ~700px on small screens (pan sideways instead — the 720px
SVG at iPhone width rendered ~6px text), all SVG chart text is 2px bigger, tables render
at near-body size on phones, and each chart has an "open full-size ↗" link that escapes
the HA app webview into Safari where pinch-zoom works natively.

## 1.68.1 — headroom check: actual draw counts as running

A session foxctl didn't start (manual flip) is already inside grid_power — the start-check
added the expected draw on top and double-counted, reporting "no headroom" while the car
charged. `ev_kw ≥ 0.3` now counts as running for the supply-cap deadband.

## 1.68.0 — free window: car charges ALONGSIDE the battery fill

Battery-full is no longer a precondition for the free-window car soak — the ~60A supply
carries the battery force-charge (10.5 kW) + house + car (~2.4 kW measured) with headroom
to spare, so waiting wasted free-window hours. New `ev_supply_cap_kw` (default 14.5,
the observed max import): the car starts only if current import + its expected draw
(biggest recent session peak, not the 7 kW nameplate) fits under the cap, and pauses if
import exceeds it. The force-charge safety hold now applies only OUTSIDE the free window
(pre-peak shoulder top-up, manual force charge — paid power). Charge advisor text
harmonised: free window now reads "plug in now (0c) — car and battery charge together"
instead of the contradictory "battery filling first".

## 1.67.3 — four4free free window corrected to 11:00–15:00

User-confirmed with GloBird 2026-07-10; the profile had 10:00–14:00. Moves the money
pricing bands, the free-window car soak, and the pre-dawn dump horizon (dump now runs
04:00→11:00).

## 1.67.2 — pre-dawn no-draw park no longer races a slow car wake-up

First live dump (2026-07-10 04:00) exposed a race: the portable charger took ~5½ min to
wake the car, one tick past the 300s no-draw window — the dump parked itself for the day
just as the car started pulling 2.4 kW (only the switch dwell kept the session alive).
Two changes: the pre-dawn park threshold is now 600s (the generic 300s "no draw" note is
unchanged), and a park is CLEARED if the socket shows real draw (≥0.3 kW) while the
switch is still on — the car proving it's awake un-parks the branch.

## 1.67.1 — scheduler "active" is now time-aware

A hand-programmed inverter scheduler group (e.g. the four4free 10:00–14:00 import window)
is enabled 24/7 but only RUNS inside its window — foxctl treated "enabled" as "active",
so ev_divert held the car off with "battery force-charging — car held off" at 2am and the
pre-dawn dump could never start. `scheduler_status` now reports `active` only while the
clock is inside the group's window, and adds `segment` (what's programmed, regardless of
time). Also makes `already_charging`/`already_selling` in the apply path and the manual
override "(active)" label time-correct. Default `tariff_mode` → four4free.

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

## 1.66.1 — debounce the stale-telemetry notification

A single failed poll cycle self-heals (control already goes on safety hold for
that cycle), so it no longer pages the phone.

- **`notify_stale_cycles`** (default **3**): the stale notification only fires
  after that many *consecutive* stale cycles, once per outage; recovery resets it.
- Message no longer claims "HA sensors frozen" (those legacy poller entities are
  gone by design since foxctl became the single FoxESS poller) — it now reports
  how many cycles telemetry has been unavailable.
- New `tests/test_notify.py` covering the debounce behaviour.

## 1.66.0 — run standalone under docker compose (post-HAOS migration)

Same image now runs both as a HAOS add-on and as a plain docker compose service
(the add-on host was migrated from a Pi 4/HAOS to a Pi 5 running HA Container).

- **`HA_URL` / `MQTT_HOST` env overrides.** `build_config.py` defaults stay add-on
  compatible (`http://supervisor/core`, `core-mosquitto`); compose sets
  `HA_URL=http://localhost:8123` and `MQTT_HOST=localhost`. nemfuel's `mqtt.env`
  is generated with the same host.
- **`HA_TOKEN` support.** `run.sh` seeds the token file from `HA_TOKEN` (long-lived
  token) when set, falls back to `SUPERVISOR_TOKEN` under the Supervisor, and
  otherwise keeps a pre-seeded `/data/.config/sen66/ha_token`.
- **Standalone builds.** `BUILD_FROM` defaults to `alpine:3.21` so plain
  `docker build` works, and the image installs `tzdata` — tariff windows use
  naive local time, so `TZ=Australia/Sydney` must actually resolve.

## 1.65.0 — winter charge-to-100% + pre-peak shoulder top-up

Fills the battery to full while it's still cheap, so cold nights don't drain it into expensive grid.

- **Seasonal charge target.** New `charge_target_soc` (default **100**) is the fill target for **both** the
  free window and the new shoulder top-up — so the free 0c window fills all the way first, and we never
  pay shoulder rates for capacity the free window could have given. Lifts the daily fill above the 90%
  `max_soc` health cap for winter; drop it back to 90 when the cold passes. (`max_soc` still governs the
  other logic.)
- **Pre-peak shoulder top-up.** If the free window ends (usually 14:00) with the battery **below target**
  **and** the remaining solar won't finish the fill, foxctl force-charges in the **shoulder window between
  free-end and peak-start (14:00–16:00)** — the cheapest import left before peak — so the evening peak
  (16:00–23:00) is ridden off battery, not bought at 59.4c. Fires only when shoulder is genuinely cheaper
  than the coming peak, and **never charges into peak** (the decision re-evaluates every cycle and stops at
  16:00). New `shoulder_topup` toggle (default on). `decide_zerohero` now takes `solar_remaining`.

## 1.64.0 — outlook-driven EV auto stop/start (forward surplus budget)

The car now stops itself before a cold night eats the battery, instead of only reacting to live solar.

- **Forward surplus budget.** Each cycle foxctl computes `car_budget = usable battery + remaining solar −
  tonight's expected load (heating incl.) − comfort reserve`. Spare-solar diversion to the car is held
  whenever that budget can't prove a surplus over the night, so the car stops **anticipatorily** as sunset
  nears / cloud rolls in / the forecast turns cold — not after the battery's already at risk. Free-window
  0c grid soak (battery full) stays exempt. New `ev_car_budget` / `_load_to_sunrise` reuse
  `predict_base_load` + the temperature factor, consistent with `survival_soc`.
- **Deadband + visibility.** Stop at budget < 0, require budget > `start_margin_kwh` to (re)start, on top of
  the existing dwell/cap/manual-override. `snap["car_budget"]` carries the number + parts breakdown, and the
  stop/start reason spells out the outlook. New options: `ev_outlook_gate`, `ev_comfort_reserve_kwh`,
  `ev_start_margin_kwh`.
- **Real-watt EV metering.** `ev_power_entity` default moves to `sensor.6294ha_series_2_power` (measured W)
  from the current×voltage estimate.

## 1.63.0 — forecast "what's coming & why" table + Faikin AC awareness

Turns temperature + AC into a forward-looking plan you can read at a glance.

- **Forecast table** on the dashboard: **Now / Tonight / Tomorrow day / Tomorrow evening**, each with
  the forecast **temperature**, **predicted usage** (hour-of-day profile × temperature nudge), **solar**,
  and a **battery/grid implication** — plus a plain-English takeaway, e.g. *"Cold night (→9°C) lifts usage
  ~22% (heating) — battery covers it."* Cold/hot periods are flagged in red.
- **Real forecast temps.** The weather entity dropped its `forecast` attribute, so the temp nudge in
  v1.62 only ever saw *current* temp. Now foxctl calls the `weather.get_forecasts` service (daily + hourly)
  to get tonight's low and tomorrow's temps — the nudge and the table use the actual forecast.
- **Faikin AC awareness.** Your Daikin (via Faikin) has no watt sensor, so foxctl reads **compressor
  frequency** as the load proxy and estimates AC draw as `comp_Hz × k`. It shows live AC state
  (heating/cooling, compressor Hz, indoor/target/outdoor temps, est. kW), and uses the Faikin's **local
  outdoor sensor** as the current temperature (more accurate than the weather entity). `k` **self-calibrates**
  over time via a running least-squares regression of measured base load on compressor Hz (persisted).
- New options: `weather_entity`, `ac_climate_entity`, `ac_comp_entity`, `ac_fan_entity`,
  `ac_outside_entity`, `ac_kw_per_hz` (auto-defaulted to the discovered Faikin entities).

## 1.62.0 — money awareness + "good time to charge" advisor + free-window auto-charge

Turns the telemetry into dollars and decisions.

- **Money, baked in.** New live money tracker prices every grid kWh at the current tariff band and
  accumulates today's real spend, export credit, and — the headline — **$ saved vs buying everything
  from the grid** (no-solar-no-battery baseline). Dashboard **"Money today"** card: `$X saved`, spend,
  by-band breakdown (peak / shoulder / free kWh used vs the 50 kWh cap), and a rolling week total.
  Rates come from the existing tariff profiles; helpers `import_rate_c` / `export_rate_c` / `current_band`.
  Persisted per calendar day (matches the bill + free cap) in `money.json`.
- **"Good time to charge?" advisor.** A colour-coded good / ok / avoid card that answers whether now is a
  good moment to put the car on, with a one-line reason (free-window headroom, spare solar, peak avoidance,
  shoulder cost, or a hot/cold-evening caution).
- **Free-window auto-charge.** `ev_divert` now has a second way to switch the car socket ON: during the
  free tariff window, **once the house battery is full**, it soaks up remaining FREE grid energy (0c),
  capped by the daily 50 kWh free headroom — so if there's spare free capacity it gets used, and if there
  isn't, it doesn't. Battery still fills first; peak/shoulder still hold the socket off (unless spare solar).
  New `ev_free_window_charge` option (default on).
- **Tariff-band shading on the timeline.** The −24h→+24h chart is now shaded by band — green **free**,
  red **peak** — so *why* the plan charges midday and coasts through the evening peak is self-evident.
- **Weather-aware base load.** The existing (previously unused) `temp_*` config is now wired in: a hot or
  cold evening lifts the predicted coast/base load (HVAC), which raises the battery survival reserve. Read
  from the weather entity's temperature + today's forecast; surfaced as `temp_nudge` and on the weather card.
- Battery capacity fallback corrected to 41.44 kWh (4-battery pack).
- **Tariff transition note:** GloBird shoulder → **four4free** (free 10:00–14:00, 50 kWh/day) is already a
  built-in profile — flip `tariff_profile: zerohero → four4free` when it lands. Until then zerohero (free
  11:00–14:00) stays active.

## 1.61.0 — resizable charts (iOS HA app) + battery SoC on the day-by-day chart

- **Charts are now resizable and readable on a phone.** They were fixed 720×300 SVGs shown at
  `width:100%`, so on a ~390px phone they rendered ~150px tall with ~6px text — full width but tiny.
  Two fixes: (1) **tap any chart to enlarge** it to a fullscreen overlay — on a portrait phone the wide
  chart rotates 90° to fill the screen (landscape); tap again / Esc to close. (2) A **"Chart size" −/＋/fit
  stepper** (persisted in localStorage) scales the inline charts up to 4×, with horizontal scroll. Both
  work in the iOS HA app's WKWebView and survive the 60s soft-refresh (CSS var on `<body>` + event
  delegation).
- **Battery SoC on the day-by-day chart.** The bottom chart now overlays average battery **State of
  Charge %** on a secondary right-hand axis (0–100%), as a dashed blue line inside its own day-to-day
  spread band — alongside usage & solar (kWh) on the left axis. New per-day hourly SoC is fetched from
  FoxESS history (`SoC` variable, hour-averaged) and stored with each day; the axis only appears once SoC
  data has accrued. Verified in-browser (dual-axis layout, stepper, and lightbox).

## 1.60.0 — real EV metering via the current sensor + charge log + spread band

- **EV charger draw now read from `sensor.6294ha_series_2_current`.** `resolve_ev_power` is now unit-aware: it handles power sensors (W/kW) **and current sensors (A/mA)**, converting amps → kW via a configurable `ev_voltage` (default 240 V, AU nominal). Auto-discovery also strips a trailing `_socket_N`/`_outlet_N`/… off the switch object_id so it reaches device-root companion sensors (e.g. the `…_series_2_socket_2` switch shares a device with `sensor.…_series_2_current`). The dashboard "meter:" line shows the unit conversion (`(A→kW @240V)`) so it's clear where the number comes from.
- **Actual car-charge session log — "where we charged and how much".** A new tracker watches the live EV draw and records each real charging session (start/end, kWh delivered, peak kW), preferring the monotonic cumulative EV counter for energy. The dashboard gets a **"Recent car charges"** panel (per-session time range, duration, kWh, peak) plus a "● charging now · X kWh today" line on the Car card. The timeline chart now draws **solid green blocks for sessions that actually happened** (past) and keeps the dashed block for the **planned** free-window (future only).
- **Day-by-day usage chart is less wispy.** The spaghetti of one faint line per day is replaced by a smoothed hour-of-day **average line inside a shaded spread band** (10th–90th percentile across days, or full min–max when only a couple of days). Much easier to read the typical shape and the day-to-day range at a glance.
- Config: new `ev_power_entity` default + `ev_voltage` option (wired through config.yaml → build_config.py → foxctl_config.json).

## 1.48.0 — ZeroHero: real tariff, export off by default

- **Matches the actual GloBird ZeroHero charges** and the "feed-in is bad, don't export" call. ZeroHero export to grid is now **gated on `sell_enabled` (auto_sell)** — with it off (set `auto_sell: false`), the 18:00–21:00 window simply behaves like the rest of the peak: cover load from battery, **zero grid import, no feed-in**. The schedule is now purely import-cost driven: FREE-charge 11:00–14:00 (first 50 kWh @ 0c) → full by 2pm; **never import** in the 16:00–23:00 peak (44c); run off battery through the 14–16 & 23–11 shoulder/overnight (33c) until the next free window.
- Dashboard ZeroHero card updated to show the real tariff bands (0c / 44c / 33c) and whether export is on or off.
- 1 new test (no export when feed-in disabled → falls through to peak hold); 93 total green.

To run your plan: `tariff_mode: zerohero`, `auto_sell: false` (no export), `max_soc: 100` (fill to full midday).

## 1.47.0 — ZeroHero: explicit 16:00–23:00 peak (zero import)

- **Makes the GloBird ZeroHero ToU exact.** The strategy already did the right shape (free-charge 11:00–14:00 → max, export 18:00–21:00, run off battery otherwise), but it only *named* the 18–21 export sub-window. It now models the **full 16:00–23:00 peak** as an explicit "cover load from battery, ZERO grid import, never force-charge" window (`peak_start_h`/`peak_end_h`, default 16/23), so the 16–18 and 21–23 shoulders of the peak are handled and shown clearly. Grid force-charge is hard-restricted to the free window — never before 11:00, never in the peak.
- The export window (18–21) still discharges only **down to the survival floor**, which is sized to coast through the rest of the peak + overnight to the next 11:00 free window — i.e. "no import before 11am". The ZeroHero dashboard card now spells the whole schedule out.
- 7 new ZeroHero window tests (free-charge, before-11 no-import, peak no-import even when low, export, hold-at-survival); 92 total green.

To switch: set `tariff_mode: zerohero` in the add-on Configuration (and `max_soc: 100` to fill to full). In ZeroHero mode the Amber price logic + LLM strategist are bypassed for this deterministic schedule.

## 1.46.0 — TOP-UP mode: stay full for spike-sell readiness (buying cheaply)

- **New `topup_to_target` option (default ON).** You wanted the battery kept **as full as possible** so there's always something to dump into an unpredictable spike — not the strict cost-minimal "only buy what you need" of Phase 2. Top-up mode targets the **headroom to the charge cap** (less today's remaining solar, so you don't pay grid for what the sun will still give) instead of just the survival deficit. Crucially it still buys **only in the cheapest forward slots ≤ ceiling** — so it fills *cheaply and opportunistically*, never at premium prices, and the spike auto-sell recoups it. Set `topup_to_target: false` to go back to minimal import.
- This is why the SoC line wasn't reaching 100% — need-based buying only covered the ~5 kWh deficit. With top-up on, it now fills toward `target_soc` whenever cheap slots exist (in winter your small base load means solar covers the day and grid gently tops up overnight). The FOUNDATION card shows the active mode ("TOP-UP (keep full, buy cheap)" vs "NEED-BASED").
- Sizing extracted to a tested `buy_target_kwh` helper (survival vs headroom vs buffer); 5 new tests, 85 total green.

## 1.45.0 — Opus stays primary through transient API overloads

- **Stop dropping to Haiku on a blip.** The strategist was falling back to the fallback model the instant a call failed — including **HTTP 529 (Overloaded)**, which is a transient server-side hiccup, not a real failure. So Opus 4.8 looked "not working" when it was just losing the occasional coin-flip. The call now **retries the primary model on transient errors (429/5xx/529) with a short backoff (1s→2s→4s)** before falling back; permanent errors (400/404 — bad model / no access) still fall back immediately, no wasted retries.
- **No more silent fallback.** When a turn does end up on the fallback model, the Strategist panel now shows a clear notice ("⚠️ primary X was unavailable (e.g. 529 overloaded) — this turn ran on fallback Y"), so you can always tell which model actually answered.
- 3 new tests (retry-then-succeed, fallback-after-exhaustion, no-retry-on-permanent); 80 total green.

## 1.44.0 — One chat-driven strategist (Phase 3)

- **The two LLM boxes are now one.** The separate silent "Dynamic LLM" verdict card is gone; its rating, reasoning, current levers, and any "📣 Needs you" operator action are folded into the **single Strategist panel** alongside the conversation. One surface that both explains and (within hard guardrails) acts.
- **Re-targeted the strategist's levers onto the Phase-2 model.** The old `charge_start_price` knob (made vestigial by need-based buying) is replaced by three **relative** nudges, all hard-clamped: `target_soc` (charge cap), **`spike_sell_buffer_kwh`** (extra import beyond strict survival so the battery carries something to export into an improbable-but-possible spike — clamped ≤ half the pack and added to the forward deficit), and **`buy_bar_cap`** (refuse to buy above $X even when needed — clamped to [floor, ceiling], tightens the buy ceiling). The mission prompt + LLM context now describe exactly these.
- Operator chat + the persistent mission history are unchanged; the strategist just has coherent, model-appropriate levers again (an operator note like "hold out for cheaper" maps to a bar cap; "keep some to sell tonight" maps to a buffer).
- 6 new tests (lever clamps + buy-ceiling tightening); 77 total green.

This completes the safety → foundation → strategist → UX rework (Phases 1–4).

## 1.43.0 — Charts match the new model + usable-in-HA dashboard (Phase 4)

- **Fixes "no SoC line hits 100% any more" and "next cheap buy at 10am isn't on the chart".** Both charts and the SoC projection were still keyed to the old absolute `charge_start_price`, which Phase 2 made vestigial — so they never charged in the projection and never shaded the slots the controller actually buys. They now use the **relative buy bar** (`rec.buy_bar` — the cheapest forward slots covering the deficit): the green "buy ≤ $X (relative)" line + shading mark the real chosen windows (your 10am slot shows up), and the rules-model SoC line fills during them. (If the SoC line still tops out below 100%, that's your `max_soc` cap — raise `max_soc`/`target_soc` to 100 to see/charge to full.)
- **Charts you can actually read in the HA iframe.** Removed the 60s `<meta refresh>` that reloaded the whole page and wiped any resize. Replaced with a **soft refresh** (every 60s it swaps just the live regions — recommendation, spike, cards, charts — preserving chart size, scroll, and the chat/note inputs) plus explicit **− / +** height buttons that work inside the sandboxed iframe (drag-to-resize still works when opened directly at `host:8770`).
- **Buttons next to the results.** Evaluate / Apply / ↻ Refresh / Stop now sit **in the recommendation card** they act on; occasional actions (LLM review, test force-charge, backfill) moved into a collapsed "More actions". The FOUNDATION card shows the live need-based model (bar, deficit, slots).
- 2 new chart tests; 72 total green.

## 1.42.0 — Need-based, RELATIVE buying (Phase 2 foundation)

- **"Cheap" is now relative to the forward forecast and sized to what you actually need.** The buy decision no longer fires on an absolute `charge_start_price`. Instead, each cycle the controller forecasts the **import deficit** to the next solar ramp (load-to-next-solar from your trend profile, minus usable battery + remaining solar), then imports **only during the cheapest forward slots that cover that deficit** (`plan_buy_slots`). The affordability "bar" rises when the forecast is dear and falls when it's cheap — but never exceeds the **ceiling** (hard veto), and at/below the **floor** we always top up. **No forward deficit → no import** (so it stops buying mediocre power "just in case").
- This is the "always look forward, buy only what's needed at the cheapest times" the design was meant to deliver — and it's encoded as the foundation with named guardrails (floor always-OK, ceiling veto) and a full test suite (9 new tests incl. defer-to-cheaper-future, large-deficit-widens-bar, ceiling block, no-deficit-no-buy).
- Removed the old absolute/`charge_start_price` + horizon/defer pre-charge branches that led to buying expensive power ahead of peaks. The dashboard FOUNDATION card now shows the live model: "buy in cheapest slots ≤ $bar · need N kWh to next solar → cheapest M slot(s)".
- Note: the LLM's `charge_start_price` knob is now vestigial (target_soc still caps the charge); the strategist is reworked to nudge the relative knobs in Phase 3.

## 1.41.0 — SAFETY: stop stranding the inverter at a high min-SoC

- **Root-cause fix for the "imports expensive power, sells nothing" bug.** Auto-sell was pushing the *computed survival level* onto the inverter as its grid min-SoC (`minSocOnGrid`), which lands on **66%** under normal evening math — and disabling the schedule never resets it, so in SelfUse the inverter kept importing grid power to hold 66%. foxctl now **never writes a computed or raised min-SoC**: the only value it ever sends is a constant safety floor, **`inverter_min_soc` (default 10%, new option)**, which you should set to match the FoxESS app. Survival and export-buffer are enforced **in software** (the loop stops charging/selling when the target is reached), never on the device.
- **Self-healing + visibility:** the next force-charge/sell window rewrites the floor to 10%, undoing any legacy stranding; and if the inverter is read sitting above the floor, a red banner appears ("Inverter min-SoC is N% … clear it in the FoxESS app") plus a log warning, so a stranded value can never be silent again.
- **Manual SELL** now stops at its requested floor in software too (no device floor pushed).
- **Sell threshold "sticks":** the saved sell override was applied to decisions but missing from the snapshot the baseline form read back, so the form showed the default — fixed; the form now shows your saved value.
- Tests: 9 new — no scheduler write ever carries a min-SoC above the constant floor (incl. a 66% survival case), `get_min_soc` parsing, manual-sell software floor, and sell-override persistence + display.

This is the first of a multi-phase rework (safety first); relative need-based buying, a single chat-driven strategist, and the UI fixes follow.

## 1.40.0
- **Spike-readiness card** at the top of the dashboard (right under the recommendation). At a glance: is auto-sell armed, the export threshold, and **how much is sellable above the survival buffer** right now (kWh + %), with a clear state — ✅ ready, 💰 selling now, 🔌 charging (will sell once it tops & price clears), or ⚠️ auto-sell off / at the buffer. Spells out that the buffer keeps *N%* (~*X* kWh) specifically to ride out **extended** high-price runs without importing — so a single ephemeral spike isn't critical, but sustained volatility still leaves a cushion.

## 1.39.0
- **Teach the strategist what the controller does automatically.** The LLM was recommending manual actions for things foxctl already does on its own — e.g. telling you to flip the inverter to "Feed-in Priority" to export into a price spike, when the controller **auto-sells** (sets the FoxESS scheduler to ForceDischarge, selling the battery to grid down to the overnight survival floor) the moment the feed-in price clears the sell threshold. The mission prompt now spells out the automation boundary — auto-sell, auto force-charge, auto EV-divert all run every cycle — and the per-cycle context carries the live `automation` state (auto-sell enabled? threshold? survival SoC?). It's told never to ask the operator to manually export/stop-exporting or switch inverter modes, and `operator_action` is restricted to the settings a human actually controls.
- **Clear-chat button** on the Strategist panel (`/api/chat_clear`): wipe the conversation to drop stale reasoning after a capabilities change and start the mission fresh.

## 1.38.1
- **Auto-migrate the LLM model on upgrade.** Home Assistant keeps your previously-saved add-on options across updates, so existing installs were still pinned to the old baked default (date-suffixed Haiku) — showing "model haiku · fallback haiku". Config build now treats that exact legacy string (or empty) as unset and upgrades it to `claude-opus-4-8`, so the thorough strategist takes effect without editing options. A deliberate `claude-haiku-4-5` choice is preserved.

## 1.38.0
- **Thorough strategist + persistent, mission-anchored chat.** The dynamic-policy LLM is now **Claude Opus 4.8** by default (was Haiku) with **adaptive thinking**, and Haiku 4.5 is kept as an automatic **fallback** if the primary API call fails — set via the new `llm_model` / `llm_fallback_model` add-on options.
- The advisor is no longer a stateless one-shot. It's now **one continuous conversation that spans days**, anchored to a frozen "overall mission" system prompt (prompt-cached): each automated policy turn and every operator message share the same rolling history, so the strategist remembers what it advised before and how prices/solar actually played out. History is persisted to `/data` (`llm_chat.json`) so it survives restarts/updates, and is bounded (last ~40 chat messages + the most recent policy turn) to keep cost trivial.
- **New "Strategist chat" panel** on the dashboard: read the running conversation and **talk to the advisor directly** — ask why it set a knob, or give standing guidance it carries forward (`/api/chat`). Routine "POLICY CONTEXT" state uploads are collapsed for readability. Still advisory: it only tunes `charge_start_price` + `target_soc` within the hard foundation guardrails.

## 1.37.1
- Round the telemetry power values (PV, load, EV, grid import/export, battery) to 2 dp in the snapshot, so cards show `1.4 kW` instead of float artifacts like `1.4009999999999998 kW`. Also tidies the published MQTT sensor values.

## 1.37.0
- **EV divert now yields to active battery operations (safety).** The car charger is held OFF while the inverter is **selling** (force-discharge to grid) or **force-charging the battery toward a target it's still >5% below** — so plugging in at a critical moment can't redirect power away from a high-price export or starve a critical battery fill. Near the top of a charge (within 5% of target) the car may still charge alongside a cheap top-off. Status shows e.g. `battery is selling to grid — car held off`.

## 1.36.0
- **Buy price on the SoC chart.** The dedicated SoC chart now overlays the forecast **buy price** (blue line, right $ axis) plus the charge-start (green) and sell (pink) threshold lines — so you can see whether the shadow plan actually tracks price (charging below charge-start, selling above the sell threshold).
- **EV "no draw" detection.** If the car-charger socket is ON but pulling ~0 kW for 5+ minutes, the EV status now flags `⚠️ no draw — car full or unplugged`, so an idle socket is obvious instead of looking like it's charging.

## 1.35.0
- **Dedicated SoC % chart** (its own single axis, below the price charts) comparing the **rules-model projection** (teal) against the **shadow plan + floor envelope** (orange) with the survival line and current SoC — so you can see why the plan runs leaner without fighting the crowded 3-axis price chart. Backed by a new shared `project_soc_path` (the rules-model forward sim) so both the price chart and this one use the same numbers.
- **CSV export** (`/api/export.csv`, linked under the SoC chart): yesterday + today **5-minute actuals** (SoC, PV, load, grid import/export, from FoxESS history in ≤24h windows) **plus** the forward per-slot **forecast/plan** (buy + feed-in price, expected load/solar, rules-model SoC, shadow-plan SoC, plan floor) — one spreadsheet to see what actually happened vs what's planned.

## 1.34.0
- **FoxESS API rate-limit / error banner.** The client now records FoxESS API failures (HTTP 429, or errno/message indicating frequency/limit) and clears on the next success. When reads are currently failing, a prominent banner appears at the top of the page — red ⛔ "FoxESS API RATE-LIMITED — reads are being rejected … ease off Backfill & rapid actions" or orange ⚠️ for other API errors — with the last error and how long ago. So a stale dashboard now says *why*, instead of silently showing old data.

## 1.33.0
- **Dashboard responsiveness after actions.** Control actions (stop/sell/force-charge/scheduler-off/ev/apply) now schedule two follow-up snapshot refreshes (~40s and ~110s later) so the page reflects the change as the inverter + FoxESS telemetry catch up — instead of showing the pre-action state until the next 5-min poll. The Grid-flow card now stamps the reading age (`… · Ns ago`) so a laggy "EXPORTING" is honestly shown as stale, not live.

## 1.32.2
- Feed-in forecast: align the sign to the live feed-in reading (Amber sometimes reports the forecast with the opposite/raw sign), so exporting earnings stay positive and the sell logic/chart read correctly. (Confirmed entity: `sensor.amber_feed_in_forecast`.)

## 1.32.1
- **Fix: feed-in forecast wasn't showing.** It lives on a dedicated sensor (like the buy forecast's `..._general_forecast`), not as an attribute of the feed-in *price* sensor. Added `amber_feedin_forecast_entity` (default `sensor.amber_feed_in_forecast`) and read its `forecasts` (falls back to a `forecasts` attribute on the price sensor). The Feed-in card now shows `forecast: Npt` / `no forecast (check entity)` so you can verify the entity name.

## 1.32.0
- **Feed-in (export) price forecast — on the chart and in the logic.** foxctl now reads Amber's feed-in forecast (the `forecasts` attribute of the feed-in sensor) and plots it as a green dashed line alongside buy/AEMO. Crucially, the **"would sell" windows, the SoC projection, and the shadow planner now use the real per-slot feed-in forecast** to decide when to sell, instead of the buy-price proxy — so sell windows line up with when export is actually lucrative. Falls back to the buy-price proxy where no feed-in forecast is available.

## 1.31.1
- **Fix work mode freezing.** The FoxESS work-mode read (every Nth cycle) wasn't error-wrapped, so a flaky/rate-limited settings call would throw, crash that cycle, and leave the cached work mode frozen at the last value it read (e.g. 'backup'). Now it's resilient: on failure it keeps the cached value, logs `work mode read failed …`, and the cycle continues. The Work mode card shows how long ago it was read (and flags ⚠️ stale > 30 min) so a failing read is visible.

## 1.31.0
- **Solar range now comes from your actual generation history, not a forecast-vs-actual calibration.** The chart draws a "typical solar" overlay — hour-of-day **average + min/max band** straight from the backfilled `pvPower` actuals (`forecast_profiles` now exposes `solar_min`/`solar_max`). It shows as soon as there's ≥1 day of generation, with no forward forecast-pairing needed (orange avg line + dashed min/max edges). The Solcast bell stays as the weather-aware "today's forecast"; the calibration-spread band is gone in favour of the real-data range.

## 1.30.1
- **Solar forecast error band is now legible.** It was a same-gold wash over the gold bell (invisible). Now drawn as clear dashed dark-goldenrod **min/max edge lines** + a faint fill, so the calibration's forecast-error envelope actually shows.
- **Solar calibration engages sooner:** `SOLAR_CAL_MIN` 5 → 3 days. (It still can't be backfilled — it needs the *external forecast made for each past day*, which only accrues going forward; the 7-day backfill only had actual generation.)

## 1.30.0
- **Interim daily car-charge cap** (until a real car-SoC sensor exists). `ev_session_cap_kwh` (default 30) limits how much the **auto-divert** charges the car per day — measured from the `ev_energy` counter, anchored to a 4am day boundary. Once hit, auto-divert holds the car off until the day rolls over or you press **Force car charge** (which clears the cap and starts a fresh session). Manual force-charge always overrides it. The EV status line shows `… · 12.3/30kWh today`. On the ~42 kWh Abarth pack, ~30 kWh ≈ a 0→80% day. Set to 0 to disable.

## 1.29.1
- Add a clear **⏹ stop** button directly on the Force-charge and SELL rows (cancels the active manual override → back to auto). Previously the only stop was the easy-to-miss "cancel override" on the Floor row.

## 1.29.0
- **EV divert: charge the car alongside a cheap-grid battery top-off.** The battery-priority gate now applies ONLY to the limited solar-surplus path (give spare solar to the battery before a sell). On the cheap-GRID path it no longer holds the car off — cheap grid import is unlimited, so the car charges at the same cheap price while the battery finishes topping up. Previously the car waited for the battery even when both could charge cheaply from the grid. Reason strings updated.

## 1.28.0
- **Force car charge for N hours.** New 🔌 quick-control buttons (1/2/3/4/6h) that turn the car-charger socket ON regardless of the divert economics and the battery gate, then auto-revert to auto divert (✖ stop → auto). Endpoint `/api/ev_charge?h=N` / `/api/ev_off`.
- **Page reorganised.** Decision controls (quick-controls + action buttons) moved to the top under the recommendation so they're not buried; the at-a-glance status cards moved below the charts.
- **Three charts instead of one** — **Next 6 hours**, **Next 18 hours** (large), and **Full forecast** (everything the feed provides). `render_forecast_svg` now takes a horizon + a unique container id, x-axis ticks scale to the horizon, and each chart keeps its own resize. Bigger by default.

## 1.27.0
- **Grid-flow card.** The page now shows whether you're importing or exporting right now — `⬆ EXPORTING 3.2 kW @ $0.67/kWh` (highlighted green) or `⬇ importing 2.5 kW @ $0.20` — so the live grid direction + price is visible at a glance. (Note: a high feed-in like 67c means selling beats car-charging, so the EV divert correctly stays off then; divert is for cheap export ≤ $0.10.)

## 1.26.2
- **Fix dashboard crash** introduced in 1.26.0: the EV card concatenated the emoji with the numeric `ev_kw` (`str + float`), 500-ing the page once the charger reported power. Now formats safely. Added a full `render()` smoke test so a page-crashing template bug can't ship again.

## 1.26.1
- Default `ev_charger_switch` to `switch.6294ha_series_2_socket_2` (the Tuya socket feeding the car charger) so EV solar-diversion activates on update. Power/energy tracking uses `sensor.6294ha_series_2_power` (the existing `ev_power_entity`).

## 1.26.0
- **EV solar-diversion.** foxctl can now turn a car-charger power point ON when export is too cheap to bother (feed-in ≤ `ev_divert_feedin_max`, default $0.10, while actually exporting ≥ `ev_divert_min_export_kw`) and/or when grid import is cheap (≤ the charge-start price), and OFF otherwise — soaking surplus into the car instead of dumping it to the grid. It **yields to the house battery**: while the shadow planner is pre-charging the battery toward its target before a sell (`battery_priority`, default on), the charger stays off so the battery fills first. Edge-triggered with a dwell (`ev_divert_min_dwell_min`) so it won't cycle the charger; needs `control.allow_control`. Set `ev_charger_switch` to your HA switch entity to enable (blank = off).
- **EV tracking sensors.** New `sensor.foxctl_ev_power` (kW), `foxctl_ev_energy` (cumulative kWh, Energy-dashboard ready), and `foxctl_ev_charger` (on/off), plus an EV card on the page. Uses the existing `ev_power_entity` for measurement.
- 4 new tests (34 total).

## 1.25.0
- **More HA sensors (live).** foxctl now also publishes a cumulative **House load energy** counter (for the Energy dashboard) plus the forecast/planner metrics: plan target SoC + action, energy shortfall, calibrated solar remaining/tomorrow, solar forecast bias, and avg daily load — all updating every cycle (~5 min) as `sensor.foxctl_*`.
- **HA history backfill.** New `backfill-ha` CLI + a "⤓ Backfill 7d → HA stats" button + `/api/backfill_ha` endpoint that import the last N days of hourly load + solar into HA long-term statistics via the WebSocket `recorder/import_statistics` API (external stats `foxctl:load_energy` / `foxctl:solar_energy`). Hourly is HA's import resolution — 5-min raw history can't be backfilled via any public API; the live 5-min sensors cover "now" going forward. No-data days are skipped per metric. Adds the `websocket-client` dep.

## 1.24.0
- **Shadow planner: arbitrage fill.** The planner now goes beyond covering load requirements — in a cheap slot it fills toward `max_soc` to capture a profitable spread when a future sell-window exists (buy-cheap, sell-into-the-spike), gated on `sell_thr × efficiency > buy price` and bounded by the actual export capacity of those future windows. Still SHADOW (does not drive control); the orange ideal-SoC line will now rise toward full before a forecast price spike. Degrades to requirement-only when there's no profitable spread. 2 new tests (29 total).

## 1.23.0
- **Forecasting Phase 4: shadow-mode planner (receding-horizon "ideal SoC line").** A new `plan_soc_trajectory` computes a requirement-aware SoC plan over the forecast horizon: a backward pass builds a minimum-SoC **envelope** (every expensive future slot's net-load must already be stored entering it; cheap slots relax the requirement), and a forward pass simulates the cost-minimising dispatch that follows it (solar first, grid-charge only in cheap slots up to the envelope, sell above the threshold while above survival). It runs every cycle in **SHADOW MODE — it does NOT drive control**: the ideal SoC line + min-SoC floor envelope are drawn on the chart (orange dashed) next to the heuristic projection, the recommendation card shows the plan's action vs the heuristic, and divergences are logged. This is the MPC reference trajectory to validate before it's ever allowed to act. 5 new tests (27 total).

## 1.22.5
- **Fix averages polluted by no-data days.** The 21-day backfill window includes days before the system/panels existed, which come back as all-zeros. Those were being averaged in, dragging the load and (especially) solar means toward zero. `forecast_profiles` now excludes no-data days **per metric** (a day with ~0 total load = no telemetry; ~0 total solar = pre-panels), so the load average uses real load days and the solar average uses only generating days. The calibration also ignores zero-actual days. Usage card shows the valid-day counts (load / solar).

## 1.22.4
- Usage card's daily **avg/min/max now come from the FoxESS history store** (sum of each stored day's hourly `loads`), matching the chart's source, once ≥3 days are backfilled. `today` stays from live integration (the store only holds completed days); EV is unchanged. Falls back to self-integration until the backfill matures.

## 1.22.3
- Chart now shows a **solar forecast error band** around the solar bells, derived from the Phase-3 calibration's actual-vs-forecast spread (drawn only once the calibration has enough samples). Right-axis scale widened so the band's top doesn't clip.

## 1.22.2
- Chart usage overlay now shows the hour-of-day **min–max range as a shaded band** around the average line (same source as the line — FoxESS history or self-integrated). Hover shows the avg plus the (min–max) range at that time.

## 1.22.1
- Usage card now shows the daily **range (min–max) and average** kWh/day over the sampled days, not just the average.

## 1.22.0
- **Forecasting Phase 3: solar forecast calibration.** foxctl now records the external (Forecast.Solar) full-day forecast each day and pairs it with the actual generation from the forecast store (integrated `pvPower`) to learn a per-site bias = mean(actual)/mean(forecast). Once ≥5 completed days are sampled, the clamped bias (±, range 0.5–1.6) is applied to the forward solar (remaining-today + tomorrow) that feeds the survival/shortfall calc, the solar bells, and the SoC projection — correcting this site's systematic forecast optimism/pessimism. No-op (bias 1.0) until it has learned, so behaviour is unchanged for the first ~5 days. The Solar forecast card shows the bias and learning progress; raw forecast values are retained for display. Persisted to `/data`. 3 new tests (21 total).

## 1.21.0
- **Forecasting Phase 2: real hour-of-day profiles from FoxESS history.** A persistent forecast store (`/data`) backfills the last 21 days of per-hour **load** (`report` `loads`) and **solar** (`history` `pvPower`, integrated trapezoidally — `generation` is inverter throughput incl. battery, not PV, so it's deliberately not used). Fetches at most one day per cycle and one per 2 min, so a full backfill spreads gently over ~an hour and stays well inside the FoxESS API quota; then one incremental day daily. Once ≥3 days are stored, the load profile that drives the shortfall/survival calcs and the chart usage overlay switches from the slow self-integrated estimate (needs ~2 weeks) to the FoxESS history (accurate in days). The Usage card shows the profile source + backfill progress. Validated end-to-end against the live inverter. Solar profile is stored for Phase 3 (forecast calibration).

## 1.20.0
- **Forecasting Phase 1: FoxESS history client + read-only spike.** New `FoxESS.report()` (hourly kWh per variable for a day — `loads`, `generation`, `feedin`, `gridConsumption`, charge/discharge) and `FoxESS.history()` (raw sub-hourly telemetry). Both read-only. New `foxess_probe.py` (shipped in the image) confirms the data shape/granularity against the real inverter before we build the forecast store — run `FOXCTL_CONFIG=/data/.config/foxctl/config.json python3 /foxess_probe.py --days 3`. Request-shape unit tests added (15 tests total). No behaviour change yet; this is the data-acquisition foundation for the load/solar forecasts.

## 1.19.0
- **Chart: projected SoC + "would sell" windows.** A forward projection rolls the current SoC through the forecast (grid-charge while buy ≤ charge-start, export while price ≥ sell threshold and SoC is above the survival floor, else solar−load) and draws it as a teal % curve with the survival-floor line. Slots where it would sell are shaded pink, and hover now shows the projected SoC and a SELL tag at that time. Estimate only (uses the buy-price forecast as an export proxy until a per-slot feed-in forecast exists) — labelled as such in the legend. First concrete piece of the forecasting plan.

## 1.18.0
- **Restore the v1.15.1 fixes that v1.16.0 silently reverted.** v1.16.0 was committed on a pre-1.15.1 copy of `foxctl.py`, dropping `apply_and_record` (so `/api/apply` again left the dashboard header showing a stale `applied`) and `refresh_control` (so the loop went back to capturing the auto-apply flag once at startup, needing a restart to pick up `allow_control`/`auto_apply` changes). Both are back.
- **Chart: hover-for-time + usage overlay.** Mousing over the forecast chart now shows a cursor line + tooltip with the clock time, Amber price, and expected usage at that point. The rolling hour-of-day usage profile is overlaid as a dashed purple curve on the right (kW) axis, alongside the solar estimate.

## 1.17.0
- **Hour-of-day usage profile.** foxctl now records base-load (EV excluded) by hour-of-day across recent days and uses it to predict the remaining-today / overnight load — replacing the flat `daily/24` assumption in the shortfall and ZeroHero-survival calcs. Needs ~2 weeks to be solid; improves daily. EV charging (sensor.6294ha_series_2_power) is tracked separately so it doesn't distort the base profile.

## 1.16.1
- Solar-defer is now energy-balance aware: foxctl no longer skips a cheap grid-charge on a momentary PV surplus when the day projects a shortfall (usable battery + remaining solar < remaining load). Fixes it sitting idle at the day's cheapest price while heading for an evening shortfall.

## 1.16.0
- **ZeroHero mode** (GloBird) — set `tariff_mode: zerohero` to switch from Amber price-forecasting to a time-of-use schedule: grid-charge FREE 11:00–14:00 to max SoC; 18:00–21:00 cover all load from battery (zero grid import = $1/day credit) and export surplus at 9c down to a computed overnight survival floor; run off battery otherwise. No LLM/price logic in this mode. Defaults to `amber` (no change until you flip it).

## 1.15.1
- **Fix "Apply recommendation" appearing to do nothing.** The `/api/apply` button ran the apply but never wrote the result back into the shared snapshot the dashboard renders, so the header kept showing `applied: None` after a successful apply (and "Evaluate now" reset it to None for up to a poll interval). The outcome is now persisted to the header (`apply_and_record`). Long-standing since v1.3.1, not a regression.
- **Live control toggles.** The loop now reloads the `control` block (`allow_control`, `auto_apply`, `set_force_charge`, …) from the config each cycle (`refresh_control`), so toggling auto-apply takes effect on the next cycle instead of needing a process restart. In-memory tuned strategy params are untouched.
- First unit tests (`tests/test_foxctl.py`, stdlib `unittest`): force-charge decision branch, foundation price-ceiling veto, apply-persists-to-header, and live control reload. Run with `python3 -m unittest discover -s tests`.

## 1.15.0
- Full single-poller telemetry set: foxctl now also publishes per-string PV (pv1-6), battery charge/discharge power, and **cumulative kWh energy counters** (grid import/export, battery charge/discharge, solar) for the HA Energy dashboard. 21 sensors total. Prepares removal of foxess-ha.

## 1.14.0
- **foxctl is now the single FoxESS poller.** Telemetry (SoC, PV, load, grid import/export, battery power) comes straight from the FoxESS API each cycle and is **published to MQTT discovery** as `sensor.foxctl_*` (device "FoxESS (foxctl)"), plus the live charge-start price and target SoC. Decisions use foxctl's own poll — no dependency on the foxess-ha integration, which can be disabled to end the dual-poller rate-limit freezes. On a FoxESS fetch failure it reuses the last good values and holds control (stale safety). `publish_telemetry` option.

## 1.13.1
- Fix stale price: foxctl only syncs its poll cadence to the foxess-HA sensor when that sensor is fresh. When foxess-HA is frozen (it intermittently does), it was anchoring the schedule to a dead timestamp and drifting, so the displayed Amber price went several minutes stale. Now falls back to a steady fixed poll interval, keeping the price current.

## 1.13.0
- **Auto-sell on silly-high feed-in.** When the Amber feed-in price reaches `sell_price` (default $0.50), the auto-policy force-discharges the battery to the grid — but only down to a computed **overnight survival floor** (covers expected load until tomorrow's solar ramp, minus remaining solar today), so it never strands you. Sends a notification when it starts (`notify_on_sell`). Toggle with `auto_sell`.
- **Set-baseline panel** at the bottom of the foxctl page: type permanent **buy floor** and **sell threshold** values (persisted) — no need to open the add-on config page. Relax/increase remain the quick temporary nudges.

## 1.12.0
- **Quick-control buttons** on the foxctl page, backed by a manual-override engine the auto-loop respects (a press isn't undone by the next cycle) and that auto-reverts when its timer ends:
  - ⚡ **Force-charge 1–6 h** (charge battery from grid to max SoC).
  - 💰 **SELL 1–6 h** — force-discharge the battery to the grid (export/sell). NOTE: uses the FoxESS ForceDischarge scheduler mode — verify on your device.
  - 🪙 **Relax / increase floor** (±0.03) and **cancel override → auto**.
- **Persisted floor override:** the charge floor is now a saved base setting the floor buttons change, and that a *forceful* operator note can update lastingly (the LLM may set `base_floor`, bounded by the ceiling, logged). Notes guide the dynamic layer; forceful notes can move the base.

## 1.11.0
- **Operator steering note.** A free-text box on the foxctl page (persisted to /data) that's fed to the dynamic LLM as a *priority* instruction — e.g. "let the battery discharge until the ~9c midday trough, then charge." While a note is active the charge floor is relaxed (still capped by the ceiling + SoC limits) so guidance to wait for cheaper prices actually takes effect; saving a note forces an immediate re-evaluation. Clear the box to return to normal.

## 1.10.0
- **"Needs you" notifications.** The dynamic LLM now emits an `operator_action` only when it genuinely wants you to change something it can't (a foundation setting like the ceiling/floor/capacity). foxctl pushes a notification when there's a *new* such suggestion (de-duped, rate-limited by `notify_min_gap_min`, default 180 min) — so you're pinged to review only when it matters, not on every auto-tweak. Shown as a "📣 Needs you" banner on the page too. Options: `notify_on_llm_action`, `notify_min_gap_min`.
- **Resizable forecast chart.** Drag the bottom-right corner to resize; the size persists across the page's auto-refresh (localStorage). Bigger default size + larger fonts for readability.

## 1.9.2
- Packaging fix: the version string had been stuck at 1.8.0, so the 1.8.1/1.9.0/1.9.1 changes (feed-in entity fix, rolling consumption, EV hook, solar today_total) never advanced in the add-on store. This bump ships them all.

## 1.9.1
- Fix misleading solar forecast in the LLM context: it was only sent remaining-today (small in the evening), which it mislabelled as "today". Now sends today_total_forecast + remaining_today_only + tomorrow with an explicit note; UI shows today total too.

## 1.9.0
- **Rolling measured consumption.** foxctl integrates `foxess_load_power` itself into per-day kWh buckets (persisted to /data, restart-safe) and feeds the dynamic policy a real rolling daily-usage average instead of a static guess — once 2+ days are recorded. New "Usage (rolling avg)" card.
- **EV-aware (optional).** Set `ev_power_entity` to a Tuya/energy-monitoring plug's power sensor and foxctl tracks EV charging separately (total vs base load), so an occasional car charge doesn't distort the predictable base load fed to the LLM. Inert until configured.

## 1.8.1
- Fix feed-in entity: use sensor.amber_feed_in_price (the site's home_feed_in_price is empty) so the export price reaches the LLM.

## 1.8.0
- **charge_start_floor (default $0.15):** the controller is always willing to grid-charge at/below this price; the LLM may raise charge_start_price up to the ceiling but never below the floor. Stops the dynamic policy from chasing the exact forecast trough and leaving the battery flat in winter. Effective = clamp(max(LLM, floor), 0, ceiling).
- **Energy-balance inputs to the LLM:** battery capacity + stored kWh (configurable `battery_capacity_kwh`, default 30 → 40 soon), typical daily load, and the live **feed-in price** — so it plans by usage vs capacity vs solar forecast, not price alone.
- **Feed-in is now enabled** (export earns the Amber feed-in price): SITE_FACTS/GOAL updated — solar is no longer "wasted", store-vs-export is weighed, never import-to-export.
- **Charge persistence:** once a force-charge starts, a flaky FoxESS scheduler read no longer drops it mid-window (fixes the hysteresis losing track and stopping early).

## 1.7.1
- Forecast chart: **estimated solar overlay** (sunny times + intensity, half-sine scaled to forecast kWh, on a right kW axis), **real clock times** on the x-axis (was +Nh), Amber legend now **blue** to match the line, and a **wider page** (1280px) with a bigger chart.

## 1.7.0
- **Forecast.Solar wired into the dynamic policy.** foxctl sums the per-plane `energy_production_*` sensors (remaining-today + tomorrow kWh) and feeds them to the LLM, so it leaves battery headroom when real solar is coming and only grid-charges overnight when tomorrow looks poor — replacing the coarse weather string. New solar-forecast card on the web UI. Entity lists in `foxctl_config.json` (`solar_fc_remaining_entities` / `solar_fc_tomorrow_entities`).

## 1.6.1
- **The LLM now sees the whole 18h forecast.** It was only handed 1h of Amber (`forecast[:12]`); it now gets a ~30-min-spaced digest of the next 18h with the cheapest/peak points + times, so it can actually plan against tonight's trough and the evening peak instead of extrapolating.
- **Forecast-horizon chart** on the web UI: an SVG of the 18h Amber + AEMO curves with the LLM's charge-start price, the foundation ceiling, shaded "would grid-charge" windows, the now marker, and the cheapest/peak points — so you can see what the policy is reasoning over.

## 1.6.0
- **Two-tier policy.** A deterministic **foundation** (hard guardrails you must override yourself): absolute price ceiling (`price_ceiling`, never grid-charge above it), SoC floor/cap (`reserve_soc`/`max_soc`), stale-telemetry hold, and "spend $0, else cheapest point only". On top, a **dynamic** layer where the LLM tunes two knobs each interval — `charge_start_price` and `target_soc` — always clamped to the foundation. Toggle with `dynamic_policy`.
- The LLM is now given the goal (spend $0 → capture all solar → cheapest import) and site facts: no feed-in (surplus solar is wasted, so store it / leave headroom only when real solar is coming), EA116 flat network with no demand charge, and the season, so it tops up cheaply in low-solar winter and leaves headroom in high-solar summer.
- **Demand window no longer blocks charging** (EA116 has no $/kW demand charge) — `avoid_demand_window` now defaults false.
- Web UI shows the active foundation + dynamic knobs and who set them.

## 1.5.2
- LLM review now critiques against foxctl's *actual* policy: the prompt includes the controller's real thresholds (charge start/stop price, target/reserve SoC, solar-defer, demand-window avoidance, horizon pre-charge + ≥0.35 peak rule) and its full reason string, so it stops faulting rules the controller already has.
- Three-way rating AGREE / REFINE / DISAGREE (was AGREE/DISAGREE). Notifications fire only on DISAGREE.
- Staleness safety: when HA sensors are frozen *and* the FoxESS fallback fetch fails (telemetry_source=HA(stale)), control is held (no inverter writes), the web UI flags it, and an optional notification fires (notify_on_stale).

## 1.5.1
- Robust telemetry: if HA foxess sensors are missing or stale (>15 min old), pull SoC/PV/load straight from the FoxESS API. Fixes decisions made on frozen data.


## 1.5.0
- Push notifications via the HA notify service when a decision is worth a look: LLM disagrees, price spike, or negative price. Edge-triggered + de-duped. Options: notify_enabled, notify_service, notify_on_llm_disagree/spike/ludicrous.


## 1.4.0
- Horizon-aware charging: pre-charge in the cheapest forward window before a forecast price peak (horizon_charge / horizon_hours / horizon_window_margin).
- Advisory LLM review of each decision (Anthropic API, Haiku) — logged, shown on the web UI, with a "review now" button. Advisory only; never controls the battery. Options: llm_review, anthropic_api_key, llm_model, llm_interval_min.


## 1.3.2
- Fix: webui must use [PORT:8770] placeholder (HA OS 18 supervisor rejected the literal port, detaching the add-on).

## 1.3.1
- Clearer demand-window card label ("won’t grid-charge battery").

## 1.3.0
- Web UI: respect dark mode (prefers-color-scheme).
- Web UI: show Amber demand-window status card.

## 1.2.0
- Add `avoid_demand_window` switch: skip grid force-charge while Amber's demand
  window is active (avoids peak-demand charges).
- Add "Open Web UI" button for foxctl (webui).

## 1.1.0
- Expose tuning thresholds + control flags as add-on options
  (charge_start_price, charge_stop_margin, target_soc, reserve_soc,
  force_charge_power_kw, solar_defer_kw, defer_if_cheaper_by, poll_seconds,
  allow_control, auto_apply, set_work_mode, set_force_charge).

## 1.0.0
- Initial release: foxctl (price-aware FoxESS control) + nemfuel (NEM fuel-mix
  feed) packaged as a single Home Assistant add-on.
