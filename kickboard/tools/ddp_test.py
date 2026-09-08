#!/usr/bin/env python3
"""Phase 1 bench script: drive a strip node with test patterns over DDP.

    python3 tools/ddp_test.py kick-left.local solid --rgb 255 147 41
    python3 tools/ddp_test.py kick-left.local chase
    python3 tools/ddp_test.py kick-left.local gradient --minutes 10
    python3 tools/ddp_test.py kick-left.local off
    python3 tools/ddp_test.py kick-left.local ruler --leds 750

ruler: LED 0 green, every 10th warm white, every 50th blue, every 100th
red — count the marks to the physical end of the strip to get its LED
count, and use it with the sim's chase to check §9.1 geometry.

Acceptance (PLAN.md phase 1): solid colour, chase, and a 30 fps gradient
running 10 minutes with no glitch.
"""

import argparse
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kickboard.ddp import DDP_PORT, DDPSender  # noqa: E402


def frame_solid(n, t, rgb):
    return bytes(rgb) * n


def frame_chase(n, t, rgb):
    buf = bytearray(n * 3)
    i = int(t * 30) % n          # one LED stepping at 30 px/s
    buf[i * 3:i * 3 + 3] = bytes(rgb)
    return bytes(buf)


def frame_gradient(n, t, rgb):
    # sine wave sweeping along the strip; exercises every LED every cycle
    buf = bytearray(n * 3)
    for i in range(n):
        v = 0.5 + 0.5 * math.sin(2 * math.pi * (i / n - t / 4))
        buf[i * 3:i * 3 + 3] = bytes(int(c * v) for c in rgb)
    return bytes(buf)


def frame_ruler(n, t, rgb):
    buf = bytearray(n * 3)
    for i in range(n):
        if i == 0:
            c = (0, 200, 0)
        elif i % 100 == 0:
            c = (200, 0, 0)
        elif i % 50 == 0:
            c = (0, 0, 200)
        elif i % 10 == 0:
            c = tuple(v // 3 for v in rgb)
        else:
            continue
        buf[i * 3:i * 3 + 3] = bytes(c)
    return bytes(buf)


PATTERNS = {"solid": frame_solid, "chase": frame_chase,
            "gradient": frame_gradient, "ruler": frame_ruler,
            "off": lambda n, t, rgb: bytes(n * 3)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("host")
    ap.add_argument("pattern", choices=PATTERNS)
    ap.add_argument("--port", type=int, default=DDP_PORT)
    ap.add_argument("--leds", type=int, default=240)
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--rgb", type=int, nargs=3, default=[255, 147, 41])
    ap.add_argument("--minutes", type=float, default=0,
                    help="run this long then send off (0 = until ^C)")
    args = ap.parse_args()

    sender = DDPSender()
    fn = PATTERNS[args.pattern]
    period = 1.0 / args.fps
    t0 = time.monotonic()
    frames = 0
    try:
        while True:
            t = time.monotonic() - t0
            if args.minutes and t > args.minutes * 60:
                break
            sender.send_frame(args.host, args.port, fn(args.leds, t, args.rgb))
            frames += 1
            if frames % int(args.fps * 5) == 0:
                print(f"{t:7.1f}s  {frames} frames  {frames / t:.1f} fps")
            time.sleep(max(0.0, t0 + frames * period - time.monotonic()))
            if args.pattern in ("off", "solid", "ruler") and not args.minutes:
                # static patterns don't need 30 fps; refresh at 2 Hz
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        sender.send_frame(args.host, args.port, bytes(args.leds * 3))
        print(f"done: {frames} frames")


if __name__ == "__main__":
    main()
