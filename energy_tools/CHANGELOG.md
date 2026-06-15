# Changelog

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
