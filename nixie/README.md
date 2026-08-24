# Nixie SoC display

A six-tube IN-8-2 spark-tube clock (board `IN-8-2-p 1.4v ds`, ATmega328P) converted
to display house-battery state of charge from Home Assistant.

Stock firmware was factory-locked (no serial bootloader, flash read blocked), so the
chip was erased and re-flashed with Optiboot. Everything below was reverse-engineered
with a multimeter and test sketches — the vendor has no schematics.

## Architecture

```
Home Assistant  --wifi/API-->  ESP32-C6 (ESPHome)  --UART 57600-->  ATmega328P  -->  tubes
```

The AVR owns the display: HV boost control, multiplex refresh, LED bar. The ESP32
only fetches data and pushes a short line over serial. Splitting it this way keeps
the refresh loop free of a WiFi stack that could stall it.

## Pin map (all meter-verified)

### Shift-register chain — 2x TPIC6B595 (SOIC-20, markings sanded off)

| Signal | TPIC pin | AVR pin | Arduino |
|---|---|---|---|
| SER IN (data) | 3 | 23 | A0 |
| SRCK (shift clock) | 13 | 26 | A3 |
| RCK (latch) | 12 | 24 | A1 |
| SER OUT (cascade to 2nd chip) | 18 | — | — |
| /SRCLR | 8 | tied to VCC | — |
| /G (output enable) | 9 | tied to GND | — |

Chain is 16 bits. **Bit N lights digit N** (0-9), identical for every tube — the
cathodes are bused. Bits 10-15 are unused.

### Tube anodes (active HIGH, via 2-transistor high-side stage)

| Tube (left to right) | Arduino pin |
|---|---|
| 1 | D10 |
| 2 | D8 |
| 3 | D7 |
| 4 | D6 |
| 5 | D5 |
| 6 | D4 |

### Other

| Function | Pin | Notes |
|---|---|---|
| Master blanking | A2 | **active LOW** — must be driven LOW or the display stays dark |
| HV boost PWM | D9 / PB1 (OC1A) | Timer1 fast PWM mode 14, ICR1=511, ~31.25 kHz |
| Under-tube RGB LEDs | D11 | WS2812, 6 of them, **shared with ISP MOSI** |
| Separator LEDs | D3 | plain GPIO, both LEDs wired together, no individual control |
| UART RX (from ESP32) | pin 30 / PD0 | pad trace damaged; wire soldered direct to MCU leg |
| UART TX | pin 31 / PD1 | deliberately not wired — link is one-way |

## HV boost

Single stage, 5V in to ~156V out: OC1A PWM -> driver transistor -> KND4820B
(N-FET, 200V) -> 150uH inductor -> 3.3uF 450V cap. No hardware regulation; the
firmware *is* the power supply. Unloaded output is roughly `2.04 * duty + 6` volts.

**Duty must exceed the unloaded calibration once tubes draw current.** At duty 74
the rail reads 156V unloaded but sags to ~130V under load — below the IN-8-2 strike
voltage, so nothing lights. Multiplexing six tubes needs more still.

| Situation | Working duty |
|---|---|
| One tube held on | 140 |
| Six tubes multiplexed | 240-280 |

Always ramp the duty up gradually from zero, and clear the shift chain *before*
starting the ramp — the registers power up in a random state and will flicker
garbage across the tubes as the rail crosses strike voltage.

## Serial protocol

The ESP32 sends one line every 10 seconds:

```
SSSPPPD\n
```

* `SSS` — state of charge, 3 digits, zero-padded
* `PPP` — absolute net battery power in 0.1 kW units, 3 digits
* `D` — direction: `C` charging, `D` discharging

Example: `079014D` = 79%, discharging 1.4 kW.

## Flashing

ISP via USBasp, wired to the castellated pads on the underside of the board:

```
MOSI -> IDC 1    RST -> IDC 5    SCK -> IDC 7    MISO -> IDC 9    GND -> IDC 4/6/8/10
```

Leave IDC 2 (VTG) unconnected — the clock powers itself.

```sh
arduino-cli compile -b arduino:avr:uno --output-dir build .
avrdude -c usbasp -p m328p -e \
  -U flash:w:build/nixie_live.ino.hex:i \
  -U flash:w:optiboot_atmega328.hex:i \
  -U lock:w:0xCF:m
```

Always write the sketch and Optiboot together in one command: `-e` erases the whole
chip including the bootloader, and flashing with `-D` over existing code corrupts it.

Fuses are stock and already correct for a 16 MHz crystal (lfuse `0xFF`, hfuse `0xDE`,
efuse `0xFD`) — do not touch them.

**The programmer must be unplugged to run.** MOSI is shared with the WS2812 data
line, so a connected USBasp corrupts the LEDs.

## Sketches

* `nixie_live.ino` — the real firmware: parses the serial protocol, drives the display
* `nixie_static.ino` — static `123456`, no serial. Useful for checking the display path
* `nixie_bounce.ino` — LED animation, kept for use as an alarm pattern
