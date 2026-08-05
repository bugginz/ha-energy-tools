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

pixlet render "$DIR/battery.star" \
  "soc=$SOC" "kwh=$KWH" "net_kw=$NET" "health=$HEALTH" \
  "coast=$COAST" "t_now=$TNOW" "t_min=$TMIN" "t_max=$TMAX" \
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
