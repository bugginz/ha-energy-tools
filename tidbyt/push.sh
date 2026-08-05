#!/usr/bin/env bash
# Render the house-battery app and push it to the Tidbyt. Run from cron on the Pi.
#
# Reads live values from HA (localhost:8123, long-lived token from the
# energy-tools token file), passes them to battery.star as params, pushes the
# rendered webp as a background installation. The Tidbyt API key lives ONLY in
# /opt/stack/tidbyt/tidbyt_key (mode 600) — never in the repo.
set -euo pipefail

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
NET=$(python3 -c "print(round(float('$CHG') - float('$DIS'), 2))")

pixlet render "$DIR/battery.star" \
  "soc=$SOC" "kwh=$KWH" "net_kw=$NET" "health=$HEALTH" \
  -o /tmp/tidbyt_battery.webp

pixlet push --api-token "$(cat $DIR/tidbyt_key)" \
  --installation-id housebattery --background \
  "$DEVICE" /tmp/tidbyt_battery.webp
