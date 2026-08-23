# Animation playground for the battery bar — renders ONE style, named by param:
#
#   pixlet render bar_lab.star style=chevrons -o chevrons.gif
#
# Styles: sweep (production), chevrons, comet, breathe, barber, tip.
# Fixed demo state: 67% fill, discharging (motion runs right->left; every style
# flips with direction in the same way as production).

load("render.star", "render")

BG = "#232935"
GREEN = "#22c55e"
MUTED = "#94a3b8"

FILL = 43  # 67% of 64
UP = False  # discharging for the demo


def px(x, w, color, y = 0, h = 5):
    return render.Padding(pad = (x, y, 0, 0), child = render.Box(width = w, height = h, color = color))


def base():
    return [render.Box(width = 64, height = 5, color = BG),
            render.Box(width = FILL, height = 5, color = GREEN)]


def frames_sweep():
    n, w = 12, 7
    travel = FILL - w
    out = []
    for i in range(n):
        pos = i if UP else n - 1 - i
        x = int(pos * travel / (n - 1))
        out.append(render.Stack(children = base() + [px(x, w, "#ffffff55")]))
    return out, 100


def chevron(x, left):
    """3-wide chevron drawn pixel by pixel; points the way it moves."""
    a, b, c = (x + 2, x + 1, x) if left else (x, x + 1, x + 2)
    col = "#171b21aa"
    return [px(a, 1, col, 0, 1), px(a, 1, col, 4, 1),
            px(b, 1, col, 1, 1), px(b, 1, col, 3, 1),
            px(c, 1, col, 2, 1)]


def frames_chevrons():
    period = 8
    out = []
    for i in range(period):
        off = (period - 1 - i) if not UP else i
        marks = []
        for s in range(off, FILL - 3, period):
            marks += chevron(s, not UP)
        out.append(render.Stack(children = base() + marks))
    return out, 110


def frames_comet():
    n = 16
    tail = [(0, "#ffffffcc"), (2, "#ffffff88"), (4, "#ffffff44"), (6, "#ffffff22")]
    out = []
    for i in range(n):
        pos = i if UP else n - 1 - i
        head = int(pos * (FILL - 2) / (n - 1))
        seg = []
        for d, col in tail:
            x = head - d if UP else head + d
            if 0 <= x and x <= FILL - 2:
                seg.append(px(x, 2, col))
        out.append(render.Stack(children = base() + seg))
    return out, 80


def frames_breathe():
    alphas = ["00", "18", "30", "48", "60", "48", "30", "18"]
    return [render.Stack(children = base() + [px(0, FILL, "#ffffff" + a)])
            for a in alphas], 140


def frames_barber():
    period, w = 8, 4
    out = []
    for i in range(period):
        off = i if UP else period - 1 - i
        stripes = []
        for s in range(off - period, FILL, period):
            x0 = max(s, 0)
            x1 = min(s + w, FILL)
            if x1 > x0:
                stripes.append(px(x0, x1 - x0, "#ffffff3a"))
        out.append(render.Stack(children = base() + stripes))
    return out, 120


def frames_tip():
    alphas = ["00", "30", "60", "90", "b0", "90", "60", "30"]
    return [render.Stack(children = base() + [px(FILL - 3, 3, "#ffffff" + a)])
            for a in alphas], 150


STYLES = {
    "sweep": frames_sweep,
    "chevrons": frames_chevrons,
    "comet": frames_comet,
    "breathe": frames_breathe,
    "barber": frames_barber,
    "tip": frames_tip,
}


def main(config):
    style = config.str("style", "sweep")
    frames, delay = STYLES.get(style, frames_sweep)()
    return render.Root(
        delay = delay,
        child = render.Column(
            expanded = True,
            main_align = "space_between",
            children = [
                render.Text(style, font = "tom-thumb", color = MUTED),
                render.Animation(children = frames),
            ],
        ),
    )
