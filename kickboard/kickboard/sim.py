"""Simulator / debug web UI (PLAN.md §8.7) and the /health endpoint.

Single HTML page (static/index.html): plan view of the kitchen with every
LED coloured by its live output, drag-to-inject a fake target, sliders
for the tunables, a dropout button, and raw-vs-smoothed trails once the
radar is live. State streams over a websocket at ~15 Hz.
"""

# NOTE: no `from __future__ import annotations` here. FastAPI resolves the
# `WebSocket` annotation on the /ws endpoint at runtime; as a postponed
# string with the name imported lazily it silently became a required
# query parameter and every socket was refused with HTTP 403.

import asyncio
import threading
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

STATIC_DIR = Path(__file__).parent / "static"
STREAM_HZ = 15.0


def create_app(service) -> FastAPI:
    app = FastAPI(title="kickboard sim")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/health")
    def health():
        return JSONResponse({
            "ok": True,
            "uptime_s": round(time.time() - service.started, 1),
            "radar_alive": service.radar_alive(),
            "frames_seen": service.frames_seen,
            "radar_nodes": {n: {k: v for k, v in hb.items() if k != "ts"}
                            | {"age_s": round(time.time() - hb["ts"], 1)}
                            for n, hb in service.radar_nodes.items()},
            "stats": service.renderer.stats(),
        })

    @app.get("/api/geometry")
    def get_geometry():
        return JSONResponse(service.geometry_state())

    @app.post("/api/params")
    async def set_params(body: dict):
        p = service.params
        if "sigma_mm" in body:
            p.sigma_mm = max(50.0, float(body["sigma_mm"]))
        if "peak" in body:
            p.peak = min(1.0, max(0.0, float(body["peak"])))
        if "ambient" in body:
            p.ambient = min(0.2, max(0.0, float(body["ambient"])))
        if "ema_tau_s" in body:
            p.ema_tau_s = min(2.0, max(0.02, float(body["ema_tau_s"])))
        if "cct_k" in body:
            p.set_cct(float(body["cct_k"]))
        if "enabled" in body:
            p.enabled = bool(body["enabled"])
        if "mode" in body and body["mode"] in ("auto", "manual"):
            p.mode = body["mode"]
        return {"ok": True, "params": p.snapshot()}

    @app.post("/api/sim_target")
    async def sim_target(body: dict):
        service.set_sim_target(
            active=bool(body.get("active", False)),
            x=float(body.get("x", 0.0)), y=float(body.get("y", 0.0)),
            noise_mm=(float(body["noise_mm"]) if "noise_mm" in body else None))
        return {"ok": True}

    @app.post("/api/dropout")
    async def dropout(body: dict):
        service.trigger_dropout(float(body.get("seconds", 3.0)))
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        try:
            while True:
                await sock.send_json(service.ui_state())
                await asyncio.sleep(1.0 / STREAM_HZ)
        except (WebSocketDisconnect, RuntimeError):
            pass

    return app


def serve_in_thread(service, host: str, port: int) -> threading.Thread:
    import uvicorn

    app = create_app(service)
    server = uvicorn.Server(uvicorn.Config(
        app, host=host, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True, name="sim-web")
    thread.start()
    return thread
