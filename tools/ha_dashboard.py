#!/usr/bin/env python3
"""Read/write Home Assistant Lovelace dashboards over the websocket API.

Editing `.storage/lovelace.*` by hand does not work on a running HA — the config is held in
memory and rewritten on shutdown, so direct edits are silently discarded (or clobbered).
The websocket API is the supported live path and needs no restart.

Usage (run where HA is reachable; needs a long-lived token):

    HA_TOKEN=$(cat /data/.config/sen66/ha_token) python3 ha_dashboard.py list
    ... python3 ha_dashboard.py get <url_path>            # prints config JSON
    ... python3 ha_dashboard.py save <url_path> <file>    # writes config from a JSON file
    ... python3 ha_dashboard.py create <url_path> <title> [icon]
"""
import json
import os
import sys

import websocket   # websocket-client


class HAWS:
    def __init__(self, url=None, token=None):
        self.url = url or os.environ.get("HA_WS", "ws://localhost:8123/api/websocket")
        self.token = token or os.environ.get("HA_TOKEN", "")
        if not self.token:
            raise SystemExit("HA_TOKEN not set")
        self.ws = websocket.create_connection(self.url, timeout=20)
        hello = json.loads(self.ws.recv())
        if hello.get("type") != "auth_required":
            raise SystemExit(f"unexpected greeting: {hello}")
        self.ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        res = json.loads(self.ws.recv())
        if res.get("type") != "auth_ok":
            raise SystemExit(f"auth failed: {res}")
        self._id = 0

    def cmd(self, **payload):
        self._id += 1
        payload["id"] = self._id
        self.ws.send(json.dumps(payload))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id and msg.get("type") == "result":
                if not msg.get("success"):
                    raise SystemExit(f"command failed: {msg.get('error')}")
                return msg.get("result")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def main(argv):
    if not argv:
        raise SystemExit(__doc__)
    action, rest = argv[0], argv[1:]
    ha = HAWS()
    try:
        if action == "list":
            for d in ha.cmd(type="lovelace/dashboards/list"):
                print(f"{d.get('url_path')}\t{d.get('title')}\t{d.get('mode')}")
        elif action == "get":
            cfg = ha.cmd(type="lovelace/config", url_path=rest[0])
            print(json.dumps(cfg, indent=2))
        elif action == "save":
            cfg = json.load(open(rest[1]))
            ha.cmd(type="lovelace/config/save", url_path=rest[0], config=cfg)
            print(f"saved {rest[1]} -> {rest[0]}")
        elif action == "create":
            url_path, title = rest[0], rest[1]
            icon = rest[2] if len(rest) > 2 else "mdi:tablet-dashboard"
            ha.cmd(type="lovelace/dashboards/create", url_path=url_path, title=title,
                   icon=icon, show_in_sidebar=True, require_admin=False)
            print(f"created dashboard {url_path} ({title})")
        else:
            raise SystemExit(f"unknown action: {action}")
    finally:
        ha.close()


if __name__ == "__main__":
    main(sys.argv[1:])
