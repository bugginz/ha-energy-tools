#!/usr/bin/env sh
set -e

# Load the s6 container environment so SUPERVISOR_TOKEN (and friends) are available.
if [ -d /run/s6/container_environment ]; then
  for f in /run/s6/container_environment/*; do
    [ -f "$f" ] && export "$(basename "$f")"="$(cat "$f")"
  done
fi

export HOME=/data
mkdir -p /data/.config/foxctl /data/.config/nemfuel /data/.config/sen66

# HA API token for foxctl. Under the Supervisor this is the proxy token;
# standalone (docker compose) pass HA_TOKEN (a long-lived token) instead,
# or pre-seed /data/.config/sen66/ha_token and set neither.
if [ -n "${HA_TOKEN:-}" ]; then
  printf '%s' "${HA_TOKEN}" > /data/.config/sen66/ha_token
elif [ -n "${SUPERVISOR_TOKEN:-}" ]; then
  printf '%s' "${SUPERVISOR_TOKEN}" > /data/.config/sen66/ha_token
fi
if [ ! -s /data/.config/sen66/ha_token ]; then
  echo "[energy_tools] WARNING: no HA token (HA_TOKEN/SUPERVISOR_TOKEN unset, ha_token missing)"
fi

# Build config files from add-on options + baked templates
python3 /build_config.py

echo "[energy_tools] starting nemfuel (background) + foxctl (web on :8770)"
python3 -u /nemfuel.py &
exec python3 -u /foxctl.py serve
