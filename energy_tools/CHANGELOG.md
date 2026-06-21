# Changelog

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
