#!/usr/bin/env bash
# Produce ONE set of energy figures for every display to render from.
#
# Why this exists: push.sh (Tidbyt) and tronbyt-wide/dash/push_wide.sh each used
# to query HA and derive the flows themselves. That went wrong twice in one day:
#   * the two grew different maths — the night-time battery derivation was fixed
#     in one and not the other, so the same sensors produced 1.7kW on one screen
#     and 1.5kW on the other;
#   * even with identical code they sample at different instants, and the wide
#     render takes 3s, so a fast-moving clamp reads differently on each screen.
# One producer, one derivation, one instant: the displays cannot disagree.
#
# Writes a shell-sourceable env file atomically to /dev/shm (tmpfs — no SD wear,
# writable by the user cron runs as, and empty after a reboot so nothing serves
# stale figures).
# Display-specific values (bins, weather, appliance list, easter eggs) stay in
# the individual pushers; only what is shown on more than one screen lives here.
set -euo pipefail

HA=http://localhost:8123
TOKEN=$(cat /opt/stack/energy_tools/data/.config/sen66/ha_token)
OUT=/dev/shm/tronbyt/snapshot.env

get() {
  curl -sf -m 10 -H "Authorization: Bearer $TOKEN" "$HA/api/states/$1" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])'
}

# Numeric fetch with fallback: a sensor reporting unavailable/unknown must
# degrade the display, not wedge it.
getn() {
  local v
  v=$(get "$1" 2>/dev/null)
  python3 -c "print(float('$v'))" 2>/dev/null || echo "$2"
}

# Numeric fetch, two sources: live modbus first, FoxESS cloud second. The RS485
# link (foxess_modbus, 10s polls) is new; the cloud sensors are the same figures
# minutes late, so they are the right degraded mode if the adapter goes away.
getn2() {
  local v
  v=$(get "$1" 2>/dev/null)
  python3 -c "print(float('$v'))" 2>/dev/null || getn "$2" "$3"
}

# AC flows come from the LOCAL Meross 18ch clamps (seconds cadence), not the
# FoxESS cloud: the cloud takes one instantaneous sample every ~2min, so a
# cycling load (oven thermostat) makes it flip between 0.5 and 3.6kW while the
# real average was 2.4kW. What only the inverter knows — SoC, the battery
# split, DC solar — now comes LIVE over RS485 (foxess_modbus, 10s polls); the
# cloud is the fallback for those if the link goes away.
SOC=$(getn2 sensor.battery_soc sensor.foxess_foxctl_battery_soc 0)
HOUSE_W=$(getn sensor.circuits_total_power NA)
GRID_W=$(getn sensor.grid_main_power_local NA)
INV_W=$(getn sensor.inverter_ac_power_local NA)
CHG=$(getn2 sensor.battery_charge sensor.foxess_foxctl_battery_charge_power 0)
DIS=$(getn2 sensor.battery_discharge sensor.foxess_foxctl_battery_discharge_power 0)
SOLAR_LIVE=$(getn2 sensor.solar_power_local sensor.foxess_foxctl_solar_power 0)
GRIDIN=$(getn2 sensor.grid_consumption sensor.foxess_foxctl_grid_import 0)
GRIDOUT=$(getn2 sensor.feed_in sensor.foxess_foxctl_grid_export 0)
LOAD_CLOUD=$(getn2 sensor.load_power sensor.foxess_foxctl_house_load 0)
COAST=$(getn sensor.battery_coast_margin 0)
KWH=$(getn sensor.battery_energy 0)
TNOW=$(get sensor.living_room_ac_outside 2>/dev/null || echo '?')
SUN_STATE=$(get sun.sun 2>/dev/null || echo below_horizon)

# RECONCILE, so the four published figures always satisfy
#     solar + discharge + import  ==  house + charge + export
# The displays draw a flow diagram from these numbers, so a set that does not
# balance cannot be drawn honestly: it shows a node carrying power with nothing
# attached to it, or a stub that dead-ends at the junction. The sources genuinely
# disagree — the clamps are seconds fresh, the FoxESS cloud is minutes behind —
# so rather than draw the disagreement we pin the identities that are physically
# exact and let the cloud decide only the one thing it alone knows.
#
# Two identities hold at every instant:
#     busbar:   house = invac + grid          (grid +import / -export)
#     inverter: invac = solar - batt          (batt +charging / -discharging)
# The clamps are three measurements of a two-degree-of-freedom system, so they
# over-determine it and disagree by ~40W. GRID IS THE ARBITER OF ITSELF: the ch1
# CT measures it directly, so the diagram draws grid flow only when that clamp
# sees it — deriving grid as house - invac invented 149 phantom import events in
# one night (worst 1.47kW) from sampling skew between two other clamps, while
# the real clamp saw exactly one (318W of battery ramp lag). Inverter stays
# measured too (its clamp feeds the battery figure at night); the ~40W
# disagreement lands on house, where it is invisible at one decimal place.
# The cloud is then used for ONE thing: how to split the inverter's output
# between solar and battery. Everything else follows by arithmetic.
# Blip suppression state: the raw grid clamp reading from the PREVIOUS tick.
# A real import event must hold for two consecutive ticks (~60s) before it is
# drawn; battery ramp lag lasts seconds and never shows. Lives in tmpfs beside
# the snapshot, so a reboot forgets it harmlessly.
GRID_PREV_FILE=/dev/shm/tronbyt/grid_prev
GRID_PREV=$(cat "$GRID_PREV_FILE" 2>/dev/null || echo 0)

read -r LOAD GRID NET SOLAR BAL BAL_ADJ GRID_RAW < <(python3 -c "
PV_MAX = 7.0                                # array peaks at 5.64kW; a stale
                                            # cloud figure must not exceed this
def w(v):
    return None if v == 'NA' else float(v) / 1000.0
chg, dis = float('$CHG'), float('$DIS')
batt_live = chg - dis                       # + charging / - discharging
house, grid_c, invac = w('$HOUSE_W'), w('$GRID_W'), w('$INV_W')
night = '$SUN_STATE' == 'below_horizon'

if house is None or invac is None:
    # Clamps down. The inverter's own trio came from one modbus poll, so it is
    # at least internally consistent; derive grid from it to close the balance.
    house, solar, batt = float('$LOAD_CLOUD'), float('$SOLAR_LIVE'), batt_live
    if night:
        solar, batt = 0.0, -(house - (float('$GRIDIN') - float('$GRIDOUT')))
    grid = house + batt - solar
    graw = grid
    adj = 0.0
else:
    # Grid from its own CT, gated twice: a deadband (under 100W the clamp is
    # reading meter offset, not flow) and two-tick persistence (a figure only
    # counts if the PREVIOUS tick also saw it, so a seconds-long battery-lag
    # blip cannot flicker the pylon; sustained flow appears one tick late,
    # which nobody can see).
    graw = grid_c if grid_c is not None else house - invac
    prev = float('$GRID_PREV')
    grid = graw
    if abs(graw) < 0.10:
        grid = 0.0
    elif abs(graw) < 0.50 and (abs(prev) < 0.10 or (graw > 0) != (prev > 0)):
        # Persistence applies ONLY to small readings — battery-lag blips are a
        # few hundred watts for a few seconds. A large reading is trusted
        # immediately: on 2026-08-30 the free-window charge ramped to 13.6kW
        # and the gate held grid at 0 for a tick, which forced the busbar
        # identity to publish house = invac = -7.3kW. A 13kW figure on a
        # direct, seconds-fresh CT is not a blip.
        grid = 0.0
    # House absorbs the clamp disagreement so the busbar identity holds
    # exactly. adj records how much it had to move — sustained growth here
    # means a CT is lying.
    house_meas = house
    house = invac + grid
    adj = house_meas - house
    if night:
        # After dark the inverter's AC output IS the battery. Nothing else can
        # be true, so do not let a stale cloud figure invent solar.
        solar = 0.0
    else:
        # Daytime: the inverter supplies the split, live over modbus (10s
        # polls — the cloud used to sit here, minutes behind, mis-attributing
        # ramps between solar and battery). Still bounded, and the battery is
        # still re-derived below so the SET stays exactly consistent.
        solar = min(max(invac + batt_live, 0.0), PV_MAX)
    batt = solar - invac                    # inverter identity, exact by fiat

# Proof the set balances; published so a drift shows up as a number, not a
# mystery on the screen.
err = (solar + max(-batt, 0.0) + max(grid, 0.0)) - (house + max(batt, 0.0) + max(-grid, 0.0))
# adj = how far the grid clamp had to move to close the busbar identity. Normally
# tens of watts (meter offset); a sustained large value means a CT is lying.
print(round(house, 2), round(grid, 2), round(batt, 2), round(solar, 2),
      round(err, 3), round(adj, 3), round(graw if 'graw' in dir() else 0.0, 3))
")
echo "$GRID_RAW" > "$GRID_PREV_FILE"

# Which source is carrying the house right now — drives the icon on both faces.
SRC=$(python3 -c "
solar, load, grid, batt = float('$SOLAR'), float('$LOAD'), float('$GRID'), float('$NET')
dis, imp = max(-batt, 0.0), max(grid, 0.0)
if solar > 0.05 and solar >= load:
    print('sun')
elif dis > 0.05 and dis >= imp:
    print('batt')
elif imp > 0.05:
    print('grid')
else:
    print('')
")

# Car SoC, dash-calibrated: the Fiat shows a higher figure than soc_real and the
# gap narrows as the pack fills, so raw/95.5 read ~3 points low near the top.
# Blank (not stale) once the reading is over 12h old — the car may have moved.
CAR=$(python3 - << 'PYEOF'
import json, urllib.request, datetime
tok = open('/opt/stack/energy_tools/data/.config/sen66/ha_token').read().strip()
try:
    req = urllib.request.Request('http://localhost:8123/api/states/sensor.wican_soc_real',
                                 headers={'Authorization': 'Bearer ' + tok})
    s = json.load(urllib.request.urlopen(req, timeout=10))
    changed = datetime.datetime.fromisoformat(s['last_changed'].replace('Z', '+00:00'))
    age_h = (datetime.datetime.now(datetime.timezone.utc) - changed).total_seconds() / 3600
    raw = float(s['state'])
    print(min(100, int(raw - (40 - raw) / 7 + 0.5)) if age_h <= 12 else '')
except Exception:
    print('')
PYEOF
)

# Car draw, kW — direct from the Ogemray plug (seconds fresh). The charger is
# on a clamped circuit, so this is a SUBSET of LOAD; the 5-node displays
# subtract it to get house-without-car. Zero when the switch is off or the
# draw is under 100W (the plug's own idle burn is not the car charging).
CH_SW=$(get switch.ogemray25a_70af09ed9950 2>/dev/null || echo off)
CH_W=$(getn sensor.ogemray25a_70af09ed9950_power 0)
CARKW=$(python3 -c "print(round(float('$CH_W') / 1000.0, 2) if '$CH_SW' == 'on' and float('$CH_W') > 100 else 0.0)")

# Is foxctl deliberately steering surplus into the car right now? Decides
# whether solar is attributed to the car FIRST on the flow diagrams. The
# api/state ev_divert field is a human-readable sentence; 'charger ON' at the
# front is its machine-readable part (fragile by design — if foxctl reworks
# the wording, this quietly reverts to house-first attribution, which is the
# safe default).
EVDIV=$(curl -sf -m 5 localhost:8770/api/state 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(1 if 'charger ON' in str(d.get('ev_divert') or '') else 0)" || echo 0)

# WAN failover: the fibre enters the GL.iNet X2000 (the Orbi's "external" IP
# is 192.168.8.x — X2000 LAN), which fails over to LTE. The house is on
# cellular exactly when NO wired uplink is Up and the modem holds an IPv4.
# Verified 2026-09-11: eth1/eth2/tethering all Down, egress IP 1.145.46.2
# (Telstra mobile). Unavailable sensors read as not-Up, so the cellular-IP
# check is what stops an integration outage from raising a false alarm.
E1=$(get sensor.gl_inet_x2000_ethernet_1_status 2>/dev/null || echo '?')
E2=$(get sensor.gl_inet_x2000_ethernet_2_status 2>/dev/null || echo '?')
TE=$(get sensor.gl_inet_x2000_tethering_status 2>/dev/null || echo '?')
CIP=$(get sensor.gl_inet_x2000_cellular_wan_ipv4 2>/dev/null || echo '')
WANFAIL=$(python3 -c "
wired_up = 'Up' in ('$E1', '$E2', '$TE')
print(1 if (not wired_up) and '$CIP'.count('.') == 3 else 0)")

mkdir -p "$(dirname "$OUT")"
TMP=$(mktemp "$OUT.XXXXXX")
cat > "$TMP" << EOF
# generated by snapshot.sh at $(date '+%Y-%m-%d %H:%M:%S') — do not edit
SNAP_TS=$(date +%s)
BAL=$BAL
BAL_ADJ=$BAL_ADJ
SOC=$SOC
LOAD=$LOAD
GRID=$GRID
NET=$NET
SOLAR=$SOLAR
SRC=$SRC
CAR=$CAR
CARKW=$CARKW
EVDIV=$EVDIV
WANFAIL=$WANFAIL
COAST=$COAST
KWH=$KWH
TNOW=$TNOW
SUN_STATE=$SUN_STATE
GRIDIN=$GRIDIN
GRIDOUT=$GRIDOUT
LOAD_CLOUD=$LOAD_CLOUD
EOF
mv -f "$TMP" "$OUT"          # atomic: a reader never sees a half-written file
