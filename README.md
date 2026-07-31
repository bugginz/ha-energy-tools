# Energy Tools — Home Assistant add-on

A single Home Assistant add-on that runs two services for an Australian NEM home
with a FoxESS inverter + battery:

- **foxctl** — price-aware FoxESS work-mode controller. Reads Amber/AEMO prices,
  battery SoC, solar PV and house load from HA, and grid-charges the battery only
  when it makes sense (cheap price, with hysteresis, solar-awareness, demand-window
  avoidance, and "wait for a cheaper trough" logic). Serves a small web UI on `:8770`.
- **nemfuel** — pulls the live NEM generation fuel mix (coal/gas/wind/solar/hydro/
  battery + renewables %) from the OpenElectricity API and publishes it to HA via
  MQTT discovery.

> Personal project, shared as-is. Region/entities default to NSW1 + the FoxESS-HA
> integration's entity names — adjust for your setup.

## Install (as an add-on repository)

1. **Settings → Apps → Add-on Store → ⋮ → Repositories** → add:
   `https://github.com/bugginz/ha-energy-tools`
2. Install **Energy Tools (foxctl + nemfuel)** from the new repository.
3. In **Configuration**, set the options (see below), then **Start**.

(Or drop the `energy_tools/` folder into `/addons/` as a local add-on.)

## Requirements

- HA OS / Supervised (it's an add-on).
- **Mosquitto broker** add-on + MQTT integration (for nemfuel's sensors).
- The **FoxESS-HA** integration (`macxq/foxess-ha`) providing `sensor.foxess_*`.
- The **Amber Electric** integration (price/forecast/demand-window) and, optionally,
  the **AEMO NEM Pricing** HACS integration (wholesale forecast).
- A free **OpenElectricity** API key (platform.openelectricity.org.au) for nemfuel.

## Options

| Option | Default | Notes |
|---|---|---|
| `foxess_token`, `foxess_sn` | — | FoxESS OpenAPI key + inverter SN |
| `oe_key` | — | OpenElectricity API key |
| `region` | `NSW1` | NEM region |
| `mqtt_user`, `mqtt_pass` | — | MQTT broker creds |
| `charge_start_price` | `0.12` | grid-charge at/below this $/kWh |
| `charge_stop_margin` | `0.04` | stop charging once price > start + this |
| `target_soc` / `reserve_soc` | `80` / `20` | charge cap / floor |
| `force_charge_power_kw` | `10.5` | grid-charge power |
| `solar_defer_kw` | `0.5` | skip grid-charge if solar surplus ≥ this |
| `defer_if_cheaper_by` | `0.04` | wait for a forecast trough this much cheaper |
| `poll_seconds` | `300` | evaluation interval |
| `avoid_demand_window` | `false` | skip grid-charge during Amber demand windows (off — no $/kW demand charge on EA116) |
| `price_ceiling` | `0.20` | **foundation**: never grid-charge above this $/kWh, whatever the dynamic layer asks |
| `max_soc` | `90` | **foundation**: hard charge cap |
| `dynamic_policy` | `true` | let the LLM tune `charge_start_price` + `target_soc` each interval, clamped to the foundation |
| `allow_control` / `auto_apply` / `set_work_mode` / `set_force_charge` | `true` | control switches |

## Kiosk V2 dashboard

`dashboards/build_kiosk.py` generates the "Kiosk V2" Lovelace dashboard (url_path
`kiosk-v2`). The original Kiosk dashboard is left untouched.

```sh
python3 dashboards/build_kiosk.py > dashboards/kiosk_v2.json
HA_TOKEN=... python3 tools/ha_dashboard.py save kiosk-v2 dashboards/kiosk_v2.json
```

Two constraints shape the design, both established by probing the live instance:

- **The markdown card strips all HTML** — inline styles, raw `<svg>` and `<img>` all
  render blank. **`picture-elements` with a data-URI SVG works**, including CSS keyframe
  animation inside the SVG. So each tile is a bespoke SVG (background, label, accent,
  animation) with a `state-label` overlaying the live value. No card-mod or button-card is
  installed, and none is needed.
- **A section spanning two view columns exposes a 24-column grid, not 12.** A third of a row
  is 8, not 4. Every card carries an explicit `grid_options` span so tiles align by
  construction rather than by whatever width the display happens to be — the old dashboard
  sized its picture-elements off an invisible 20:10 SVG, which is why alignment depended on
  the iPad.

Picture-elements images cannot be templated, so state-driven variation swaps between
pre-rendered variants using `visibility` conditions: battery charging/discharging/idle,
house load under 2 kW / 2-4 / over 4, and the A/C arrow by hvac mode.

Edit dashboards over the **websocket** API (`tools/ha_dashboard.py`), never by editing
`.storage/lovelace.*` — a running HA holds that config in memory and will discard or
overwrite direct edits.

## Deploying

This host runs **HA Container**, not HAOS — there is no add-on store, and
`/opt/stack/energy_tools/src` on the Pi is a plain copied tree, not a git clone.
**`git push` alone deploys nothing.** Use the script, which pushes, rsyncs and
rebuilds in one step so the repo and the running container cannot drift apart:

```sh
./deploy.sh              # push + rsync + docker compose build/up + verify
./deploy.sh --dry-run    # show what would sync, change nothing
./deploy.sh --no-push    # rsync + rebuild only
```

It refuses to run on a dirty working tree, checks that `config.yaml` and
`foxctl.py` agree on the version, pins the compose `image:` tag to that version,
backs up the live `options.json`, and tails the startup log when it is done.

New Python modules must be added to the `COPY` line in `energy_tools/Dockerfile`
— it copies named files, not the directory. New options only need adding to
`/opt/stack/energy_tools/data/options.json`; `run.sh` regenerates the runtime
config from it on every start.

Note that `log_event` writes the structured timeline to
`data/foxctl/events.jsonl`, **not** stdout — `docker logs` will look empty of
ev_divert / fill_plan / base_schedule activity even when plenty is happening.

## Notes

- foxctl uses the Supervisor proxy (`http://supervisor/core`) + `SUPERVISOR_TOKEN`
  for HA access — no long-lived token needed.
- The web UI on `:8770` respects light/dark mode and shows live price/SoC/solar,
  the current recommendation, a decision log, an actions log, and control buttons.
- See [`docs/energy-data-sources.md`](docs/energy-data-sources.md) for a guide to
  the underlying HA data sources (no FoxESS specifics).

## Disclaimer

This controls a home battery/inverter. Use at your own risk. Control is gated by
the `allow_control` / `set_force_charge` switches; start with them off and watch the
decision log before enabling autonomous control.
