#!/bin/sh
# pingstat.sh HOST|gw — one burst of ICMP probes, summarised as a single JSON
# line for the HA command_line sensors in ha-package-network.yaml.
# Deployed at /opt/stack/ha/config/shell/pingstat.sh (/config/shell inside the
# HA container); this repo copy is the source of truth.
#
#   {"target":"8.8.8.8","sent":10,"recv":10,"loss":0.0,
#    "rtt":12.3,"min":11.0,"max":15.2,"jitter":0.8,"error":null}
#
# rtt/min/max/jitter are ms over the replies; jitter is the mean absolute
# difference between consecutive replies (RFC 3550's idea, unsmoothed). They
# are null when nothing came back. "gw" pings this host's default gateway,
# read from /proc/net/route, so no LAN address lives in the repo.
#
# The parse is by line shape, not by implementation, because the HA image
# ships busybox ping and a host shell has iputils: per-reply lines contain
# "time=12.3 ms" in both, the summary "N packets transmitted" in both, and
# only iputils prints mdev, so jitter is computed here instead.
#
# "Network is unreachable" (no route — the host's uplink is gone) counts as
# 100% loss. Anything else that stops ping before it sends (binary missing,
# no raw-socket permission) is reported in "error" with loss null, so a broken
# probe reads as unavailable in HA rather than as an internet outage.

target=${1:?usage: pingstat.sh HOST|gw}
count=${PINGSTAT_COUNT:-10}
route=${PINGSTAT_ROUTE:-/proc/net/route}
ping=${PINGSTAT_PING:-ping}   # tests point this at a stub

if [ "$target" = gw ]; then
  target=$(awk '
    function hex(s,  i, n) {
      n = 0
      for (i = 1; i <= length(s); i++)
        n = n * 16 + index("0123456789ABCDEF", toupper(substr(s, i, 1))) - 1
      return n
    }
    # default route: destination 0, gateway set; the address is little-endian
    $2 == "00000000" && $3 != "00000000" {
      g = $3
      printf "%d.%d.%d.%d", hex(substr(g, 7, 2)), hex(substr(g, 5, 2)),
                            hex(substr(g, 3, 2)), hex(substr(g, 1, 2))
      exit
    }' "$route" 2>/dev/null)
  if [ -z "$target" ]; then
    printf '{"target":"gw","sent":null,"recv":null,"loss":null,"rtt":null,"min":null,"max":null,"jitter":null,"error":"no default route"}\n'
    exit 0
  fi
fi

"$ping" -c "$count" -w $((count + 2)) "$target" 2>&1 | awk -v target="$target" -v count="$count" '
  /DUP!/ { next }
  /time[=<][0-9.]+/ {
    match($0, /time[=<][0-9.]+/)
    t = substr($0, RSTART + 5, RLENGTH - 5) + 0
    n++; sum += t
    if (n == 1 || t < mn) mn = t
    if (n == 1 || t > mx) mx = t
    if (n > 1) { d = t - prev; if (d < 0) d = -d; jsum += d }
    prev = t
    next
  }
  /packets transmitted/ { sent = $1 + 0; stats = 1; next }
  /[Uu]nreachable/ { unreach = 1 }
  NF && first == "" { first = $0 }
  END {
    if (!stats && unreach) { sent = count; stats = 1 }
    if (!stats) {
      gsub(/["\\]/, "", first)
      printf "{\"target\":\"%s\",\"sent\":null,\"recv\":null,\"loss\":null,\"rtt\":null,\"min\":null,\"max\":null,\"jitter\":null,\"error\":\"%s\"}\n", target, (first == "" ? "no output from ping" : first)
      exit
    }
    if (sent < n) sent = n
    loss = sent ? 100 * (sent - n) / sent : 100
    if (n) {
      printf "{\"target\":\"%s\",\"sent\":%d,\"recv\":%d,\"loss\":%.1f,\"rtt\":%.1f,\"min\":%.1f,\"max\":%.1f,\"jitter\":%s,\"error\":null}\n", target, sent, n, loss, sum / n, mn, mx, (n > 1 ? sprintf("%.1f", jsum / (n - 1)) : "null")
    } else {
      printf "{\"target\":\"%s\",\"sent\":%d,\"recv\":0,\"loss\":100.0,\"rtt\":null,\"min\":null,\"max\":null,\"jitter\":null,\"error\":null}\n", target, sent
    }
  }'
