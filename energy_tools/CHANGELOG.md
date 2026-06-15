# Changelog

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
