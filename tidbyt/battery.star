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


def soc_bar(fill, col, net, style):
    """The SoC bar. Animation travels in the direction of the current — right
    while charging, left while discharging, still when idle. Rate drives the
    look: frame delay (see flow_delay), chevron contrast, tip brightness and a
    fill lightening all scale with |net| up to 5kW.
      sweep   — baseline: soft light band gliding along the fill
      chevtip — chevrons marching + a glow at the leading edge"""
    t = min(abs(net) / 5.0, 1.0)
    hot = _lighten(col, t * 0.25)
    base = [
        render.Box(width = 64, height = 5, color = "#232935"),
        render.Box(width = fill, height = 5, color = hot),
    ]
    if abs(net) <= 0.05:
        return render.Stack(children = [
            render.Box(width = 64, height = 5, color = "#232935"),
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
CYAN, ORANGE = "#38bdf8", "#fb923c"
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
    kw1 = str(int(abs(net) * 10 + 0.5) / 10.0)
    if net > 0.05:
        word, wcol, up = kw1 + "kW", GREEN, True
    elif net < -0.05:
        word, wcol, up = kw1 + "kW", RED, False
    else:
        word, wcol, up = "", MUTED, None

    # Coast margin to the 10:00 free window, same bands as the kiosk COAST tile.
    ccol = GREEN if coast >= 10 else (AMBER if coast >= 0 else RED)
    ctxt = ("+" if coast >= 0 else "") + str(coast)

    fill = max(1, min(64, int(soc * 64 / 100 + 0.5)))

    return render.Root(
        delay = flow_delay(net),
        child = render.Column(
            expanded = True,
            main_align = "space_between",
            children = [
                render.Row(
                    expanded = True,
                    main_align = "space_between",
                    cross_align = "center",
                    children = [
                        # Big number in 10x20, tiny % beside it: "100%" all in
                        # 10x20 is 40px and collides with the right column.
                        render.Row(
                            cross_align = "start",
                            children = [
                                render.Text("%d" % soc, font = "10x20", color = INK),
                                render.Text("%", font = "tom-thumb", color = MUTED),
                            ],
                        ),
                        render.Column(
                            cross_align = "end",
                            children = [
                                # "=" reads as "equivalent to"; dropped at 100%
                                # where the row runs out of pixels.
                                render.Text(("" if soc >= 100 else "=") + kwh + "kWh",
                                            font = "tom-thumb", color = MUTED),
                                render.Row(
                                    cross_align = "center",
                                    children = ([] if up == None else
                                                [flow_arrow(up, wcol),
                                                 render.Box(width = 1, height = 1)]) + [
                                        render.Text(word, font = "tom-thumb", color = wcol),
                                    ],
                                ),
                            ],
                        ),
                    ],
                ),
                # Coast margin | temps: now (white) - overnight low (blue) -
                # tomorrow high (orange). Colour carries the labels.
                render.Row(
                    expanded = True,
                    main_align = "space_between",
                    children = [
                        render.Text(ctxt, font = "tom-thumb", color = ccol),
                        render.Row(
                            children = [
                                render.Text(t_now + "°", font = "tom-thumb", color = INK),
                                render.Box(width = 3, height = 1),
                                render.Text(t_min + "°", font = "tom-thumb", color = CYAN),
                                render.Box(width = 3, height = 1),
                                render.Text(t_max + "°", font = "tom-thumb", color = ORANGE),
                            ],
                        ),
                    ],
                ),
                # SoC bar, coloured by the coast-health sensor like the kiosk band.
                soc_bar(fill, col, net, config.str("bar", "sweep")),
            ],
        ),
    )
