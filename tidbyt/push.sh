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

SOC=$(getn sensor.foxess_foxctl_battery_soc 0)
KWH=$(getn sensor.battery_energy 0)
CHG=$(getn sensor.foxess_foxctl_battery_charge_power 0)
DIS=$(getn sensor.foxess_foxctl_battery_discharge_power 0)
SOLAR=$(getn sensor.foxess_foxctl_solar_power 0)
GRIDIN=$(getn sensor.foxess_foxctl_grid_import 0)
HEALTH=$(get sensor.kiosk_battery_soc_health)
COAST=$(getn sensor.battery_coast_margin 0)
TNOW=$(get sensor.living_room_ac_outside || echo '?')
COND=$(get weather.forecast_home || echo '')
NET=$(python3 -c "print(round(float('$CHG') - float('$DIS'), 2))")
LOAD=$(getn sensor.foxess_foxctl_house_load 0)
# What is carrying the house right now. 'sun' is reserved for the house running
# ENTIRELY on sunshine (solar >= load) — a winter morning trickle of 0.2kW is
# technically the largest of the three but flips the icon and the whole bar
# colour every few minutes as clouds pass, which reads as noise (Rob 2026-08-17).
# Otherwise the gap-filler wins: battery, else grid.
SRC=$(python3 -c "
solar, dis, grid, load = float('$SOLAR'), float('$DIS'), float('$GRIDIN'), float('$LOAD')
if solar > 0.05 and solar >= load:
    print('sun')
elif dis > 0.05 and dis >= grid:
    print('batt')
elif grid > 0.05:
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

pixlet render "$DIR/battery.star" \
  "soc=$SOC" "kwh=$KWH" "net_kw=$NET" "health=$HEALTH" \
  "coast=$COAST" "t_now=$TNOW" "t_min=$TMIN" "t_max=$TMAX" "bar=chevtip" "cond=$COND" \
  "cond_n=$CNIGHT" "cond_t=$CTMRW" "bins=$BINS" "car=$CAR" "src=$SRC" "load=$LOAD" \
  -o /tmp/tidbyt_battery.webp

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
  --magnify 8 -o "$PREVIEW_DIR/now.webp.tmp" 2>/dev/null \
  && mv "$PREVIEW_DIR/now.webp.tmp" "$PREVIEW_DIR/now.webp" || true
