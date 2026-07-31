#!/usr/bin/env python3
"""Inspect and bulk-rename Home Assistant entities over the websocket API.

The entity registry is not exposed over REST, so renaming in bulk means the
websocket `config/entity_registry/*` commands. Renaming in the UI is one entity
at a time, which is unworkable for a device with 18 channels x several sensors.

    HA_TOKEN=... python3 ha_entities.py list <substring>
    HA_TOKEN=... python3 ha_entities.py plan <plan.json>     # dry run, shows diff
    HA_TOKEN=... python3 ha_entities.py apply <plan.json>    # writes it

A plan is [{"entity_id": ..., "name": ..., "new_entity_id": ...}, ...];
`name` and `new_entity_id` are both optional per row.
"""
import json
import sys

from ha_dashboard import HAWS


def registry(ha):
    return ha.cmd(type="config/entity_registry/list")


def main(argv):
    if not argv:
        raise SystemExit(__doc__)
    action, rest = argv[0], argv[1:]
    ha = HAWS()
    try:
        if action == "list":
            needle = rest[0].lower()
            rows = [e for e in registry(ha)
                    if needle in str(e.get("entity_id", "")).lower()
                    or needle in str(e.get("original_name") or "").lower()]
            for e in sorted(rows, key=lambda r: r["entity_id"]):
                print(json.dumps({
                    "entity_id": e["entity_id"],
                    "name": e.get("name"),
                    "original_name": e.get("original_name"),
                    "platform": e.get("platform"),
                    "device_id": e.get("device_id"),
                    "disabled": bool(e.get("disabled_by")),
                }))
        elif action in ("plan", "apply"):
            plan = json.load(open(rest[0]))
            current = {e["entity_id"]: e for e in registry(ha)}
            for row in plan:
                eid = row["entity_id"]
                cur = current.get(eid)
                if not cur:
                    print(f"MISSING  {eid}")
                    continue
                bits = []
                if "name" in row and row["name"] != cur.get("name"):
                    bits.append(f'name {cur.get("name")!r} -> {row["name"]!r}')
                if row.get("new_entity_id") and row["new_entity_id"] != eid:
                    bits.append(f'id -> {row["new_entity_id"]}')
                if not bits:
                    continue
                print(("APPLY   " if action == "apply" else "WOULD   ") + eid + "  " + "; ".join(bits))
                if action == "apply":
                    payload = {"type": "config/entity_registry/update", "entity_id": eid}
                    if "name" in row:
                        payload["name"] = row["name"]
                    if row.get("new_entity_id"):
                        payload["new_entity_id"] = row["new_entity_id"]
                    ha.cmd(**payload)
        else:
            raise SystemExit(f"unknown action: {action}")
    finally:
        ha.close()


if __name__ == "__main__":
    main(sys.argv[1:])
