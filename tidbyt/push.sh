#!/usr/bin/env bash
# Render the house-battery app and push it to the Tidbyt. Run from cron on the Pi.
#
# Reads live values from HA (localhost:8123, long-lived token from the
# energy-tools token file), passes them to battery.star as params, pushes the
# rendered webp as a background installation to the LOCAL tronbyt-server
# (docker, :8000) — the Tidbyt runs Tronbyt firmware since 2026-08-19 and polls
# that server; the Tidbyt cloud is no longer involved. The server API key lives
# ONLY in /opt/stack/tidbyt/tronbyt_key (mode 600) — never in the repo.
set -euo pipefail

# Cron's PATH is /usr/bin:/bin — pixlet lives in /usr/local/bin.
export PATH=/usr/local/bin:$PATH

DIR=/opt/stack/tidbyt
HA=http://localhost:8123
TOKEN=$(cat /opt/stack/energy_tools/data/.config/sen66/ha_token)
source "$DIR/tronbyt_push.sh"

get() {
  curl -sf -m 10 -H "Authorization: Bearer $TOKEN" "$HA/api/states/$1" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])'
}

# Numeric fetch with fallback: a sensor reporting unavailable/unknown must
# degrade the display, not wedge it (2026-08-13: three template sensors died
# and every render crashed on float("unavailable") for five hours).
getn() {
  local v
  v=$(get "$1" 2>/dev/null)
  python3 -c "print(float('$v'))" 2>/dev/null || echo "$2"
}

# Demo tour: armed from the Main dashboard. Disarm FIRST so the next cron tick
# doesn't restart it, then hand over to the scenario script and skip the live push.
DEMO=$(get input_boolean.tidbyt_demo || echo off)
if [ "$DEMO" = "on" ]; then
  curl -sf -m 10 -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    "$HA/api/services/input_boolean/turn_off" \
    -d '{"entity_id": "input_boolean.tidbyt_demo"}' > /dev/null
  exec "$DIR/demo.sh"
fi

# AC flows come from the LOCAL Meross 18ch clamps (seconds cadence), not the
# FoxESS cloud: the cloud takes one instantaneous sample every ~5min, so a
# cycling load (oven thermostat) makes it flip between 0.5 and 3.6kW while the
# real average was 2.4kW — confirmed against the SoC drop rate (2026-08-21).
# The cloud keeps solar (DC, no clamp on it), SoC, kWh, coast and forecast,
# and remains the fallback if the clamps go unavailable.
SOC=$(getn sensor.foxess_foxctl_battery_soc 0)
KWH=$(getn sensor.battery_energy 0)
HOUSE_W=$(getn sensor.circuits_total_power NA)
GRID_W=$(getn sensor.grid_main_power_local NA)
INV_W=$(getn sensor.inverter_ac_power_local NA)
CHG=$(getn sensor.foxess_foxctl_battery_charge_power 0)
DIS=$(getn sensor.foxess_foxctl_battery_discharge_power 0)
SOLAR=$(getn sensor.foxess_foxctl_solar_power 0)
GRIDIN=$(getn sensor.foxess_foxctl_grid_import 0)
HEALTH=$(get sensor.kiosk_battery_soc_health)
COAST=$(getn sensor.battery_coast_margin 0)
TNOW=$(get sensor.living_room_ac_outside || echo '?')
COND=$(get weather.forecast_home || echo '')
LOAD_CLOUD=$(getn sensor.foxess_foxctl_house_load 0)
read -r LOAD GRID NET < <(python3 -c "
def w(v):
    return None if v == 'NA' else float(v) / 1000.0
solar = float('$SOLAR')
house, grid, invac = w('$HOUSE_W'), w('$GRID_W'), w('$INV_W')
if house is None or invac is None:          # clamps down -> cloud fallback
    house = float('$LOAD_CLOUD')
    grid = float('$GRIDIN')
    batt = float('$CHG') - float('$DIS')
else:
    if grid is None:
        grid = house - invac
    batt = solar - invac                    # + charging / - discharging
print(round(house, 2), round(grid, 2), round(batt, 2))
")
# What is carrying the house right now. 'sun' is reserved for the house running
# ENTIRELY on sunshine (solar >= load) — a winter morning trickle of 0.2kW is
# technically the largest of the three but flips the icon and the whole bar
# colour every few minutes as clouds pass, which reads as noise (Rob 2026-08-17).
# Otherwise the gap-filler wins: battery, else grid.
SRC=$(python3 -c "
solar, load, grid, batt = float('$SOLAR'), float('$LOAD'), float('$GRID'), float('$NET')
dis, imp = max(-batt, 0.0), max(grid, 0.0)
if solar > 0.05 and solar >= load:
    print('sun')
elif dis > 0.05 and dis >= imp:
    print('batt')
elif imp > 0.05:
    print('grid')
else:
    print('')")
TNOW=$(python3 -c "print(int(round(float('$TNOW'))))" 2>/dev/null || echo '?')

# Overnight low + tomorrow's high and their forecast CONDITIONS: overnight icon
# from the hourly forecast at ~3am, tomorrow's from the daily forecast.
read -r TMIN TMAX CNIGHT CTMRW < <(python3 - << 'PYEOF'
import json, urllib.request, datetime
tok = open('/opt/stack/energy_tools/data/.config/sen66/ha_token').read().strip()
def fc(kind):
    req = urllib.request.Request(
        'http://localhost:8123/api/services/weather/get_forecasts?return_response',
        data=json.dumps({'entity_id': 'weather.forecast_home', 'type': kind}).encode(),
        method='POST',
        headers={'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'})
    return list(json.load(urllib.request.urlopen(req, timeout=10))['service_response'].values())[0]['forecast']
try:
    daily = fc('daily')
    tmin = int(round(daily[0]['templow']))
    tmax = int(round(daily[1]['temperature']))
    ctmrw = daily[1].get('condition', '')
    cnight = daily[0].get('condition', '')
    for h in fc('hourly'):
        d = datetime.datetime.fromisoformat(h['datetime'].replace('Z', '+00:00')).astimezone()
        if d.hour == 3 and d > datetime.datetime.now().astimezone():
            cnight = h.get('condition', cnight)
            break
    print(tmin, tmax, cnight, ctmrw)
except Exception:
    print('? ? ? ?')
PYEOF
)

# Bin reminder: Friday through Sunday night, unless the "Bins out" button is on.
# Letters = bins due at the coming Monday collection: R waste, Y recycling, G organic.
BINS=$(python3 - << 'PYEOF'
import json, urllib.request, datetime
tok = open('/opt/stack/energy_tools/data/.config/sen66/ha_token').read().strip()
def get(e):
    req = urllib.request.Request('http://localhost:8123/api/states/' + e,
                                 headers={'Authorization': 'Bearer ' + tok})
    return json.load(urllib.request.urlopen(req, timeout=10))
try:
    out = ''
    today = datetime.date.today()
    if today.weekday() >= 4 and get('input_boolean.bins_out')['state'] != 'on':
        horizon = today + datetime.timedelta(days=7 - today.weekday())
        # Organic (green) is every week — no information, not shown (Rob 2026-08-09).
        for ent, letter in [('sensor.waste_collection_schedule_waste', 'R'),
                            ('sensor.waste_collection_schedule_bins', 'Y')]:
            for k in get(ent)['attributes']:
                try:
                    d = datetime.date.fromisoformat(k)
                except ValueError:
                    continue
                if today <= d <= horizon:
                    out += letter
                    break
    print(out)
except Exception:
    print('')
PYEOF
)

# Car SoC: shown only if the WiCAN value CHANGED in the last 12h (stale reads
# hide rather than mislead). Raw tops out ~95.5 when the car manages itself to
# full, so scale raw/95.5 and cap at 100 (Rob 2026-08-09).
CAR=$(python3 - << 'PYEOF'
import json, urllib.request, datetime
tok = open('/opt/stack/energy_tools/data/.config/sen66/ha_token').read().strip()
try:
    req = urllib.request.Request('http://localhost:8123/api/states/sensor.wican_soc_real',
                                 headers={'Authorization': 'Bearer ' + tok})
    s = json.load(urllib.request.urlopen(req, timeout=10))
    changed = datetime.datetime.fromisoformat(s['last_changed'].replace('Z', '+00:00'))
    age_h = (datetime.datetime.now(datetime.timezone.utc) - changed).total_seconds() / 3600
    raw = float(s['state'])
    print(min(100, int(raw / 95.5 * 100 + 0.5)) if age_h <= 12 else '')
except Exception:
    print('')
PYEOF
)

# Car charger (Ogemray 25A smart switch — replaced the Shelly 1PM, 2026-08):
# bolt pulses while current actually flows, steady dim bolt when the switch is
# on but idle. unavailable -> no bolt (hide rather than mislead).
CHSW=$(get switch.ogemray25a_70af09ed9950 2>/dev/null || echo unavailable)
CARCHG=""
if [ "$CHSW" = "on" ]; then
  CHPW=$(getn sensor.ogemray25a_70af09ed9950_power 0)
  CARCHG=$(python3 -c "print('chg' if float('$CHPW') > 100 else 'on')")
fi

pixlet render "$DIR/battery.star" \
  "soc=$SOC" "kwh=$KWH" "net_kw=$NET" "health=$HEALTH" \
  "coast=$COAST" "t_now=$TNOW" "t_min=$TMIN" "t_max=$TMAX" "bar=chevtip" "cond=$COND" \
  "cond_n=$CNIGHT" "cond_t=$CTMRW" "bins=$BINS" "car=$CAR" "src=$SRC" "load=$LOAD" \
  "carchg=$CARCHG" \
  -o /tmp/tidbyt_battery.webp

# Night mode (the server's dim window, set from the HA Tidbyt dashboard):
# while it is active the frame is recoloured red-only (nightshade.py) before
# pushing — red + 1-2% brightness beats any dim full-colour render for glare.
# Shading happens before the compare, so entering/leaving the window pushes.
NIGHT=$(curl -sf -m 5 -H "Authorization: Bearer $(cat "$TRONBYT_KEY_FILE")" \
  "$TRONBYT/v0/devices/$TRONBYT_DEVICE" \
  | python3 -c 'import sys, json; print(1 if json.load(sys.stdin)["nightMode"]["active"] else 0)' \
  || echo 0)
if [ "$NIGHT" = "1" ]; then
  python3 "$DIR/nightshade.py" /tmp/tidbyt_battery.webp /tmp/tidbyt_battery.webp
fi

# Runs every minute, pushes only when the render actually differs from what is
# already on the device — pixlet renders are deterministic, so identical bytes
# mean an identical display.
if cmp -s /tmp/tidbyt_battery.webp "$DIR/last_pushed.webp"; then
  echo "unchanged — no push"
  exit 0
fi

push_webp /tmp/tidbyt_battery.webp housebattery background
cp /tmp/tidbyt_battery.webp "$DIR/last_pushed.webp"
echo "pushed"

# Live preview for the HA Tidbyt dashboard (/local/tidbyt/now.webp): same
# render magnified 8x so the browser doesn't blur the pixels. Best effort.
PREVIEW_DIR=/opt/stack/ha/config/www/tidbyt
mkdir -p "$PREVIEW_DIR" 2>/dev/null && pixlet render "$DIR/battery.star" \
  "soc=$SOC" "kwh=$KWH" "net_kw=$NET" "health=$HEALTH" \
  "coast=$COAST" "t_now=$TNOW" "t_min=$TMIN" "t_max=$TMAX" "bar=chevtip" "cond=$COND" \
  "cond_n=$CNIGHT" "cond_t=$CTMRW" "bins=$BINS" "car=$CAR" "src=$SRC" "load=$LOAD" \
  "carchg=$CARCHG" \
  --magnify 8 -o "$PREVIEW_DIR/now.webp.tmp" 2>/dev/null \
  && { [ "$NIGHT" != "1" ] || python3 "$DIR/nightshade.py" "$PREVIEW_DIR/now.webp.tmp" "$PREVIEW_DIR/now.webp.tmp"; } \
  && mv "$PREVIEW_DIR/now.webp.tmp" "$PREVIEW_DIR/now.webp" || true
