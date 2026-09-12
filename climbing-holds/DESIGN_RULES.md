# Climbing hold design rules — PA6-GF FDM (Bambu)

Working split: everything geometric is enforced in `holdgen.py` at model
time; everything process-related stays in the slicer. Holds print back-face
down, no supports.

    python holdgen.py            # every hold
    python holdgen.py crimp jug  # just those
    python holdgen.py --list

Each run writes `output/<name>.stl` plus a 4-view preview and a 3-plane
section sheet, and prints a pass/fail line per hold.

## Frame convention

As the hold sits on the wall: **+X** right, **+Y** up the wall, **+Z** out
of the wall (= up off the print bed). The gripping surface faces +Y; you
pull down into it. The back face is the Z=0 plane.

## Baked into geometry (holdgen.py)

| Rule | Implementation |
|---|---|
| Screw-on mounting, 10g stainless decking screws | 5.8 mm through-hole per screw |
| Conical washer spreads load into a boss | 90° cone seat sized to washer OD (14.5 mm default — **confirm actual washer**), washer recessed 1.5 mm, solid boss column ≥ 6 mm under every seat, back-side boss ring excluded from hollowing |
| Wall thickness ≥ 5 walls | Min feature = 2 × 5 × 0.42 mm = 4.2 mm, checked per print layer by 2D erosion on every slice; only vertically-persistent (> 2.5 mm tall) thin material fails, since feathered daylight edges print fine backed by solid |
| Fuzzy skin on active surface | Two-octave displacement (~0.45 mm) baked into the mesh, masked to Z > 5 mm and away from washer seats — unlike slicer fuzzy skin it never touches the bed-adhesion band or the seat faces |
| Warping | Hollowed back (10 mm pockets, 5 mm rim, 5.2 mm rib grid at 18 mm pitch) cuts solid cross-section and shrinkage force; footprint has no sharp plan-view corners; texture masked off the first 5 mm so the outer bottom perimeter stays smooth for adhesion |
| Driver access | Washer pocket spans only from the seat up to the surface it breaks through, flaring 3 mm to clear the daylight ring; a separate check reports horizontal clearance to anything standing ≥ 8 mm proud within a 10 mm socket radius |

## Two constraints the fastener imposes on shape

These are not tunables — they fall out of the 10g screw + conical washer
stack and drove several redesigns:

1. **Minimum thickness at a seat ≈ 12 mm.** washer sink 1.5 + cone depth
   4.35 + boss 6 = 11.85 mm of material under the washer before the hold
   even starts. A true micro-crimp thinner than that cannot be screw-on
   with this hardware. `crimp` is therefore an 18 mm incut edge, not a
   10 mm crimp.
2. **A tall feature needs a skirt.** A driver needs a straight shaft to the
   head, so `screw offset ≥ feature half-width + driver radius`, and the
   seat needs 9 mm of flat inside the footprint: **skirt half-width ≥
   feature half-width + ~20 mm**. That is why `pinch` is a column on a
   68 mm skirt rather than a bare column.

## Blank choice

- **Ellipsoid** (`sd_ellipsoid`) for domes — jug, sloper, pinch column.
- **Rounded box** (`sd_round_box`) for edges and chips — crimp, foot,
  pinch skirt. It holds full thickness out to the footprint edge, so a
  washer seat near the rim still gets its boss column, and its corner
  radius sets the grip edge radius directly. Ellipsoid blanks fall away at
  the rim and starve edge-mounted seats; that is what the boss-column
  check kept rejecting.

## Left to the slicer / process

- **Walls**: 5 loops, 0.42 mm line width (if you change either, change
  `WALLS`/`LINE_W` to match — MIN_FEATURE is derived from them)
- **Shrinkage compensation**: Bambu PA6-GF profile value; holds are not
  precision parts, only the washer seat matters and it has 0.75 mm radial
  clearance
- **Warping, process side**: dry filament (PA6-GF is hygroscopic),
  enclosure, textured PEI + PA-safe adhesive, generous brim; elephant-foot
  compensation
- **Infill**: pockets already remove bulk; 40 %+ gyroid in what remains, or
  solid — the load path is the boss columns and the shell
- **Nozzle**: hardened, 0.4 mm+ (glass fiber is abrasive)

## Open parameters to confirm

- Conical washer OD and cone angle (`SCREW["washer_r"]`, `cone_angle`) —
  currently 14.5 mm / 90°
- Driver/socket radius (`SCREW["driver_r"]`) — currently 10 mm
- Whether the 1.5 mm washer recess keeps screw heads clear of skin

## Checks run on every generation

1. Watertight mesh
2. Boss column depth under every cone seat ≥ 6 mm
3. Driver clearance at every seat ≥ 10 mm
4. Per-layer min-feature scan (4.2 mm, persistence-filtered)
5. Volume / mass estimate at 1.25 g/cm³

## Current set

| hold | size (mm) | cm³ | g (PA6-GF) | screws |
|---|---|---|---|---|
| jug | 96 × 68 × 37 | 105 | 131 | 3 |
| sloper | 96 × 82 × 34 | 97 | 121 | 3 |
| crimp | 76 × 44 × 18 | 46 | 58 | 2 |
| pinch | 68 × 68 × 44 | 65 | 81 | 2 |
| foot | 56 × 40 × 15 | 29 | 37 | 2 |
