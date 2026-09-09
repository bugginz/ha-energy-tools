# Climbing hold design rules — PA6-GF FDM (Bambu)

Working split: everything geometric is enforced in `holdgen.py` at model
time; everything process-related stays in the slicer. Holds print back-face
down, no supports.

## Baked into geometry (holdgen.py)

| Rule | Implementation |
|---|---|
| Screw-on mounting, 10g stainless decking screws | 5.8 mm through-hole per screw, positions parametric per hold |
| Conical washer spreads load into a boss | 90° cone seat sized to washer OD (14.5 mm default — **confirm actual washer**), washer recessed 1.5 mm below local surface, solid boss column ≥ 6 mm under every seat (checker warns if violated), back-side boss ring excluded from hollowing |
| Wall thickness ≥ 5 walls | Min feature = 2 × 5 × 0.42 mm = 4.2 mm. Automated per-print-layer check: 2D erosion test on every slice; only vertically-persistent (> 2.5 mm tall) thin material fails, since feathered daylight edges at pocket mouths / dome caps print fine. Ribs sized 5.2 mm — width must clear 4.2 mm with margin or voxel/slicer quantization makes them borderline |
| Fuzzy skin on active surface | Two-octave displacement (~0.3–0.45 mm) baked into the mesh, masked to z > 5 mm and away from washer seats — unlike slicer fuzzy skin it never touches the bed-adhesion band or the seat faces. Slicer fuzzy skin can still be painted on top if wanted |
| Warping | Hollowed back (10 mm pockets, 5 mm rim, 5.2 mm rib grid at 18 mm pitch → ~13 mm bridges) cuts the solid cross-section and shrinkage force ~20 %; footprint has no sharp plan-view corners; texture masked off the first 5 mm so the outer bottom perimeter stays smooth for adhesion |
| Access | Washer pockets flare at 45° above the seat — avoids standing thin rings where a straight bore daylights through the curved surface, and gives driver clearance |

## Left to the slicer / process

- **Walls**: 5 loops, 0.42 mm line width (matches MIN_FEATURE above — if you
  change either, change `WALLS`/`LINE_W` in holdgen.py to match)
- **Shrinkage compensation**: use the Bambu PA6-GF filament profile value;
  holds are not precision parts, only the washer seat matters and it has
  0.75 mm radial clearance
- **Warping, process side**: dry filament (PA6-GF is hygroscopic), enclosure,
  textured PEI + PA-safe adhesive, generous brim; elephant-foot compensation
- **Infill**: pockets already remove bulk; 40 %+ gyroid in what remains, or
  solid — the load path is the boss columns and the shell
- **Nozzle**: hardened, 0.4 mm+ (glass fiber is abrasive)

## Open parameters to confirm

- Actual conical washer OD and cone angle (`SCREW["washer_r"]`,
  `SCREW["cone_angle"]`) — currently 14.5 mm / 90°
- Screws per hold vs. hold size (current jug: 3)
- Whether washer recess depth (1.5 mm) is enough to keep screw heads off skin

## Checks run on every generation

1. Watertight mesh
2. Boss column depth under every cone seat ≥ 6 mm (prints warning + location)
3. Per-layer min-feature scan (4.2 mm, persistence-filtered) → PASS/CHECK
4. Volume / mass estimate at 1.25 g/cm³
