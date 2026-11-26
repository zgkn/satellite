#!/usr/bin/env python3
"""Run the operational image script unchanged, optionally with a minimal caption (ad-hoc use only).

himawari_truecolor.py is imported as it is and its ``main()`` does all the work; with --minimal-caption the function that draws
the text box is swapped, in this process only, for one that writes just the satellite name and the timestamp. Graticule,
coastlines and fire marks are drawn as usual (and so are streamlines if --winds is given); the recipe line, notes (fire
count, low-sun warning, ...) and colour legend are left out.

With --smoke-stretch RED_MAX,GREEN_MAX,GAMMA (for example 25,40,1.3) the Smoke RGB red and green channels are scaled 0..RED_MAX %
and 0..GREEN_MAX % (operational: 45 and 65) and get that gamma (operational: 1), which lifts the dark sea and thin haze; the blue
(10.4 um, inverted) channel is as operational. Only this process is changed. All other arguments are the operational script's.

    python scripts/adhoc_products.py --minimal-caption --region papua --bbox 123,-11,141.5,1 --segments 5-7 \
        --datetime-utc 202610160600 --product smoke --outdir out
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import himawari_truecolor as h  # noqa: E402


def overlay_minimal(rgb, scan, product, gammas, rayleigh, out, bbox=None, resolution=None, coast_width=0.0,
                    wind=None, wind_spacing=45.0, notes=(), corner="bottom-right", grid_step=0.0,
                    recipe_line=None, legend=None, fire_mask=None) -> None:
    """Same layers as ``h.overlay_and_save`` (graticule, streamlines, coastlines, fire) but the caption is two lines."""
    from PIL import Image, ImageDraw

    img = Image.fromarray(rgb, "RGB").convert("RGBA")
    if grid_step > 0:
        img = h.draw_graticule(img, bbox, resolution, grid_step)
    if wind is not None:
        img = h.ecmwf_winds.draw_streamlines(img, wind, bbox, resolution, wind_spacing, product.get("wind_rgb"))
    if coast_width > 0:
        h.draw_coastline(img, bbox, resolution, coast_width, product["coast_rgba"])
    if fire_mask is not None and fire_mask.any():
        img = h.draw_fire(img, fire_mask, resolution)
    size = max(12, round(min(rgb.shape[0], 0.56 * rgb.shape[1]) / 48))
    font = h._font(size)
    sgt = scan.astimezone(h.SGT)
    lines = ["Himawari-9 AHI", f"{scan:%Y-%m-%d %H:%M} UTC  /  {sgt:%Y-%m-%d %H:%M} SGT"]
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    pad, gap = size // 2, size // 4
    boxes = [d.textbbox((0, 0), t, font=font) for t in lines]
    bw = max(b[2] - b[0] for b in boxes) + 2 * pad
    bh = sum(b[3] - b[1] for b in boxes) + gap * (len(lines) - 1) + 2 * pad
    x0 = pad if corner.endswith("left") else img.width - bw - pad
    y0 = pad if corner.startswith("top") else img.height - bh - pad
    d.rectangle([x0, y0, x0 + bw, y0 + bh], fill=(0, 0, 0, 140))
    y = y0 + pad
    for text, b in zip(lines, boxes):
        d.text((x0 + pad - b[0], y - b[1]), text, font=font, fill=(255, 255, 255, 255))
        y += b[3] - b[1] + gap
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.alpha_composite(img, layer).convert("RGB").save(out, optimize=True)


def brighten_smoke(spec: str) -> None:
    """Replace the Smoke RGB red/green stretch and gamma in this process (see the module docstring)."""
    try:
        red_max, green_max, gamma = (float(v) for v in spec.split(","))
        if not (red_max > 0 and green_max > 0 and gamma > 0):
            raise ValueError
    except ValueError:
        raise SystemExit(f"--smoke-stretch needs RED_MAX,GREEN_MAX,GAMMA as positive numbers, got {spec!r}")
    prod = h.PRODUCTS["smoke"]
    (rb, rc, _, _, _), (gb, gc, _, _, _), blue = prod["channels"]
    h.PRODUCTS["smoke"] = {**prod, "channels": ((rb, rc, 0, red_max, gamma), (gb, gc, 0, green_max, gamma), blue)}
    h.log(f"Smoke RGB stretch changed for this run: R 0-{red_max:g}%  G 0-{green_max:g}%  gamma {gamma:g}")


def main(argv: list[str]) -> int:
    argv = list(argv)
    if "--minimal-caption" in argv:
        argv.remove("--minimal-caption")
        h.overlay_and_save = overlay_minimal
    if "--smoke-stretch" in argv:
        i = argv.index("--smoke-stretch")
        brighten_smoke(argv[i + 1] if i + 1 < len(argv) else "")
        del argv[i:i + 2]
    return h.main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
