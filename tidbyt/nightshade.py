#!/usr/bin/env python3
"""Recolour a rendered Tidbyt WebP to red-only for night viewing.

Usage: nightshade.py in.webp out.webp

Every frame becomes monochrome red: pixel luma < CUT is treated as background
and goes black (the card/bar backgrounds sit ~#232935, luma ~43 — at night
they'd otherwise glow); everything else maps onto [FLOOR..255] red so thin
tom-thumb strokes survive the panel dropping low bitplanes at 1-2%%
brightness. Animation frame timing and looping are preserved. Red is the
kindest channel for night vision, and red LEDs run ~1/3 the luminous
intensity of green at equal duty, so red @1-2%% is far gentler than the
palette at the same level.
"""
import sys
from PIL import Image, ImageSequence

CUT = 48      # below this luma: background, goes fully dark
FLOOR = 96    # dimmest surviving red

def shade(v):
    if v < CUT:
        return 0
    return FLOOR + (v - CUT) * (255 - FLOOR) // (255 - CUT)

def main(src, dst):
    im = Image.open(src)
    frames, durations = [], []
    for f in ImageSequence.Iterator(im):
        dur = f.info.get("duration", im.info.get("duration", 100))
        l = f.convert("L").point(shade)
        zero = Image.new("L", l.size, 0)
        frames.append(Image.merge("RGB", (l, zero, zero)))
        durations.append(dur)
    frames[0].save(dst, format="WEBP", save_all=True, lossless=True,
                   append_images=frames[1:], duration=durations, loop=0)

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
