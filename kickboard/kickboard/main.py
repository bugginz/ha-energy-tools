"""Service entry point: wires radar -> tracker -> renderer -> DDP.

    python -m kickboard.main --config config.yaml
    python -m kickboard.main --config config.yaml --dump-map
    python -m kickboard.main --config config.yaml --replay data/radar-logs/radar-2026-09-10.jsonl
    python -m kickboard.main --config config.yaml --no-ddp   # sim UI only

The render loop runs at a fixed rate off a monotonic clock and samples
the latest tracker state — render rate and the radar's 10 Hz are
deliberately decoupled (PLAN.md §8.5).
"""

from __future__ import annotations

import argparse
import logging
import random
import threading
import time

from . import config as config_mod
from . import geometry, radar
from .ddp import DDPSender
from .renderer import Renderer
from .tracker import Tracker

log = logging.getLogger("kickboard")


class Service:
    def __init__(self, cfg, no_ddp: bool = False):
        self.cfg = cfg
        self.no_ddp = no_ddp
        self.params = config_mod.Params(cfg)
        self.led_maps = geometry.build_maps(cfg)
        self.tracker = Tracker(cfg.tracker, self.params,
                               cfg.room.polygon, cfg.room.exclusion_zones)
        self.renderer = Renderer(cfg, self.params, self.led_maps)
        self.ddp = DDPSender()
        self.pose = radar.Pose.from_cfg(cfg.radar.pose)
        # elevated-mount slant correction; 0 for a wall mount (see radar.py)
        self.slant_h = (float(cfg.radar.pose.get("mount_height_mm", 0.0))
                        - float(cfg.radar.pose.get("target_height_mm", 1000.0)))
        self.started = time.time()

        self.recorder = None
        if cfg.logging.record_radar:
            self.recorder = radar.Recorder(str(cfg.logging.record_dir),
                                           int(cfg.logging.keep_days))

        self.ha = None
        if cfg.mqtt.enabled:
            from .ha import HaMqtt
            self.ha = HaMqtt(cfg, self.params)

        # sim target injection (debug UI, PLAN.md §8.7)
        self.sim_active = False
        self.sim_xy = (0.0, 0.0)
        self.sim_noise_mm = 0.0
        self.dropout_until = 0.0
        self._next_sim_inject = 0.0

        # radar liveness
        self.last_frame_ts = 0.0
        self.last_heartbeat_ts = 0.0
        self.frames_seen = 0
        self.radar_nodes: dict[str, dict] = {}   # last heartbeat per node name

        self._stop = threading.Event()
        self._source = None

    # -- radar input -------------------------------------------------------

    def on_raw_frame(self, ts: float, raw: bytes) -> None:
        if self.recorder:
            self.recorder.record(ts, raw)
        frame = radar.parse_frame(raw, ts)
        if frame is None:
            return
        self.last_frame_ts = ts
        self.frames_seen += 1
        if self.sim_active or time.time() < self.dropout_until:
            return                      # sim target replaces the radar
        dets = [(*self.pose.to_room(
                    *radar.slant_to_floor(t.x_mm, t.y_mm, self.slant_h)),
                 t.speed_cms)
                for t in frame.targets]
        # the tracker runs on the monotonic clock, same as the render loop
        self.tracker.update(time.monotonic(), dets)

    def on_heartbeat(self, ts: float, payload: dict) -> None:
        self.last_heartbeat_ts = ts
        node = str(payload.get("node", "?"))
        self.radar_nodes[node] = payload | {"ts": ts}
        log.debug("radar heartbeat %s", payload)

    def radar_alive(self) -> bool:
        timeout = float(self.cfg.radar.heartbeat_timeout_s)
        now = time.time()
        return (now - self.last_frame_ts < timeout
                or now - self.last_heartbeat_ts < timeout)

    def start_radar(self) -> None:
        rc = self.cfg.radar
        if str(rc.source) == "serial":
            self._source = radar.SerialRadarSource(
                str(rc.serial_port), int(rc.serial_baud), self.on_raw_frame)
        else:
            self._source = radar.UdpRadarSource(
                int(rc.udp_port), self.on_raw_frame, self.on_heartbeat)
        self._source.start()

    # -- sim controls (called from the web UI) -----------------------------

    def set_sim_target(self, active: bool, x: float = 0.0, y: float = 0.0,
                       noise_mm: float | None = None) -> None:
        self.sim_active = active
        self.sim_xy = (x, y)
        if noise_mm is not None:
            self.sim_noise_mm = max(0.0, noise_mm)

    def trigger_dropout(self, seconds: float) -> None:
        """Suppress all detections for N s — tests the fade-to-ambient path."""
        self.dropout_until = time.time() + max(0.0, seconds)

    def _inject_sim(self, now: float) -> None:
        if not self.sim_active or now < self._next_sim_inject:
            return
        self._next_sim_inject = now + 0.1      # the radar's native 10 Hz
        if time.time() < self.dropout_until:
            self.tracker.update(now, [])
            return
        x, y = self.sim_xy
        n = self.sim_noise_mm
        if n > 0:
            x += random.gauss(0, n)
            y += random.gauss(0, n)
        self.tracker.update(now, [(x, y, 0.0)])

    # -- render loop -------------------------------------------------------

    def run(self) -> None:
        if self.ha:
            self.ha.start()
        fps = float(self.cfg.service.render_fps)
        period = 1.0 / fps
        next_t = time.monotonic()
        last_t = next_t
        last_ha = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now < next_t:
                time.sleep(min(period, next_t - now))
                continue
            next_t += period
            if now - next_t > 1.0:      # fell far behind; don't burst
                next_t = now + period
            dt = min(0.25, now - last_t)
            last_t = now

            self._inject_sim(now)
            targets = self.tracker.targets()
            occupied = self.tracker.occupied(
                now, float(self.cfg.service.idle_timeout_s))
            frames = self.renderer.render(dt, targets, occupied)
            if not self.no_ddp:
                for node in self.cfg.nodes:
                    self.ddp.send_frame(str(node.host), int(node.port),
                                        frames[node.name])
            if self.ha and time.time() - last_ha > 1.0:
                last_ha = time.time()
                est = sum(s["est_ma"] for s in self.renderer.stats().values())
                self.ha.publish_state(occupied, len(targets), est)

    def stop(self) -> None:
        self._stop.set()
        if self._source:
            self._source.stop()
        if self.ha:
            self.ha.stop()
        if self.recorder:
            self.recorder.close()
        self.ddp.close()

    # -- state for the sim UI ---------------------------------------------

    def ui_state(self) -> dict:
        now = time.monotonic()
        return {
            "params": self.params.snapshot(),
            "master": round(self.renderer.master, 3),
            "occupancy": self.tracker.occupied(
                now, float(self.cfg.service.idle_timeout_s)),
            "radar_alive": self.radar_alive(),
            "sim": {"active": self.sim_active, "x": self.sim_xy[0],
                    "y": self.sim_xy[1], "noise_mm": self.sim_noise_mm,
                    "dropout_s": max(0.0, self.dropout_until - time.time())},
            "targets": [{
                "id": t.id, "x": round(t.x), "y": round(t.y),
                "raw_x": round(t.raw_x), "raw_y": round(t.raw_y),
                "conf": round(t.confidence, 3), "held": t.held,
                "trail": [[round(x), round(y)] for _, x, y in t.trail[-100:]],
            } for t in self.tracker.tracks],
            "nodes": {name: {
                "rgb": ns.last_rgb.flatten().tolist(),
                "est_ma": round(ns.est_ma), "clamped": ns.clamped,
            } for name, ns in self.renderer.nodes.items()},
            "stats": self.renderer.stats(),
        }

    def geometry_state(self) -> dict:
        import math
        return {
            "room": [list(p) for p in self.cfg.room.polygon],
            # sensor position and boresight in room coords, for the plan view.
            # RD-03D field of view is about +-60 deg azimuth, ~8 m range.
            "radar": {"x": self.pose.tx_mm, "y": self.pose.ty_mm,
                      "theta_deg": math.degrees(self.pose.theta_rad),
                      "fov_deg": 120, "range_mm": 8000},
            "zones": [[list(p) for p in z] for z in self.cfg.room.exclusion_zones],
            "nodes": {name: xy.tolist() for name, xy in self.led_maps.items()},
        }


def replay(service: Service, path: str, speed: float = 1.0) -> None:
    """Feed a recorded radar log through the pipeline (PLAN.md §8.9)."""
    log.info("replaying %s at %gx", path, speed)
    prev_ts = None
    for ts, raw in radar.replay_frames(path):
        if service._stop.is_set():
            break
        if prev_ts is not None:
            time.sleep(max(0.0, (ts - prev_ts) / speed))
        prev_ts = ts
        service.on_raw_frame(time.time(), raw)
    log.info("replay finished")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="kickboard lighting service")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dump-map", action="store_true",
                    help="print the LED map as an ASCII plan view and exit")
    ap.add_argument("--replay", metavar="FILE",
                    help="feed a recorded radar log instead of live input")
    ap.add_argument("--replay-speed", type=float, default=1.0)
    ap.add_argument("--no-ddp", action="store_true",
                    help="don't send to strips (sim UI only)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    cfg = config_mod.load_config(args.config)

    if args.dump_map:
        maps = geometry.build_maps(cfg)
        print(geometry.ascii_map(cfg, maps))
        return 0

    service = Service(cfg, no_ddp=args.no_ddp)

    if args.replay:
        threading.Thread(target=replay, args=(service, args.replay,
                                              args.replay_speed),
                         daemon=True, name="replay").start()
    else:
        service.start_radar()

    if cfg.sim.enabled:
        from .sim import serve_in_thread
        serve_in_thread(service, str(cfg.sim.host), int(cfg.sim.port))
        log.info("sim UI on http://%s:%s/", cfg.sim.host, cfg.sim.port)

    try:
        service.run()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
