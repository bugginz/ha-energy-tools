#!/usr/bin/env python3
"""Assemble foxctl/nemfuel config from add-on options + baked templates.

Complex/rarely-changed settings (GloBird tariff profile, HA entity names) live in
the baked template foxctl_config.json. The tunable thresholds + control flags are
add-on OPTIONS and override the template here.
"""
import json

opt = json.load(open("/data/options.json"))

# ---- foxctl ----
fc = json.load(open("/foxctl_config.json"))
fc["foxess"]["token"] = opt["foxess_token"]
fc["foxess"]["sn"] = opt["foxess_sn"]
fc["ha"]["url"] = "http://supervisor/core"
fc["ha"]["token_file"] = "/data/.config/sen66/ha_token"
fc["state_dir"] = "/data/.config/foxctl"   # persistent across restarts/updates (rolling consumption)
if opt.get("ev_power_entity"):
    fc["ha"]["ev_power_entity"] = opt["ev_power_entity"]
if opt.get("ev_voltage"):
    fc["ha"]["ev_voltage"] = float(opt["ev_voltage"])
# Solar diversion to a car-charger power point (needs allow_control). switch="" disables.
fc["ev_divert"] = {
    "switch": opt.get("ev_charger_switch", ""),
    "feedin_max": float(opt.get("ev_divert_feedin_max", 0.10)),
    "allow_grid": bool(opt.get("ev_divert_allow_grid", True)),
    "min_export_kw": float(opt.get("ev_divert_min_export_kw", 1.0)),
    "min_soc": int(opt.get("ev_divert_min_soc", 0)),
    "battery_priority": bool(opt.get("ev_divert_battery_priority", True)),
    "min_dwell_min": int(opt.get("ev_divert_min_dwell_min", 10)),
    "session_cap_kwh": float(opt.get("ev_session_cap_kwh", 30)),
}
# foxctl is the single FoxESS poller: publish telemetry to MQTT for the dashboards.
fc["mqtt"] = {"publish": bool(opt.get("publish_telemetry", True)),
              "host": "core-mosquitto", "port": 1883,
              "user": opt.get("mqtt_user", ""), "pass": opt.get("mqtt_pass", "")}

S = fc["strategy"]
for k in ("force_charge_power_kw", "solar_defer_kw",
          "battery_capacity_kwh", "typical_daily_load_kwh",
          "ev_charge_kw", "ev_expected_kwh"):
    if k in opt:
        S[k] = float(opt[k])
for k in ("reserve_soc", "max_soc", "inverter_min_soc"):
    if k in opt:
        S[k] = int(opt[k])
if "poll_seconds" in opt:
    fc["poll_seconds"] = int(opt["poll_seconds"])
if "avoid_demand_window" in opt:
    S["avoid_demand_window"] = bool(opt["avoid_demand_window"])
if "topup_to_target" in opt:
    S["topup_to_target"] = bool(opt["topup_to_target"])
if opt.get("tariff_mode"):
    S["tariff_profile"] = str(opt["tariff_mode"])
if "sell_price" in opt:
    S["sell_price"] = float(opt["sell_price"])
if "auto_sell" in opt:
    S["sell_enabled"] = bool(opt["auto_sell"])

C = fc["control"]
for k in ("allow_control", "auto_apply", "set_work_mode", "set_force_charge"):
    if k in opt:
        C[k] = bool(opt[k])

# ---- notifications ----
N = fc.setdefault("notify", {})
N["enabled"] = bool(opt.get("notify_enabled", False))
N["service"] = opt.get("notify_service", "notify.mobile_app_phoney")
N["on_stale"] = bool(opt.get("notify_on_stale", True))
N["on_sell"] = bool(opt.get("notify_on_sell", True))
N["min_gap_min"] = int(opt.get("notify_min_gap_min", 180))

json.dump(fc, open("/data/.config/foxctl/config.json", "w"), indent=2)

# ---- nemfuel ----
nf = json.load(open("/nemfuel_config.json"))
nf["api_key"] = opt["oe_key"]
nf["region"] = opt.get("region", "NSW1")
json.dump(nf, open("/data/.config/nemfuel/config.json", "w"), indent=2)

# ---- MQTT creds for nemfuel ----
with open("/data/.config/sen66/mqtt.env", "w") as f:
    f.write("MQTT_HOST=core-mosquitto\nMQTT_PORT=1883\n"
            "MQTT_USER=%s\nMQTT_PASS=%s\n" % (opt.get("mqtt_user", ""), opt.get("mqtt_pass", "")))

print("[energy_tools] config written (tariff=%s max_soc=%s%% control=%s/%s)" % (
    S.get("tariff_mode"), S.get("max_soc"),
    C.get("allow_control"), C.get("set_force_charge")))
