# House battery for a Tidbyt (64x32).
#
# Pure renderer: all live values arrive as CLI params from push.sh, so this app
# holds no URLs or tokens and renders the same anywhere. Kiosk palette.
#
#   pixlet render battery.star soc=64 kwh=26.1 net_kw=-1.9 health=green

load("render.star", "render")

INK = "#f1f5f9"
MUTED = "#94a3b8"
BG = "#232935"
HEALTH = {"green": "#22c55e", "orange": "#f59e0b", "red": "#ef4444"}

def main(config):
    soc = int(float(config.str("soc", "0")) + 0.5)
    kwh = config.str("kwh", "?")
    net = float(config.str("net_kw", "0"))  # + charging, - discharging
    col = HEALTH.get(config.str("health", "green"), HEALTH["green"])

    # Starlark's % has no float-precision verbs, so one-decimal by hand.
    kw1 = str(int(abs(net) * 10 + 0.5) / 10.0)
    if net > 0.05:
        word, wcol = "CHG " + kw1, HEALTH["green"]
    elif net < -0.05:
        word, wcol = "DIS " + kw1, HEALTH["orange"]
    else:
        word, wcol = "IDLE", MUTED

    fill = max(1, min(64, int(soc * 64 / 100 + 0.5)))

    return render.Root(
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
                                render.Text("%skWh" % kwh, font = "tom-thumb", color = MUTED),
                                render.Text(word, font = "tom-thumb", color = wcol),
                            ],
                        ),
                    ],
                ),
                # SoC bar, coloured by the coast-health sensor like the kiosk band.
                render.Stack(
                    children = [
                        render.Box(width = 64, height = 5, color = BG),
                        render.Box(width = fill, height = 5, color = col),
                    ],
                ),
            ],
        ),
    )
