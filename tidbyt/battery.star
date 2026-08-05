# House battery for a Tidbyt (64x32).
#
# Pure renderer: all live values arrive as CLI params from push.sh, so this app
# holds no URLs or tokens and renders the same anywhere. Kiosk palette.
#
#   pixlet render battery.star soc=64 kwh=26.1 net_kw=-1.9 health=green \
#       coast=20 t_now=8 t_min=2 t_max=17

load("render.star", "render")

def soc_bar(fill, col, net):
    """The SoC bar, animated like the kiosk band: a light sweep travelling in
    the direction of the current — right while charging, left while
    discharging, still when idle."""
    base = [
        render.Box(width = 64, height = 5, color = "#232935"),
        render.Box(width = fill, height = 5, color = col),
    ]
    if abs(net) <= 0.05:
        return render.Stack(children = base)
    n = 12
    sweep_w = 7
    travel = max(fill - sweep_w, 1)
    frames = []
    for i in range(n):
        pos = i if net > 0 else n - 1 - i
        x = int(pos * travel / (n - 1))
        frames.append(render.Stack(children = base + [
            render.Padding(
                pad = (x, 0, 0, 0),
                child = render.Box(width = sweep_w, height = 5, color = "#ffffff55"),
            ),
        ]))
    return render.Animation(children = frames)

def flow_arrow(up, color):
    """5x3 pixel triangle: point up while charging, down while discharging."""
    rows = [1, 3] if up else [3, 1]
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

    if net > 0.05:
        word, wcol, up = "charge", GREEN, True
    elif net < -0.05:
        word, wcol, up = "discharge", RED, False
    else:
        word, wcol, up = "idle", MUTED, None

    # Coast margin to the 10:00 free window, same bands as the kiosk COAST tile.
    ccol = GREEN if coast >= 10 else (AMBER if coast >= 0 else RED)
    ctxt = ("+" if coast >= 0 else "") + str(coast)

    fill = max(1, min(64, int(soc * 64 / 100 + 0.5)))

    return render.Root(
        delay = 100,
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
                soc_bar(fill, col, net),
            ],
        ),
    )
