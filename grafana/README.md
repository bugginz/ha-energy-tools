# Grafana dashboards for the energy stack

Grafana (`:3001`) and InfluxDB 1.8 run in the Pi's compose stack; Home Assistant's
`influxdb:` integration streams every entity into the `homeassistant` database
(one measurement per unit — `kW`, `%`, `kWh`, `$/kWh`, `state` — tag `entity_id`,
field `value`). History starts 2026-06-10, no retention limit.

Everything here is provisioned from files, so Grafana needs no manual setup and no
admin password to configure:

| Path | What |
|---|---|
| `provisioning/datasources/influxdb.yaml` | the InfluxDB datasource (`uid: influxdb-ha`, default) |
| `provisioning/dashboards/energy.yaml` | file provider: loads `dashboards/*.json` into folder **Energy** |
| `build_dashboards.py` | generates `dashboards/*.json` — **edit this, not the JSON** |
| `deploy.sh` | regenerate, rsync to `/opt/stack/grafana/{provisioning,dashboards}`, restart grafana |

Dashboards: **Energy overview** (live stats, power flows, battery, prices, work mode,
daily kWh), **Battery & foxctl**, **Circuits** (the 16-channel clamp monitor, stacked,
per-day integral), **Solar** (strings, forecasts), **NEM market** (wholesale price,
generation mix, renewables, interconnectors), **Climate & house** (four A/C units,
outside, UPS), **Costs** (GloBird billing sensors).

Daily energy bars use `spread(value)` per day on the lifetime kWh counters; circuit
energy is `integral(value, 1h)` of the W series. Provisioned dashboards are read-only
in the UI (`allowUiUpdates: false`) so the repo stays the source of truth — duplicate
one via *Save as* if you want to experiment in the UI.

Entity names are Home Assistant's; list what InfluxDB has with

```sh
curl -sG homeassistant.local:8086/query --data-urlencode db=homeassistant \
  --data-urlencode 'q=SHOW TAG VALUES FROM "kW" WITH KEY = entity_id'
```
