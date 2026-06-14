# Home Assistant — Energy Data Setup (Australia / NEM)

A practical guide to wiring live electricity **prices**, **wholesale market data**, and **generation fuel mix** into Home Assistant. Everything here is free (Amber needs you to be their customer); none of it is inverter/brand specific.

---

## 0. Prerequisites

- **Home Assistant** running (HA OS on a Pi/mini-PC is easiest).
- **HACS** installed — the community store, needed for two of the integrations. Guide: <https://hacs.xyz>
- **For the OpenElectricity fuel-mix feed only:** an MQTT broker + somewhere to run a tiny Python script (the HA box itself, or a spare Pi). Steps below.

---

## 1. AEMO NEM Pricing — wholesale market (free, no key)

Live + forecast wholesale spot price, demand, generation, interconnector flows, for your NEM region.

1. HACS → **Integrations** → ⋮ → **Custom repositories** → add `https://github.com/cabberley/HA_AemoNemData` (category: Integration). *(Or search HACS for "AEMO NEM Pricing".)*
2. Install, then **restart HA**.
3. **Settings → Devices & Services → Add Integration → "AEMO NEM Pricing"**.
4. Pick your **region** (NSW1, VIC1, QLD1, SA1, TAS1).
5. Done — you get `sensor.aemo_nem_<region>_*` entities.

> Tip: in the integration **options**, set the polling interval to **60s** (the source only updates every 5 min, so faster just wastes calls). It self-limits to ~528 calls/day, well under AEMO's 1,440/day cap.

---

## 2. Amber Electric — retail price (Amber customers only)

Live + forecast **retail** price (what you actually pay), feed-in/export price, renewables %, spike/demand flags.

1. Log in at **app.amber.com.au** → **Settings → Developers** (or **amber.com.au**) → **Generate API token**.
2. In HA: **Settings → Devices & Services → Add Integration → "Amber Electric"**.
3. Paste the token, select your site.
4. Done — `sensor.<name>_general_price`, `_general_forecast`, `_feed_in_price` (once solar/feed-in is active), `_renewables`, etc.

> Not an Amber customer? Skip this — AEMO (above) still gives you wholesale prices for free.

---

## 3. OpenElectricity — generation fuel mix (free key + small feed)

Live generation **by fuel type** (coal / gas / hydro / wind / solar / battery) + renewables %. This is the data behind those NEM "fuel mix" pie charts.

### 3a. Get an API key
Register free at **platform.openelectricity.org.au** → create an **API key** (COMMUNITY plan).

### 3b. Set up MQTT (if you don't have it)
1. **Settings → Add-ons → Add-on Store → Mosquitto broker** → Install → Start.
2. **Settings → Devices & Services** → it auto-discovers MQTT → **Configure** (or Add Integration → MQTT → broker `core-mosquitto`, port `1883`, your HA username/password).

### 3c. Run the feed
The old OpenNEM HACS integration is dead (API changed), so use this tiny script. Put it on the HA box or a Pi that can reach the broker. Requires `paho-mqtt` (`pip install paho-mqtt`, or it's already present on HA OS via an add-on like "Advanced SSH").

Save as `nemfuel.py`, edit the CONFIG block:

```python
#!/usr/bin/env python3
"""OpenElectricity NEM fuel mix -> Home Assistant via MQTT discovery."""
import json, time, urllib.request
from datetime import datetime, timedelta
import paho.mqtt.client as mqtt

# ---- CONFIG ----
OE_KEY   = "oe_xxxxxxxxxxxxxxxx"     # your OpenElectricity API key
REGION   = "NSW1"                    # your NEM region
MQTT_HOST= "homeassistant.local"     # or your broker IP
MQTT_USER= "your_mqtt_user"
MQTT_PASS= "your_mqtt_pass"
INTERVAL = 300                       # seconds (NEM is 5-min data)
# ----------------

BASE = "https://api.openelectricity.org.au"
FUELS = [("coal","Coal"),("gas","Gas"),("distillate","Distillate"),("hydro","Hydro"),
         ("wind","Wind"),("solar","Solar"),("bioenergy","Bioenergy"),
         ("battery_charging","Battery charging"),("battery_discharging","Battery discharging")]
RENEW = {"hydro","wind","solar","bioenergy"}
GEN   = {"coal","gas","distillate","hydro","wind","solar","bioenergy","battery_discharging"}

def fetch():
    end = datetime.now().astimezone(); start = end - timedelta(minutes=40)
    f = "%Y-%m-%dT%H:%M:%S"
    q = (f"{BASE}/v4/data/network/NEM?metrics=power&network_region={REGION}"
         f"&secondary_grouping=fueltech_group&date_start={start.strftime(f)}&date_end={end.strftime(f)}")
    req = urllib.request.Request(q, headers={"Authorization":"Bearer "+OE_KEY,"Accept":"application/json"})
    d = json.load(urllib.request.urlopen(req, timeout=30))
    raw = {}
    for s in d.get("data", []):
        for r in s.get("results", []):
            if r["columns"].get("region") != REGION: continue
            pts = [p for p in r["data"] if p[1] is not None]
            if pts: raw[r["columns"]["fueltech_group"]] = round(pts[-1][1], 1)
    mix = {f: float(raw.get(f, 0) or 0) for f, _ in FUELS}
    total = sum(mix[f] for f in GEN if mix.get(f,0) > 0)
    renew = sum(mix[f] for f in RENEW if mix.get(f,0) > 0)
    mix["total_generation"] = round(total, 1)
    mix["renewables_pct"] = round(100*renew/total, 1) if total else 0
    return mix

def main():
    try: c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="nemfuel")
    except Exception: c = mqtt.Client(client_id="nemfuel")
    c.username_pw_set(MQTT_USER, MQTT_PASS)
    c.connect(MQTT_HOST, 1883, 60); c.loop_start()
    region = REGION.lower(); st = f"nem_{region}/state"
    dev = {"identifiers":[f"nem_{region}"], "name":f"NEM {REGION} Generation", "manufacturer":"OpenElectricity"}
    for key, name in FUELS + [("total_generation","Total generation"),("renewables_pct","Renewables")]:
        oid = f"nem_{region}_{key}"
        unit = "%" if key == "renewables_pct" else "MW"
        cfg = {"name":name,"unique_id":oid,"object_id":oid,"state_topic":st,
               "value_template":"{{ value_json.%s }}" % key,"unit_of_measurement":unit,
               "state_class":"measurement","device":dev}
        c.publish(f"homeassistant/sensor/{oid}/config", json.dumps(cfg), qos=1, retain=True)
    while True:
        try:
            mix = fetch(); c.publish(st, json.dumps(mix), retain=True)
            print("renewables", mix["renewables_pct"], "%")
        except Exception as e: print("error:", e)
        time.sleep(INTERVAL)

if __name__ == "__main__": main()
```

Run it as a service so it survives reboots. On a systemd machine, `~/.config/systemd/user/nemfuel.service`:

```ini
[Unit]
Description=NEM fuel mix -> HA
After=network-online.target
[Service]
ExecStart=/usr/bin/python3 -u /home/USER/nemfuel.py
Restart=always
[Install]
WantedBy=default.target
```
Then: `systemctl --user enable --now nemfuel.service` (and `loginctl enable-linger USER` so it runs without login).

Entities `sensor.nem_<region>_*` appear in HA automatically via MQTT discovery.

> **Free-tier caveat:** utility-scale generation only — **no rooftop solar** (behind-the-meter; needs a paid OpenElectricity tier).

---

## 4. Solar forecast (optional — your own roof)

Predicts *your* generation, great for automations.

- **Solcast** (best for AU): register at **solcast.com.au** (free hobbyist tier, 10 calls/day) → create a rooftop site (location, **kW, tilt, azimuth**) → API key. Then HACS → **"Solcast PV Solar"** integration.
- Or **Forecast.Solar** — native HA integration, no key for basic use.

---

## 5. Weather (free)

- **met.no** — built into HA, no key (Add Integration → "Met.no").
- **OpenWeatherMap** — free API key if you want more detail.

---

## 6. Charts (optional but worth it)

For the price/forecast graphs and fuel-mix donut, install **ApexCharts Card** via HACS (Frontend → search "apexcharts-card"). Native HA cards can't plot forecast (future-dated) data; ApexCharts can.

---

## Minimum viable stack
- **Anyone in the NEM:** Home Assistant + HACS + **AEMO NEM Pricing** (free) → wholesale prices & demand.
- **+ Amber customers:** add **Amber Electric** → retail price + forecast + feed-in.
- **+ fuel mix:** add a free **OpenElectricity** key + the feed above.

Everything except Amber works for anyone in the NEM, regardless of electricity retailer.
