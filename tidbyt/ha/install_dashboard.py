"""Create/update the Tidbyt dashboard over HA's WebSocket API.

Run INSIDE the HA container (it has `websockets`):
  docker exec -e HA_TOKEN="$(cat /opt/stack/energy_tools/data/.config/sen66/ha_token)" \
    homeassistant python3 /config/packages/../tidbyt_dash/install_dashboard.py /config/tidbyt_dash/dashboard_tidbyt.json
Idempotent: creates the dashboard entry if missing, then saves the config.
"""
import asyncio, json, os, sys
import websockets

URL_PATH = "dashboard-tidbyt"

async def main(cfg_path):
    cfg = json.load(open(cfg_path))
    async with websockets.connect("ws://localhost:8123/api/websocket", max_size=None) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": os.environ["HA_TOKEN"]}))
        print("auth:", json.loads(await ws.recv())["type"])
        n = 0
        async def call(msg):
            nonlocal n
            n += 1; msg["id"] = n
            await ws.send(json.dumps(msg))
            while True:
                r = json.loads(await ws.recv())
                if r.get("id") == n:
                    return r
        r = await call({"type": "lovelace/dashboards/list"})
        if not any(d["url_path"] == URL_PATH for d in r["result"]):
            r = await call({"type": "lovelace/dashboards/create", "url_path": URL_PATH,
                            "title": "Tidbyt", "icon": "mdi:television-ambient-light",
                            "show_in_sidebar": True, "require_admin": False, "mode": "storage"})
            print("create:", r.get("success"), r.get("error"))
        else:
            print("dashboard exists")
        r = await call({"type": "lovelace/config/save", "url_path": URL_PATH, "config": cfg})
        print("save:", r.get("success"), r.get("error"))

asyncio.run(main(sys.argv[1]))
