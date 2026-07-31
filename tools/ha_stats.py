#!/usr/bin/env python3
"""Pull Home Assistant long-term statistics over the websocket API.

The recorder purges raw history after ~10 days, but entities with a `state_class`
keep hourly long-term statistics indefinitely. That is the only way to look at
something weeks back — /api/history will simply return nothing.

    HA_TOKEN=... python3 ha_stats.py <ISO start> <ISO end> <entity_id> [entity_id...]

Prints one JSON object per hour bucket per entity.
"""
import json
import sys

from ha_dashboard import HAWS


def fetch(ha, start, end, ids, period="hour"):
    return ha.cmd(type="recorder/statistics_during_period",
                  start_time=start, end_time=end,
                  statistic_ids=ids, period=period,
                  types=["mean", "max", "min"])


def main(argv):
    if len(argv) < 3:
        raise SystemExit(__doc__)
    start, end, ids = argv[0], argv[1], argv[2:]
    ha = HAWS()
    try:
        res = fetch(ha, start, end, ids)
        for eid, rows in res.items():
            for r in rows:
                print(json.dumps({"entity_id": eid, **r}))
    finally:
        ha.close()


if __name__ == "__main__":
    main(sys.argv[1:])
