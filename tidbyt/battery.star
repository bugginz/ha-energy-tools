# House battery for a Tidbyt (64x32).
#
# Pure renderer: all live values arrive as CLI params from push.sh, so this app
# holds no URLs or tokens and renders the same anywhere. Kiosk palette.
#
#   pixlet render battery.star soc=64 kwh=26.1 net_kw=-1.9 health=green \
#       coast=20 t_now=8 t_min=2 t_max=17

load("render.star", "render")

def _px(x, w, color, y = 0, h = 5):
    return render.Padding(pad = (x, y, 0, 0), child = render.Box(width = w, height = h, color = color))


def _chevron_c(x, left, col):
    """3-wide chevron drawn pixel by pixel; points the way it moves."""
    a, b, c = (x + 2, x + 1, x) if left else (x, x + 1, x + 2)
    return [_px(a, 1, col, 0, 1), _px(a, 1, col, 4, 1),
            _px(b, 1, col, 1, 1), _px(b, 1, col, 3, 1),
            _px(c, 1, col, 2, 1)]


HEXD = "0123456789abcdef"


def _hex2(v):
    v = min(max(int(v), 0), 255)
    return HEXD[v // 16] + HEXD[v % 16]


def _lighten(hexcol, f):
    """Blend a #rrggbb colour toward white by f (0..1)."""
    r = int(hexcol[1:3], 16)
    g = int(hexcol[3:5], 16)
    b = int(hexcol[5:7], 16)
    return "#" + _hex2(r + (255 - r) * f) + _hex2(g + (255 - g) * f) + _hex2(b + (255 - b) * f)


def flow_delay(net):
    """Frame delay from draw rate: ~0.5kW gentle (~190ms), 2kW busy (~95ms),
    5kW+ violent (~45ms)."""
    if abs(net) <= 0.05:
        return 100
    return min(max(int(280 / (1 + abs(net))), 45), 280)


def soc_bar(fill, col, net, style, bw = 64):
    """The SoC bar. Animation travels in the direction of the current — right
    while charging, left while discharging, still when idle. Rate drives the
    look: frame delay (see flow_delay), chevron contrast, tip brightness and a
    fill lightening all scale with |net| up to 5kW.
      sweep   — baseline: soft light band gliding along the fill
      chevtip — chevrons marching + a glow at the leading edge"""
    t = min(abs(net) / 5.0, 1.0)
    hot = _lighten(col, t * 0.25)
    base = [
        render.Box(width = bw, height = 5, color = "#232935"),
        render.Box(width = fill, height = 5, color = hot),
    ]
    if abs(net) <= 0.05:
        return render.Stack(children = [
            render.Box(width = bw, height = 5, color = "#232935"),
            render.Box(width = fill, height = 5, color = col),
        ])
    up = net > 0
    frames = []
    if style == "chevtip":
        period = 8
        chev = "#171b21" + _hex2(0x77 + int(t * 0x88))
        peak = 0x50 + int(t * 0xa8)
        tip_alpha = [_hex2(peak * k // 4) for k in [0, 1, 2, 3, 4, 3, 2, 1]]
        for i in range(period):
            off = i if up else period - 1 - i
            marks = []
            for s in range(off, fill - 3, period):
                marks += _chevron_c(s, not up, chev)
            marks.append(_px(max(fill - 3, 0), 3, "#ffffff" + tip_alpha[i]))
            frames.append(render.Stack(children = base + marks))
        return render.Animation(children = frames)
    n = 12
    sweep_w = 7
    travel = max(fill - sweep_w, 1)
    for i in range(n):
        pos = i if up else n - 1 - i
        x = int(pos * travel / (n - 1))
        frames.append(render.Stack(children = base + [_px(x, sweep_w, "#ffffff55")]))
    return render.Animation(children = frames)

def _p(x, y, w, h, c):
    return render.Padding(pad = (x, y, 0, 0), child = render.Box(width = w, height = h, color = c))


def weather_icon(cond):
    """7x6 pixel-art current-condition icon; empty box for unknown conditions."""
    YEL, GRY, DGR = "#fcd34d", "#94a3b8", "#64748b"
    BLU, WHT, PAL = "#38bdf8", "#e2e8f0", "#cbd5e1"
    cloud = [_p(1, 1, 4, 1, GRY), _p(0, 2, 6, 2, GRY)]
    art = []
    if cond == "sunny":
        art = [_p(2, 1, 3, 3, YEL), _p(3, 0, 1, 1, YEL), _p(3, 4, 1, 1, YEL),
               _p(0, 2, 1, 1, YEL), _p(6, 2, 1, 1, YEL)]
    elif cond == "clear-night":
        art = [_p(2, 1, 3, 3, PAL), _p(3, 1, 2, 2, "#171b21"), _p(6, 0, 1, 1, WHT)]
    elif cond == "partlycloudy":
        art = [_p(1, 0, 2, 2, YEL), _p(3, 2, 3, 1, GRY), _p(2, 3, 5, 2, GRY)]
    elif cond in ("cloudy", "windy", "windy-variant"):
        art = cloud
    elif cond in ("rainy", "pouring", "hail"):
        art = cloud + [_p(1, 5, 1, 1, BLU), _p(3, 5, 1, 1, BLU), _p(5, 5, 1, 1, BLU)]
    elif cond in ("lightning", "lightning-rainy"):
        art = cloud + [_p(3, 4, 1, 1, YEL), _p(2, 5, 1, 1, YEL)]
    elif cond in ("snowy", "snowy-rainy"):
        art = cloud + [_p(1, 5, 1, 1, WHT), _p(3, 5, 1, 1, WHT), _p(5, 5, 1, 1, WHT)]
    elif cond == "fog":
        art = [_p(0, 1, 6, 1, DGR), _p(1, 3, 5, 1, DGR), _p(0, 5, 6, 1, DGR)]
    if not art:
        return render.Box(width = 0, height = 0)
    return render.Stack(children = [render.Box(width = 7, height = 6, color = "#00000000")] + art)


BIN_COLORS = {"R": "#ef4444", "Y": "#fcd34d", "G": "#22c55e"}


def bins_icons(letters):
    """Mini wheelie-bins for the bottom-right corner: R waste, Y recycling,
    G organic. 3x6 each: handle, body, wheels."""
    kids = []
    for i in range(len(letters)):
        c = BIN_COLORS.get(letters[i], "#94a3b8")
        if i:
            kids.append(render.Box(width = 1, height = 1))
        kids.append(render.Stack(children = [
            render.Box(width = 3, height = 6, color = "#00000000"),
            _p(1, 0, 1, 1, c),
            _p(0, 1, 3, 4, c),
            _p(0, 5, 1, 1, "#94a3b8"),
            _p(2, 5, 1, 1, "#94a3b8"),
        ]))
    row = render.Row(cross_align = "end", children = kids)
    # Slow blink while the reminder is up: 10 frames on, 6 off; the transparent
    # placeholder keeps the header row from reflowing on off frames.
    w = 4 * len(letters) - 1
    blank = render.Box(width = w, height = 6, color = "#00000000")
    return render.Animation(children = [row] * 10 + [blank] * 6)


def src_icon(kind):
    """7x6 'house is fed by' icon: sun (solar), violet pylon (grid), amber
    battery pack (battery)."""
    if kind == "sun":
        return weather_icon("sunny")
    if kind == "grid":
        V = "#a78bfa"
        return render.Stack(children = [
            render.Box(width = 7, height = 6, color = "#00000000"),
            _p(0, 1, 7, 1, V), _p(3, 0, 1, 5, V), _p(1, 3, 5, 1, V),
            _p(1, 5, 1, 1, V), _p(5, 5, 1, 1, V),
        ])
    if kind == "batt":
        A = "#f59e0b"
        return render.Stack(children = [
            render.Box(width = 7, height = 6, color = "#00000000"),
            _p(3, 0, 2, 1, A),          # terminal nub on top
            _p(2, 1, 4, 5, A),          # upright body
        ])
    return render.Box(width = 0, height = 0)


def coaster_icon(cart_col):
    """7x6 rollercoaster: a track hump with the cart at the crest, cart coloured
    by coast health. Separator between kWh and the coast margin."""
    track = "#64748b"
    pts = [(0, 5), (1, 4), (2, 3), (3, 2), (4, 3), (5, 4), (6, 5)]
    return render.Stack(children = [render.Box(width = 7, height = 6, color = "#00000000")] +
                        [_p(x, y, 1, 1, track) for x, y in pts] +
                        [_p(2, 1, 2, 1, cart_col)])


def car_icon():
    """7x5 side-view car: cabin, body, wheels."""
    c = "#22d3ee"
    return render.Stack(children = [
        render.Box(width = 7, height = 5, color = "#00000000"),
        _p(1, 0, 4, 2, c),
        _p(0, 2, 7, 2, c),
        _p(1, 4, 1, 1, "#64748b"),
        _p(5, 4, 1, 1, "#64748b"),
    ])


def house_icon():
    """7x6 house: roof + body, for the 'house is drawing' figure when the
    battery itself is idle."""
    W = "#e2e8f0"
    return render.Stack(children = [
        render.Box(width = 7, height = 6, color = "#00000000"),
        _p(3, 0, 1, 1, W), _p(2, 1, 3, 1, W), _p(1, 2, 5, 1, W),
        _p(2, 3, 4, 3, W), _p(3, 4, 1, 2, "#171b21"),
    ])


def flow_arrow(up, color):
    """5x3 pixel triangle: point up while charging, down while discharging."""
    rows = [1, 3, 5] if up else [5, 3, 1]
    return render.Column(
        cross_align = "center",
        children = [render.Box(width = w, height = 1, color = color) for w in rows],
    )

INK = "#f1f5f9"
MUTED = "#94a3b8"
BG = "#232935"
GREEN, AMBER, RED = "#22c55e", "#f59e0b", "#ef4444"
CYAN, ORANGE, BLUE = "#38bdf8", "#fb923c", "#38bdf8"
HEALTH = {"green": GREEN, "orange": AMBER, "red": RED}

def main(config):
    soc = int(float(config.str("soc", "0")) + 0.5)
    kwh = config.str("kwh", "?")
    net = float(config.str("net_kw", "0"))  # + charging, - discharging
    col = HEALTH.get(config.str("health", "green"), GREEN)
    coast = int(float(config.str("coast", "0")) + 0.5)
    t_now = config.str("t_now", "?")
    t_min = config.str("t_min", "?")
    t_max = config.str("t_max", "?")

    # No words (Rob 2026-08-05): arrow gives direction, number gives rate.
    # Rate colour (Rob 2026-08-09): charging keeps green; discharge grades
    # blue < 0.4kW, orange < 2kW, red >= 2kW.
    kw1 = str(int(abs(net) * 10 + 0.5) / 10.0)
    if net > 0.05:
        word, wcol, up = kw1 + "kW", GREEN, True
    elif net < -0.05:
        rate = abs(net)
        wcol = BLUE if rate < 0.4 else (ORANGE if rate < 2.0 else RED)
        word, up = kw1 + "kW", False
    else:
        word, wcol, up = "", MUTED, None

    # Battery idle: the slot shows the HOUSE draw instead (with a house glyph),
    # so a full battery floating on solar still tells you what the house pulls.
    hload = float(config.str("load", "0"))
    house = False
    if up == None and hload > 0.05:
        word = str(int(hload * 10 + 0.5) / 10.0) + "kW"
        wcol = INK
        house = True

    # Coast margin to the 10:00 free window, same bands as the kiosk COAST tile.
    ccol = GREEN if coast >= 10 else (AMBER if coast >= 0 else RED)
    ctxt = ("+" if coast >= 0 else "") + str(coast)

    bins = config.str("bins", "")
    car = config.str("car", "")
    cond_n = config.str("cond_n", "")
    cond_t = config.str("cond_t", "")
    source = config.str("src", "")
    fill = max(1, min(64, int(soc * 64 / 100 + 0.5)))

    def at(x, y, child):
        return render.Padding(pad = (x, y, 0, 0), child = child)

    def right_at(y, kids):
        # +1px right margin: tom-thumb's W overhangs its advance and clips at
        # the display edge otherwise (seen on "0.4kW" at 100%%).
        return at(0, y, render.Row(expanded = True, main_align = "end",
                                   cross_align = "center",
                                   children = kids + [render.Box(width = 1, height = 1)]))

    # Absolute layout on a 64x32 canvas. Decluttered per Rob 2026-08-09: no kWh
    # line, coast lives in the bar's empty end, weather high, breathing room low.
    els = [render.Box(width = 64, height = 32, color = "#00000000")]

    # Big number: health-coloured (green when on track) and double-struck for
    # weight — 10x20 is the largest built-in font, so bold is the "bigger".
    # Digit ink cropped to the very top; % and the bin reminder ride beside it.
    num = render.Stack(children = [
        render.Text("%d" % soc, font = "10x20", color = col),
        at(1, 0, render.Text("%d" % soc, font = "10x20", color = col)),
    ])
    header = [
        render.Padding(pad = (0, -2, 0, -3), child = num),
        render.Text("%", font = "tom-thumb", color = MUTED),
    ]
    if bins:
        header += [render.Box(width = 2, height = 1), bins_icons(bins)]
    els.append(at(0, 0, render.Row(cross_align = "start", children = header)))

    # Source icon sits directly under the % symbol (Rob 2026-08-10).
    if source:
        els.append(at(10 * len(str(soc)) + 1, 7, src_icon(source)))

    # Right column: rate, then car (fresh WiCAN only) — 6px slots.
    y = 0
    if word:
        lead = []
        if up != None:
            lead = [flow_arrow(up, wcol), render.Box(width = 1, height = 1)]
        elif house:
            lead = [house_icon(), render.Box(width = 1, height = 1)]
        els.append(right_at(y, lead + [render.Text(word, font = "tom-thumb", color = wcol)]))
        y += 6
    if car:
        els.append(right_at(y, [car_icon(), render.Box(width = 2, height = 1),
                                render.Text(car + "%", font = "tom-thumb", color = CYAN)]))
        y += 6

    # Weather line: current / overnight / tomorrow, each icon + temp. Colour
    # still carries the label: white now, cyan overnight low, orange tomorrow.
    def wgroup(c, t, colr):
        return render.Row(cross_align = "center", children = [
            weather_icon(c), render.Box(width = 1, height = 1),
            render.Text(t + "\u00b0", font = "tom-thumb", color = colr),
        ])

    els.append(at(0, 17, render.Row(expanded = True, main_align = "space_between", children = [
        wgroup(config.str("cond", ""), t_now, INK),
        wgroup(cond_n, t_min, CYAN),
        wgroup(cond_t, t_max, ORANGE),
    ])))

    barcol = "#a78bfa" if source == "grid" else ("#fcd34d" if source == "sun" else col)
    els.append(at(0, 27, soc_bar(fill, barcol, net, config.str("bar", "sweep"), 64)))

    # Coast margin tucked into the bar's unfilled end — shown whenever the
    # unfilled bar has room for it (space appears as SoC drops below ~75%,
    # which is exactly when the coast question starts mattering).
    if (64 - fill) >= 4 * len(ctxt) + 2:
        els.append(right_at(26, [render.Text(ctxt, font = "tom-thumb", color = ccol),
                                 render.Box(width = 1, height = 1)]))

    return render.Root(
        delay = flow_delay(net),
        child = render.Stack(children = els),
    )
