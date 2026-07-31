#!/usr/bin/env python3
"""Live EM16P channel monitor for breaker testing.

Run it, flip a breaker, watch which row moves. One HTTP request per sample: it POSTs a
Jinja template to /api/template and gets every channel plus the derived supply back in a
single small payload.

    HA_TOKEN=$(cat /opt/stack/energy_tools/data/.config/sen66/ha_token) \
        python3 circuit_probe.py [--interval 5] [--url http://localhost:8123]

Reading it
----------
A clamped circuit switching off drops BOTH its own row and the supply.
An unclamped circuit switching off drops the supply ONLY — visible as the residual
falling toward its floor. That is the discriminator between "missing clamp" and
"clamp on a dead circuit".

The residual floor is not zero: summing twelve clamps against the supply carries a
small systematic offset (measured at about -15 W on this install). Compare the residual
against that floor, not against zero.

The EM16P reports roughly every 30 s, so hold each breaker state for at least a minute
before believing what you see.
"""
import argparse
import json
import os
import sys
import time
import urllib.request

DEV = "sensor.em16p_26041762237810740701c4e7ae2e4c89_power_"

TEMPLATE = ("{% set ns = namespace(v=[]) %}"
            "{% for i in range(1, 19) %}"
            "{% set ns.v = ns.v + [states('" + DEV + "' ~ i) | float(0) | round(1)] %}"
            "{% endfor %}{{ ns.v | tojson }}")


def sample(url, token):
    req = urllib.request.Request(f"{url.rstrip('/')}/api/template", method="POST",
                                 data=json.dumps({"template": TEMPLATE}).encode(),
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--url", default=os.environ.get("HA_URL", "http://localhost:8123"))
    ap.add_argument("--threshold", type=float, default=15.0,
                    help="W change vs baseline before a row is flagged")
    args = ap.parse_args()
    token = os.environ.get("HA_TOKEN")
    if not token:
        raise SystemExit("HA_TOKEN not set")

    names = {1: "Grid", 7: "Inverter", 8: "Main AC", 9: "Hot Water", 10: "Oven",
             12: "Downstairs", 14: "ch14 (lighting?)", 16: "Cooktop", 18: "Car charger"}
    base = None
    print("sampling… first reading becomes the baseline; Ctrl-C to stop\n")
    while True:
        try:
            vals = sample(args.url, token)
        except Exception as e:
            print(f"  sample failed: {e}")
            time.sleep(args.interval)
            continue
        # ch1 grid + ch7 inverter is the supply; everything else is a house circuit.
        supply = vals[0] + vals[6]
        circuits = sum(v for i, v in enumerate(vals) if i not in (0, 6))
        resid = supply - circuits
        if base is None:
            base = list(vals) + [resid]
            print("baseline captured\n")
        moved = []
        for i, v in enumerate(vals):
            ch = i + 1
            if abs(v - base[i]) >= args.threshold:
                moved.append(f"ch{ch} {names.get(ch, '')} {base[i]:.0f}->{v:.0f} W")
        dres = resid - base[-1]
        stamp = time.strftime("%H:%M:%S")
        print(f"{stamp}  supply {supply:7.0f} W   circuits {circuits:7.0f} W   "
              f"residual {resid:6.0f} W ({dres:+.0f} vs base)")
        for m in moved:
            print(f"           MOVED  {m}")
        if not moved and abs(dres) >= args.threshold:
            print("           residual moved but NO channel did "
                  "-> that circuit is UNCLAMPED")
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
