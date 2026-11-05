#!/usr/bin/env python3
"""Ad-hoc True Color and Ash RGB images of one Himawari-9 scan over a box, with only coastlines and a timestamp on them.

Separate from the operational pipeline: himawari_truecolor.py is imported as it is (not modified). Nothing is drawn
on the pictures except the Natural Earth coastlines (green on True Color as in the operational product, magenta on Ash
RGB because green, red, yellow and blue all carry meaning in the Ash colour key; --no-coastlines for none) and one small
timestamp line: no graticule, streamlines, fire marks or legend.

True Color: the JMA recipe as in the operational product (Rayleigh on by default).
Ash RGB: JMA Himawari Ash RGB Quick Guide, Ver. 1.0 (each channel scaled min..max, clipped, then ** (1/gamma)):
    R  B15 - B13   (12.4 - 10.4 um)   -3.0 .. 7.5 K     gamma 1.0
    G  B13 - B11   (10.4 -  8.6 um)   -1.6 .. 4.9 K     gamma 1.2     (--green b14: B14 - B11, -5.9 .. 5.1 K, gamma 0.85)
    B  B13         (10.4 um)         243.6 .. 303.2 K   gamma 1.0
The guide lists the bands in wavelength order ("B13-B15", "B11-B13"); the arithmetic used here is the longer wavelength
minus the shorter one, which is what makes ash red (12.4 um is warmer than 10.4 um in an ash plume).

    python scripts/adhoc_rgb.py --scan 202609050800 --bbox 101.0,-9.5,106.5,-3.0 --outdir out
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import himawari_truecolor as h  # noqa: E402

# (band_long, band_short, min, max, gamma) for the two difference channels, and the blue channel
ASH = {
    "b13": dict(green=("B13", "B11", -1.6, 4.9, 1.2)),
    "b14": dict(green=("B14", "B11", -5.9, 5.1, 0.85)),
}
ASH_RED = ("B15", "B13", -3.0, 7.5, 1.0)
ASH_BLUE = ("B13", 243.6, 303.2, 1.0)
COAST_RGBA = {"true_color": h.PRODUCTS["true_color"]["coast_rgba"], "ash": (255, 70, 255, 255)}


def smooth(a, sigma: float):
    """Light NaN-safe Gaussian smoothing of a brightness-temperature field (the IR bands have 2 km pixels)."""
    import numpy as np
    from scipy.ndimage import gaussian_filter

    if sigma <= 0:
        return a
    ok = np.isfinite(a)
    num = gaussian_filter(np.where(ok, a, 0.0), sigma)
    den = gaussian_filter(ok.astype("float32"), sigma)
    return np.where(den > 0.05, num / np.maximum(den, 1e-6), np.nan).astype("float32")


def make_ash(files, bbox, resolution, green: str, sigma: float):
    """Ash RGB uint8 (H, W, 3) per the JMA quick guide."""
    import numpy as np

    g_long, g_short, g_min, g_max, g_gamma = ASH[green]["green"]
    need = sorted({ASH_RED[0], ASH_RED[1], g_long, g_short, ASH_BLUE[0]})
    bt = {b: smooth(h.make_bt(files, bbox, resolution, b), sigma) for b in need}
    chans = [
        (bt[ASH_RED[0]] - bt[ASH_RED[1]], ASH_RED[2], ASH_RED[3], ASH_RED[4], f"{ASH_RED[0]}-{ASH_RED[1]}"),
        (bt[g_long] - bt[g_short], g_min, g_max, g_gamma, f"{g_long}-{g_short}"),
        (bt[ASH_BLUE[0]], ASH_BLUE[1], ASH_BLUE[2], ASH_BLUE[3], ASH_BLUE[0]),
    ]
    out = np.empty(chans[0][0].shape + (3,), dtype="float32")
    for i, (a, lo, hi, gamma, name) in enumerate(chans):
        p1, p50, p99 = np.nanpercentile(a, [1, 50, 99])
        h.log(f"  {name} K: p1 {p1:.1f}  median {p50:.1f}  p99 {p99:.1f}  (scaled {lo:g}..{hi:g}, gamma {gamma:g})")
        out[..., i] = np.power(np.nan_to_num(np.clip((a - lo) / (hi - lo), 0.0, 1.0), nan=0.0), 1.0 / gamma)
    return np.round(out * 255.0).astype("uint8")


def stamp(rgb, text: str, out: Path, corner: str, coast=None) -> None:
    """Save ``rgb`` with one small timestamp line in a corner; ``coast`` = None or (bbox, resolution, width_px, rgba)
    adds the coastlines. Nothing else is drawn."""
    from PIL import Image, ImageDraw

    img = Image.fromarray(rgb, "RGB").convert("RGBA")
    if coast:
        h.draw_coastline(img, *coast)
    size = max(12, round(min(rgb.shape[0], 0.56 * rgb.shape[1]) / 48))
    font = h._font(size)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    box = d.textbbox((0, 0), text, font=font)
    pad = size // 2
    bw, bh = box[2] - box[0] + 2 * pad, box[3] - box[1] + 2 * pad
    x0 = pad if corner.endswith("left") else img.width - bw - pad
    y0 = pad if corner.startswith("top") else img.height - bh - pad
    d.rectangle([x0, y0, x0 + bw, y0 + bh], fill=(0, 0, 0, 140))
    d.text((x0 + pad - box[0], y0 + pad - box[1]), text, font=font, fill=(255, 255, 255, 255))
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.alpha_composite(img, layer).convert("RGB").save(out, optimize=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan", required=True, help="Himawari scan time YYYYMMDDHHMM (UTC)")
    ap.add_argument("--bbox", default="101.0,-9.5,106.5,-3.0", help="west,south,east,north (default: Sunda Strait, a little west of Anak Krakatau)")
    ap.add_argument("--coastlines", action=argparse.BooleanOptionalAction, default=True,
                    help="draw the Natural Earth coastlines (default on; green on True Color, magenta on Ash)")
    ap.add_argument("--coast-width", type=float, default=1.5, help="coastline width in output pixels")
    ap.add_argument("--segments", default="5-6")
    ap.add_argument("--resolution", type=float, default=0.005)
    ap.add_argument("--products", default="true_color,ash", help="comma list of true_color, ash")
    ap.add_argument("--rayleigh", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--green", choices=tuple(ASH), default="b13", help="Ash green channel: b13 (8.6 vs 10.4 um) or b14 (8.6 vs 11.2 um)")
    ap.add_argument("--smooth", type=float, default=1.5,
                    help="Gaussian sigma in output pixels applied to the infrared bands (0 = raw 2 km blocks; default 1.5)")
    ap.add_argument("--corner", default="bottom-left", choices=("bottom-left", "bottom-right", "top-left", "top-right"))
    ap.add_argument("--outdir", default="output-adhoc-rgb")
    args = ap.parse_args(argv)

    try:
        bbox = h.parse_bbox(args.bbox)
        segments = h.parse_segments(args.segments)
        scan = h.parse_scan_time(args.scan)
        products = [p.strip() for p in args.products.split(",") if p.strip()]
        if not products or any(p not in ("true_color", "ash") for p in products):
            raise h.UserError("products must be a comma list of true_color and/or ash")
        h.check_segments_cover_bbox(bbox, segments)
        bands = []
        if "true_color" in products:
            bands += list(h.PRODUCTS["true_color"]["bands"])
        if "ash" in products:
            g = ASH[args.green]["green"]
            bands += [ASH_RED[0], ASH_RED[1], g[0], g[1], ASH_BLUE[0]]
        bands = sorted(set(bands))
    except h.UserError as exc:
        h.annotate("error", str(exc))
        return 2

    out = Path(args.outdir)
    work = Path(tempfile.mkdtemp(prefix="adhocrgb_", dir=os.environ.get("RUNNER_TEMP")))
    os.environ["TMPDIR"] = str(work)
    tempfile.tempdir = None
    try:
        from satpy import config as satpy_config

        satpy_config.set(tmp_dir=str(work))
        s3 = h.s3_client()
        try:
            _, keys = h.select_scan(s3, segments, scan, 3, bands)
        except h.UserError as exc:
            h.annotate("error", str(exc))
            return 2
        files = h.download(s3, keys, work / "hsd")
        text = f"{scan:%Y-%m-%d %H:%M} UTC  /  {scan.astimezone(h.SGT):%H:%M} SGT"
        for name in products:
            if name == "true_color":
                prod = h.PRODUCTS["true_color"]
                gammas = tuple(c[4] for c in prod["channels"])
                h.log(f"--- True Color, Rayleigh {'on' if args.rayleigh else 'off'}")
                rgb = h.make_rgb(files, bbox, args.resolution, prod, gammas, args.rayleigh)
            else:
                h.log(f"--- Ash RGB (JMA quick guide), green = {args.green}, smoothing sigma {args.smooth:g} px")
                rgb = make_ash(files, bbox, args.resolution, args.green, args.smooth)
            png = out / f"{scan:%Y%m%d_%H%M}_{name}.png"
            stamp(rgb, text, png, args.corner,
                  (bbox, args.resolution, args.coast_width, COAST_RGBA[name]) if args.coastlines else None)
            h.log(f"Wrote {png} ({png.stat().st_size / 1e6:.1f} MB, {rgb.shape[1]}x{rgb.shape[0]})")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
