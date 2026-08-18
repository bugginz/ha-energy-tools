# HA tooling

Small websocket clients for things Home Assistant does not expose over REST.

- `ha_dashboard.py` — read/write Lovelace dashboards and resources. Editing
  `.storage/lovelace.*` directly does not work on a running HA (the config is
  held in memory and rewritten on shutdown), so the websocket API is the only
  safe path.
- `ha_entities.py` — inspect and **bulk-rename** entities in the entity registry.
- `hacs_install.py` — install a HACS frontend repository (HACS is websocket-only).

All take `HA_TOKEN` from the environment. They run inside the energy-tools
container, which already has `websocket-client` and reaches HA on localhost:

```sh
scp tools/ha_entities.py tools/ha_dashboard.py robwil@homeassistant.local:/tmp/
ssh robwil@homeassistant.local \
  "docker cp /tmp/ha_entities.py energy-tools:/tmp/ && \
   docker cp /tmp/ha_dashboard.py energy-tools:/tmp/ && \
   docker exec -e HA_TOKEN=\"\$(cat /opt/stack/energy_tools/data/.config/sen66/ha_token)\" \
   -w /tmp energy-tools python3 /tmp/ha_entities.py list em16p"
```

## Bulk renaming (the EM16P case)

The 18-channel Meross monitor exposes **six sensors per channel** — power,
current, voltage, factor, mconsume, energy_estimate — with no grouping between
them. HA renames one entity at a time, so naming a circuit properly is 6 edits,
and the whole device is 108. Renaming just the `power_N` sensor (the obvious
thing to do from the device page) leaves the other five reading "Current 1",
"Voltage 1" and so on.

`ha_entities.py` takes a plan file and applies it in one pass:

```json
[{"entity_id": "sensor.…_current_1", "name": "Grid current"}]
```

`plan` dry-runs and prints the diff; `apply` writes it. Derive the plan from the
name already set on each channel's `power_N` sensor rather than inventing new
ones — that keeps the device named the way its owner intended.

**Rename display names, not entity_ids.** The EM16P entity_ids are referenced by
foxctl's `ev_power_entity` (`power_18`), the circuits-total template in
`template.yaml` (which builds `power_2..18` in a loop), the Power Circuits
dashboard, and the 18 "Circuit N max 7d" statistics helpers. Changing ids breaks
all four, silently.

## WiCAN / Car Scanner log decoding

- `wican_log_decode.py` — turns a Car Scanner adapter log into per-ECU/per-DID
  value histories (ISO-TP reassembled, `\r`-aware), decodes the known Fiat 500e
  fields (`--decode`), and prints WiCAN `B<n>` indices for a payload
  (`--wican-index`). Runs anywhere; no HA access needed. Findings are written up
  in `docs/wican/fiat500e-2020-decoded-pids.md`.
