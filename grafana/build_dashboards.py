#!/usr/bin/env python3
"""Generate the Grafana dashboards (grafana/dashboards/*.json) for the energy stack.

Data: Home Assistant -> InfluxDB 1.8, database "homeassistant", one measurement
per unit ("kW", "%", "kWh", "$/kWh", "state"...), tag entity_id, field value.
Datasource uid "influxdb-ha" is provisioned by provisioning/datasources/.

    python3 grafana/build_dashboards.py        # rewrites grafana/dashboards/*.json
    ./grafana/deploy.sh                        # rsync to the Pi + restart grafana

Edit this file, not the JSON. Entity names come from HA; check with
tools/ha_entities.py or `SHOW TAG VALUES FROM "kW" WITH KEY = entity_id`.
"""

import json
from pathlib import Path

OUT = Path(__file__).parent / "dashboards"
DS = {"type": "influxdb", "uid": "influxdb-ha"}
TZ = "Australia/Sydney"

# ---------------------------------------------------------------- queries

def q_mean(unit, entity, alias, fn="mean"):
    return {"datasource": DS, "rawQuery": True, "resultFormat": "time_series", "alias": alias,
            "query": (f'SELECT {fn}("value") FROM "{unit}" WHERE "entity_id" = \'{entity}\' '
                      f'AND $timeFilter GROUP BY time($__interval) fill(null)')}


def q_slow(unit, entity, alias):
    """Slow-changing value (a setting, a target, a temperature that HA only writes on
    change): look back 30 d so a panel range with no writes still shows the last value."""
    return {"datasource": DS, "rawQuery": True, "resultFormat": "time_series", "alias": alias,
            "query": (f'SELECT last("value") FROM "{unit}" WHERE "entity_id" = \'{entity}\' '
                      f'AND time > ${{__from}}ms - 30d AND time < ${{__to}}ms '
                      f'GROUP BY time($__interval) fill(previous)')}


def q_daily_spread(unit, entity, alias):
    """Per-day energy from a lifetime kWh counter (max - min within the day)."""
    return {"datasource": DS, "rawQuery": True, "resultFormat": "time_series", "alias": alias,
            "query": (f'SELECT spread("value") FROM "{unit}" WHERE "entity_id" = \'{entity}\' '
                      f'AND $timeFilter GROUP BY time(1d) fill(none) tz(\'{TZ}\')')}


def q_daily_integral(entity, alias, unit="W"):
    """Per-day Wh from a power series (integral over time), shown as kWh."""
    return {"datasource": DS, "rawQuery": True, "resultFormat": "time_series", "alias": alias,
            "query": (f'SELECT integral("value", 1h) / 1000 FROM "{unit}" WHERE "entity_id" = \'{entity}\' '
                      f'AND $timeFilter GROUP BY time(1d) fill(none) tz(\'{TZ}\')')}


def q_last(unit, entity, alias):
    return {"datasource": DS, "rawQuery": True, "resultFormat": "time_series", "alias": alias,
            "query": f'SELECT last("value") FROM "{unit}" WHERE "entity_id" = \'{entity}\' AND $timeFilter'}


def q_state(entity, alias):
    return {"datasource": DS, "rawQuery": True, "resultFormat": "time_series", "alias": alias,
            "query": f'SELECT "state" FROM "state" WHERE "entity_id" = \'{entity}\' AND $timeFilter'}

# ---------------------------------------------------------------- panels

_id = [0]


def _next():
    _id[0] += 1
    return _id[0]


def timeseries(title, targets, unit="kwatt", w=12, h=9, stack=False, fill=10, min_=None,
               decimals=None, overrides=None, legend="bottom", line=1):
    p = {
        "id": _next(), "type": "timeseries", "title": title, "datasource": DS,
        "gridPos": {"w": w, "h": h}, "targets": targets,
        "fieldConfig": {"defaults": {
            "unit": unit, "custom": {"lineWidth": line, "fillOpacity": fill, "showPoints": "never",
                                     "spanNulls": True, "stacking": {"mode": "normal" if stack else "none"}}},
            "overrides": overrides or []},
        "options": {"legend": {"displayMode": "list", "placement": legend, "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }
    if min_ is not None:
        p["fieldConfig"]["defaults"]["min"] = min_
    if decimals is not None:
        p["fieldConfig"]["defaults"]["decimals"] = decimals
    return p


def bars(title, targets, unit="kwatth", w=12, h=9, stack=False):
    return {
        "id": _next(), "type": "barchart", "title": title, "datasource": DS,
        "gridPos": {"w": w, "h": h}, "targets": targets,
        "fieldConfig": {"defaults": {"unit": unit, "decimals": 1,
                                     "custom": {"fillOpacity": 80, "lineWidth": 1}}, "overrides": []},
        "options": {"orientation": "vertical", "stacking": "normal" if stack else "none",
                    "xTickLabelRotation": -45, "showValue": "never",
                    "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "none"}},
        "transformations": [{"id": "formatTime", "options": {"timeField": "Time", "outputFormat": "ddd D MMM"}}],
    }


def stat(title, target, unit, w=4, h=4, decimals=1, thresholds=None):
    return {
        "id": _next(), "type": "stat", "title": title, "datasource": DS,
        "gridPos": {"w": w, "h": h}, "targets": [target],
        "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals,
                                     "thresholds": {"mode": "absolute", "steps": thresholds or
                                                    [{"color": "text", "value": None}]}},
                        "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "value", "graphMode": "area", "textMode": "value"},
    }


def state_timeline(title, targets, w=24, h=4):
    return {
        "id": _next(), "type": "state-timeline", "title": title, "datasource": DS,
        "gridPos": {"w": w, "h": h}, "targets": targets,
        "fieldConfig": {"defaults": {"custom": {"fillOpacity": 70, "lineWidth": 0}}, "overrides": []},
        "options": {"showValue": "auto", "mergeValues": True, "rowHeight": 0.8,
                    "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}},
    }


def row(title):
    return {"id": _next(), "type": "row", "title": title, "collapsed": False, "gridPos": {"w": 24, "h": 1},
            "panels": []}


def layout(panels):
    """Assign x/y: rows span the width; other panels flow left-to-right."""
    x = y = 0
    row_h = 0
    for p in panels:
        w, h = p["gridPos"]["w"], p["gridPos"]["h"]
        if p["type"] == "row" or x + w > 24:
            x, y = 0, y + row_h
            row_h = 0
        p["gridPos"].update({"x": x, "y": y})
        x += w
        row_h = max(row_h, h)
    return panels


def dashboard(uid, title, panels, tags=(), refresh="1m", time_from="now-24h"):
    return {
        "uid": uid, "title": title, "tags": ["energy", *tags], "timezone": TZ, "editable": True,
        "schemaVersion": 39, "version": 1, "refresh": refresh, "graphTooltip": 1,
        "time": {"from": time_from, "to": "now"}, "panels": layout(panels),
        "templating": {"list": []}, "annotations": {"list": []},
    }

# ---------------------------------------------------------------- entities

F = "foxess_foxctl_"
CIRCUITS = {  # em16p channel -> name (ch 1 grid and ch 7 inverter are the supply, not loads)
    8: "A/C", 9: "Hot water", 10: "Oven", 11: "Upstairs", 12: "Downstairs", 14: "Lights",
    16: "Cooktop", 18: "Car charger", 2: "Spare 2", 3: "Spare 3", 4: "Spare 4", 5: "Spare 5",
    6: "Spare 6", 13: "Spare 13", 15: "Spare 15", 17: "Spare 17",
}
EM = "em16p_26041762237810740701c4e7ae2e4c89_power_"
AC_ROOMS = {"living_room_ac": "Living room", "main_bedroom_ac": "Main bedroom",
            "isobel_bedroom": "Isobel", "mathilda_bedroom": "Mathilda"}

# ---------------------------------------------------------------- dashboards


def energy_overview():
    p = [row("Now")]
    p += [
        stat("House load", q_last("kW", F + "house_load", "load"), "kwatt"),
        stat("Solar", q_last("kW", F + "solar_power", "solar"), "kwatt"),
        stat("Grid import", q_last("kW", F + "grid_import", "import"), "kwatt",
             thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}, {"color": "red", "value": 5}]),
        stat("Grid export", q_last("kW", F + "grid_export", "export"), "kwatt"),
        stat("Battery SoC", q_last("%", F + "battery_soc", "soc"), "percent", decimals=0,
             thresholds=[{"color": "red", "value": None}, {"color": "orange", "value": 25}, {"color": "green", "value": 50}]),
        stat("Price", q_last("$/kWh", "home_general_price", "price"), "currencyUSD", decimals=3,
             thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 0.3}, {"color": "red", "value": 0.6}]),
        row("Power"),
        timeseries("Power flows", [
            q_mean("kW", F + "house_load", "House load"),
            q_mean("kW", F + "solar_power", "Solar"),
            q_mean("kW", F + "grid_import", "Grid import"),
            q_mean("kW", F + "grid_export", "Grid export"),
            q_mean("kW", F + "battery_charge_power", "Battery charge"),
            q_mean("kW", F + "battery_discharge_power", "Battery discharge"),
            q_mean("kW", F + "ev_charger_power", "Car charger"),
        ], w=24, h=10, fill=5),
        timeseries("Battery", [
            q_mean("%", F + "battery_soc", "SoC"),
            q_slow("%", F + "target_soc", "Target"),
            q_mean("%", "battery_coast_margin", "Coast margin"),
        ], unit="percent", w=12, h=8, fill=0, min_=0),
        timeseries("Prices", [
            q_mean("$/kWh", "home_general_price", "Import (GloBird ToU)"),
            q_mean("$/kWh", "aemo_nem_nsw1_current_5min_period_price", "AEMO 5-min"),
            q_mean("$/kWh", "amber_feed_in_price", "Amber feed-in"),
        ], unit="currencyUSD", w=12, h=8, fill=0, decimals=2),
        state_timeline("Inverter work mode", [q_state("work_mode", "Work mode")]),
        row("Energy per day"),
        bars("Daily energy", [
            q_daily_spread("kWh", F + "solar_energy", "Solar"),
            q_daily_spread("kWh", F + "house_load_energy", "House load"),
            q_daily_spread("kWh", F + "grid_import_energy", "Grid import"),
            q_daily_spread("kWh", F + "grid_export_energy", "Grid export"),
            q_daily_spread("kWh", F + "battery_charge_energy", "Battery charge"),
            q_daily_spread("kWh", F + "battery_discharge_energy", "Battery discharge"),
            q_daily_spread("kWh", F + "ev_charger_energy", "Car charger"),
        ], w=24, h=10),
    ]
    return dashboard("energy-overview", "Energy overview", p, tags=["foxess"], time_from="now-7d")


def battery():
    p = [
        timeseries("State of charge", [
            q_mean("%", F + "battery_soc", "SoC"),
            q_slow("%", F + "target_soc", "Target"),
            q_mean("%", "battery_coast_margin", "Coast margin"),
            q_slow("%", "min_soc", "Min SoC"), q_slow("%", "max_soc", "Max SoC"),
        ], unit="percent", w=24, h=9, fill=0, min_=0),
        state_timeline("Work mode", [q_state("work_mode", "Work mode")]),
        timeseries("Charge / discharge", [
            q_mean("kW", F + "battery_charge_power", "Charge"),
            q_mean("kW", F + "battery_discharge_power", "Discharge"),
            q_slow("kW", "force_charge_power", "Force-charge setting"),
            q_slow("kW", "force_discharge_power", "Force-discharge setting"),
            q_slow("kW", "grid_upload_kw", "Grid-upload slider"),
        ], w=12, h=9, fill=5),
        timeseries("Temperatures", [
            q_mean("°C", "battery_temp", "Battery"),
            q_mean("°C", "invtemp", "Inverter"),
            q_mean("°C", "ambtemp", "Ambient"),
        ], unit="celsius", w=12, h=9, fill=0),
        timeseries("Health", [
            q_slow("%", "battery_soh", "SoH"),
            q_mean("kWh", "battery_energy", "Battery kWh remaining"),
        ], unit="percent", w=12, h=7, fill=0,
            overrides=[{"matcher": {"id": "byName", "options": "Battery kWh remaining"},
                        "properties": [{"id": "unit", "value": "kwatth"}, {"id": "custom.axisPlacement", "value": "right"}]}]),
        timeseries("Planner", [
            q_mean("kWh", F + "avg_daily_load", "Avg daily load"),
            q_mean("kWh", F + "solar_remaining_today_cal", "Solar remaining today"),
            q_mean("kWh", F + "solar_tomorrow_cal", "Solar tomorrow"),
        ], unit="kwatth", w=12, h=7, fill=0),
        bars("Battery energy per day", [
            q_daily_spread("kWh", F + "battery_charge_energy", "Charge"),
            q_daily_spread("kWh", F + "battery_discharge_energy", "Discharge"),
        ], w=24, h=8),
    ]
    return dashboard("battery", "Battery & foxctl", p, tags=["foxess", "foxctl"], time_from="now-48h")


def circuits():
    loads = [q_mean("W", f"{EM}{ch}", name) for ch, name in CIRCUITS.items() if not name.startswith("Spare")]
    p = [
        timeseries("House circuits (stacked)", loads, unit="watt", w=24, h=11, stack=True, fill=60),
        timeseries("Supply vs sum of circuits", [
            q_mean("W", "house_supply_power", "Supply (grid + inverter)"),
            q_mean("W", "circuits_total_power", "Sum of circuits"),
            q_mean("W", "circuits_residual_power", "Residual (unclamped)"),
        ], unit="watt", w=12, h=8, fill=0),
        timeseries("Grid and inverter channels", [
            q_mean("W", f"{EM}1", "Grid"), q_mean("W", f"{EM}7", "Inverter"),
        ], unit="watt", w=12, h=8, fill=0),
        bars("Energy per circuit per day", [q_daily_integral(f"{EM}{ch}", name)
                                            for ch, name in CIRCUITS.items() if not name.startswith("Spare")],
             w=24, h=10, stack=True),
    ]
    return dashboard("circuits", "Circuits", p, tags=["circuits"], time_from="now-24h")


def solar():
    p = [
        timeseries("PV strings", [q_mean("kW", f"{F}pv_string_{i}", f"String {i}") for i in range(1, 7)],
                   w=24, h=9, stack=True, fill=50),
        timeseries("Solar vs generation", [
            q_mean("kW", F + "solar_power", "Solar (foxctl)"),
            q_mean("kW", "foxess_modbus_pv_power", "PV (inverter modbus)"),
            q_mean("kW", F + "grid_export", "Export"),
        ], w=12, h=8, fill=0),
        timeseries("Forecast (kWh)", [
            q_mean("kWh", "energy_production_today", "Today (1)"),
            q_mean("kWh", "energy_production_today_2", "Today (2)"),
            q_mean("kWh", "energy_production_today_3", "Today (3)"),
            q_mean("kWh", "energy_production_today_4", "Today (4)"),
            q_mean("kWh", F + "solar_remaining_today_cal", "foxctl remaining today"),
            q_mean("kWh", F + "solar_tomorrow_cal", "foxctl tomorrow"),
        ], unit="kwatth", w=12, h=8, fill=0),
        bars("Solar per day", [q_daily_spread("kWh", F + "solar_energy", "Solar"),
                               q_daily_spread("kWh", F + "grid_export_energy", "Exported")], w=12, h=8),
        timeseries("Sky", [
            q_mean("%", "openweathermap_cloud_coverage", "Cloud cover"),
            q_mean("°C", "openweathermap_temperature", "Outside °C"),
        ], unit="percent", w=12, h=8, fill=0),
    ]
    return dashboard("solar", "Solar", p, tags=["solar"], time_from="now-7d")


def market():
    gen = ["coal", "gas", "hydro", "wind", "solar_utility", "battery_discharging", "bioenergy", "distillate"]
    p = [
        timeseries("NSW wholesale price", [
            q_mean("$/kWh", "aemo_nem_nsw1_current_5min_period_price", "5-min"),
            q_mean("$/kWh", "aemo_nem_nsw1_current_30min_avg", "30-min avg"),
            q_mean("$/kWh", "aemo_nem_nsw1_current_30min_forecast", "30-min forecast"),
            q_mean("$/kWh", "home_general_price", "Retail import"),
            q_mean("$/kWh", "amber_feed_in_price", "Amber feed-in"),
        ], unit="currencyUSD", w=24, h=9, fill=0, decimals=3),
        timeseries("NSW generation mix", [q_mean("MW", f"nem_nsw1_generation_{g}", g.replace("_", " "))
                                          for g in gen], unit="megwatt", w=12, h=9, stack=True, fill=60),
        timeseries("Renewables & carbon", [
            q_mean("%", "nem_nsw1_generation_renewables", "Renewables %"),
            q_mean("%", "home_renewables", "Home renewables %"),
            q_mean("%", "electricity_maps_grid_fossil_fuel_percentage", "Fossil %"),
        ], unit="percent", w=12, h=9, fill=0, min_=0),
        timeseries("CO2 intensity", [q_mean("gCO2eq/kWh", "electricity_maps_co2_intensity", "gCO2/kWh")],
                   unit="none", w=12, h=7, fill=10),
        timeseries("Interconnectors", [
            q_mean("MW", "aemo_nem_nsw1_nsw1_qld1_2", "NSW→QLD"),
            q_mean("MW", "aemo_nem_nsw1_vic1_nsw1_2", "VIC→NSW"),
        ], unit="megwatt", w=12, h=7, fill=0),
    ]
    return dashboard("market", "NEM market", p, tags=["nem"], time_from="now-48h")


def climate():
    p = []
    for key, name in AC_ROOMS.items():
        p.append(timeseries(name, [
            q_mean("°C", f"{key}_ac_home", "Room"),
            q_slow("°C", f"{key}_ac_target", "Target"),
            q_slow("°C", f"{key}_outside", "Outside unit"),
            q_slow("Hz", f"{key}_comp", "Compressor Hz"),
        ], unit="celsius", w=12, h=8, fill=0,
            overrides=[{"matcher": {"id": "byName", "options": "Compressor Hz"},
                        "properties": [{"id": "unit", "value": "rothz"}, {"id": "custom.axisPlacement", "value": "right"}]}]))
    p += [
        timeseries("Outside", [
            q_mean("°C", "openweathermap_temperature", "Temperature"),
            q_mean("°C", "openweathermap_apparent_temperature", "Feels like"),
            q_mean("%", "openweathermap_humidity", "Humidity %"),
        ], unit="celsius", w=12, h=8, fill=0),
        timeseries("A/C circuit power", [q_mean("W", f"{EM}8", "A/C")], unit="watt", w=12, h=8),
        timeseries("UPS", [
            q_slow("%", "eaton_battery_charge", "Charge %"),
            q_slow("%", "eaton_load", "Load %"),
        ], unit="percent", w=12, h=6, fill=0, min_=0),
    ]
    return dashboard("climate", "Climate & house", p, tags=["climate"], time_from="now-48h")


def costs():
    p = [
        stat("Latest daily cost", q_last("AUD", "globird_energy_latest_daily_cost", "cost"), "currencyUSD", decimals=2),
        stat("Billing period so far", q_last("AUD", "globird_energy_billing_period_cost", "cost"), "currencyUSD", decimals=2),
        stat("Expected monthly", q_last("AUD", "globird_energy_expected_monthly_cost", "cost"), "currencyUSD", decimals=2),
        stat("Account balance", q_last("AUD", "globird_energy_balance", "bal"), "currencyUSD", decimals=2),
        timeseries("Daily cost (GloBird)", [q_mean("AUD", "globird_energy_latest_daily_cost", "Daily cost", fn="last")],
                   unit="currencyUSD", w=24, h=8, fill=10, decimals=2),
        bars("GloBird metered per day", [
            q_mean("kWh", "globird_energy_latest_day_usage", "Usage", fn="last"),
            q_mean("kWh", "globird_energy_latest_day_solar_export", "Solar export", fn="last"),
        ], w=24, h=8),
    ]
    return dashboard("costs", "Costs", p, tags=["globird"], time_from="now-30d")


def main():
    OUT.mkdir(exist_ok=True)
    for build in (energy_overview, battery, circuits, solar, market, climate, costs):
        _id[0] = 0
        d = build()
        (OUT / f"{d['uid']}.json").write_text(json.dumps(d, indent=1) + "\n")
        print(f"{d['uid']:16s} {len(d['panels']):2d} panels")


if __name__ == "__main__":
    main()
