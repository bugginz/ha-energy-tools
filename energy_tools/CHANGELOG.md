# Changelog

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
