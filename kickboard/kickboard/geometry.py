"""Strip geometry: LED index -> room (x, y) in mm (PLAN.md §8.3).

Each strip is configured as a polyline of (led_index, x_mm, y_mm)
waypoints — start, corners, end — and LEDs between waypoints are placed
by linear interpolation. Computed once at startup.
"""

from __future__ import annotations

import numpy as np


def build_led_map(waypoints, num_leds: int) -> np.ndarray:
    """waypoints: [(led_index, x_mm, y_mm), ...] covering 0..num_leds-1.

    Returns float array of shape (num_leds, 2).
    """
    wps = [(int(i), float(x), float(y)) for i, x, y in waypoints]
    idx = np.array([w[0] for w in wps])
    if idx[0] != 0 or idx[-1] != num_leds - 1 or np.any(np.diff(idx) <= 0):
        raise ValueError("waypoints must run from LED 0 to num_leds-1, "
                         "strictly increasing")
    leds = np.arange(num_leds)
    xs = np.interp(leds, idx, [w[1] for w in wps])
    ys = np.interp(leds, idx, [w[2] for w in wps])
    return np.stack([xs, ys], axis=1)


def build_maps(cfg) -> dict[str, np.ndarray]:
    return {n.name: build_led_map([(w[0], w[1], w[2]) for w in n.waypoints],
                                  int(n.num_leds))
            for n in cfg.nodes}


def ascii_map(cfg, maps: dict[str, np.ndarray], width: int = 100) -> str:
    """Plan-view scatter of the room polygon and every LED, for --dump-map.

    Sanity-check the geometry before any lights are on: each node's LEDs
    draw with its own symbol (a, b, ...), polygon vertices with '+'.
    """
    poly = np.array(cfg.room.polygon, dtype=float)
    pts = [poly] + list(maps.values())
    allpts = np.concatenate(pts)
    lo = allpts.min(axis=0) - 100
    hi = allpts.max(axis=0) + 100
    span = np.maximum(hi - lo, 1.0)
    height = max(10, int(width * (span[1] / span[0]) * 0.5))  # chars ~2:1

    grid = [[" "] * width for _ in range(height)]

    def plot(x, y, ch):
        cx = int((x - lo[0]) / span[0] * (width - 1))
        cy = int((y - lo[1]) / span[1] * (height - 1))
        grid[height - 1 - cy][cx] = ch  # y up

    for sym, (name, led_xy) in zip("abcdefgh", maps.items()):
        for x, y in led_xy:
            plot(x, y, sym)
        plot(*led_xy[0], sym.upper())   # LED 0 in caps, to check direction
    for x, y in poly:
        plot(x, y, "+")

    lines = ["".join(row) for row in grid]
    legend = ", ".join(f"{sym}={name} ({len(maps[name])} LEDs, LED0={sym.upper()})"
                       for sym, name in zip("abcdefgh", maps.keys()))
    header = (f"room {span[0]:.0f} x {span[1]:.0f} mm "
              f"(origin bottom-left of view, +=polygon vertex)\n{legend}\n")
    return header + "\n".join(lines)
