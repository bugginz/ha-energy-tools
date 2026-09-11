#!/usr/bin/env python3
"""Radar pose calibration from known floor points (PLAN.md §9.2), live.

Stand on a known room coordinate, capture ~10 s of raw detections from
every radar via the running service's websocket, repeat for 3-4 points,
then solve each sensor's rigid transform (Procrustes) and write it back.

    python3 tools/calibrate.py capture --label c1 --room 500 500
    python3 tools/calibrate.py capture --label c2 --room 4500 500
    ...
    python3 tools/calibrate.py show
    python3 tools/calibrate.py solve --config config.bench.yaml          # print
    python3 tools/calibrate.py solve --config config.bench.yaml --write  # update poses

Points accumulate in --points (JSON); recapturing a label replaces it.
Per source, a capture is the median of that sensor's detections over the
window (robust against a stray ghost); its spread (median absolute
deviation) is reported and captures with too few/too scattered detections
are skipped in the fit. Slant-range projection is applied with the
config's mount/target heights before fitting, exactly as the service does.
"""

import argparse
import asyncio
import json
import math
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kickboard import config as config_mod  # noqa: E402
from kickboard import radar  # noqa: E402

DEFAULT_POINTS = "data/calibration-points.json"


def load_points(path):
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else []


def save_points(path, pts):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(pts, indent=1))


async def collect(url, seconds):
    import websockets
    per_ip = {}
    async with websockets.connect(url, max_size=None) as ws:
        t_end = time.time() + seconds
        last_seen = {}
        while time.time() < t_end:
            d = json.loads(await ws.recv())
            for ip, sc in d.get("radar", {}).items():
                # the ws repeats a frame until the next arrives: dedupe on age
                key = (sc.get("age_s"), tuple(map(tuple, sc.get("last", []))))
                if last_seen.get(ip) == key:
                    continue
                last_seen[ip] = key
                per_ip.setdefault(ip, []).extend(
                    (x, y) for x, y, _v in sc.get("last", []))
    return per_ip


CELL = 300.0      # clustering grid, mm
GHOST_R = 600.0   # a cluster this close to one seen at another spot is static


def clusters(dets):
    """Greedy grid clustering -> [(x, y, n)] densest first."""
    cells = {}
    for x, y in dets:
        cells.setdefault((round(x / CELL), round(y / CELL)), []).append((x, y))
    out = []
    used = set()
    for key in sorted(cells, key=lambda k: -len(cells[k])):
        if key in used:
            continue
        group = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                k2 = (key[0] + dx, key[1] + dy)
                if k2 in cells and k2 not in used:
                    used.add(k2)
                    group += cells[k2]
        mx = statistics.median(g[0] for g in group)
        my = statistics.median(g[1] for g in group)
        out.append((mx, my, len(group)))
    return sorted(out, key=lambda c: -c[2])


def summarise(dets, ghosts=()):
    """Pick the person's cluster: the densest one that is not a known ghost
    (something that sat in the same place during another capture)."""
    if not dets:
        return {"n": 0}
    cl = clusters(dets)
    live = [c for c in cl if not any(math.dist(c[:2], g) < GHOST_R for g in ghosts)]
    if not live:
        live = cl
    mx, my, _n = live[0]
    members = [d for d in dets if math.dist(d, (mx, my)) < 2 * CELL]
    spread = statistics.median(math.dist(d, (mx, my)) for d in members)
    return {"x": round(mx), "y": round(my), "n": len(members), "spread": round(spread),
            "total": len(dets),
            "clusters": [[round(x), round(y), n] for x, y, n in cl[:4]]}


def ghosts_for(pts, ip, exclude_label):
    """Cluster centres this sensor reported at >= 2 OTHER spots: something
    that sits still while the person moves around is a reflection. (One
    other spot is not enough — two corners 1.2 m apart look alike from a
    sensor 4 m away, which is bearing resolution, not a ghost.)"""
    seen = []   # (label, (x, y))
    for p in pts:
        if p["label"] == exclude_label:
            continue
        for c in p["sources"].get(ip, {}).get("clusters", []):
            seen.append((p["label"], (c[0], c[1])))
    out = []
    for la, a in seen:
        if any(lb != la and math.dist(a, b) < GHOST_R for lb, b in seen):
            out.append(a)
    return out


def resummarise(pts):
    """Re-pick clusters for every capture that kept its raw detections,
    with ghost knowledge from all the others."""
    for p in pts:
        for ip, s in p["sources"].items():
            raw = s.get("raw")
            if raw:
                s.update(summarise([tuple(d) for d in raw],
                                   ghosts_for(pts, ip, p["label"])))
    return pts


def cmd_capture(args):
    print(f"capturing {args.seconds:g} s for '{args.label}' at room "
          f"({args.room[0]:.0f}, {args.room[1]:.0f}) — stand there, sway gently…",
          flush=True)
    per_ip = asyncio.run(collect(args.url, args.seconds))
    pts = [p for p in load_points(args.points) if p["label"] != args.label]
    entry = {"label": args.label, "room": [float(args.room[0]), float(args.room[1])],
             "ts": time.time(), "sources": {}}
    for ip, dets in per_ip.items():
        s = summarise(dets, ghosts_for(pts, ip, args.label))
        s["raw"] = [[round(x), round(y)] for x, y in dets]
        entry["sources"][ip] = s
    pts.append(entry)
    pts = resummarise(pts)
    save_points(args.points, pts)
    for ip, s in entry["sources"].items():
        if s["n"]:
            print(f"  {ip}: person at ({s['x']}, {s['y']}) mm  n={s['n']}/{s['total']}  "
                  f"spread={s['spread']} mm  clusters={s['clusters']}"
                  + ("  <- weak" if s["n"] < args.min_n or s["spread"] > args.max_spread else ""))
        else:
            print(f"  {ip}: no detections")
    print(f"saved to {args.points} ({len(pts)} points)")


def cmd_show(args):
    for p in load_points(args.points):
        srcs = ", ".join(f"{ip}: ({s['x']},{s['y']}) n={s['n']} ±{s['spread']}"
                         if s["n"] else f"{ip}: -" for ip, s in p["sources"].items())
        print(f"{p['label']:6s} room ({p['room'][0]:.0f},{p['room'][1]:.0f})  {srcs}")


def cmd_solve(args):
    cfg = config_mod.load_config(args.config)
    _default, by_ip = radar.build_pose_table(cfg.radar)
    pts = resummarise(load_points(args.points))
    results = {}
    for src in cfg.radar.get("sources", []):
        ip = str(src.ip)
        slant_h = by_ip[ip][1]
        sensor, room, labels = [], [], []
        for p in pts:
            s = p["sources"].get(ip, {"n": 0})
            if s["n"] < args.min_n or s["spread"] > args.max_spread:
                continue
            if math.hypot(s["x"], s["y"]) < args.min_range:
                print(f"  skipping {p['label']}: {math.hypot(s['x'], s['y']):.0f} mm is "
                      "near-field (under the sensor)")
                continue
            sensor.append(radar.slant_to_floor(s["x"], s["y"], slant_h))
            room.append(tuple(p["room"]))
            labels.append(p["label"])
        print(f"\n{src.name} ({ip}): {len(sensor)} usable points {labels}")
        if len(sensor) < 2:
            print("  need at least 2 — capture more corners this sensor can see")
            continue
        best = None
        for mirror in (False, True):
            pts_s = [((-x if mirror else x), y) for x, y in sensor]
            theta, tx, ty = radar.solve_pose(pts_s, room)
            pose = radar.Pose(math.radians(theta), tx, ty, mirror_x=mirror)
            res = [math.dist(pose.to_room(*s), r) for s, r in zip(sensor, room)]
            rms = math.sqrt(sum(r * r for r in res) / len(res))
            print(f"  mirror_x={str(mirror):5s} theta {theta:7.1f} deg  tx {tx:7.0f}  ty {ty:7.0f}   "
                  f"residuals mm: {[round(r) for r in res]}  (rms {rms:.0f})")
            if best is None or rms < best[0]:
                best = (rms, theta, tx, ty, mirror)
        rms, theta, tx, ty, mirror = best
        print(f"  -> using mirror_x={mirror}" + ("  (sensor mounted flipped)" if mirror else ""))
        if len(sensor) == 2:
            print("  (2 points fit exactly; residuals are meaningless — add a third)")
        results[ip] = (theta, tx, ty, mirror)
    if args.write and results:
        text = Path(args.config).read_text()
        for ip, (theta, tx, ty, mirror) in results.items():
            pat = re.compile(r"(ip:\s*" + re.escape(ip) + r"\s*\n\s*pose:\s*\{)([^}]*)(\})")
            m = pat.search(text)
            if not m:
                print(f"  could not find a pose line for {ip} in {args.config}")
                continue
            old = m.group(2)
            keep = [kv for kv in old.split(",") if kv.strip() and
                    kv.split(":")[0].strip() not in ("theta_deg", "tx_mm", "ty_mm", "mirror_x")]
            new = ", ".join([f"theta_deg: {theta:.1f}", f"tx_mm: {tx:.0f}", f"ty_mm: {ty:.0f}",
                             f"mirror_x: {'true' if mirror else 'false'}"]
                            + [kv.strip() for kv in keep])
            text = text[:m.start(2)] + new + text[m.end(2):]
        Path(args.config).write_text(text)
        print(f"\nwrote poses to {args.config} — restart the service to apply")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--points", default=DEFAULT_POINTS)
    ap.add_argument("--url", default="ws://127.0.0.1:8771/ws")
    ap.add_argument("--min-n", type=int, default=20)
    ap.add_argument("--max-spread", type=float, default=400)
    ap.add_argument("--min-range", type=float, default=1000,
                    help="skip captures closer than this (mm): under an elevated "
                         "sensor the slant->floor projection is unusable")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture"); c.add_argument("--label", required=True)
    c.add_argument("--room", type=float, nargs=2, required=True, metavar=("X_MM", "Y_MM"))
    c.add_argument("--seconds", type=float, default=10)
    sub.add_parser("show")
    s = sub.add_parser("solve"); s.add_argument("--config", default="config.yaml")
    s.add_argument("--write", action="store_true")
    args = ap.parse_args()
    {"capture": cmd_capture, "show": cmd_show, "solve": cmd_solve}[args.cmd](args)


if __name__ == "__main__":
    main()
