# MQTT on this network — how devices get onboarded

Written after the Faikout (`iz_ac`) setup went smoothly while other projects
(e.g. the ESP32 e-paper display) have struggled. This is the whole recipe;
nothing else is required.

## The broker

Mosquitto runs as a **docker container on the Pi** (`/opt/stack` compose —
container name `mosquitto`), listening on **`homeassistant.local:1883`**.
It is NOT the HA add-on (this HA is Container, not HAOS).

- Auth: username/password, `password_file` at `/mosquitto/config/passwd`
  inside the container (`/opt/stack/mosquitto/config/passwd` on the host,
  root-owned — use `docker exec`, don't edit the host file).
- Anonymous connections are **refused** — every "can't connect" struggle so
  far has been a device trying to connect without credentials.
- Home Assistant's MQTT integration is already connected to this broker, so
  anything that publishes **MQTT discovery** (`homeassistant/...` topics)
  appears in HA automatically. No HA-side configuration per device.

## Convention

**One broker user per device, username = device hostname** (`ac_main`,
`iz_ac`, ...). Never share credentials between devices — revoking one
device then never breaks another.

## Onboarding a new device (the whole procedure)

1. **Mint credentials** (on the Pi; takes effect without restarting anything):

   ```sh
   PW=$(python3 -c "import secrets; print(secrets.token_urlsafe(16))")
   docker exec mosquitto mosquitto_passwd -b /mosquitto/config/passwd <hostname> "$PW"
   docker exec mosquitto sh -c 'kill -HUP 1'      # reload passwd file
   echo "$PW"                                      # configure into the device, then forget it
   ```

   (`mosquitto_passwd` prints warnings about file ownership — harmless.)

2. **Point the device at the broker**: host `homeassistant.local`, port
   `1883`, the username/password from step 1, TLS off. If the device's mDNS
   is unreliable, use the Pi's IP instead of `homeassistant.local`.

3. **Verify** — watch it authenticate (or fail) in the broker log:

   ```sh
   docker logs mosquitto --since 2m 2>&1 | grep -i <hostname>
   ```

   `New client connected ... u'<hostname>'` = done. `not authorised` = the
   password in the device doesn't match step 1. (If the log is silent even
   for working clients, connection logging is off — fall back to checking
   for the device's entities/topics in HA, or subscribe with
   `docker exec mosquitto mosquitto_sub -u <hostname> -P <pw> -t '#' -C 1`
   to at least prove the credentials.)

4. If the device does MQTT discovery, its entities appear in HA within
   seconds. If not, add manual `mqtt:` entities in HA against its topics.

The password lives only in the device's flash and the broker's hash file.
If it's ever lost: rerun step 1 with a new password and reconfigure the
device — nothing else cares.

## Worked example: Faikin/Faikout air-con (2026-09-19)

RevK-firmware devices are configured over HTTP; only *bare-named* fields are
saved (the form's underscore-prefixed names are display-only):

```sh
curl -X POST "http://iz_ac.local/revk-settings" \
  --data-urlencode "mqtthost=homeassistant.local" \
  --data-urlencode "mqttuser=iz_ac" \
  --data-urlencode "mqttpass=$PW" \
  --data-urlencode "haenable=on"
```

Settings pages if doing it by hand: `http://<dev>.local/revk-settings?_page=-1`
is the tab with hostname/MQTT/password (the tabs are `_page=0,-1,-2,-3`).
`haenable=on` turns on HA MQTT discovery — the climate entity plus the full
sensor/switch suite self-register.

## ESPHome devices (the e-paper display case)

ESPHome talks to HA via its **native API by default — it does not need MQTT
at all**. Only add `mqtt:` if something else must consume the data. If you
do want MQTT:

```yaml
mqtt:
  broker: homeassistant.local
  username: epaper          # minted via step 1
  password: !secret mqtt_password
  discovery: true
```

Caveats that have bitten:
- `api:` + `mqtt:` together is fine, but with both present HA may discover
  the device twice (once per transport). Prefer one or the other.
- ESPHome's default `birth_message`/`will_message` need no changes.
- If the log shows `Connecting to MQTT...` looping: it's auth (check the
  broker log per step 3), or the device can't resolve `.local` — use the IP.

## Existing broker users (as of 2026-09-19)

`robwil`, `ac_main` (living room AC), `ac_br1` (main bedroom AC), `iz_ac`
(Isobel's bedroom AC), `power_monitor`, `homeassistant`, `addons`,
`sensiron`, `tronbyt`, and `epaper`.

Note for the e-paper project specifically: **its broker user already
exists** — if the device can't connect, the stored password is wrong or
lost. Recovery is the standard one: re-mint the password for `epaper`
(step 1) and put the new one in the device. List current users:

```sh
docker exec mosquitto sh -c 'cut -d: -f1 /mosquitto/config/passwd'
```
