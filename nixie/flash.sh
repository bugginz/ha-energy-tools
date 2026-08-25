#!/bin/bash
# Flash the nixie AVR over WiFi, with retries.
#
# WiFi latency occasionally desyncs the STK500 byte stream (a failed attempt
# dies cleanly at the sync stage before writing anything, so retrying is
# safe). Usage:
#   ./flash.sh [hexfile]
set -u
HEX="${1:-$(dirname "$0")/firmware/nixie_live/build/nixie_live.ino.hex}"
HOST=net:nixie-soc-display.local:6638

for i in 1 2 3 4 5; do
  echo "--- attempt $i"
  if avrdude -c arduino -p m328p -P "$HOST" -U "flash:w:$HEX:i" 2>&1 \
      | grep -vi tiocm | grep -Ei "writing|verified|error"; then
    :
  fi
  if avrdude -c arduino -p m328p -P "$HOST" -U "flash:v:$HEX:i" 2>&1 \
      | grep -qi "verified"; then
    echo "OK: flashed and verified"
    exit 0
  fi
  sleep 8
done
echo "FAILED after 5 attempts" >&2
exit 1
