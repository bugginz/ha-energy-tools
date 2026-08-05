# Tidbyt — house battery

`battery.star` is a pure renderer (64x32): SoC big, kWh + charge/discharge in
the corner, SoC bar coloured by `sensor.kiosk_battery_soc_health` along the
bottom. All live values arrive as CLI params.

`push.sh` runs on the Pi from cron (`*/5`): reads HA over localhost with the
energy-tools token, renders with pixlet (v0.34.0, /usr/local/bin on the Pi
host), and pushes to the device as background installation `housebattery`.

The Tidbyt API key lives ONLY on the Pi at `/opt/stack/tidbyt/tidbyt_key`
(mode 600) — do not commit it. Deploy changes with:

    scp tidbyt/battery.star tidbyt/push.sh robwil@homeassistant.local:/opt/stack/tidbyt/
