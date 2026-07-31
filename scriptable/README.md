# Scriptable widgets

## FoxESS Battery.js

Lock Screen and Home Screen battery widget for the FoxESS/foxctl system.

**Install:** copy `FoxESS Battery.js` into the `Scriptable` folder in iCloud Drive
(Files → iCloud Drive → Scriptable), then open Scriptable — it appears as a
script named "FoxESS Battery".

**First run:** open the script inside the Scriptable app once and press Run. It
prompts for a Home Assistant long-lived access token (Profile → Security →
Long-lived access tokens) and stores it in the device keychain. Widgets cannot
show prompts, so this step has to happen in the app or the widget renders an
error.

**Add the widget:** long-press the Lock Screen → Customise → tap a widget slot →
Scriptable → choose the family, then edit the widget and set Script to "FoxESS
Battery". Home Screen small/medium work too and render in full colour.

### Design notes

- **One request per refresh.** It POSTs a Jinja template to `/api/template` and
  gets back ~130 bytes, rather than pulling `/api/states` (hundreds of entities)
  or making one call per sensor. The coast-to-10am health colour is computed by
  `sensor.kiosk_battery_soc_health` in HA, so the widget cannot drift from the
  dashboard's version of the same logic.
- **Tailscale URL by default.** `ha.tail78279c.ts.net` resolves both on the LAN
  and remotely on the tailnet; a LAN address would leave the widget stale
  whenever you left the house.
- **Stale-tolerant.** A failed fetch falls back to the last good reading from the
  cache, labelled with its age, instead of rendering an empty widget.
- **Lock Screen widgets render monochrome**, so the SoC colour only shows on the
  Home Screen. On the lock screen the gauge conveys level by shape.
- **Refresh cadence is iOS's decision** — the script asks for 10 minutes, but the
  system budget typically delivers something closer to 15–30.
