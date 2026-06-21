#!/usr/bin/env python3
"""Read-only FoxESS API spike (forecasting Phase 1).

Confirms the shape/granularity of the report + history endpoints BEFORE we build the forecast
store on top of them. Makes NO writes to the inverter — only /report/query and /history/query.

Run it where a FoxESS token is available. Credentials are resolved in this order:
  1. CLI:  --token / --sn
  2. env:  FOXESS_TOKEN / FOXESS_SN
  3. the foxctl config (FOXCTL_CONFIG env, else ~/.config/foxctl/config.json)

Inside the add-on container the config path is /data/.config/foxctl/config.json, e.g.:
  FOXCTL_CONFIG=/data/.config/foxctl/config.json python3 /foxess_probe.py --days 3

What to look for in the output:
  * REPORT dimension=day → each variable should have n=24 (one value per hour). That's the
    hourly load (`loads`) + generation we'll average into the forecast profiles.
  * HISTORY → the step between points tells us the raw telemetry granularity (~5 min typical).
  * Any variable that errors / comes back empty tells us the name isn't valid for this account.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from foxctl import FoxESS, CONFIG_PATH  # noqa: E402

REPORT_VARS = ["loads", "generation", "feedin", "gridConsumption",
               "chargeEnergyToTal", "dischargeEnergyToTal"]
HISTORY_VARS = ["loadsPower", "pvPower", "SoC"]


def resolve_creds(args):
    if args.token and args.sn:
        return args.token, args.sn
    t, s = os.environ.get("FOXESS_TOKEN"), os.environ.get("FOXESS_SN")
    if t and s:
        return t, s
    p = Path(os.environ.get("FOXCTL_CONFIG", str(CONFIG_PATH)))
    if p.exists():
        c = json.loads(p.read_text())
        return c["foxess"]["token"], c["foxess"]["sn"]
    sys.exit("no credentials: pass --token/--sn, set FOXESS_TOKEN/FOXESS_SN, or have a foxctl config")


def probe_report(fox, days):
    for d in range(1, days + 1):
        day = datetime.now() - timedelta(days=d)
        print(f"\n=== REPORT dimension=day {day:%Y-%m-%d} ===")
        try:
            res = fox.report(REPORT_VARS, "day", day)
        except Exception as e:
            print(f"  report failed: {e}")
            continue
        if not res:
            print("  (empty result)")
        for item in res:
            vals = item.get("values") or []
            nums = [v for v in vals if isinstance(v, (int, float))]
            total = round(sum(nums), 2)
            print(f"  {str(item.get('variable')):22} n={len(vals):2}  total={total:8}  unit={item.get('unit')}")
            if item.get("variable") in ("loads", "generation"):
                print("       hourly:", [round(v, 2) if isinstance(v, (int, float)) else v for v in vals])


def probe_history(fox):
    end = int(datetime.now().timestamp() * 1000)
    begin = end - 2 * 3600 * 1000   # last 2 hours
    print("\n=== HISTORY last 2h (granularity check) ===")
    try:
        res = fox.history(HISTORY_VARS, begin, end)
    except Exception as e:
        print(f"  history failed: {e}")
        return
    datas = (res[0].get("datas") if res else None) or []
    if not datas:
        print("  (empty result)")
    for ds in datas:
        pts = ds.get("data") or []
        print(f"  {str(ds.get('variable')):14} points={len(pts):4}  unit={ds.get('unit')}")
        for p in pts[:3]:
            print("       ", p)


def main():
    ap = argparse.ArgumentParser(description="read-only FoxESS report/history spike")
    ap.add_argument("--token")
    ap.add_argument("--sn")
    ap.add_argument("--days", type=int, default=2, help="how many past days of hourly report to sample")
    args = ap.parse_args()
    token, sn = resolve_creds(args)
    fox = FoxESS(token, sn)
    print(f"FoxESS probe — sn=…{sn[-4:]}  (READ-ONLY: report + history only)")
    probe_report(fox, max(1, args.days))
    probe_history(fox)
    print("\nDone. If REPORT rows show n=24, the hourly load/generation profile is available to backfill.")


if __name__ == "__main__":
    main()
