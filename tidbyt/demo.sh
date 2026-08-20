#!/usr/bin/env bash
# Tidbyt demo tour: renders scripted scenarios and pushes each as a momentary
# (foreground) frame, ~8s apiece. Triggered by input_boolean.tidbyt_demo (the
# "Tidbyt demo" button on Main) via push.sh; the flag is disarmed before this
# runs so the tour fires exactly once. Normal live pushes resume automatically
# on the next cron tick after the tour ends.
set -euo pipefail
export PATH=/usr/local/bin:$PATH

DIR=/opt/stack/tidbyt
source "$DIR/tronbyt_push.sh"

# soc kwh net health coast t_now t_min t_max cond cond_n cond_t bins car src [load] [carchg]
SCENARIOS=(
  "55 22.8 9.5 green 40 22 12 24 sunny clear-night sunny '' 45 sun 0 chg"  # free-window: sun blazing, battery+car gulping
  "68 28.2 -1.2 green 30 11 8 13 rainy rainy rainy '' '' batt"           # rainy holding pattern, battery carries the house
  "85 35.2 -3.4 green 24 15 9 17 clear-night clear-night sunny '' 100 batt 0 on"  # evening peak, car full, charger armed
  "72 29.8 -0.8 green 28 13 10 16 partlycloudy clear-night partlycloudy RY '' batt"  # bin night: both bins due
  "100 41.4 0.0 green 33 18 11 21 sunny sunny sunny '' 100 sun 1.4"     # full battery floating on solar; house draw shows
  "64 26.5 -2.4 green 21 6 3 15 clear-night fog sunny '' 32 batt 0 chg"  # pre-dawn dump into a low car
  "40 16.6 -1.6 orange 3 9 7 12 cloudy rainy cloudy '' '' batt"          # coast getting tight
  "22 9.1 -2.2 red -5 8 6 11 lightning lightning-rainy rainy '' 18 grid" # rough night: storm, grid, red everywhere
)

for s in "${SCENARIOS[@]}"; do
  eval "set -- $s"
  pixlet render "$DIR/battery.star" \
    "soc=$1" "kwh=$2" "net_kw=$3" "health=$4" "coast=$5" \
    "t_now=$6" "t_min=$7" "t_max=$8" "bar=chevtip" \
    "cond=$9" "cond_n=${10}" "cond_t=${11}" "bins=${12}" "car=${13}" "src=${14}" "load=${15:-0}" \
    "carchg=${16:-}" \
    -o /tmp/tidbyt_demo_frame.webp
  push_webp /tmp/tidbyt_demo_frame.webp "" foreground
  sleep 8
done
echo "demo tour complete"
