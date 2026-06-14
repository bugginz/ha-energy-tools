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

# HA API token for foxctl (talks to core via the supervisor proxy)
printf '%s' "${SUPERVISOR_TOKEN}" > /data/.config/sen66/ha_token
echo "[energy_tools] SUPERVISOR_TOKEN length: ${#SUPERVISOR_TOKEN}"

# Build config files from add-on options + baked templates
python3 /build_config.py

echo "[energy_tools] starting nemfuel (background) + foxctl (web on :8770)"
python3 -u /nemfuel.py &
exec python3 -u /foxctl.py serve
