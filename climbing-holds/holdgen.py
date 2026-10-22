"""Parametric climbing hold generator for PA6-GF FDM printing (Bambu).

Holds are modeled as signed distance fields and meshed with marching cubes,
so shapes stay organic while every fastening/printing rule is enforced
programmatically. Output is a watertight binary STL in millimeters,
back face on z=0 (wall contact face = print bed face, no supports).

Frame convention, as the hold sits on the wall:
    +X  right across the wall
    +Y  up the wall      (the gripping surface faces +Y; you pull down on it)
    +Z  out from the wall (= up off the print bed)

Design rules baked into geometry (see DESIGN_RULES.md):
  - screw-on mounting: 10g stainless decking screws + conical washers,
    countersunk seat, solid boss column under every seat
  - minimum feature thickness = 2 sides x WALLS x LINE_W (default 4.2 mm)
  - hollowed, ribbed back to cut PA6-GF warping forces and mass
  - grip texture baked only into the active surface (not sides/base band)
  - rounded footprint, flat back, no supports needed

Run:  python holdgen.py              -> every hold in HOLDS
      python holdgen.py crimp jug    -> just those
      python holdgen.py --list       -> names only
"""

import os
import sys
import numpy as np
from skimage import measure
from scipy import ndimage
import trimesh

# ------------------------------------------------------------------ process
LINE_W = 0.42          # mm, Bambu 0.4 nozzle default line width
WALLS = 5              # slicer wall loops (user rule)
MIN_FEATURE = 2 * WALLS * LINE_W   # 4.2 mm: thinnest allowed feature
RES = 0.35             # mm per voxel

# ------------------------------------------------- fastener: 10g deck screw
SCREW = dict(
    shank_clear_r=2.9,   # 10g shank ~4.9 mm -> 5.8 mm clearance hole
    washer_r=7.25,       # conical washer OD 14.5 mm  (EDIT to match yours)
    cone_angle=90.0,     # included angle of washer cone (EDIT to match)
    washer_sink=1.5,     # washer top recessed below local surface
    access_clear=0.75,   # radial clearance of the access pocket
    access_flare=3.0,    # how far the pocket flares open above the seat.
                         # MUST stay small: the flare is only there to clear
                         # the daylight ring where the bore leaves a curved
                         # surface. Uncapped it becomes a 45 deg cone that
                         # swallows any part of the hold rising above a seat.
    driver_r=10.0,       # socket/bit radius that must reach the screw head
    min_boss=6.0,        # minimum solid column depth under the cone seat
)

# -------------------------------------------------------- back hollowing
POCKET = dict(
    depth=10.0,          # pocket depth from the back face
    rim=5.0,             # solid rim inset from the outline
    rib_w=5.2,           # rib width (MIN_FEATURE + margin so ribs slice solid)
    rib_pitch=18.0,      # rib grid pitch (-> ~13 mm bridges, fine for FDM)
    boss_extra=3.0,      # extra solid radius around screw bosses
)

PA6GF_DENSITY = 1.25  # g/cm^3 (Bambu PA6-GF)

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


# ----------------------------------------------------------- SDF helpers
def sd_ellipsoid(p, center, radii):
    q = (p - np.asarray(center, dtype=np.float32)) / np.asarray(
        radii, dtype=np.float32)
    k0 = np.linalg.norm(q, axis=-1)
    k1 = np.linalg.norm(q / np.asarray(radii, dtype=np.float32), axis=-1)
    return k0 * (k0 - 1.0) / np.maximum(k1, 1e-9)


def sd_sphere(p, center, r):
    return np.linalg.norm(p - np.asarray(center, dtype=np.float32),
                          axis=-1) - r


def sd_round_box(p, center, half, r):
    """Box with radius-r rounded edges — a true distance field.

    The right blank for edges and chips: unlike an ellipsoid it holds full
    thickness out to the footprint edge, so a washer seat near the rim still
    gets its boss column, and the rounding radius sets the grip edge radius
    directly."""
    q = (np.abs(p - np.asarray(center, dtype=np.float32))
         - (np.asarray(half, dtype=np.float32) - r))
    outside = np.linalg.norm(np.maximum(q, 0.0), axis=-1)
    inside = np.minimum(q.max(axis=-1), 0.0)
    return outside + inside - r


def smin(a, b, k):
    """Polynomial smooth union."""
    h = np.clip(0.5 + 0.5 * (b - a) / k, 0.0, 1.0)
    return b * (1 - h) + a * h - k * h * (1 - h)


def smax(a, b, k):
    """Smooth intersection/subtraction partner — rounds the resulting edge,
    which keeps lips above MIN_FEATURE instead of tapering to a knife."""
    return -smin(-a, -b, k)


# ------------------------------------------------------------- generator
def generate(body_fn, screws, name, bbox, texture=1.0):
    """body_fn(P) -> SDF of the hold body (before back clip, holes, pockets).
    screws: list of (x, y) screw positions. bbox: ((x0,x1),(y0,y1),(z0,z1)).
    texture: grip-texture amplitude scale (0 disables)."""
    (x0, x1), (y0, y1), (z0, z1) = bbox
    xs = np.arange(x0, x1, RES, dtype=np.float32)
    ys = np.arange(y0, y1, RES, dtype=np.float32)
    zs = np.arange(z0, z1, RES, dtype=np.float32)
    X, Y, Z = np.meshgrid(xs, ys, zs, indexing="ij")
    P = np.stack([X, Y, Z], axis=-1)

    d = body_fn(P).astype(np.float32)
    del P

    # -- grip texture, only on the active surface -------------------------
    # two octaves ~= deep fuzzy skin, but masked: not near the bed (adhesion
    # band stays smooth for warp control) and not in the washer seats.
    if texture:
        tex = (0.28 * np.sin(0.9 * X) * np.sin(1.1 * Y + 1.7)
               * np.sin(0.8 * Z + 0.6)
               + 0.18 * np.sin(2.2 * X + 3.0) * np.sin(2.0 * Y + 1.2)
               * np.sin(1.7 * Z))
        mask = np.clip((Z - 5.0) / 8.0, 0, 1)
        for sx, sy in screws:
            rseat = np.hypot(X - sx, Y - sy)
            mask *= np.clip((rseat - (SCREW["washer_r"] + 2.5)) / 2.0, 0, 1)
        d += texture * tex * mask
        del tex, mask

    # -- flat back at z=0 (wall face / print bed) -------------------------
    # keep the pre-clip field: after the clip, values near the back read
    # "distance to the bed", which would break the pocket rim inset below
    d_noclip = d.copy()
    d = np.maximum(d, -Z)
    d_body = d.copy()

    # local height map (top of the solid per column)
    occ = d_body < 0
    ksurf = occ.shape[2] - 1 - np.argmax(occ[:, :, ::-1], axis=2)
    H2d = np.where(occ.any(axis=2), zs[ksurf], 0.0).astype(np.float32)
    X2, Y2 = np.meshgrid(xs, ys, indexing="ij")
    del occ, ksurf

    # -- screw seats ------------------------------------------------------
    warnings = []
    cone_h = (SCREW["washer_r"] - SCREW["shank_clear_r"]) / np.tan(
        np.radians(SCREW["cone_angle"] / 2.0))
    for sx, sy in screws:
        disk = (X2 - sx) ** 2 + (Y2 - sy) ** 2 <= (
            SCREW["washer_r"] + SCREW["access_clear"] + 1.0) ** 2
        hmin = float(H2d[disk].min())
        seat_top = hmin - SCREW["washer_sink"]
        cone_bot = seat_top - cone_h
        if cone_bot < SCREW["min_boss"]:
            warnings.append(f"screw ({sx:+.0f},{sy:+.0f}) boss column "
                            f"{cone_bot:.1f} mm < {SCREW['min_boss']} mm")
        r = np.hypot(X - sx, Y - sy)
        # through hole -> cone seat -> capped at washer radius
        prof = SCREW["shank_clear_r"] + np.clip(
            (Z - cone_bot) * np.tan(np.radians(SCREW["cone_angle"] / 2.0)),
            0, SCREW["washer_r"] - SCREW["shank_clear_r"])
        hole = r - prof
        # access pocket flares at 45 deg above the seat: a straight bore
        # through a curved surface leaves a standing sub-MIN_FEATURE ring
        # at the daylight line; the flare removes it entirely
        # The pocket spans only from the seat up to the surface it breaks
        # through. Left unbounded it is an infinite cylinder that deletes
        # anything standing near a seat (it once ate a whole pinch column).
        access = np.maximum.reduce([
            r - (SCREW["washer_r"] + SCREW["access_clear"]
                 + np.clip(Z - seat_top, 0, SCREW["access_flare"])),
            seat_top - Z,
            Z - (hmin + SCREW["access_flare"]),
        ])
        d = np.maximum(d, -np.minimum(hole, access))

        # driver clearance: anything standing well proud of the surface at
        # the seat and closer than a socket radius blocks the screwdriver.
        # Referenced to the height at the seat centre, not the disk minimum,
        # or a dome's own rise reads as an obstruction.
        ci = int(np.argmin(np.abs(xs - sx)))
        cj = int(np.argmin(np.abs(ys - sy)))
        blocking = H2d > H2d[ci, cj] + 8.0
        blocking[(X2 - sx) ** 2 + (Y2 - sy) ** 2
                 <= (SCREW["washer_r"] + SCREW["access_clear"]) ** 2] = False
        clear = (np.hypot(X2[blocking] - sx, Y2[blocking] - sy).min()
                 if blocking.any() else np.inf)
        if clear < SCREW["driver_r"]:
            warnings.append(f"screw ({sx:+.0f},{sy:+.0f}) driver clearance "
                            f"{clear:.1f} mm < {SCREW['driver_r']} mm")
        print(f"  screw ({sx:+.0f},{sy:+.0f}): local top {hmin:5.1f} mm, "
              f"seat {seat_top:5.1f} mm, boss column {cone_bot:5.1f} mm, "
              f"driver clear {clear:.0f} mm")
    del r, prof, hole, access

    # -- hollowed ribbed back (warp + mass reduction) ---------------------
    p_rim = d_noclip + POCKET["rim"]
    p_depth = Z - POCKET["depth"]
    gx = np.abs(((X + POCKET["rib_pitch"] / 2) % POCKET["rib_pitch"])
                - POCKET["rib_pitch"] / 2)
    gy = np.abs(((Y + POCKET["rib_pitch"] / 2) % POCKET["rib_pitch"])
                - POCKET["rib_pitch"] / 2)
    p_rib = POCKET["rib_w"] / 2 - np.minimum(gx, gy)
    boss_r = SCREW["washer_r"] + POCKET["boss_extra"]
    p_boss = np.full_like(d, -1e3)
    for sx, sy in screws:
        p_boss = np.maximum(p_boss, boss_r - np.hypot(X - sx, Y - sy))
    # only hollow columns tall enough to keep a solid cap over the pocket
    p_tall = np.broadcast_to(
        (POCKET["depth"] + POCKET["rim"]) - H2d[:, :, None], d.shape)
    pocket = np.maximum(np.maximum(p_rim, p_depth),
                        np.maximum(np.maximum(p_rib, p_boss), p_tall))
    d = np.maximum(d, -pocket)
    del p_rim, p_depth, p_rib, p_boss, p_tall, pocket, gx, gy, X, Y, Z

    # -- mesh -------------------------------------------------------------
    verts, faces, _, _ = measure.marching_cubes(d, level=0.0,
                                                spacing=(RES, RES, RES))
    verts += np.array([xs[0], ys[0], zs[0]])
    mesh = trimesh.Trimesh(vertices=verts, faces=faces)
    mesh.process(validate=True)
    mesh.fill_holes()
    mesh = max(mesh.split(only_watertight=False), key=lambda m: m.area)

    # -- min-feature check, per print layer (2D): wall thickness is a -----
    # per-slice property in FDM. Material farther than MIN_FEATURE from a
    # MIN_FEATURE-wide core in its own layer can't carry WALLS perimeters.
    # only material thin over >2.5 mm of height counts: feathered daylight
    # edges (pocket mouths, dome caps, lip crests) are backed by solid
    # below and print fine; a standing thin wall does not
    solid = d < 0
    col_thin = np.zeros(solid.shape[:2], dtype=np.int32)
    for k in range(solid.shape[2]):
        sl = solid[:, :, k]
        if not sl.any():
            continue
        edt = ndimage.distance_transform_edt(sl, sampling=RES)
        core = edt >= MIN_FEATURE / 2
        d2c = ndimage.distance_transform_edt(~core, sampling=RES)
        col_thin += (sl & (d2c > MIN_FEATURE)).astype(np.int32)
    persistent = col_thin[col_thin * RES > 2.5]
    thin_mm3 = float(persistent.sum()) * RES ** 3

    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{name}.stl")
    mesh.export(path)

    stats = dict(
        name=name,
        watertight=bool(mesh.is_watertight),
        faces=len(mesh.faces),
        extents=np.round(mesh.extents, 1),
        volume_cm3=mesh.volume / 1000.0,
        mass_g=mesh.volume / 1000.0 * PA6GF_DENSITY,
        thin_mm3=thin_mm3,
        min_feature_ok=thin_mm3 < 25,
        warnings=warnings,
        screws=screws,
        path=path,
    )
    for w in warnings:
        print(f"  WARNING: {w}")
    print(f"  watertight={stats['watertight']}, {stats['faces']} faces, "
          f"extents {stats['extents']} mm")
    print(f"  volume {stats['volume_cm3']:.0f} cm^3, "
          f"~{stats['mass_g']:.0f} g in PA6-GF")
    print(f"  min-feature ({MIN_FEATURE:.1f} mm): "
          f"{'PASS' if stats['min_feature_ok'] else f'CHECK ({thin_mm3:.0f} mm^3 thin)'}")
    print(f"  wrote {path}")
    return mesh, stats


# --------------------------------------------------------------- preview
def preview(mesh, name, screws):
    """4 shaded views + 3 cross-sections, written next to the STL."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    m = mesh
    if len(mesh.faces) > 150000:
        try:
            m = mesh.simplify_quadric_decimation(face_count=150000)
        except BaseException:
            m = mesh
    tris = m.vertices[m.faces]
    n = m.face_normals
    ext = mesh.extents
    lim = float(max(ext[0], ext[1])) / 2 + 5
    ctr = mesh.bounds.mean(axis=0)

    def shaded(light):
        s = np.clip(0.35 + 0.65 * (n @ np.asarray(light)), 0.15, 1)
        return np.stack([0.91 * s, 0.45 * s, 0.29 * s, np.ones_like(s)],
                        axis=1)

    front = shaded([0.3, -0.5, 0.8])
    back = shaded([0.3, -0.5, -0.8])
    views = [(35, -60, "3/4 view", front),
             (8, -90, "front (climber's view)", front),
             (80, -90, "top-down", front),
             (-70, -90, "back (ribs + bosses)", back)]

    fig = plt.figure(figsize=(13, 9))
    for i, (elev, azim, title, col) in enumerate(views, 1):
        ax = fig.add_subplot(2, 2, i, projection="3d")
        ax.add_collection3d(Poly3DCollection(tris, facecolors=col,
                                             edgecolor="none"))
        ax.set_xlim(ctr[0] - lim, ctr[0] + lim)
        ax.set_ylim(ctr[1] - lim, ctr[1] + lim)
        ax.set_zlim(0, 2 * lim)
        ax.set_box_aspect([1, 1, 1])
        ax.view_init(elev=elev, azim=azim)
        ax.set_title(title, fontsize=10)
        ax.set_axis_off()
    fig.suptitle(f"{name}   {ext[0]:.0f} x {ext[1]:.0f} x {ext[2]:.0f} mm",
                 fontsize=12)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, f"{name}_preview.png"), dpi=100,
                bbox_inches="tight")
    plt.close(fig)

    # Sections are drawn against real world axes, not section.to_2D()'s
    # arbitrary in-plane basis — otherwise the profile is unreadable and
    # you cannot tell an incut edge from a vertical face.
    AX = "XYZ"
    sy = screws[0][1]
    planes = [([0, sy, 0], 1, f"y={sy:+.0f}  screw row"),
              ([0, 0, 0], 0, "x=0  profile (wall at Z=0, up-wall is +Y)"),
              ([0, 0, 4.0], 2, "z=4  back ribs")]
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.6))
    for ax, (origin, axis, title) in zip(axes, planes):
        normal = np.eye(3)[axis]
        sec = mesh.section(plane_origin=origin, plane_normal=normal)
        if sec is None:
            ax.set_title(title + "  (empty)", fontsize=10)
            ax.set_axis_off()
            continue
        u, v = [i for i in range(3) if i != axis]
        for ent in sec.entities:
            pts = sec.vertices[ent.points]
            ax.plot(pts[:, u], pts[:, v], "k-", lw=0.8)
        ax.set_aspect("equal")
        ax.grid(alpha=0.25, lw=0.4)
        ax.set_xlabel(f"{AX[u]} (mm)", fontsize=8)
        ax.set_ylabel(f"{AX[v]} (mm)", fontsize=8)
        ax.set_title(title, fontsize=10)
    fig.suptitle(f"{name} cross-sections", fontsize=12)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, f"{name}_sections.png"), dpi=110,
                bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------------------ hold recipes
# Each body_fn returns the SDF of the raw blank; generate() adds texture,
# the flat back, the screw seats and the ribbed hollow.

def jug_body(P):
    """Big incut bucket — fingers wrap into the top scoop."""
    d = sd_ellipsoid(P, [0, 0, 2], [46, 34, 30])
    d = smin(d, sd_ellipsoid(P, [-16, 6, 6], [30, 26, 34]), 8)
    d = smin(d, sd_ellipsoid(P, [18, -4, 4], [28, 24, 30]), 8)
    # incut scoop, smooth-subtracted so the lip stays rounded and thick
    d = smax(d, -sd_sphere(P, [0, 26, 58], 38), 5)
    return d


def sloper_body(P):
    """Rounded dome, no positive edge — all friction, open-handed."""
    d = sd_ellipsoid(P, [0, -4, -20], [52, 44, 54])
    # roll the crown away toward +Y. Kept shallow: a deeper bite starves the
    # upper screw boss, which the boss-column check then rejects.
    d = smax(d, -sd_sphere(P, [0, 58, 54], 48), 10)
    return d


def crimp_body(P):
    """Incut edge: flat top, defined crest at +Y, front face receding below
    it. A round-box blank keeps full 18 mm thickness under the washers."""
    d = sd_round_box(P, [0, -2, 0], [38, 22, 18], 6.0)
    # pull the lower front back toward the wall — that overhang is the incut
    d = smax(d, -sd_sphere(P, [0, 36, -16], 30), 5)
    # break the top-front corner into a crest your fingers can curl over
    d = smax(d, -sd_sphere(P, [0, 46, 40], 36), 6)
    return d


def pinch_body(P):
    """Tall column with dished flanks, standing on a wide skirt.

    The skirt is not decoration: a driver needs a straight 22 mm shaft down
    to each screw head, so the seats have to sit clear of the column's plan
    outline or the access pockets bore the column away."""
    skirt = sd_round_box(P, [0, 0, 0], [34, 34, 13], 6.0)
    column = sd_ellipsoid(P, [0, 0, -6], [26, 13, 50])
    d = smin(skirt, column, 8)
    # dish both flanks to form the pinch faces (~28 mm across)
    d = smax(d, -sd_sphere(P, [34, 0, 32], 20), 7)
    d = smax(d, -sd_sphere(P, [-34, 0, 32], 20), 7)
    return d


def foot_body(P):
    """Small positive foot chip — stand on the +Y edge."""
    d = sd_round_box(P, [0, -2, 0], [28, 20, 15], 5.0)
    d = smax(d, -sd_sphere(P, [0, 32, -14], 26), 4)
    d = smax(d, -sd_sphere(P, [0, 40, 32], 30), 5)
    return d


HOLDS = {
    "jug": dict(body=jug_body,
                screws=[(-25, -4), (25, -4), (0, 16)],
                bbox=((-52, 52), (-42, 42), (-2, 48))),
    "sloper": dict(body=sloper_body,
                   screws=[(-26, -6), (26, -6), (0, 14)],
                   bbox=((-58, 58), (-50, 50), (-2, 42))),
    "crimp": dict(body=crimp_body,
                  screws=[(-20, -10), (20, -10)],
                  bbox=((-46, 46), (-32, 30), (-2, 26))),
    "pinch": dict(body=pinch_body,
                  screws=[(0, -22), (0, 22)],
                  bbox=((-42, 42), (-42, 42), (-2, 50))),
    "foot": dict(body=foot_body,
                 screws=[(-13, -6), (13, -6)],
                 bbox=((-36, 36), (-28, 26), (-2, 22))),
}


def main(argv):
    if "--list" in argv:
        print(" ".join(HOLDS))
        return
    names = [a for a in argv if not a.startswith("-")] or list(HOLDS)
    unknown = [n for n in names if n not in HOLDS]
    if unknown:
        sys.exit(f"unknown hold(s): {', '.join(unknown)}\n"
                 f"available: {', '.join(HOLDS)}")

    all_stats = []
    for n in names:
        spec = HOLDS[n]
        print(f"\n=== {n} ===")
        mesh, stats = generate(spec["body"], spec["screws"], n, spec["bbox"])
        preview(mesh, n, spec["screws"])
        all_stats.append(stats)

    print(f"\n{'hold':8} {'size (mm)':>18} {'cm^3':>6} {'g':>6} "
          f"{'min-feat':>9}  notes")
    for s in all_stats:
        e = s["extents"]
        note = "; ".join(s["warnings"]) or ("" if s["watertight"]
                                            else "NOT WATERTIGHT")
        print(f"{s['name']:8} {e[0]:5.0f}x{e[1]:4.0f}x{e[2]:4.0f}      "
              f"{s['volume_cm3']:6.0f} {s['mass_g']:6.0f} "
              f"{'PASS' if s['min_feature_ok'] else 'CHECK':>9}  {note}")


if __name__ == "__main__":
    main(sys.argv[1:])
