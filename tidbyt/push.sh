#!/usr/bin/env bash
# Render the house-battery app and push it to the Tidbyt. Run from cron on the Pi.
#
# Reads live values from HA (localhost:8123, long-lived token from the
# energy-tools token file), passes them to battery.star as params, pushes the
# rendered webp as a background installation. The Tidbyt API key lives ONLY in
# /opt/stack/tidbyt/tidbyt_key (mode 600) — never in the repo.
set -euo pipefail

# Cron's PATH is /usr/bin:/bin — pixlet lives in /usr/local/bin.
export PATH=/usr/local/bin:$PATH

DIR=/opt/stack/tidbyt
DEVICE=subsequently-infallible-vital-kakapo-497
HA=http://localhost:8123
TOKEN=$(cat /opt/stack/energy_tools/data/.config/sen66/ha_token)

get() {
  curl -sf -m 10 -H "Authorization: Bearer $TOKEN" "$HA/api/states/$1" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])'
}

SOC=$(get sensor.foxess_foxctl_battery_soc)
KWH=$(get sensor.battery_energy)
CHG=$(get sensor.foxess_foxctl_battery_charge_power)
DIS=$(get sensor.foxess_foxctl_battery_discharge_power)
HEALTH=$(get sensor.kiosk_battery_soc_health)
COAST=$(get sensor.battery_coast_margin || echo 0)
TNOW=$(get sensor.living_room_ac_outside || echo '?')
COND=$(get weather.forecast_home || echo '')
NET=$(python3 -c "print(round(float('$CHG') - float('$DIS'), 2))")
TNOW=$(python3 -c "print(int(round(float('$TNOW'))))" 2>/dev/null || echo '?')

# Overnight low (today's templow) + tomorrow's high, from the daily forecast.
read -r TMIN TMAX < <(curl -sf -m 10 -X POST \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  "$HA/api/services/weather/get_forecasts?return_response" \
  -d '{"entity_id": "weather.forecast_home", "type": "daily"}' \
  | python3 -c '
import json, sys
fc = list(json.load(sys.stdin)["service_response"].values())[0]["forecast"]
print(int(round(fc[0]["templow"])), int(round(fc[1]["temperature"])))
' || echo '? ?')

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
  "coast=$COAST" "t_now=$TNOW" "t_min=$TMIN" "t_max=$TMAX" "bar=chevtip" "cond=$COND" "bins=$BINS" "car=$CAR" \
  -o /tmp/tidbyt_battery.webp

# Runs every minute, pushes only when the render actually differs from what is
# already on the device — pixlet renders are deterministic, so identical bytes
# mean an identical display.
if cmp -s /tmp/tidbyt_battery.webp "$DIR/last_pushed.webp"; then
  echo "unchanged — no push"
  exit 0
fi

pixlet push --api-token "$(cat $DIR/tidbyt_key)" \
  --installation-id housebattery --background \
  "$DEVICE" /tmp/tidbyt_battery.webp
cp /tmp/tidbyt_battery.webp "$DIR/last_pushed.webp"
echo "pushed"
