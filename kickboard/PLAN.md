# Kickboard Position-Aware Lighting — Build Plan

Status: design complete, hardware partially in hand. This document is the brief for implementation.
Anything marked **ASSUMED** must be confirmed or measured before it is relied on.

---

## 1. Goal

Kitchen toe-kick LED strips (one per side of a ~4 m galley) light up as a soft pool centred on
wherever a person is standing. Multiple people → multiple pools. Nobody in the kitchen → off
(or a dim ambient floor, configurable). No app, no switches, no cloud.

### UX spec (these are the tuning targets, not the implementation)

- Pool: Gaussian brightness falloff along the strip, σ ≈ 0.5 m (tunable 0.3–0.8 m).
- Pool tracks the person with a visible lag of roughly 150–300 ms. Faster looks jittery, slower looks laggy.
- Ambient floor: 0–5 % brightness across the whole strip while the room is occupied. Default 3 %.
- Colour: warm white (~2700–3000 K) synthesised from RGB. Default `(255, 147, 41)` scaled; tune by eye.
- Dropout: if tracking is lost while presence is still likely, the last pool fades over ~2 s to the ambient floor. It does not snap off.
- Idle: no targets for `idle_timeout` (default 30 s) → fade everything to zero over 3 s.
- Update rate to strips: 30 fps target. 20 fps minimum.

---

## 2. Hardware in hand / on order

| Item | Status | Notes |
|---|---|---|
| BTF-LIGHTING SMD WS2812B RGB strip (model SMD-WS2812B-RGB-02-ML) | In hand | **5 V**, discrete SMD, 3 wires (5V / GND / DI). LED density **CONFIRMED 60/m**. Length on hand **5 m / 300 LEDs** (one reel; two 4 m sides need a second). |
| Ai-Thinker RD-03D mmWave sensor | Ordered, Core Electronics, ETA 9 Sep | 24 GHz, 1T2R, X/Y for up to 3 targets, UART 256000 8N1, 3.3 V logic |
| Seeed XIAO ESP32-C6 (several) | In hand | Used as strip driver node(s) and optionally radar node |
| Raspberry Pi 5 + Hailo-10H | In hand, runs HA (Docker) | Runs the Python tracking + render service |
| Raspberry Pi Zero 2 W | In hand, currently e-paper display | Fallback only — see §10 |
| USB-serial adapter (CP2102/CH340) | In hand (DSD TECH) | Only needed if radar goes direct to Pi 5. Confirm 3.3 V I/O jumper. |
| 5 V PSU, level shifter, fuses, wire, aluminium channel | **To buy** | See §3 |

### Why the strip isn't ideal, and why we're using it anyway

24 V addressable COB was the recommendation (no dotting, no injection headaches). WS2812B at 5 V
is what arrived first. It is entirely adequate for proving the concept and tuning the renderer.
Treat it as the prototype strip; swapping to COB later is a mechanical change plus one config edit
(LED count, pitch, pixel order). Nothing in the software should assume WS2812B specifics beyond the
config file.

---

## 3. Power design

### 3.1 The numbers (60 LED/m confirmed; 4 m per side still ASSUMED, 2 sides = 480 LEDs)

| Case | Per LED | Per side (240) | Total (480) |
|---|---|---|---|
| Datasheet max, full white 255/255/255 | 60 mA | 14.4 A / 72 W | 28.8 A / 144 W |
| Warm white (255,147,41) at 100 % | ~35 mA | 8.4 A | 16.8 A |
| Realistic: one pool, σ 0.5 m, 60 % peak, 3 % ambient | — | ~1.5 A | ~2.5 A |
| Three pools worst case + ambient | — | ~4 A | ~7 A |

Design rule: **the software enforces a current budget**, and the PSU is sized to the budget with
headroom — not to the theoretical maximum. A 144 W kitchen night-light is absurd.

- Software budget: **6 A per side** (30 W), enforced by the renderer scaling the whole frame down
  if the estimated draw exceeds it (same logic as WLED's ABL). Estimated draw =
  `Σ (r+g+b)/765 × 60 mA` per LED, plus ~1 mA quiescent per LED.
- PSU: **5 V, 20 A minimum** if one PSU feeds both sides; or **5 V 10–12 A per side** if two.
  Mean Well LRS-100-5 (18 A, open frame — needs an enclosure) or LRS-150-5 (30 A). An IP67
  "LED driver" brick is the tidier option for inside a cabinet. Avoid the plastic-wall-wart
  class of "5V 10A" supply; they aren't.

### 3.2 Voltage drop — the part that bites at 5 V

WS2812B strips have thin copper rails. Typical 10 mm FPCB: roughly 0.1–0.2 Ω per metre per rail.
At 4 A over 4 m, single-end fed: ~2×(0.15×4)×4 ≈ **4.8 V drop**. Unusable. The far end goes
orange/brown and the data signal fails before the light does.

Rule: **inject 5 V at both ends of each 4 m run and at the midpoint** (three injection points per
side). Each injection feed is a home run back to the PSU in **1.5 mm² (16 AWG)** twin, not daisy-
chained through the strip. Common ground everywhere.

### 3.3 Protection and hygiene

- Fuse **every injection feed** individually: 5 A blade fuse at the PSU end.
- **1000 µF / 6.3 V** electrolytic across 5V/GND at each injection point (or at least at the head of each strip).
- **330 Ω** series resistor on each data line, as close to the strip DI pad as practical.
- **Level shifter** on data: the C6 outputs 3.3 V, WS2812B wants VIH ≥ 0.7×VDD = 3.5 V. Some units
  tolerate 3.3 V, many flicker or lock up after the first few metres of heat. Use a **74AHCT125**
  (or single-gate SN74AHCT1G125) powered from 5 V. Don't skip this; it is the number one cause of
  "random glitching" with 5 V strips.
- Data run from C6 to strip ≤ 1 m. If longer is unavoidable, use a twisted pair with GND.
- Power the C6 from the same 5 V rail (XIAO 5V pin). Never back-feed USB while the 5 V rail is on.
- Thermal: 60/m strip in a closed aluminium channel at full white runs warm. At the software budget
  above it's a non-issue; at datasheet max it isn't. Another reason the budget exists.

---

## 4. Architecture

```
 RD-03D ──UART 256000──> [radar node]  ──UDP JSON, 10 Hz──┐
                         (XIAO C6 #1)                     │
                                                          ▼
                                            Pi 5: kickboard service (Python)
                                            ├─ radar frame parser
                                            ├─ tracker (per-target smoothing, ID persistence)
                                            ├─ strip geometry map (LED index → x,y in mm)
                                            ├─ renderer (pools → RGB frame, current budget)
                                            ├─ DDP sender (UDP 4048)
                                            ├─ MQTT ↔ Home Assistant (state, config, overrides)
                                            └─ simulator / debug web UI
                                                          │
                              ┌──── DDP, ~30 fps ─────────┼───────── DDP, ~30 fps ────┐
                              ▼                                                       ▼
                    [strip node L] XIAO C6 #2                               [strip node R] XIAO C6 #3
                    DDP receiver → RMT → WS2812B                            DDP receiver → RMT → WS2812B
```

Decisions baked in:

1. **All the maths lives on the Pi 5, in Python.** ESP nodes are dumb: one is a UART-to-UDP bridge,
   the others are DDP-to-pixels. This keeps every tunable in one config file and makes iteration
   fast. Latency budget: radar 100 ms frame + WiFi ~10 ms + render ~2 ms + WiFi ~10 ms — well under
   the 150–300 ms perceptual lag target.
2. **One C6 per strip.** Simpler wiring (each node sits at the head of its strip, next to an injection
   point), and each DDP frame is one UDP packet (240 × 3 = 720 B). A single C6 driving both is also
   fine (two RMT channels) if the geometry allows short data runs to both strips.
3. **Radar on a C6, not USB to the Pi**, because the sensor wants to be mounted at ~1.4 m at the end
   of the kitchen and the Pi 5 is wherever it is. If the Pi 5 *is* within ~2 m of that spot, drop
   the radar node and read the RD-03D on the Pi via the USB-serial adapter — one fewer thing on WiFi.
4. **WLED is not used.** WLED has no stable ESP32-C6 support (upstream issue #3078 still open).
   Custom firmware is ~100 lines and avoids WLED's effect engine, which we don't want anyway.
5. **DDP over E1.31**: no universe splitting, one packet per frame, trivially simple header.

---

## 5. Component: strip node firmware (XIAO ESP32-C6)

Language: C++ on arduino-esp32 core **3.x** (C6 needs 3.x). Alternatively ESP-IDF with the
`led_strip` component. Either is acceptable; Arduino is faster to iterate.

LED library: **FastLED ≥ 3.9** (has RMT5 C6 support) or **NeoPixelBus** with the RMT method.
Verify the chosen library actually compiles for `esp32-c6` before writing anything else.
Reference for a working C6 + RMT + UDP receiver pattern: github.com/lydonator/WLED_Protocol_Shim_ESP32C6.

Behaviour:

- Boot, join WiFi (credentials in a header not committed), static DHCP reservation per node.
- Listen UDP **4048**. Parse DDP header (10 bytes):
  `flags(1)=0x41 (v1|push) | seq(1) | type(1) | dest(1)=1 | offset(4, BE) | len(2, BE)` then payload.
  Copy payload into the pixel buffer at `offset/3`. On the push flag, `show()`.
- Support multi-packet frames (offset ≠ 0) even though we won't need them at 240 LEDs. Costs nothing.
- **Watchdog**: if no DDP packet for 2 s, fade all pixels to black over 1 s. A crashed Pi must not
  leave the kitchen lit.
- Serial debug at 115200: packets/s, last seq, dropped frames.
- Config constants: `NUM_LEDS`, `DATA_PIN`, `COLOR_ORDER` (WS2812B is **GRB**), `MAX_MILLIAMPS`
  (secondary hardware-side limit, set to the PSU rating; FastLED's `setMaxPowerInVoltsAndMilliamps`
  handles this).
- mDNS name `kick-left.local` / `kick-right.local`.
- OTA update (ArduinoOTA) so the node never has to come out of the toe kick.

Pin: any free GPIO on the XIAO C6. Suggest **D0 (GPIO0)** is *not* used (strapping); use **D1** or **D10**.

---

## 6. Component: radar node firmware (XIAO ESP32-C6)

- `Serial1` at **256000 8N1** on two free GPIOs (e.g. D6/D7 = the XIAO TX/RX pads). RD-03D TX → C6 RX.
- On boot, send the RD-03D **multi-target mode** command (see §7). Confirm the ack.
- Read the byte stream, sync on the frame header, validate the tail, and forward each complete frame
  as a UDP packet to the Pi at **10 Hz** (the sensor's native rate; do not decimate, do not buffer).
- Payload: either the raw 30-byte frame (Pi does the parsing — preferred, keeps the node dumb) or a
  pre-parsed JSON. Raw is preferred; parsing lives in one place.
- Also publish a heartbeat every 5 s so the Pi can distinguish "radar dead" from "nobody here".
- Power: RD-03D from the C6's 3V3 pin. **Confirm the RD-03D's rated supply on the datasheet before
  connecting** — Ai-Thinker modules are 3.3 V but some carrier boards have their own regulator and
  expect 5 V. Peak current is a few hundred mA; XIAO 3V3 rail is rated 700 mA.
- Mount: **1.3–1.5 m high, at the end of the kitchen, facing down the long axis.** Boresight along
  the axis where accuracy matters most (range axis ≈ ±15 cm; bearing axis ≈ ±5° ≈ ±35 cm at 4 m).
  No metal in front. Not in the toe kick.

---

## 7. RD-03D protocol (verify against docs.ai-thinker.com/en/Rd-03D_V2 — V1 and V2 differ)

Output frame, 30 bytes, little-endian:

```
AA FF 03 00                          header
[target 1: 8 bytes]                  x(int16) y(int16) speed(int16) dist_res(uint16)
[target 2: 8 bytes]
[target 3: 8 bytes]
55 CC                                tail
```

Units: x/y in **mm**, speed in **cm/s**. All-zero target block = no target in that slot.

**Sign encoding gotcha (this is the one that wastes an evening):** x, y and speed are NOT two's
complement. Bit 15 is a sign flag where **1 = positive, 0 = negative**, and the magnitude is the low
15 bits. Decode as:

```python
def s15(raw: int) -> int:
    mag = raw & 0x7FFF
    return mag if raw & 0x8000 else -mag
```

If the first plot shows the person walking through a wall, this is why. Sensor frame: X lateral
(right positive), Y forward from the sensor face, origin at the sensor.

Mode commands (send on boot; check the ack frame):
- Multi-target: `FD FC FB FA 02 00 90 00 04 03 02 01`
- Single-target: `FD FC FB FA 02 00 80 00 04 03 02 01`

---

## 8. Component: Pi 5 service (Python 3.12, runs as a Docker container beside HA)

Package layout suggestion: `kickboard/` with modules below. `uv` or plain venv; numpy is the only
heavy dependency.

### 8.1 `radar.py` — frame parser
- Receives UDP raw frames (or reads the USB serial port, selectable in config).
- Sync/validate, decode with `s15()`, emit `RadarFrame(ts, targets=[(x_mm, y_mm, v_cms), ...])`.
- Apply the **sensor→room transform**: rotation θ and translation (tx, ty) from calibration (§9).
  Everything downstream works in **room coordinates, millimetres**, origin at a chosen kitchen corner.

### 8.2 `tracker.py` — smoothing and identity
- Per-target exponential smoothing on x,y with a time constant of ~150 ms (tunable), or a 2D
  constant-velocity Kalman filter if EMA is visibly laggy on direction changes. Start with EMA.
- Slot-to-track association by nearest neighbour (the sensor's slot order is not stable).
- Track confidence: ramps up over ~300 ms of consistent detection, decays over ~2 s of absence.
  The renderer uses confidence as the pool's peak brightness multiplier — this gives free
  fade-in/fade-out and rejects one-frame ghosts.
- Reject targets outside the kitchen polygon (config) — the radar sees through the doorway.
- Static-target hold: if a track vanishes with speed ≈ 0 and no track appeared elsewhere, hold its
  last position at decaying confidence for `hold_time` (default 10 s). Handles the "leaning on the
  bench reading" dropout.

### 8.3 `geometry.py` — strip map
- Config lists each strip as a polyline of `(led_index, x_mm, y_mm)` waypoints (start, corners, end).
- Linear interpolation → array `led_xy[node][i] = (x, y)` for every LED. Computed once at startup.
- A `--dump-map` CLI prints it as a plan-view scatter so the geometry can be sanity-checked before
  any lights are on.

### 8.4 `renderer.py`
- Inputs: list of `(x, y, confidence)`, config, `led_xy`.
- For each node: `d² = (led_x − px)² + (led_y − py)²` vectorised over all LEDs;
  `b = ambient + Σ_targets conf × peak × exp(−d² / 2σ²)`, clipped to 1.0.
- Perceptual: apply gamma (2.2) after summing, before quantising. Then multiply by the warm-white
  RGB vector, quantise to uint8, reorder to the node's colour order.
- **Current budget**: estimate mA from the uint8 frame; if over `budget_ma[node]`, scale the whole
  frame linearly. Log when this happens — it's a tuning signal, not a normal state.
- Temporal smoothing on the *output frame* (short EMA, ~50 ms) in addition to input smoothing, to
  suppress any residual per-LED flicker from quantisation.
- Output: `bytes` per node, ready for DDP.

### 8.5 `ddp.py` — sender
- One UDP socket, one packet per node per frame, sequence number incrementing, push flag set.
- Fixed-rate loop (30 fps) driven by a monotonic clock; radar data is sampled from the latest
  tracker state, not awaited. Render rate and radar rate are decoupled.

### 8.6 `ha.py` — MQTT / Home Assistant
- MQTT discovery so HA gets: an on/off switch (master enable), number entities for σ, peak, ambient,
  colour temperature, and a `sensor` with target count. Config changes take effect live.
- Publish `occupancy` (bool) — this is also a useful presence sensor for other automations.
- Honour an HA-side override: if HA sets `mode: manual` and a colour/brightness, render that and
  ignore the radar. Lets the strips double as ordinary kickboard lights.

### 8.7 `sim.py` — simulator and debug UI (build this FIRST)
- Small FastAPI + single HTML page. Plan view of the kitchen polygon, strips drawn as polylines,
  every LED drawn as a dot coloured by its current output.
- **Drag a target with the mouse** to inject a fake `(x, y)` into the tracker in place of radar.
  Sliders for σ, peak, ambient, lag. Noise slider (adds Gaussian jitter to the fake target) and a
  "dropout" button, to tune robustness before the radar even arrives.
- When the radar is live, the same page overlays real targets. Shows raw vs smoothed positions and
  a 10 s trail. This is the primary calibration and ghost-hunting tool.

### 8.8 Config (`config.yaml`)
Everything tunable, including strip geometry, kitchen polygon, radar pose, per-node LED count,
colour order, budgets, timing constants, MQTT broker. No magic numbers in code.

### 8.9 Ops
- Dockerfile, `docker-compose` service on the existing HA stack, restart: unless-stopped.
- Structured logs; a `/health` endpoint; `--replay file.log` to re-run recorded radar data through
  the pipeline for offline tuning.
- Record raw radar frames to a rotating log by default. Cheap, and the ghost analysis needs it.

---

## 9. Calibration procedures

### 9.1 Strip geometry
Measure the room. Tape a reference corner as origin. For each strip record the (x, y) of LED 0, each
corner, and the last LED. Enter as waypoints. Check with `--dump-map` and with a "chase" test
pattern (light one LED at a time while the sim shows where it thinks that LED is).

### 9.2 Radar pose
Tape three floor crosses at known room coordinates spread across the kitchen. Stand on each for
10 s, record the mean raw (x, y). Solve the 2D rigid transform (rotation + translation; a
least-squares Procrustes fit, `scipy` or ten lines of numpy). Store θ, tx, ty in config. Residuals
tell you the real accuracy; expect 10–20 cm.

### 9.3 Ghost survey
Walk the kitchen perimeter slowly while recording. Plot the trail. Persistent second targets that
don't correspond to a person are multipath off the fridge/oven/splashback. Mark those regions as
exclusion zones in config (the tracker ignores detections whose *first* appearance is inside one),
or accept them if they're transient.

### 9.4 Perceptual tuning
Using the sim page with the real strip: adjust σ, peak, ambient, colour, lag until it feels like
the light follows you rather than reacts to you. Expect σ to end up wider than intuition suggests.

---

## 10. Phases and acceptance tests

| Phase | Deliverable | Done when |
|---|---|---|
| 0 | Confirm strip density and length; buy PSU/fuses/shifter/wire/channel | Numbers in §3 updated with real values |
| 1 | Strip node firmware on one C6, bench-driven from a Python DDP test script | Solid colour, chase, and a 30 fps gradient run for 10 min with no glitch |
| 2 | Geometry + renderer + sim UI, driving the bench strip from a dragged mouse target | Pool moves with the mouse; current budget clamp demonstrated |
| 3 | Radar node firmware; raw frames arriving on the Pi; CSV logging | Walk a taped line, plot it, it looks like a line |
| 4 | Tracker + calibration; sim shows real target on plan view | Residuals < 20 cm on the three reference crosses |
| 5 | Install: channel, injection wiring, fusing, PSU siting, both nodes in the toe kick | Both strips run 10 min at software budget with no colour shift at far ends |
| 6 | MQTT/HA integration, manual override, occupancy sensor | Switchable from HA; survives Pi reboot and node power-cycle |
| 7 | Live tuning, ghost zones, static-hold behaviour | Two people in the kitchen for an evening without anyone reaching for the light switch |

Phases 1–2 need no radar and no installed strip. Start there.

### 10.1 Fallback if the C6 toolchain fights back
The Pi Zero 2 W can drive both strips directly with `rpi_ws281x` (PWM0 on GPIO18, PWM1 on GPIO13
— note two channels are limited to PWM0/PWM1) and read the radar over USB, running the whole
service locally with no WiFi in the loop. Costs the e-paper display its host and still needs a
level shifter. Keep in reserve; don't start there.

---

## 11. Open decisions for Rob

1. Actual LED density and total length of the strip on hand.
2. Is the Pi 5 within ~2 m of the radar mounting spot? (Decides radar node vs USB direct.)
3. ~~One PSU for both sides or one per side?~~ DECIDED 2026-09-08: one per side — the sides
   are on opposite walls with no single 240 V point serving both. Each side is fully
   independent (own PSU, own C6); no ground run crosses the kitchen, WiFi is the only link.
4. Ambient floor when occupied: 0 % (pure follow effect) or ~3 % (kickboard lighting that also follows)?
5. Should the strips be usable as plain kickboard lights from HA when the radar is off? (§8.6 assumes yes.)

---

## 12. References

- RD-03D V2 docs: https://docs.ai-thinker.com/en/Rd-03D_V2/index.html
- RD-03D on espboards: https://www.espboards.dev/sensors/rd03/
- DDP protocol: http://www.3waylabs.com/ddp/
- WLED DDP notes: https://kno.wled.ge/interfaces/ddp/
- C6 + RMT + UDP receiver reference: https://github.com/lydonator/WLED_Protocol_Shim_ESP32C6
- WLED C6 status: https://github.com/wled/WLED/issues/3078
- XIAO ESP32-C6 pinout: https://wiki.seeedstudio.com/xiao_pin_multiplexing_esp33c6/
- FastLED: https://github.com/FastLED/FastLED
- rpi_ws281x (fallback): https://github.com/jgarff/rpi_ws281x
