#!/usr/bin/env bash
# One display tick: sample once, render every face from that sample.
#
# Driven by tronbyt-tick.timer every 30s (it replaced the two independent
# per-minute cron entries). The ordering is the whole point — snapshot.sh runs
# FIRST and writes /dev/shm/tronbyt/snapshot.env, then both pushers source it
# and skip their own refresh because it is seconds old. Same numbers, same
# instant, on all three screens.
#
# The two renders run in parallel: they are independent, the Pi has 4 cores, and
# it drops the cycle from ~4.1s to ~3s so the screens also change together
# rather than 3s apart.
#
# Nothing here is fatal: a failed snapshot leaves the previous one in place (the
# pushers refresh it themselves if it ages past 90s), and a failed render leaves
# that display showing its last good frame.
set -uo pipefail
export PATH=/usr/local/bin:$PATH

/opt/stack/tidbyt/snapshot.sh || echo "snapshot failed — pushers will refresh" >&2

/opt/stack/tronbyt-wide/push_wide.sh > /opt/stack/tronbyt-wide/last.log 2>&1 &
wide=$!
/opt/stack/tidbyt/push.sh > /opt/stack/tidbyt/last.log 2>&1 &
tidbyt=$!

wait $wide  || echo "wide push failed (see tronbyt-wide/last.log)" >&2
wait $tidbyt || echo "tidbyt push failed (see tidbyt/last.log)" >&2
