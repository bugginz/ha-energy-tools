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
        "amber_feedin_forecast_entity": "sensor.home_feed_in_forecast",
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
        "spike_sell_buffer_kwh": 0.0,  # strategist lever: extra import beyond survival, for spike-sell readiness
        # TOP-UP mode: buy (cheaply) toward target_soc to stay full for spike-sell readiness, instead of
        # only covering the survival deficit. Still bounded by the ceiling + cheapest-slot selection.
        "topup_to_target": True,
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
        # The ONLY min-SoC foxctl ever writes to the inverter — a constant safety floor, never a
        # computed survival level. Keep it low and matching the FoxESS app's own min-SoC; survival and
        # export buffers are enforced in software (when to stop charging/selling), never on the device.
        "inverter_min_soc": 10,
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
    # model = the thorough strategist; fallback_model = a cheaper model used if the primary API call fails.
    "llm": {"enabled": False, "api_key": "", "model": "claude-opus-4-8",
            "fallback_model": "claude-haiku-4-5", "interval_min": 30},
    # Push notifications when a decision is worth a human look (via HA notify service).
    "notify": {"enabled": False, "service": "notify.mobile_app_phoney",
               "on_llm_disagree": True, "on_spike": True, "on_ludicrous": True},
    # Solar diversion: turn a car-charger power point ON when export is too cheap to bother (and/or grid
    # is cheap), OFF otherwise. Needs control.allow_control. switch="" disables. Tracked via ev_power_entity.
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


class HAPrices:
    """Reads Amber price + forecast from Home Assistant (reuses the HA token)."""

    def __init__(self, url: str, token: str, price_entity: str, forecast_entity: str,
                 aemo_forecast_entity: str | None = None, feedin_entity: str | None = None,
                 feedin_forecast_entity: str | None = None):
        self.url, self.token = url.rstrip("/"), token
        self.price_entity, self.forecast_entity = price_entity, forecast_entity
        self.aemo_forecast_entity = aemo_forecast_entity
        self.feedin_entity = feedin_entity
        self.feedin_forecast_entity = feedin_forecast_entity

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
        feedin, feedin_fc = None, []
        if self.feedin_entity:
            try:
                feedin = float(self._state(self.feedin_entity)["state"])
            except Exception:
                feedin = None  # entity not present yet (no solar/feed-in channel)
        # Feed-in forecast: prefer a dedicated forecast sensor (mirrors the general forecast sensor);
        # fall back to a 'forecasts' attribute on the feed-in price sensor if that's where it lives.
        for src in (self.feedin_forecast_entity, self.feedin_entity):
            if not src:
                continue
            try:
                raw = self._state(src)["attributes"].get("forecasts", [])
                if raw:
                    feedin_fc = [{"t": p.get("nem_date"), "price": p.get("per_kwh")} for p in raw]
                    break
            except Exception:
                pass
        # Align the forecast sign to the live feed-in reading (Amber sometimes reports the forecast with
        # the opposite/raw sign), so "earning to export" stays positive like the rest of the logic expects.
        vals = [f["price"] for f in feedin_fc if isinstance(f.get("price"), (int, float))]
        if feedin not in (None, 0) and vals:
            med = sorted(vals)[len(vals) // 2]
            if med != 0 and (feedin > 0) != (med > 0):
                for f in feedin_fc:
                    if isinstance(f.get("price"), (int, float)):
                        f["price"] = -f["price"]
        return {
            "price": price,
            "descriptor": cur["attributes"].get("descriptor") if isinstance(cur.get("attributes"), dict) else None,
            "forecast": fc,
            "aemo_price": aemo_price,
            "aemo_forecast": aemo_fc,
            "feedin": feedin,
            "feedin_forecast": feedin_fc,
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


def plan_buy_slots(fc, price_now, now, deficit_kwh, charge_power_kw, ceiling, floor,
                   horizon_h=18, slot_h=0.5):
    """Need-based, RELATIVE buy planner — the Phase 2 foundation rule.

    "Cheap" is relative to the forward forecast and sized to what we actually need: forecast the
    import deficit (what trend data says we must buy to bridge to the next solar window), then accept
    only the cheapest forward slots that cover it. The affordability 'bar' rises when the forecast is
    dear and falls when it's cheap — but never exceeds the ceiling. At/below the operator's floor we
    always top up (cheap insurance). No deficit → no import.

    Returns {should_charge, bar, slots_needed, deficit_kwh, eligible, reason}.
    """
    out = {"should_charge": False, "bar": None, "slots_needed": 0,
           "deficit_kwh": round(float(deficit_kwh or 0.0), 1), "eligible": 0, "reason": ""}
    # Always-OK floor: at/below the operator's floor we top up regardless (it's cheap insurance, and
    # leaves the battery ready to ride expensive periods / export into a spike).
    if price_now is not None and floor is not None and price_now <= floor:
        out.update(should_charge=True, bar=round(float(floor), 3),
                   reason=f"price {price_now:.3f} ≤ floor {floor:.3f} (always-OK)")
        return out
    if price_now is None:
        out["reason"] = "no price"
        return out
    if price_now > ceiling:
        out["reason"] = f"price {price_now:.3f} > ceiling {ceiling:.3f}"
        return out
    if (deficit_kwh or 0.0) <= 0:
        out.update(bar=round(float(floor), 3), reason="no forward deficit — only top up at/below floor")
        return out
    # Buyable candidates: now + each forward slot within the horizon, only at/below the ceiling.
    cand = [float(price_now)]
    for p in fc:
        t = _parse_t(p.get("t") or "")
        pr = p.get("price")
        if t and pr is not None:
            dt = (t - now).total_seconds()
            if 0 < dt <= horizon_h * 3600 and pr <= ceiling:
                cand.append(float(pr))
    energy_per_slot = max(0.01, charge_power_kw * slot_h)
    slots_needed = max(1, math.ceil(deficit_kwh / energy_per_slot))
    buyable = sorted(cand)
    k = min(slots_needed, len(buyable))
    bar = round(buyable[k - 1], 3)            # most expensive slot we'd accept given the need
    out.update(slots_needed=slots_needed, eligible=len(buyable), bar=bar,
               should_charge=(price_now <= bar))
    out["reason"] = (f"need {out['deficit_kwh']:.1f}kWh → cheapest {slots_needed} slot(s) of {len(buyable)}; "
                     f"bar ${bar:.3f}; now ${price_now:.3f} {'≤' if out['should_charge'] else '>'} bar")
    return out


def buy_target_kwh(soc, cap_kwh, eff_target, survival_def, solar_remaining, topup, buffer=0.0):
    """How much grid energy the buy planner should aim to acquire this cycle.

    NEED-BASED (topup=False): just the survival deficit (bridge to the next solar ramp). TOP-UP
    (topup=True, operator preference): also fill the headroom to the charge cap — less today's
    remaining solar, so we don't pay grid for what the sun will still give — to stay full for
    spike-sell readiness. Plus the strategist's spike-sell buffer. The cheapest-slot selection +
    ceiling still decide WHEN/IF to actually buy, so top-up fills cheaply, never at premium."""
    need = max(0.0, float(survival_def or 0.0))
    if topup:
        headroom = max(0.0, (eff_target - soc) / 100.0 * cap_kwh)
        need = max(need, max(0.0, headroom - (solar_remaining or 0.0)))
    return round(need + float(buffer or 0.0), 1)


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

    # FOUNDATION (Phase 2): import is NEED-BASED and RELATIVE — buy only the cheapest forward slots
    # that cover the forecast deficit (trend-derived), never an absolute "charge below $X". floor =
    # always-OK; ceiling = hard veto; no deficit → no import.
    floor = strat.get("charge_start_floor", 0.0)
    deficit = strat.get("import_deficit_kwh", strat.get("energy_shortfall_kwh", 0.0)) or 0.0
    # Strategist's optional cap on the relative bar — refuse to buy above $X even when needed (tighter
    # than, never looser than, the foundation ceiling).
    buy_ceiling = min(ceiling, float(strat.get("buy_bar_cap", ceiling)))
    buy = plan_buy_slots(fc, price, now, deficit, strat.get("force_charge_power_kw", 10.5),
                         buy_ceiling, floor, horizon_h=horizon_h, slot_h=strat.get("slot_hours", 0.5))
    bar = buy.get("bar")
    stop_margin = strat.get("charge_stop_margin", 0.02)

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
    elif price > ceiling:
        reasons.append(f"FOUNDATION: price {price:.3f} > ceiling {ceiling:.3f} → refuse grid import. SelfUse.")
        action, target_mode = "SET_MODE", "SelfUse"
    elif solar_covering:
        reasons.append(f"Solar surplus {solar_surplus:.1f}kW covers load/need (no projected shortfall) → "
                       f"no grid charge. SelfUse.")
        action, target_mode = "SET_MODE", "SelfUse"
    elif buy["should_charge"]:
        reasons.append(f"NEED-BASED BUY: {buy['reason']} → force-charge to {target}%.")
        action, force_charge = "FORCE_CHARGE", True
    elif currently_charging and bar is not None and price <= bar + stop_margin and soc < target:
        reasons.append(f"Continuing charge: price {price:.3f} ≤ bar {bar:.3f}+{stop_margin:.2f} (hysteresis).")
        action, force_charge = "FORCE_CHARGE", True
    else:
        reasons.append(f"Hold ({buy['reason']}). SelfUse.")
        action, target_mode = "SET_MODE", "SelfUse"

    # FOUNDATION guardrail (defensive double-check): never grid-charge above the absolute price ceiling
    # (ludicrous/negative is free money and exempt).
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
        "buy_bar": buy.get("bar"),
        "buy_slots_needed": buy.get("slots_needed"),
        "import_deficit_kwh": buy.get("deficit_kwh"),
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
_WM = {"value": None, "options": None, "i": 0, "ts": 0.0, "min_soc": None}  # work-mode + device min-SoC cache
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
# Persistent strategist conversation (mission-anchored, spans days): [{role, content, kind, ts}].
# kind="policy" = automated state→knobs turns; kind="chat" = free-text operator dialogue.
_CHAT = {"loaded": False, "msgs": []}
CHAT_MAX_CHAT = 40      # retain the last N free-text chat messages (operator memory)
CHAT_KEEP_POLICY = 2    # retain only the most recent policy exchange (routine telemetry would bloat it)
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


# ---- persistent strategist chat (mission + rolling history, referenceable over time) ----

def load_chat(cfg):
    if not _CHAT["loaded"]:
        try:
            _CHAT["msgs"] = json.loads((_state_dir(cfg) / "llm_chat.json").read_text()).get("msgs", [])
        except Exception:
            _CHAT["msgs"] = []
        _CHAT["loaded"] = True
    return _CHAT["msgs"]


def save_chat(cfg):
    try:
        p = _state_dir(cfg) / "llm_chat.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"msgs": _CHAT["msgs"]}))
    except Exception as e:
        print(f"chat persist failed: {e}", file=sys.stderr)


def _chat_add(role, content, kind):
    _CHAT["msgs"].append({"role": role, "content": content, "kind": kind,
                          "ts": datetime.now().isoformat(timespec="seconds")})


def _prune_chat():
    """Bound the history: keep the last CHAT_MAX_CHAT free-text chat messages (the operator memory) plus
    only the most recent policy exchange; routine state→knobs turns would otherwise dominate. Index-based
    so original chronological order (and user/assistant alternation within exchanges) is preserved."""
    msgs = _CHAT["msgs"]
    chat_idx = [i for i, m in enumerate(msgs) if m.get("kind") != "policy"][-CHAT_MAX_CHAT:]
    pol_idx = [i for i, m in enumerate(msgs) if m.get("kind") == "policy"][-CHAT_KEEP_POLICY:]
    keep = sorted(set(chat_idx) | set(pol_idx))
    _CHAT["msgs"] = [msgs[i] for i in keep]


def _api_messages(msgs):
    """Turn the stored history into a valid Messages payload: merge consecutive same-role turns and drop
    any leading assistant turn so it always starts with a user message."""
    out = []
    for m in msgs:
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"] += "\n\n" + m.get("content", "")
        else:
            out.append({"role": m["role"], "content": m.get("content", "")})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def chat_view(cfg, n=24):
    load_chat(cfg)
    return _CHAT["msgs"][-n:]


def clear_chat(cfg):
    """Wipe the persistent strategist conversation (and force a fresh policy review) — e.g. to drop stale
    reasoning after the mission/capabilities change."""
    _CHAT["msgs"] = []
    _CHAT["loaded"] = True
    save_chat(cfg)
    _LLM["last_ts"] = 0.0
    _LLM["last"] = None
    log_event("chat", "strategist conversation cleared")
    return {"cleared": True}


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
    """One past day → (load_hours[24] from report `loads`, solar_hours[24] from pvPower history)."""
    load_hours = [0.0] * 24
    for it in fox.report(["loads"], "day", day):
        if it.get("variable") == "loads":
            vals = (list(it.get("values") or []) + [0.0] * 24)[:24]
            load_hours = [round(float(v), 3) if isinstance(v, (int, float)) else 0.0 for v in vals]
    begin = int(datetime(day.year, day.month, day.day).timestamp() * 1000)
    solar_hours = [0.0] * 24
    res = fox.history(["pvPower"], begin, begin + 24 * 3600 * 1000)
    for ds in ((res[0].get("datas") if res else None) or []):
        if ds.get("variable") == "pvPower":
            solar_hours = _integrate_hourly(ds.get("data") or [])
    return load_hours, solar_hours


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
    load_avg, load_min, load_max = stats("load")
    solar_avg, solar_min, solar_max = stats("solar")
    lvalid = valid_for("load")
    totals = [round(sum(x for x in d["load"] if isinstance(x, (int, float))), 1) for d in lvalid]
    daily = {"avg": round(sum(totals) / len(totals), 1), "min": min(totals), "max": max(totals)} if totals else {}
    return {"days": len(lvalid), "days_solar": len(valid_for("solar")),
            "load_profile": load_avg, "load_min": load_min, "load_max": load_max,
            "solar_profile": solar_avg, "solar_min": solar_min, "solar_max": solar_max,
            "daily_total": daily}


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
            lh, sh = fetch_forecast_day(fox, datetime.strptime(d, "%Y-%m-%d"))
            _FCAST["days"][d] = {"load": lh, "solar": sh}
            save_fcast(cfg)
            log_event("forecast", f"backfilled {d}: load={round(sum(lh),1)}kWh solar={round(sum(sh),1)}kWh "
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
            lh, sh = fetch_forecast_day(fox, day)
            _FCAST["days"][dk] = {"load": lh, "solar": sh}
    save_fcast(cfg)
    series = build_stat_series(days)
    if not series:
        return {}
    done = ha_import_statistics(cfg, series)
    log_event("backfill", f"HA statistics backfill: {done}")
    return done


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

# The frozen "overall mission" that anchors the PERSISTENT strategist conversation. It is sent as the
# system prompt (prompt-cached) on every call so the running dialogue — automated policy turns AND the
# operator's free-text chat — stays grounded in one continuous mission across days.
MISSION = (
    "You are foxctl's strategist: the persistent dynamic-policy advisor for a REAL home solar-battery "
    "system in the Australian NEM (NSW). This is ONE continuous conversation that spans days — earlier "
    "turns are your own prior reasoning about this same home, and the human operator also talks to you "
    "here directly. Maintain continuity: remember what you advised before, learn from how prices and "
    "solar actually played out, and keep the operator's standing intentions in mind.\n\n"
    + GOAL + "\n\n"
    "WHAT THE CONTROLLER DOES AUTOMATICALLY (you do NOT need a human for any of this — and must NOT ask "
    "for it):\n"
    "- AUTO-SELL: it force-discharges the battery to the grid whenever the feed-in/export price is at or "
    "above the sell threshold, automatically holding back an overnight survival buffer. Price spikes are "
    "ALREADY captured this way — never tell the operator to 'start exporting' or 'manually stop exporting' "
    "during a spike; that happens on its own.\n"
    "- AUTO FORCE-CHARGE is NEED-BASED and RELATIVE: each cycle the controller forecasts the import "
    "deficit to the next solar ramp, then buys only during the CHEAPEST forward slots that cover it "
    "(an affordability 'bar' that rises when the forecast is dear, falls when it's cheap, capped by the "
    "ceiling; at/below the floor it always tops up). You do NOT set an absolute buy price — that is "
    "computed. No deficit → no import.\n"
    "- AUTO EV-DIVERT: when enabled it sends cheap surplus to the car charger on its own.\n"
    "These run every cycle without you. The automation's current settings and state are in the context "
    "('automation' + 'foundation' + 'buy'); read them so you don't recommend something already happening.\n\n"
    "WHO CONTROLS WHAT:\n"
    "- YOUR levers (auto-applied, hard-clamped) — three relative nudges only:\n"
    "  • target_soc (%): the charge cap.\n"
    "  • spike_sell_buffer_kwh: extra energy to import beyond strict survival so the battery carries "
    "something to EXPORT into an improbable-but-possible spike. 0 = buy only what's needed; raise it when "
    "a fat sell looks plausible. Clamped to ≤ half the pack.\n"
    "  • buy_bar_cap ($/kWh): refuse to buy above this even when needed (tighter than the ceiling) — use "
    "it to hold out for cheaper when the deficit isn't urgent. Clamped to [floor, ceiling].\n"
    "- The OPERATOR's levers (the ONLY things operator_action may ask for): price_ceiling, "
    "charge_start_floor, max_soc, battery_capacity_kwh, the sell threshold / turning auto-sell on or off, "
    "and the manual force-charge / sell / floor override buttons. Never put a routine or automatic action "
    "in operator_action — leave it empty unless you genuinely need the human to change one of THOSE.\n\n"
    "Two kinds of message arrive:\n"
    "1. A message beginning 'POLICY CONTEXT' is an automated state + forecast update. Reply with ONLY a "
    "JSON object (no prose, no code fences) setting your relative levers:\n"
    '{"target_soc": <int %>, "spike_sell_buffer_kwh": <num ≥0>, '
    '"buy_bar_cap": <num $/kWh or null to leave at the ceiling>, '
    '"rating": "AGREE"|"REFINE"|"DISAGREE", "reason": "<=2 sentences", '
    '"operator_action": "<short thing that REQUIRES the human — e.g. change a foundation setting they '
    'control (price_ceiling, charge_start_floor, max_soc, battery_capacity_kwh) — or empty string>", '
    '"base_floor": <number or null — set ONLY when an operator note clearly asks for a LASTING change to '
    'the minimum price always willing to charge at (the base floor); else null>}. '
    "Stay within the foundation guardrails in the context (your values are hard-clamped anyway). Keep "
    "spike_sell_buffer_kwh at 0 unless a profitable export window genuinely looks likely; the controller "
    "already buys at the cheapest forecast points. If operator_note is present it is a DIRECT instruction "
    "— follow it as a priority within the guardrails. rating: AGREE=same as baseline, REFINE=minor auto "
    "tweak, DISAGREE=you think the policy/state is wrong. Only fill operator_action when you genuinely "
    "need a human to change something you cannot; leave it empty for normal auto-applied tuning.\n"
    "2. Any other message is the human operator talking to you. Reply conversationally and concisely (a "
    "few sentences) — explain your reasoning, answer questions about strategy, or acknowledge new standing "
    "guidance and carry it forward. Do NOT emit the policy JSON for these.")


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


def _supports_adaptive(model):
    """Adaptive thinking is an Opus 4.x / Sonnet 4.6 feature; older/Haiku models would 400 on it."""
    return model.startswith("claude-opus") or model == "claude-sonnet-4-6"


def _llm_post(api_key, model, mission, messages, max_tokens, timeout=60):
    """One raw Messages API call (urllib, no SDK — keeps the add-on dependency-free). The frozen mission
    is the system prompt with cache_control so its tokens are reused across the running conversation."""
    body = {"model": model, "max_tokens": max_tokens,
            "system": [{"type": "text", "text": mission, "cache_control": {"type": "ephemeral"}}],
            "messages": messages}
    if _supports_adaptive(model):
        body["thinking"] = {"type": "adaptive"}   # let the thorough model reason before answering
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(), method="POST",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text").strip()
    return text, d.get("usage", {})


def _is_transient_api(e):
    """529 (Overloaded), 429 (rate limit) and 5xx are transient — worth RETRYING the SAME model before
    dropping to the fallback. A 400/404 (bad model / no access) is permanent — fall back immediately."""
    return getattr(e, "code", None) in (429, 500, 502, 503, 529)


def _llm_call(api_key, model, fallback, mission, messages, max_tokens=2048, retries=3):
    """Call the thorough primary model, RETRYING it on transient API errors (esp. 529 Overloaded) with a
    short backoff before falling back to the cheaper/robust model. Returns (text, used_model, usage)."""
    last = None
    for attempt in range(retries):
        try:
            text, usage = _llm_post(api_key, model, mission, messages, max_tokens)
            return text, model, usage
        except Exception as e:
            last = e
            if _is_transient_api(e) and attempt < retries - 1:
                time.sleep(min(8, 2 ** attempt))      # 1s, 2s, 4s — ride out a transient overload
                continue
            break
    if fallback and fallback != model:
        log_event("llm", f"primary {model} failed after {retries} tries ({last}); falling back to {fallback}")
        text, usage = _llm_post(api_key, fallback, mission, messages, max_tokens)
        return text, fallback, usage
    raise last


def _llm_dynamic(cfg, api_key, model, fallback, ctx):
    """A POLICY turn in the persistent strategist conversation: append the state as a 'POLICY CONTEXT'
    message, ask for the two knobs (grounded in the mission + prior turns), and record the exchange.
    Returns {params, rating, text, operator_action, base_floor, ts, model}."""
    load_chat(cfg)
    user = "POLICY CONTEXT (JSON):\n" + json.dumps(ctx)
    pending = _CHAT["msgs"] + [{"role": "user", "content": user, "kind": "policy"}]
    text, used, usage = _llm_call(api_key, model, fallback, MISSION, _api_messages(pending), max_tokens=4096)
    # Commit the exchange only now that the call succeeded (a failed call leaves history untouched).
    _chat_add("user", user, "policy")
    _chat_add("assistant", text, "policy")
    _prune_chat(); save_chat(cfg)
    obj = _extract_json(text)
    rating = str(obj.get("rating", "")).upper()
    rating = rating if rating in ("AGREE", "REFINE", "DISAGREE") else "REFINE"
    params = {}
    if isinstance(obj.get("target_soc"), (int, float)):
        params["target_soc"] = int(obj["target_soc"])
    if isinstance(obj.get("spike_sell_buffer_kwh"), (int, float)):
        params["spike_sell_buffer_kwh"] = float(obj["spike_sell_buffer_kwh"])
    if isinstance(obj.get("buy_bar_cap"), (int, float)):
        params["buy_bar_cap"] = float(obj["buy_bar_cap"])
    action = str(obj.get("operator_action", "") or "").strip()
    bf = obj.get("base_floor")
    return {"params": params, "rating": rating, "agree": rating == "AGREE",
            "text": obj.get("reason", text)[:400], "operator_action": action[:300],
            "base_floor": float(bf) if isinstance(bf, (int, float)) else None,
            "ts": datetime.now().isoformat(timespec="seconds"), "model": used}


def llm_chat_reply(cfg, text):
    """Operator sends a free-text message to the persistent strategist (same mission + shared history, so
    they can reference earlier discussion). Records the exchange; returns {reply, model} or {error}."""
    llm = cfg.get("llm", {})
    if not llm.get("enabled") or not llm.get("api_key"):
        return {"error": "LLM disabled — enable llm_review and set anthropic_api_key in the add-on options"}
    text = (text or "").strip()[:2000]
    if not text:
        return {"error": "empty message"}
    load_chat(cfg)
    pending = _CHAT["msgs"] + [{"role": "user", "content": text, "kind": "chat"}]
    try:
        reply, used, _ = _llm_call(llm["api_key"], llm.get("model", "claude-opus-4-8"),
                                   llm.get("fallback_model", "claude-haiku-4-5"),
                                   MISSION, _api_messages(pending), max_tokens=2048)
    except Exception as e:
        return {"error": f"LLM error: {e}"}
    _chat_add("user", text, "chat")
    _chat_add("assistant", reply, "chat")
    _prune_chat(); save_chat(cfg)
    log_event("chat", f"operator: {text[:80]} → {reply[:120]}")
    return {"reply": reply, "model": used}


def apply_dynamic_params(strat, params, foundation):
    """Merge the strategist's relative levers into a copy of strat, hard-clamped to the foundation
    guardrails. Phase 3 levers (the only things it can nudge): target_soc (charge cap), a spike-sell
    buffer (extra kWh to import beyond strict survival, so there's something to export into a spike),
    and a cap on the relative buy bar (refuse to buy above $X even when needed). Everything else —
    the need-based buy slot selection, floor, ceiling, sell threshold — stays deterministic."""
    out = dict(strat)
    floor = float(foundation.get("charge_start_floor", 0.0))
    ceiling = float(foundation["price_ceiling"])
    cap_kwh = float(strat.get("battery_capacity_kwh", 30))
    ts = params.get("target_soc")
    if isinstance(ts, (int, float)):
        lo, hi = strat.get("reserve_soc", 20) + 10, foundation["max_soc"]
        out["target_soc"] = int(max(lo, min(int(ts), hi)))
    buf = params.get("spike_sell_buffer_kwh")
    if isinstance(buf, (int, float)):
        out["spike_sell_buffer_kwh"] = round(max(0.0, min(float(buf), 0.5 * cap_kwh)), 1)  # ≤ half the pack
    cap = params.get("buy_bar_cap")
    if isinstance(cap, (int, float)):
        out["buy_bar_cap"] = round(max(floor, min(float(cap), ceiling)), 3)               # within [floor, ceiling]
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
        v = _llm_dynamic(cfg, llm["api_key"], llm.get("model", "claude-opus-4-8"),
                         llm.get("fallback_model", "claude-haiku-4-5"), ctx)
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
    """Pure policy: should the car charger be ON this cycle? Diverts when export is too cheap (≥min_export
    at ≤feedin_max) and/or grid is cheap. On the SOLAR-surplus path it yields to the house battery until
    the battery reaches the (shadow-planner) target (so the battery fills before a sell); on the CHEAP-GRID
    path it charges the car alongside the battery top-off (no yield — unlimited cheap import). (want, why)."""
    feedin_price, feedin_power, buy = snap.get("feedin"), snap.get("feedin_power") or 0.0, snap.get("price")
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
    surplus = (feedin_power >= ev.get("min_export_kw", 1.0)
               and feedin_price is not None and feedin_price <= ev.get("feedin_max", 0.10))
    charge_start = (snap.get("dynamic") or {}).get("charge_start_price")
    cheap_grid = bool(ev.get("allow_grid")) and buy is not None and charge_start is not None and buy <= charge_start
    if not (surplus or cheap_grid):
        return False, "export not cheap / no surplus"
    # Battery priority applies ONLY to the limited solar-surplus path — give the spare solar to the
    # battery before a sell. Cheap GRID import is unlimited, so it can top the battery AND charge the
    # car at the same cheap price; don't gate that path.
    if surplus and not cheap_grid:
        gate = ev.get("min_soc", 0) or 0
        plan_tgt = (snap.get("plan") or {}).get("target_now")
        if ev.get("battery_priority", True) and isinstance(plan_tgt, (int, float)):
            gate = max(gate, plan_tgt - 2)
        if isinstance(soc, (int, float)) and soc < gate:
            return False, f"battery {soc:.0f}% < target {gate:.0f}% (solar to battery first)"
    why = (f"export ${feedin_price:.2f}≤{ev.get('feedin_max',0.10):.2f} @ {feedin_power:.1f}kW" if surplus
           else f"grid ${buy:.3f}≤charge-start (cheap — charging car + battery)")
    return True, why


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
    ("foxctl_load_energy", "House load energy", "kWh", "energy", "total_increasing"),
    ("foxctl_ev_power", "EV charger power", "kW", "power", "measurement"),
    ("foxctl_ev_energy", "EV charger energy", "kWh", "energy", "total_increasing"),
    ("foxctl_ev_charger", "EV charger state", None, None, None),
    ("foxctl_charge_start", "Charge-start price", "$/kWh", None, None),
    ("foxctl_target_soc", "Target SoC", "%", None, None),
    # Forecast + shadow-planner metrics (Phase 2-4) so they're graphable/automatable in HA.
    ("foxctl_plan_target_soc", "Plan target SoC", "%", "battery", "measurement"),
    ("foxctl_plan_action", "Plan action", None, None, None),
    ("foxctl_energy_shortfall", "Energy shortfall", "kWh", "energy", "measurement"),
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
                "charge_start": dyn.get("charge_start_price"), "target_soc": dyn.get("target_soc")}
        plan = snap.get("plan") or {}
        sf = snap.get("solar_forecast") or {}
        sc = snap.get("solar_cal") or {}
        cons = snap.get("consumption") or {}
        tele.update({"plan_target_soc": plan.get("target_now"), "plan_action": plan.get("action_now"),
                     "energy_shortfall": snap.get("energy_shortfall_kwh"),
                     "solar_remaining": sf.get("remaining_today"), "solar_tomorrow": sf.get("tomorrow"),
                     "solar_cal_bias": sc.get("bias"), "avg_daily_load": cons.get("avg_daily_total_kwh"),
                     "ev_power": snap.get("ev_kw"), "ev_energy": et.get("ev"),
                     "ev_charger": ("on" if _EV.get("on") else "off") if (cfg.get("ev_divert") or {}).get("switch") else "n/a"})
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
    ps, pe = z.get("peak_start_h", 16), z.get("peak_end_h", 23)   # full ToU peak (no import)
    max_soc = strat.get("max_soc", 90)
    reserve = strat.get("reserve_soc", 20)
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
        reasons.append(f"ZeroHero FREE window {fs:02d}:00–{fe:02d}:00 → grid-charge to {max_soc}% (free) — full by {fe:02d}:00.")
    elif in_free:
        reasons.append(f"ZeroHero free window, battery full ({soc:.0f}% ≥ {max_soc}%). SelfUse.")
    elif in_eve and soc > survival_soc + 1:
        action, fd = "SELL", True
        reasons.append(f"ZeroHero export {es:02d}:00–{ee:02d}:00 (Super Export) → cover load (zero import) + "
                       f"export surplus down to survival {survival_soc}% (keeps enough to coast to 11:00).")
    elif in_eve:
        reasons.append(f"ZeroHero export {es:02d}:00–{ee:02d}:00 → hold at survival; cover load from battery "
                       f"(zero grid import). SelfUse.")
    elif in_peak:
        reasons.append(f"ZeroHero PEAK {ps:02d}:00–{pe:02d}:00 → cover load from battery, ZERO grid import "
                       f"(no force-charge in peak). SelfUse.")
    elif soc <= reserve:
        reasons.append(f"ZeroHero off-window but SoC {soc:.0f}% ≤ reserve {reserve}% — battery low. SelfUse.")
    else:
        reasons.append("ZeroHero off-window → run off battery, avoid grid import until the 11:00 free window. SelfUse.")
    rec = {"action": action, "target_mode": target_mode, "force_charge": fc, "force_discharge": fd,
           "sell_floor": survival_soc, "band": "zerohero", "min_future_h": None, "peak_future_h": None,
           "reason": " ".join(reasons)}
    if fc:
        rec["force_charge_plan"] = {"window": f"{fs:02d}:00–{fe:02d}:00 free", "max_soc": max_soc,
                                    "min_soc_on_grid": strat.get("min_soc_on_grid", 10),
                                    "power_kw": strat.get("force_charge_power_kw", 10.5)}
    return rec


def _solar_kw_at(bells, h):
    """Sample the half-sine solar bells (forecast kW) at `h` hours-from-now."""
    tot = 0.0
    for b in bells:
        if b["s"] <= h <= b["e"] and b["pmax"] > 0:
            frac = (h - b["s"]) / ((b["e"] - b["s"]) or 1)
            tot += b["pmax"] * math.sin(math.pi * min(max(frac, 0), 1))
    return tot


_PLAN = {"last_class": None}


def plan_soc_trajectory(slots, soc0_pct, cap_kwh, p):
    """Requirement-aware 'ideal SoC line' over the forecast horizon. SHADOW ONLY — never drives control.

    Backward pass builds a minimum-SoC *envelope*: walking from the horizon end, every expensive slot's
    net-load (load − solar) is something we'd rather cover from the battery than import dear, so it must
    already be stored entering that slot; cheap slots relax the requirement (we can refill there). The
    forward pass then simulates the cost-minimising dispatch that respects the envelope: solar serves
    load first, grid-charge happens only in cheap slots (up to what the envelope ahead needs), and we
    sell when the price clears the threshold while staying above survival.

    slots: [{h, price, load, solar, dt}] in time order. Returns the SoC line + floor envelope (both as
    [(h, pct)]) and the recommended action/target for the *current* slot."""
    n = len(slots)
    if n == 0:
        return {"soc_line": [], "floor_line": [], "action_now": "hold", "target_now": round(soc0_pct, 1)}
    reserve = cap_kwh * p["reserve"] / 100.0
    mx = cap_kwh * p["max_soc"] / 100.0
    surv = cap_kwh * max(p["reserve"], p.get("survival", p["reserve"])) / 100.0
    cstart, sell_thr = p["charge_start"], p.get("sell_thr")
    sell_on, eff, cpwr = bool(p.get("sell_on")), p.get("eff", 0.92), p.get("charge_kw", 10.5)
    # backward pass: minimum SoC (kWh) entering each slot to cover future expensive net-load from battery
    req = [reserve] * (n + 1)
    for i in range(n - 1, -1, -1):
        net = max(0.0, slots[i]["load"] - slots[i]["solar"])
        step = cpwr * slots[i]["dt"]
        req[i] = (req[i + 1] + net) if slots[i]["price"] > cstart else (req[i + 1] - step)
        req[i] = max(reserve, min(mx, req[i]))
    # ARBITRAGE target: floor + the export capacity of future sell-windows, so a cheap slot fills toward
    # max_soc to capture a profitable spread (buy now, sell into the spike later) — not just to cover load.
    want = list(req)
    arb_on = bool(p.get("arbitrage", True)) and sell_on and sell_thr is not None
    if arb_on:
        sell_ahead = 0.0
        for i in range(n - 1, -1, -1):
            if slots[i].get("sell_price", slots[i]["price"]) >= sell_thr:   # real feed-in forecast if present
                sell_ahead += cpwr * slots[i]["dt"]      # this slot can export this much later
            want[i] = min(mx, req[i] + sell_ahead)
    # forward pass: simulate the ideal dispatch following the envelope
    soc, line, floor, act0, tgt0 = cap_kwh * soc0_pct / 100.0, [], [], "hold", None
    for i, s in enumerate(slots):
        net, price, step = s["load"] - s["solar"], s["price"], cpwr * s["dt"]
        action = "hold"
        if net < 0:                                       # solar surplus charges the battery
            soc = min(mx, soc + (-net) * eff)
        elif price > cstart:                              # dear: serve the deficit from the battery
            soc -= net
            if net > 0.01:
                action = "discharge"
        # cheap slots: deficit is imported cheaply (battery untouched), and we top up toward the target —
        # the requirement floor, OR (when a future sell beats buying now after efficiency) the arb target.
        profitable = arb_on and sell_thr * eff > price
        target = want[i + 1] if profitable else req[i + 1]
        if price <= cstart and soc < target and soc < mx:
            add = min(step, mx - soc, target - soc)
            if add > 0.01:
                soc += add * eff
                action = "charge"
        if sell_on and sell_thr is not None and s.get("sell_price", price) >= sell_thr and soc > surv:
            sell = min(step, soc - surv)
            if sell > 0.01:
                soc -= sell
                action = "sell"
        soc = max(reserve, min(mx, soc))
        if i == 0:
            act0, tgt0 = action, round(soc / cap_kwh * 100.0, 1)
        line.append((round(s["h"], 3), round(soc / cap_kwh * 100.0, 1)))
        floor.append((round(s["h"], 3), round(req[i] / cap_kwh * 100.0, 1)))
    return {"soc_line": line, "floor_line": floor, "action_now": act0, "target_now": tgt0}


def project_soc_path(slots, soc0_pct, cap_kwh, p):
    """Forward-simulate the rules-based (heuristic) policy's SoC over the horizon: charge toward target
    while buy ≤ charge-start, sell while the feed-in forecast ≥ the threshold and above survival, else
    run on solar−load. Returns [{h, soc, sell}] — the conservative counterpart to plan_soc_trajectory,
    so the two can be compared on the SoC chart."""
    reserve = cap_kwh * 0.10
    mx = cap_kwh * p.get("max_soc", 90) / 100.0
    tgt = cap_kwh * p.get("target_soc", 90) / 100.0
    surv_pct = p.get("survival", 20)
    surv = cap_kwh * surv_pct / 100.0
    cstart, sell_thr = p.get("charge_start", 0.1), p.get("sell_thr")
    sell_on, eff, cpwr = bool(p.get("sell_on")), p.get("eff", 0.92), p.get("charge_kw", 10.5)
    soc, out = cap_kwh * soc0_pct / 100.0, []
    for s in slots:
        price, step = s["price"], cpwr * s["dt"]
        ep = s.get("sell_price", price)
        soc_pct = soc / cap_kwh * 100.0
        sell = sell_on and sell_thr is not None and ep >= sell_thr and soc_pct > surv_pct
        if price <= cstart and soc < tgt:                 # cheap → charge toward target (+ solar surplus)
            soc = min(tgt, soc + step) + max(0.0, s["solar"] - s["load"])
        elif sell:                                         # export to grid down to survival
            soc -= min(step, soc - surv)
        else:
            soc += s["solar"] - s["load"]
        soc = max(reserve, min(mx, soc))
        out.append({"h": round(s["h"], 3), "soc": round(soc / cap_kwh * 100.0, 1), "sell": sell})
    return out


def gather_and_decide(cfg: dict) -> dict:
    fox = FoxESS(cfg["foxess"]["token"], cfg["foxess"]["sn"])
    ha_token = Path(os.path.expanduser(cfg["ha"]["token_file"])).read_text().strip()
    ha = HAPrices(cfg["ha"]["url"], ha_token, cfg["ha"]["amber_price_entity"],
                  cfg["ha"]["amber_forecast_entity"], cfg["ha"].get("aemo_forecast_entity"),
                  cfg["ha"].get("amber_feedin_entity"), cfg["ha"].get("amber_feedin_forecast_entity"))

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
        "baseline": {"target_soc": strat.get("target_soc"),
                     "spike_sell_buffer_kwh": strat.get("spike_sell_buffer_kwh", 0.0),
                     "buy_bar_cap": strat.get("buy_bar_cap")},
        # What the deterministic controller does on its own each cycle, so the strategist doesn't ask a
        # human (or itself) to do something already automatic — e.g. exporting into a price spike.
        "automation": {
            "auto_sell": {
                "enabled": bool(strat.get("sell_enabled", True)),
                "exports_when_feedin_at_or_above": round(sell_eff, 3),
                "keeps_overnight_survival_soc": survival_soc,
                "mechanism": "controller sets the FoxESS scheduler to ForceDischarge and sells the battery "
                             "to grid automatically — no human, app change, or 'Feed-in Priority' toggle "
                             "needed. A high feed-in price during a spike triggers this on its own.",
            },
            "auto_force_charge": "NEED-BASED + RELATIVE: each cycle it forecasts the import deficit to the "
                                 "next solar ramp and buys only the cheapest forward slots that cover it, "
                                 "capped by the ceiling and your buy_bar_cap; floor is always-OK. The buy "
                                 "price is computed, not set. No deficit → no import.",
            "ev_divert_enabled": bool((cfg.get("ev_divert") or {}).get("switch")),
            "your_levers": ["target_soc", "spike_sell_buffer_kwh", "buy_bar_cap"],
            "operator_levers": ["price_ceiling", "charge_start_floor", "max_soc", "battery_capacity_kwh",
                                "sell_threshold", "auto_sell_on_off", "manual override buttons"],
            "note": "Price spikes are captured automatically by auto-sell. Do NOT tell the operator to "
                    "manually export, stop exporting, or switch inverter modes.",
        },
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
        # Import deficit for the NEED-BASED buy planner: forecast load to the next solar ramp (not just
        # to midnight) minus usable battery + remaining solar today. This is what we must import to
        # bridge the expensive overnight window — the "always look forward" the buy rule selects against.
        pred_solar = (predict_base_load(consumption.get("hour_profile"), hrs_to_solar)
                      if consumption.get("profile_days", 0) >= 2 else float(typical_load) * (hrs_to_solar / 24.0))
        # Survival need: what we MUST import to bridge to the next solar ramp. TOP-UP mode (operator
        # preference) additionally fills the headroom to the charge cap so the battery stays full for
        # spike-sell readiness — still bought ONLY in the cheapest forward slots ≤ ceiling.
        survival_def = max(0.0, pred_solar - usable_now - (solar_remaining or 0.0))
        eff_tgt = min(working.get("target_soc", strat.get("target_soc", 100)), foundation["max_soc"])
        working["import_deficit_kwh"] = buy_target_kwh(
            soc, cap_kwh, eff_tgt, survival_def, solar_remaining,
            topup=strat.get("topup_to_target", False),
            buffer=working.get("spike_sell_buffer_kwh", 0.0))
        rec = decide(prices, soc, pv, wm.get("value"), working,
                     currently_charging=charging, load_kw=load, demand_window=demand_window)

    # ---- SHADOW PLANNER (Phase 4): requirement-aware ideal SoC line over the forecast. Does NOT drive
    # control — it's drawn on the chart next to the heuristic projection and logged when they diverge,
    # so the receding-horizon plan can be validated before it's ever allowed to act. ----
    plan, projection, plan_slots = None, None, None
    try:
        now_utc = datetime.now(timezone.utc)
        parsed = []
        for pp in (prices.get("forecast") or []):
            t = _parse_t(pp.get("t") or "")
            if t and pp.get("price") is not None:
                hh = (t - now_utc).total_seconds() / 3600.0
                if -0.2 <= hh <= 24:
                    parsed.append((hh, float(pp["price"]), t))
        parsed.sort()
        hp = consumption.get("hour_profile") or {}
        # per-slot feed-in (export) forecast → real sell prices for the planner (else buy-price proxy)
        fin_map = {}
        for pp in (prices.get("feedin_forecast") or []):
            t = _parse_t(pp.get("t") or "")
            if t and pp.get("price") is not None:
                fin_map[round((t - now_utc).total_seconds() / 3600.0, 2)] = float(pp["price"])
        slots = []
        for i, (hh, price, t) in enumerate(parsed):
            nh = parsed[i + 1][0] if i + 1 < len(parsed) else hh + 0.5
            dt = min(1.5, max(0.05, nh - hh))
            lh = t.astimezone().hour
            load_kwh = (hp.get(lh, hp.get(str(lh))) or 0.0) * dt
            slots.append({"h": hh, "price": price, "dt": dt,
                          "load": load_kwh, "solar": _solar_kw_at(solar_bells, (hh + nh) / 2.0) * dt,
                          "sell_price": fin_map.get(round(hh, 2), price)})
        if slots and not zerohero:
            # Phase 2: the projections + chart must charge on the SAME relative bar the controller uses
            # (need-based cheapest slots), NOT the vestigial absolute charge_start_price — otherwise the
            # SoC line never fills and the chosen cheap windows aren't shaded.
            buy_bar = rec.get("buy_bar")
            charge_thresh = buy_bar if isinstance(buy_bar, (int, float)) \
                else max(foundation.get("charge_start_floor", 0.0),
                         working.get("charge_start_price", strat.get("charge_start_price", 0.1)))
            pp = {"reserve": reserve, "max_soc": foundation["max_soc"], "survival": survival_soc,
                  "charge_start": charge_thresh,
                  "sell_thr": sell_eff, "sell_on": bool(strat.get("sell_enabled", True)),
                  "charge_kw": strat.get("force_charge_power_kw", 10.5), "eff": 0.92}
            plan = plan_soc_trajectory(slots, soc, cap_kwh, pp)
            proj_pp = dict(pp, target_soc=working.get("target_soc", strat.get("target_soc", 90)))
            projection = project_soc_path(slots, soc, cap_kwh, proj_pp)
            plan_slots = slots
            heur = "charge" if rec.get("force_charge") else ("sell" if rec.get("force_discharge") else "hold")
            pc = plan["action_now"] if plan["action_now"] in ("charge", "sell") else "hold"
            plan["heuristic"] = heur
            plan["diverges"] = pc != heur
            if plan["diverges"] and _PLAN["last_class"] != (pc, heur):
                log_event("planner", f"shadow plan wants {plan['action_now']} → {plan['target_now']}%, "
                                     f"heuristic says {heur} (rec {rec.get('action')})")
            _PLAN["last_class"] = (pc, heur)
    except Exception as e:
        print(f"shadow planner failed: {e}", file=sys.stderr)

    now_epoch = time.time()
    return {
        "demand_window": demand_window,
        "weather": weather,
        "solar_forecast": {"today_total": solar_today_total, "remaining_today": solar_remaining,
                           "tomorrow": solar_tomorrow,
                           "remaining_today_raw": solar_remaining_raw, "tomorrow_raw": solar_tomorrow_raw},
        "solar_cal": solar_cal,
        "solar_bells": solar_bells,
        "llm": plan,
        "dynamic": {"source": dyn_src, "mode": ("zerohero" if zerohero else "amber"),
                    "charge_start_price": (None if zerohero else working.get("charge_start_price")),
                    "target_soc": working.get("target_soc"),
                    "price_ceiling": foundation["price_ceiling"], "max_soc": foundation["max_soc"],
                    "charge_start_floor": (None if zerohero else foundation["charge_start_floor"]),
                    "sell_price": (None if zerohero else sell_eff), "survival_soc": survival_soc,
                    "spike_sell_buffer_kwh": working.get("spike_sell_buffer_kwh", 0.0),
                    "buy_bar_cap": working.get("buy_bar_cap"),
                    "topup": bool(strat.get("topup_to_target", False)),
                    "sell_enabled": (True if zerohero else bool(strat.get("sell_enabled", True)))},
        "battery": {"capacity_kwh": cap_kwh, "stored_kwh": stored_kwh},
        "consumption": consumption,
        "forecast_profiles": fcast,   # FoxESS-history hour-of-day load + solar (Phase 2)
        "energy_shortfall_kwh": working.get("energy_shortfall_kwh"),
        "feedin": prices.get("feedin"),
        "grid_power": round(grid_power, 2),
        "feedin_power": round(feedin_power, 2),
        "battery_power": round(battery_power, 2),
        "bat_charge_power": bat_charge_power,
        "bat_discharge_power": bat_discharge_power,
        "pv_strings": pv_strings,
        "energy_totals": energy,
        "sched_active": sched_active,
        "note": note,
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
        "price": prices.get("price"),
        "descriptor": prices.get("descriptor"),
        "aemo_price": prices.get("aemo_price"),
        "feedin": prices.get("feedin"),
        "forecast_next": prices.get("forecast", [])[:6],
        "aemo_forecast_next": prices.get("aemo_forecast", [])[:6],
        "forecast_h": prices.get("forecast", []),
        "aemo_forecast_h": prices.get("aemo_forecast", []),
        "feedin_forecast_h": prices.get("feedin_forecast", []),
        "soc": soc,
        "pv_kw": round(pv, 2),
        "real": real,
        "work_mode": wm.get("value"),
        "work_mode_age_s": int(time.time() - _WM["ts"]) if _WM.get("ts") else None,
        "inverter_min_soc_read": _WM.get("min_soc"),
        "inverter_min_soc_floor": int(strat.get("inverter_min_soc", 10)),
        "work_mode_options": wm.get("enumList"),
        "recommendation": rec,
        "plan": plan,
        "projection": projection,
        "plan_slots": plan_slots,
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
            inv_floor = int(strat.get("inverter_min_soc", 10))   # constant device floor — never the survival number
            fox.enable_force_discharge((now.hour, now.minute), (eh, em),
                                       inv_floor, strat["force_charge_power_kw"])
            m = (f"AUTO-SELL START until ~{eh:02d}:{em:02d} (sells toward {rec.get('sell_floor')}% survival "
                 f"[software-stopped]; inverter hard floor {inv_floor}% @ {strat['force_charge_power_kw']}kW)")
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
                            int(strat.get("inverter_min_soc", 10)), strat["target_soc"], strat["force_charge_power_kw"])
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
    if do_apply:
        snap["ev_divert"] = ev_divert_tick(cfg, snap)   # solar diversion to the car charger (auto only)
    mqtt_publish(cfg, snap)
    run_band_actions(cfg, snap)
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
async function sendChat(){const i=document.getElementById('chatmsg');const t=(i.value||'').trim();if(!t)return;
 i.value='';document.getElementById('msg').textContent='asking strategist…';
 const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})});
 const j=await r.json();document.getElementById('msg').textContent=j.reply?('🤖 '+j.reply):JSON.stringify(j);
 setTimeout(()=>location.reload(),1200);}
async function clearChat(){document.getElementById('msg').textContent='clearing…';
 const r=await fetch('/api/chat_clear',{method:'POST'});const j=await r.json();
 document.getElementById('msg').textContent=JSON.stringify(j);setTimeout(()=>location.reload(),800);}
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
// Resize all charts together (works inside the HA iframe — buttons, no drag/localStorage needed).
function chApply(h){document.querySelectorAll('.chartwrap').forEach(function(c){c.style.height=h+'px';});}
function chSize(d){const c=document.querySelector('.chartwrap');if(!c)return;
 const h=Math.max(160,(parseInt(c.style.height)||320)+d);chApply(h);
 try{localStorage.setItem('foxctl_ch_h',h);}catch(e){}}
(function(){try{const h=parseInt(localStorage.getItem('foxctl_ch_h'));if(h)chApply(h);}catch(e){}})();
// SOFT refresh: swap only the live regions' innerHTML from a fresh render — charts keep their size,
// inputs (chat/note) and the scroll position are untouched (replaces the old full-page meta-refresh
// that wiped resizes in the HA iframe). manual=1 shows a flash in the header.
async function softRefresh(manual){
 try{
  const r=await fetch(location.pathname,{cache:'no-store'});const t=await r.text();
  const doc=new DOMParser().parseFromString(t,'text/html');
  ['cw6','cw18','cwmax','socwrap'].forEach(function(id){const n=document.getElementById(id),m=doc.getElementById(id);if(n&&m)n.innerHTML=m.innerHTML;});
  ['reccard','spikecard','cardsrow','dyncard','ts'].forEach(function(id){const n=document.getElementById(id),m=doc.getElementById(id);if(n&&m)n.innerHTML=m.innerHTML;});
  const rf=document.getElementById('refr');if(rf)rf.textContent='updated '+new Date().toLocaleTimeString();
  refreshLog();refreshEvents();
 }catch(e){const rf=document.getElementById('refr');if(rf)rf.textContent='refresh failed';}
}
setInterval(tick,1000);tick();
refreshLog();refreshEvents();
setInterval(softRefresh,60000);"""


def render_forecast_svg(snap: dict, cfg: dict | None = None, hours: float = 18, cid: str = "chartwrap") -> str:
    """SVG of the price horizon the controller reasons over (out to `hours`): Amber + AEMO curves, the
    LLM-set charge-start price, the foundation ceiling, shaded 'would-charge'/'would-sell' windows, the
    projected + planned SoC, and usage/solar overlays. `cid` is the wrapping element id so multiple
    charts on one page don't collide in the hover JS."""
    dyn = snap.get("dynamic") or {}
    now = datetime.now(timezone.utc)
    amber, aemo = [], []
    for p in snap.get("forecast_h") or []:
        t = _parse_t(p.get("t") or "")
        if t and p.get("price") is not None:
            h = (t - now).total_seconds() / 3600.0
            if -0.3 <= h <= hours:
                amber.append((h, p["price"], t))
    for p in snap.get("aemo_forecast_h") or []:
        t = _parse_t(p.get("t") or "")
        if t and p.get("price") is not None:
            h = (t - now).total_seconds() / 3600.0
            if -0.3 <= h <= hours:
                aemo.append((h, p["price"]))
    fin = []                                          # Amber feed-in (export) price forecast
    for p in snap.get("feedin_forecast_h") or []:
        t = _parse_t(p.get("t") or "")
        if t and p.get("price") is not None:
            h = (t - now).total_seconds() / 3600.0
            if -0.3 <= h <= hours:
                fin.append((h, p["price"]))
    fin_map = {round(h, 2): pr for h, pr in fin}      # per-slot feed-in lookup for the sell logic
    if not amber:
        return "<small>no forecast to chart yet</small>"
    tz = amber[0][2].tzinfo   # label the x-axis in the forecast's own (Sydney) time
    W, H, padL, padR, padT, padB = 1180, 310, 50, 50, 16, 34
    iw, ih = W - padL - padR, H - padT - padB
    # Phase 2: the green line/shading is the RELATIVE buy bar (cheapest slots covering the deficit),
    # not the vestigial charge_start_price — so the chart shows the slots actually chosen to buy.
    _rec = snap.get("recommendation") or {}
    csp = _rec.get("buy_bar")
    if not isinstance(csp, (int, float)):
        csp = dyn.get("charge_start_price") or 0.0
    ceil = dyn.get("price_ceiling") or 0.20
    allp = [pr for _, pr, _ in amber] + [pr for _, pr in aemo] + [ceil, csp]
    ymax = max(allp) * 1.12 or 0.3
    ymin = min(min(allp), 0.0)
    xmin = min(h for h, _, _ in amber)
    xmax = max(h for h, _, _ in amber) or 1
    bells = snap.get("solar_bells") or []
    # Rolling hour-of-day usage profile (avg kWh in each wall-clock hour ≈ avg kW) + min/max range band.
    _cons = snap.get("consumption") or {}
    hour_profile = _cons.get("hour_profile") or {}
    def _prof_at(prof, t):
        lh = t.astimezone(tz).hour
        v = prof.get(lh, prof.get(str(lh)))
        return float(v) if isinstance(v, (int, float)) else None
    def _usage_at(t):
        return _prof_at(hour_profile, t)
    usage = [(h, _usage_at(t)) for h, _, t in amber if _usage_at(t) is not None]
    hmin, hmax = _cons.get("hour_min") or {}, _cons.get("hour_max") or {}
    usage_band = [(h, _prof_at(hmin, t), _prof_at(hmax, t)) for h, _, t in amber]
    usage_band = [(h, lo, hi) for h, lo, hi in usage_band if lo is not None and hi is not None]
    # Historical solar from ACTUALS: hour-of-day avg + min/max range (real data, no forecast/calibration).
    _fp = snap.get("forecast_profiles") or {}
    s_avg, s_min, s_max = _fp.get("solar_profile") or {}, _fp.get("solar_min") or {}, _fp.get("solar_max") or {}
    solar_hist = [(h, _prof_at(s_avg, t), _prof_at(s_min, t), _prof_at(s_max, t)) for h, _, t in amber]
    solar_hist = [(h, a, lo, hi) for h, a, lo, hi in solar_hist if a is not None and lo is not None and hi is not None]
    peak_usage = max([v for _, v in usage] + [hi for _, _, hi in usage_band] + [0.0])
    peak_solar = max([b["pmax"] for b in bells] + [hi for _, _, _, hi in solar_hist] + [0.0])
    skw = max([peak_solar, peak_usage, 1.0]) * 1.15   # right-axis kW scale (solar + usage)
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
            export_price = fin_map.get(round(h, 2), buy)   # real feed-in forecast if we have it, else buy proxy
            sell = sell_on and export_price >= sell_thr and soc_pct > survival
            if buy <= csp and soc_kwh < tgt_kwh:                       # grid force-charge window
                soc_kwh = min(tgt_kwh, soc_kwh + charge_kw * dt) + max(0.0, solar_kwh - load_kwh)
            elif sell:                                                  # export to grid down to floor
                soc_kwh -= min(charge_kw * dt, soc_kwh - cap * survival / 100.0)
            else:
                soc_kwh += solar_kwh - load_kwh
            soc_kwh = max(floor_kwh, min(max_kwh, soc_kwh))
            proj.append({"h": h, "soc": round(soc_kwh / cap * 100.0, 1), "sell": sell})
    out = [f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMid meet" style="font:14px system-ui">']
    # SOLAR forecast bell (Solcast, weather-aware for today) — filled gold, on the right kW axis.
    for b in bells:
        s, e = max(b["s"], xmin), min(b["e"], xmax)
        if e <= s or b["pmax"] <= 0:
            continue
        steps = 40
        pts = [f"{X(s):.1f},{SY(0):.1f}"]
        for k in range(steps + 1):
            h = s + (e - s) * k / steps
            frac = (h - b["s"]) / ((b["e"] - b["s"]) or 1)
            pts.append(f"{X(h):.1f},{SY(b['pmax'] * math.sin(math.pi * min(max(frac, 0), 1))):.1f}")
        pts.append(f"{X(e):.1f},{SY(0):.1f}")
        out.append(f'<polygon points="{" ".join(pts)}" fill="#f5c518" opacity="0.18"/>')
        hpk = b["s"] + (b["e"] - b["s"]) / 2
        if xmin <= hpk <= xmax:
            out.append(f'<text x="{X(hpk):.1f}" y="{SY(b["pmax"])-4:.1f}" text-anchor="middle" '
                       f'fill="#b8860b">☀ {b["kwh"]}kWh (~{b["pmax"]}kW)</text>')
    # TYPICAL solar from your ACTUAL history: hour-of-day avg (solid) + min/max range (dashed edges).
    # Real data, shown as soon as there's ≥1 day of generation — no forecast or calibration needed.
    if solar_hist:
        top = " ".join(f"{X(h):.1f},{SY(hi):.1f}" for h, _, _, hi in solar_hist)
        bot = " ".join(f"{X(h):.1f},{SY(lo):.1f}" for h, _, lo, _ in reversed(solar_hist))
        out.append(f'<polygon points="{top} {bot}" fill="#e67e22" opacity="0.10"/>')
        out.append('<polyline points="' + " ".join(f"{X(h):.1f},{SY(hi):.1f}" for h, _, _, hi in solar_hist) +
                   '" fill="none" stroke="#cf6a12" stroke-width="1" stroke-dasharray="5 3" opacity="0.75"/>')
        out.append('<polyline points="' + " ".join(f"{X(h):.1f},{SY(lo):.1f}" for h, _, lo, _ in solar_hist) +
                   '" fill="none" stroke="#cf6a12" stroke-width="1" stroke-dasharray="5 3" opacity="0.75"/>')
        out.append('<polyline points="' + " ".join(f"{X(h):.1f},{SY(a):.1f}" for h, a, _, _ in solar_hist) +
                   '" fill="none" stroke="#cf6a12" stroke-width="1.6" opacity="0.9"/>')
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
               f'fill="#1a9e4b">buy ≤ ${csp:.2f} (relative)</text>')
    # left y axis ($) + right y axis (kW solar)
    for k in range(5):
        yv = ymin + (ymax - ymin) * k / 4
        out.append(f'<text x="{padL-6}" y="{Y(yv)+4:.1f}" text-anchor="end" fill="#999">${yv:.2f}</text>'
                   f'<line x1="{padL}" y1="{Y(yv):.1f}" x2="{W-padR}" y2="{Y(yv):.1f}" stroke="#8884" stroke-width="0.5"/>')
        kv = skw * k / 4
        out.append(f'<text x="{W-padR+6}" y="{SY(kv)+4:.1f}" text-anchor="start" fill="#b8860b">{kv:.1f}kW</text>')
    # x axis ticks — REAL clock times, spacing scaled to the horizon
    tick = max(1, round((xmax - xmin) / 6))
    for hh in range(0, int(xmax) + 1, tick):
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
    if fin:
        pl = " ".join(f"{X(h):.1f},{Y(pr):.1f}" for h, pr in fin)
        out.append(f'<polyline points="{pl}" fill="none" stroke="#27ae60" stroke-width="1.8" '
                   f'stroke-dasharray="6 2" opacity="0.95"/>')
    pl = " ".join(f"{X(h):.1f},{Y(pr):.1f}" for h, pr, _ in amber)
    out.append(f'<polyline points="{pl}" fill="none" stroke="#2980d9" stroke-width="2.2"/>')
    # rolling usage min–max range (shaded band) + avg overlay (right kW axis)
    if usage_band:
        top = " ".join(f"{X(h):.1f},{SY(hi):.1f}" for h, lo, hi in usage_band)
        bot = " ".join(f"{X(h):.1f},{SY(lo):.1f}" for h, lo, hi in reversed(usage_band))
        out.append(f'<polygon points="{top} {bot}" fill="#8e44ad" opacity="0.12"/>')
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
    # SHADOW PLANNER: ideal SoC line + min-SoC requirement envelope (does not drive control)
    plan = snap.get("plan") or {}
    pline = [(h, s) for h, s in (plan.get("soc_line") or []) if xmin <= h <= xmax]
    if pline:
        out.append('<polyline points="' + " ".join(f"{X(h):.1f},{PY(s):.1f}" for h, s in pline) +
                   '" fill="none" stroke="#d35400" stroke-width="1.6" stroke-dasharray="7 3" opacity="0.85"/>')
        fl = [(h, s) for h, s in (plan.get("floor_line") or []) if xmin <= h <= xmax]
        if fl:
            out.append('<polyline points="' + " ".join(f"{X(h):.1f},{PY(s):.1f}" for h, s in fl) +
                       '" fill="none" stroke="#d35400" stroke-width="0.8" stroke-dasharray="1 4" opacity="0.5"/>')
        out.append(f'<text x="{X(pline[0][0])+3:.1f}" y="{PY(pline[0][1])+13:.1f}" fill="#d35400">plan</text>')
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
                    "use": _usage_at(t), "umin": _prof_at(hmin, t), "umax": _prof_at(hmax, t),
                    "soc": (d["soc"] if d else None), "sell": (bool(d["sell"]) if d else False)})
    hover = {"pts": pts, "W": W, "padR": padR}
    script = ("<script>(function(){var w=document.getElementById('" + cid + "');if(!w)return;"
              "var s=w.querySelector('svg');if(!s)return;var D=" + json.dumps(hover) + ";"
              "var ln=s.getElementById('hovline'),tp=s.getElementById('hovtip'),bg=s.getElementById('hovtipbg');"
              "function hide(){ln.style.display='none';tp.style.display='none';bg.style.display='none';}"
              "s.addEventListener('mousemove',function(e){var m=s.getScreenCTM();if(!m)return;"
              "var p=s.createSVGPoint();p.x=e.clientX;p.y=e.clientY;var l=p.matrixTransform(m.inverse());"
              "var best=null,bd=1e9;for(var i=0;i<D.pts.length;i++){var d=Math.abs(D.pts[i].x-l.x);"
              "if(d<bd){bd=d;best=D.pts[i];}}if(!best){hide();return;}"
              "ln.setAttribute('x1',best.x);ln.setAttribute('x2',best.x);ln.style.display='';"
              "tp.textContent=best.t+'   $'+best.price.toFixed(2)+(best.use!=null?'   ~'+best.use.toFixed(1)+'kW use'+(best.umin!=null&&best.umax!=null?' ('+best.umin.toFixed(1)+'-'+best.umax.toFixed(1)+')':''):'')"
              "+(best.soc!=null?'   SoC '+Math.round(best.soc)+'%':'')+(best.sell?'   ⟶ SELL':'');"
              "tp.style.display='';var tx=best.x+9;tp.setAttribute('y',18);tp.setAttribute('x',tx);"
              "var b=tp.getBBox();if(b.x+b.width>D.W-D.padR){tx=best.x-9-b.width;tp.setAttribute('x',tx);b=tp.getBBox();}"
              "bg.setAttribute('x',b.x-5);bg.setAttribute('y',b.y-3);bg.setAttribute('width',b.width+10);"
              "bg.setAttribute('height',b.height+6);bg.style.display='';});"
              "s.addEventListener('mouseleave',hide);})();</script>")
    legend = ('<small><b style="color:#2980d9">— Amber retail</b> (charges on this) · '
              '<span style="color:#e67e22">- - AEMO wholesale</span> · '
              '<span style="color:#27ae60">- - feed-in (export) forecast</span> · '
              '<span style="color:#b8860b">▮ solar forecast</span> <span style="color:#cf6a12">— typical solar from history (avg + min/max)</span> · '
              '<span style="color:#8e44ad">- - your usage avg + range band (right kW axis)</span> · '
              '<span style="color:#16a085">— projected SoC %</span> · '
              '<span style="color:#d35400">- - shadow plan (ideal SoC + floor)</span> · '
              '<span style="color:#2ecc71">▮ would grid-charge</span> · '
              '<span style="color:#e84393">▮ would sell</span> · hover for time<br>'
              '<i>SoC projection &amp; sell windows use the feed-in forecast where available '
              '(else buy-price proxy) + forecast solar/load — directional, not exact.</i></small>')
    return "".join(out) + script + legend


def render_soc_svg(snap: dict, hours: float = 24) -> str:
    """Dedicated single-axis (%) SoC chart: the rules-model projection vs the shadow plan + its floor
    envelope, with the survival line and the current SoC. Keeps SoC off the crowded price chart."""
    proj = snap.get("projection") or []
    plan = snap.get("plan") or {}
    pl, fl = plan.get("soc_line") or [], plan.get("floor_line") or []
    hs = [d["h"] for d in proj] + [h for h, _ in pl]
    if not hs:
        return "<small>no SoC projection yet</small>"
    now = datetime.now(timezone.utc)
    tz = datetime.now().astimezone().tzinfo
    xmin, xmax = min(0.0, min(hs)), (min(hours, max(hs)) or 1)
    W, H, padL, padR, padT, padB = 1180, 230, 42, 60, 14, 28
    iw, ih = W - padL - padR, H - padT - padB
    X = lambda h: padL + iw * (h - xmin) / ((xmax - xmin) or 1)
    Y = lambda pct: padT + ih * (1 - max(0.0, min(100.0, pct)) / 100.0)
    # right $ axis: forecast buy price + charge-start / sell thresholds, so you can see if the plan
    # tracks price (charges below charge-start, sells above the sell threshold).
    dyn = snap.get("dynamic") or {}
    buy = []
    for p in snap.get("forecast_h") or []:
        t = _parse_t(p.get("t") or "")
        if t and p.get("price") is not None:
            hh = (t - now).total_seconds() / 3600.0
            if xmin <= hh <= xmax:
                buy.append((hh, float(p["price"])))
    cstart = (snap.get("recommendation") or {}).get("buy_bar")
    if not isinstance(cstart, (int, float)):
        cstart = dyn.get("charge_start_price") or 0.0
    sthr = dyn.get("sell_price") or 0.0
    dmax = (max([pr for _, pr in buy] + [cstart, sthr, 0.1])) * 1.1
    RY = lambda d: padT + ih * (1 - min(max(d, 0.0), dmax) / (dmax or 1))
    out = [f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMid meet" style="font:13px system-ui">']
    for pct in (0, 20, 40, 60, 80, 100):
        out.append(f'<line x1="{padL}" y1="{Y(pct):.1f}" x2="{W-padR}" y2="{Y(pct):.1f}" stroke="#8884" '
                   f'stroke-width="0.5"/><text x="{padL-4}" y="{Y(pct)+4:.1f}" text-anchor="end" fill="#999">{pct}%</text>')
    for k in range(3):                                    # right-axis $ labels
        dv = dmax * k / 2
        out.append(f'<text x="{W-padR+4}" y="{RY(dv)+4:.1f}" fill="#2980d9">${dv:.2f}</text>')
    out.append(f'<line x1="{padL}" y1="{RY(cstart):.1f}" x2="{W-padR}" y2="{RY(cstart):.1f}" stroke="#1a9e4b" '
               f'stroke-dasharray="5 4" stroke-width="0.8" opacity="0.7"/>')
    if sthr:
        out.append(f'<line x1="{padL}" y1="{RY(sthr):.1f}" x2="{W-padR}" y2="{RY(sthr):.1f}" stroke="#e84393" '
                   f'stroke-dasharray="5 4" stroke-width="0.8" opacity="0.7"/>')
    if buy:
        out.append('<polyline points="' + " ".join(f"{X(h):.1f},{RY(pr):.1f}" for h, pr in buy) +
                   '" fill="none" stroke="#2980d9" stroke-width="1.6" opacity="0.85"/>')
    tick = max(1, round((xmax - xmin) / 6))
    for hh in range(0, int(xmax) + 1, tick):
        if xmin <= hh <= xmax:
            lab = (now + timedelta(hours=hh)).astimezone(tz).strftime("%H:%M")
            out.append(f'<line x1="{X(hh):.1f}" y1="{padT}" x2="{X(hh):.1f}" y2="{H-padB}" stroke="#8883" '
                       f'stroke-width="0.5"/><text x="{X(hh):.1f}" y="{H-padB+14}" text-anchor="middle" fill="#999">{lab}</text>')
    out.append(f'<line x1="{X(0):.1f}" y1="{padT}" x2="{X(0):.1f}" y2="{H-padB}" stroke="#3498db" stroke-width="1"/>')
    surv = (snap.get("dynamic") or {}).get("survival_soc")
    if isinstance(surv, (int, float)):
        out.append(f'<line x1="{padL}" y1="{Y(surv):.1f}" x2="{W-padR}" y2="{Y(surv):.1f}" stroke="#c0392b" '
                   f'stroke-dasharray="4 4" stroke-width="0.8" opacity="0.6"/>'
                   f'<text x="{W-padR+3}" y="{Y(surv)+4:.1f}" fill="#c0392b">surv {int(surv)}%</text>')
    fl2 = [(h, s) for h, s in fl if xmin <= h <= xmax]
    if fl2:
        out.append('<polyline points="' + " ".join(f"{X(h):.1f},{Y(s):.1f}" for h, s in fl2) +
                   '" fill="none" stroke="#d35400" stroke-width="0.8" stroke-dasharray="1 4" opacity="0.6"/>')
    pj = [(d["h"], d["soc"]) for d in proj if xmin <= d["h"] <= xmax]
    if pj:
        out.append('<polyline points="' + " ".join(f"{X(h):.1f},{Y(s):.1f}" for h, s in pj) +
                   '" fill="none" stroke="#16a085" stroke-width="2" opacity="0.9"/>')
    pl2 = [(h, s) for h, s in pl if xmin <= h <= xmax]
    if pl2:
        out.append('<polyline points="' + " ".join(f"{X(h):.1f},{Y(s):.1f}" for h, s in pl2) +
                   '" fill="none" stroke="#d35400" stroke-width="2" stroke-dasharray="7 3" opacity="0.9"/>')
    cs = snap.get("soc")
    if isinstance(cs, (int, float)):
        out.append(f'<circle cx="{X(0):.1f}" cy="{Y(cs):.1f}" r="3.5" fill="#3498db"/>'
                   f'<text x="{X(0)+5:.1f}" y="{Y(cs)-5:.1f}" fill="#3498db">now {cs:.0f}%</text>')
    out.append("</svg>")
    legend = ('<small><span style="color:#16a085">— rules-model SoC</span> · '
              '<span style="color:#d35400">- - shadow plan + floor</span> · '
              '<span style="color:#c0392b">- - survival</span> · '
              '<span style="color:#2980d9">— buy $ (right axis)</span> '
              '<span style="color:#1a9e4b">·charge≤</span> <span style="color:#e84393">·sell≥</span> · '
              '<a href="/api/export.csv">⤓ export CSV (actuals + forecast/plan)</a></small>')
    return "".join(out) + legend


def chat_panel_html(cfg: dict, snap: dict | None = None) -> str:
    """The ONE strategist surface: its latest verdict + current relative levers, the persistent
    conversation, and an input to talk to it. (Phase 3 merged the old separate 'Dynamic LLM' verdict
    box into here.) Reads the rolling history; routine 'POLICY CONTEXT' uploads are collapsed."""
    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    snap = snap or {}
    # Verdict header — the strategist's latest rating/reason, its live levers, and anything needing a human.
    llm = snap.get("llm") or {}
    dyn = snap.get("dynamic") or {}
    if llm.get("rating"):
        verdict = {"AGREE": "✅ AGREE", "REFINE": "🔧 REFINE", "DISAGREE": "🔍 DISAGREE"}.get(llm.get("rating"), "⚠️")
        levers = (f'target {dyn.get("target_soc","?")}% · spike-buffer '
                  f'{dyn.get("spike_sell_buffer_kwh", 0)}kWh · bar-cap '
                  f'{("$"+format(dyn.get("buy_bar_cap"),".3f")) if isinstance(dyn.get("buy_bar_cap"), (int,float)) else "—"}')
        act = (llm.get("operator_action") or "").strip()
        act_html = (f'<div style="margin-top:.4rem;padding:.5rem .7rem;background:#fff3cd;color:#5c4600;'
                    f'border-radius:8px"><b>📣 Needs you:</b> {esc(act)}</div>') if act else ""
        # Surface a silent fallback: the model that ANSWERED vs the configured primary.
        primary = (cfg.get("llm") or {}).get("model", "")
        used = llm.get("model", "")
        fb_html = (f'<div style="margin-top:.4rem;padding:.4rem .6rem;background:#fdecea;color:#7a271a;'
                   f'border-radius:8px"><small>⚠️ primary <b>{esc(primary)}</b> was unavailable (e.g. 529 '
                   f'overloaded) — this turn ran on the fallback <b>{esc(used)}</b>. Transient; it retries '
                   f'the primary first each cycle.</small></div>') if (used and primary and used != primary) else ""
        head = (f'<div style="padding:.4rem .6rem;border-bottom:1px solid #9c6ade44;margin-bottom:.4rem">'
                f'<b>{verdict}</b> <small>{esc(used)} · {esc(llm.get("ts",""))}</small>'
                f'<div>{esc(llm.get("text",""))}</div><small>levers: {levers}</small>{fb_html}{act_html}</div>')
    else:
        head = ('<div style="padding:.4rem .6rem;margin-bottom:.4rem"><small>advisory off — enable '
                'llm_review + set the API key, or just chat below (it can\'t control until enabled)</small></div>')
    try:
        msgs = chat_view(cfg, 24)
    except Exception:
        msgs = []
    if not msgs:
        rows = ('<div><small>no conversation yet — turns appear once llm_review is enabled, '
                'or just say hi below.</small></div>')
    else:
        parts = []
        for m in msgs:
            who = "🧑 you" if m["role"] == "user" else "🤖 claude"
            content = m.get("content", "")
            if m.get("kind") == "policy" and m["role"] == "user":
                content = "POLICY CONTEXT — automated state + forecast update"
            tag = " · policy" if m.get("kind") == "policy" else ""
            parts.append(f'<div style="margin:.45rem 0;padding:.3rem .5rem;border-left:3px solid '
                         f'{"#9c6ade" if m["role"] == "assistant" else "#bbb"}">'
                         f'<small>{who} · {esc(m.get("ts", "")[5:16].replace("T", " "))}{tag}</small>'
                         f'<div style="white-space:pre-wrap">{esc(content)}</div></div>')
        rows = "".join(parts)
    llmc = cfg.get("llm", {})
    return (f'<h3>🤖 Strategist <small>(one surface — verdict + chat; nudges target SoC / spike-buffer / '
            f'bar-cap within hard guardrails, and explains)</small></h3>'
            f'<div class=card style="border-color:#9c6ade">'
            f'{head}'
            f'<div id=chatlog style="max-height:360px;overflow:auto">{rows}</div>'
            f'<div style="margin-top:.5rem;display:flex;gap:.4rem">'
            f'<input id=chatmsg style="flex:1;font:inherit;padding:.45rem;box-sizing:border-box" '
            f'placeholder="Ask the strategist about its plan, or give standing guidance…" '
            f'onkeydown="if(event.key===\'Enter\')sendChat()">'
            f'<button onclick="sendChat()">Send</button>'
            f'<button class=danger onclick="if(confirm(\'Clear the whole strategist conversation?\'))clearChat()">🗑 Clear</button></div>'
            f'<small>model {esc(llmc.get("model", "-"))} · fallback {esc(llmc.get("fallback_model", "-"))}'
            f'{"" if llmc.get("enabled") else " · ⚠️ llm_review off"}</small></div>')


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
    # Grid flow card: are we importing or exporting right now, how much, at what price.
    _exp = float(snap.get("feedin_power") or 0.0)
    _imp = float(snap.get("grid_power") or 0.0)
    _age = snap.get("data_age_s")
    _age_txt = f' · {_age}s ago' if isinstance(_age, (int, float)) else ''
    if _exp > 0.1:
        grid_html = (f'<div class=card style="background:#e8f5e9;border-color:#2ecc71"><small>Grid flow</small>'
                     f'<div class=big>⬆ {_exp:.2f} <small>kW</small></div>'
                     f'<small>EXPORTING @ ${snap.get("feedin")}/kWh{_age_txt}</small></div>')
    elif _imp > 0.1:
        grid_html = (f'<div class=card><small>Grid flow</small><div class=big>⬇ {_imp:.2f} <small>kW</small></div>'
                     f'<small>importing @ ${snap.get("price")}/kWh{_age_txt}</small></div>')
    else:
        grid_html = f'<div class=card><small>Grid flow</small><div class=big>– <small>kW</small></div><small>no grid flow{_age_txt}</small></div>'
    # Top-of-page FoxESS API banner (rate-limited / errors → telemetry & control reads failing).
    _fe = snap.get("fox_error")
    if _fe and _fe.get("rate_limited"):
        fox_banner = (f'<div style="background:#c0392b;color:#fff;padding:.6rem 1rem;border-radius:8px;'
                      f'margin:.6rem 0;font-weight:600">⛔ FoxESS API RATE-LIMITED — reads are being rejected '
                      f'(last error {_fe["age"]}s ago: {_fe["msg"]}). Telemetry/control may be stale; ease off '
                      f'Backfill &amp; rapid actions until it clears.</div>')
    elif _fe:
        fox_banner = (f'<div style="background:#e67e22;color:#fff;padding:.6rem 1rem;border-radius:8px;'
                      f'margin:.6rem 0;font-weight:600">⚠️ FoxESS API errors — telemetry may be stale '
                      f'(last error {_fe["age"]}s ago: {_fe["msg"]}).</div>')
    else:
        fox_banner = ''
    # Stranded-floor banner: the inverter's own min-SoC sits above our safety floor (legacy 66% bug).
    _ims, _imf = snap.get("inverter_min_soc_read"), snap.get("inverter_min_soc_floor", 10)
    if isinstance(_ims, (int, float)) and _ims > _imf + 1:
        fox_banner += (f'<div style="background:#c0392b;color:#fff;padding:.6rem 1rem;border-radius:8px;'
                       f'margin:.6rem 0;font-weight:600">⛔ Inverter min-SoC is {int(_ims)}% (should be ≤{_imf}%). '
                       f'While stranded high, SelfUse imports grid power to hold it. foxctl will reset it to '
                       f'{_imf}% on the next force-charge/sell window — or clear it now in the FoxESS app '
                       f'(Min SoC On Grid).</div>')
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
    # Spike readiness: at a glance, is the controller set up to dump into a price spike, and how much
    # headroom sits above the survival buffer (the buffer that rides out *extended* high-price runs).
    cap_kwh2 = bat.get("capacity_kwh") or cfg["strategy"].get("battery_capacity_kwh", 30)
    soc_now = snap.get("soc"); feed_now = snap.get("feedin")
    sell_on = bool(dyn2.get("sell_enabled")) and isinstance(sell_p, (int, float))
    selling_now = bool(rec.get("force_discharge"))
    charging_now = bool(rec.get("force_charge")) or bool(snap.get("currently_charging"))
    head_pct = max(0, round(soc_now - surv)) if isinstance(soc_now, (int, float)) and isinstance(surv, (int, float)) else None
    head_kwh = round(cap_kwh2 * head_pct / 100.0, 1) if head_pct is not None else None
    buf_kwh = round(cap_kwh2 * surv / 100.0, 1) if isinstance(surv, (int, float)) else None
    if dyn2.get("mode") == "zerohero":
        sp_col, sp_icon, sp_txt = "#2ecc71", "⏰", f"ZeroHero exports in the evening window; keeps ≥{surv}% overnight buffer"
        sp_head = f'export window 18:00–21:00'
    elif not sell_on:
        sp_col, sp_icon, sp_txt = "#e67e22", "⚠️", "auto-sell is OFF — a spike will NOT be captured (enable auto_sell / set a sell threshold)"
        sp_head = "export disabled"
    elif selling_now:
        sp_col, sp_icon, sp_txt = "#2ecc71", "💰", f"SELLING NOW into ${feed_now}/kWh — discharging to grid down to the {surv}% buffer"
        sp_head = f"export ≥ ${sell_p}"
    elif head_pct is not None and head_pct <= 1:
        sp_col, sp_icon, sp_txt = "#e67e22", "⚠️", f"at the {surv}% survival buffer — nothing sellable until it refills"
        sp_head = f"export ≥ ${sell_p}"
    elif charging_now:
        sp_col, sp_icon, sp_txt = "#3498db", "🔌", f"charging now; {head_kwh}kWh above the buffer is ready to sell once feed-in ≥ ${sell_p}"
        sp_head = f"export ≥ ${sell_p}"
    else:
        sp_col, sp_icon, sp_txt = "#2ecc71", "✅", f"ready — {head_kwh}kWh ({head_pct}%) sellable above the {surv}% buffer when feed-in ≥ ${sell_p}"
        sp_head = f"export ≥ ${sell_p}"
    spike_html = (f'<div class="card" style="border-color:{sp_col}"><small>⚡ SPIKE READINESS · auto-sell '
                  f'{"ON" if sell_on or dyn2.get("mode") == "zerohero" else "OFF"}</small>'
                  f'<div class=big>{sp_icon} {sp_head}</div><div>{sp_txt}</div>'
                  f'<small>buffer: keeps {surv}% (~{buf_kwh}kWh) to ride out <b>extended</b> high prices without '
                  f'importing · SoC {round(soc_now) if isinstance(soc_now, (int, float)) else "?"}% · battery '
                  f'{bat.get("stored_kwh", "?")}/{cap_kwh2}kWh · feed-in now ${feed_now}</small></div>')
    evbtns = "".join(f'<button onclick="ov(\'/api/ev_charge?h={h}\')">{h}h</button>' for h in (1, 2, 3, 4, 6))
    ev_row = (f'<div style="margin-top:.4rem">🔌 <b>Force car charge:</b> {evbtns} '
              f'<button onclick="ov(\'/api/ev_off\')">✖ stop → auto</button></div>'
              if (cfg.get("ev_divert") or {}).get("switch") else "")
    controls_html = (f'<h3>Quick controls <small>(manual overrides — auto-revert when the timer ends)</small></h3>'
                     f'<div class=card><div><b>Status:</b> {ov_status} · buy ≤ ${round(eff_floor,3)}'
                     f'{" (override)" if ov.get("floor") is not None else ""} · {auto_sell_txt}</div>'
                     f'<div style="margin-top:.5rem">⚡ <b>Force-charge:</b> {fcbtns} '
                     f'<button class=danger onclick="ov(\'/api/cancel_override\')">⏹ stop</button></div>'
                     f'<div style="margin-top:.4rem">💰 <b>SELL (discharge to grid):</b> {sellbtns} '
                     f'<button class=danger onclick="ov(\'/api/cancel_override\')">⏹ stop</button></div>'
                     f'{ev_row}'
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
                    f'<div>FREE-charge 11:00–14:00 → <b>{dyn.get("max_soc","?")}%</b> by 2pm · export 18:00–21:00 · '
                    f'no import 16:00–23:00 peak</div>'
                    f'<small>before 11:00 &amp; 16:00–23:00 peak: run off battery, zero grid import · '
                    f'export down to ≥{dyn.get("survival_soc","?")}% (keeps enough to coast to the 11:00 free window) · '
                    f'battery {bat.get("stored_kwh","?")}/{bat.get("capacity_kwh","?")}kWh · feed-in ${snap.get("feedin","?")}</small></div>')
    else:
        _bb = rec.get("buy_bar"); _df = rec.get("import_deficit_kwh"); _ns = rec.get("buy_slots_needed")
        _bar_txt = (f'buy in cheapest slots ≤ <b>${_bb:.3f}</b>' if isinstance(_bb, (int, float))
                    else 'no import needed')
        _mode = "TOP-UP (keep full, buy cheap)" if dyn.get("topup") else "NEED-BASED (minimal import)"
        dyn_html = (f'<div class="card"><small>⚙️ FOUNDATION — {_mode} (Amber)</small>'
                    f'<div>{_bar_txt} · target <b>{dyn.get("target_soc","?")}%</b></div>'
                    f'<small>{"fill toward target" if dyn.get("topup") else "need"} <b>{_df if _df is not None else "?"} kWh</b> via cheapest '
                    f'{_ns if _ns is not None else "?"} forward slot(s); relative bar rises/falls with the forecast '
                    f'(floor ${dyn.get("charge_start_floor","?")} always-OK ≤ charge ≤ ceiling ${dyn.get("price_ceiling","?")}) · '
                    f'max SoC {dyn.get("max_soc","?")}% · battery {bat.get("stored_kwh","?")}/{bat.get("capacity_kwh","?")}kWh · '
                    f'feed-in ${snap.get("feedin","?")}</small></div>')
    # The strategist's verdict is now shown INSIDE the single chat panel (one surface), not a 2nd box.
    llm_html = dyn_html
    return f"""<!doctype html><html><head><meta charset=utf-8>
<title>foxctl</title><style>{CSS}</style></head><body>
<h1>foxctl — <span id=ts>{snap.get('ts','-')}</span> <small id=refr style="color:#888"></small></h1>
{fox_banner}
<div id=reccard><div class="card rec"><small>RECOMMENDATION</small>
 <div class=big>{rec.get('action')} → {rec.get('target_mode')} {'⚡FORCE-CHARGE' if rec.get('force_charge') else ''}</div>
 <div>{rec.get('reason','')}</div>
 <div><small>applied: {snap.get('applied')} · control: allow={ctrl.get('allow_control')} auto_apply={ctrl.get('auto_apply')} force_charge={ctrl.get('set_force_charge')}</small></div>
 <div><small>🔭 shadow plan (not driving): {(snap.get('plan') or {}).get('action_now','–')} → {(snap.get('plan') or {}).get('target_now','–')}%{' · ⚠️ differs from heuristic' if (snap.get('plan') or {}).get('diverges') else ' · agrees with heuristic' if snap.get('plan') else ''}</small></div>
 <div style="margin-top:.4rem">
  <button onclick="post('/api/evaluate')">Evaluate now</button>
  <button onclick="post('/api/apply')">Apply recommendation</button>
  <button onclick="softRefresh(1)">↻ Refresh</button>
  <button class=danger onclick="post('/api/scheduler_off')">Stop / disable scheduler</button>
 </div></div></div>
<div id=spikecard>{spike_html}</div>
<div id=controlscard>{controls_html}</div>
<details style="margin:.3rem 0"><summary><small>More actions</small></summary>
 <button onclick="post('/api/review')">🤖 LLM review now</button>
 <button class=danger onclick="if(confirm('Grid-charge for 10 min to {cfg['strategy']['target_soc']}%?'))post('/api/force_charge_test')">⚡ Test force-charge (10 min)</button>
 <button onclick="if(confirm('Backfill 7 days of hourly load+solar into HA statistics?'))post('/api/backfill_ha?days=7')">⤓ Backfill 7d → HA stats</button>
</details>
<div id=msg></div>
<h3>Charts <small>(auto-refresh keeps your size · use −/+ or drag corner ↘ to resize)</small>
 <button onclick="chSize(-80)" title="shorter">−</button><button onclick="chSize(80)" title="taller">+</button></h3>
<h4 style="margin:.3rem 0">Next 6 hours</h4><div class=card style="padding:.5rem"><div id=cw6 class=chartwrap style="height:300px">{render_forecast_svg(snap, cfg, 6, "cw6")}</div></div>
<h4 style="margin:.3rem 0">Next 18 hours</h4><div class=card style="padding:.5rem"><div id=cw18 class=chartwrap style="height:440px">{render_forecast_svg(snap, cfg, 18, "cw18")}</div></div>
<h4 style="margin:.3rem 0">Full forecast (all available)</h4><div class=card style="padding:.5rem"><div id=cwmax class=chartwrap style="height:320px">{render_forecast_svg(snap, cfg, 72, "cwmax")}</div></div>
<h4 style="margin:.3rem 0">Battery SoC % — rules model vs shadow plan</h4><div class=card style="padding:.5rem"><div id=socwrap class=chartwrap style="height:240px">{render_soc_svg(snap)}</div></div>
<div class=row id=cardsrow>
 <div class=card><small>Amber price</small><div class=big>${snap.get('price')}</div>
   <span class=pill style="background:{color}">{band}</span></div>
 <div class=card><small>AEMO (wholesale)</small><div class=big>${snap.get('aemo_price')}</div></div>
 <div class=card><small>Feed-in (export)</small><div class=big>{('$'+str(snap.get('feedin'))) if snap.get('feedin') is not None else 'n/a'}</div><small>{('forecast: '+str(len(snap.get('feedin_forecast_h') or []))+'pt') if (snap.get('feedin_forecast_h')) else 'no forecast (check entity)'}</small></div>
 {grid_html}
 <div class=card><small>Battery SoC</small><div class=big>{round(snap.get('soc',0))}%</div></div>
 <div class=card><small>Solar (PV)</small><div class=big>{snap.get('pv_kw')} kW</div></div>
 <div class=card><small>Solar forecast</small><div class=big>{(snap.get('solar_forecast') or {}).get('today_total','?')} <small>kWh today</small></div><small>{(snap.get('solar_forecast') or {}).get('remaining_today','?')} left · tomorrow {(snap.get('solar_forecast') or {}).get('tomorrow','?')}<br>cal ×{(snap.get('solar_cal') or {}).get('bias','?')} {'(applied)' if (snap.get('solar_cal') or {}).get('applied') else f"({(snap.get('solar_cal') or {}).get('samples',0)}/{SOLAR_CAL_MIN}d learning)"}</small></div>
 <div class=card><small>Usage (rolling avg)</small><div class=big>{(snap.get('consumption') or {}).get('avg_daily_total_kwh') if (snap.get('consumption') or {}).get('days_sampled') else '–'} <small>kWh/day</small></div><small>range {(snap.get('consumption') or {}).get('min_daily_total_kwh','–')}–{(snap.get('consumption') or {}).get('max_daily_total_kwh','–')} · avg {(snap.get('consumption') or {}).get('avg_daily_total_kwh','–')} kWh ({(snap.get('consumption') or {}).get('days_sampled',0)}d)<br>EV {(snap.get('consumption') or {}).get('avg_daily_ev_kwh','0')} · today {(snap.get('consumption') or {}).get('today_so_far_kwh','0')} · profile: {(snap.get('consumption') or {}).get('profile_source','self')} ({(snap.get('forecast_profiles') or {}).get('days',0)} load / {(snap.get('forecast_profiles') or {}).get('days_solar',0)} solar valid days)</small></div>
 <div class=card><small>Demand window</small><div class=big>{'ACTIVE' if snap.get('demand_window') else 'off'}</div><small>{'no demand charge (EA116) — OK to charge if cheap' if snap.get('demand_window') else ''}</small></div>
 <div class=card style="{'background:#fff3e0;border-color:#e67e22' if (snap.get('work_mode_age_s') or 0) > 1800 else ''}"><small>Work mode</small><div class=big>{snap.get('work_mode')}</div><small>{('read '+str(snap.get('work_mode_age_s'))+'s ago' + (' ⚠️ stale' if (snap.get('work_mode_age_s') or 0) > 1800 else '')) if snap.get('work_mode_age_s') is not None else 'no read yet'}</small></div>
 <div class=card style="{'background:#e8f5e9;border-color:#2ecc71' if (snap.get('ev_divert') or '').startswith('car charger ON') else ''}"><small>EV charger</small><div class=big>🔌 {snap.get('ev_kw') if snap.get('ev_kw') is not None else '–'} <small>kW</small></div><small>{snap.get('ev_divert') or ('no switch set' if not (cfg.get('ev_divert') or {}).get('switch') else 'idle')}</small></div>
 <div class=card style="{'background:#fff3e0;border-color:#e67e22' if 'stale' in (snap.get('telemetry_source') or '') or 'down' in (snap.get('telemetry_source') or '') else ''}"><small>Data age / source</small>
   <div class=big><span id=age>{snap.get('data_age_s') if snap.get('data_age_s') is not None else '–'}</span>s
   · <span id=countdown>–</span></div>
   <small>{'⚠️ STALE — control on hold' if ('stale' in (snap.get('telemetry_source') or '') or 'down' in (snap.get('telemetry_source') or '')) else 'FoxESS direct (sole poller)'}</small></div>
 <div class=card style="{'background:#fff3e0;border-color:#e67e22' if (snap.get('scheduler') or {}).get('active') and snap['scheduler']['active']['mode']=='ForceCharge' else ''}">
   <small>Force-charge</small><div class=big>{('⚡ ON' if (snap.get('scheduler') or {}).get('active') and snap['scheduler']['active']['mode']=='ForceCharge' else ('sched on' if (snap.get('scheduler') or {}).get('enabled') else 'OFF'))}</div>
   <small>{(snap.get('scheduler') or {}).get('active',{}).get('window','') if (snap.get('scheduler') or {}).get('active') else ''}</small></div>
</div>
<div id=dyncard>{llm_html}</div>
{chat_panel_html(cfg, snap)}
{note_html}
<h3>Actions taken <small>(real changes: applies, force-charge, disables, band actions)</small></h3>
<table id=events></table>
<h3>Next forecast</h3>{atable}
<h3>Decision log <small>(every cycle, recommendation even when not applied)</small></h3>
<table id=log></table>
{baseline_html}
<p><small>auto-refresh 60s · <a href=/api/state>/api/state</a> · <a href=/api/log?n=100>/api/log</a></small></p>
<script>{JS}</script>
</body></html>"""


def build_export_csv(cfg, snap):
    """CSV for a spreadsheet: yesterday→now 5-min ACTUALS (from FoxESS history) + the forward
    forecast/plan per slot (buy/feed-in price, expected load/solar, rules-model + shadow-plan SoC)."""
    import csv as _csv
    import io as _io
    header = ["time", "kind", "soc_pct", "pv_kw", "load_kw", "grid_import_kw", "grid_export_kw",
              "buy_price", "feedin_price", "exp_load_kwh", "exp_solar_kwh",
              "rules_soc_pct", "plan_soc_pct", "plan_floor_pct"]
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
                        r.get("grid_import_kw", ""), r.get("grid_export_kw", ""), "", "", "", "", "", "", ""])
    except Exception as e:
        w.writerow([f"# actuals unavailable: {e}"])
    # --- forecast / plan (from the snapshot, no extra API calls) ---
    proj = {round(d["h"], 2): d["soc"] for d in (snap.get("projection") or [])}
    plan = snap.get("plan") or {}
    psoc = {round(h, 2): s for h, s in (plan.get("soc_line") or [])}
    pflo = {round(h, 2): s for h, s in (plan.get("floor_line") or [])}
    now = datetime.now()
    for s in (snap.get("plan_slots") or []):
        k = round(s["h"], 2)
        sp = s.get("sell_price")
        w.writerow([(now + timedelta(hours=s["h"])).strftime("%Y-%m-%d %H:%M"), "forecast", "", "", "", "", "",
                    round(s["price"], 3), round(sp, 3) if isinstance(sp, (int, float)) else "",
                    round(s["load"], 3), round(s["solar"], 3),
                    proj.get(k, ""), psoc.get(k, ""), pflo.get(k, "")])
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
            elif self.path.startswith("/api/chat_clear"):
                try:
                    res = clear_chat(cfg)
                except Exception as e:
                    res = {"error": str(e)}
                self._send(200, json.dumps(res, default=str), "application/json")
            elif self.path.startswith("/api/chat"):
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(n).decode()) if n else {}
                    res = llm_chat_reply(cfg, body.get("text", ""))
                except Exception as e:
                    res = {"error": str(e)}
                self._send(200, json.dumps(res, default=str), "application/json")
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
    if args.cmd == "backfill-ha":
        done = backfill_ha_statistics(cfg, max(1, args.days))
        print("HA statistics backfill imported (hourly points):", done or "nothing (no stored days yet)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
