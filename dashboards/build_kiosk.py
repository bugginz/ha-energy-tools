#!/usr/bin/env python3
"""Generate the Kiosk V2 Lovelace dashboard (2026-07-31).

Why it is built this way
------------------------
The markdown card strips all HTML, so inline styles, raw <svg> and <img> are dead ends
(probed on the live instance: all three render blank). `picture-elements` with a data-URI
SVG *does* render, including CSS keyframe animation inside the SVG. So each tile is a
bespoke SVG — background, label, accent, animation — with a `state-label` overlaying the
live value. That gives full typographic control and needs no custom cards; the instance has
no card-mod or button-card.

Layout is explicit: every card carries `grid_options` with a fixed column span out of the
sections grid's 12, so tiles align by construction rather than by whatever width an iPad
happens to be. The old dashboard sized its picture-elements off an invisible 20:10 SVG,
which is why alignment depended on the device.

State-driven variation uses `visibility` conditions to swap between pre-rendered variants
(the same trick the old dashboard used for the SoC colour) — picture-elements images cannot
be templated.

    python3 dashboards/build_kiosk.py > dashboards/kiosk_v2.json
"""
import json
import sys
from urllib.parse import quote

# ---------------------------------------------------------------- palette ----
BG0, BG1 = "#171b21", "#232935"
INK = "#f1f5f9"
MUTED = "#94a3b8"
GREEN, AMBER, RED = "#22c55e", "#f59e0b", "#ef4444"
BLUE, CYAN, VIOLET = "#38bdf8", "#22d3ee", "#a78bfa"
ORANGE = "#fb923c"

# Tile artwork height, in the SVG's own units against a 300-wide viewBox. With rows:"auto"
# a picture-elements card takes its image's aspect ratio, so this IS the card height knob.
# Raised from 150 (2:1) on 2026-07-31 so the tiles read from across the room; 250 was too
# tall on the real iPad, so settled at 200. This is the one knob for tile height.
TILE_H = 200

EV_POWER = "sensor.foxess_foxctl_ev_charger_power"
EV_STATE = "sensor.foxess_foxctl_ev_charger_state"
GRID_IN = "sensor.foxess_foxctl_grid_import"
GRID_OUT = "sensor.foxess_foxctl_grid_export"
SOC = "sensor.foxess_foxctl_battery_soc"
LOAD = "sensor.foxess_foxctl_house_load"
SOLAR = "sensor.foxess_foxctl_solar_power"
CHG = "sensor.foxess_foxctl_battery_charge_power"
DIS = "sensor.foxess_foxctl_battery_discharge_power"
INSIDE = "sensor.timmerflotte_temp_hmd_sensor_temperature"
OUTSIDE = "sensor.living_room_ac_outside"
AC = "climate.living_room_ac_mqtt_hvac"
HEALTH = "sensor.kiosk_battery_soc_health"


def data_uri(svg):
    """SVG -> data URI. quote() is the ONLY encoding pass — write the SVG with plain '#'
    and '%' and let it escape them. Pre-escaping and then quoting double-encodes ('%' ->
    '%25' -> '%2525'), which silently breaks gradient refs and keyframe stops and collapses
    the card to zero height."""
    return "data:image/svg+xml," + quote(" ".join(svg.split()), safe="")


def tile_svg(label, accent, *, sub="", glyph="", pulse=None, h=None):
    """A stat tile: dark rounded panel, small-caps label, accent underline, room in the
    middle for the state-label overlay. `pulse` = 'up' | 'down' | None."""
    h = h or TILE_H
    anim = ""
    if pulse:
        # Chevrons drifting up (charging) or down (discharging), staggered so the row reads
        # as motion rather than a blink.
        dirn = -1 if pulse == "up" else 1
        chevs = []
        for i in range(7):
            x = 34 + i * 39
            cy = (h or TILE_H) - 46
            d = (f"M{x} {cy + 9} l9 -9 l9 9" if pulse == "up" else f"M{x} {cy} l9 9 l9 -9")
            chevs.append(
                f"<path d='{d}' fill='none' stroke='{accent}' stroke-width='3.4' "
                f"stroke-linecap='round' stroke-linejoin='round' opacity='0' "
                f"style='animation:cv 1.9s ease-in-out {i * 0.13:.2f}s infinite'/>")
        anim = (f"<style>@keyframes cv{{0%{{opacity:0;transform:translateY({7 * -dirn}px)}}"
                f"45%{{opacity:.95;transform:translateY(0)}}"
                f"100%{{opacity:0;transform:translateY({7 * dirn}px)}}}}</style>"
                + "".join(chevs))
    glyph_el = (f"<text x='274' y='42' font-family='system-ui,sans-serif' font-size='28' "
                f"text-anchor='end' fill='{accent}' opacity='.9'>{glyph}</text>") if glyph else ""
    sub_el = (f"<text x='26' y='{int(h * 0.76)}' font-family='system-ui,sans-serif' "
              f"font-size='16' fill='{MUTED}'>{sub}</text>") if sub else ""
    return f"""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 300 {h}'>
<defs><linearGradient id='g' x1='0' y1='0' x2='0' y2='1'>
<stop offset='0' stop-color='{BG1}'/><stop offset='1' stop-color='{BG0}'/></linearGradient>
<linearGradient id='a' x1='0' y1='0' x2='1' y2='0'>
<stop offset='0' stop-color='{accent}' stop-opacity='.95'/>
<stop offset='1' stop-color='{accent}' stop-opacity='.15'/></linearGradient></defs>
<rect x='2' y='2' width='296' height='{h - 4}' rx='18' fill='url(#g)' stroke='{accent}' stroke-opacity='.28'/>
<rect x='2' y='2' width='296' height='5' rx='2.5' fill='url(#a)'/>
<text x='26' y='44' font-family='system-ui,sans-serif' font-size='20' font-weight='600'
 letter-spacing='2.5' fill='{accent}'>{label}</text>
{glyph_el}{sub_el}{anim}</svg>"""


# The temp bar is one tall card instead of two square ones: stacking inside above outside on a
# single warm->cool strip makes which-is-which positional rather than something to read off an
# icon. Height tracks TILE_H so it lines up with two rows of power cards beside it.
TEMP_W = 160


def temp_bar_svg(setpoint=True, ac_accent=RED, ac_word="heating to", tile_h=None):
    h = int(round((tile_h or TILE_H) * 1.65))
    mid = h // 2
    seg = ""   # the A/C marker is positioned by card-mod, not drawn here
    return f"""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {TEMP_W} {h}'>
<defs><linearGradient id='g' x1='0' y1='0' x2='0' y2='1'>
<stop offset='0' stop-color='{BG1}'/><stop offset='1' stop-color='{BG0}'/></linearGradient>
<linearGradient id='s' x1='0' y1='0' x2='0' y2='1'>
<stop offset='0' stop-color='{ORANGE}'/><stop offset='.5' stop-color='{VIOLET}' stop-opacity='.6'/>
<stop offset='1' stop-color='{CYAN}'/></linearGradient></defs>
<rect x='2' y='2' width='{TEMP_W - 4}' height='{h - 4}' rx='18' fill='url(#g)'
 stroke='{VIOLET}' stroke-opacity='.25'/>
<rect x='12' y='16' width='9' height='{h - 32}' rx='4.5' fill='url(#s)'/>
<text x='30' y='84' font-family='system-ui,sans-serif' font-size='15' font-weight='600'
 letter-spacing='2' fill='{ORANGE}'>INSIDE</text>
{seg}
<text x='30' y='{h - 22}' font-family='system-ui,sans-serif' font-size='15' font-weight='600'
 letter-spacing='2' fill='{CYAN}'>OUTSIDE</text></svg>"""


def soc_bar_svg(accent, pulse, word=None):
    """The battery status band — now the only battery bar, the tile above it having been
    dropped as redundant. Colour carries meaning: green while charging or full, and while
    DISCHARGING the colour comes from the coast-to-10am health helper, so an amber or red
    band means the battery is not on track to reach the next free window. pulse=None draws
    it steady (full or idle); otherwise the wave and chevrons run with the current."""
    if pulse is None:
        return f"""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1040 86'>
<rect x='2' y='2' width='1036' height='82' rx='18' fill='{BG0}' stroke='{accent}'
 stroke-opacity='.45'/>
<rect x='24' y='12' width='992' height='62' rx='9' fill='{accent}' opacity='.28'/>
<text x='520' y='80' font-family='system-ui,sans-serif' font-size='15' font-weight='600'
 letter-spacing='4' text-anchor='middle' fill='{accent}' opacity='.85'>{word}</text></svg>"""
    up = pulse == "up"
    n = 24
    els = []
    for i in range(n):
        x = 24 + i * 42
        # Wave order reverses with direction, so the motion itself reads as charge/discharge.
        delay = (i if up else n - 1 - i) * 0.075
        els.append(
            f"<rect x='{x}' y='12' width='28' height='62' rx='9' fill='{accent}' opacity='.16' "
            f"style='animation:bp 1.8s ease-in-out {delay:.2f}s infinite'/>")
        d = (f"M{x + 6} 52 l8 -9 l8 9" if up else f"M{x + 6} 34 l8 9 l8 -9")
        els.append(
            f"<path d='{d}' fill='none' stroke='{BG0}' stroke-width='3' stroke-linecap='round' "
            f"stroke-linejoin='round' opacity='.55' "
            f"style='animation:bp 1.8s ease-in-out {delay:.2f}s infinite'/>")
    word = word or ("CHARGING" if up else "DISCHARGING")
    return f"""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1040 86'>
<style>@keyframes bp{{0%{{opacity:.12}}45%{{opacity:1}}100%{{opacity:.12}}}}</style>
<rect x='2' y='2' width='1036' height='82' rx='18' fill='{BG0}' stroke='{accent}' stroke-opacity='.35'/>
{''.join(els)}
<text x='520' y='80' font-family='system-ui,sans-serif' font-size='15' font-weight='600'
 letter-spacing='4' text-anchor='middle' fill='{accent}' opacity='.75'>{word}</text></svg>"""


def idle_bar_svg(accent):
    return f"""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1040 86'>
<rect x='2' y='2' width='1036' height='82' rx='18' fill='{BG0}' stroke='{accent}' stroke-opacity='.22'/>
<text x='520' y='50' font-family='system-ui,sans-serif' font-size='20' font-weight='600'
 letter-spacing='5' text-anchor='middle' fill='{MUTED}'>BATTERY IDLE</text></svg>"""


def value(entity, *, attribute=None, suffix=None, prefix=None,
          size="min(58px, 5vw)", top="52%", color=INK):
    """Live value overlaid on the tile artwork.

    A state-label already appends the entity's unit_of_measurement, so adding a suffix
    double-prints it and the text overflows the tile. Attributes carry no unit, so the A/C
    setpoint is the one place a suffix is wanted. The size is viewport-relative because the
    label is HTML: unlike the SVG beneath it, it does not scale with the card."""
    el = {"type": "state-label", "entity": entity,
          "style": {"top": top, "left": "50%", "font-size": size, "font-weight": "300",
                    "letter-spacing": "-1px", "white-space": "nowrap", "color": color}}
    if attribute:
        el["attribute"] = attribute
    if suffix:
        el["suffix"] = suffix
    if prefix:
        el["prefix"] = prefix
    return el


# A section spanning 2 view columns exposes a 24-column grid, not 12 — so a third of a row
# is 8, not 4. Getting this wrong silently packs six tiles into one row and clips the values.
COLS_ROW = 24
THIRD = COLS_ROW // 3
HALF = COLS_ROW // 2
QUARTER = 6          # V4: narrow left column for the vertical temperature bar
POWER = 9            # V4: 6 + 9 + 9 = 24, so the power cards tile 2x2 beside it
# Row spans must be EXPLICIT here. With rows:"auto" every card is one grid row tall, so the
# tall temperature card simply made that row tall and the 2x2 never formed beside it. A
# sections row is 56px with an 8px gap, so 6 rows (376px) is exactly two 3-row cards (184px)
# plus the gap between them, and the columns line up.
# A card spanning R rows is 64R-8 px tall, so two stacked 3-row cards (2*184 + 8 gap = 376)
# equal ONE 6-row card. The gap is already inside the arithmetic — adding one for it makes the
# temperature column overshoot by a row.
POWER_ROWS_DAY = 6
POWER_ROWS_EVE = 6
# With an explicit row span the card box is a fixed pixel height, so the artwork's aspect has
# to be drawn to match or the image letterboxes inside it. A row is 64px less an 8px gap, and
# a V4 power card is 9/24 of the section, so these track the row spans above.
# Tuned against the kiosk's real geometry, measured in the browser at the iPad's 1180px
# width: the section is 1032 wide, so a power card box is 382x184 and the temperature box
# 252x376. The artwork must be drawn to those aspects or it crops — at 190 the bottom of the
# temperature column was cut off and the tiles looked squat with their values pushed out.
#   TILE_H = 300 * box_h / box_w  ->  300 * 184/382 = 145   (day)
#                                     300 * 312/382 = 245   (evening, 5 rows)
# These are specific to a ~1180px-wide display. On a very different screen they want redoing.
# 6 rows -> a 382x376 power box on the kiosk, so TILE_H = 300 * 376/382 = 295. Two such rows
# (760px) plus the band and padding overflow a ~820px screen, which is the point: the Edit
# card is pushed below the fold rather than sitting in valuable real estate.
TILE_H_DAY = 295
TILE_H_EVE = 295


def temp_rows(power_rows):
    return power_rows * 2


# Distance-adaptive: walking past in daylight wants density; from the couch after dark wants
# fewer, larger things. sun.sun is the only zero-hardware trigger available — there is no
# living-room presence sensor yet. Swapping whole card sets is the only way to change size,
# since a picture-elements image cannot be templated.
DAY = [{"condition": "state", "entity": "sun.sun", "state": "above_horizon"}]
EVENING = [{"condition": "state", "entity": "sun.sun", "state": "below_horizon"}]
CARD_MOD = False


def pe(svg, elements, columns, rows="auto"):
    """rows defaults to auto: a picture-elements card keeps its image's aspect ratio, so a
    fixed row count leaves dead space under the artwork."""
    return {"type": "picture-elements", "image": data_uri(svg), "elements": elements,
            "grid_options": {"columns": columns, "rows": rows}}


def vis_state(entity, state):
    return [{"condition": "state", "entity": entity, "state": state}]


def vis_above(entity, above):
    return [{"condition": "numeric_state", "entity": entity, "above": above}]


def vis_band(entity, above, below):
    c = {"condition": "numeric_state", "entity": entity}
    if above is not None:
        c["above"] = above
    if below is not None:
        c["below"] = below
    return [c]


# House load colour thresholds (kW), per the 2026-07-31 request: orange over 2, red over 4.
LOAD_WARN_KW, LOAD_ALERT_KW = 2, 4

FULL_SOC = 98.5
CHARGING = [{"condition": "numeric_state", "entity": CHG, "above": 0.05}]
DISCHARGING = [{"condition": "numeric_state", "entity": DIS, "above": 0.05}]
IDLE = [{"condition": "numeric_state", "entity": CHG, "below": 0.05},
        {"condition": "numeric_state", "entity": DIS, "below": 0.05}]


# ---------------------------------------------------------------- card-mod ----
# v3 only. Shadow path confirmed by inspecting the live DOM:
#   hui-tile-card $ ha-card > ha-tile-container > hui-card-features
#     $ hui-card-feature $ hui-bar-gauge-card-feature $ div:first-of-type
# That first unclassed div IS the fill (its inline width is the percentage); the sibling
# .bar-gauge-background is the track. --tile-color drives the fill colour, so templating it
# on ha-card collapses v2's three colour-variant cards into one.
CHARGING_J = "states('" + CHG + "')|float(0) > 0.05"
DISCHARGING_J = "states('" + DIS + "')|float(0) > 0.05"
SOC_J = "states('" + SOC + "')|float(0)"

_BAR_CSS = (
    "@keyframes socSweep{0%{background-position:-140% 0}100%{background-position:240% 0}}"
    "@keyframes socGlow{0%,100%{filter:brightness(1)}50%{filter:brightness(1.45)}}"
    "div:first-of-type{"
    "background-image:linear-gradient(90deg,rgba(255,255,255,0) 0%,"
    "rgba(255,255,255,.65) 50%,rgba(255,255,255,0) 100%);"
    "background-size:38% 100%;background-repeat:no-repeat;"
    "box-shadow:0 0 16px var(--tile-color);"
    # The sweep runs with the charge: left-to-right as the bar fills, reversed as it drains.
    "{% if " + CHARGING_J + " %}"
    "animation:socSweep 1.9s linear infinite, socGlow 1.9s ease-in-out infinite;"
    "{% elif " + DISCHARGING_J + " %}"
    "animation:socSweep 1.9s linear infinite reverse, socGlow 1.9s ease-in-out infinite;"
    "{% else %}animation:none;box-shadow:none;{% endif %}"
    "}"
)

# !important is required: HA sets --tile-color inline on ha-card, and an inline declaration
# beats a stylesheet rule. Without it the template is silently ignored and the bar keeps HA's
# stock green (verified in the live DOM). --feature-height is NOT settable this way — the bar
# div carries its own height — so the bar stays at HA's standard 42px.
_CARD_CSS = (
    "ha-card{"
    "--tile-color:{% if " + SOC_J + " < 30 %}" + RED +
    "{% elif " + SOC_J + " < 60 %}" + AMBER + "{% else %}" + GREEN + "{% endif %} !important;"
    "}"
)


def soc_tile_cardmod():
    """One SoC tile whose bar is coloured by level and animated by charge direction."""
    return {
        "type": "tile", "entity": SOC, "name": "Battery",
        "features": [{"type": "bar-gauge"}],
        "grid_options": {"rows": "auto", "columns": "full"},
        "card_mod": {"style": {
            # The icon and "Battery 100%" caption duplicate the BATTERY card below, so the
            # tile is reduced to the bar itself.
            ".": _CARD_CSS + "ha-tile-icon,ha-tile-info{display:none!important;}"
                             "ha-tile-container{padding:6px 12px!important;}",
            "hui-card-features": {"$": {"hui-card-feature": {"$": {
                "hui-bar-gauge-card-feature": {"$": _BAR_CSS}}}}},
        }},
    }


def build(card_mod=False, v4=False):
    cards = []

    # --- SoC bar ---
    if card_mod:
        # v3: the bar itself carries the colour and the motion, so the flow band below is
        # redundant decoration rather than the only way to show direction.
        cards.append(soc_tile_cardmod())
    else:
        # v2: no card-mod, so colour needs one pre-coloured card per band.
        for colour, health in (("green", "green"), ("orange", "orange"), ("red", "red")):
            cards.append({
                "type": "tile", "entity": SOC, "color": colour, "name": "Battery",
                "features": [{"type": "bar-gauge"}],
                "grid_options": {"rows": "auto", "columns": "full"},
                "visibility": vis_state(HEALTH, health)})

    # --- direction banner: pulses up while charging, down while discharging ---
    cards.append({**pe(soc_bar_svg(GREEN, "up"), [], "full"), "visibility": CHARGING})
    cards.append({**pe(soc_bar_svg(AMBER, "down"), [], "full"), "visibility": DISCHARGING})
    cards.append({**pe(idle_bar_svg(MUTED), [], "full"), "visibility": IDLE})

    # --- row: battery / house load / solar ---
    cards.append({**pe(tile_svg("BATTERY", GREEN, sub="charging", glyph="▲", pulse="up"),
                       [value(SOC)], THIRD), "visibility": CHARGING})
    cards.append({**pe(tile_svg("BATTERY", AMBER, sub="discharging", glyph="▼", pulse="down"),
                       [value(SOC)], THIRD), "visibility": DISCHARGING})
    cards.append({**pe(tile_svg("BATTERY", MUTED, sub="idle"), [value(SOC)], THIRD),
                  "visibility": IDLE})

    # House load bands: green under 2 kW, orange 2-4, red above 4.
    for accent, sub, band in ((GREEN, "normal", vis_band(LOAD, None, LOAD_WARN_KW)),
                              (ORANGE, "high", vis_band(LOAD, LOAD_WARN_KW, LOAD_ALERT_KW)),
                              (RED, "very high", vis_above(LOAD, LOAD_ALERT_KW))):
        cards.append({**pe(tile_svg("HOUSE LOAD", accent, sub=sub, glyph="⌂"),
                           [value(LOAD)], THIRD), "visibility": band})

    cards.append(pe(tile_svg("SOLAR", VIOLET, sub="generating now", glyph="☀"),
                    [value(SOLAR)], THIRD))

    # --- row: inside / outside / AC setpoint ---
    # Labelled in the artwork itself, so which reading is which is unambiguous.
    cards.append(pe(tile_svg("INSIDE", ORANGE, sub="living room", glyph="⌂"),
                    [value(INSIDE)], THIRD))
    cards.append(pe(tile_svg("OUTSIDE", CYAN, sub="ambient", glyph="❄"),
                    [value(OUTSIDE)], THIRD))

    for accent, sub, glyph, state in ((RED, "heating to", "▲", "heat"),
                                      (BLUE, "cooling to", "▼", "cool"),
                                      (VIOLET, "auto", "◆", "heat_cool")):
        cards.append({**pe(tile_svg("A/C SET", accent, sub=sub, glyph=glyph),
                           [value(AC, attribute="temperature", suffix="°")], THIRD),
                      "visibility": vis_state(AC, state)})
    cards.append({**pe(tile_svg("A/C", MUTED, sub="off"), [], THIRD),
                  "visibility": vis_state(AC, "off")})

    if v4:
        cards = (build_v4_cards(POWER_ROWS_DAY, mode=DAY)
                 + build_v4_cards(POWER_ROWS_EVE, vsize="min(96px, 8vw)",
                                  tsize="min(64px, 5.4vw)", show_bar=False, mode=EVENING,
                                  th=TILE_H_EVE))

    controls = [
        {"type": "tile", "entity": "input_boolean.hide_header", "name": "Edit",
         "icon": "mdi:pencil", "color": "grey", "hide_state": True,
         "tap_action": {"action": "toggle"}, "grid_options": {"columns": HALF, "rows": 2}},
    ]

    return {
        "wallpanel": {"enabled": True, "hide_toolbar": False, "hide_sidebar": False,
                      "fullscreen": False, "idle_time": 10,
                      "screensaver_entity": "input_boolean.wallpanel_screensaver",
                      "profile_entity": "input_select.wallpanel_profile",
                      "profiles": {"hold": {"idle_time": 0}}},
        "kiosk_mode": {"hide_header": '{{ is_state("input_boolean.hide_header", "on") }}',
                       "hide_sidebar": '{{ is_state("input_boolean.hide_header", "on") }}'},
        "views": [{"title": "Home", "type": "sections", "max_columns": 2,
                   "sections": [{"type": "grid", "column_span": 2, "cards": cards},
                                {"type": "grid", "column_span": 2, "cards": controls}]}],
    }


INSIDE_J = "states('" + INSIDE + "')|float(-99)"
OUTSIDE_J = "states('" + OUTSIDE + "')|float(-99)"
SETPOINT_J = "state_attr('" + AC + "','temperature')|float(-99)"


def ac_marker_css(accent):
    """Order the A/C setpoint against the readings instead of pinning it mid-bar: above the
    inside temperature when it is set higher, below the outside one when set lower, between
    them otherwise. The bar then reads top-to-bottom as warmest-to-coolest."""
    return ("hui-state-label-element:nth-of-type(3){"
            "top:{% if " + SETPOINT_J + " > " + INSIDE_J + " %}9%"
            "{% elif " + SETPOINT_J + " > " + OUTSIDE_J + " %}55%"
            "{% else %}88%{% endif %} !important;"
            f"border-top:2px dashed {accent};padding-top:5px;"
            "}")


def battery_band_cards():
    """Charging, discharging (coloured by coast health), full and idle."""
    still = [{"condition": "numeric_state", "entity": CHG, "below": 0.05},
             {"condition": "numeric_state", "entity": DIS, "below": 0.05}]
    full = {"condition": "numeric_state", "entity": SOC, "above": FULL_SOC}
    not_full = {"condition": "numeric_state", "entity": SOC, "below": FULL_SOC}
    # Mutually exclusive: full means full AND at rest, so a battery sitting at 100% while
    # trickling out reads DISCHARGING rather than showing two bands at once.
    out = [{**pe(soc_bar_svg(GREEN, None, "BATTERY FULL"), [], "full"),
            "visibility": still + [full]},
           {**pe(soc_bar_svg(GREEN, "up"), [], "full"), "visibility": CHARGING}]
    for colour, health in ((GREEN, "green"), (AMBER, "orange"), (RED, "red")):
        out.append({**pe(soc_bar_svg(colour, "down"), [], "full"),
                    "visibility": DISCHARGING + vis_state(HEALTH, health)})
    out.append({**pe(idle_bar_svg(MUTED), [], "full"), "visibility": still + [not_full]})
    # Car readout rides on every band variant, shown only while the charger is actually
    # pulling — the Meross relay can be on with the car full or unplugged.
    car = {"type": "conditional",
           "conditions": [{"condition": "state", "entity": EV_STATE, "state": "on"},
                          {"condition": "numeric_state", "entity": EV_POWER, "above": 0.3}],
           "elements": [{"type": "state-label", "entity": EV_POWER, "prefix": "\U0001F697 ",
                         "style": {"top": "50%", "left": "88%", "font-size": "min(22px, 1.9vw)",
                                   "font-weight": "400", "white-space": "nowrap",
                                   "color": CYAN}}]}
    soc_lbl = {"type": "state-label", "entity": SOC,
               "style": {"top": "50%", "left": "8%", "font-size": "min(30px, 2.6vw)",
                         "font-weight": "300", "white-space": "nowrap", "color": INK}}
    for c in out:
        c["elements"] = [soc_lbl, car]
    return out


def build_v4_cards(rows=POWER_ROWS_DAY, vsize="min(76px, 6.4vw)",
                   tsize="min(52px, 4.4vw)", show_bar=True, mode=None, th=TILE_H_DAY):
    """Temperature column on the left, the four halves of the energy equation on the right.

    Solar in, battery store, house draw and grid exchange are one system, so they get equal
    weight in a 2x2 block. The A/C setpoint moves onto the temperature bar as a marker line
    rather than occupying a tile of its own.
    """
    cards = list(battery_band_cards())

    # Temperature column: inside on top, outside at the foot, A/C setpoint as the line between.
    def temp_card(setpoint, accent, word, vis):
        els = [value(INSIDE, size=tsize, top="33%"), value(OUTSIDE, size=tsize, top="77%")]
        if setpoint:
            els.append(value(AC, attribute="temperature", suffix="°", prefix="A/C ",
                             size="min(26px, 2.2vw)", top="55%", color=accent))
        c = pe(temp_bar_svg(setpoint, accent, word, th), els, QUARTER, temp_rows(rows))
        if setpoint:
            c["card_mod"] = {"style": ac_marker_css(accent)}
        return {**c, "visibility": vis} if vis else c

    for accent, word, state in ((RED, "heating to", "heat"), (BLUE, "cooling to", "cool"),
                                (VIOLET, "auto", "heat_cool")):
        cards.append(temp_card(True, accent, word, vis_state(AC, state)))
    cards.append(temp_card(False, MUTED, "off", vis_state(AC, "off")))

    # Solar / battery / load / grid, 2x2.
    cards.append(pe(tile_svg("SOLAR", VIOLET, sub="generating now", glyph="☀", h=th),
                    [value(SOLAR, size=vsize)], POWER, rows))
    cards.append({**pe(tile_svg("BATTERY", GREEN, sub="charging", glyph="▲", pulse="up", h=th),
                       [value(SOC, size=vsize)], POWER, rows), "visibility": CHARGING})
    cards.append({**pe(tile_svg("BATTERY", AMBER, sub="discharging", glyph="▼", pulse="down", h=th),
                       [value(SOC, size=vsize)], POWER, rows), "visibility": DISCHARGING})
    cards.append({**pe(tile_svg("BATTERY", MUTED, sub="idle", h=th), [value(SOC, size=vsize)], POWER, rows),
                  "visibility": IDLE})
    for accent, sub, band in ((GREEN, "normal", vis_band(LOAD, None, LOAD_WARN_KW)),
                              (ORANGE, "high", vis_band(LOAD, LOAD_WARN_KW, LOAD_ALERT_KW)),
                              (RED, "very high", vis_above(LOAD, LOAD_ALERT_KW))):
        cards.append({**pe(tile_svg("HOUSE LOAD", accent, sub=sub, glyph="⌂", h=th),
                           [value(LOAD, size=vsize)], POWER, rows), "visibility": band})
    # Grid: exporting is a different event from importing, so it gets its own colour and word.
    cards.append({**pe(tile_svg("GRID", CYAN, sub="exporting", glyph="↑", h=th),
                       [value(GRID_OUT, size=vsize)], POWER, rows),
                  "visibility": vis_above(GRID_OUT, 0.05)})
    cards.append({**pe(tile_svg("GRID", AMBER, sub="importing", glyph="↓", h=th),
                       [value(GRID_IN, size=vsize)], POWER, rows),
                  "visibility": [{"condition": "numeric_state", "entity": GRID_OUT, "below": 0.05}]})
    if mode:
        # visibility is a list, evaluated as AND — appending the mode gate keeps each card's
        # own condition intact.
        for c in cards:
            c["visibility"] = list(c.get("visibility") or []) + mode
    return cards


if __name__ == "__main__":
    CARD_MOD = "--card-mod" in sys.argv
    json.dump(build(card_mod=CARD_MOD, v4="--v4" in sys.argv), sys.stdout, indent=1)
