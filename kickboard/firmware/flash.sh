#!/bin/bash
# Compile + flash a kickboard XIAO ESP32-C6 node over USB (Mac or Linux).
#
#   ./flash.sh strip [kick-left|kick-right] [port]
#   ./flash.sh radar [port]
#
# "port" is a USB device (/dev/cu.usbmodem*) for the first flash, or the
# node's IP for an over-the-air update afterwards (ArduinoOTA via the
# core's espota.py, so the node never leaves the toe kick):
#   ./flash.sh strip kick-left 192.168.1.118
#
# First run installs the esp32 arduino core (3.x — the C6 needs 3.x) and
# FastLED via arduino-cli (brew install arduino-cli / see arduino.cc).
# Copy wifi_credentials.h.example to wifi_credentials.h in the sketch dir
# and fill it in first; the script refuses to build without it.
#
# Overrides via environment, applied to a temp copy of the sketch so the
# committed defaults stay untouched:
#   NUM_LEDS=60  MAX_MA=450  ./flash.sh strip kick-left     # bench strip
#   PI_HOST=192.168.1.10     ./flash.sh radar
#   FQBN=esp32:esp32:XIAO_ESP32S3 PI_HOST=... ./flash.sh radar   # XIAO S3 Sense host
#   LOG_HOST=192.168.1.10    ./flash.sh strip kick-left 192.168.1.118   # UDP log sink
#
# BENCH NOTE: powering a strip segment from the XIAO's USB 5V alone, set
# MAX_MA=450 or the Mac's USB port will brown out — the committed default
# (6000) assumes the real PSU. If upload fails to sync, hold the BOOT
# button while plugging the board in and rerun with the new port.
set -euo pipefail
cd "$(dirname "$0")"

FQBN="${FQBN:-esp32:esp32:XIAO_ESP32C6}"
KIND="${1:-}"
case "$KIND" in
  strip) SKETCH=strip_node; NODE="${2:-kick-left}"; PORT="${3:-}" ;;
  radar) SKETCH=radar_node; NODE=kick-radar;        PORT="${2:-}" ;;
  *) sed -n '2,19p' "$0"; exit 2 ;;
esac
case "$NODE" in kick-left|kick-right|kick-radar) ;; *)
  echo "bad node name '$NODE' (kick-left | kick-right)" >&2; exit 2 ;;
esac

command -v arduino-cli >/dev/null || {
  echo "arduino-cli not found — brew install arduino-cli" >&2; exit 1; }
[ -f "$SKETCH/wifi_credentials.h" ] || {
  echo "missing $SKETCH/wifi_credentials.h — copy the .example and fill it in" >&2
  exit 1; }

# core + library, first run only
if ! arduino-cli core list 2>/dev/null | grep -q '^esp32:esp32'; then
  echo "--- installing esp32 core (one-off, a few minutes)"
  arduino-cli core update-index
  arduino-cli core install esp32:esp32
fi
CORE_VER=$(arduino-cli core list | awk '/^esp32:esp32/ {print $2}')
case "$CORE_VER" in
  3.*) ;;
  *) echo "esp32 core $CORE_VER installed but the C6 needs 3.x:" >&2
     echo "  arduino-cli core upgrade esp32:esp32" >&2; exit 1 ;;
esac
if [ "$SKETCH" = strip_node ] && \
   ! arduino-cli lib list 2>/dev/null | grep -q '^FastLED'; then
  echo "--- installing FastLED"
  arduino-cli lib install FastLED
fi

# port autodetect: C6 native USB shows as usbmodem (mac) / ttyACM (linux)
if [ -z "$PORT" ]; then
  PORTS=$(ls /dev/cu.usbmodem* /dev/ttyACM* 2>/dev/null || true)
  COUNT=$(echo "$PORTS" | grep -c . || true)
  if [ "$COUNT" -eq 0 ]; then
    echo "no board found (looked for /dev/cu.usbmodem*, /dev/ttyACM*)" >&2
    exit 1
  elif [ "$COUNT" -gt 1 ]; then
    echo "several candidates — pass one explicitly:" >&2
    echo "$PORTS" >&2; exit 1
  fi
  PORT=$PORTS
fi

# per-node / bench overrides on a temp copy, committed defaults untouched
BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD"' EXIT
cp -r "$SKETCH" "$BUILD/"
DIR="$BUILD/$SKETCH"
override() {  # override NAME value  -> rewrites '#define NAME ...'
  sed -i.bak "s|^#define $1 .*|#define $1 $2|" "$DIR/$SKETCH.ino"
  rm -f "$DIR/$SKETCH.ino.bak"
}
override NODE_NAME "\"$NODE\""
[ -n "${NUM_LEDS:-}" ] && override NUM_LEDS "$NUM_LEDS"
[ -n "${MAX_MA:-}" ]   && override MAX_MILLIAMPS "$MAX_MA"
[ -n "${PI_HOST:-}" ]  && override PI_HOST "\"$PI_HOST\""
[ -n "${LOG_HOST:-}" ] && override LOG_HOST "\"$LOG_HOST\""
grep -E '^#define (NODE_NAME|NUM_LEDS|MAX_MILLIAMPS|PI_HOST|LOG_HOST|DATA_PIN)' \
  "$DIR/$SKETCH.ino" | sed 's/^/    /'

OUT="$BUILD/out"
echo "--- compiling $SKETCH for $FQBN"
arduino-cli compile --fqbn "$FQBN" --build-path "$OUT" "$DIR"
case "$PORT" in
  /dev/*)
    echo "--- uploading to $PORT (serial)"
    arduino-cli upload --fqbn "$FQBN" -p "$PORT" --input-dir "$OUT" "$DIR"
    echo "OK: $NODE flashed. Watch it boot with:"
    echo "  arduino-cli monitor -p $PORT -c baudrate=115200"
    ;;
  *)
    # ArduinoOTA. Call espota.py directly rather than 'arduino-cli upload
    # --protocol network', which only works if mDNS discovery happened to
    # list the node in the last few seconds.
    ESPOTA=""
    for f in "$HOME"/Library/Arduino15/packages/esp32/hardware/esp32/*/tools/espota.py \
             "$HOME"/.arduino15/packages/esp32/hardware/esp32/*/tools/espota.py; do
      [ -f "$f" ] && ESPOTA=$f
    done
    [ -n "$ESPOTA" ] || { echo "espota.py not found in the esp32 core" >&2; exit 1; }
    echo "--- uploading to $PORT (OTA)"
    python3 "$ESPOTA" -r -i "$PORT" -p 3232 --auth="${OTA_PASS:-}" \
      -f "$OUT/$SKETCH.ino.bin" 2>&1 | tr '\r' '\n' \
      | { grep -vE "^Uploading: \[.*\] +[0-9]?[0-9]% *$" || true; }
    echo "OK: $NODE flashed over the air."
    ;;
esac
