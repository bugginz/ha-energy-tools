#!/usr/bin/env bash
# Deploy the Grafana provisioning + dashboards to the Pi and restart grafana.
#
#   ./grafana/deploy.sh            regenerate JSON, rsync, restart, show provisioning log
#   ./grafana/deploy.sh --dry-run  show what would sync
#
# Grafana runs from /opt/stack/docker-compose.yml with these mounts (added 2026-09-29):
#   /opt/stack/grafana/provisioning -> /etc/grafana/provisioning
#   /opt/stack/grafana/dashboards   -> /var/lib/grafana/dashboards (ro)
# Provisioned dashboards are read-only in the UI on purpose: edit build_dashboards.py.
set -euo pipefail
HOST="${DEPLOY_HOST:-robwil@homeassistant.local}"
CD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$CD"
python3 build_dashboards.py
FLAGS=(-a --delete --exclude __pycache__)
[ "${1:-}" = "--dry-run" ] && FLAGS+=(--dry-run -v)
rsync "${FLAGS[@]}" provisioning/ "$HOST:/opt/stack/grafana/provisioning/"
rsync "${FLAGS[@]}" dashboards/ "$HOST:/opt/stack/grafana/dashboards/"
[ "${1:-}" = "--dry-run" ] && exit 0
ssh "$HOST" "cd /opt/stack && docker compose up -d grafana >/dev/null 2>&1; sleep 8;
  docker logs --since 1m grafana 2>&1 | grep -iE 'provision|error' | tail -12"
echo "==> http://homeassistant.local:3001/dashboards"
