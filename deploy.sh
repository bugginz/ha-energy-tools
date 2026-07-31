#!/usr/bin/env bash
# Deploy energy_tools to the Pi.
#
# GitHub is the source of truth but NOT the deploy path: /opt/stack/energy_tools/src on the
# Pi is a plain copied tree, not a git clone, so `git push` alone ships nothing. This script
# does the push and the rsync+rebuild together so the two can never drift apart.
#
#   ./deploy.sh              commit must already exist; pushes, rsyncs, rebuilds, verifies
#   ./deploy.sh --no-push    skip the push (rsync + rebuild only)
#   ./deploy.sh --dry-run    show what would sync, change nothing
set -euo pipefail

HOST="${DEPLOY_HOST:-robwil@homeassistant.local}"
REMOTE_SRC="/opt/stack/energy_tools/src"
STACK="/opt/stack"
CD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$CD"

PUSH=1
DRY=0
for a in "$@"; do
  case "$a" in
    --no-push) PUSH=0 ;;
    --dry-run) DRY=1 ;;
    *) echo "unknown flag: $a" >&2; exit 2 ;;
  esac
done

VERSION="$(sed -n 's/^version: "\(.*\)"/\1/p' energy_tools/config.yaml)"
[ -n "$VERSION" ] || { echo "could not read version from energy_tools/config.yaml" >&2; exit 1; }
CODE_VERSION="$(sed -n 's/^VERSION = "\(.*\)".*/\1/p' energy_tools/foxctl.py)"
if [ "$VERSION" != "$CODE_VERSION" ]; then
  echo "version mismatch: config.yaml=$VERSION foxctl.py=$CODE_VERSION — bump both" >&2
  exit 1
fi
echo "==> deploying v$VERSION to $HOST"

if [ -n "$(git status --porcelain)" ]; then
  echo "working tree is dirty — commit first (deploying uncommitted code makes the Pi" >&2
  echo "and GitHub disagree, which is the exact drift this script exists to prevent):" >&2
  git status --short >&2
  exit 1
fi

if [ "$DRY" = 0 ] && [ "$PUSH" = 1 ]; then
  echo "==> git push"
  git push origin HEAD
fi

RSYNC_FLAGS=(-a --delete --exclude .git --exclude __pycache__ --exclude .superpowers)
if [ "$DRY" = 1 ]; then
  echo "==> rsync (dry run)"
  rsync "${RSYNC_FLAGS[@]}" --dry-run -v ./ "$HOST:$REMOTE_SRC/"
  echo "==> dry run: stopping before build"
  exit 0
fi

echo "==> backing up live options.json"
ssh "$HOST" "cp $STACK/energy_tools/data/options.json \
             $STACK/energy_tools/data/options.json.bak-deploy"

echo "==> rsync -> $REMOTE_SRC"
rsync "${RSYNC_FLAGS[@]}" ./ "$HOST:$REMOTE_SRC/"

echo "==> pinning compose image tag to $VERSION and rebuilding"
ssh "$HOST" "set -e
  sed -i -E 's|image: energy-tools:[0-9]+\.[0-9]+\.[0-9]+|image: energy-tools:$VERSION|' \
      $STACK/docker-compose.yml
  cd $STACK
  docker compose build energy-tools
  docker compose up -d energy-tools"

echo "==> waiting for startup"
sleep 25
ssh "$HOST" "set -e
  echo '--- container ---'
  docker ps --filter name=energy-tools --format '{{.Names}}  {{.Image}}  {{.Status}}'
  echo '--- log ---'
  docker logs --since 2m energy-tools 2>&1 | tail -8"

RUNNING="$(ssh "$HOST" "curl -s --max-time 10 http://localhost:8770/api/state \
           | python3 -c 'import json,sys; print(json.load(sys.stdin).get(\"version\",\"?\"))' 2>/dev/null || echo '?'")"
echo "==> deployed v$VERSION (web reports: $RUNNING)"
echo "    events timeline: ssh $HOST tail -f $STACK/energy_tools/data/foxctl/events.jsonl"
