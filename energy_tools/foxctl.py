#!/usr/bin/env python3
"""foxctl — tariff-aware FoxESS work-mode controller.

Gathers the inputs you care about every cycle:
  * Inverter SoC + solar/PV  (FoxESS OpenAPI, authoritative)
  * Current work mode        (FoxESS OpenAPI)
  * Household usage + grid in/out history (FoxESS report/history → forecast)
...runs a transparent GloBird time-of-use decision engine, and RECOMMENDS a work setting. It can
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
        "demand_window_entity": "binary_sensor.home_demand_window",
        # Read inverter telemetry from HA (foxess-ha integration) to avoid a 2nd FoxESS poller.
        "soc_entity": "sensor.foxess_bat_soc",
        "pv_entity": "sensor.foxess_pv_power",
        "load_entity": "sensor.foxess_load_power",
    },
    "strategy": {
        # --- Tariff-driven time-of-use (the ONLY decision model) -------------------------------------
        # The home is on a GloBird time-of-use plan with a FREE midday import window. We grid-charge the
        # battery (and car) only in that window, run off battery through the expensive peak/shoulder, and
        # bank only what the demand estimator says we need to coast to the next free window. Swap plans by
        # changing `tariff_profile`; both profiles live in `tariffs`. No price forecasting.
        "tariff_profile": "zerohero",
        "tariffs": {
            "zerohero": {
                "label": "GloBird ZeroHero",
                "supply_c": 181.5,                                  # daily supply charge (c/day)
                "free": {"start": 11, "end": 14, "free_kwh": 50, "excess_c": 30.8},
                "peak": {"start": 16, "end": 23, "c": 59.4},        # cover from battery, ZERO grid import
                "shoulder_c": 51.7,                                 # everything outside free + peak
                "fit_peak_c": 2.0, "fit_else_c": 0.0,              # feed-in tariff (export earnings)
                "export": {"start": 18, "end": 21, "c": 10.0, "cap_kwh": 15},  # Super Export window
            },
            "four4free": {
                "label": "GloBird Four4Free",
                "supply_c": 134.2,
                "free": {"start": 10, "end": 14, "free_kwh": 50, "excess_c": 26.4},
                "peak": {"start": 16, "end": 23, "c": 59.95},
                "shoulder_c": 37.51,
                "fit_peak_c": 8.0, "fit_else_c": 0.0,
                # $1/day credit for keeping export ≤0.03 kWh/hr in 18–21 — honoured for free (export off).
                "export_credit": {"start": 18, "end": 21, "max_kwh_per_h": 0.03, "dollar_per_day": 1.0},
            },
        },
        "max_soc": 100,             # hard charge cap — never grid-charge above this
        "reserve_soc": 20,          # software coast floor — plan never drains below this overnight
        "battery_capacity_kwh": 41.44,  # usable pack (4 batteries)
        "typical_daily_load_kwh": 30,   # fallback until enough consumption history is sampled
        "force_charge_minutes": 120,    # max force-charge window length (safety cap); re-evaluated each cycle
        "force_charge_power_kw": 13.5,  # inverter max grid charge rate (13500 W)
        # Temperature nudge: scale the predicted coast load up in hot/cold weather (HVAC). Gentle in v1;
        # tune per_c once temp↔load history accumulates. mild_c is the no-nudge baseline temperature.
        "temp_mild_c": 20.0, "temp_hot_c": 28.0, "temp_cold_c": 12.0,
        "temp_per_c_hot": 0.015, "temp_per_c_cold": 0.020, "temp_nudge_max": 0.40,
        "min_soc_on_grid": 10,
        # The ONLY min-SoC foxctl ever writes to the inverter — a constant safety floor, never a
        # computed survival level. Keep it low and matching the FoxESS app's own min-SoC; survival is
        # enforced in software (when to stop charging/selling), never on the device.
        "inverter_min_soc": 10,
        # Export (feed-in) is OFF by default — feed-in is poor on these plans. Turn on per profile's
        # export window only if sell_enabled. Selling never drains below the coast floor.
        "sell_enabled": False,
    },
    "control": {
        "allow_control": False,     # master switch for ANY write to the inverter
        "auto_apply": False,        # let the loop apply without a human pressing apply
        "set_work_mode": True,      # may change work mode
        "set_force_charge": False,  # may push grid force-charge windows (more invasive)
    },
    # Push notifications when a decision is worth a human look (via HA notify service).
    "notify": {"enabled": False, "service": "notify.mobile_app_phoney"},
    # Solar diversion: turn a car-charger power point ON when export is too cheap to bother to sell,
    # OFF otherwise. Needs control.allow_control. switch="" disables. Tracked via ev_power_entity.
    "ev_divert": {"switch": "", "feedin_max": 0.10, "allow_grid": True,
                  "min_export_kw": 1.0, "min_dwell_min": 10,
                  "battery_priority": True, "min_soc": 0,
                  # Interim daily car cap (until a real car-SoC sensor exists): auto-divert charges up to
                  # this many kWh/day then stops; resets ~4am or when you press Force car charge. 0 = off.
                  "session_cap_kwh": 30},
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

# Last FoxESS API error (for the dashboard rate-limit banner). ok_ts beats ts → recovered.
_FOX_STATUS = {"err": None, "ts": 0.0, "rate_limited": False, "ok_ts": 0.0}


def _note_fox_status(msg, rate_limited=False):
    _FOX_STATUS.update(err=msg, ts=time.time(), rate_limited=rate_limited)


def fox_error_status():
    """Return the current FoxESS error state if calls are presently failing (else None)."""
    if _FOX_STATUS["ts"] and _FOX_STATUS["ts"] > _FOX_STATUS["ok_ts"] and (time.time() - _FOX_STATUS["ts"]) < 900:
        return {"msg": _FOX_STATUS["err"], "rate_limited": _FOX_STATUS["rate_limited"],
                "age": int(time.time() - _FOX_STATUS["ts"])}
    return None


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
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                raw = r.read().decode().strip()
        except urllib.error.HTTPError as e:
            _note_fox_status(f"HTTP {e.code}", rate_limited=(e.code == 429))
            raise
        except Exception as e:
            _note_fox_status(str(e)[:100])
            raise
        if not raw:
            _FOX_STATUS["ok_ts"] = time.time()
            return {"errno": 0, "msg": "empty"}   # some setters return 200 + no body on success
        d = json.loads(raw)
        if d.get("errno") not in (0, None):
            errno, msg = d.get("errno"), str(d.get("msg") or "")
            rl = errno in (40256, 40400, 41807) or any(k in msg.lower()
                 for k in ("frequ", "limit", "frequency", "too many", "exceed"))
            _note_fox_status(f"errno {errno}: {msg}"[:100], rate_limited=rl)
            raise RuntimeError(f"FoxESS {path} errno={errno}: {msg}")
        _FOX_STATUS["ok_ts"] = time.time()
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
        Same scheduler/enable schema as force-charge; fdSoc is the SoC floor to discharge to.

        IMPORTANT: min_soc here is the inverter's CONSTANT safety floor (inverter_min_soc), NOT a
        computed survival level. survival/buffer is enforced in software (the loop stops the window
        when the survival target is reached) — never pushed onto the device, because a high min that
        leaks into SelfUse makes the inverter import expensive grid power to hold it."""
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

        NB: disabling does NOT reset minSocOnGrid on the device — the last group's value persists.
        That is exactly why foxctl never writes a value above the constant inverter_min_soc: so a
        reverted SelfUse can never be stranded holding a high floor (the cause of the 66% import bug).
        """
        return self.call("/op/v0/device/scheduler/set/flag", {"deviceSN": self.sn, "enable": 0})

    def get_min_soc(self):
        """Read the inverter's configured grid min-SoC (%), best-effort. Used only to DETECT a stranded
        high floor (e.g. a legacy 66%) and warn — foxctl never raises it. Returns int or None."""
        for key in ("MinSocOnGrid", "MinSoc"):
            try:
                v = (self.call("/op/v0/device/setting/get", {"sn": self.sn, "key": key})
                     .get("result") or {}).get("value")
                if v is not None:
                    return int(float(v))
            except Exception:
                continue
        return None


class HAClient:
    """Reads Home Assistant entity states (reuses the HA token). Used for the EV plug, demand window,
    weather, Forecast.Solar planes, and sun.sun — no price data (the Amber apparatus is gone)."""

    def __init__(self, url: str, token: str):
        self.url, self.token = url.rstrip("/"), token

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


# ---------------------------------------------------------------- engine -----

def _parse_t(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None



# ---------------------------------------------------------------- runtime ----

LAST: dict = {}
LAST_LOCK = Lock()
_WM = {"value": None, "options": None, "i": 0, "ts": 0.0, "min_soc": None}  # work-mode + device min-SoC cache
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
    totals = [p["load"] for p in past]
    avg_total = round(sum(totals) / n, 1) if n else None
    min_total = round(min(totals), 1) if totals else None
    max_total = round(max(totals), 1) if totals else None
    avg_ev = round(sum(p["ev"] for p in past) / n, 1) if n else None
    avg_base = round(avg_total - avg_ev, 1) if avg_total is not None else None
    # Hour-of-day base-load profile: avg (+ min/max range) kWh per hour across complete past days.
    hour_profile, hour_min, hour_max, hp_days = {}, {}, {}, [p for p in past if p.get("hours")]
    if hp_days:
        for h in range(24):
            vals = [p["hours"].get(str(h), 0.0) for p in hp_days]
            hour_profile[h] = round(sum(vals) / len(vals), 3)
            hour_min[h], hour_max[h] = round(min(vals), 3), round(max(vals), 3)
    tk = _CONS["days"].get(today, {})
    return {"days_sampled": n, "avg_daily_total_kwh": avg_total, "avg_daily_ev_kwh": avg_ev,
            "min_daily_total_kwh": min_total, "max_daily_total_kwh": max_total,
            "avg_daily_base_kwh": avg_base, "today_so_far_kwh": round(tk.get("load", 0.0), 1),
            "hour_profile": hour_profile, "hour_min": hour_min, "hour_max": hour_max,
            "profile_days": len(hp_days)}


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


# ---- FORECAST STORE: hour-of-day load (report `loads`) + solar (history `pvPower`) from FoxESS -----
# Phase 2: backfill real per-hour history so the load/solar profiles are accurate in days, not the
# ~2 weeks the self-integrated profile needs. Read-only API. `generation` in the report is inverter
# throughput (incl. battery), NOT PV — so solar comes from integrating pvPower, verified by the probe.
_FCAST = {"path": None, "days": {}, "loaded": False, "last_fill_ts": 0.0}
FCAST_BACKFILL_DAYS = 21      # how many past days to keep / backfill
FCAST_MIN_DAYS = 3            # prefer the FoxESS profile over self-integration once we have this many
FCAST_FILL_GAP_S = 120        # fetch at most one backfill day per this interval (quota-friendly)


def _fcast_path(cfg):
    return _state_dir(cfg) / "forecast_store.json"


def load_fcast(cfg):
    if _FCAST["loaded"]:
        return
    _FCAST["path"] = _fcast_path(cfg)
    try:
        _FCAST["days"] = json.loads(_FCAST["path"].read_text()).get("days", {})
    except Exception:
        pass
    _FCAST["loaded"] = True


def save_fcast(cfg):
    try:
        _FCAST["path"].parent.mkdir(parents=True, exist_ok=True)
        _FCAST["path"].write_text(json.dumps({"days": _FCAST["days"]}))
    except Exception as e:
        print(f"forecast store persist failed: {e}", file=sys.stderr)


def _hist_time(ts):
    """Parse a FoxESS history timestamp ('2026-06-20 13:05:00 AEST+1000') → naive local datetime."""
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _integrate_hourly(points):
    """Power samples (kW) → 24-element hourly kWh, trapezoidal over the real sample gaps."""
    hourly = [0.0] * 24
    pt = pv = None
    for p in points:
        t = _hist_time(p.get("time") or "")
        v = p.get("value")
        if t is None or not isinstance(v, (int, float)):
            continue
        if pt is not None:
            dt_h = (t - pt).total_seconds() / 3600.0
            if 0 < dt_h <= 0.5:                 # skip big gaps (restart/downtime)
                hourly[pt.hour] += (pv + v) / 2.0 * dt_h
        pt, pv = t, v
    return [round(x, 3) for x in hourly]


def fetch_forecast_day(fox, day):
    """One past day → hourly kWh arrays (24 each) for load, grid import, grid export (one report call
    covering the `loads`/`gridConsumption`/`feedin` stats) and solar (integrated pvPower history)."""
    def _vals(it):
        v = (list(it.get("values") or []) + [0.0] * 24)[:24]
        return [round(float(x), 3) if isinstance(x, (int, float)) else 0.0 for x in v]
    by = {}
    for it in fox.report(["loads", "gridConsumption", "feedin"], "day", day):
        by[it.get("variable")] = _vals(it)
    load_hours = by.get("loads", [0.0] * 24)
    grid_in_hours = by.get("gridConsumption", [0.0] * 24)   # grid import
    grid_out_hours = by.get("feedin", [0.0] * 24)           # grid export
    begin = int(datetime(day.year, day.month, day.day).timestamp() * 1000)
    solar_hours = [0.0] * 24
    res = fox.history(["pvPower"], begin, begin + 24 * 3600 * 1000)
    for ds in ((res[0].get("datas") if res else None) or []):
        if ds.get("variable") == "pvPower":
            solar_hours = _integrate_hourly(ds.get("data") or [])
    return load_hours, solar_hours, grid_in_hours, grid_out_hours


def forecast_profiles():
    """Hour-of-day average load + solar (kWh) across the stored days, with the load min/max range.
    Excludes no-data days PER METRIC: a day whose total for that metric is ~0 is treated as missing
    (pre-install / no telemetry) and left out of the average — otherwise the backfill window's empty
    early days drag the means down."""
    days = list(_FCAST["days"].values())
    def valid_for(key):
        return [d for d in days if isinstance(d.get(key), list) and len(d[key]) == 24
                and sum(x for x in d[key] if isinstance(x, (int, float))) > 0.05]
    def stats(key):
        vd = valid_for(key)
        avg, lo, hi = {}, {}, {}
        for h in range(24):
            vals = [d[key][h] for d in vd if isinstance(d[key][h], (int, float))]
            if vals:
                avg[h] = round(sum(vals) / len(vals), 3)
                lo[h], hi[h] = round(min(vals), 3), round(max(vals), 3)
        return avg, lo, hi
    def daily_totals(key):
        vd = valid_for(key)
        tot = [round(sum(x for x in d[key] if isinstance(x, (int, float))), 1) for d in vd]
        series = {"days": len(tot), "series": tot}
        if tot:
            series.update(avg=round(sum(tot) / len(tot), 1), min=min(tot), max=max(tot))
        return series
    load_avg, load_min, load_max = stats("load")
    solar_avg, solar_min, solar_max = stats("solar")
    gin_avg, _, _ = stats("grid_in")
    gout_avg, _, _ = stats("grid_out")
    lvalid = valid_for("load")
    return {"days": len(lvalid), "days_solar": len(valid_for("solar")),
            "load_profile": load_avg, "load_min": load_min, "load_max": load_max,
            "solar_profile": solar_avg, "solar_min": solar_min, "solar_max": solar_max,
            "grid_in_profile": gin_avg, "grid_out_profile": gout_avg,
            # per-metric daily totals (for the historical bar trend) + back-compat `daily_total`
            "daily": {"load": daily_totals("load"), "solar": daily_totals("solar"),
                      "grid_in": daily_totals("grid_in"), "grid_out": daily_totals("grid_out")},
            "daily_total": daily_totals("load")}


def update_forecast_store(cfg, fox):
    """Ensure the last FCAST_BACKFILL_DAYS complete days are stored. Fetches at most ONE missing day
    per call AND no more than one per FCAST_FILL_GAP_S — so a backfill spreads over many cycles and
    stays well inside the FoxESS daily API quota. Read-only. Returns the current profiles."""
    load_fcast(cfg)
    want = [(datetime.now() - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(1, FCAST_BACKFILL_DAYS + 1)]
    for k in [k for k in _FCAST["days"] if k not in want]:   # prune anything older than the window
        _FCAST["days"].pop(k, None)
    missing = [d for d in want if d not in _FCAST["days"]]
    if missing and (time.time() - _FCAST["last_fill_ts"]) >= FCAST_FILL_GAP_S:
        _FCAST["last_fill_ts"] = time.time()
        d = missing[0]
        try:
            lh, sh, gi, go = fetch_forecast_day(fox, datetime.strptime(d, "%Y-%m-%d"))
            _FCAST["days"][d] = {"load": lh, "solar": sh, "grid_in": gi, "grid_out": go}
            save_fcast(cfg)
            log_event("forecast", f"backfilled {d}: load={round(sum(lh),1)}kWh solar={round(sum(sh),1)}kWh "
                                  f"grid_in={round(sum(gi),1)} grid_out={round(sum(go),1)}kWh "
                                  f"({len(_FCAST['days'])}/{FCAST_BACKFILL_DAYS} days)")
        except Exception as e:
            print(f"forecast backfill {d} failed: {e}", file=sys.stderr)
    return forecast_profiles()


# ---- SOLAR FORECAST CALIBRATION (Phase 3): learn external-forecast-vs-actual bias per site ---------
# The external (Forecast.Solar/Solcast) entities give a forward daily total; the forecast store gives
# the ACTUAL generation (integrated pvPower) once a day completes. Pairing them over time yields a
# bias = mean(actual)/mean(forecast) that corrects this site's systematic optimism/pessimism. Applied
# (clamped, and only after enough samples) to the forward solar feeding survival/shortfall + projection.
_SOLAR_CAL = {"path": None, "fc": {}, "samples": [], "loaded": False}
SOLAR_CAL_MIN = 3               # need this many completed forecast-vs-actual days before applying
SOLAR_CAL_CLAMP = (0.5, 1.6)    # never trust the correction beyond ±this


def _scal_path(cfg):
    return _state_dir(cfg) / "solar_cal.json"


def load_scal(cfg):
    if _SOLAR_CAL["loaded"]:
        return
    _SOLAR_CAL["path"] = _scal_path(cfg)
    try:
        d = json.loads(_SOLAR_CAL["path"].read_text())
        _SOLAR_CAL["fc"], _SOLAR_CAL["samples"] = d.get("fc", {}), d.get("samples", [])
    except Exception:
        pass
    _SOLAR_CAL["loaded"] = True


def save_scal(cfg):
    try:
        _SOLAR_CAL["path"].parent.mkdir(parents=True, exist_ok=True)
        _SOLAR_CAL["path"].write_text(json.dumps({"fc": _SOLAR_CAL["fc"], "samples": _SOLAR_CAL["samples"][-60:]}))
    except Exception as e:
        print(f"solar cal persist failed: {e}", file=sys.stderr)


def update_solar_cal(cfg, today_forecast_total):
    """Record today's external full-day solar forecast, then pair any completed day's forecast with its
    actual generation (from the forecast store) into a calibration sample. Returns
    {bias, samples, mae_kwh, applied} — bias is 1.0 (no-op) until SOLAR_CAL_MIN samples exist."""
    load_scal(cfg)
    load_fcast(cfg)
    today = datetime.now().strftime("%Y-%m-%d")
    if isinstance(today_forecast_total, (int, float)) and today_forecast_total >= 0:
        _SOLAR_CAL["fc"][today] = round(float(today_forecast_total), 2)
    have = {s["d"] for s in _SOLAR_CAL["samples"]}
    changed = False
    for d, fc in list(_SOLAR_CAL["fc"].items()):
        if d == today or d in have or not fc or fc <= 0:
            continue
        day = _FCAST["days"].get(d)
        if day and isinstance(day.get("solar"), list):
            act = round(sum(x for x in day["solar"] if isinstance(x, (int, float))), 2)
            if act <= 0.05:        # no-data / pre-panel day → not a valid forecast-vs-actual sample
                continue
            _SOLAR_CAL["samples"].append({"d": d, "fc": fc, "act": act})
            have.add(d)
            changed = True
    keep = {(datetime.now() - timedelta(days=k)).strftime("%Y-%m-%d") for k in range(0, 35)}
    for k in [k for k in _SOLAR_CAL["fc"] if k not in keep]:
        _SOLAR_CAL["fc"].pop(k, None)
        changed = True
    _SOLAR_CAL["samples"] = _SOLAR_CAL["samples"][-60:]
    if changed:
        save_scal(cfg)
    s = _SOLAR_CAL["samples"]
    n = len(s)
    bias, lo, hi = 1.0, 1.0, 1.0
    if n >= SOLAR_CAL_MIN:
        den = sum(x["fc"] for x in s)
        if den > 0:
            bias = max(SOLAR_CAL_CLAMP[0], min(SOLAR_CAL_CLAMP[1], sum(x["act"] for x in s) / den))
        ratios = [x["act"] / x["fc"] for x in s if x["fc"] > 0]
        if ratios:                       # forecast error margin = spread of actual/forecast (clamped)
            lo = round(max(0.2, min(ratios)), 3)
            hi = round(min(2.0, max(ratios)), 3)
    mae = round(sum(abs(x["act"] - x["fc"]) for x in s) / n, 1) if n else None
    return {"bias": round(bias, 3), "lo": lo, "hi": hi, "samples": n, "mae_kwh": mae,
            "applied": n >= SOLAR_CAL_MIN}


# ---- HA LONG-TERM STATISTICS BACKFILL ------------------------------------------------------------
# HA can only ingest HISTORICAL data via the WebSocket recorder/import_statistics API, at HOURLY
# resolution into long-term statistics (5-min raw state history can't be backfilled via any public
# API). We feed the forecast store's hourly load + solar as external statistics foxctl:* so the past
# shows up in HA's statistics/Energy graphs; live 5-min sensors cover "now" going forward.
_STAT_DEFS = {"load": ("foxctl:load_energy", "kWh", "House load energy"),
              "solar": ("foxctl:solar_energy", "kWh", "Solar energy")}


def build_stat_series(days=7):
    """From the forecast store, build hourly cumulative-sum series per metric for HA import_statistics.
    Skips no-data days (per metric). Returns {statistic_id: (unit, name, [(start_local_dt, cum_kwh)])}."""
    keys = sorted(_FCAST["days"].keys())[-days:]
    out = {}
    for metric, (sid, unit, name) in _STAT_DEFS.items():
        series, csum = [], 0.0
        for dk in keys:
            arr = (_FCAST["days"].get(dk) or {}).get(metric)
            if not (isinstance(arr, list) and len(arr) == 24):
                continue
            if sum(x for x in arr if isinstance(x, (int, float))) <= 0.05:
                continue   # no telemetry / pre-install day for this metric → leave a gap
            y, m, d = (int(x) for x in dk.split("-"))
            for h in range(24):
                v = arr[h] if isinstance(arr[h], (int, float)) else 0.0
                csum += max(0.0, v)
                series.append((datetime(y, m, d, h).astimezone(), round(csum, 3)))
        if series:
            out[sid] = (unit, name, series)
    return out


def ha_import_statistics(cfg, series):
    """Push hourly external statistics to HA via the WebSocket recorder/import_statistics API."""
    import websocket  # websocket-client (added to the image)
    base = cfg["ha"]["url"].rstrip("/")
    ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
    token = Path(os.path.expanduser(cfg["ha"]["token_file"])).read_text().strip()
    ws = websocket.create_connection(ws_url, timeout=25)
    try:
        json.loads(ws.recv())                                    # auth_required
        ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth = json.loads(ws.recv())
        if auth.get("type") != "auth_ok":
            raise RuntimeError(f"HA WebSocket auth failed: {auth}")
        mid, done = 0, {}
        for sid, (unit, name, pts) in series.items():
            mid += 1
            stats = [{"start": s.isoformat(), "sum": c} for s, c in pts]
            ws.send(json.dumps({"id": mid, "type": "recorder/import_statistics",
                                "metadata": {"has_mean": False, "has_sum": True, "name": name,
                                             "source": sid.split(":")[0], "statistic_id": sid,
                                             "unit_of_measurement": unit},
                                "stats": stats}))
            resp = json.loads(ws.recv())
            if not resp.get("success"):
                raise RuntimeError(f"import {sid} failed: {resp}")
            done[sid] = len(stats)
        return done
    finally:
        ws.close()


def backfill_ha_statistics(cfg, days=7):
    """One-shot: ensure the last `days` are in the forecast store (fetch any missing), then import them
    into HA as hourly long-term statistics. Returns a per-statistic count of imported points."""
    fox = FoxESS(cfg["foxess"]["token"], cfg["foxess"]["sn"])
    load_fcast(cfg)
    for d in range(1, days + 1):
        day = datetime.now() - timedelta(days=d)
        dk = day.strftime("%Y-%m-%d")
        if dk not in _FCAST["days"]:
            lh, sh, gi, go = fetch_forecast_day(fox, day)
            _FCAST["days"][dk] = {"load": lh, "solar": sh, "grid_in": gi, "grid_out": go}
    save_fcast(cfg)
    series = build_stat_series(days)
    if not series:
        return {}
    done = ha_import_statistics(cfg, series)
    log_event("backfill", f"HA statistics backfill: {done}")
    return done


LOG_PATH = Path(os.environ.get("FOXCTL_LOG", Path.home() / "foxctl/decisions.jsonl"))


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



_NOTIFY = {"last_stale": False, "last_selling": False}

_EV = {"on": None, "last_change": 0.0, "override_until": 0.0, "lowdraw_since": 0.0,
       "session_day": None, "session_start_kwh": None, "capped": False}   # divert + manual force + daily cap


def ha_call_service(cfg, domain, service, entity_id):
    """Call an arbitrary HA service on an entity (e.g. switch.turn_on)."""
    url = cfg["ha"]["url"].rstrip("/")
    token = Path(os.path.expanduser(cfg["ha"]["token_file"])).read_text().strip()
    body = json.dumps({"entity_id": entity_id}).encode()
    req = urllib.request.Request(f"{url}/api/services/{domain}/{service}", data=body, method="POST",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15).read()


def ev_divert_decision(snap, ev):
    """Pure policy: should the car charger be ON this cycle? Diverts spare SOLAR (export ≥ min_export_kw)
    into the car. Yields to the house battery until it reaches the survival floor (battery fills first),
    and never steals power while the inverter is selling or force-charging. (want, why)."""
    feedin_power = snap.get("feedin_power") or 0.0
    soc = snap.get("soc")
    # SAFETY: never divert to the car while the inverter is actively SELLING (export→grid) or
    # FORCE-CHARGING the battery toward a target it's still well below — plugging in would steal that
    # power (drain export revenue / starve the critical battery fill). Yields to the battery scheduler.
    rec = snap.get("recommendation") or {}
    active = (snap.get("scheduler") or {}).get("active") or {}
    if rec.get("force_discharge") or active.get("mode") == "ForceDischarge":
        return False, "battery is selling to grid — car held off (don't redirect export)"
    target = (snap.get("dynamic") or {}).get("target_soc")
    if (rec.get("force_charge") or active.get("mode") == "ForceCharge") and isinstance(soc, (int, float)) \
            and isinstance(target, (int, float)) and soc < target - 5:
        return False, f"battery force-charging to {target}% (now {soc:.0f}%) — car held off"
    if feedin_power < ev.get("min_export_kw", 1.0):
        return False, "no spare solar export"
    # Battery priority: give the spare solar to the battery until it reaches the survival floor before
    # diverting to the car.
    gate = ev.get("min_soc", 0) or 0
    surv = (snap.get("dynamic") or {}).get("survival_soc")
    if ev.get("battery_priority", True) and isinstance(surv, (int, float)):
        gate = max(gate, surv - 2)
    if isinstance(soc, (int, float)) and soc < gate:
        return False, f"battery {soc:.0f}% < target {gate:.0f}% (solar to battery first)"
    return True, f"spare solar {feedin_power:.1f}kW ≥ {ev.get('min_export_kw', 1.0):.1f}kW → car"


def ev_divert_tick(cfg, snap):
    """Drive the car-charger switch from ev_divert_decision, edge-triggered with a dwell so it doesn't
    cycle the charger. Honours control.allow_control. No-op unless ev_divert.switch is configured."""
    ev = cfg.get("ev_divert") or {}
    sw = ev.get("switch")
    if not sw:
        return None
    if not cfg["control"].get("allow_control"):
        return "ev divert: control disabled"
    now = time.time()
    # Interim daily car cap: count kWh delivered since the session start (4am-anchored day); once the
    # cap is hit, hold off auto-divert until the day rolls over or a manual force-charge resets it.
    cap = float(ev.get("session_cap_kwh", 0) or 0)
    ev_cum = (snap.get("energy_totals") or {}).get("ev")
    ev_cum = float(ev_cum) if isinstance(ev_cum, (int, float)) else None
    day = (datetime.now() - timedelta(hours=4)).strftime("%Y-%m-%d")
    if _EV.get("session_day") != day:            # new day (anchored at 4am) → reset the cap
        _EV["session_day"], _EV["capped"], _EV["session_start_kwh"] = day, False, ev_cum
    if _EV.get("session_start_kwh") is None and ev_cum is not None:
        _EV["session_start_kwh"] = ev_cum
    session = (ev_cum - _EV["session_start_kwh"]) if (ev_cum is not None and _EV.get("session_start_kwh") is not None) else 0.0
    if now < _EV.get("override_until", 0):        # manual force-charge: ignore economics, dwell, battery gate, cap
        want, why = True, f"manual force-charge ({int((_EV['override_until'] - now) / 60)}min left)"
    else:
        want, why = ev_divert_decision(snap, ev)
        if cap > 0 and ev_cum is not None:
            if session >= cap:
                _EV["capped"] = True
            if _EV.get("capped"):
                want, why = False, f"daily cap {cap:.0f}kWh reached ({session:.1f}kWh) — resets ~4am / Force car charge"
            elif want:
                why += f" · {session:.1f}/{cap:.0f}kWh today"
    due = (now - _EV["last_change"]) >= ev.get("min_dwell_min", 10) * 60
    if now < _EV.get("override_until", 0):
        due = True                               # apply a manual force-charge immediately, no dwell wait
    if _EV["on"] is None or (want != _EV["on"] and due):
        try:
            ha_call_service(cfg, "switch", "turn_on" if want else "turn_off", sw)
        except Exception as e:
            print(f"ev divert switch failed: {e}", file=sys.stderr)
            return f"ev divert error: {e}"
        if _EV["on"] != want:
            log_event("ev_divert", f"car charger {'ON' if want else 'OFF'} ({why})")
        _EV["on"], _EV["last_change"] = want, now
    # No-draw detection: socket on but ~0 power for a while → car is full or not plugged in.
    ev_kw = snap.get("ev_kw")
    note = ""
    if _EV["on"] and isinstance(ev_kw, (int, float)) and ev_kw < 0.1:
        _EV["lowdraw_since"] = _EV.get("lowdraw_since") or now
        if now - _EV["lowdraw_since"] > 300:
            note = " · ⚠️ no draw — car full or unplugged"
    else:
        _EV["lowdraw_since"] = 0.0
    return f"car charger {'ON' if _EV['on'] else 'off'} ({why}){note}"


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
    out = []
    selling = bool(rec.get("force_discharge"))
    if nc.get("on_sell", True) and selling and not _NOTIFY["last_selling"]:
        out.append(("💰 foxctl auto-selling",
                    f"Exporting battery to grid at {snap.get('feedin_power')}kW down to "
                    f"{rec.get('sell_floor')}% (overnight buffer kept)."))
    _NOTIFY["last_selling"] = selling
    stale = "stale" in (snap.get("telemetry_source") or "") or "down" in (snap.get("telemetry_source") or "")
    if nc.get("on_stale", True) and stale and not _NOTIFY["last_stale"]:
        out.append(("⚠️ foxctl telemetry stale",
                    "HA sensors frozen and FoxESS fallback failed — control on safety hold until data recovers."))
    _NOTIFY["last_stale"] = stale
    for t, m in out:
        ha_notify(cfg, t, m)


def append_log(snap: dict):
    """Append a compact decision record (JSONL) for later observation."""
    rec = snap.get("recommendation", {})
    row = {
        "ts": snap.get("ts"), "soc": snap.get("soc"), "pv_kw": snap.get("pv_kw"),
        "load_kw": snap.get("load_kw"), "grid_kw": snap.get("grid_power"),
        "work_mode": snap.get("work_mode"), "action": rec.get("action"),
        "target_mode": rec.get("target_mode"), "force_charge": rec.get("force_charge"),
        "force_discharge": rec.get("force_discharge"),
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
    ("foxctl_load_energy", "House load energy", "kWh", "energy", "total_increasing"),
    ("foxctl_ev_power", "EV charger power", "kW", "power", "measurement"),
    ("foxctl_ev_energy", "EV charger energy", "kWh", "energy", "total_increasing"),
    ("foxctl_ev_charger", "EV charger state", None, None, None),
    ("foxctl_target_soc", "Target SoC", "%", None, None),
    # Forecast metrics so they're graphable/automatable in HA.
    ("foxctl_solar_remaining", "Solar remaining today (cal)", "kWh", "energy", "measurement"),
    ("foxctl_solar_tomorrow", "Solar tomorrow (cal)", "kWh", "energy", "measurement"),
    ("foxctl_solar_cal_bias", "Solar forecast bias", None, None, "measurement"),
    ("foxctl_avg_daily_load", "Avg daily load", "kWh", "energy", "measurement"),
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
                "load_energy": et.get("load"),
                "target_soc": dyn.get("target_soc")}
        sf = snap.get("solar_forecast") or {}
        sc = snap.get("solar_cal") or {}
        cons = snap.get("consumption") or {}
        tele.update({"solar_remaining": sf.get("remaining_today"), "solar_tomorrow": sf.get("tomorrow"),
                     "solar_cal_bias": sc.get("bias"), "avg_daily_load": cons.get("avg_daily_total_kwh"),
                     "ev_power": snap.get("ev_kw"), "ev_energy": et.get("ev"),
                     "ev_charger": ("on" if _EV.get("on") else "off") if (cfg.get("ev_divert") or {}).get("switch") else "n/a"})
        tele.update({k: round(v, 3) for k, v in ps.items()})
        cli.publish("foxctl/telemetry", json.dumps(tele), qos=0, retain=True)
    except Exception as e:
        print(f"mqtt publish failed: {e}", file=sys.stderr)


def decide_zerohero(soc, work_mode, strat, profile, survival_soc):
    """GloBird time-of-use strategy (import-cost driven, no price forecasting). Reads the ACTIVE tariff
    `profile` (the resolved tariffs[tariff_profile] dict) — free/peak/export windows + per-band cents:
      • FREE window   → grid-charge battery to full (first free_kwh/day are 0c).
      • PEAK window   → cover ALL load from battery, ZERO grid import.
      • shoulder/overnight → run off battery, avoid grid import until the next free window.
      • Export to grid is OFF by default (poor feed-in) — needs sell_enabled AND a profile export window.
    Returns a rec dict describing the desired battery action."""
    free = profile.get("free") or {}
    peak = profile.get("peak") or {}
    expw = profile.get("export") or {}
    fs, fe = free.get("start", 11), free.get("end", 14)
    es, ee = expw.get("start", 18), expw.get("end", 21)
    ps, pe = peak.get("start", 16), peak.get("end", 23)          # full ToU peak (no import)
    peak_c, shoulder_c = peak.get("c"), profile.get("shoulder_c")
    pc_txt = f"{peak_c:g}c" if isinstance(peak_c, (int, float)) else "peak"
    sh_txt = f"{shoulder_c:g}c" if isinstance(shoulder_c, (int, float)) else "shoulder"
    max_soc = strat.get("max_soc", 90)
    reserve = strat.get("reserve_soc", 20)
    # export to grid (feed-in) — off by default; needs both the master toggle AND a profile export window
    sell_on = bool(strat.get("sell_enabled", False)) and bool(expw)
    nowl = datetime.now()
    h = nowl.hour + nowl.minute / 60.0
    in_free = fs <= h < fe
    in_eve = es <= h < ee
    in_peak = ps <= h < pe
    action, target_mode, fc, fd = "SET_MODE", (work_mode or "SelfUse"), False, False
    reasons = []
    # Force-charge from grid ONLY in the FREE window — never before 11:00 and never in the peak.
    if in_free and soc < max_soc:
        action, fc = "FORCE_CHARGE", True
        reasons.append(f"ZeroHero FREE window {fs:02d}:00–{fe:02d}:00 (0c, first {free.get('free_kwh', 50):g}kWh) → "
                       f"grid-charge to {max_soc}% — full by {fe:02d}:00.")
    elif in_free:
        reasons.append(f"ZeroHero free window, battery full ({soc:.0f}% ≥ {max_soc}%). SelfUse.")
    elif in_eve and sell_on and soc > survival_soc + 1:
        action, fd = "SELL", True
        reasons.append(f"ZeroHero export {es:02d}:00–{ee:02d}:00 → export surplus down to {survival_soc}% "
                       f"(keeps enough to coast to 11:00).")
    elif in_peak:
        reasons.append(f"ZeroHero PEAK {ps:02d}:00–{pe:02d}:00 ({pc_txt}) → cover load from battery, ZERO grid "
                       f"import (no force-charge, no feed-in). SelfUse.")
    elif soc <= reserve:
        reasons.append(f"ZeroHero off-window but SoC {soc:.0f}% ≤ reserve {reserve}% — battery low. SelfUse.")
    else:
        reasons.append(f"ZeroHero shoulder/overnight ({sh_txt}) → run off battery, avoid grid import until the "
                       f"{fs:02d}:00 free window. SelfUse.")
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
    ha = HAClient(cfg["ha"]["url"], ha_token)

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
    # EV charger power (Tuya plug reports W; convert to kW) — read here so it feeds the energy counters too.
    ev_kw = ha.get_num(cfg["ha"].get("ev_power_entity")) if cfg["ha"].get("ev_power_entity") else None
    if ev_kw is not None and ev_kw > 100:
        ev_kw = ev_kw / 1000.0
    # Cumulative energy counters (kWh, total_increasing) for the HA Energy dashboard.
    energy = update_energy(cfg, {"grid_import": grid_power, "grid_export": feedin_power,
                                 "battery_charge": bat_charge_power, "battery_discharge": bat_discharge_power,
                                 "solar": pv, "load": load, "ev": ev_kw or 0.0}) if tsrc == "FoxESS" else _ENERGY.get("totals", {})
    # work mode rarely changes externally — refresh it every Nth cycle, cache otherwise, to save API calls
    refresh = int(cfg.get("work_mode_refresh_cycles", 3))
    _WM["i"] += 1
    if _WM["value"] is None or _WM["i"] % refresh == 0:
        try:                                  # don't let a flaky/rate-limited settings read crash the cycle
            w = fox.work_mode()               # (which would freeze the cached value indefinitely)
            _WM["value"], _WM["options"], _WM["ts"] = w.get("value"), w.get("enumList"), time.time()
        except Exception as e:
            print(f"work mode read failed (keeping cached '{_WM.get('value')}'): {e}", file=sys.stderr)
        try:                                  # piggyback: detect a stranded high device min-SoC (legacy bug)
            ms = fox.get_min_soc()
            if ms is not None:
                _WM["min_soc"] = ms
                if ms > int(cfg["strategy"].get("inverter_min_soc", 10)) + 1:
                    print(f"⚠️ inverter min-SoC reads {ms}% (> floor {cfg['strategy'].get('inverter_min_soc', 10)}%) "
                          f"— will self-heal on the next force window; clear it in the FoxESS app to stop imports now.",
                          file=sys.stderr)
        except Exception:
            pass
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
    # Phase 3: calibrate the forward solar by this site's learned forecast-vs-actual bias (clamped,
    # and only once enough days are sampled). Raw values are kept for display; the calibrated ones
    # feed the survival/shortfall calc, the bells, and the SoC projection.
    solar_cal = update_solar_cal(cfg, solar_today_total)
    solar_remaining_raw, solar_tomorrow_raw = solar_remaining, solar_tomorrow
    if solar_cal["applied"]:
        if isinstance(solar_remaining, (int, float)):
            solar_remaining = round(solar_remaining * solar_cal["bias"], 2)
        if isinstance(solar_tomorrow, (int, float)):
            solar_tomorrow = round(solar_tomorrow * solar_cal["bias"], 2)
    try:
        sa = ha._state("sun.sun")["attributes"]
        sun_rise, sun_set = sa.get("next_rising"), sa.get("next_setting")
    except Exception:
        sun_rise = sun_set = None
    solar_bells = _solar_bells(sun_rise, sun_set, solar_tomorrow, solar_remaining)

    # Rolling household consumption (foxctl integrates load_power itself; EV plug tracked separately).
    consumption = update_consumption(cfg, load, ev_kw)
    # Prefer the FoxESS-history hour-of-day profile (accurate in days, not weeks) once it has matured;
    # falls back to the self-integrated profile until the backfill reaches FCAST_MIN_DAYS. Read-only.
    try:
        fcast = update_forecast_store(cfg, fox)
    except Exception as e:
        fcast = {"days": 0, "load_profile": {}, "solar_profile": {}}
        print(f"forecast store update failed: {e}", file=sys.stderr)
    if fcast["days"] >= FCAST_MIN_DAYS and fcast["load_profile"]:
        consumption["hour_profile"] = fcast["load_profile"]
        consumption["hour_min"] = fcast.get("load_min", {})
        consumption["hour_max"] = fcast.get("load_max", {})
        consumption["profile_days"] = fcast["days"]
        consumption["profile_source"] = "foxess"
        # Daily avg/min/max also come from FoxESS history (sum of each stored day's hourly `loads`),
        # so the card matches the chart's source. `today` stays from live integration (the store only
        # holds completed days); EV is unchanged (no FoxESS EV channel).
        dt = fcast.get("daily_total") or {}
        if dt:
            consumption["avg_daily_total_kwh"] = dt.get("avg")
            consumption["min_daily_total_kwh"] = dt.get("min")
            consumption["max_daily_total_kwh"] = dt.get("max")
            consumption["days_sampled"] = fcast["days"]
            if isinstance(consumption.get("avg_daily_ev_kwh"), (int, float)):
                consumption["avg_daily_base_kwh"] = round(dt["avg"] - consumption["avg_daily_ev_kwh"], 1)
    else:
        consumption.setdefault("profile_source", "self")

    load_ov(cfg)
    strat = cfg["strategy"]
    cap_kwh = float(strat.get("battery_capacity_kwh", 30))
    stored_kwh = round(cap_kwh * soc / 100.0, 1)
    # Use the measured rolling base load if we have enough history; else the static estimate.
    typical_load = consumption["avg_daily_total_kwh"] if consumption["days_sampled"] >= 2 \
        else strat.get("typical_daily_load_kwh", 30)
    reserve = strat.get("reserve_soc", 20)
    # Hours until tomorrow's solar ramp — for the overnight survival floor.
    hrs_to_solar = 12.0
    if sun_rise:
        rt = _parse_t(sun_rise)
        if rt:
            hrs_to_solar = min(16.0, max(1.0, (rt - datetime.now(timezone.utc)).total_seconds() / 3600.0 + 2))

    # Active GloBird time-of-use tariff profile — the ONLY decision model (deterministic, no forecasting).
    profile_key = strat.get("tariff_profile")
    tariffs = strat.get("tariffs") or {}
    profile = tariffs.get(profile_key) if profile_key else None
    if profile is None:                            # misconfig → first defined tariff so control never bricks
        profile = next(iter(tariffs.values()), {}) if tariffs else {}

    # Survival floor: enough SoC to coast to the next free window (where we grid-charge to full).
    nowl = datetime.now(); hh = nowl.hour + nowl.minute / 60.0
    free_start = (profile.get("free") or {}).get("start", 11)
    hrs_to_free = (free_start - hh) % 24 or 24.0          # hours until next free window
    pred_free = predict_base_load(consumption.get("hour_profile"), hrs_to_free) if consumption.get("profile_days", 0) >= 2 else None
    need_kwh = max(0.0, (pred_free if pred_free is not None else float(typical_load) * (hrs_to_free / 24.0)) - (solar_remaining or 0.0))
    survival_soc = int(min(strat.get("max_soc", 90), reserve + round(need_kwh / cap_kwh * 100)))
    rec = decide_zerohero(soc, wm.get("value"), strat, profile, survival_soc)

    # Load forecast for the dashboard (learned hour-of-day profile, phased from the current hour).
    have_profile = consumption.get("profile_days", 0) >= 2 and bool(consumption.get("hour_profile"))
    hrs_to_midnight = 24 - hh
    rest_today_load = round(predict_base_load(consumption.get("hour_profile"), hrs_to_midnight), 1) if have_profile else None
    next24_load = round(predict_base_load(consumption.get("hour_profile"), 24), 1) if have_profile else None

    sell_eff = _OV["sell"] if _OV.get("sell") is not None else strat.get("sell_price", 0.50)
    # export to grid off by default — needs the master toggle AND a profile export window
    sell_enabled = bool(strat.get("sell_enabled", False)) and bool(profile.get("export"))

    now_epoch = time.time()
    return {
        "demand_window": demand_window,
        "weather": weather,
        "solar_forecast": {"today_total": solar_today_total, "remaining_today": solar_remaining,
                           "tomorrow": solar_tomorrow,
                           "remaining_today_raw": solar_remaining_raw, "tomorrow_raw": solar_tomorrow_raw},
        "solar_cal": solar_cal,
        "solar_bells": solar_bells,
        "dynamic": {"source": "zerohero", "mode": "tariff",
                    "tariff_label": profile.get("label"),
                    "tariff": {"free": profile.get("free"), "peak": profile.get("peak"),
                               "shoulder_c": profile.get("shoulder_c"), "export": profile.get("export")},
                    "target_soc": strat.get("max_soc", 90),
                    "max_soc": strat.get("max_soc", 90),
                    "survival_soc": survival_soc,
                    "topup": bool(strat.get("topup_to_target", False)),
                    "sell_enabled": sell_enabled,
                    "sell_price": (sell_eff if sell_enabled else None)},
        "battery": {"capacity_kwh": cap_kwh, "stored_kwh": stored_kwh},
        "consumption": consumption,
        "forecast_profiles": fcast,   # FoxESS-history hour-of-day load + solar + grid in/out
        "load_forecast": {"rest_today_kwh": rest_today_load, "next24_kwh": next24_load,
                          "typical_daily_kwh": round(float(typical_load), 1)},
        "grid_power": round(grid_power, 2),
        "feedin_power": round(feedin_power, 2),
        "battery_power": round(battery_power, 2),
        "bat_charge_power": bat_charge_power,
        "bat_discharge_power": bat_discharge_power,
        "pv_strings": pv_strings,
        "energy_totals": energy,
        "sched_active": sched_active,
        "override": {"floor": _OV["floor"], "sell": _OV["sell"], "manual": _OV["manual"]},
        "ts": datetime.now().isoformat(timespec="seconds"),
        "scheduler": sched,
        "soc_updated_epoch": soc_ts,
        "data_age_s": int(now_epoch - soc_ts) if soc_ts else None,
        "load_kw": round(load, 2),
        "ev_kw": round(ev_kw, 2) if isinstance(ev_kw, (int, float)) else ev_kw,
        "solar_surplus_kw": round(pv - load, 2),
        "telemetry_source": tsrc,
        "fox_error": fox_error_status(),
        "soc": soc,
        "pv_kw": round(pv, 2),
        "real": real,
        "work_mode": wm.get("value"),
        "work_mode_age_s": int(time.time() - _WM["ts"]) if _WM.get("ts") else None,
        "inverter_min_soc_read": _WM.get("min_soc"),
        "inverter_min_soc_floor": int(strat.get("inverter_min_soc", 10)),
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
    inv_floor = int(cfg["strategy"].get("inverter_min_soc", 10))   # constant device floor — never computed
    # Manual SELL floor is enforced in SOFTWARE: stop the discharge when SoC reaches the requested
    # floor, rather than pushing that floor onto the inverter (which would strand it in SelfUse).
    soc_now = snap.get("soc")
    if mo["mode"] == "sell" and isinstance(soc_now, (int, float)) and soc_now <= mo.get("min_soc", inv_floor):
        try:
            fox.disable_scheduler()
        except Exception as e:
            print(f"manual sell floor revert failed: {e}", file=sys.stderr)
        _OV["manual"] = None; save_ov(cfg)
        log_event("override", f"manual sell reached {mo.get('min_soc')}% floor → stop")
        return None
    active = (snap.get("scheduler") or {}).get("active") or {}
    end = datetime.now() + timedelta(seconds=mo["until"] - now)
    hhmm = end.strftime("%H:%M")
    if active.get("mode") == want:
        return f"MANUAL {mo['mode']} until {hhmm} (active)"
    nd = datetime.now()
    if mo["mode"] == "charge":
        fox.enable_force_charge((nd.hour, nd.minute), (end.hour, end.minute),
                                inv_floor, mo["cap"], mo["power"])
        _CHARGE["until"] = mo["until"]
    else:
        fox.enable_force_discharge((nd.hour, nd.minute), (end.hour, end.minute),
                                   inv_floor, mo["power"])
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
    # Charge cap for force-charge windows: the tariff profile's max SoC (free-window fill target).
    eff_target = (snap.get("dynamic") or {}).get("target_soc") or strat.get("max_soc", 90)
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
            inv_floor = int(strat.get("inverter_min_soc", 10))   # constant device floor — never the survival number
            fox.enable_force_discharge((now.hour, now.minute), (eh, em),
                                       inv_floor, strat["force_charge_power_kw"])
            m = (f"AUTO-SELL START until ~{eh:02d}:{em:02d} (sells toward {rec.get('sell_floor')}% survival "
                 f"[software-stopped]; inverter hard floor {inv_floor}% @ {strat['force_charge_power_kw']}kW)")
            msgs.append(m); log_event("sell", m, {"feedin_kw": snap.get("feedin_power"), "soc": snap.get("soc")})
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
                                    int(strat.get("inverter_min_soc", 10)), eff_target,
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
                            int(strat.get("inverter_min_soc", 10)), strat.get("max_soc", 90), strat["force_charge_power_kw"])
    msg = (f"force-charge TEST enabled {now.hour:02d}:{now.minute:02d}→{eh:02d}:{em:02d} "
           f"cap {strat.get('max_soc', 90)}% @ {strat['force_charge_power_kw']}kW")
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
    if do_apply:
        snap["ev_divert"] = ev_divert_tick(cfg, snap)   # solar diversion to the car charger (auto only)
    mqtt_publish(cfg, snap)
    maybe_notify(cfg, snap)
    append_log(snap)
    with LAST_LOCK:
        LAST.clear(); LAST.update(snap)
    return snap


def schedule_repoll(cfg, delays=(40, 110)):
    """After a control action, refresh the snapshot a couple of times as the inverter + FoxESS telemetry
    catch up (FoxESS telemetry lags ~1-2 min). Otherwise the dashboard keeps showing the pre-action
    snapshot until the next 5-min poll, which looks like the action didn't take."""
    def _repoll():
        try:
            run_once(cfg, do_apply=False)
        except Exception as e:
            print(f"repoll failed: {e}", file=sys.stderr)
    import threading
    for d in delays:
        t = threading.Timer(d, _repoll)
        t.daemon = True
        t.start()


# ------------------------------------------------------------------- web -----

CSS = """body{font:16px system-ui;margin:1.5rem auto;max-width:1040px;padding:0 1rem}
h1{font-size:1.3rem;font-weight:600;margin:.2rem 0 1rem}
.row{display:flex;gap:1rem;flex-wrap:wrap;margin:1rem 0}
.card{border:1px solid #ddd;border-radius:12px;padding:.9rem 1.1rem;min-width:150px;flex:1}
.card small{color:#666;display:block} .big{font-size:1.9rem;font-weight:600;margin:.15rem 0}
.warn{background:#fff3e0;border-color:#e67e22}
.chart{border:1px solid #eee;border-radius:12px;padding:.6rem .7rem;margin:1.2rem 0}
.chart svg{width:100%;height:auto;display:block}
.muted{color:#888;padding:1.4rem;text-align:center}
a{color:#06c}
@media (prefers-color-scheme: dark){
 body{background:#111418;color:#e3e3e3}
 .card,.chart{background:#1e2227;border-color:#3a3f46}
 .card small,small,.muted{color:#9aa3ad}
 .warn{background:#3a2c1a;border-color:#e67e22}
 a{color:#6cf}
}"""

JS = """
async function softRefresh(){
 try{
  const r=await fetch(location.pathname,{cache:'no-store'});const t=await r.text();
  const doc=new DOMParser().parseFromString(t,'text/html');
  ['cards','chart'].forEach(function(id){const n=document.getElementById(id),m=doc.getElementById(id);if(n&&m)n.innerHTML=m.innerHTML;});
  const rf=document.getElementById('refr');if(rf)rf.textContent='updated '+new Date().toLocaleTimeString();
 }catch(e){const rf=document.getElementById('refr');if(rf)rf.textContent='refresh failed';}
}
setInterval(softRefresh,60000);"""


def _hod(d, h):
    """hour-of-day lookup tolerant of int/str keys."""
    v = (d or {}).get(h, (d or {}).get(str(h)))
    return float(v) if isinstance(v, (int, float)) else 0.0


def render_solar_usage_chart(snap: dict) -> str:
    """Next-24h expected solar generation (calibrated Solcast bells) overlaid with the learned house-usage
    curve. The gap between them is the surplus available to charge the car — the basis for the upcoming
    car-charging logic that factors in overnight carried-over SoC."""
    bells = snap.get("solar_bells") or []
    prof = (snap.get("consumption") or {}).get("hour_profile") or {}
    now = datetime.now()
    h0 = now.hour + now.minute / 60.0

    def bell_kw(hh):
        hh %= 24
        tot = 0.0
        for b in bells:
            s, e, pm = b.get("s"), b.get("e"), b.get("pmax", 0)
            if s is not None and e is not None and e > s and s <= hh <= e and pm:
                tot += pm * math.sin(math.pi * (hh - s) / (e - s))
        return max(0.0, tot)

    N = 24
    sol = [bell_kw(h0 + i) for i in range(N + 1)]
    use = [_hod(prof, int(h0 + i) % 24) for i in range(N + 1)]
    if not any(sol) and not any(use):
        return ('<div class=muted>No solar forecast or usage history to chart yet — '
                'this fills in within a few hours of running.</div>')
    W, H, pL, pR, pT, pB = 720, 260, 38, 12, 22, 26
    iw, ih = W - pL - pR, H - pT - pB
    ymax = max(max(sol), max(use), 0.5) * 1.15
    X = lambda i: pL + iw * i / N
    Y = lambda v: pT + ih * (1 - min(v, ymax) / ymax)
    out = [f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMid meet" '
           f'xmlns="http://www.w3.org/2000/svg" font-family="system-ui">']
    for f in (0, .5, 1):
        y = pT + ih * (1 - f)
        out.append(f'<line x1={pL} y1={y:.1f} x2={W-pR} y2={y:.1f} stroke="#888" stroke-opacity=0.18/>')
        out.append(f'<text x={pL-5} y={y+4:.1f} font-size=11 fill="#999" text-anchor=end>{ymax*f:.1f}</text>')
    for i in range(0, N + 1, 3):
        out.append(f'<text x={X(i):.0f} y={H-7} font-size=11 fill="#999" '
                   f'text-anchor=middle>{int(h0+i)%24:02d}</text>')
    area = (f"{X(0):.1f},{Y(0):.1f} " + " ".join(f"{X(i):.1f},{Y(sol[i]):.1f}" for i in range(N + 1))
            + f" {X(N):.1f},{Y(0):.1f}")
    out.append(f'<polygon points="{area}" fill="#f5c518" fill-opacity=0.30/>')
    out.append('<polyline points="' + " ".join(f"{X(i):.1f},{Y(sol[i]):.1f}" for i in range(N + 1))
               + '" fill=none stroke="#f5b800" stroke-width=2.4/>')
    out.append('<polyline points="' + " ".join(f"{X(i):.1f},{Y(use[i]):.1f}" for i in range(N + 1))
               + '" fill=none stroke="#8e44ad" stroke-width=2.2/>')
    out.append(f'<rect x={pL+4} y=4 width=11 height=11 fill="#f5b800"/>'
               f'<text x={pL+19} y=14 font-size=12 fill="#888">Expected solar (kW)</text>')
    out.append(f'<rect x={pL+186} y=4 width=11 height=11 fill="#8e44ad"/>'
               f'<text x={pL+201} y=14 font-size=12 fill="#888">House usage (kW)</text>')
    out.append('</svg>')
    return "".join(out)


def render(snap: dict, cfg: dict) -> str:
    wm = snap.get("work_mode")
    wma = snap.get("work_mode_age_s")
    wm_stale = isinstance(wma, (int, float)) and wma > 1800
    bat = snap.get("battery") or {}
    soc = snap.get("soc")
    sf = snap.get("solar_forecast") or {}
    sc = snap.get("solar_cal") or {}
    ev = cfg.get("ev_divert") or {}
    ev_status = snap.get("ev_divert") or ("no charger configured" if not ev.get("switch") else "idle")
    ev_on = (snap.get("ev_divert") or "").startswith("car charger ON")
    cal_txt = f' · cal ×{sc.get("bias")}' if sc.get("applied") else ""

    def _n(v):
        return v if v is not None else "–"

    fe = snap.get("fox_error")
    if fe and fe.get("rate_limited"):
        banner = (f'<div class="card warn">⛔ FoxESS API rate-limited — telemetry/control may be stale '
                  f'(last error {fe.get("age")}s ago).</div>')
    elif fe:
        banner = (f'<div class="card warn">⚠️ FoxESS API errors — telemetry may be stale '
                  f'(last error {fe.get("age")}s ago).</div>')
    else:
        banner = ''

    cards = "".join([
        f'<div class="card{" warn" if wm_stale else ""}"><small>Work mode</small><div class=big>{_n(wm)}</div>'
        f'<small>{("read "+str(wma)+"s ago"+(" ⚠ stale" if wm_stale else "")) if wma is not None else "no read yet"}</small></div>',
        f'<div class=card><small>Battery</small><div class=big>{round(soc) if isinstance(soc,(int,float)) else "–"}%</div>'
        f'<small>{_n(bat.get("stored_kwh"))}/{_n(bat.get("capacity_kwh"))} kWh stored</small></div>',
        f'<div class="card{" warn" if ev_on else ""}"><small>Car charging</small>'
        f'<div class=big>🔌 {_n(snap.get("ev_kw"))} <small>kW</small></div><small>{ev_status}</small></div>',
        f'<div class=card><small>Weather &amp; solar</small><div class=big>{_n(sf.get("today_total"))} <small>kWh today</small></div>'
        f'<small>{snap.get("weather") or "—"} · {_n(sf.get("remaining_today"))} kWh left · tomorrow {_n(sf.get("tomorrow"))} kWh'
        f'{cal_txt}</small></div>',
    ])
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>foxctl</title><style>{CSS}</style></head><body>
<h1>foxctl <small id=refr style="color:#888;font-weight:400"></small></h1>
{banner}
<div class=row id=cards>{cards}</div>
<div class=chart><div style="font-size:.9rem;color:#888;margin:.1rem .3rem .4rem">Next 24 hours — expected solar vs house usage</div>
<div id=chart>{render_solar_usage_chart(snap)}</div></div>
<p><small>auto-refresh 60s · <a href=/api/state>/api/state</a></small></p>
<script>{JS}</script>
</body></html>"""


def build_export_csv(cfg, snap):
    """CSV for a spreadsheet: yesterday→now 5-min ACTUALS (from FoxESS history) + the forward next-24h
    hourly forecast (expected usage, solar, derived net grid import/export)."""
    import csv as _csv
    import io as _io
    header = ["time", "kind", "soc_pct", "pv_kw", "load_kw", "grid_import_kw", "grid_export_kw",
              "exp_load_kwh", "exp_solar_kwh", "exp_net_import_kwh", "exp_net_export_kwh"]
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(header)
    # --- actuals: yesterday + today, in ≤24h windows (FoxESS history caps each call at one day) ---
    try:
        fox = FoxESS(cfg["foxess"]["token"], cfg["foxess"]["sn"])
        keymap = {"pvPower": "pv_kw", "loadsPower": "load_kw", "SoC": "soc_pct",
                  "gridConsumptionPower": "grid_import_kw", "feedinPower": "grid_export_kw"}
        by_time = {}
        t0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        windows = [(t0 - timedelta(days=1), t0 - timedelta(seconds=1)), (t0, datetime.now())]
        for w0, w1 in windows:
            res = fox.history(["pvPower", "loadsPower", "SoC", "gridConsumptionPower", "feedinPower"],
                              int(w0.timestamp() * 1000), int(w1.timestamp() * 1000))
            for ds in ((res[0].get("datas") if res else None) or []):
                col = keymap.get(ds.get("variable"))
                if not col:
                    continue
                for pt in (ds.get("data") or []):
                    if pt.get("time") is not None:
                        by_time.setdefault(pt["time"][:19], {})[col] = pt.get("value")
        for t in sorted(by_time):
            r = by_time[t]
            w.writerow([t, "actual", r.get("soc_pct", ""), r.get("pv_kw", ""), r.get("load_kw", ""),
                        r.get("grid_import_kw", ""), r.get("grid_export_kw", ""), "", "", "", ""])
    except Exception as e:
        w.writerow([f"# actuals unavailable: {e}"])
    # --- forward next-24h hourly forecast (learned usage profile + solar bells, no extra API calls) ---
    cons = snap.get("consumption") or {}
    prof = cons.get("hour_profile") or {}
    bells = snap.get("solar_bells") or []
    now = datetime.now()
    h0 = now.hour + now.minute / 60.0

    def _bell_kw(hh):
        hh %= 24
        tot = 0.0
        for b in bells:
            s, e, pm = b.get("s"), b.get("e"), b.get("pmax", 0)
            if s is not None and e is not None and e > s and s <= hh <= e and pm:
                tot += pm * math.sin(math.pi * (hh - s) / (e - s))
        return max(0.0, tot)

    for i in range(24):
        use = _hod(prof, int(h0 + i) % 24)
        sol = round(_bell_kw(h0 + i), 3)
        w.writerow([(now + timedelta(hours=i)).strftime("%Y-%m-%d %H:00"), "forecast", "", "", "", "", "",
                    round(use, 3), sol, round(max(0.0, use - sol), 3), round(max(0.0, sol - use), 3)])
    return buf.getvalue()


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
            elif self.path.startswith("/api/export.csv"):
                with LAST_LOCK:
                    snap = dict(LAST)
                try:
                    self._send(200, build_export_csv(cfg, snap), "text/csv")
                except Exception as e:
                    self._send(200, f"error,{e}", "text/csv")
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
            # State-changing actions: refresh the snapshot 40s/110s later so the dashboard reflects the
            # change as the inverter + FoxESS telemetry catch up, instead of at the next 5-min poll.
            if any(self.path.startswith(p) for p in ("/api/cancel_override", "/api/force_charge",
                    "/api/sell", "/api/scheduler_off", "/api/ev_charge", "/api/ev_off", "/api/apply")):
                schedule_repoll(cfg)
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
            elif self.path.startswith("/api/backfill_ha"):
                try:
                    days = 7
                    if "days=" in self.path:
                        days = max(1, min(60, int(self.path.split("days=")[1].split("&")[0])))
                    done = backfill_ha_statistics(cfg, days)
                    msg = f"imported {done}" if done else "no stored days to import yet"
                except Exception as e:
                    msg = f"ERROR: {e}"
                log_action(f"backfill_ha -> {msg}")
                self._send(200, json.dumps({"backfill_ha": msg}, default=str), "application/json")
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
            elif self.path.startswith("/api/ev_charge") or self.path.startswith("/api/ev_off"):
                try:
                    sw = (cfg.get("ev_divert") or {}).get("switch")
                    if not sw:
                        msg = "no ev_charger_switch configured"
                    elif not cfg["control"].get("allow_control"):
                        msg = "control disabled (allow_control=false)"
                    elif self.path.startswith("/api/ev_off"):
                        _EV["override_until"] = 0.0
                        ha_call_service(cfg, "switch", "turn_off", sw)
                        _EV["on"], _EV["last_change"] = False, time.time()
                        msg = "car charging forced OFF → back to auto divert"
                        log_event("ev_divert", msg)
                    else:
                        h = 2
                        if "h=" in self.path:
                            h = max(1, min(12, int(float(self.path.split("h=")[1].split("&")[0]))))
                        _EV["override_until"] = time.time() + h * 3600
                        _EV["capped"] = False    # a manual force clears the daily cap and starts a fresh session
                        ev_cum = (_ENERGY.get("totals") or {}).get("ev")
                        _EV["session_start_kwh"] = float(ev_cum) if isinstance(ev_cum, (int, float)) else None
                        ha_call_service(cfg, "switch", "turn_on", sw)
                        _EV["on"], _EV["last_change"] = True, time.time()
                        msg = f"car charging forced ON for {h}h (overrides divert economics + battery gate + daily cap)"
                        log_event("ev_divert", msg)
                except Exception as e:
                    msg = f"ERROR: {e}"
                log_action(f"ev_charge -> {msg}")
                self._send(200, json.dumps({"ev": msg}, default=str), "application/json")
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
            tl = (snap.get("dynamic") or {}).get("tariff_label", "tariff")
            print(f"{snap['ts']} {tl} soc={snap['soc']:.0f}% "
                  f"mode={snap['work_mode']} -> {r['action']}/{r['target_mode']}"
                  + (f" applied={snap['applied']}" if auto else ""))
        except Exception as e:
            snap = None
            print(f"{datetime.now().isoformat(timespec='seconds')} ERROR: {e}", file=sys.stderr)
        # Sync next poll to the foxess-ha update cycle ONLY when that sensor is fresh — otherwise
        # (frozen integration → FoxESS fallback) anchoring to its dead timestamp drifts the cadence
        # and the telemetry goes stale. In that case just poll on a steady fixed interval.
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
                    choices=["status", "recommend", "apply", "loop", "serve", "backfill-ha"])
    ap.add_argument("--init", action="store_true", help="write a starter config and exit")
    ap.add_argument("--json", action="store_true", help="JSON output for status/recommend")
    ap.add_argument("--days", type=int, default=7, help="days to backfill for backfill-ha")
    args = ap.parse_args(argv)

    if args.init:
        write_default_config(); return 0

    cfg = load_config()

    if args.cmd in ("status", "recommend"):
        snap = run_once(cfg, do_apply=False)
        if args.json:
            print(json.dumps(snap, indent=2, default=str)); return 0
        r = snap["recommendation"]
        dyn = snap.get("dynamic") or {}
        print(f"[{snap['ts']}]")
        print(f"  Tariff      : {dyn.get('tariff_label','?')} (max SoC {dyn.get('max_soc','?')}%, survival {dyn.get('survival_soc','?')}%)")
        print(f"  Grid now    : import {snap.get('grid_power')} kW / export {snap.get('feedin_power')} kW")
        print(f"  Battery SoC : {snap['soc']:.0f}%")
        print(f"  Solar (PV)  : {snap['pv_kw']} kW  (load {snap.get('load_kw')} kW, surplus {snap.get('solar_surplus_kw')} kW)")
        print(f"  Usage fc    : next24 {(snap.get('load_forecast') or {}).get('next24_kwh','?')} kWh, rest-today {(snap.get('load_forecast') or {}).get('rest_today_kwh','?')} kWh")
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
    if args.cmd == "backfill-ha":
        done = backfill_ha_statistics(cfg, max(1, args.days))
        print("HA statistics backfill imported (hourly points):", done or "nothing (no stored days yet)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
