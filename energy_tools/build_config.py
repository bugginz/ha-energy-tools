#!/usr/bin/env python3
"""Assemble foxctl/nemfuel config from add-on options + baked templates.

Complex/rarely-changed settings (price bands, charge/avoid bands, HA entity names)
live in the baked template foxctl_config.json. The tunable thresholds + control
flags are add-on OPTIONS and override the template here.
"""
import json

opt = json.load(open("/data/options.json"))

# ---- foxctl ----
fc = json.load(open("/foxctl_config.json"))
fc["foxess"]["token"] = opt["foxess_token"]
fc["foxess"]["sn"] = opt["foxess_sn"]
fc["ha"]["url"] = "http://supervisor/core"
fc["ha"]["token_file"] = "/data/.config/sen66/ha_token"

S = fc["strategy"]
for k in ("charge_start_price", "charge_stop_margin", "force_charge_power_kw",
          "solar_defer_kw", "defer_if_cheaper_by", "price_ceiling"):
    if k in opt:
        S[k] = float(opt[k])
for k in ("target_soc", "reserve_soc", "max_soc"):
    if k in opt:
        S[k] = int(opt[k])
if "poll_seconds" in opt:
    fc["poll_seconds"] = int(opt["poll_seconds"])
if "avoid_demand_window" in opt:
    S["avoid_demand_window"] = bool(opt["avoid_demand_window"])
if "dynamic_policy" in opt:
    S["dynamic_policy"] = bool(opt["dynamic_policy"])
if "horizon_charge" in opt:
    S["horizon_charge"] = bool(opt["horizon_charge"])
if "horizon_hours" in opt:
    S["horizon_hours"] = int(opt["horizon_hours"])
if "horizon_window_margin" in opt:
    S["horizon_window_margin"] = float(opt["horizon_window_margin"])

C = fc["control"]
for k in ("allow_control", "auto_apply", "set_work_mode", "set_force_charge"):
    if k in opt:
        C[k] = bool(opt[k])

# ---- LLM review (advisory) ----
L = fc.setdefault("llm", {})
L["enabled"] = bool(opt.get("llm_review", False))
L["api_key"] = opt.get("anthropic_api_key", "")
L["model"] = opt.get("llm_model", "claude-haiku-4-5-20251001")
L["interval_min"] = int(opt.get("llm_interval_min", 30))

# ---- notifications ----
N = fc.setdefault("notify", {})
N["enabled"] = bool(opt.get("notify_enabled", False))
N["service"] = opt.get("notify_service", "notify.mobile_app_phoney")
N["on_llm_disagree"] = bool(opt.get("notify_on_llm_disagree", True))
N["on_spike"] = bool(opt.get("notify_on_spike", True))
N["on_ludicrous"] = bool(opt.get("notify_on_ludicrous", True))
N["on_stale"] = bool(opt.get("notify_on_stale", True))

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

print("[energy_tools] config written (start=%.3f stop=+%.3f target=%s%% control=%s/%s)" % (
    S.get("charge_start_price"), S.get("charge_stop_margin"), S.get("target_soc"),
    C.get("allow_control"), C.get("set_force_charge")))
