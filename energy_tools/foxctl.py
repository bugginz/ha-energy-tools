#!/usr/bin/env python3
"""foxctl — price-aware FoxESS work-mode controller.

Gathers the inputs you care about every cycle:
  * Amber price + forecast   (read from your existing Home Assistant entities)
  * Inverter SoC + solar/PV  (FoxESS OpenAPI, authoritative)
  * Current work mode        (FoxESS OpenAPI)
...runs a transparent decision engine, and RECOMMENDS a work setting. It can
also SET it (work mode and/or a grid force-charge window) — but only when you
explicitly enable control in the config. Default is recommend-only.

Stdlib only (urllib + http.server) so it runs on a bare Pi with no pip installs.

Usage:
    python3 foxctl.py status            # one-shot: gather + recommend, print
    python3 foxctl.py recommend --json  # machine-readable recommendation
    python3 foxctl.py apply             # apply the recommendation (needs control enabled)
    python3 foxctl.py loop              # run forever, every poll_seconds
    python3 foxctl.py serve             # web dashboard + background loop

Config: ~/.config/foxctl/config.json  (see write_default_config / --init).
Safety: control.allow_control gates ALL writes; control.auto_apply gates the
loop writing on its own. Even then, work-mode writes are reversible and the
force-charge window is bounded in time and SoC.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread

CONFIG_PATH = Path(os.environ.get("FOXCTL_CONFIG", Path.home() / ".config/foxctl/config.json"))
FOX_DOMAIN = "https://www.foxesscloud.com"
WORK_MODES = ["SelfUse", "Feedin", "Backup", "PeakShaving"]

# ---------------------------------------------------------------- config -----

DEFAULT_CONFIG = {
    "foxess": {
        "token": "PUT-FOXESS-OPENAPI-KEY-HERE",
        "sn": "PUT-INVERTER-SN-HERE",
    },
    "ha": {
        "url": "http://homeassistant.local:8123",
        "token_file": "~/.config/sen66/ha_token",
        "amber_price_entity": "sensor.home_general_price",
        "amber_forecast_entity": "sensor.home_general_forecast",
        # Solar offload / feed-in (export) tariff — appears once Amber feed-in channel is live.
        "amber_feedin_entity": "sensor.home_feed_in_price",
        "demand_window_entity": "binary_sensor.home_demand_window",
        # Read inverter telemetry from HA (foxess-ha integration) to avoid a 2nd FoxESS poller.
        "soc_entity": "sensor.foxess_bat_soc",
        "pv_entity": "sensor.foxess_pv_power",
        "load_entity": "sensor.foxess_load_power",
        # AEMO wholesale forecast (forward visibility, longer + steadier horizon than Amber)
        "aemo_forecast_entity": "sensor.aemo_nem_nsw1_current_30min_forecast",
    },
    "strategy": {
        "cheap_price": 0.10,        # Amber retail $/kWh at/below which we charge from grid
        "expensive_price": 0.35,    # Amber retail $/kWh at/above which we avoid grid import
        "target_soc": 100,          # force-charge cap (fdSoc)
        "reserve_soc": 20,          # never plan to go below this
        "precharge_lookahead_h": 3, # if an expensive peak is within this window, pre-charge
        # AEMO wholesale thresholds (different scale to retail) used for forward peak/trough detection
        "aemo_expensive": 0.20,     # wholesale $/kWh that signals a coming peak
        "aemo_cheap": 0.05,         # wholesale $/kWh that signals a coming trough
        "charge_start_price": 0.12, # begin grid-charge at/below this (hysteresis low)
        "charge_stop_margin": 0.05, # keep charging until price > start + this (hysteresis high)
        "defer_lookahead_h": 3,     # look this far ahead for a cheaper trough
        "defer_if_cheaper_by": 0.04,# …and wait if it's at least this much cheaper than now
        "force_charge_minutes": 120,  # max window length (safety cap); loop re-evaluates & stops early
        "force_charge_power_kw": 10.5,  # 10500 W
        "solar_defer_kw": 0.5,      # if PV exceeds load by this much, let solar charge (skip grid)
        "avoid_demand_window": True,  # skip grid force-charge while Amber demand window is active
        "horizon_charge": True,       # pre-charge in the cheapest forward window before a forecast peak
        "horizon_hours": 18,          # how far ahead to scan the price forecast
        "horizon_window_margin": 0.03,  # "near cheapest" = within this of the forward minimum
        "min_soc_on_grid": 10,
        # Price bands ($/kWh, retail). Ordered low->high; "upto" is the exclusive upper bound,
        # last band (upto null) is the catch-all. charge_bands grid-charge; avoid_bands hold battery.
        "bands": [
            {"name": "ludicrous",     "upto": 0.0},     # negative: paid to consume
            {"name": "extremely_low", "upto": 0.10},    # 0-10c
            {"name": "low",           "upto": 0.20},
            {"name": "normal",        "upto": 0.35},
            {"name": "high",          "upto": 1.00},
            {"name": "spike",         "upto": None}      # >= $1
        ],
        "charge_bands": ["ludicrous", "extremely_low"],
        "avoid_bands": ["high", "spike"],
    },
    "control": {
        "allow_control": False,     # master switch for ANY write to the inverter
        "auto_apply": False,        # let the loop apply without a human pressing apply
        "set_work_mode": True,      # may change work mode
        "set_force_charge": False,  # may push grid force-charge windows (more invasive)
        "allow_actions": False,     # may run band-triggered shell actions (downloads etc.)
    },
    # Edge-triggered shell commands run when ENTERING a band (needs control.allow_actions).
    # e.g. {"ludicrous": ["/home/robwil/bin/force-downloads.sh"], "spike": ["/home/robwil/bin/shed-load.sh"]}
    "actions": {},
    # Advisory LLM review of each decision (Anthropic API) — never controls anything.
    "llm": {"enabled": False, "api_key": "", "model": "claude-haiku-4-5-20251001", "interval_min": 30},
    # Push notifications when a decision is worth a human look (via HA notify service).
    "notify": {"enabled": False, "service": "notify.mobile_app_phoney",
               "on_llm_disagree": True, "on_spike": True, "on_ludicrous": True},
    "poll_seconds": 300,
    "web": {"host": "0.0.0.0", "port": 8770},
}


def write_default_config():
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
    os.chmod(CONFIG_PATH, 0o600)
    print(f"Wrote starter config to {CONFIG_PATH} — edit foxess.token/sn then run `status`.")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"No config at {CONFIG_PATH}. Run: python3 foxctl.py --init")
    cfg = json.loads(CONFIG_PATH.read_text())
    # shallow-merge defaults so new keys don't break old configs
    for k, v in DEFAULT_CONFIG.items():
        if isinstance(v, dict):
            cfg[k] = {**v, **cfg.get(k, {})}
        else:
            cfg.setdefault(k, v)
    return cfg


# --------------------------------------------------------------- clients -----

class FoxESS:
    """Minimal FoxESS OpenAPI client. Signature uses LITERAL \\r\\n (not CRLF)."""

    def __init__(self, token: str, sn: str):
        self.token, self.sn = token, sn

    def _sign(self, path: str):
        ts = round(time.time() * 1000)
        raw = f"{path}\\r\\n{self.token}\\r\\n{ts}"   # literal backslash-r-backslash-n
        sig = hashlib.md5(raw.encode()).hexdigest()
        return {
            "token": self.token, "lang": "en", "timestamp": str(ts),
            "Content-Type": "application/json", "signature": sig,
            "User-Agent": "foxctl", "Connection": "close",
        }

    def call(self, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            FOX_DOMAIN + path,
            data=(json.dumps(body).encode() if body is not None else None),
            headers=self._sign(path),
        )
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read().decode().strip()
        if not raw:
            return {"errno": 0, "msg": "empty"}   # some setters return 200 + no body on success
        d = json.loads(raw)
        if d.get("errno") not in (0, None):
            raise RuntimeError(f"FoxESS {path} errno={d.get('errno')}: {d.get('msg')}")
        return d

    def real(self, variables: list[str]) -> dict:
        d = self.call("/op/v0/device/real/query", {"sn": self.sn, "variables": variables})
        out = {}
        for item in d["result"][0]["datas"]:
            out[item["variable"]] = item.get("value")
        return out

    def report(self, variables: list[str], dimension: str = "day", when: datetime | None = None) -> list:
        """Energy report (kWh), read-only. dimension="day" → each variable's "values" is a 24-element
        HOURLY array for `when`'s date; "month" → per-day; "year" → per-month. Variables are energy
        stat names: loads, generation, feedin, gridConsumption, chargeEnergyToTal, dischargeEnergyToTal.
        Returns the raw result list: [{"variable","unit","values":[...]}]."""
        when = when or datetime.now()
        body = {"sn": self.sn, "dimension": dimension, "variables": list(variables),
                "year": when.year, "month": when.month, "day": when.day}
        return self.call("/op/v0/device/report/query", body).get("result") or []

    def history(self, variables: list[str], begin_ms: int, end_ms: int) -> list:
        """Raw telemetry time-series between begin/end (epoch ms), read-only. Sub-hourly granularity.
        Variables are power/SoC names: loadsPower, pvPower, gridConsumptionPower, feedinPower, SoC, …
        Returns the raw result list: [{"datas":[{"variable","unit","data":[{"time","value"},…]}]}]."""
        body = {"sn": self.sn, "variables": list(variables), "begin": int(begin_ms), "end": int(end_ms)}
        return self.call("/op/v0/device/history/query", body).get("result") or []

    def work_mode(self) -> dict:
        d = self.call("/op/v0/device/setting/get", {"sn": self.sn, "key": "WorkMode"})
        return d["result"]  # {value, enumList, ...}

    def set_work_mode(self, mode: str) -> dict:
        if mode not in WORK_MODES:
            raise ValueError(f"bad work mode {mode!r}; allowed {WORK_MODES}")
        return self.call("/op/v0/device/setting/set", {"sn": self.sn, "key": "WorkMode", "value": mode})

    def scheduler(self) -> dict:
        return self.call("/op/v0/device/scheduler/get", {"deviceSN": self.sn}).get("result")

    def scheduler_enabled(self) -> bool:
        r = self.scheduler() or {}
        return bool(r.get("enable"))

    def enable_force_charge(self, start_hm, end_hm, min_soc, cap_soc, power_kw) -> dict:
        """Activate ONE ForceCharge window via scheduler/enable (8-group schema, no overlap).

        NB: the group schema has no maxSoc — `fdSoc` is the charge cap. The other 7
        groups are sent disabled so we never touch the user's stored template behaviour
        while active; disable_scheduler() reverts to plain work mode afterwards.
        """
        sh, sm = start_hm; eh, em = end_hm
        # Send ONE group; FoxESS pads the rest with empty "Invalid" slots — clean, no junk.
        fc = {"startHour": sh, "startMinute": sm, "endHour": eh, "endMinute": em,
              "workMode": "ForceCharge", "minSocOnGrid": int(min_soc), "fdSoc": int(cap_soc),
              "fdPwr": int(power_kw * 1000), "enable": 1}
        return self.call("/op/v0/device/scheduler/enable", {"deviceSN": self.sn, "groups": [fc]})

    def enable_force_discharge(self, start_hm, end_hm, min_soc, power_kw) -> dict:
        """Activate ONE ForceDischarge window — sell battery to the grid (export) down to min_soc.
        Same scheduler/enable schema as force-charge; fdSoc is the SoC floor to discharge to."""
        sh, sm = start_hm; eh, em = end_hm
        fc = {"startHour": sh, "startMinute": sm, "endHour": eh, "endMinute": em,
              "workMode": "ForceDischarge", "minSocOnGrid": int(min_soc), "fdSoc": int(min_soc),
              "fdPwr": int(power_kw * 1000), "enable": 1}
        return self.call("/op/v0/device/scheduler/enable", {"deviceSN": self.sn, "groups": [fc]})

    def scheduler_status(self) -> dict:
        """Compact view for the dashboard: is a schedule active, and which window."""
        r = self.scheduler() or {}
        active = None
        if r.get("enable"):
            for g in r.get("groups", []):
                if g.get("enable") and g.get("workMode") not in (None, "Invalid"):
                    active = {"mode": g.get("workMode"),
                              "window": "%02d:%02d-%02d:%02d" % (g.get("startHour"), g.get("startMinute"),
                                                                 g.get("endHour"), g.get("endMinute")),
                              "fdSoc": g.get("fdSoc"), "fdPwr": g.get("fdPwr")}
                    break
        return {"enabled": bool(r.get("enable")), "active": active}

    def disable_scheduler(self) -> dict:
        """Stop any active schedule -> inverter reverts to its plain WorkMode.

        The working stop is set/flag enable=0 (scheduler/disable returns an empty
        body and does NOT actually clear the enable flag on this firmware).
        """
        return self.call("/op/v0/device/scheduler/set/flag", {"deviceSN": self.sn, "enable": 0})


class HAPrices:
    """Reads Amber price + forecast from Home Assistant (reuses the HA token)."""

    def __init__(self, url: str, token: str, price_entity: str, forecast_entity: str,
                 aemo_forecast_entity: str | None = None, feedin_entity: str | None = None):
        self.url, self.token = url.rstrip("/"), token
        self.price_entity, self.forecast_entity = price_entity, forecast_entity
        self.aemo_forecast_entity = aemo_forecast_entity
        self.feedin_entity = feedin_entity

    def _state(self, entity: str) -> dict:
        req = urllib.request.Request(
            f"{self.url}/api/states/{entity}",
            headers={"Authorization": "Bearer " + self.token},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())

    def get_num(self, entity: str):
        """Numeric HA state or None (unknown/unavailable/missing)."""
        if not entity:
            return None
        try:
            return float(self._state(entity)["state"])
        except Exception:
            return None

    def get_state(self, entity: str):
        """Raw HA state string or None."""
        if not entity:
            return None
        try:
            return self._state(entity)["state"]
        except Exception:
            return None

    def get_value_age(self, entity: str):
        """(value, last_updated_epoch) for an HA entity, or (None, None)."""
        if not entity:
            return None, None
        try:
            s = self._state(entity)
            v = float(s["state"])
            ts = datetime.fromisoformat(s["last_updated"]).timestamp()
            return v, ts
        except Exception:
            return None, None

    def snapshot(self) -> dict:
        cur = self._state(self.price_entity)
        try:
            price = float(cur["state"])
        except (ValueError, TypeError):
            price = None
        fc = []
        try:
            raw = self._state(self.forecast_entity)["attributes"].get("forecasts", [])
            for p in raw:
                fc.append({"t": p.get("nem_date"), "price": p.get("per_kwh"),
                           "spot": p.get("spot_per_kwh"), "descriptor": p.get("descriptor")})
        except Exception:
            pass
        aemo_price, aemo_fc = None, []
        if self.aemo_forecast_entity:
            try:
                a = self._state(self.aemo_forecast_entity)
                try:
                    aemo_price = float(a["state"])
                except (ValueError, TypeError):
                    aemo_price = None
                for p in a["attributes"].get("forecast", []):
                    aemo_fc.append({"t": p.get("start_time"), "price": p.get("price")})
            except Exception:
                pass
        feedin = None
        if self.feedin_entity:
            try:
                feedin = float(self._state(self.feedin_entity)["state"])
            except Exception:
                feedin = None  # entity not present yet (no solar/feed-in channel)
        return {
            "price": price,
            "descriptor": cur["attributes"].get("descriptor") if isinstance(cur.get("attributes"), dict) else None,
            "forecast": fc,
            "aemo_price": aemo_price,
            "aemo_forecast": aemo_fc,
            "feedin": feedin,
        }


# ---------------------------------------------------------------- engine -----

def _parse_t(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def classify_band(price, bands) -> str:
    """Map a $/kWh price to a named band. Negative prices land in the first band."""
    if price is None:
        return "unknown"
    for b in bands:
        upto = b.get("upto")
        if upto is None or price < upto:
            return b["name"]
    return bands[-1]["name"] if bands else "unknown"


def decide(prices: dict, soc: float, pv_kw: float, work_mode: str, strat: dict,
           currently_charging: bool = False, load_kw: float = 0.0,
           demand_window: bool = False) -> dict:
    """Transparent rules with charge hysteresis. Returns recommendation (no side effects)."""
    price = prices.get("price")
    fc = prices.get("forecast") or []
    aemo_fc = prices.get("aemo_forecast") or []
    cheap, exp = strat["cheap_price"], strat["expensive_price"]
    start_p = strat.get("charge_start_price", cheap)   # begin charging at/below this
    stop_p = start_p + strat.get("charge_stop_margin", 0.05)  # keep charging until price > start + margin
    aemo_exp, aemo_cheap = strat.get("aemo_expensive", 0.20), strat.get("aemo_cheap", 0.05)
    reserve = strat["reserve_soc"]
    # FOUNDATION: hard SoC cap — never charge above max_soc, whatever the dynamic layer asks.
    max_soc = strat.get("max_soc", 90)
    target = min(strat["target_soc"], max_soc)
    ceiling = strat.get("price_ceiling", 0.20)   # FOUNDATION: never grid-charge above this $
    now = datetime.now(timezone.utc)
    look_h = strat.get("precharge_lookahead_h", 3)

    band = classify_band(price, strat.get("bands", []))
    charge_bands = strat.get("charge_bands", [])
    avoid_bands = strat.get("avoid_bands", [])

    reasons = []
    action = "HOLD"
    target_mode = work_mode or "SelfUse"
    force_charge = False
    force_discharge = False
    feedin = prices.get("feedin")
    sell_p = strat.get("sell_price", 0.50)            # auto-sell when feed-in ≥ this ("silly" high)
    sell_enabled = strat.get("sell_enabled", True)
    sell_floor = strat.get("sell_floor_soc", reserve)  # survival SoC floor (computed in gather)

    def within(p, hours, thresh, cmp_ge=True):
        t = _parse_t(p.get("t") or "")
        if not t:
            return False
        dt = (t - now).total_seconds()
        if not (0 <= dt <= hours * 3600):
            return False
        v = p.get("price")
        if v is None:
            return False
        return v >= thresh if cmp_ge else v <= thresh

    # Peak coming soon? Confirmed by EITHER Amber retail OR AEMO wholesale forecast.
    amber_peak = any(within(p, look_h, exp) for p in fc)
    aemo_peak = any(within(p, look_h, aemo_exp) for p in aemo_fc)
    peak_soon = amber_peak or aemo_peak
    # Cheaper trough imminent (next 1h)? If so, don't grid-charge now at a mediocre price.
    aemo_trough_soon = any(within(p, 1, aemo_cheap, cmp_ge=False) for p in aemo_fc)
    peak_src = "Amber" if amber_peak else ("AEMO" if aemo_peak else None)

    # Solar awareness: if PV is producing more than the house is using, that surplus is
    # charging the battery for free — so don't pay the grid to do the same (and don't
    # curtail the panels). Ludicrous (negative) prices are handled before this and still charge.
    solar_surplus = round(pv_kw - load_kw, 2)
    solar_defer = strat.get("solar_defer_kw", 0.5)
    # ...BUT only defer to solar if the day's energy balance actually covers the rest of the day.
    # If we project a shortfall (usable battery + remaining solar < remaining load), don't skip a
    # cheap grid-charge just because there's a momentary PV surplus.
    shortfall = strat.get("energy_shortfall_kwh", 0.0)
    short_margin = strat.get("solar_defer_shortfall_margin", 1.5)
    solar_covering = (solar_surplus >= solar_defer) and (shortfall <= short_margin)

    # Horizon view: cheapest / most-expensive price across the forward forecast window.
    horizon_h = strat.get("horizon_hours", 18)
    fwin = [p.get("price") for p in fc
            if _parse_t(p.get("t") or "") and 0 < (_parse_t(p["t"]) - now).total_seconds() <= horizon_h * 3600
            and p.get("price") is not None]
    min_future_h = round(min(fwin), 3) if fwin else None
    peak_future_h = round(max(fwin), 3) if fwin else None
    win_margin = strat.get("horizon_window_margin", 0.03)
    horizon_on = strat.get("horizon_charge", True)
    peak_coming = peak_future_h is not None and peak_future_h >= exp
    near_cheapest = (min_future_h is not None and price is not None and price <= min_future_h + win_margin)

    if price is None:
        reasons.append("No price available; defaulting to SelfUse.")
        action, target_mode = "SET_MODE", "SelfUse"
    elif (sell_enabled and feedin is not None and feedin >= sell_p
          and soc > sell_floor + 1 and not currently_charging and not solar_covering):
        action, force_discharge = "SELL", True
        reasons.append(f"Feed-in {feedin:.2f} ≥ sell {sell_p:.2f} (silly high) and SoC {soc:.0f}% > survival "
                       f"floor {sell_floor:.0f}% → SELL to grid down to {sell_floor:.0f}% (keeps overnight buffer).")
    elif soc >= target:
        reasons.append(f"SoC {soc:.0f}% ≥ target {target}% → battery full, no grid charge. SelfUse.")
        action, target_mode = "SET_MODE", "SelfUse"
    elif demand_window and strat.get("avoid_demand_window", True):
        reasons.append("Amber demand window active → avoid grid import (no force-charge). SelfUse.")
        action, target_mode = "SET_MODE", "SelfUse"
    elif band == "ludicrous":
        reasons.append(f"Price {price:.3f} LUDICROUS (paid to consume!) → force-charge to {target}%.")
        action, force_charge = "FORCE_CHARGE", True
    elif price <= start_p and solar_covering:
        reasons.append(f"Price {price:.3f} ≤ start {start_p:.3f}, but solar surplus {solar_surplus:.1f}kW "
                       f"is charging the battery → no grid charge needed. SelfUse.")
        action, target_mode = "SET_MODE", "SelfUse"
    elif price <= start_p:
        # "unless it's forecast for much lower soon" — defer to the cheaper trough.
        look = strat.get("defer_lookahead_h", 3)
        delta = strat.get("defer_if_cheaper_by", 0.04)
        future = [p.get("price") for p in fc
                  if _parse_t(p.get("t") or "") and 0 < (_parse_t(p["t"]) - now).total_seconds() <= look * 3600
                  and p.get("price") is not None]
        min_future = min(future) if future else None
        if (not currently_charging and min_future is not None
                and (price - min_future) >= delta and soc > reserve):
            reasons.append(f"Price {price:.3f} ≤ start {start_p:.3f}, but forecast dips to {min_future:.3f} "
                           f"within {look}h (≥{delta:.2f} cheaper) → wait for the trough. SelfUse.")
            action, target_mode = "SET_MODE", "SelfUse"
        else:
            reasons.append(f"Price {price:.3f} ≤ start {start_p:.3f} → start/continue force-charge to {target}%.")
            action, force_charge = "FORCE_CHARGE", True
    elif currently_charging and price < stop_p:
        reasons.append(f"Already charging and price {price:.3f} < stop {stop_p:.3f} (hysteresis) → keep charging.")
        action, force_charge = "FORCE_CHARGE", True
    elif horizon_on and peak_coming and near_cheapest and not solar_covering and soc < target:
        reasons.append(f"Forecast peak {peak_future_h:.2f} within {horizon_h}h and now {price:.3f} is near the cheapest "
                       f"pre-peak window (min {min_future_h:.3f}+{win_margin:.2f}) → pre-charge in the cheap window now.")
        action, force_charge = "FORCE_CHARGE", True
    elif peak_soon and not aemo_trough_soon and solar_covering:
        reasons.append(f"Peak within {look_h}h but solar surplus {solar_surplus:.1f}kW is charging → "
                       f"let the sun pre-charge. SelfUse.")
        action, target_mode = "SET_MODE", "SelfUse"
    elif peak_soon and not aemo_trough_soon:
        reasons.append(f"Expensive peak within {look_h}h (per {peak_src}) and SoC {soc:.0f}% < {target}% → pre-charge.")
        action, force_charge = "FORCE_CHARGE", True
    elif band in avoid_bands or price >= exp:
        reasons.append(f"Price {price:.3f} [band={band}] high → use battery, avoid grid import. SelfUse.")
        action, target_mode = "SET_MODE", "SelfUse"
    else:
        why = "price rose above stop threshold → cancel charge" if currently_charging else f"band={band}"
        reasons.append(f"Price {price:.3f} [{why}] → SelfUse.")
        action, target_mode = "SET_MODE", "SelfUse"

    # FOUNDATION guardrail: never grid-charge above the absolute price ceiling (ludicrous/negative
    # is free money and exempt). Vetoes anything the dynamic layer or a branch above proposed.
    if force_charge and band != "ludicrous" and price is not None and price > ceiling:
        force_charge = False
        action, target_mode = "SET_MODE", "SelfUse"
        reasons.append(f"FOUNDATION: price {price:.3f} > ceiling {ceiling:.3f} → refuse grid-charge. SelfUse.")

    if soc <= reserve:
        reasons.append(f"SoC {soc:.0f}% at/below reserve {reserve}%.")

    rec = {
        "action": action,
        "target_mode": target_mode,
        "force_charge": force_charge,
        "force_discharge": force_discharge,
        "sell_floor": sell_floor,
        "band": band,
        "min_future_h": min_future_h,
        "peak_future_h": peak_future_h,
        "reason": " ".join(reasons),
    }
    if force_charge:
        rec["force_charge_plan"] = {
            "window": "now → next cheap-end (capped 1h, re-evaluated each cycle)",
            "max_soc": target, "min_soc_on_grid": strat["min_soc_on_grid"],
            "power_kw": strat["force_charge_power_kw"],
        }
    return rec


# ---------------------------------------------------------------- runtime ----

LAST: dict = {}
LAST_LOCK = Lock()
_WM = {"value": None, "options": None, "i": 0}  # work-mode cache (refresh every Nth cycle)
_LLM = {"last_ts": 0.0, "last_fc": False, "last": None}  # LLM review state + cached verdict
_CHARGE = {"until": 0.0}  # epoch until which WE intend to force-charge (survives a flaky scheduler read)
_TELE = {"last": None, "ts": None}  # last good FoxESS telemetry (foxctl is the sole poller)
_MQTT = {"client": None, "disc": False}
_ENERGY = {"totals": {}, "last_ts": 0.0, "loaded": False}  # cumulative kWh per channel (total_increasing)


def update_energy(cfg, powers):
    """Integrate power channels (kW) into cumulative kWh counters (total_increasing) for the HA Energy
    dashboard. Persisted to /data so they survive restarts (monotonic)."""
    if not _ENERGY["loaded"]:
        try:
            d = json.loads((_state_dir(cfg) / "energy.json").read_text())
            _ENERGY["totals"], _ENERGY["last_ts"] = d.get("totals", {}), d.get("last_ts", 0.0)
        except Exception:
            pass
        _ENERGY["loaded"] = True
    now = time.time()
    dt_h = (now - _ENERGY["last_ts"]) / 3600.0 if _ENERGY["last_ts"] else 0.0
    if 0 < dt_h <= 1.0:
        for ch, kw in powers.items():
            _ENERGY["totals"][ch] = round(_ENERGY["totals"].get(ch, 0.0) + max(0.0, kw) * dt_h, 4)
    _ENERGY["last_ts"] = now
    try:
        p = _state_dir(cfg) / "energy.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"totals": _ENERGY["totals"], "last_ts": now}))
    except Exception as e:
        print(f"energy persist failed: {e}", file=sys.stderr)
    return _ENERGY["totals"]
_CONS = {"days": {}, "last_ts": 0.0, "loaded": False, "path": None}  # rolling daily consumption (kWh)
_NOTE = {"text": None}  # free-text operator steering note fed to the LLM
# Overrides: floor = a persisted base charge-floor override (None = use config); manual = a temporary
# forced action {mode: 'charge'|'sell', until: epoch, power, min_soc, cap} that the loop enforces.
_OV = {"floor": None, "sell": None, "manual": None, "loaded": False}


def _state_dir(cfg):
    return Path(cfg.get("state_dir") or str(Path.home() / "foxctl"))


def load_ov(cfg):
    if not _OV["loaded"]:
        try:
            d = json.loads((_state_dir(cfg) / "overrides.json").read_text())
            _OV["floor"], _OV["sell"], _OV["manual"] = d.get("floor"), d.get("sell"), d.get("manual")
        except Exception:
            pass
        _OV["loaded"] = True


def save_ov(cfg):
    try:
        p = _state_dir(cfg) / "overrides.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"floor": _OV["floor"], "sell": _OV["sell"], "manual": _OV["manual"]}))
    except Exception as e:
        print(f"overrides persist failed: {e}", file=sys.stderr)


def set_baseline(cfg, floor, sell, ceiling):
    """Permanently set the buy floor and/or sell threshold (persisted overrides)."""
    load_ov(cfg)
    if floor is not None:
        _OV["floor"] = round(max(0.0, min(float(floor), ceiling)), 3)
    if sell is not None:
        _OV["sell"] = round(max(0.0, float(sell)), 3)
    save_ov(cfg)
    _LLM["last_ts"] = 0.0; _LLM["last"] = None
    log_event("override", f"baseline set: buy floor={_OV['floor']} sell={_OV['sell']}")
    return {"floor": _OV["floor"], "sell": _OV["sell"]}


def set_floor_override(cfg, floor, ceiling):
    load_ov(cfg)
    _OV["floor"] = None if floor is None else round(max(0.0, min(float(floor), ceiling)), 3)
    save_ov(cfg)
    _LLM["last_ts"] = 0.0; _LLM["last"] = None
    log_event("override", f"charge floor → {_OV['floor']}")
    return _OV["floor"]


def set_manual(cfg, mode, hours, power_kw, min_soc, cap=None):
    load_ov(cfg)
    if mode is None:
        _OV["manual"] = None
    else:
        _OV["manual"] = {"mode": mode, "until": time.time() + hours * 3600,
                         "power": power_kw, "min_soc": int(min_soc),
                         "cap": int(cap) if cap is not None else None}
    save_ov(cfg)
    log_event("override", f"manual {mode or 'cancel'}" + (f" {hours}h" if mode else ""))
    return _OV["manual"]


def get_note(cfg):
    if _NOTE["text"] is None:
        try:
            _NOTE["text"] = (_state_dir(cfg) / "operator_note.txt").read_text().strip()
        except Exception:
            _NOTE["text"] = ""
    return _NOTE["text"]


def set_note(cfg, text):
    text = (text or "").strip()[:1000]
    _NOTE["text"] = text
    try:
        p = _state_dir(cfg) / "operator_note.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    except Exception as e:
        print(f"note persist failed: {e}", file=sys.stderr)
    _LLM["last_ts"] = 0.0      # force a fresh LLM review so the note takes effect immediately
    _LLM["last"] = None
    log_event("note", f"operator note set: {text[:160]}" if text else "operator note cleared")
    return text


def _cons_path(cfg):
    d = cfg.get("state_dir") or str(Path.home() / "foxctl")
    return Path(d) / "consumption.json"


def update_consumption(cfg, load_kw, ev_kw=None):
    """Integrate house load (and optional EV-plug load) into per-day kWh buckets, persisted across
    restarts. Returns a rolling-average summary the dynamic policy uses instead of a static guess.
    EV is tracked separately so an occasional car charge doesn't distort the predictable base load."""
    if not _CONS["loaded"]:
        _CONS["path"] = _cons_path(cfg)
        try:
            _CONS.update(json.loads(_CONS["path"].read_text())); _CONS["loaded"] = True
        except Exception:
            _CONS["loaded"] = True
    now = time.time()
    last = _CONS["last_ts"]
    dt_h = (now - last) / 3600.0 if last else 0.0
    base_kw = max(0.0, load_kw - (ev_kw or 0.0))   # base load = house minus the EV charger
    if 0 < dt_h <= 1.0:   # skip the first sample and any long gap (restart/downtime) to avoid spikes
        day = datetime.now().strftime("%Y-%m-%d")
        rec = _CONS["days"].setdefault(day, {"load": 0.0, "ev": 0.0, "hours": {}})
        rec.setdefault("hours", {})
        rec["load"] += max(0.0, load_kw) * dt_h
        rec["ev"] += max(0.0, ev_kw or 0.0) * dt_h
        hk = str(datetime.now().hour)
        rec["hours"][hk] = round(rec["hours"].get(hk, 0.0) + base_kw * dt_h, 4)   # base load by hour-of-day
    _CONS["last_ts"] = now
    for k in sorted(_CONS["days"])[:-15]:   # keep last ~15 days (for a fuller hourly profile)
        _CONS["days"].pop(k, None)
    try:
        _CONS["path"].parent.mkdir(parents=True, exist_ok=True)
        _CONS["path"].write_text(json.dumps({k: _CONS[k] for k in ("days", "last_ts")}))
    except Exception as e:
        print(f"consumption persist failed: {e}", file=sys.stderr)
    today = datetime.now().strftime("%Y-%m-%d")
    past = [v for k, v in _CONS["days"].items() if k != today]
    n = len(past)
    avg_total = round(sum(p["load"] for p in past) / n, 1) if n else None
    avg_ev = round(sum(p["ev"] for p in past) / n, 1) if n else None
    avg_base = round(avg_total - avg_ev, 1) if avg_total is not None else None
    # Hour-of-day base-load profile: avg kWh per hour across complete past days.
    hour_profile, hp_days = {}, [p for p in past if p.get("hours")]
    if hp_days:
        for h in range(24):
            vals = [p["hours"].get(str(h), 0.0) for p in hp_days]
            hour_profile[h] = round(sum(vals) / len(vals), 3)
    tk = _CONS["days"].get(today, {})
    return {"days_sampled": n, "avg_daily_total_kwh": avg_total, "avg_daily_ev_kwh": avg_ev,
            "avg_daily_base_kwh": avg_base, "today_so_far_kwh": round(tk.get("load", 0.0), 1),
            "hour_profile": hour_profile, "profile_days": len(hp_days)}


def predict_base_load(hour_profile, hours_ahead):
    """Sum the hour-of-day base-load profile over the next `hours_ahead` hours (prorated for the
    partial current hour). Falls back to None if there's no profile yet."""
    if not hour_profile:
        return None
    now = datetime.now()
    total = 0.0
    remaining = float(hours_ahead)
    frac = 1.0 - now.minute / 60.0          # remaining fraction of the current hour
    h = now.hour
    while remaining > 0:
        take = min(frac, remaining)
        total += hour_profile.get(h % 24, hour_profile.get(str(h % 24), 0.0)) * take
        remaining -= take
        h += 1; frac = 1.0
    return round(total, 1)
LOG_PATH = Path(os.environ.get("FOXCTL_LOG", Path.home() / "foxctl/decisions.jsonl"))


# ---- DYNAMIC POLICY: the LLM tunes two knobs within the FOUNDATION guardrails ----
# The foundation (deterministic, in decide()) is the part you must override yourself: an absolute
# price ceiling, the SoC floor/cap, the stale-telemetry hold, and "cheapest point only". The LLM
# only nudges charge_start_price (0..ceiling) and target_soc (reserve+10..max_soc) each interval,
# then deterministic code clamps and executes. It can optimise; it can't break the guardrails.

# Site facts the model must reason with (NSW). Update these if the meter/plan changes.
SITE_FACTS = {
    "network": "Essential Energy EA116 ~flat ~2c/kWh network (NO kW demand charge) — "
               "the Amber 'demand window' is therefore NOT a cost risk; charge in it when cheap.",
    "feed_in": "Feed-in/export IS now enabled — surplus solar (or battery) exported to the grid earns the "
               "Amber feed-in price (see feedin_price in context). So solar is no longer wasted: store it "
               "when it offsets a more expensive import later, but exporting is also a valid use. Do NOT "
               "grid-charge the battery just to export — that loses money (import price > feed-in).",
    "battery": "Large battery (~60+ kWh usable). Plan charging by ENERGY BALANCE, not just price: compare "
               "stored energy (residual_kwh) + expected solar (solar_forecast_kwh) against expected "
               "household usage to the next solar window, and import enough cheap energy to bridge the gap.",
    "season_note": "Winter / low generation right now: little solar to capture, so topping the battery up "
                   "from the grid at genuinely cheap prices to bridge to a peak is reasonable. In "
                   "summer/high-solar, keep charge_start_price low and leave headroom for free solar.",
}

GOAL = ("Goal priority: (1) MINIMISE COST — prefer $0 import; only grid-charge when it avoids a more "
        "expensive import later (energy balance: residual + solar forecast vs expected usage to the next "
        "solar window). (2) If import is needed, buy at the CHEAPEST forecast point, not a mediocre price "
        "now. (3) Feed-in is enabled — surplus solar can be exported for the feed-in price, so weigh "
        "store-vs-export, but never import-to-export. A charge_start_floor sets the minimum price the "
        "operator is always willing to charge at; you may set charge_start_price higher (up to the "
        "ceiling) when the energy balance justifies it, but the floor will be applied either way.")


def _forecast_digest(fc, hours=18, step_min=30):
    """Condense a fine-grained forecast into a compact forward view for the LLM:
    a ~30-min-spaced series over `hours` plus the cheapest/most-expensive points + their times."""
    now = datetime.now(timezone.utc)
    pts = []
    for p in fc:
        t = _parse_t(p.get("t") or "")
        if t and p.get("price") is not None:
            dt = (t - now).total_seconds()
            if 0 <= dt <= hours * 3600:
                pts.append((dt, t, p["price"]))
    if not pts:
        return {"series": [], "cheapest": None, "most_expensive": None}
    mn = min(pts, key=lambda x: x[2])
    mx = max(pts, key=lambda x: x[2])
    series, last = [], -1e9
    for dt, t, pr in pts:
        if dt - last >= step_min * 60 - 1:
            series.append({"t": t.strftime("%H:%M"), "p": round(pr, 3)})
            last = dt
    lbl = lambda x: {"t": x[1].strftime("%H:%M"), "p": round(x[2], 3)}
    return {"series": series, "cheapest": lbl(mn), "most_expensive": lbl(mx)}


def _solar_bells(rise_iso, set_iso, kwh_tomorrow, kwh_remaining):
    """Approximate solar power as a half-sine over daylight, scaled so the area = forecast kWh.
    Returns bells (hours-from-now) for the chart: a 'sunny times / high solar' overlay."""
    now = datetime.now(timezone.utc)
    R = _parse_t(rise_iso) if rise_iso else None
    S = _parse_t(set_iso) if set_iso else None
    bells = []

    def mk(start, end, kwh):
        if not (start and end) or end <= start or not kwh:
            return None
        daylen = (end - start).total_seconds() / 3600.0
        pmax = kwh * math.pi / (2 * daylen) if daylen > 0 else 0
        return {"s": (start - now).total_seconds() / 3600.0,
                "e": (end - now).total_seconds() / 3600.0,
                "pmax": round(pmax, 2), "kwh": round(kwh, 1)}
    if R and S:
        if R < S:   # nighttime now → next daylight [R,S] is tomorrow
            b = mk(R, S, kwh_tomorrow)
            if b: bells.append(b)
        else:       # daytime now → rest of today (to S) + tomorrow (from R, ~10h day)
            b = mk(S - timedelta(hours=10), S, kwh_remaining)
            if b: bells.append(b)
            b = mk(R, R + timedelta(hours=10), kwh_tomorrow)
            if b: bells.append(b)
    return bells


def _extract_json(text):
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e <= s:
        raise ValueError("no JSON object in reply")
    return json.loads(text[s:e + 1])


def _llm_dynamic(api_key, model, ctx):
    """Ask the LLM for the two dynamic knobs. Returns {params, rating, text, ts, model}."""
    system = ("You are the DYNAMIC POLICY layer of an automated home solar-battery controller in the "
              "Australian NEM (NSW). " + GOAL + " You set two knobs the deterministic controller will "
              "use: charge_start_price ($/kWh at/below which it grid-charges) and target_soc (%). Stay "
              "within the foundation guardrails in the context (your values are hard-clamped anyway). "
              "Default charge_start_price near 0 unless the forecast/SoC/solar genuinely justify importing. "
              "If operator_note is present, it is a DIRECT instruction from the human operator — follow it as "
              "a priority within the guardrails (ceiling + SoC limits; the charge floor is relaxed while a "
              "note is active). E.g. a note to 'let the battery discharge until the 9c midday trough' means "
              "set charge_start_price low (~0.09) and don't charge until then. "
              "Reply with ONLY a JSON object: "
              '{"charge_start_price": <num>, "target_soc": <int>, '
              '"rating": "AGREE"|"REFINE"|"DISAGREE", "reason": "<=2 sentences", '
              '"operator_action": "<short suggestion that REQUIRES the human operator — e.g. change a '
              'foundation setting they control (price_ceiling, charge_start_floor, max_soc, '
              'battery_capacity_kwh) — or empty string if nothing needs them>", '
              '"base_floor": <number or null — set ONLY when the operator_note clearly asks for a LASTING '
              'change to the minimum price always willing to charge at (the base floor); else null>}. '
              "Only fill operator_action when you genuinely want a human to change something you cannot; "
              "leave it empty for normal auto-applied tuning. rating: AGREE=same as baseline, REFINE=minor "
              "auto tweak, DISAGREE=you think the policy/state is wrong.")
    user = "Context (JSON):\n" + json.dumps(ctx)
    body = json.dumps({"model": model, "max_tokens": 320, "system": system,
                       "messages": [{"role": "user", "content": user}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode())
    text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text").strip()
    obj = _extract_json(text)
    rating = str(obj.get("rating", "")).upper()
    rating = rating if rating in ("AGREE", "REFINE", "DISAGREE") else "REFINE"
    params = {}
    if isinstance(obj.get("charge_start_price"), (int, float)):
        params["charge_start_price"] = float(obj["charge_start_price"])
    if isinstance(obj.get("target_soc"), (int, float)):
        params["target_soc"] = int(obj["target_soc"])
    action = str(obj.get("operator_action", "") or "").strip()
    bf = obj.get("base_floor")
    return {"params": params, "rating": rating, "agree": rating == "AGREE",
            "text": obj.get("reason", text)[:400], "operator_action": action[:300],
            "base_floor": float(bf) if isinstance(bf, (int, float)) else None,
            "ts": datetime.now().isoformat(timespec="seconds"), "model": model}


def apply_dynamic_params(strat, params, foundation):
    """Merge the LLM's knobs into a copy of strat, hard-clamped to the foundation guardrails.
    charge_start_price is floored at charge_start_floor (always willing to charge that cheap) and
    capped at the price_ceiling: effective = clamp(max(LLM, floor), 0, ceiling)."""
    out = dict(strat)
    # A live operator note relaxes the charge floor so guidance like "wait for 9c" can take effect
    # (still capped by the ceiling and SoC limits).
    floor = 0.0 if foundation.get("note_active") else float(foundation.get("charge_start_floor", 0.0))
    ceiling = foundation["price_ceiling"]
    csp = params.get("charge_start_price")
    base = float(csp) if isinstance(csp, (int, float)) else floor
    out["charge_start_price"] = round(max(0.0, min(max(base, floor), ceiling)), 3)
    ts = params.get("target_soc")
    if isinstance(ts, (int, float)):
        lo, hi = strat.get("reserve_soc", 20) + 10, foundation["max_soc"]
        out["target_soc"] = int(max(lo, min(int(ts), hi)))
    return out


def maybe_llm_review(cfg, ctx, force=False):
    """Run the dynamic-policy LLM, gated by interval (+ force) to keep cost trivial; cache + reuse the
    knobs between cycles. ctx is the decision context (state + forecasts + foundation). Returns the
    cached plan dict or None when disabled."""
    llm = cfg.get("llm", {})
    if not llm.get("enabled") or not llm.get("api_key"):
        return None
    now = time.time()
    due = (now - _LLM["last_ts"]) >= llm.get("interval_min", 30) * 60
    if not (force or due or _LLM["last"] is None):
        return _LLM["last"]
    _LLM["last_ts"] = now
    try:
        v = _llm_dynamic(llm["api_key"], llm.get("model", "claude-haiku-4-5-20251001"), ctx)
        log_event("llm", f'{v["rating"]} csp={v["params"].get("charge_start_price")} '
                         f'target={v["params"].get("target_soc")}: {v["text"][:160]}')
        # A forceful note can lastingly change the base floor — apply it (bounded, logged).
        bf = v.get("base_floor")
        ceil = cfg.get("strategy", {}).get("price_ceiling", 0.20)
        if bf is not None:
            load_ov(cfg)
            new = round(max(0.0, min(float(bf), ceil)), 3)
            if new != _OV["floor"]:
                _OV["floor"] = new; save_ov(cfg)
                log_event("override", f"base charge floor → {new} (from operator note via LLM)")
    except Exception as e:
        v = {"params": {}, "agree": None, "rating": None,
             "text": f"LLM dynamic-policy unavailable: {e}",
             "ts": datetime.now().isoformat(timespec="seconds")}
    _LLM["last"] = v
    return v


_NOTIFY = {"last_band": None, "last_llm_ts": None, "last_stale": False,
           "last_action": None, "last_action_ts": 0.0, "last_selling": False}


def ha_notify(cfg, title, message):
    try:
        url = cfg["ha"]["url"].rstrip("/")
        token = Path(os.path.expanduser(cfg["ha"]["token_file"])).read_text().strip()
        svc = cfg.get("notify", {}).get("service", "notify.mobile_app_phoney")
        name = svc.split(".", 1)[1] if "." in svc else svc
        body = json.dumps({"title": title, "message": message}).encode()
        req = urllib.request.Request(f"{url}/api/services/notify/{name}", data=body, method="POST",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
        log_event("notify", f"{title}: {message[:120]}")
    except Exception as e:
        print(f"notify failed: {e}", file=sys.stderr)


def maybe_notify(cfg, snap):
    """Ping the phone when a decision is worth a human look. Edge-triggered, de-duped."""
    nc = cfg.get("notify", {})
    if not nc.get("enabled"):
        return
    rec = snap.get("recommendation", {})
    band = rec.get("band")
    llm = snap.get("llm") or {}
    out = []
    if nc.get("on_spike", True) and band == "spike" and _NOTIFY["last_band"] != "spike":
        out.append(("⚡ Price spike", f"Amber ${snap.get('price')}/kWh — consider cutting usage / lean on battery."))
    if nc.get("on_ludicrous", True) and band == "ludicrous" and _NOTIFY["last_band"] != "ludicrous":
        out.append(("💸 Negative price!", f"Amber ${snap.get('price')}/kWh — great time to charge the car / run appliances."))
    if nc.get("on_llm_disagree", True) and llm.get("rating") == "DISAGREE" and llm.get("ts") != _NOTIFY["last_llm_ts"]:
        _NOTIFY["last_llm_ts"] = llm.get("ts")
        out.append(("🤖 foxctl review", "Claude disagrees with the plan: " + llm.get("text", "")[:150]))
    # "Something for YOU to do": the LLM raised an operator action (e.g. change a foundation setting).
    # Notify when it's a NEW suggestion, rate-limited so the same one doesn't nag.
    action = (llm.get("operator_action") or "").strip()
    gap = nc.get("min_gap_min", 180) * 60
    if (nc.get("on_llm_action", True) and action and action != _NOTIFY["last_action"]
            and (time.time() - _NOTIFY["last_action_ts"]) >= gap):
        _NOTIFY["last_action"] = action
        _NOTIFY["last_action_ts"] = time.time()
        out.append(("🤖 foxctl — review suggestion", action[:200]))
    selling = bool(rec.get("force_discharge"))
    if nc.get("on_sell", True) and selling and not _NOTIFY["last_selling"]:
        out.append(("💰 foxctl auto-selling",
                    f"Feed-in ${snap.get('feedin')}/kWh is silly high — exporting battery to grid down to "
                    f"{rec.get('sell_floor')}% (overnight buffer kept)."))
    _NOTIFY["last_selling"] = selling
    stale = "stale" in (snap.get("telemetry_source") or "") or "down" in (snap.get("telemetry_source") or "")
    if nc.get("on_stale", True) and stale and not _NOTIFY["last_stale"]:
        out.append(("⚠️ foxctl telemetry stale",
                    "HA sensors frozen and FoxESS fallback failed — control on safety hold until data recovers."))
    _NOTIFY["last_stale"] = stale
    _NOTIFY["last_band"] = band
    for t, m in out:
        ha_notify(cfg, t, m)


_BAND_STATE = {"band": None}   # for edge-triggered actions


def append_log(snap: dict):
    """Append a compact decision record (JSONL) for later observation."""
    rec = snap.get("recommendation", {})
    row = {
        "ts": snap.get("ts"), "price": snap.get("price"), "aemo_price": snap.get("aemo_price"),
        "band": rec.get("band"), "soc": snap.get("soc"), "pv_kw": snap.get("pv_kw"),
        "work_mode": snap.get("work_mode"), "action": rec.get("action"),
        "target_mode": rec.get("target_mode"), "force_charge": rec.get("force_charge"),
        "applied": snap.get("applied"), "reason": rec.get("reason"),
    }
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as e:
        print(f"log write failed: {e}", file=sys.stderr)


ACTION_LOG = Path(os.environ.get("FOXCTL_ACTIONLOG", Path.home() / "foxctl/actions.log"))
EVENTS_PATH = Path(os.environ.get("FOXCTL_EVENTS", Path.home() / "foxctl/events.jsonl"))


def log_action(msg: str):
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, file=sys.stderr)
    try:
        ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(ACTION_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def log_event(kind: str, detail: str, extra: dict | None = None):
    """Structured timeline of real things that happened: applies, force-charge,
    disables, band-triggered actions. Shown on the dashboard."""
    row = {"ts": datetime.now().isoformat(timespec="seconds"), "kind": kind, "detail": detail}
    if extra:
        row.update(extra)
    try:
        EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(EVENTS_PATH, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as e:
        print(f"event log failed: {e}", file=sys.stderr)


def read_events(n: int = 50) -> list:
    if not EVENTS_PATH.exists():
        return []
    out = []
    for ln in EVENTS_PATH.read_text().splitlines()[-n:]:
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out


def read_log(n: int = 50) -> list:
    if not LOG_PATH.exists():
        return []
    lines = LOG_PATH.read_text().splitlines()[-n:]
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out


def run_band_actions(cfg: dict, snap: dict):
    """Run configured shell commands when ENTERING a new band (edge-triggered)."""
    band = snap.get("recommendation", {}).get("band")
    if band == _BAND_STATE["band"]:
        return
    prev = _BAND_STATE["band"]
    _BAND_STATE["band"] = band
    if prev is None:
        return  # don't fire on first observation after start
    cmds = (cfg.get("actions") or {}).get(band, [])
    if not cmds:
        return
    if not cfg["control"].get("allow_actions"):
        snap.setdefault("notes", []).append(f"band→{band}: {len(cmds)} action(s) skipped (allow_actions=false)")
        return
    import subprocess
    for c in cmds:
        try:
            subprocess.Popen(c, shell=True)
            snap.setdefault("notes", []).append(f"band→{band}: ran {c!r}")
            log_event("action", f"band→{band}: ran {c}", {"band": band})
        except Exception as e:
            snap.setdefault("notes", []).append(f"band→{band}: FAILED {c!r}: {e}")
            log_event("action", f"band→{band}: FAILED {c}: {e}", {"band": band})


# Telemetry sensors foxctl publishes to MQTT discovery (object_id, friendly, unit, device_class).
MQTT_DISCOVERY = "homeassistant"
# (object_id, friendly, unit, device_class, state_class). state_class total_increasing → Energy dashboard.
_MQTT_SENSORS = [
    ("foxctl_soc", "Battery SoC", "%", "battery", "measurement"),
    ("foxctl_pv_power", "Solar power", "kW", "power", "measurement"),
    ("foxctl_load_power", "House load", "kW", "power", "measurement"),
    ("foxctl_grid_power", "Grid import", "kW", "power", "measurement"),
    ("foxctl_feedin_power", "Grid export", "kW", "power", "measurement"),
    ("foxctl_battery_power", "Battery power", "kW", "power", "measurement"),
    ("foxctl_battery_charge_power", "Battery charge power", "kW", "power", "measurement"),
    ("foxctl_battery_discharge_power", "Battery discharge power", "kW", "power", "measurement"),
    ("foxctl_pv1_power", "PV string 1", "kW", "power", "measurement"),
    ("foxctl_pv2_power", "PV string 2", "kW", "power", "measurement"),
    ("foxctl_pv3_power", "PV string 3", "kW", "power", "measurement"),
    ("foxctl_pv4_power", "PV string 4", "kW", "power", "measurement"),
    ("foxctl_pv5_power", "PV string 5", "kW", "power", "measurement"),
    ("foxctl_pv6_power", "PV string 6", "kW", "power", "measurement"),
    ("foxctl_grid_import_energy", "Grid import energy", "kWh", "energy", "total_increasing"),
    ("foxctl_grid_export_energy", "Grid export energy", "kWh", "energy", "total_increasing"),
    ("foxctl_battery_charge_energy", "Battery charge energy", "kWh", "energy", "total_increasing"),
    ("foxctl_battery_discharge_energy", "Battery discharge energy", "kWh", "energy", "total_increasing"),
    ("foxctl_solar_energy", "Solar energy", "kWh", "energy", "total_increasing"),
    ("foxctl_charge_start", "Charge-start price", "$/kWh", None, None),
    ("foxctl_target_soc", "Target SoC", "%", None, None),
]


def mqtt_publish(cfg, snap):
    """Publish FoxESS telemetry + foxctl status to MQTT discovery so HA gets sensor.foxctl_* — the
    dashboards then read these instead of the flaky foxess-ha integration. Best-effort/non-fatal."""
    mc = cfg.get("mqtt") or {}
    if not mc.get("publish"):
        return
    try:
        import paho.mqtt.client as mqtt
    except Exception:
        return
    try:
        cli = _MQTT["client"]
        if cli is None:
            try:
                cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="foxctl")
            except (AttributeError, TypeError):
                cli = mqtt.Client(client_id="foxctl")
            if mc.get("user"):
                cli.username_pw_set(mc["user"], mc.get("pass", ""))
            cli.will_set("foxctl/availability", "offline", qos=1, retain=True)
            cli.connect(mc.get("host", "core-mosquitto"), int(mc.get("port", 1883)), 60)
            cli.loop_start()
            _MQTT["client"] = cli
        if not _MQTT["disc"]:
            dev = {"identifiers": ["foxctl_foxess"], "name": "FoxESS (foxctl)",
                   "manufacturer": "FoxESS", "model": "foxctl single-poller"}
            for oid, name, unit, dclass, sclass in _MQTT_SENSORS:
                conf = {"name": name, "unique_id": oid, "object_id": oid,
                        "state_topic": "foxctl/telemetry",
                        "value_template": "{{ value_json.%s }}" % oid[len("foxctl_"):],
                        "unit_of_measurement": unit, "availability_topic": "foxctl/availability",
                        "device": dev}
                if dclass:
                    conf["device_class"] = dclass
                if sclass:
                    conf["state_class"] = sclass
                cli.publish(f"{MQTT_DISCOVERY}/sensor/{oid}/config", json.dumps(conf), qos=1, retain=True)
            cli.publish("foxctl/availability", "online", qos=1, retain=True)
            _MQTT["disc"] = True
            print(f"foxctl published MQTT discovery for {len(_MQTT_SENSORS)} sensors", file=sys.stderr)
        dyn = snap.get("dynamic") or {}
        et = snap.get("energy_totals") or {}
        ps = snap.get("pv_strings") or {}
        tele = {"soc": round(snap.get("soc", 0)), "pv_power": snap.get("pv_kw"),
                "load_power": snap.get("load_kw"), "grid_power": snap.get("grid_power"),
                "feedin_power": snap.get("feedin_power"), "battery_power": snap.get("battery_power"),
                "battery_charge_power": snap.get("bat_charge_power"),
                "battery_discharge_power": snap.get("bat_discharge_power"),
                "grid_import_energy": et.get("grid_import"), "grid_export_energy": et.get("grid_export"),
                "battery_charge_energy": et.get("battery_charge"),
                "battery_discharge_energy": et.get("battery_discharge"), "solar_energy": et.get("solar"),
                "charge_start": dyn.get("charge_start_price"), "target_soc": dyn.get("target_soc")}
        tele.update({k: round(v, 3) for k, v in ps.items()})
        cli.publish("foxctl/telemetry", json.dumps(tele), qos=0, retain=True)
    except Exception as e:
        print(f"mqtt publish failed: {e}", file=sys.stderr)


def decide_zerohero(soc, work_mode, strat, survival_soc):
    """GloBird ZeroHero time-of-use strategy (no price forecasting):
      • 11:00–14:00 free window  → grid-charge battery to full (FREE).
      • 18:00–21:00 evening peak → cover all load from battery (zero grid import → $1/day credit)
        and export surplus to grid (9c + Super Export) down to the overnight survival floor.
      • all other times          → run off battery, avoid grid import.
    Returns a rec dict in the same shape decide() produces."""
    z = strat.get("zerohero", {})
    fs, fe = z.get("free_start_h", 11), z.get("free_end_h", 14)
    es, ee = z.get("evening_start_h", 18), z.get("evening_end_h", 21)
    max_soc = strat.get("max_soc", 90)
    reserve = strat.get("reserve_soc", 20)
    nowl = datetime.now()
    h = nowl.hour + nowl.minute / 60.0
    in_free = fs <= h < fe
    in_eve = es <= h < ee
    action, target_mode, fc, fd = "SET_MODE", (work_mode or "SelfUse"), False, False
    reasons = []
    if in_free and soc < max_soc:
        action, fc = "FORCE_CHARGE", True
        reasons.append(f"ZeroHero FREE window {fs:02d}:00–{fe:02d}:00 → grid-charge to {max_soc}% (free).")
    elif in_free:
        reasons.append(f"ZeroHero free window, battery full ({soc:.0f}% ≥ {max_soc}%). SelfUse.")
    elif in_eve and soc > survival_soc + 1:
        action, fd = "SELL", True
        reasons.append(f"ZeroHero peak {es:02d}:00–{ee:02d}:00 → cover load (zero import = $1/day) + export "
                       f"surplus at 9c down to survival {survival_soc}%.")
    elif in_eve:
        reasons.append(f"ZeroHero peak {es:02d}:00–{ee:02d}:00 → hold; cover load from battery "
                       f"(zero grid import = $1/day). SelfUse.")
    elif soc <= reserve:
        reasons.append(f"ZeroHero off-window but SoC {soc:.0f}% ≤ reserve {reserve}% — battery low. SelfUse.")
    else:
        reasons.append("ZeroHero off-window → run off battery, avoid grid import until the free window. SelfUse.")
    rec = {"action": action, "target_mode": target_mode, "force_charge": fc, "force_discharge": fd,
           "sell_floor": survival_soc, "band": "zerohero", "min_future_h": None, "peak_future_h": None,
           "reason": " ".join(reasons)}
    if fc:
        rec["force_charge_plan"] = {"window": f"{fs:02d}:00–{fe:02d}:00 free", "max_soc": max_soc,
                                    "min_soc_on_grid": strat.get("min_soc_on_grid", 10),
                                    "power_kw": strat.get("force_charge_power_kw", 10.5)}
    return rec


def gather_and_decide(cfg: dict) -> dict:
    fox = FoxESS(cfg["foxess"]["token"], cfg["foxess"]["sn"])
    ha_token = Path(os.path.expanduser(cfg["ha"]["token_file"])).read_text().strip()
    ha = HAPrices(cfg["ha"]["url"], ha_token, cfg["ha"]["amber_price_entity"],
                  cfg["ha"]["amber_forecast_entity"], cfg["ha"].get("aemo_forecast_entity"),
                  cfg["ha"].get("amber_feedin_entity"))

    prices = ha.snapshot()
    # foxctl is the SINGLE FoxESS poller: telemetry comes straight from the FoxESS API each cycle
    # (one call), is published to MQTT for the dashboards, and on a fetch failure we reuse the last
    # good values (cached) and flag stale so control holds. No dependency on the foxess-ha integration.
    VARS = ["SoC", "pvPower", "loadsPower", "gridConsumptionPower", "feedinPower",
            "batChargePower", "batDischargePower",
            "pv1Power", "pv2Power", "pv3Power", "pv4Power", "pv5Power", "pv6Power"]
    real = {}; tsrc = "FoxESS"
    try:
        real = fox.real(VARS)
        _TELE["last"] = real
        _TELE["ts"] = time.time()
        soc_ts = _TELE["ts"]
    except Exception as e:
        print(f"FoxESS telemetry fetch failed: {e}", file=sys.stderr)
        real = _TELE.get("last") or {}
        soc_ts = _TELE.get("ts")
        tsrc = "FoxESS(stale)" if real else "FoxESS(down)"
    soc = float(real.get("SoC") or 0)
    pv = float(real.get("pvPower") or 0)
    load = float(real.get("loadsPower") or 0)
    grid_power = float(real.get("gridConsumptionPower") or 0)
    feedin_power = float(real.get("feedinPower") or 0)
    bat_charge_power = float(real.get("batChargePower") or 0)
    bat_discharge_power = float(real.get("batDischargePower") or 0)
    battery_power = round(bat_charge_power - bat_discharge_power, 3)
    pv_strings = {f"pv{i}_power": float(real.get(f"pv{i}Power") or 0) for i in range(1, 7)}
    # Cumulative energy counters (kWh, total_increasing) for the HA Energy dashboard.
    energy = update_energy(cfg, {"grid_import": grid_power, "grid_export": feedin_power,
                                 "battery_charge": bat_charge_power, "battery_discharge": bat_discharge_power,
                                 "solar": pv}) if tsrc == "FoxESS" else _ENERGY.get("totals", {})
    # work mode rarely changes externally — refresh it every Nth cycle, cache otherwise, to save API calls
    refresh = int(cfg.get("work_mode_refresh_cycles", 3))
    _WM["i"] += 1
    if _WM["value"] is None or _WM["i"] % refresh == 0:
        w = fox.work_mode()
        _WM["value"], _WM["options"] = w.get("value"), w.get("enumList")
    wm = {"value": _WM["value"], "enumList": _WM["options"]}
    sched = fox.scheduler_status()
    sched_active = bool(sched["enabled"] and sched["active"] and sched["active"]["mode"] == "ForceCharge")
    # Persistence: if WE started a force-charge whose window hasn't elapsed, treat as charging even if
    # this scheduler read came back flaky — so hysteresis doesn't drop a charge mid-window (see 11:22 bug).
    charging = sched_active or (time.time() < _CHARGE["until"])
    demand_window = (ha.get_state(cfg["ha"].get("demand_window_entity")) == "on")
    weather = ha.get_state(cfg["ha"].get("weather_entity", "weather.forecast_home"))

    # Forecast.Solar: sum the per-plane sensors into a single forward solar view (kWh).
    def _sum_ents(ids):
        tot, seen = 0.0, False
        for e in ids or []:
            v = ha.get_num(e)
            if v is not None:
                tot += v; seen = True
        return round(tot, 2) if seen else None
    solar_remaining = _sum_ents(cfg["ha"].get("solar_fc_remaining_entities"))
    solar_tomorrow = _sum_ents(cfg["ha"].get("solar_fc_tomorrow_entities"))
    solar_today_total = _sum_ents(cfg["ha"].get("solar_fc_today_entities"))   # full-day forecast (not leftover)
    try:
        sa = ha._state("sun.sun")["attributes"]
        sun_rise, sun_set = sa.get("next_rising"), sa.get("next_setting")
    except Exception:
        sun_rise = sun_set = None
    solar_bells = _solar_bells(sun_rise, sun_set, solar_tomorrow, solar_remaining)

    # Rolling household consumption (foxctl integrates load_power itself; EV plug tracked separately).
    ev_kw = ha.get_num(cfg["ha"].get("ev_power_entity")) if cfg["ha"].get("ev_power_entity") else None
    if ev_kw is not None and ev_kw > 100:   # entity is in W (Tuya plugs report watts) → kW
        ev_kw = ev_kw / 1000.0
    consumption = update_consumption(cfg, load, ev_kw)

    note = get_note(cfg)
    load_ov(cfg)
    strat = cfg["strategy"]
    floor_eff = _OV["floor"] if _OV["floor"] is not None else strat.get("charge_start_floor", 0.0)
    foundation = {"price_ceiling": strat.get("price_ceiling", 0.20), "max_soc": strat.get("max_soc", 90),
                  "charge_start_floor": floor_eff, "note_active": bool(note)}
    cap_kwh = float(strat.get("battery_capacity_kwh", 30))
    stored_kwh = round(cap_kwh * soc / 100.0, 1)
    # Use the measured rolling base load if we have enough history; else the static estimate.
    typical_load = consumption["avg_daily_total_kwh"] if consumption["days_sampled"] >= 2 \
        else strat.get("typical_daily_load_kwh", 30)
    # Auto-sell survival floor: keep enough SoC to cover load until tomorrow's solar ramp (minus any
    # solar still to come today), so selling on a silly-high feed-in never strands us overnight.
    reserve = strat.get("reserve_soc", 20)
    hrs_to_solar = 12.0
    if sun_rise:
        rt = _parse_t(sun_rise)
        if rt:
            hrs_to_solar = min(16.0, max(1.0, (rt - datetime.now(timezone.utc)).total_seconds() / 3600.0 + 2))
    overnight_load = float(typical_load) * (hrs_to_solar / 24.0)
    survival_kwh = max(0.0, overnight_load - (solar_remaining or 0.0))
    survival_soc = int(min(strat.get("max_soc", 90), reserve + round(survival_kwh / cap_kwh * 100)))
    sell_eff = _OV["sell"] if _OV.get("sell") is not None else strat.get("sell_price", 0.50)
    # Dynamic policy: the LLM tunes charge_start_price + target_soc within the foundation guardrails.
    plan_ctx = {
        "goal": GOAL, "site": SITE_FACTS, "month_now": datetime.now().month, "weather": weather,
        "operator_note": note or None,
        "solar_forecast_kwh": {"today_total_forecast": solar_today_total,
                               "remaining_today_only": solar_remaining, "tomorrow": solar_tomorrow,
                               "system_size_kw": 6.975,
                               "note": "today_total_forecast = whole day; remaining_today_only = future "
                                       "part still to come (small in the evening). Use remaining + tomorrow "
                                       "for forward planning, NOT remaining as 'today's solar'."},
        "battery": {"capacity_kwh": cap_kwh, "stored_kwh": stored_kwh, "soc_pct": round(soc),
                    "reserve_soc": strat.get("reserve_soc"),
                    "typical_daily_load_kwh": typical_load},
        "consumption": consumption,   # measured rolling daily kWh (total / base / EV), days_sampled
        "feedin_price": prices.get("feedin"),
        "amber_price": prices.get("price"), "amber_descriptor": prices.get("descriptor"),
        "aemo_price": prices.get("aemo_price"),
        "soc_pct": round(soc), "solar_kw": round(pv, 2), "load_kw": round(load, 2),
        "solar_surplus_kw": round(pv - load, 2), "demand_window": demand_window,
        "currently_charging": charging,
        "amber_forecast_18h": _forecast_digest(prices.get("forecast", []), 18, 30),
        "aemo_forecast_18h": _forecast_digest(prices.get("aemo_forecast", []), 18, 60),
        "foundation": {**foundation, "reserve_soc": strat.get("reserve_soc")},
        "baseline": {"charge_start_price": strat.get("charge_start_price"), "target_soc": strat.get("target_soc")},
    }
    _LLM["last_ctx"] = plan_ctx
    zerohero = strat.get("tariff_mode") == "zerohero"
    working, dyn_src = dict(strat), "static"
    if zerohero:
        # GloBird ZeroHero: deterministic time-of-use schedule (no Amber price forecasting / LLM).
        nowl = datetime.now(); hh = nowl.hour + nowl.minute / 60.0
        free_start = strat.get("zerohero", {}).get("free_start_h", 11)
        hrs_to_free = (free_start - hh) % 24 or 24.0          # hours until next free window
        pred = predict_base_load(consumption.get("hour_profile"), hrs_to_free) if consumption.get("profile_days", 0) >= 2 else None
        need_kwh = max(0.0, (pred if pred is not None else float(typical_load) * (hrs_to_free / 24.0)) - (solar_remaining or 0.0))
        survival_soc = int(min(strat.get("max_soc", 90), reserve + round(need_kwh / cap_kwh * 100)))
        rec = decide_zerohero(soc, wm.get("value"), strat, survival_soc)
        plan, dyn_src = None, "zerohero"
        working["target_soc"] = strat.get("max_soc", 90)
    else:
        plan = maybe_llm_review(cfg, plan_ctx)
        if plan and plan.get("params") and strat.get("dynamic_policy", True):
            working = apply_dynamic_params(strat, plan["params"], foundation)
            dyn_src = "LLM"
        # Inject auto-sell parameters (deterministic foundation behaviour) into the working strategy.
        working["sell_price"] = sell_eff
        working["sell_floor_soc"] = survival_soc
        working["sell_enabled"] = bool(strat.get("sell_enabled", True))
        # Day energy balance: usable battery (above reserve) + remaining solar vs remaining load.
        # Prefer the learned hour-of-day profile to predict the rest of today; else flat fallback.
        hrs_to_midnight = 24 - (datetime.now().hour + datetime.now().minute / 60.0)
        pred = predict_base_load(consumption.get("hour_profile"), hrs_to_midnight) if consumption.get("profile_days", 0) >= 2 else None
        remaining_load = pred if pred is not None else max(0.0, float(typical_load) - float(consumption.get("today_so_far_kwh") or 0))
        usable_now = max(0.0, stored_kwh - cap_kwh * reserve / 100.0)
        working["energy_shortfall_kwh"] = round(remaining_load - (usable_now + (solar_remaining or 0.0)), 1)
        rec = decide(prices, soc, pv, wm.get("value"), working,
                     currently_charging=charging, load_kw=load, demand_window=demand_window)

    now_epoch = time.time()
    return {
        "demand_window": demand_window,
        "weather": weather,
        "solar_forecast": {"today_total": solar_today_total, "remaining_today": solar_remaining,
                           "tomorrow": solar_tomorrow},
        "solar_bells": solar_bells,
        "llm": plan,
        "dynamic": {"source": dyn_src, "mode": ("zerohero" if zerohero else "amber"),
                    "charge_start_price": (None if zerohero else working.get("charge_start_price")),
                    "target_soc": working.get("target_soc"),
                    "price_ceiling": foundation["price_ceiling"], "max_soc": foundation["max_soc"],
                    "charge_start_floor": (None if zerohero else foundation["charge_start_floor"]),
                    "sell_price": (None if zerohero else sell_eff), "survival_soc": survival_soc,
                    "sell_enabled": (True if zerohero else bool(strat.get("sell_enabled", True)))},
        "battery": {"capacity_kwh": cap_kwh, "stored_kwh": stored_kwh},
        "consumption": consumption,
        "energy_shortfall_kwh": working.get("energy_shortfall_kwh"),
        "feedin": prices.get("feedin"),
        "grid_power": grid_power,
        "feedin_power": feedin_power,
        "battery_power": battery_power,
        "bat_charge_power": bat_charge_power,
        "bat_discharge_power": bat_discharge_power,
        "pv_strings": pv_strings,
        "energy_totals": energy,
        "sched_active": sched_active,
        "note": note,
        "override": {"floor": _OV["floor"], "manual": _OV["manual"]},
        "ts": datetime.now().isoformat(timespec="seconds"),
        "scheduler": sched,
        "soc_updated_epoch": soc_ts,
        "data_age_s": int(now_epoch - soc_ts) if soc_ts else None,
        "load_kw": load,
        "solar_surplus_kw": round(pv - load, 2),
        "telemetry_source": tsrc,
        "price": prices.get("price"),
        "descriptor": prices.get("descriptor"),
        "aemo_price": prices.get("aemo_price"),
        "feedin": prices.get("feedin"),
        "forecast_next": prices.get("forecast", [])[:6],
        "aemo_forecast_next": prices.get("aemo_forecast", [])[:6],
        "forecast_h": prices.get("forecast", []),
        "aemo_forecast_h": prices.get("aemo_forecast", []),
        "soc": soc,
        "pv_kw": pv,
        "real": real,
        "work_mode": wm.get("value"),
        "work_mode_options": wm.get("enumList"),
        "recommendation": rec,
        "applied": None,
    }


def manual_tick(cfg, snap):
    """If a manual override (force-charge / sell) is active, enforce it and return a status string.
    Reverts and returns None when it has expired or none is set. Honours allow_control."""
    load_ov(cfg)
    mo = _OV["manual"]
    if not mo:
        return None
    if not cfg["control"].get("allow_control"):
        return "manual override set but control disabled"
    fox = FoxESS(cfg["foxess"]["token"], cfg["foxess"]["sn"])
    now = time.time()
    if now >= mo["until"]:                      # expired → revert to auto
        try:
            fox.disable_scheduler()
        except Exception as e:
            print(f"manual revert failed: {e}", file=sys.stderr)
        _CHARGE["until"] = 0.0
        _OV["manual"] = None; save_ov(cfg)
        log_event("override", f"manual {mo['mode']} expired → revert to auto")
        return None
    want = "ForceCharge" if mo["mode"] == "charge" else "ForceDischarge"
    active = (snap.get("scheduler") or {}).get("active") or {}
    end = datetime.now() + timedelta(seconds=mo["until"] - now)
    hhmm = end.strftime("%H:%M")
    if active.get("mode") == want:
        return f"MANUAL {mo['mode']} until {hhmm} (active)"
    nd = datetime.now()
    if mo["mode"] == "charge":
        fox.enable_force_charge((nd.hour, nd.minute), (end.hour, end.minute),
                                cfg["strategy"]["min_soc_on_grid"], mo["cap"], mo["power"])
        _CHARGE["until"] = mo["until"]
    else:
        fox.enable_force_discharge((nd.hour, nd.minute), (end.hour, end.minute),
                                   mo["min_soc"], mo["power"])
    msg = f"MANUAL {mo['mode']} START until {hhmm} @ {mo['power']}kW"
    log_event("override", msg)
    return msg


def apply_and_record(cfg: dict, snap: dict) -> str:
    """Apply the recommendation and persist the outcome into the shared LAST snapshot so the dashboard
    header reflects what just happened (instead of the stale value from the previous evaluate)."""
    msg = apply_recommendation(cfg, snap)
    with LAST_LOCK:
        if LAST:
            LAST["applied"] = msg
    return msg


def apply_recommendation(cfg: dict, snap: dict) -> str:
    ctrl = cfg["control"]
    rec = snap["recommendation"]
    if not ctrl.get("allow_control"):
        return "control disabled (control.allow_control=false) — not applying"
    # Safety: never act on stale telemetry (FoxESS poll failed → using cached/old values).
    if "stale" in (snap.get("telemetry_source") or "") or "down" in (snap.get("telemetry_source") or ""):
        return "telemetry STALE (FoxESS poll failed) — not applying (safety hold)"
    msgs = []
    fox = FoxESS(cfg["foxess"]["token"], cfg["foxess"]["sn"])
    strat = cfg["strategy"]
    # Use the dynamic (LLM-tuned, foundation-clamped) target SoC for the actual charge cap.
    eff_target = (snap.get("dynamic") or {}).get("target_soc") or strat["target_soc"]
    sch = snap.get("scheduler") or {}
    already_charging = bool(sch.get("enabled") and sch.get("active")
                            and sch["active"].get("mode") == "ForceCharge")
    already_selling = bool(sch.get("enabled") and sch.get("active")
                           and sch["active"].get("mode") == "ForceDischarge")
    # AUTO-SELL: export to grid on a silly-high feed-in, down to the survival floor.
    if rec.get("force_discharge"):
        if not ctrl.get("set_force_charge"):
            msgs.append("auto-sell wanted but control.set_force_charge=false — skipped")
        elif already_selling:
            msgs.append("already selling (no rewrite)")
        else:
            now = datetime.now()
            mins = int(strat.get("force_charge_minutes", 120))
            tot = now.hour * 60 + now.minute + mins
            eh, em = (tot // 60) % 24, tot % 60
            fox.enable_force_discharge((now.hour, now.minute), (eh, em),
                                       rec.get("sell_floor", strat["reserve_soc"]),
                                       strat["force_charge_power_kw"])
            m = f"AUTO-SELL START until ~{eh:02d}:{em:02d} (down to {rec.get('sell_floor')}% @ {strat['force_charge_power_kw']}kW)"
            msgs.append(m); log_event("sell", m, {"feedin": snap.get("feedin"), "soc": snap.get("soc")})
        return "; ".join(msgs) or "selling"
    if already_selling and not rec.get("force_discharge"):
        fox.disable_scheduler()
        msgs.append("auto-sell STOP → revert to work mode")
        log_event("disable", "auto-sell STOP → revert to work mode")
    if rec["force_charge"]:
        if not ctrl.get("set_force_charge"):
            msgs.append("force-charge recommended but control.set_force_charge=false — skipped")
        elif already_charging:
            msgs.append("already force-charging (no rewrite)")   # <-- no API write while charging
        else:
            now = datetime.now()
            mins = int(strat.get("force_charge_minutes", 120))   # safety cap; re-evaluated each cycle
            tot = now.hour * 60 + now.minute + mins
            eh, em = (tot // 60) % 24, tot % 60
            fox.enable_force_charge((now.hour, now.minute), (eh, em),
                                    strat["min_soc_on_grid"], eff_target,
                                    strat["force_charge_power_kw"])
            _CHARGE["until"] = time.time() + mins * 60   # remember our intended charge window
            m = f"force-charge START until ~{eh:02d}:{em:02d} (cap {eff_target}% @ {strat['force_charge_power_kw']}kW)"
            msgs.append(m); log_event("force_charge", m, {"band": rec.get("band"), "soc": snap.get("soc")})
    else:
        # Stop only on the transition out of charging — one write, not every cycle.
        if ctrl.get("set_force_charge") and (already_charging or time.time() < _CHARGE["until"]):
            fox.disable_scheduler()
            _CHARGE["until"] = 0.0
            msgs.append("force-charge STOP → revert to work mode")
            log_event("disable", "force-charge STOP → revert to work mode", {"band": rec.get("band")})
    if rec["action"] in ("SET_MODE", "FORCE_CHARGE") and ctrl.get("set_work_mode"):
        if snap["work_mode"] != rec["target_mode"]:
            fox.set_work_mode(rec["target_mode"])
            msgs.append(f"work mode {snap['work_mode']} → {rec['target_mode']}")
            log_event("work_mode", f"{snap['work_mode']} → {rec['target_mode']}", {"band": rec.get("band")})
        else:
            msgs.append(f"work mode already {rec['target_mode']}")
    return "; ".join(msgs) or "nothing to do"


def force_charge_test(cfg: dict, minutes: int = 10) -> str:
    """Manual, bounded force-charge — the supervised test button. Needs allow_control."""
    if not cfg["control"].get("allow_control"):
        return "control disabled (control.allow_control=false)"
    fox = FoxESS(cfg["foxess"]["token"], cfg["foxess"]["sn"])
    strat = cfg["strategy"]
    now = datetime.now()
    tot = now.hour * 60 + now.minute + max(1, int(minutes))
    eh, em = (tot // 60) % 24, tot % 60
    fox.enable_force_charge((now.hour, now.minute), (eh, em),
                            strat["min_soc_on_grid"], strat["target_soc"], strat["force_charge_power_kw"])
    msg = (f"force-charge TEST enabled {now.hour:02d}:{now.minute:02d}→{eh:02d}:{em:02d} "
           f"cap {strat['target_soc']}% @ {strat['force_charge_power_kw']}kW")
    log_event("force_charge_test", msg)
    return msg


def scheduler_off(cfg: dict) -> str:
    if not cfg["control"].get("allow_control"):
        return "control disabled (control.allow_control=false)"
    FoxESS(cfg["foxess"]["token"], cfg["foxess"]["sn"]).disable_scheduler()
    _CHARGE["until"] = 0.0
    log_event("disable", "manual: scheduler disabled → reverted to plain work mode")
    return "scheduler disabled → reverted to plain work mode"


def run_once(cfg: dict, do_apply: bool) -> dict:
    snap = gather_and_decide(cfg)
    snap["band"] = snap.get("recommendation", {}).get("band")
    # A manual override (force-charge / sell) takes precedence and is enforced every cycle regardless
    # of auto_apply, so a button press isn't undone by the next automatic evaluation.
    mo_msg = manual_tick(cfg, snap)
    if mo_msg is not None:
        snap["applied"] = mo_msg
    elif do_apply:
        snap["applied"] = apply_recommendation(cfg, snap)
    mqtt_publish(cfg, snap)
    run_band_actions(cfg, snap)
    maybe_notify(cfg, snap)
    append_log(snap)
    with LAST_LOCK:
        LAST.clear(); LAST.update(snap)
    return snap


# ------------------------------------------------------------------- web -----

BAND_COLOR = {"ludicrous": "#7b2ff7", "extremely_low": "#0a8f3c", "low": "#3c9", "normal": "#888",
              "high": "#e67e22", "spike": "#c0392b", "unknown": "#aaa"}

CSS = """body{font:16px system-ui;margin:1.5rem auto;max-width:1280px;padding:0 1rem}
h1{font-size:1.4rem} .big{font-size:2rem;font-weight:600}
.row{display:flex;gap:1.2rem;flex-wrap:wrap;margin:1rem 0}
.card{border:1px solid #ddd;border-radius:10px;padding:.8rem 1.1rem;min-width:130px}
.rec{background:#f3f8ff;border-color:#9cf} .pill{color:#fff;padding:2px 9px;border-radius:20px;font-size:.8rem}
button{font:inherit;padding:.5rem .9rem;border:1px solid #bbb;border-radius:8px;background:#fafafa;cursor:pointer;margin:.2rem}
button:hover{background:#eee} .danger{border-color:#c0392b;color:#c0392b}
table{border-collapse:collapse;margin-top:.5rem;font-size:.85rem;width:100%}
td,th{border:1px solid #eee;padding:3px 7px;text-align:right} th{background:#fafafa}
small{color:#666} #msg{margin:.5rem 0;color:#06c}
.chartwrap{resize:both;overflow:auto;width:100%;height:360px;min-width:320px;min-height:180px;max-width:none;border:1px dashed #bbb;border-radius:8px}
.chartwrap svg{width:100%;height:100%;display:block}
@media (prefers-color-scheme: dark){
 .chartwrap{border-color:#4a505a}
 body{background:#111418;color:#e3e3e3}
 .card{background:#1e2227;border-color:#3a3f46}
 .rec{background:#15263a;border-color:#3a567a}
 button{background:#262b31;color:#e3e3e3;border-color:#4a505a}
 button:hover{background:#333a42} .danger{border-color:#e06; color:#f88}
 th{background:#262b31} td,th{border-color:#3a3f46}
 small{color:#9aa3ad} #msg{color:#6cf} a{color:#6cf}
}"""

JS = """
async function saveNote(){const t=document.getElementById('note').value;
 document.getElementById('msg').textContent='saving note…';
 const r=await fetch('/api/note',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})});
 const j=await r.json();document.getElementById('msg').textContent=JSON.stringify(j);
 setTimeout(()=>location.reload(),1800);}
function clearNote(){document.getElementById('note').value='';saveNote();}
async function saveBaseline(){const f=document.getElementById('bfloor').value,s=document.getElementById('bsell').value;
 document.getElementById('msg').textContent='saving baseline…';
 const r=await fetch('/api/baseline',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({floor:f,sell:s})});
 const j=await r.json();document.getElementById('msg').textContent=JSON.stringify(j);setTimeout(()=>location.reload(),1500);}
async function ov(p){document.getElementById('msg').textContent='…';
 const r=await fetch(p,{method:'POST'});const j=await r.json();
 document.getElementById('msg').textContent=JSON.stringify(j);setTimeout(()=>location.reload(),1500);}
async function post(p){document.getElementById('msg').textContent='…';
 const r=await fetch(p,{method:'POST'});const j=await r.json();
 document.getElementById('msg').textContent=JSON.stringify(j);
 setTimeout(()=>location.reload(),1200);}
async function refreshLog(){const r=await fetch('/api/log?n=40');const rows=await r.json();
 const t=document.getElementById('log');t.innerHTML='<tr><th>time</th><th>$</th><th>aemo</th><th>band</th><th>soc</th><th>action→mode</th><th>applied</th></tr>'+
 rows.reverse().map(x=>`<tr><td>${(x.ts||'').slice(11,19)}</td><td>${x.price}</td><td>${x.aemo_price}</td><td>${x.band||''}</td><td>${Math.round(x.soc||0)}%</td><td>${x.action}→${x.target_mode}${x.force_charge?' ⚡':''}</td><td style=text-align:left>${x.applied||''}</td></tr>`).join('');}
async function refreshEvents(){const r=await fetch('/api/events?n=30');const rows=await r.json();
 const t=document.getElementById('events');
 if(!rows.length){t.innerHTML='<tr><td><small>no actions taken yet</small></td></tr>';return;}
 t.innerHTML='<tr><th>time</th><th>kind</th><th>detail</th></tr>'+
 rows.reverse().map(x=>`<tr><td>${(x.ts||'').slice(5,19).replace('T',' ')}</td><td><b>${x.kind}</b></td><td style=text-align:left>${x.detail||''}</td></tr>`).join('');}
async function tick(){const r=await fetch('/api/state');const d=await r.json();
 const el=document.getElementById('countdown');const ae=document.getElementById('age');
 if(d.next_poll_epoch){const s=Math.max(0,Math.round(d.next_poll_epoch-Date.now()/1000));
   el.textContent='next in '+s+'s';}
 if(d.soc_updated_epoch){ae.textContent=Math.round(Date.now()/1000-d.soc_updated_epoch);}}
setInterval(tick,1000);tick();
refreshLog();refreshEvents();
(function(){const c=document.getElementById('chartwrap');if(!c)return;
 try{const s=JSON.parse(localStorage.getItem('foxctl_chart')||'{}');if(s.w)c.style.width=s.w;if(s.h)c.style.height=s.h;}catch(e){}
 c.addEventListener('mouseup',()=>{try{localStorage.setItem('foxctl_chart',JSON.stringify({w:c.style.width,h:c.style.height}));}catch(e){}});})();"""


def render_forecast_svg(snap: dict, cfg: dict | None = None) -> str:
    """SVG of the 18h price horizon the controller reasons over: Amber + AEMO curves, the
    LLM-set charge-start price, the foundation ceiling, shaded 'would-charge' windows, and the
    cheapest/peak markers. Makes it visible whether the future schedule is actually considered."""
    dyn = snap.get("dynamic") or {}
    now = datetime.now(timezone.utc)
    amber, aemo = [], []
    for p in snap.get("forecast_h") or []:
        t = _parse_t(p.get("t") or "")
        if t and p.get("price") is not None:
            h = (t - now).total_seconds() / 3600.0
            if -0.3 <= h <= 18:
                amber.append((h, p["price"], t))
    for p in snap.get("aemo_forecast_h") or []:
        t = _parse_t(p.get("t") or "")
        if t and p.get("price") is not None:
            h = (t - now).total_seconds() / 3600.0
            if -0.3 <= h <= 18:
                aemo.append((h, p["price"]))
    if not amber:
        return "<small>no forecast to chart yet</small>"
    tz = amber[0][2].tzinfo   # label the x-axis in the forecast's own (Sydney) time
    W, H, padL, padR, padT, padB = 1180, 310, 50, 50, 16, 34
    iw, ih = W - padL - padR, H - padT - padB
    csp = dyn.get("charge_start_price") or 0.0
    ceil = dyn.get("price_ceiling") or 0.20
    allp = [pr for _, pr, _ in amber] + [pr for _, pr in aemo] + [ceil, csp]
    ymax = max(allp) * 1.12 or 0.3
    ymin = min(min(allp), 0.0)
    xmin = min(h for h, _, _ in amber)
    xmax = max(h for h, _, _ in amber) or 1
    bells = snap.get("solar_bells") or []
    # Rolling hour-of-day usage profile (avg kWh in each wall-clock hour ≈ avg kW) overlaid on the kW axis.
    hour_profile = (snap.get("consumption") or {}).get("hour_profile") or {}
    def _usage_at(t):
        lh = t.astimezone(tz).hour
        v = hour_profile.get(lh, hour_profile.get(str(lh)))
        return float(v) if isinstance(v, (int, float)) else None
    usage = [(h, _usage_at(t)) for h, _, t in amber]
    usage = [(h, v) for h, v in usage if v is not None]
    usage_max = max([v for _, v in usage] + [0.0])
    skw = max([b["pmax"] for b in bells] + [usage_max, 1.0]) * 1.15   # right-axis kW scale (solar + usage)
    X = lambda h: padL + iw * (h - xmin) / ((xmax - xmin) or 1)
    Y = lambda p: padT + ih * (1 - (p - ymin) / ((ymax - ymin) or 1))
    SY = lambda kw: padT + ih * (1 - kw / skw)   # power (kW) → y (right axis)
    PY = lambda pct: padT + ih * (1 - max(0.0, min(100.0, pct)) / 100.0)   # SoC % → y (full height)
    # --- forward SoC projection (ESTIMATE) + "would sell" windows ---------------------------------
    # Roll current SoC through the forecast: grid force-charge while buy ≤ charge-start, export while
    # the price is silly-high (≥ sell threshold) and SoC is above the survival floor, else solar−load.
    # Sell uses the buy-price forecast as a proxy (no per-slot feed-in forecast yet) — see chart legend.
    soc0 = snap.get("soc")
    cap = float((snap.get("battery") or {}).get("capacity_kwh") or 30) or 30.0
    target_pct = dyn.get("target_soc") or 90
    max_pct = dyn.get("max_soc") or 90
    survival = dyn.get("survival_soc") or 20
    sell_thr = dyn.get("sell_price")
    sell_on = bool(dyn.get("sell_enabled")) and isinstance(sell_thr, (int, float))
    charge_kw = float(((cfg or {}).get("strategy") or {}).get("force_charge_power_kw", 10.5))
    def _bell_kw(hh):
        tot = 0.0
        for b in bells:
            if b["s"] <= hh <= b["e"] and b["pmax"] > 0:
                frac = (hh - b["s"]) / ((b["e"] - b["s"]) or 1)
                tot += b["pmax"] * math.sin(math.pi * min(max(frac, 0), 1))
        return tot
    proj = []
    if isinstance(soc0, (int, float)):
        fut = [(h, pr, t) for h, pr, t in amber if h >= -0.05]
        soc_kwh = cap * float(soc0) / 100.0
        floor_kwh, max_kwh, tgt_kwh = cap * 0.10, cap * max_pct / 100.0, cap * target_pct / 100.0
        for i, (h, buy, t) in enumerate(fut):
            nh = fut[i + 1][0] if i + 1 < len(fut) else h + 0.5
            dt = min(1.5, max(0.05, nh - h))
            solar_kwh = _bell_kw((h + nh) / 2.0) * dt
            lu = _usage_at(t)
            load_kwh = (lu or 0.0) * dt
            soc_pct = soc_kwh / cap * 100.0
            sell = sell_on and buy >= sell_thr and soc_pct > survival
            if buy <= csp and soc_kwh < tgt_kwh:                       # grid force-charge window
                soc_kwh = min(tgt_kwh, soc_kwh + charge_kw * dt) + max(0.0, solar_kwh - load_kwh)
            elif sell:                                                  # export to grid down to floor
                soc_kwh -= min(charge_kw * dt, soc_kwh - cap * survival / 100.0)
            else:
                soc_kwh += solar_kwh - load_kwh
            soc_kwh = max(floor_kwh, min(max_kwh, soc_kwh))
            proj.append({"h": h, "soc": round(soc_kwh / cap * 100.0, 1), "sell": sell})
    out = [f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMid meet" style="font:14px system-ui">']
    # SOLAR overlay (sunny times / intensity) — half-sine bell, area = forecast kWh, on right kW axis
    for b in bells:
        s, e = max(b["s"], xmin), min(b["e"], xmax)
        if e <= s or b["pmax"] <= 0:
            continue
        steps = 40
        pts = [f"{X(s):.1f},{SY(0):.1f}"]
        for k in range(steps + 1):
            h = s + (e - s) * k / steps
            frac = (h - b["s"]) / ((b["e"] - b["s"]) or 1)
            kw = b["pmax"] * math.sin(math.pi * min(max(frac, 0), 1))
            pts.append(f"{X(h):.1f},{SY(kw):.1f}")
        pts.append(f"{X(e):.1f},{SY(0):.1f}")
        out.append(f'<polygon points="{" ".join(pts)}" fill="#f5c518" opacity="0.22"/>')
        hpk = b["s"] + (b["e"] - b["s"]) / 2
        if xmin <= hpk <= xmax:
            out.append(f'<text x="{X(hpk):.1f}" y="{SY(b["pmax"])-4:.1f}" text-anchor="middle" '
                       f'fill="#b8860b">☀ {b["kwh"]}kWh (~{b["pmax"]}kW)</text>')
    # shaded "would grid-charge" bands (Amber price <= charge-start price)
    seg_start = None
    for h, pr, _ in amber + [(amber[-1][0], 1e9, None)]:
        below = pr <= csp
        if below and seg_start is None:
            seg_start = h
        elif not below and seg_start is not None:
            out.append(f'<rect x="{X(seg_start):.1f}" y="{padT}" width="{max(1,X(h)-X(seg_start)):.1f}" '
                       f'height="{ih}" fill="#2ecc71" opacity="0.16"/>')
            seg_start = None
    # shaded "would sell" windows (price ≥ sell threshold AND projected SoC above the survival floor)
    sseg = None
    for d in proj + [{"h": proj[-1]["h"] if proj else 0, "sell": False}]:
        if d["sell"] and sseg is None:
            sseg = d["h"]
        elif not d["sell"] and sseg is not None:
            out.append(f'<rect x="{X(sseg):.1f}" y="{padT}" width="{max(1,X(d["h"])-X(sseg)):.1f}" '
                       f'height="{ih}" fill="#e84393" opacity="0.16"/>')
            sseg = None
    # threshold lines
    out.append(f'<line x1="{padL}" y1="{Y(ceil):.1f}" x2="{W-padR}" y2="{Y(ceil):.1f}" stroke="#c0392b" '
               f'stroke-dasharray="5 4" stroke-width="1"/><text x="{padL+3}" y="{Y(ceil)-3:.1f}" '
               f'fill="#c0392b">ceiling ${ceil:.2f}</text>')
    out.append(f'<line x1="{padL}" y1="{Y(csp):.1f}" x2="{W-padR}" y2="{Y(csp):.1f}" stroke="#1a9e4b" '
               f'stroke-dasharray="5 4" stroke-width="1"/><text x="{padL+3}" y="{Y(csp)-3:.1f}" '
               f'fill="#1a9e4b">charge ≤ ${csp:.2f}</text>')
    # left y axis ($) + right y axis (kW solar)
    for k in range(5):
        yv = ymin + (ymax - ymin) * k / 4
        out.append(f'<text x="{padL-6}" y="{Y(yv)+4:.1f}" text-anchor="end" fill="#999">${yv:.2f}</text>'
                   f'<line x1="{padL}" y1="{Y(yv):.1f}" x2="{W-padR}" y2="{Y(yv):.1f}" stroke="#8884" stroke-width="0.5"/>')
        kv = skw * k / 4
        out.append(f'<text x="{W-padR+6}" y="{SY(kv)+4:.1f}" text-anchor="start" fill="#b8860b">{kv:.1f}kW</text>')
    # x axis ticks every 3h — REAL clock times
    for hh in range(0, 19, 3):
        if xmin <= hh <= xmax:
            lab = (now + timedelta(hours=hh)).astimezone(tz).strftime("%H:%M")
            out.append(f'<line x1="{X(hh):.1f}" y1="{padT}" x2="{X(hh):.1f}" y2="{H-padB}" stroke="#8883" stroke-width="0.5"/>'
                       f'<text x="{X(hh):.1f}" y="{H-padB+16}" text-anchor="middle" fill="#999">{lab}</text>')
    # now marker
    out.append(f'<line x1="{X(0):.1f}" y1="{padT}" x2="{X(0):.1f}" y2="{H-padB}" stroke="#3498db" stroke-width="1.2"/>'
               f'<text x="{X(0)+3:.1f}" y="{padT+11}" fill="#3498db">now</text>')
    if aemo:
        pl = " ".join(f"{X(h):.1f},{Y(pr):.1f}" for h, pr in aemo)
        out.append(f'<polyline points="{pl}" fill="none" stroke="#e67e22" stroke-width="1.4" stroke-dasharray="3 3" opacity="0.85"/>')
    pl = " ".join(f"{X(h):.1f},{Y(pr):.1f}" for h, pr, _ in amber)
    out.append(f'<polyline points="{pl}" fill="none" stroke="#2980d9" stroke-width="2.2"/>')
    # rolling avg usage overlay (right kW axis)
    if usage:
        upl = " ".join(f"{X(h):.1f},{SY(v):.1f}" for h, v in usage)
        out.append(f'<polyline points="{upl}" fill="none" stroke="#8e44ad" stroke-width="1.6" '
                   f'stroke-dasharray="6 3" opacity="0.85"/>')
    # projected SoC curve (% on full height) + survival-floor line
    if proj:
        spl = " ".join(f"{X(d['h']):.1f},{PY(d['soc']):.1f}" for d in proj)
        out.append(f'<polyline points="{spl}" fill="none" stroke="#16a085" stroke-width="1.8" opacity="0.9"/>')
        out.append(f'<line x1="{padL}" y1="{PY(survival):.1f}" x2="{W-padR}" y2="{PY(survival):.1f}" '
                   f'stroke="#16a085" stroke-dasharray="2 4" stroke-width="0.8" opacity="0.6"/>')
        out.append(f'<text x="{W-padR-2}" y="{PY(survival)-3:.1f}" text-anchor="end" fill="#16a085">'
                   f'survival {int(survival)}%</text>')
        out.append(f'<text x="{X(proj[0]["h"])+3:.1f}" y="{PY(proj[0]["soc"])-5:.1f}" '
                   f'fill="#16a085">SoC {proj[0]["soc"]:.0f}%</text>')
    cheapest = min(amber, key=lambda x: x[1]); peak = max(amber, key=lambda x: x[1])
    for (h, pr, _), col, txt in ((cheapest, "#1a9e4b", f"min ${cheapest[1]:.2f}"),
                                 (peak, "#c0392b", f"peak ${peak[1]:.2f}")):
        out.append(f'<circle cx="{X(h):.1f}" cy="{Y(pr):.1f}" r="3.5" fill="{col}"/>'
                   f'<text x="{X(h):.1f}" y="{Y(pr)-6:.1f}" text-anchor="middle" fill="{col}">{txt}</text>')
    # hover cursor + tooltip (positioned client-side from the embedded sample points)
    out.append(f'<line id="hovline" x1="0" y1="{padT}" x2="0" y2="{H-padB}" stroke="#555" '
               f'stroke-width="1" stroke-dasharray="2 2" style="display:none"/>')
    out.append('<rect id="hovtipbg" x="0" y="0" rx="3" fill="#000" opacity="0.78" style="display:none"/>')
    out.append('<text id="hovtip" x="0" y="0" fill="#fff" font-size="13" style="display:none"></text>')
    out.append("</svg>")
    socmap = {round(d["h"], 3): d for d in proj}
    pts = []
    for h, pr, t in amber:
        d = socmap.get(round(h, 3))
        pts.append({"x": round(X(h), 1), "t": t.astimezone(tz).strftime("%H:%M"), "price": round(pr, 3),
                    "use": _usage_at(t), "soc": (d["soc"] if d else None),
                    "sell": (bool(d["sell"]) if d else False)})
    hover = {"pts": pts, "W": W, "padR": padR}
    script = ("<script>(function(){var w=document.getElementById('chartwrap');if(!w)return;"
              "var s=w.querySelector('svg');if(!s)return;var D=" + json.dumps(hover) + ";"
              "var ln=s.getElementById('hovline'),tp=s.getElementById('hovtip'),bg=s.getElementById('hovtipbg');"
              "function hide(){ln.style.display='none';tp.style.display='none';bg.style.display='none';}"
              "s.addEventListener('mousemove',function(e){var m=s.getScreenCTM();if(!m)return;"
              "var p=s.createSVGPoint();p.x=e.clientX;p.y=e.clientY;var l=p.matrixTransform(m.inverse());"
              "var best=null,bd=1e9;for(var i=0;i<D.pts.length;i++){var d=Math.abs(D.pts[i].x-l.x);"
              "if(d<bd){bd=d;best=D.pts[i];}}if(!best){hide();return;}"
              "ln.setAttribute('x1',best.x);ln.setAttribute('x2',best.x);ln.style.display='';"
              "tp.textContent=best.t+'   $'+best.price.toFixed(2)+(best.use!=null?'   ~'+best.use.toFixed(1)+'kW use':'')"
              "+(best.soc!=null?'   SoC '+Math.round(best.soc)+'%':'')+(best.sell?'   ⟶ SELL':'');"
              "tp.style.display='';var tx=best.x+9;tp.setAttribute('y',18);tp.setAttribute('x',tx);"
              "var b=tp.getBBox();if(b.x+b.width>D.W-D.padR){tx=best.x-9-b.width;tp.setAttribute('x',tx);b=tp.getBBox();}"
              "bg.setAttribute('x',b.x-5);bg.setAttribute('y',b.y-3);bg.setAttribute('width',b.width+10);"
              "bg.setAttribute('height',b.height+6);bg.style.display='';});"
              "s.addEventListener('mouseleave',hide);})();</script>")
    legend = ('<small><b style="color:#2980d9">— Amber retail</b> (charges on this) · '
              '<span style="color:#e67e22">- - AEMO wholesale</span> · '
              '<span style="color:#b8860b">▮ est. solar (right kW axis)</span> · '
              '<span style="color:#8e44ad">- - your avg usage (right kW axis)</span> · '
              '<span style="color:#16a085">— projected SoC %</span> · '
              '<span style="color:#2ecc71">▮ would grid-charge</span> · '
              '<span style="color:#e84393">▮ would sell</span> · hover for time<br>'
              '<i>SoC projection &amp; sell windows are estimates (buy-price proxy for export, '
              'forecast solar/load) — directional, not exact.</i></small>')
    return "".join(out) + script + legend


def render(snap: dict, cfg: dict) -> str:
    rec = snap.get("recommendation", {})
    band = rec.get("band", "unknown")
    color = BAND_COLOR.get(band, "#888")
    fc = snap.get("forecast_next", [])
    arows = "".join(
        f"<tr><td>{(p.get('t') or '')[11:16]}</td><td>${p.get('price')}</td><td>{p.get('descriptor') or ''}</td></tr>"
        for p in fc)
    atable = f"<table><tr><th>time</th><th>Amber $/kWh</th><th>band</th></tr>{arows}</table>" if arows else "<small>none</small>"
    ctrl = cfg["control"]
    dyn = snap.get("dynamic") or {}
    bat = snap.get("battery") or {}
    note_esc = (get_note(cfg) or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    note_html = (f'<h3>Steering note <small>(free text — the LLM reads this as a priority instruction; '
                 f'a note relaxes the charge floor)</small></h3>'
                 f'<div class=card><textarea id=note rows=3 style="width:100%;font:inherit;box-sizing:border-box" '
                 f'placeholder="e.g. Let the battery discharge until the ~9c midday trough, then charge.">{note_esc}</textarea>'
                 f'<div style="margin-top:.4rem"><button onclick="saveNote()">Save note</button>'
                 f'<button onclick="clearNote()">Clear</button> '
                 f'<small>{"📣 active — floor relaxed, LLM following your note" if note_esc else "no note set"}</small></div></div>')
    ov = snap.get("override") or {}
    mo = ov.get("manual")
    eff_floor = ov.get("floor") if ov.get("floor") is not None else cfg["strategy"].get("charge_start_floor", 0.0)
    if mo:
        import time as _t
        mins = max(0, int((mo.get("until", 0) - _t.time()) / 60))
        ov_status = f'⚡ MANUAL {mo.get("mode","?").upper()} active — ~{mins} min left'
    else:
        ov_status = "no manual override — running on auto policy"
    fcbtns = "".join(f'<button onclick="ov(\'/api/force_charge?h={h}\')">{h}h</button>' for h in (1, 2, 3, 4, 5, 6))
    sellbtns = "".join(f'<button class=danger onclick="if(confirm(\'Force-discharge (SELL) to grid for {h}h?\'))ov(\'/api/sell?h={h}\')">{h}h</button>' for h in (1, 2, 3, 4, 5, 6))
    dyn2 = snap.get("dynamic") or {}
    sell_p = dyn2.get("sell_price"); surv = dyn2.get("survival_soc")
    auto_sell_txt = (f'auto-sell ≥ ${sell_p} (keep ≥{surv}% overnight)' if dyn2.get("sell_enabled")
                     else "auto-sell off")
    controls_html = (f'<h3>Quick controls <small>(manual overrides — auto-revert when the timer ends)</small></h3>'
                     f'<div class=card><div><b>Status:</b> {ov_status} · buy ≤ ${round(eff_floor,3)}'
                     f'{" (override)" if ov.get("floor") is not None else ""} · {auto_sell_txt}</div>'
                     f'<div style="margin-top:.5rem">⚡ <b>Force-charge:</b> {fcbtns}</div>'
                     f'<div style="margin-top:.4rem">💰 <b>SELL (discharge to grid):</b> {sellbtns}</div>'
                     f'<div style="margin-top:.4rem">🪙 <b>Floor:</b> '
                     f'<button onclick="ov(\'/api/floor?delta=-0.03\')">– relax</button>'
                     f'<button onclick="ov(\'/api/floor?delta=0.03\')">+ increase</button> '
                     f'<button onclick="ov(\'/api/cancel_override\')">✖ cancel override → auto</button></div></div>')
    base_buy = ov.get("floor") if ov.get("floor") is not None else cfg["strategy"].get("charge_start_floor", 0.0)
    base_sell = ov.get("sell") if ov.get("sell") is not None else cfg["strategy"].get("sell_price", 0.50)
    baseline_html = (f'<h3>Set baseline <small>(permanent buy/sell thresholds — saved here, no need for the '
                     f'add-on config page)</small></h3>'
                     f'<div class=card>'
                     f'<label>Buy floor $ <input id=bfloor type=number step=0.01 value="{round(base_buy,3)}" '
                     f'style="width:6em"></label> <small>always willing to grid-charge at/below this</small><br>'
                     f'<label style="display:inline-block;margin-top:.4rem">Sell threshold $ '
                     f'<input id=bsell type=number step=0.01 value="{round(base_sell,3)}" style="width:6em"></label> '
                     f'<small>auto-sell when feed-in ≥ this</small><br>'
                     f'<button style="margin-top:.5rem" onclick="saveBaseline()">Set baseline</button></div>')
    if dyn.get("mode") == "zerohero":
        dyn_html = (f'<div class="card" style="border-color:#2ecc71"><small>⚙️ ZEROHERO MODE (GloBird)</small>'
                    f'<div>free-charge 11:00–14:00 → {dyn.get("max_soc","?")}% · zero-import + export 18:00–21:00</div>'
                    f'<small>keep ≥{dyn.get("survival_soc","?")}% overnight (survival to next free window) · '
                    f'battery {bat.get("stored_kwh","?")}/{bat.get("capacity_kwh","?")}kWh · feed-in ${snap.get("feedin","?")}</small></div>')
    else:
        dyn_html = (f'<div class="card"><small>⚙️ FOUNDATION + DYNAMIC POLICY (Amber)</small>'
                    f'<div>charge ≤ <b>${dyn.get("charge_start_price","?")}</b> · target <b>{dyn.get("target_soc","?")}%</b> '
                    f'<small>(set by {dyn.get("source","static")})</small></div>'
                    f'<small>floor ${dyn.get("charge_start_floor","?")} ≤ charge ≤ ceiling ${dyn.get("price_ceiling","?")} · '
                    f'max SoC {dyn.get("max_soc","?")}% · battery {bat.get("stored_kwh","?")}/{bat.get("capacity_kwh","?")}kWh · '
                    f'feed-in ${snap.get("feedin","?")}</small></div>')
    llm = snap.get("llm")
    if llm:
        verdict = {"AGREE": "✅ AGREE", "REFINE": "🔧 REFINE",
                   "DISAGREE": "🔍 DISAGREE"}.get(llm.get("rating"), "⚠️ n/a")
        act = (llm.get("operator_action") or "").strip()
        act_html = (f'<div style="margin-top:.4rem;padding:.5rem .7rem;background:#fff3cd;color:#5c4600;'
                    f'border-radius:8px"><b>📣 Needs you:</b> {act}</div>') if act else ""
        llm_html = (f'<div class="card" style="border-color:#9c6ade"><small>🤖 DYNAMIC LLM ({llm.get("model","")} · {llm.get("ts","")})</small>'
                    f'<div class=big>{verdict}</div><div>{llm.get("text","")}</div>{act_html}</div>')
    else:
        llm_html = '<div class="card"><small>🤖 Dynamic LLM</small><div>off (enable llm_review + set API key in add-on options)</div></div>'
    llm_html = dyn_html + llm_html
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta http-equiv=refresh content=60><title>foxctl</title><style>{CSS}</style></head><body>
<h1>foxctl — {snap.get('ts','-')}</h1>
<div class=row>
 <div class=card><small>Amber price</small><div class=big>${snap.get('price')}</div>
   <span class=pill style="background:{color}">{band}</span></div>
 <div class=card><small>AEMO (wholesale)</small><div class=big>${snap.get('aemo_price')}</div></div>
 <div class=card><small>Feed-in (export)</small><div class=big>{('$'+str(snap.get('feedin'))) if snap.get('feedin') is not None else 'n/a'}</div><small>{'solar offload' if snap.get('feedin') is not None else 'awaiting solar'}</small></div>
 <div class=card><small>Battery SoC</small><div class=big>{round(snap.get('soc',0))}%</div></div>
 <div class=card><small>Solar (PV)</small><div class=big>{snap.get('pv_kw')} kW</div></div>
 <div class=card><small>Solar forecast</small><div class=big>{(snap.get('solar_forecast') or {}).get('today_total','?')} <small>kWh today</small></div><small>{(snap.get('solar_forecast') or {}).get('remaining_today','?')} left · tomorrow {(snap.get('solar_forecast') or {}).get('tomorrow','?')}</small></div>
 <div class=card><small>Usage (rolling avg)</small><div class=big>{(snap.get('consumption') or {}).get('avg_daily_total_kwh') if (snap.get('consumption') or {}).get('days_sampled') else '–'} <small>kWh/day</small></div><small>{(snap.get('consumption') or {}).get('days_sampled',0)}d · EV {(snap.get('consumption') or {}).get('avg_daily_ev_kwh','0')} · today {(snap.get('consumption') or {}).get('today_so_far_kwh','0')}</small></div>
 <div class=card><small>Demand window</small><div class=big>{'ACTIVE' if snap.get('demand_window') else 'off'}</div><small>{'no demand charge (EA116) — OK to charge if cheap' if snap.get('demand_window') else ''}</small></div>
 <div class=card><small>Work mode</small><div class=big>{snap.get('work_mode')}</div></div>
 <div class=card style="{'background:#fff3e0;border-color:#e67e22' if 'stale' in (snap.get('telemetry_source') or '') or 'down' in (snap.get('telemetry_source') or '') else ''}"><small>Data age / source</small>
   <div class=big><span id=age>{snap.get('data_age_s') if snap.get('data_age_s') is not None else '–'}</span>s
   · <span id=countdown>–</span></div>
   <small>{'⚠️ STALE — control on hold' if ('stale' in (snap.get('telemetry_source') or '') or 'down' in (snap.get('telemetry_source') or '')) else 'FoxESS direct (sole poller)'}</small></div>
 <div class=card style="{'background:#fff3e0;border-color:#e67e22' if (snap.get('scheduler') or {}).get('active') and snap['scheduler']['active']['mode']=='ForceCharge' else ''}">
   <small>Force-charge</small><div class=big>{('⚡ ON' if (snap.get('scheduler') or {}).get('active') and snap['scheduler']['active']['mode']=='ForceCharge' else ('sched on' if (snap.get('scheduler') or {}).get('enabled') else 'OFF'))}</div>
   <small>{(snap.get('scheduler') or {}).get('active',{}).get('window','') if (snap.get('scheduler') or {}).get('active') else ''}</small></div>
</div>
<div class="card rec"><small>RECOMMENDATION</small>
 <div class=big>{rec.get('action')} → {rec.get('target_mode')} {'⚡FORCE-CHARGE' if rec.get('force_charge') else ''}</div>
 <div>{rec.get('reason','')}</div>
 <div><small>applied: {snap.get('applied')} · control: allow={ctrl.get('allow_control')} auto_apply={ctrl.get('auto_apply')} force_charge={ctrl.get('set_force_charge')}</small></div>
</div>
{llm_html}
{note_html}
{controls_html}
<h3>Forecast horizon <small>(what the policy reasons over — 18h ahead · drag the corner ↘ to resize)</small></h3>
<div class=card style="padding:.5rem"><div id=chartwrap class=chartwrap>{render_forecast_svg(snap, cfg)}</div></div>
<h3>Make things happen</h3>
<button onclick="post('/api/evaluate')">Evaluate now</button>
<button onclick="post('/api/apply')">Apply recommendation</button>
<button onclick="post('/api/review')">🤖 LLM review now</button>
<button class=danger onclick="if(confirm('Grid-charge for 10 min to {cfg['strategy']['target_soc']}%?'))post('/api/force_charge_test')">⚡ Test force-charge (10 min)</button>
<button class=danger onclick="post('/api/scheduler_off')">Stop / disable scheduler</button>
<div id=msg></div>
<h3>Actions taken <small>(real changes: applies, force-charge, disables, band actions)</small></h3>
<table id=events></table>
<h3>Next forecast</h3>{atable}
<h3>Decision log <small>(every cycle, recommendation even when not applied)</small></h3>
<table id=log></table>
{baseline_html}
<p><small>auto-refresh 60s · <a href=/api/state>/api/state</a> · <a href=/api/log?n=100>/api/log</a></small></p>
<script>{JS}</script>
</body></html>"""


def make_handler(cfg):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code, body, ctype="text/html"):
            b = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path.startswith("/api/state"):
                with LAST_LOCK:
                    self._send(200, json.dumps(LAST, default=str), "application/json")
            elif self.path.startswith("/api/log"):
                n = 50
                if "n=" in self.path:
                    try:
                        n = int(self.path.split("n=")[1].split("&")[0])
                    except Exception:
                        pass
                self._send(200, json.dumps(read_log(n), default=str), "application/json")
            elif self.path.startswith("/api/events"):
                n = 50
                if "n=" in self.path:
                    try:
                        n = int(self.path.split("n=")[1].split("&")[0])
                    except Exception:
                        pass
                self._send(200, json.dumps(read_events(n), default=str), "application/json")
            elif self.path == "/" or self.path.startswith("/index"):
                with LAST_LOCK:
                    snap = dict(LAST)
                self._send(200, render(snap, cfg) if snap else "<p>warming up… refresh shortly</p>")
            else:
                self._send(404, "not found")

        def do_POST(self):
            log_action(f"POST {self.path} from {self.client_address[0]}")
            if self.path.startswith("/api/evaluate"):
                snap = run_once(cfg, do_apply=False)
                self._send(200, json.dumps(snap, default=str), "application/json")
            elif self.path.startswith("/api/apply"):
                with LAST_LOCK:
                    snap = dict(LAST)
                if not snap:
                    snap = run_once(cfg, do_apply=False)
                msg = apply_and_record(cfg, snap)    # persist so the dashboard header reflects the apply
                self._send(200, json.dumps({"applied": msg}, default=str), "application/json")
            elif self.path.startswith("/api/force_charge_test"):
                try:
                    msg = force_charge_test(cfg, 10)
                except Exception as e:
                    msg = f"ERROR: {e}"
                log_action(f"force_charge_test -> {msg}")
                self._send(200, json.dumps({"force_charge_test": msg}, default=str), "application/json")
            elif self.path.startswith("/api/scheduler_off"):
                try:
                    msg = scheduler_off(cfg)
                except Exception as e:
                    msg = f"ERROR: {e}"
                log_action(f"scheduler_off -> {msg}")
                self._send(200, json.dumps({"scheduler_off": msg}, default=str), "application/json")
            elif self.path.startswith("/api/review"):
                ctx = _LLM.get("last_ctx")
                if not ctx:
                    run_once(cfg, do_apply=False)
                    ctx = _LLM.get("last_ctx")
                v = maybe_llm_review(cfg, ctx, force=True) if ctx else None
                with LAST_LOCK:
                    LAST["llm"] = v
                self._send(200, json.dumps(v or {"text": "LLM review disabled / no key"}, default=str), "application/json")
            elif self.path.startswith("/api/note"):
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(n).decode()) if n else {}
                    text = set_note(cfg, body.get("text", ""))
                    snap = run_once(cfg, do_apply=False)   # re-evaluate immediately with the note
                    rec = snap.get("recommendation", {})
                    self._send(200, json.dumps({"note": text or "(cleared)",
                                                "now": f"{rec.get('action')} fc={rec.get('force_charge')}",
                                                "charge_start": (snap.get('dynamic') or {}).get('charge_start_price')},
                                               default=str), "application/json")
                except Exception as e:
                    self._send(200, json.dumps({"error": str(e)}), "application/json")
            elif self.path.startswith("/api/force_charge") or self.path.startswith("/api/sell"):
                try:
                    q = self.path.split("?", 1)[1] if "?" in self.path else ""
                    h = 1
                    for kv in q.split("&"):
                        if kv.startswith("h="):
                            h = max(1, min(6, int(float(kv[2:]))))
                    strat = cfg["strategy"]
                    pwr = strat.get("force_charge_power_kw", 10.5)
                    if self.path.startswith("/api/sell"):
                        set_manual(cfg, "sell", h, pwr, strat.get("reserve_soc", 20))
                    else:
                        set_manual(cfg, "charge", h, pwr, strat.get("min_soc_on_grid", 10),
                                   cap=strat.get("max_soc", 90))
                    snap = run_once(cfg, do_apply=True)
                    self._send(200, json.dumps({"override": _OV["manual"], "applied": snap.get("applied")},
                                               default=str), "application/json")
                except Exception as e:
                    self._send(200, json.dumps({"error": str(e)}), "application/json")
            elif self.path.startswith("/api/floor"):
                try:
                    q = self.path.split("?", 1)[1] if "?" in self.path else ""
                    delta = 0.0
                    for kv in q.split("&"):
                        if kv.startswith("delta="):
                            delta = float(kv[6:])
                    strat = cfg["strategy"]
                    cur = _OV["floor"] if _OV["floor"] is not None else strat.get("charge_start_floor", 0.0)
                    new = set_floor_override(cfg, cur + delta, strat.get("price_ceiling", 0.20))
                    snap = run_once(cfg, do_apply=False)
                    self._send(200, json.dumps({"charge_start_floor": new,
                                                "charge_start": (snap.get("dynamic") or {}).get("charge_start_price")},
                                               default=str), "application/json")
                except Exception as e:
                    self._send(200, json.dumps({"error": str(e)}), "application/json")
            elif self.path.startswith("/api/baseline"):
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(n).decode()) if n else {}
                    floor = body.get("floor"); sell = body.get("sell")
                    floor = float(floor) if floor not in (None, "") else None
                    sell = float(sell) if sell not in (None, "") else None
                    res = set_baseline(cfg, floor, sell, cfg["strategy"].get("price_ceiling", 0.20))
                    run_once(cfg, do_apply=False)
                    self._send(200, json.dumps({"baseline": res}, default=str), "application/json")
                except Exception as e:
                    self._send(200, json.dumps({"error": str(e)}), "application/json")
            elif self.path.startswith("/api/cancel_override"):
                try:
                    set_manual(cfg, None, 0, 0, 0)
                    if cfg["control"].get("allow_control"):
                        FoxESS(cfg["foxess"]["token"], cfg["foxess"]["sn"]).disable_scheduler()
                    _CHARGE["until"] = 0.0
                    snap = run_once(cfg, do_apply=True)
                    self._send(200, json.dumps({"cancelled": True, "applied": snap.get("applied")},
                                               default=str), "application/json")
                except Exception as e:
                    self._send(200, json.dumps({"error": str(e)}), "application/json")
            else:
                self._send(404, "not found")
    return H


def serve(cfg: dict):
    Thread(target=loop, args=(cfg,), daemon=True).start()
    host, port = cfg["web"]["host"], cfg["web"]["port"]
    httpd = ThreadingHTTPServer((host, port), make_handler(cfg))
    print(f"foxctl web on http://{host}:{port}  (loop every {cfg['poll_seconds']}s)")
    httpd.serve_forever()


def refresh_control(cfg: dict) -> bool:
    """Reload control flags from disk into cfg["control"] so toggling allow_control / auto_apply in the
    config takes effect without restarting the process. Best-effort: keep current values if the file is
    missing or mid-write. Only the control block is refreshed — in-memory tuned params (cfg["strategy"])
    are left untouched. Returns the effective auto-apply flag (allow_control AND auto_apply)."""
    try:
        disk_ctrl = json.loads(CONFIG_PATH.read_text()).get("control", {})
        cfg["control"].update({k: disk_ctrl[k] for k in cfg["control"] if k in disk_ctrl})
    except Exception as e:
        print(f"{datetime.now().isoformat(timespec='seconds')} control reload skipped: {e}", file=sys.stderr)
    return bool(cfg["control"].get("allow_control") and cfg["control"].get("auto_apply"))


def loop(cfg: dict):
    poll = cfg["poll_seconds"]
    lag = int(cfg.get("sync_lag_seconds", 20))  # read shortly AFTER foxess-ha refreshes
    while True:
        auto = refresh_control(cfg)
        try:
            snap = run_once(cfg, do_apply=auto)
            r = snap["recommendation"]
            print(f"{snap['ts']} price={snap['price']} soc={snap['soc']:.0f}% "
                  f"mode={snap['work_mode']} -> {r['action']}/{r['target_mode']}"
                  + (f" applied={snap['applied']}" if auto else ""))
        except Exception as e:
            snap = None
            print(f"{datetime.now().isoformat(timespec='seconds')} ERROR: {e}", file=sys.stderr)
        # Sync next poll to the foxess-ha update cycle ONLY when that sensor is fresh — otherwise
        # (frozen integration → FoxESS fallback) anchoring to its dead timestamp drifts the cadence
        # and the price goes stale. In that case just poll on a steady fixed interval.
        now = time.time()
        ts = (snap or {}).get("soc_updated_epoch")
        tsrc = (snap or {}).get("telemetry_source")
        stale_s = cfg.get("telemetry_stale_s", 900)
        if ts and tsrc == "HA" and (now - ts) < stale_s:
            nxt = ts + poll + lag
            while nxt <= now + 5:
                nxt += poll
            sleep = nxt - now
        else:
            sleep = poll
        with LAST_LOCK:
            if LAST:
                LAST["next_poll_epoch"] = now + sleep
        end = time.monotonic() + sleep
        while time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))


# ------------------------------------------------------------------- cli -----

def main(argv=None):
    ap = argparse.ArgumentParser(description="price-aware FoxESS work-mode controller")
    ap.add_argument("cmd", nargs="?", default="status",
                    choices=["status", "recommend", "apply", "loop", "serve"])
    ap.add_argument("--init", action="store_true", help="write a starter config and exit")
    ap.add_argument("--json", action="store_true", help="JSON output for status/recommend")
    args = ap.parse_args(argv)

    if args.init:
        write_default_config(); return 0

    cfg = load_config()

    if args.cmd in ("status", "recommend"):
        snap = run_once(cfg, do_apply=False)
        if args.json:
            print(json.dumps(snap, indent=2, default=str)); return 0
        r = snap["recommendation"]
        print(f"[{snap['ts']}]")
        print(f"  Amber price : ${snap['price']}  ({snap['descriptor']})")
        print(f"  AEMO price  : ${snap.get('aemo_price')} (wholesale)")
        print(f"  Feed-in     : {('$'+str(snap.get('feedin'))+' (export/offload)') if snap.get('feedin') is not None else 'n/a (awaiting solar)'}")
        print(f"  Battery SoC : {snap['soc']:.0f}%")
        print(f"  Solar (PV)  : {snap['pv_kw']} kW  (load {snap.get('load_kw')} kW, surplus {snap.get('solar_surplus_kw')} kW)")
        print(f"  Work mode   : {snap['work_mode']}  options={snap['work_mode_options']}")
        sch = snap.get('scheduler') or {}
        act = sch.get('active')
        if act and act.get('mode') == 'ForceCharge':
            fc_str = f"ON {act['window']} cap {act['fdSoc']}% {act['fdPwr']}W"
        elif sch.get('enabled'):
            fc_str = "scheduler ON"
        else:
            fc_str = "off"
        print(f"  Force-charge: {fc_str}")
        print(f"  >> {r['action']} -> {r['target_mode']}  (force_charge={r['force_charge']})")
        print(f"     {r['reason']}")
        if r.get("force_charge_plan"):
            print(f"     plan: {r['force_charge_plan']}")
        print(f"  control: allow={cfg['control']['allow_control']} auto_apply={cfg['control']['auto_apply']}")
        return 0
    if args.cmd == "apply":
        snap = run_once(cfg, do_apply=True)
        print("applied:", snap["applied"]); return 0
    if args.cmd == "loop":
        loop(cfg); return 0
    if args.cmd == "serve":
        serve(cfg); return 0


if __name__ == "__main__":
    sys.exit(main())
