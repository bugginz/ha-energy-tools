# Tidbyt — house battery

`battery.star` is a pure renderer (64x32): SoC big, kWh + charge/discharge in
the corner, SoC bar coloured by `sensor.kiosk_battery_soc_health` along the
bottom. All live values arrive as CLI params.

`push.sh` runs on the Pi from cron (`* * * * *`): reads HA over localhost with
the energy-tools token, renders with pixlet (v0.34.0, /usr/local/bin on the Pi
host), and pushes to the device as background installation `housebattery`.
`demo.sh` is the showcase tour (armed by `input_boolean.tidbyt_demo`).

## Tronbyt (since 2026-08-19)

The Tidbyt (Gen 1 — stock fw reported `tidbyt-v10`) runs **Tronbyt**
firmware and polls a local **tronbyt-server** instead of the Tidbyt cloud. The
stock firmware was heap-starved (27 KB low-watermark running BLE + MQTT/WSS)
and kept crashing; Tronbyt just HTTP-GETs a WebP.

- Server: `tronbyt-server` service in `/opt/stack/docker-compose.yml`
  (`ghcr.io/tronbyt/server:2`), UI at http://homeassistant.local:8000,
  data in `/opt/stack/tidbyt/server-data/` (sqlite + pushed webps + firmware
  releases). Device id `tidbyt`, type `tidbyt_gen1`. Owner login `robwil`,
  password in `/opt/stack/tidbyt/tronbyt_admin_pw` (600; single-user
  auto-login is on, so rarely needed).
- Push: `tronbyt_push.sh` (sourced by push.sh/demo.sh) curls the server's
  Tidbyt-compatible `POST /v0/devices/tidbyt/push` — stock pixlet's `push`
  hardcodes api.tidbyt.com. API key in `/opt/stack/tidbyt/tronbyt_key`
  (mode 600) — **never commit it**.
- Device: now in **websocket mode** — `ws://192.168.1.52:8000/tidbyt/ws`
  (instant pushes; switched over the air via the Firmware-settings
  `image_url`). Falls back to HTTP polling of `/tidbyt/next` if the URL is
  set back to http. Pi IP is static.
- **Colours: this panel needs `swap_colors` (R→B→G rotation).** It is
  stored in the device NVS and the device record (`swap_colors=1`), so
  firmware generation and OTA both pick the `tidbyt-gen1_swap` build.
  Gotcha: the NVS value wins over the build's compiled default, and the
  first boot persists `swap_colors=0` — OTA-ing the swap build alone does
  nothing; set the flag via Firmware settings (websocket mode only) or the
  AP portal, then reboot.
- Firmware updates are **OTA over wifi**: device page → firmware update
  (pick a release or upload an app-only `_firmware.bin`); the server hands
  the URL to the device and it reboots into the other OTA slot. USB
  (`esptool --chip esp32 --port /dev/cu.usbserial-8310 --baud 115200
  write-flash 0 <merged.bin>`, baud switching fails on this CP2102N) is only
  for recovery or a wifi-password change.
- Revert to stock: full 8 MB dump of the original flash (incl. NVS, i.e. wifi
  + device cert) lives on the Mac at `~/tidbyt-backup/` (secrets — not in
  git); generic stock image is tronbyt `firmware-esp32/reset/gen1_merged.bin`.

Deploy changes with:

    scp tidbyt/battery.star tidbyt/push.sh tidbyt/demo.sh tidbyt/tronbyt_push.sh \
        robwil@homeassistant.local:/opt/stack/tidbyt/
