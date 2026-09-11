"""Pools -> RGB frames, with gamma, current budget and output smoothing.

Per node (PLAN.md §8.4):
    d^2 = (led_x - px)^2 + (led_y - py)^2         vectorised over LEDs
    b   = ambient + sum_targets conf * peak * exp(-d^2 / 2 sigma^2)
clipped to 1, scaled by the master envelope (idle fade), gamma applied
after summing, multiplied by the warm-white vector, smoothed with a short
EMA on the *output* frame, then clamped to the per-node current budget
the same way WLED's ABL does — scale the whole frame, never per-pixel.
"""

from __future__ import annotations

import logging
import time

import numpy as np

log = logging.getLogger("kickboard.render")

_ORDER_IDX = {"RGB": (0, 1, 2), "GRB": (1, 0, 2), "BRG": (2, 0, 1),
              "RBG": (0, 2, 1), "GBR": (1, 2, 0), "BGR": (2, 1, 0)}


class NodeState:
    def __init__(self, node_cfg, led_xy: np.ndarray):
        self.cfg = node_cfg
        self.led_xy = led_xy                       # (n, 2) mm
        self.n = led_xy.shape[0]
        self.frame = np.zeros((self.n, 3))         # smoothed output, 0..255 float
        self.last_rgb = np.zeros((self.n, 3), dtype=np.uint8)
        self.order = _ORDER_IDX[str(node_cfg.color_order).upper()]
        self.est_ma = 0.0
        self.clamped = False
        self._last_clamp_log = 0.0


class Renderer:
    def __init__(self, cfg, params, led_maps: dict[str, np.ndarray]):
        self.cfg = cfg
        self.params = params
        self.nodes = {name: NodeState(node, led_maps[name])
                      for node, name in ((n, n.name) for n in cfg.nodes)}
        self.master = 0.0        # idle-fade envelope, 0..1

    def render(self, dt: float, targets, occupied: bool) -> dict[str, bytes]:
        """targets: [(x_mm, y_mm, confidence)]. Returns wire bytes per node."""
        p = self.params
        self._step_master(dt, occupied and p.enabled)

        out = {}
        for name, ns in self.nodes.items():
            if not p.enabled:
                target = np.zeros((ns.n, 3))
            elif p.mode == "manual":
                level = (p.manual_brightness / 255.0) if p.manual_on else 0.0
                target = np.tile(np.array(p.manual_rgb, dtype=float) * level,
                                 (ns.n, 1))
            else:
                target = self._auto_frame(ns, targets)

            # Output-frame EMA: suppresses residual per-LED flicker from
            # quantisation on top of the tracker's input smoothing.
            tau = max(1e-3, float(self.cfg.render.output_ema_s))
            alpha = 1.0 - np.exp(-dt / tau) if dt > 0 else 1.0
            ns.frame += alpha * (target - ns.frame)

            frame = self._apply_budget(ns, ns.frame)
            quant = np.clip(np.rint(frame), 0, 255).astype(np.uint8)
            ns.last_rgb = quant                    # RGB order, for the sim UI
            out[name] = quant[:, ns.order].tobytes()
        return out

    def _auto_frame(self, ns: NodeState, targets) -> np.ndarray:
        p = self.params
        two_sig2 = 2.0 * p.sigma_mm * p.sigma_mm
        b = np.zeros(ns.n)
        for x, y, conf in targets:
            d2 = ((ns.led_xy[:, 0] - x) ** 2 + (ns.led_xy[:, 1] - y) ** 2)
            b += conf * p.peak * np.exp(-d2 / two_sig2)
        # Gamma on the summed pools; ambient is a post-gamma DUTY floor —
        # a perceptual 3% (0.03^2.2) is below one 8-bit LSB and would
        # quantise to black, so the floor has to sit in duty space. The
        # master envelope is gamma'd too so idle fades look linear.
        v = p.ambient + (1.0 - p.ambient) * np.clip(b, 0.0, 1.0) ** p.gamma
        v *= self.master ** p.gamma
        return v[:, None] * np.array(p.warm_rgb, dtype=float)

    def _step_master(self, dt: float, want_on: bool) -> None:
        if want_on:
            rate = dt / max(1e-3, float(self.cfg.service.wake_fade_s))
            self.master = min(1.0, self.master + rate)
        else:
            rate = dt / max(1e-3, float(self.cfg.service.idle_fade_s))
            self.master = max(0.0, self.master - rate)

    def _apply_budget(self, ns: NodeState, frame: np.ndarray) -> np.ndarray:
        per_led = float(ns.cfg.ma_per_led_full)
        quiescent = float(ns.cfg.quiescent_ma_per_led) * ns.n
        budget = float(ns.cfg.budget_ma)
        active = frame.sum() / 765.0 * per_led
        ns.est_ma = active + quiescent
        if ns.est_ma <= budget or active <= 0:
            ns.clamped = False
            return frame
        ns.clamped = True
        scale = max(0.0, (budget - quiescent) / active)
        now = time.monotonic()
        if now - ns._last_clamp_log > 1.0:      # tuning signal, not normal state
            ns._last_clamp_log = now
            log.warning("current budget clamp on %s: est %.0f mA > %.0f mA, "
                        "scaling by %.2f", ns.cfg.name, ns.est_ma, budget, scale)
        ns.est_ma = budget
        return frame * scale

    def stats(self) -> dict:
        return {name: {"est_ma": round(ns.est_ma, 1), "clamped": ns.clamped}
                for name, ns in self.nodes.items()}
