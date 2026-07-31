#!/usr/bin/env python3
"""Free-window fill planner (spec 2026-07-31).

Pure arithmetic, no I/O — foxctl reads the snapshot and writes the scheduler; this
module only decides numbers. Design: docs/superpowers/specs/2026-07-31-free-window-fill-planner-design.md

The problem it solves: the 10:00-14:00 base ForceCharge group filled the battery at a
fixed inverter-maximum rate, which left no supply headroom for the car. The car started,
breached the ~14.5 kW supply cap, got cut by the fast-guard, restarted ten minutes later
and cut again. Sizing the fill to what the battery actually needs before the window
closes leaves deterministic room for the car and stops the flap at its source.

PV is curtailed while ForceCharge runs (measured: 0.03 kW with 12 kWh still forecast), so
the solar forecast is NOT credited against the fill. It only decides whether 90% by 14:00
is a sufficient deadline target or whether a poor afternoon means buying more than that.
"""
import math
from datetime import datetime, timedelta

__all__ = ["solar_kwh_between", "plan_fill", "car_deadline_status",
           "hours_until_weekly", "free_window_hours_before"]


def solar_kwh_between(sunset, kwh_remaining, start, end, daylen_h=10.0):
    """Forecast solar (kWh) landing between `start` and `end`.

    Same half-sine daylight model the dashboard's solar bells use: power peaks at solar
    noon and the area over [sunset - daylen_h, sunset] equals `kwh_remaining`. Integrating
    it in closed form gives the energy in any sub-interval. Returns 0.0 when the interval
    misses daylight entirely or the inputs are missing.
    """
    if not sunset or not kwh_remaining or not start or not end:
        return 0.0
    length = float(daylen_h or 0.0)
    if length <= 0 or end <= start:
        return 0.0
    win_start = sunset - timedelta(hours=length)
    a, b = max(start, win_start), min(end, sunset)
    if b <= a:
        return 0.0
    ta = (a - win_start).total_seconds() / 3600.0
    tb = (b - win_start).total_seconds() / 3600.0
    area = (math.cos(math.pi * ta / length) - math.cos(math.pi * tb / length)) / 2.0
    return round(float(kwh_remaining) * area, 4)


def plan_fill(soc, capacity_kwh, now_h, deadline_h, deadline_soc=90.0, max_soc_cap=100.0,
              solar_after_deadline_kwh=0.0, house_kw=0.0, car_kw_est=2.5,
              supply_cap_kw=14.5, max_fill_kw=10.5, min_fill_kw=1.0, guard_kw=0.8,
              trim_kw=0.0, charge_eff=0.95, margin=1.1):
    """How hard to force-charge the battery this cycle, and whether the car fits alongside.

    The battery's deadline need is the floor of the fill rate — the car never takes
    precedence over hitting `deadline_soc` by `deadline_h`, because the only way to make
    room for it would be to fill slower than that need. When the battery is already at
    target the need is zero and the battery simply absorbs whatever the house and car
    leave, so free-window energy is never wasted.

    `trim_kw` is the fast-guard backoff: a breach observed between planning cycles is
    subtracted here so the next plan is more conservative.
    """
    cap = float(capacity_kwh or 0.0)
    max_fill = float(max_fill_kw or 0.0)
    min_fill = float(min_fill_kw or 0.0)

    # Unknown SoC: assume the battery needs everything. Safe direction — the deadline is
    # the hard requirement and the car is the thing that yields.
    if soc is None or cap <= 0:
        return {"fill_kw": round(max_fill, 2), "car_ok": False, "target_soc": float(deadline_soc),
                "need_kw": max_fill, "must_kw": max_fill, "deficit_kwh": 0.0,
                "spare_kw": 0.0, "hours_left": 0.0, "trim_kw": round(float(trim_kw or 0.0), 2),
                "reason": "no SoC reading — full fill, car held off"}

    # 1. Deadline target: `deadline_soc`, raised toward the cap when the afternoon's solar
    #    cannot be trusted to finish the job after the window. `solar_after_deadline_kwh` of
    #    None means UNKNOWN (no sunset or forecast reading) — distinct from a known zero.
    #    Unknown leaves the target alone: a hiccup in sun.sun must not silently turn into
    #    "buy to 100% on free grid", which would lock the car out for the whole window.
    soc_cap = float(max_soc_cap)
    target = float(deadline_soc)
    if solar_after_deadline_kwh is not None:
        top_up_kwh = max(0.0, (soc_cap - target) / 100.0 * cap)
        shortfall = max(0.0, top_up_kwh - float(solar_after_deadline_kwh))
        if shortfall > 0:
            target = min(soc_cap, target + shortfall / cap * 100.0)

    # 2. Power the battery must take to reach that target by the deadline.
    deficit_kwh = max(0.0, (target - float(soc)) / 100.0 * cap)
    hours_left = max(0.1, float(deadline_h) - float(now_h))
    eff = float(charge_eff or 1.0) or 1.0
    need_kw = deficit_kwh / hours_left / eff
    must_kw = min(max_fill, need_kw * float(margin or 1.0))

    # 3. Split the supply cap: house first (it is not negotiable), then a guard margin and
    #    the fast-guard backoff, then the car if it fits above the battery's need.
    spare = float(supply_cap_kw) - float(house_kw or 0.0) - float(guard_kw or 0.0) - float(trim_kw or 0.0)
    with_car = spare - float(car_kw_est or 0.0)
    if with_car >= must_kw:
        car_ok = True
        fill = min(max_fill, max(with_car, must_kw))
        reason = (f"car fits: fill {fill:.1f}kW + house {float(house_kw or 0):.1f}kW "
                  f"+ car {float(car_kw_est or 0):.1f}kW under the {float(supply_cap_kw):g}kW cap")
    else:
        car_ok = False
        fill = min(max_fill, max(spare, must_kw))
        reason = (f"battery needs {must_kw:.1f}kW to reach {target:.0f}% by "
                  f"{float(deadline_h):.0f}:00 — no room for the car")
    fill = min(max_fill, max(fill, min_fill))
    total = fill + float(house_kw or 0.0) + (float(car_kw_est or 0.0) if car_ok else 0.0)

    return {"fill_kw": round(fill, 2), "total_kw": round(total, 2),
            "car_ok": car_ok, "target_soc": round(target, 1),
            "need_kw": round(need_kw, 2), "must_kw": round(must_kw, 2),
            "deficit_kwh": round(deficit_kwh, 2), "spare_kw": round(spare, 2),
            "hours_left": round(hours_left, 2), "trim_kw": round(float(trim_kw or 0.0), 2),
            "reason": reason}


def hours_until_weekly(now, weekday, hour):
    """Hours from `now` until the next occurrence of `weekday` (Mon=0 .. Sun=6) at `hour`."""
    target = now.replace(hour=int(hour), minute=0, second=0, microsecond=0)
    days = (int(weekday) - now.weekday()) % 7
    target += timedelta(days=days)
    if target <= now:
        target += timedelta(days=7)
    return round((target - now).total_seconds() / 3600.0, 6)


def free_window_hours_before(now, deadline, win_start_h, win_end_h):
    """Free-tariff window hours available between `now` and `deadline`.

    Wall-clock hours overstate what the car can actually take: it charges in the free
    window, not continuously. This sums the window's overlap with [now, deadline] day by
    day so the car's deadline projection is honest.
    """
    if not now or not deadline or deadline <= now:
        return 0.0
    total = 0.0
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= deadline:
        ws = day + timedelta(hours=float(win_start_h))
        we = day + timedelta(hours=float(win_end_h))
        a, b = max(ws, now), min(we, deadline)
        if b > a:
            total += (b - a).total_seconds() / 3600.0
        day += timedelta(days=1)
    return round(total, 4)


def car_deadline_status(car_soc, pack_kwh, target_soc, chargeable_hours, charge_kw, eff=0.9):
    """Can the car reach `target_soc` by its deadline on the charging time available?

    `on_track` is None when the car's SoC is unknown — unknown is not the same as behind,
    and a notification fired on a missing reading is noise.
    """
    pack = float(pack_kwh or 0.0)
    rate = float(charge_kw or 0.0) * float(eff or 1.0)
    if car_soc is None or pack <= 0 or rate <= 0:
        return {"kwh_needed": None, "hours_needed": None, "on_track": None,
                "chargeable_hours": round(float(chargeable_hours or 0.0), 2)}
    kwh_needed = max(0.0, (float(target_soc) - float(car_soc)) / 100.0 * pack)
    hours_needed = kwh_needed / rate
    return {"kwh_needed": round(kwh_needed, 3), "hours_needed": round(hours_needed, 3),
            "on_track": hours_needed <= float(chargeable_hours or 0.0),
            "chargeable_hours": round(float(chargeable_hours or 0.0), 2)}
