#!/usr/bin/env python3
"""NEM fuel-mix -> Home Assistant via MQTT Discovery.

Pulls live generation by fuel type for a NEM region from the OpenElectricity API
(openelectricity.org.au) and publishes per-fuel power (MW) + renewables% to HA via
MQTT discovery, so HA auto-creates a "NEM <region> Generation" device.

Config: ~/.config/nemfuel/config.json {api_key, network, region, interval_seconds, include_rooftop}
MQTT creds reused from ~/.config/sen66/mqtt.env.

Run: python3 nemfuel.py            # loop
     python3 nemfuel.py --once     # single fetch + publish
     python3 nemfuel.py --dry-run  # fetch + print, no MQTT
"""
from __future__ import annotations
import argparse, json, logging, os, signal, sys, time, urllib.request, urllib.error
from datetime import datetime, timedelta
from pathlib import Path
import paho.mqtt.client as mqtt

log = logging.getLogger("nemfuel")
OE_BASE = "https://api.openelectricity.org.au"
DISCOVERY_PREFIX = "homeassistant"

# fueltech groups we publish, with friendly name + whether renewable
FUELS = [
    ("coal", "Coal", False), ("gas", "Gas", False), ("distillate", "Distillate", False),
    ("hydro", "Hydro", True), ("wind", "Wind", True), ("solar", "Solar (utility)", True),
    ("bioenergy", "Bioenergy", True), ("rooftop_solar", "Rooftop Solar", True),
    ("battery_charging", "Battery charging", False),
    ("battery_discharging", "Battery discharging", False),
]
RENEWABLE = {f for f, _, r in FUELS if r}
# generation sources counted in the total (excludes battery_charging which is load)
GEN = {"coal", "gas", "distillate", "hydro", "wind", "solar", "bioenergy",
       "rooftop_solar", "battery_discharging"}


def active_fuels(cfg) -> list:
    """FUELS minus rooftop unless explicitly enabled (rooftop needs a paid OE plan)."""
    return [(f, n, r) for f, n, r in FUELS if f != "rooftop_solar" or cfg.get("include_rooftop")]


def load_cfg() -> dict:
    return json.loads((Path.home() / ".config/nemfuel/config.json").read_text())


def load_mqtt_env():
    p = Path.home() / ".config/sen66/mqtt.env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def oe_get(api_key: str, path: str) -> dict:
    req = urllib.request.Request(OE_BASE + path, headers={
        "Authorization": "Bearer " + api_key, "Accept": "application/json", "User-Agent": "nemfuel"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _latest_by_fuel(api_key, network, region, minutes=40) -> dict:
    end = datetime.now().astimezone()
    start = end - timedelta(minutes=minutes)
    fmt = "%Y-%m-%dT%H:%M:%S"
    q = (f"/v4/data/network/{network}?metrics=power&network_region={region}"
         f"&secondary_grouping=fueltech_group&date_start={start.strftime(fmt)}&date_end={end.strftime(fmt)}")
    d = oe_get(api_key, q)
    out = {}
    for series in d.get("data", []):
        for r in series.get("results", []):
            if r["columns"].get("region") != region:
                continue
            ft = r["columns"].get("fueltech_group")
            pts = [p for p in r["data"] if p[1] is not None]
            if pts:
                out[ft] = round(pts[-1][1], 1)
    return out


def fetch_mix(cfg: dict) -> dict:
    raw = _latest_by_fuel(cfg["api_key"], cfg["network"], cfg["region"])
    mix = {f: float(raw.get(f, 0) or 0) for f, _, _ in active_fuels(cfg)}
    # rooftop solar lives in a separate (AEMO_ROOFTOP) network, 30-min
    if cfg.get("include_rooftop"):
        try:
            rt = _latest_by_fuel(cfg["api_key"], "AEMO_ROOFTOP", cfg["region"], minutes=90)
            mix["rooftop_solar"] = round(sum(v for v in rt.values() if v and v > 0), 1)
        except Exception as e:
            log.warning("rooftop fetch failed: %s", e)
    total = sum(mix[f] for f in GEN if mix.get(f, 0) > 0)
    renew = sum(mix[f] for f in RENEWABLE if mix.get(f, 0) > 0)
    mix["total_generation"] = round(total, 1)
    mix["renewables_pct"] = round(100 * renew / total, 1) if total else 0
    return mix


def mqtt_client():
    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="nemfuel")
    except (AttributeError, TypeError):
        c = mqtt.Client(client_id="nemfuel")
    if os.environ.get("MQTT_USER"):
        c.username_pw_set(os.environ["MQTT_USER"], os.environ.get("MQTT_PASS", ""))
    c.will_set("nemfuel/availability", "offline", qos=1, retain=True)
    return c


def publish_discovery(client, region, cfg):
    dev = {"identifiers": [f"nem_{region.lower()}"], "name": f"NEM {region} Generation",
           "manufacturer": "OpenElectricity", "model": "NEM fuel mix"}
    state_topic = f"nem_{region.lower()}/state"
    def cfg_for(oid, name, unit, icon):
        return {"name": name, "unique_id": oid, "object_id": oid, "state_topic": state_topic,
                "value_template": "{{ value_json.%s }}" % oid.split(region.lower()+"_")[1],
                "unit_of_measurement": unit, "state_class": "measurement",
                "availability_topic": "nemfuel/availability", "icon": icon, "device": dev}
    pre = f"nem_{region.lower()}_"
    items = [(pre + f, name, "MW", "mdi:transmission-tower") for f, name, _ in active_fuels(cfg)]
    items.append((pre + "total_generation", "Total generation", "MW", "mdi:flash"))
    items.append((pre + "renewables_pct", "Renewables", "%", "mdi:leaf"))
    for oid, name, unit, icon in items:
        client.publish(f"{DISCOVERY_PREFIX}/sensor/{oid}/config", json.dumps(cfg_for(oid, name, unit, icon)),
                       qos=1, retain=True)
    log.info("published discovery for %d sensors", len(items))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_cfg()
    load_mqtt_env()
    region = cfg["region"]
    state_topic = f"nem_{region.lower()}/state"

    if args.dry_run:
        print(json.dumps(fetch_mix(cfg), indent=2)); return 0

    client = mqtt_client()
    client.connect(os.environ.get("MQTT_HOST", "homeassistant.local"),
                   int(os.environ.get("MQTT_PORT", "1883")), 60)
    client.loop_start()
    publish_discovery(client, region, cfg)
    client.publish("nemfuel/availability", "online", qos=1, retain=True)

    running = True
    def stop(*_):
        nonlocal running; running = False
    signal.signal(signal.SIGINT, stop); signal.signal(signal.SIGTERM, stop)

    log.info("nemfuel -> mqtt (%s, every %ss)", region, cfg["interval_seconds"])
    try:
        while running:
            try:
                mix = fetch_mix(cfg)
                # strip region prefix for the JSON keys (templates expect bare fuel names)
                client.publish(state_topic, json.dumps({k: v for k, v in mix.items()}), qos=0, retain=True)
                log.info("renewables %.0f%% | coal %.0f wind %.0f solar %.0f rooftop %.0f gas %.0f MW",
                         mix["renewables_pct"], mix.get("coal", 0), mix.get("wind", 0),
                         mix.get("solar", 0), mix.get("rooftop_solar", 0), mix.get("gas", 0))
            except Exception as e:
                log.error("fetch/publish failed: %s", e)
            if args.once:
                break
            target = time.monotonic() + cfg["interval_seconds"]
            while running and time.monotonic() < target:
                time.sleep(min(1.0, target - time.monotonic()))
    finally:
        client.publish("nemfuel/availability", "offline", qos=1, retain=True)
        time.sleep(0.3); client.loop_stop(); client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
