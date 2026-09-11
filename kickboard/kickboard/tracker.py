"""Target smoothing, identity, confidence and gating (PLAN.md §8.2).

The RD-03D's three target slots are not stable across frames, so slot
order means nothing: association is nearest-neighbour against existing
tracks with a distance gate. Confidence ramps up over ~confidence_rise_s
of consistent detection and decays over confidence_decay_s of absence;
the renderer uses it directly as the pool's peak multiplier, which gives
fade-in/fade-out for free and rejects one-frame ghosts.

Static-target hold: a track that vanishes while (near) stationary decays
over hold_time_s instead — the "leaning on the bench reading" case where
the radar loses a motionless person.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field


def point_in_polygon(x: float, y: float, poly) -> bool:
    """Ray-casting point-in-polygon. poly: sequence of (x, y)."""
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i][0], poly[i][1]
        x2, y2 = poly[(i + 1) % n][0], poly[(i + 1) % n][1]
        if (y1 > y) != (y2 > y):
            xt = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xt:
                inside = not inside
    return inside


@dataclass
class Track:
    id: int
    x: float                 # smoothed, room mm
    y: float
    raw_x: float             # last raw detection, for the debug UI
    raw_y: float
    speed_cms: float
    confidence: float
    born_ts: float
    last_seen_ts: float
    held: bool = False       # lost-while-stationary hold
    trail: list = field(default_factory=list)   # (ts, x, y), sim UI only

    def as_target(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.confidence)


class Tracker:
    TRAIL_S = 10.0
    MIN_CONF = 0.02          # below this a track is dropped

    def __init__(self, cfg, params, room_polygon, exclusion_zones=()):
        self.cfg = cfg
        self.params = params  # live ema_tau_s
        self.polygon = [tuple(p) for p in room_polygon]
        self.zones = [[tuple(p) for p in z] for z in exclusion_zones]
        self.tracks: list[Track] = []
        self.last_activity_ts: float = 0.0
        self._last_ts: float | None = None
        self._ids = itertools.count(1)

    def update(self, ts: float, detections) -> None:
        """detections: iterable of (x_mm, y_mm, speed_cms[, weight]) in room
        coords. weight (0..1, default 1) is how much to trust the fix: the
        RD-03D's bearing error grows with range (+-5 deg is +-0.4 m at 4.5 m)
        and the slant projection adds more, so with a sensor at each end of
        the room every fix is precise near its own sensor and sloppy far
        away. Low-weight fixes may nudge an existing track within a wider
        gate but cannot create one — the other sensor is close to it."""
        dt = 0.0 if self._last_ts is None else max(0.0, ts - self._last_ts)
        self._last_ts = ts

        dets = []
        for d in detections:
            x, y, v = d[0], d[1], d[2]
            w = min(1.0, max(0.0, float(d[3]))) if len(d) > 3 else 1.0
            if point_in_polygon(x, y, self.polygon):
                dets.append((x, y, v, w))

        # Greedy nearest-neighbour association, closest pairs first. The
        # gate widens for low-weight fixes (their position error is larger).
        gate = float(self.cfg.assoc_max_mm)
        far_gate = float(self.cfg.get("assoc_far_mm", gate * 2))
        pairs = sorted(
            ((math.dist((t.x, t.y), (d[0], d[1])), ti, di)
             for ti, t in enumerate(self.tracks) for di, d in enumerate(dets)),
            key=lambda p: p[0])
        matched_t: set[int] = set()
        matched_d: set[int] = set()
        for dist, ti, di in pairs:
            w = dets[di][3]
            if dist > gate + (far_gate - gate) * (1.0 - w):
                continue
            if ti in matched_t or di in matched_d:
                continue
            matched_t.add(ti)
            matched_d.add(di)
            self._absorb(self.tracks[ti], ts, dt, *dets[di])

        # Unmatched tracks: decay (or hold if lost while stationary).
        rise = float(self.cfg.confidence_rise_s)
        decay = float(self.cfg.confidence_decay_s)
        hold = float(self.cfg.hold_time_s)
        static_v = float(self.cfg.static_speed_cms)
        survivors = []
        for ti, t in enumerate(self.tracks):
            if ti in matched_t:
                survivors.append(t)
                continue
            if not t.held and abs(t.speed_cms) <= static_v:
                t.held = True
            t.confidence -= dt / (hold if t.held else decay)
            if t.confidence > self.MIN_CONF:
                survivors.append(t)
        self.tracks = survivors

        # Unmatched detections: new tracks, unless born inside a ghost zone
        # or too far from their sensor to be trusted on their own.
        min_birth_w = float(self.cfg.get("min_birth_weight", 0.0))
        excl_mm = float(self.cfg.get("birth_exclusion_mm", 0.0))
        excl_w = float(self.cfg.get("birth_exclusion_weight", 1.0))
        for di, d in enumerate(dets):
            if di in matched_d:
                continue
            x, y, v, w = d
            if w < min_birth_w:
                continue
            if (excl_mm and w < excl_w
                    and any(math.dist((x, y), (t.x, t.y)) < excl_mm for t in self.tracks)):
                continue
            if any(point_in_polygon(x, y, z) for z in self.zones):
                continue
            self.tracks.append(Track(
                id=next(self._ids), x=x, y=y, raw_x=x, raw_y=y,
                speed_cms=v, confidence=min(1.0, dt / rise if dt else 0.1),
                born_ts=ts, last_seen_ts=ts))

        if any(t.last_seen_ts == ts for t in self.tracks):
            self.last_activity_ts = ts

    def _absorb(self, t: Track, ts: float, dt: float,
                x: float, y: float, v: float, w: float = 1.0) -> None:
        tau = max(1e-3, float(self.params.ema_tau_s))
        alpha = 1.0 - math.exp(-dt / tau) if dt > 0 else 1.0
        alpha *= w                      # sloppy fixes pull gently
        t.x += alpha * (x - t.x)
        t.y += alpha * (y - t.y)
        t.raw_x, t.raw_y = x, y
        t.speed_cms = v
        t.confidence = min(1.0, t.confidence + dt / float(self.cfg.confidence_rise_s))
        t.last_seen_ts = ts
        t.held = False
        t.trail.append((ts, t.x, t.y))
        while t.trail and t.trail[0][0] < ts - self.TRAIL_S:
            t.trail.pop(0)

    def targets(self) -> list[tuple[float, float, float]]:
        """(x, y, confidence) per live track, for the renderer."""
        return [t.as_target() for t in self.tracks]

    def occupied(self, ts: float, idle_timeout_s: float) -> bool:
        return bool(self.tracks) or (ts - self.last_activity_ts) < idle_timeout_s
