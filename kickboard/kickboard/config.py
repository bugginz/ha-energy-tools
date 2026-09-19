"""Config loading and the live-tunable parameter set.

Everything tunable lives in config.yaml (see the repo copy for the full
schema). `load_config()` deep-merges the user file over DEFAULTS so the
YAML only needs to state what differs. `Params` holds the subset that can
change at runtime (MQTT numbers, sim sliders) — the renderer and tracker
read it every frame.
"""

from __future__ import annotations

import copy
import math
from typing import Any

DEFAULTS: dict[str, Any] = {
    "service": {
        "render_fps": 30,
        "idle_timeout_s": 30.0,
        "idle_fade_s": 3.0,
        "wake_fade_s": 0.3,
    },
    "radar": {
        "source": "udp",            # udp | serial
        "udp_port": 4049,
        "serial_port": "/dev/ttyUSB0",
        "serial_baud": 256000,
        "heartbeat_timeout_s": 15.0,
        # sensor -> room rigid transform (see PLAN.md §9.2). For an elevated,
        # down-tilted mount (ceiling at one end), mount_height_mm enables the
        # slant-range -> floor projection; 0 disables it (wall mount at torso
        # height needs none). target_height_mm is the assumed torso height.
        # mirror_x: the sensor is mounted flipped (its +x reads room-left);
        # calibrate.py detects this — a rigid fit cannot absorb a reflection.
        "pose": {"theta_deg": -90.0, "tx_mm": 0.0, "ty_mm": 900.0,
                 "mount_height_mm": 0.0, "target_height_mm": 1000.0,
                 "mirror_x": False},
        # Multiple sensors: one entry per radar node, keyed by the sender IP
        # of its UDP frames. Each source's pose merges over the top-level
        # pose above, so an entry only states what differs. Empty list =
        # single sensor, top-level pose applies to every frame.
        # sources:
        #   - {name: radar-a, ip: 192.168.1.119, pose: {theta_deg: -90, ...}}
        "sources": [],
    },
    "room": {
        # room coordinates, mm, origin at a chosen kitchen corner
        "polygon": [[0, 0], [4200, 0], [4200, 1800], [0, 1800]],
        # detections FIRST appearing inside these are ignored (multipath ghosts)
        "exclusion_zones": [],
    },
    "tracker": {
        "ema_tau_s": 0.15,
        "confidence_rise_s": 0.3,
        "confidence_decay_s": 2.0,
        "hold_time_s": 10.0,
        "static_speed_cms": 10.0,
        "assoc_max_mm": 800.0,
        # per-fix trust from range to its own sensor: w = (trust_range/range)^2
        # clipped to [min_weight, 1]. A fix below min_birth_weight can join a
        # track (within assoc_far_mm) but never start one.
        "trust_range_mm": 2000.0,
        "near_range_mm": 1100.0,     # inside this the slant projection is unstable
        "min_weight": 0.1,
        "assoc_far_mm": 2000.0,
        "min_birth_weight": 0.5,
        # a fix with weight below birth_exclusion_weight may not start a new
        # track within birth_exclusion_mm of an existing one (it is most
        # likely the same person seen sloppily by the other sensor)
        "birth_exclusion_mm": 2000.0,
        "birth_exclusion_weight": 0.9,
    },
    "render": {
        "sigma_mm": 500.0,
        "peak": 0.6,
        "ambient": 0.03,
        "gamma": 2.2,
        "warm_rgb": [255, 147, 41],
        "output_ema_s": 0.05,
    },
    "nodes": [],
    "mqtt": {
        "enabled": False,
        "host": "localhost",
        "port": 1883,
        "username": "",
        "password": "",
        "base_topic": "kickboard",
        "discovery_prefix": "homeassistant",
    },
    "sim": {
        "enabled": True,
        "host": "0.0.0.0",
        "port": 8771,
    },
    "logging": {
        "record_radar": True,
        "record_dir": "data/radar-logs",
        "keep_days": 14,
    },
}

NODE_DEFAULTS: dict[str, Any] = {
    "port": 4048,
    "num_leds": 240,
    # byte order ON THE WIRE. The stock strip_node firmware expects RGB
    # (FastLED's COLOR_ORDER handles the WS2812B's GRB at the chip) — only
    # change this for a receiver that shifts raw bytes straight out.
    "color_order": "RGB",
    "budget_ma": 6000,
    "ma_per_led_full": 60.0,
    "quiescent_ma_per_led": 1.0,
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Cfg:
    """Dict wrapper with attribute access, recursively."""

    def __init__(self, d: dict):
        self._d = d

    def __getattr__(self, name: str) -> Any:
        try:
            v = self._d[name]
        except KeyError:
            raise AttributeError(name) from None
        return _wrap(v)

    def __getitem__(self, name: str) -> Any:
        return _wrap(self._d[name])

    def get(self, name: str, default: Any = None) -> Any:
        return _wrap(self._d.get(name, default))

    def raw(self) -> dict:
        return self._d


def _wrap(v: Any) -> Any:
    if isinstance(v, dict):
        return Cfg(v)
    if isinstance(v, list):
        return [_wrap(x) for x in v]
    return v


def _validate(d: dict) -> None:
    if not d["nodes"]:
        raise ValueError("config: at least one node is required")
    for node in d["nodes"]:
        for key in ("name", "host", "waypoints"):
            if key not in node:
                raise ValueError(f"config: node missing '{key}': {node}")
        wps = node["waypoints"]
        idxs = [w[0] for w in wps]
        if idxs != sorted(idxs) or len(set(idxs)) != len(idxs):
            raise ValueError(f"config: node '{node['name']}' waypoint indices "
                             "must be strictly increasing")
        if idxs[0] != 0 or idxs[-1] != node["num_leds"] - 1:
            raise ValueError(f"config: node '{node['name']}' waypoints must "
                             f"start at LED 0 and end at LED {node['num_leds'] - 1}")
        if node["color_order"] and sorted(node["color_order"]) != ["B", "G", "R"]:
            raise ValueError(f"config: node '{node['name']}' bad color_order "
                             f"'{node['color_order']}'")
    if len(d["room"]["polygon"]) < 3:
        raise ValueError("config: room.polygon needs at least 3 vertices")


def load_config(path: str) -> Cfg:
    import yaml

    with open(path) as f:
        user = yaml.safe_load(f) or {}
    d = _merge(DEFAULTS, user)
    d["nodes"] = [_merge(NODE_DEFAULTS, n) for n in d.get("nodes", [])]
    for src in d["radar"].get("sources", []):
        if "ip" not in src:
            raise ValueError(f"config: radar source missing 'ip': {src}")
        src.setdefault("name", src["ip"])
        src["pose"] = _merge(d["radar"]["pose"], src.get("pose", {}))
    _validate(d)
    return Cfg(d)


def cct_to_rgb(kelvin: float) -> tuple[int, int, int]:
    """Colour temperature -> sRGB, Tanner Helland's approximation.

    Good enough for picking a warm white by feel from an HA slider; the
    default (255,147,41) corresponds to roughly 2000-2200 K as rendered
    by WS2812B primaries, so expect to tune by eye either way.
    """
    t = max(1000.0, min(12000.0, kelvin)) / 100.0
    if t <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(t) - 161.1195681661
    else:
        r = 329.698727446 * ((t - 60) ** -0.1332047592)
        g = 288.1221695283 * ((t - 60) ** -0.0755148492)
    if t >= 66:
        b = 255.0
    elif t <= 19:
        b = 0.0
    else:
        b = 138.5177312231 * math.log(t - 10) - 305.0447927307
    clamp = lambda v: int(max(0.0, min(255.0, v)))  # noqa: E731
    return clamp(r), clamp(g), clamp(b)


class Params:
    """Runtime-mutable tunables, shared by renderer / MQTT / sim UI.

    Plain attributes; single-writer-per-field and GIL-atomic reads, so no
    locking. `snapshot()` gives the sim/MQTT a dict for publishing.
    """

    def __init__(self, cfg: Cfg):
        r = cfg.render
        t = cfg.tracker
        self.enabled: bool = True
        self.mode: str = "auto"                 # auto | manual
        self.sigma_mm: float = float(r.sigma_mm)
        self.peak: float = float(r.peak)
        self.ambient: float = float(r.ambient)
        self.gamma: float = float(r.gamma)
        self.warm_rgb: tuple[int, int, int] = tuple(int(v) for v in r.warm_rgb)
        self.cct_k: float = 2700.0
        self.ema_tau_s: float = float(t.ema_tau_s)
        self.manual_rgb: tuple[int, int, int] = tuple(int(v) for v in r.warm_rgb)
        self.manual_brightness: int = 128        # 0-255
        self.manual_on: bool = False

    def set_cct(self, kelvin: float) -> None:
        self.cct_k = float(kelvin)
        self.warm_rgb = cct_to_rgb(kelvin)

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "sigma_mm": self.sigma_mm,
            "peak": self.peak,
            "ambient": self.ambient,
            "gamma": self.gamma,
            "warm_rgb": list(self.warm_rgb),
            "cct_k": self.cct_k,
            "ema_tau_s": self.ema_tau_s,
            "manual_rgb": list(self.manual_rgb),
            "manual_brightness": self.manual_brightness,
            "manual_on": self.manual_on,
        }
