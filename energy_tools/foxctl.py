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
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
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
    target, reserve = strat["target_soc"], strat["reserve_soc"]
    now = datetime.now(timezone.utc)
    look_h = strat.get("precharge_lookahead_h", 3)

    band = classify_band(price, strat.get("bands", []))
    charge_bands = strat.get("charge_bands", [])
    avoid_bands = strat.get("avoid_bands", [])

    reasons = []
    action = "HOLD"
    target_mode = work_mode or "SelfUse"
    force_charge = False

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
    solar_covering = solar_surplus >= solar_defer

    if price is None:
        reasons.append("No price available; defaulting to SelfUse.")
        action, target_mode = "SET_MODE", "SelfUse"
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

    if soc <= reserve:
        reasons.append(f"SoC {soc:.0f}% at/below reserve {reserve}%.")

    rec = {
        "action": action,
        "target_mode": target_mode,
        "force_charge": force_charge,
        "band": band,
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
LOG_PATH = Path(os.environ.get("FOXCTL_LOG", Path.home() / "foxctl/decisions.jsonl"))
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


def gather_and_decide(cfg: dict) -> dict:
    fox = FoxESS(cfg["foxess"]["token"], cfg["foxess"]["sn"])
    ha_token = Path(os.path.expanduser(cfg["ha"]["token_file"])).read_text().strip()
    ha = HAPrices(cfg["ha"]["url"], ha_token, cfg["ha"]["amber_price_entity"],
                  cfg["ha"]["amber_forecast_entity"], cfg["ha"].get("aemo_forecast_entity"),
                  cfg["ha"].get("amber_feedin_entity"))

    prices = ha.snapshot()
    # Telemetry from HA (foxess-ha integration) to avoid a 2nd FoxESS poller. Fall back to
    # FoxESS only if HA values are unavailable (e.g. integration down).
    soc, soc_ts = ha.get_value_age(cfg["ha"].get("soc_entity"))
    pv = ha.get_num(cfg["ha"].get("pv_entity"))
    load = ha.get_num(cfg["ha"].get("load_entity"))
    real = {}
    if soc is None or pv is None:
        try:
            real = fox.real(["SoC", "pvPower"])
            if soc is None:
                soc = real.get("SoC")
            if pv is None:
                pv = real.get("pvPower")
        except Exception as e:
            print(f"FoxESS telemetry fallback failed: {e}", file=sys.stderr)
    soc = float(soc or 0)
    pv = float(pv or 0)
    load = float(load or 0)
    # work mode rarely changes externally — refresh it every Nth cycle, cache otherwise, to save API calls
    refresh = int(cfg.get("work_mode_refresh_cycles", 3))
    _WM["i"] += 1
    if _WM["value"] is None or _WM["i"] % refresh == 0:
        w = fox.work_mode()
        _WM["value"], _WM["options"] = w.get("value"), w.get("enumList")
    wm = {"value": _WM["value"], "enumList": _WM["options"]}
    sched = fox.scheduler_status()
    charging = bool(sched["enabled"] and sched["active"] and sched["active"]["mode"] == "ForceCharge")
    demand_window = (ha.get_state(cfg["ha"].get("demand_window_entity")) == "on")
    rec = decide(prices, soc, pv, wm.get("value"), cfg["strategy"],
                 currently_charging=charging, load_kw=load, demand_window=demand_window)

    now_epoch = time.time()
    return {
        "demand_window": demand_window,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "scheduler": sched,
        "soc_updated_epoch": soc_ts,
        "data_age_s": int(now_epoch - soc_ts) if soc_ts else None,
        "load_kw": load,
        "solar_surplus_kw": round(pv - load, 2),
        "price": prices.get("price"),
        "descriptor": prices.get("descriptor"),
        "aemo_price": prices.get("aemo_price"),
        "feedin": prices.get("feedin"),
        "forecast_next": prices.get("forecast", [])[:6],
        "aemo_forecast_next": prices.get("aemo_forecast", [])[:6],
        "soc": soc,
        "pv_kw": pv,
        "real": real,
        "work_mode": wm.get("value"),
        "work_mode_options": wm.get("enumList"),
        "recommendation": rec,
        "applied": None,
    }


def apply_recommendation(cfg: dict, snap: dict) -> str:
    ctrl = cfg["control"]
    rec = snap["recommendation"]
    if not ctrl.get("allow_control"):
        return "control disabled (control.allow_control=false) — not applying"
    msgs = []
    fox = FoxESS(cfg["foxess"]["token"], cfg["foxess"]["sn"])
    strat = cfg["strategy"]
    sch = snap.get("scheduler") or {}
    already_charging = bool(sch.get("enabled") and sch.get("active")
                            and sch["active"].get("mode") == "ForceCharge")
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
                                    strat["min_soc_on_grid"], strat["target_soc"],
                                    strat["force_charge_power_kw"])
            m = f"force-charge START until ~{eh:02d}:{em:02d} (cap {strat['target_soc']}% @ {strat['force_charge_power_kw']}kW)"
            msgs.append(m); log_event("force_charge", m, {"band": rec.get("band"), "soc": snap.get("soc")})
    else:
        # Stop only on the transition out of charging — one write, not every cycle.
        if ctrl.get("set_force_charge") and already_charging:
            fox.disable_scheduler()
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
    log_event("disable", "manual: scheduler disabled → reverted to plain work mode")
    return "scheduler disabled → reverted to plain work mode"


def run_once(cfg: dict, do_apply: bool) -> dict:
    snap = gather_and_decide(cfg)
    snap["band"] = snap.get("recommendation", {}).get("band")
    if do_apply:
        snap["applied"] = apply_recommendation(cfg, snap)
    run_band_actions(cfg, snap)
    append_log(snap)
    with LAST_LOCK:
        LAST.clear(); LAST.update(snap)
    return snap


# ------------------------------------------------------------------- web -----

BAND_COLOR = {"ludicrous": "#7b2ff7", "extremely_low": "#0a8f3c", "low": "#3c9", "normal": "#888",
              "high": "#e67e22", "spike": "#c0392b", "unknown": "#aaa"}

CSS = """body{font:15px system-ui;margin:2rem;max-width:860px}
h1{font-size:1.3rem} .big{font-size:2rem;font-weight:600}
.row{display:flex;gap:1.2rem;flex-wrap:wrap;margin:1rem 0}
.card{border:1px solid #ddd;border-radius:10px;padding:.8rem 1.1rem;min-width:130px}
.rec{background:#f3f8ff;border-color:#9cf} .pill{color:#fff;padding:2px 9px;border-radius:20px;font-size:.8rem}
button{font:inherit;padding:.5rem .9rem;border:1px solid #bbb;border-radius:8px;background:#fafafa;cursor:pointer;margin:.2rem}
button:hover{background:#eee} .danger{border-color:#c0392b;color:#c0392b}
table{border-collapse:collapse;margin-top:.5rem;font-size:.85rem;width:100%}
td,th{border:1px solid #eee;padding:3px 7px;text-align:right} th{background:#fafafa}
small{color:#666} #msg{margin:.5rem 0;color:#06c}
@media (prefers-color-scheme: dark){
 body{background:#111418;color:#e3e3e3}
 .card{background:#1e2227;border-color:#3a3f46}
 .rec{background:#15263a;border-color:#3a567a}
 button{background:#262b31;color:#e3e3e3;border-color:#4a505a}
 button:hover{background:#333a42} .danger{border-color:#e06; color:#f88}
 th{background:#262b31} td,th{border-color:#3a3f46}
 small{color:#9aa3ad} #msg{color:#6cf} a{color:#6cf}
}"""

JS = """
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
refreshLog();refreshEvents();"""


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
 <div class=card style="{'border-color:#e67e22' if snap.get('demand_window') else ''}"><small>Demand window</small><div class=big>{'⚠️ ACTIVE' if snap.get('demand_window') else 'off'}</div><small>{'won’t grid-charge battery' if snap.get('demand_window') else ''}</small></div>
 <div class=card><small>Work mode</small><div class=big>{snap.get('work_mode')}</div></div>
 <div class=card><small>Data age / next update</small>
   <div class=big><span id=age>{snap.get('data_age_s') if snap.get('data_age_s') is not None else '–'}</span>s
   · <span id=countdown>–</span></div>
   <small>synced to foxess 5-min refresh</small></div>
 <div class=card style="{'background:#fff3e0;border-color:#e67e22' if (snap.get('scheduler') or {}).get('active') and snap['scheduler']['active']['mode']=='ForceCharge' else ''}">
   <small>Force-charge</small><div class=big>{('⚡ ON' if (snap.get('scheduler') or {}).get('active') and snap['scheduler']['active']['mode']=='ForceCharge' else ('sched on' if (snap.get('scheduler') or {}).get('enabled') else 'OFF'))}</div>
   <small>{(snap.get('scheduler') or {}).get('active',{}).get('window','') if (snap.get('scheduler') or {}).get('active') else ''}</small></div>
</div>
<div class="card rec"><small>RECOMMENDATION</small>
 <div class=big>{rec.get('action')} → {rec.get('target_mode')} {'⚡FORCE-CHARGE' if rec.get('force_charge') else ''}</div>
 <div>{rec.get('reason','')}</div>
 <div><small>applied: {snap.get('applied')} · control: allow={ctrl.get('allow_control')} auto_apply={ctrl.get('auto_apply')} force_charge={ctrl.get('set_force_charge')}</small></div>
</div>
<h3>Make things happen</h3>
<button onclick="post('/api/evaluate')">Evaluate now</button>
<button onclick="post('/api/apply')">Apply recommendation</button>
<button class=danger onclick="if(confirm('Grid-charge for 10 min to {cfg['strategy']['target_soc']}%?'))post('/api/force_charge_test')">⚡ Test force-charge (10 min)</button>
<button class=danger onclick="post('/api/scheduler_off')">Stop / disable scheduler</button>
<div id=msg></div>
<h3>Actions taken <small>(real changes: applies, force-charge, disables, band actions)</small></h3>
<table id=events></table>
<h3>Next forecast</h3>{atable}
<h3>Decision log <small>(every cycle, recommendation even when not applied)</small></h3>
<table id=log></table>
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
                msg = apply_recommendation(cfg, snap)
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
            else:
                self._send(404, "not found")
    return H


def serve(cfg: dict):
    Thread(target=loop, args=(cfg,), daemon=True).start()
    host, port = cfg["web"]["host"], cfg["web"]["port"]
    httpd = ThreadingHTTPServer((host, port), make_handler(cfg))
    print(f"foxctl web on http://{host}:{port}  (loop every {cfg['poll_seconds']}s)")
    httpd.serve_forever()


def loop(cfg: dict):
    auto = cfg["control"].get("allow_control") and cfg["control"].get("auto_apply")
    poll = cfg["poll_seconds"]
    lag = int(cfg.get("sync_lag_seconds", 20))  # read shortly AFTER foxess-ha refreshes
    while True:
        try:
            snap = run_once(cfg, do_apply=auto)
            r = snap["recommendation"]
            print(f"{snap['ts']} price={snap['price']} soc={snap['soc']:.0f}% "
                  f"mode={snap['work_mode']} -> {r['action']}/{r['target_mode']}"
                  + (f" applied={snap['applied']}" if auto else ""))
        except Exception as e:
            snap = None
            print(f"{datetime.now().isoformat(timespec='seconds')} ERROR: {e}", file=sys.stderr)
        # Sync next poll to the foxess-ha update cycle: next HA refresh ≈ last_updated + poll.
        now = time.time()
        ts = (snap or {}).get("soc_updated_epoch")
        if ts:
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
