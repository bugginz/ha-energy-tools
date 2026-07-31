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


def tile_svg(label, accent, *, sub="", glyph="", pulse=None):
    """A stat tile: dark rounded panel, small-caps label, accent underline, room in the
    middle for the state-label overlay. `pulse` = 'up' | 'down' | None."""
    anim = ""
    if pulse:
        # Chevrons drifting up (charging) or down (discharging), staggered so the row reads
        # as motion rather than a blink.
        dirn = -1 if pulse == "up" else 1
        chevs = []
        for i in range(7):
            x = 34 + i * 39
            d = (f"M{x} 118 l9 -9 l9 9" if pulse == "up" else f"M{x} 109 l9 9 l9 -9")
            chevs.append(
                f"<path d='{d}' fill='none' stroke='{accent}' stroke-width='3.4' "
                f"stroke-linecap='round' stroke-linejoin='round' opacity='0' "
                f"style='animation:cv 1.9s ease-in-out {i * 0.13:.2f}s infinite'/>")
        anim = (f"<style>@keyframes cv{{0%{{opacity:0;transform:translateY({7 * -dirn}px)}}"
                f"45%{{opacity:.95;transform:translateY(0)}}"
                f"100%{{opacity:0;transform:translateY({7 * dirn}px)}}}}</style>"
                + "".join(chevs))
    glyph_el = (f"<text x='274' y='38' font-family='system-ui,sans-serif' font-size='26' "
                f"text-anchor='end' fill='{accent}' opacity='.9'>{glyph}</text>") if glyph else ""
    sub_el = (f"<text x='26' y='134' font-family='system-ui,sans-serif' font-size='15' "
              f"fill='{MUTED}'>{sub}</text>") if sub else ""
    return f"""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 300 150'>
<defs><linearGradient id='g' x1='0' y1='0' x2='0' y2='1'>
<stop offset='0' stop-color='{BG1}'/><stop offset='1' stop-color='{BG0}'/></linearGradient>
<linearGradient id='a' x1='0' y1='0' x2='1' y2='0'>
<stop offset='0' stop-color='{accent}' stop-opacity='.95'/>
<stop offset='1' stop-color='{accent}' stop-opacity='.15'/></linearGradient></defs>
<rect x='2' y='2' width='296' height='146' rx='18' fill='url(#g)' stroke='{accent}' stroke-opacity='.28'/>
<rect x='2' y='2' width='296' height='5' rx='2.5' fill='url(#a)'/>
<text x='26' y='40' font-family='system-ui,sans-serif' font-size='19' font-weight='600'
 letter-spacing='2.5' fill='{accent}'>{label}</text>
{glyph_el}{sub_el}{anim}</svg>"""


def soc_bar_svg(accent, pulse):
    """Full-width flow band under the SoC bar. The core tile draws the bar's true length;
    this carries the direction the bar itself cannot animate — a wave travelling across it
    plus chevrons pointing the way the charge is going."""
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
    word = "CHARGING" if up else "DISCHARGING"
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


def value(entity, *, attribute=None, suffix=None, size="min(58px, 5vw)", top="62%", color=INK):
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
    return el


# A section spanning 2 view columns exposes a 24-column grid, not 12 — so a third of a row
# is 8, not 4. Getting this wrong silently packs six tiles into one row and clips the values.
COLS_ROW = 24
THIRD = COLS_ROW // 3
HALF = COLS_ROW // 2


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
            ".": _CARD_CSS,
            "hui-card-features": {"$": {"hui-card-feature": {"$": {
                "hui-bar-gauge-card-feature": {"$": _BAR_CSS}}}}},
        }},
    }


def build(card_mod=False):
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

    controls = [
        {"type": "tile", "entity": "timer.wallpanel_presence_hold", "name": "Screensaver in",
         "color": "amber", "grid_options": {"columns": HALF, "rows": 2}},
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


if __name__ == "__main__":
    json.dump(build(card_mod="--card-mod" in sys.argv), sys.stdout, indent=1)
