# Nixie SoC display — 6-LED flow encoding

Design agreed 2026-08-28. Implementation waits on the board being flashable
(see project memory: ISP via pins 15/16/17/29 + Optiboot; resolve HV-boost
regulation before chip-erase). LEDs are addressable RGB (WS2812-class).

## The one colour language, shared with the tronbyt displays

Colour = **origin of the power**, no exceptions:

| colour | meaning |
|--------|---------|
| yellow `#fcd34d` | from solar |
| violet `#a78bfa` | from grid |
| green `#22c55e` | from battery (matches SoC bar + battery icon) |

The tronbyt flow diagram adopted this on 2026-08-28 (tronbyt-wide `34b3c6d`).
Anything added later (LED strips, kiosk accents) uses the same mapping.

## What the LEDs mean: battery-centric

The tubes answer *how full*. The LEDs answer *what is happening to it and why*:

- **Charging** — LEDs chase **inward** (both ends toward the centre, 3+3
  mirrored; converging reads as filling). Colour = the source doing the
  charging: yellow when solar, violet when grid (the 10:00–14:00 free window
  makes grid-charge a daily sight). If both charge at once, partition the six
  by share — e.g. 4 yellow + 2 violet.
- **Discharging** — LEDs chase **outward** (centre to the ends), green.
- **Idle** — off, with a faint single-LED breathe every ~5s as a heartbeat so
  "off" is distinguishable from "dead".

**Rate** is encoded twice, redundantly:
- chase speed: one full cycle 2.0s at ≤0.25kW, scaling linearly to 0.4s at
  ≥5kW;
- lit count per chase: 1–6 LEDs by tier (<0.5, <1, <2, <3.5, <5, ≥5 kW) —
  legible across the room where speed differences are not.

## Data source

The same `/dev/shm/tronbyt/snapshot.env` every display renders from —
NET (battery kW, + charging), SOLAR, GRID, SOC. The reconciliation there
already enforces the balance and the ≥95%-knowledge rule (grid from its own
CT, blip-suppressed; solar zero after dark), so the nixie inherits it for
free and can never disagree with the tronbyts. Transport TBD when the board
is up — MQTT topic published by snapshot.sh is the natural fit.

## Scenario map

| scenario | tubes | LEDs |
|----------|-------|------|
| sunny, charging | SoC rising | yellow, inward |
| free-window grid charge | SoC rising | violet, inward |
| free window + sun both charging | SoC rising | 6 split by share, inward |
| evening / cloudy assist / night hold | SoC falling | green, outward |
| peak sell (battery → grid) | SoC falling | green, outward, fast |
| battery full, solar exporting | 100 | idle heartbeat (tubes tell the story) |
| battery idle, grid or solar carrying house | steady | idle heartbeat |
| load-step grid blip | steady | nothing — blip suppression upstream |

Deliberately NOT encoded: house load and export destination. Six LEDs can
say one thing well; the battery's story is that thing. The tronbyts carry
the full flow picture.

## Degradation

- Fixed-colour LEDs (if RGB assumption fails on the bench): keep direction +
  speed + count, drop colour — still says charging/discharging/how hard.
- Night: follow the same night windows as the tronbyt shade; drop brightness
  hard (these are bedroom-visible), keep the heartbeat.
