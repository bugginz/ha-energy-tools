"""Parametric climbing hold generator for PA6-GF FDM printing (Bambu).

Holds are modeled as signed distance fields and meshed with marching cubes,
so shapes stay organic while every fastening/printing rule is enforced
programmatically. Output is a watertight binary STL in millimeters,
back face on z=0 (wall contact face = print bed face, no supports).

Design rules baked into geometry (see DESIGN_RULES.md):
  - screw-on mounting: 10g stainless decking screws + conical washers,
    countersunk seat, solid boss column under every seat
  - minimum feature thickness = 2 sides x WALLS x LINE_W (default 4.2 mm)
  - hollowed, ribbed back to cut PA6-GF warping forces and mass
  - grip texture baked only into the active surface (not sides/base band)
  - rounded footprint, flat back, no supports needed

Run:  python holdgen.py          -> output/jug_hold.stl + previews
"""

import os
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
    min_boss=6.0,        # minimum solid column depth under the cone seat
)

# -------------------------------------------------------- back hollowing
POCKET = dict(
    depth=10.0,          # pocket depth from the back face
    rim=5.0,             # solid rim inset from the outline
    rib_w=5.2,           # rib width (MIN_FEATURE + margin so ribs slice solid)
    rib_pitch=18.0,      # rib grid pitch (-> ~15 mm bridges, fine for FDM)
    min_height=18.0,     # only hollow where the hold is at least this tall
    boss_extra=3.0,      # extra solid radius around screw bosses
)

PA6GF_DENSITY = 1.25  # g/cm^3 (Bambu PA6-GF)

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


# ----------------------------------------------------------- SDF helpers
def sd_ellipsoid(p, center, radii):
    q = (p - center) / radii
    k0 = np.linalg.norm(q, axis=-1)
    k1 = np.linalg.norm(q / radii, axis=-1)
    return k0 * (k0 - 1.0) / np.maximum(k1, 1e-9)


def sd_sphere(p, center, r):
    return np.linalg.norm(p - center, axis=-1) - r


def smin(a, b, k):
    """Polynomial smooth union."""
    h = np.clip(0.5 + 0.5 * (b - a) / k, 0.0, 1.0)
    return b * (1 - h) + a * h - k * h * (1 - h)


def smax(a, b, k):
    """Smooth intersection/subtraction partner — rounds the resulting edge,
    which keeps lips above MIN_FEATURE instead of tapering to a knife."""
    return -smin(-a, -b, k)


# ------------------------------------------------------------- generator
def generate(body_fn, screws, name, bbox):
    """body_fn(P) -> SDF of the hold body (before back clip, holes, pockets).
    screws: list of (x, y) screw positions. bbox: ((x0,x1),(y0,y1),(z0,z1))."""
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
    tex = (0.28 * np.sin(0.9 * X) * np.sin(1.1 * Y + 1.7) * np.sin(0.8 * Z + 0.6)
           + 0.18 * np.sin(2.2 * X + 3.0) * np.sin(2.0 * Y + 1.2) * np.sin(1.7 * Z))
    mask = np.clip((Z - 5.0) / 8.0, 0, 1)
    for sx, sy in screws:
        rseat = np.hypot(X - sx, Y - sy)
        mask *= np.clip((rseat - (SCREW["washer_r"] + 2.5)) / 2.0, 0, 1)
    d += tex * mask
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
    cone_h = (SCREW["washer_r"] - SCREW["shank_clear_r"]) / np.tan(
        np.radians(SCREW["cone_angle"] / 2.0))
    for sx, sy in screws:
        disk = (X2 - sx) ** 2 + (Y2 - sy) ** 2 <= (
            SCREW["washer_r"] + SCREW["access_clear"] + 1.0) ** 2
        hmin = float(H2d[disk].min())
        seat_top = hmin - SCREW["washer_sink"]
        cone_bot = seat_top - cone_h
        if cone_bot < SCREW["min_boss"]:
            print(f"WARNING: screw at ({sx},{sy}) boss column only "
                  f"{cone_bot:.1f} mm (< {SCREW['min_boss']} mm) — move it")
        r = np.hypot(X - sx, Y - sy)
        # through hole -> cone seat -> capped at washer radius
        prof = SCREW["shank_clear_r"] + np.clip(
            (Z - cone_bot) * np.tan(np.radians(SCREW["cone_angle"] / 2.0)),
            0, SCREW["washer_r"] - SCREW["shank_clear_r"])
        hole = r - prof
        # access pocket flares at 45 deg above the seat: a straight bore
        # through a curved surface leaves a standing sub-MIN_FEATURE ring
        # at the daylight line; the flare removes it entirely
        access = np.maximum(
            r - (SCREW["washer_r"] + SCREW["access_clear"]
                 + np.clip(Z - seat_top, 0, None)),
            seat_top - Z)
        d = np.maximum(d, -np.minimum(hole, access))
        print(f"screw ({sx:+.0f},{sy:+.0f}): local top {hmin:.1f} mm, "
              f"seat {seat_top:.1f} mm, boss column {cone_bot:.1f} mm")
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
    verdict = "PASS" if thin_mm3 < 25 else f"CHECK ({thin_mm3:.0f} mm^3 thin)"

    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{name}.stl")
    mesh.export(path)
    print(f"\n{name}: watertight={mesh.is_watertight}, "
          f"{len(mesh.faces)} faces, extents {np.round(mesh.extents, 1)} mm")
    print(f"volume {mesh.volume/1000:.0f} cm^3, "
          f"~{mesh.volume/1000*PA6GF_DENSITY:.0f} g in PA6-GF")
    print(f"min-feature ({MIN_FEATURE:.1f} mm) check: {verdict}")
    print(f"wrote {path}")
    return mesh


# ------------------------------------------------------------ hold recipes
def jug_body(P):
    d = sd_ellipsoid(P, np.array([0, 0, 2]), np.array([46, 34, 30]))
    d = smin(d, sd_ellipsoid(P, np.array([-16, 6, 6]), np.array([30, 26, 34])), 8)
    d = smin(d, sd_ellipsoid(P, np.array([18, -4, 4]), np.array([28, 24, 30])), 8)
    # incut scoop, smooth-subtracted so the lip stays rounded and thick
    d = smax(d, -sd_sphere(P, np.array([0, 26, 58]), 38), 5)
    return d


if __name__ == "__main__":
    generate(jug_body,
             screws=[(-25, -4), (25, -4), (0, 16)],
             name="jug_hold",
             bbox=((-52, 52), (-42, 42), (-2, 48)))
