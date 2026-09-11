# Kickboard position-aware lighting

Kitchen toe-kick WS2812B strips light up as a soft Gaussian pool centred on whoever
is standing at the bench, tracked by an Ai-Thinker RD-03D mmWave radar. Multiple
people, multiple pools; empty kitchen, lights off (or a dim ambient floor). No app,
no switches, no cloud. [`PLAN.md`](PLAN.md) is the full design brief; this file is
how the implementation maps onto it.

```
RD-03D --UART--> radar node (C6) --UDP raw frames--> Pi 5 service --DDP--> strip nodes (C6) --> strips
                                                        |
                                                     MQTT <-> Home Assistant
```

All the maths lives in the Python service; the ESP32-C6 nodes are deliberately dumb
(one UART-to-UDP bridge, two DDP-to-pixels receivers). Every tunable is in
`config.yaml` — no magic numbers in code.

## Layout

| Path | What |
|---|---|
| `kickboard/radar.py` | RD-03D frame parse (incl. the sign-flag gotcha), stream resync, sensor→room transform + Procrustes pose solver, UDP/serial sources, raw-frame recorder/replay |
| `kickboard/tracker.py` | NN association, EMA smoothing, confidence ramp/decay, kitchen-polygon gate, ghost exclusion zones, static-target hold |
| `kickboard/geometry.py` | waypoint polyline → per-LED (x, y) map, `--dump-map` ASCII plan view |
| `kickboard/renderer.py` | pools → RGB frames: gamma, warm-white, output EMA, per-node current budget (WLED-ABL-style whole-frame scaling) |
| `kickboard/ddp.py` | DDP sender, one packet per node per frame |
| `kickboard/ha.py` | MQTT discovery: enable switch, mode select, manual light, σ/peak/ambient/CCT numbers, occupancy + target-count sensors |
| `kickboard/sim.py` + `static/index.html` | simulator/debug UI: drag a fake target, sliders, dropout button, raw-vs-smoothed trails, live LED colours; also `/health` |
| `kickboard/main.py` | service entry point, render loop, replay |
| `firmware/strip_node/` | C6 DDP receiver (FastLED ≥ 3.9 for RMT5 C6 support), 2 s watchdog fade, OTA |
| `firmware/radar_node/` | C6 UART→UDP bridge, multi-target mode command, 5 s heartbeat, OTA |
| `tools/ddp_test.py` | phase 1 bench patterns: solid / chase / gradient / off |
| `../tests/test_kickboard.py` | stdlib-unittest suite for the hardware-free pipeline |

## Start here (no hardware needed)

Phases 1–2 of the plan need no radar and no installed strip:

```sh
pip install -r requirements.txt
python3 -m kickboard.main --config config.yaml --dump-map   # sanity-check geometry
python3 -m kickboard.main --config config.yaml --no-ddp     # sim UI on :8771
```

`config.bench.yaml` is the single-reel bench rig (300 LEDs on `kick-left`);
use it instead of `config.yaml` until the kitchen is measured.

Open http://localhost:8771/ and drag on the plan view — that injects a fake target
into the real tracker at the radar's 10 Hz, with a noise slider and a dropout
button to tune robustness before the sensor even arrives. Once a strip node is on
the bench, drop `--no-ddp` (or drive it directly with `tools/ddp_test.py` for the
phase 1 acceptance run).

Replay a recorded radar log through the whole pipeline offline:

```sh
python3 -m kickboard.main --config config.yaml --replay data/radar-logs/radar-2026-09-10.jsonl
```

Raw frames are recorded to `data/radar-logs/` by default (daily files, 14-day
retention) — the §9.3 ghost survey needs them.

**Two sensors**: list both under `radar.sources` (keyed by sender IP, each with
its own pose — see config.yaml). Fusion falls out of the tracker: both sensors'
views of one person land inside the association gate and feed the same track on
alternating updates, so opposite-end mounts cover each other's blind cones with
no extra machinery. Calibrate each sensor separately; recorded frames carry a
`src` tag so one walk's log splits per sensor. The sim draws every FOV wedge.

## Firmware

Both sketches target arduino-esp32 core **3.x** (the C6 needs 3.x). Copy
`wifi_credentials.h.example` to `wifi_credentials.h` (gitignored) in each sketch
directory, set the per-node `#define`s at the top (`NODE_NAME`, `NUM_LEDS`,
`PI_HOST`), and verify FastLED ≥ 3.9 actually compiles for `esp32-c6` before
wiring anything. Nodes are OTA-updatable (`kick-left.local`, `kick-right.local`,
`kick-radar.local`) so they never come out of the toe kick.

**Radar node as an ESPHome device instead.** The bench radar is an RD-03D on a
XIAO ESP32-S3 Sense camera node that already runs ESPHome, so rather than the
Arduino `radar_node` sketch it uses an ESPHome external component doing the same
job: `~/projects/HA/esphome/components/rd03d_bridge/` (UART sync → raw 30-byte
frames over UDP to `radar.udp_port`, JSON heartbeat every 5 s). Either node
type is interchangeable from the service's point of view.

Byte order on the wire is **RGB**: the strip firmware's FastLED `COLOR_ORDER GRB`
handles the WS2812B's chip ordering, so the per-node `color_order` in config stays
`RGB` unless a future receiver shifts raw bytes straight out.

## Home Assistant

`ha-package-kickboard.yaml` is a drop-in HA package that snapshots one of the
existing ESPHome XIAO S3 camera nodes whenever the tracker sees a first target
in an empty kitchen — ground truth for the §9.3 ghost survey with no new
firmware (set the camera entity inside; needs MQTT enabled below).

Set `mqtt.enabled: true` plus broker details and the service discovers as one
"Kickboard lighting" device: master enable, auto/manual mode, a plain RGB light
entity (turning it on switches to manual — the strips become ordinary kickboard
lights and the radar is ignored; off returns to auto), live σ/peak/ambient/colour-
temperature numbers, a target-count sensor and an `occupancy` binary sensor usable
by other automations.

## Deploying

```sh
docker compose up -d --build     # see docker-compose.yaml; mounts ./data
```

Host networking is used so mDNS node names and the UDP ports work without
juggling. For the radar-on-USB variant (Pi within ~2 m of the mount point), set
`radar.source: serial` and pass the device through — one fewer thing on WiFi.

## Tests

```sh
python3 -m unittest tests.test_kickboard -v
```

## Deviations from PLAN.md, and why

- **Ambient floor is a post-gamma duty floor, not a pre-gamma term.** A
  "3 %" ambient run through gamma 2.2 is 0.03^2.2 ≈ 0.0005 — under one 8-bit LSB,
  i.e. black. The renderer applies gamma to the summed pools and then lifts the
  frame onto an `ambient` duty floor (3 % duty ≈ RGB 8/4/1 warm white), which is
  what the spec visibly meant. The idle-fade master envelope is gamma'd too so
  fades read as linear.
- **`color_order` default is RGB** (wire order), per the firmware note above; the
  plan's table said GRB (chip order), which the firmware owns.
- **Confidence doubles as the dropout fade**: tracker confidence decays over
  `confidence_decay_s` (2 s) and multiplies pool peak, which *is* the "fade to
  ambient over ~2 s on tracking loss" behaviour — there is no separate fade path.

## Open items (blocked on hardware / Rob)

- ~~Count the strip's LED density and measure its length~~ — done 2026-09-08:
  60/m, the reel is 5 m / 300 LEDs, so two reels are needed for two 4 m sides.
  The §3 power numbers stand. `config.bench.yaml` describes the bench rig.
- Measure the room; replace the placeholder polygon, waypoints and radar pose.
  `radar.solve_pose()` does the §9.2 three-cross Procrustes fit.
- ~~One PSU or two~~ — one (Core 26 A unit), wires across the gap, strips daisy-chained
  into a single 7 or 9 m run (second reel ordered). One node vs split-at-the-gap is an
  install-time call; both work unchanged (multi-packet DDP frames are unit-tested).
- Decide: radar node vs USB-direct, ambient 0 % vs 3 %.
