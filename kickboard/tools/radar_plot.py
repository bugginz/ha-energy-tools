#!/usr/bin/env python3
"""Plot what the RD-03D sees, from the raw-frame recordings (PLAN.md §9.3).

    python3 tools/radar_plot.py data/radar-logs/radar-2026-09-11.jsonl
    python3 tools/radar_plot.py data/radar-logs/*.jsonl --minutes 30 --out /tmp/radar.png

Left: every detection in the last --minutes, in the SENSOR frame (x lateral,
y forward, mm), coloured by time, with the +-60 deg field of view and range
rings. Right: a log-density map of the whole recording — persistent bright
spots that never move are multipath ghosts (fridge, splashback...) and are
what the exclusion zones in config are for.
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kickboard import radar  # noqa: E402


def load(paths):
    ts, xs, ys, per_frame = [], [], [], []
    for path in paths:
        for t, raw in radar.replay_frames(path):
            f = radar.parse_frame(raw, t)
            if f is None:
                continue
            per_frame.append(len(f.targets))
            for tg in f.targets:
                ts.append(t)
                xs.append(tg.x_mm)
                ys.append(tg.y_mm)
    return (np.array(ts), np.array(xs), np.array(ys), np.array(per_frame))


def fov_patch(ax, range_mm=8000, half_deg=60):
    a = np.radians(np.linspace(-half_deg, half_deg, 60))
    ax.fill(np.concatenate([[0], range_mm * np.sin(a)]),
            np.concatenate([[0], range_mm * np.cos(a)]),
            color="tab:blue", alpha=0.05, lw=0)
    for r in range(1000, range_mm + 1, 1000):
        ax.plot(r * np.sin(a), r * np.cos(a), color="0.75", lw=0.5, ls=":")
        ax.text(0, r, f"{r // 1000} m", color="0.5", fontsize=7,
                ha="center", va="bottom")
    ax.plot(0, 0, "s", color="tab:blue", ms=8)
    ax.set_aspect("equal")
    ax.set_xlabel("lateral x (mm)  +right")
    ax.set_ylabel("forward y (mm)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--minutes", type=float, default=10.0,
                    help="window for the scatter panel (default 10)")
    ap.add_argument("--out", default="/tmp/radar_view.png")
    args = ap.parse_args()

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LogNorm

    ts, xs, ys, per_frame = load(args.logs)
    if len(per_frame) == 0:
        sys.exit("no frames")
    t_end = ts.max() if len(ts) else time.time()
    recent = ts >= t_end - args.minutes * 60

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(14, 7))
    fov_patch(a1)
    if recent.any():
        sc = a1.scatter(xs[recent], ys[recent], c=(ts[recent] - t_end) / 60,
                        s=6, alpha=0.5, cmap="viridis", lw=0)
        fig.colorbar(sc, ax=a1, label="minutes before end of log")
    a1.set_title(f"last {args.minutes:g} min: {int(recent.sum())} detections "
                 f"({time.strftime('%H:%M', time.localtime(t_end))})")
    a1.set_xlim(-6000, 6000)
    a1.set_ylim(-500, 8000)

    fov_patch(a2)
    if len(xs):
        h = a2.hist2d(xs, ys, bins=[120, 85], range=[[-6000, 6000], [-500, 8000]],
                      norm=LogNorm(), cmap="inferno", cmin=1)
        fig.colorbar(h[3], ax=a2, label="detections (log)")
    span_h = (ts.max() - ts.min()) / 3600 if len(ts) > 1 else 0
    a2.set_title(f"whole recording: {len(xs)} detections over {span_h:.1f} h, "
                 f"{len(per_frame)} frames")
    a2.set_xlim(-6000, 6000)
    a2.set_ylim(-500, 8000)

    fig.suptitle("RD-03D, sensor frame (sensor at origin, looking up the page)")
    fig.tight_layout()
    fig.savefig(args.out, dpi=110)

    counts = np.bincount(per_frame, minlength=4)
    print(f"{len(per_frame)} frames; targets/frame 0:{counts[0]} 1:{counts[1]} "
          f"2:{counts[2]} 3:{counts[3]}; wrote {args.out}")


if __name__ == "__main__":
    main()
