#!/usr/bin/env python3
"""Build a Himawari-9 True Color RGB image (JMA recipe) for a lat/lon bbox.

Data come from the public NOAA NODD bucket (anonymous access):
    s3://noaa-himawari9/AHI-L1b-FLDK/YYYY/MM/DD/HHMM/

Recipe (JMA True Color RGB Quick Guide):
    R = B03 (0.64 um), G = B02 (0.51 um), B = B01 (0.47 um)
    each band: reflectance 0-100 %, clip, /100, ** (1/gamma)

The Satpy built-in ``true_color`` composite is deliberately NOT used, because it
applies extra corrections.  The bands are loaded, resampled and stacked by hand.

Exit status: 0 on success or on a deliberate night-time skip (see the ``status``
field of the --json-out file), 2 for expected failures (missing data, bbox outside
the downloaded segments, bad arguments), 1 for anything unexpected.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling modules, wherever it is run from
import ecmwf_winds  # noqa: E402

BUCKET = "noaa-himawari9"
PRODUCT = "AHI-L1b-FLDK"
SATELLITE = "H09"
BANDS = ("B01", "B02", "B03")
N_SEGMENTS = 10  # full disk is split into 10 equal N-S segments, 01 = north
# Full-disk extent in geostationary projection y (metres, +north).  Verified
# against the area extents Satpy reports for real data (see check_loaded_coverage).
FD_HALF_HEIGHT_M = 5.5e6
GEOS_CRS = dict(proj="geos", h=35785863, lon_0=140.7, sweep="y", a=6378137, b=6356752.3)

COASTLINE_ZIP = Path(__file__).resolve().parent.parent / "data" / "ne_10m_coastline.zip"
COAST_RGBA = (40, 255, 70, 255)  # green

SGT = dt.timezone(dt.timedelta(hours=8))
KEY_RE = re.compile(
    r"HS_H09_(?P<ymd>\d{8})_(?P<hm>\d{4})_B(?P<band>\d{2})_FLDK_R(?P<res>\d{2})"
    r"_S(?P<seg>\d{2})(?P<nseg>\d{2})\.DAT(?:\.bz2)?$"
)


class UserError(Exception):
    """An expected failure with a message meant to be read by the user."""


def log(msg: str) -> None:
    print(msg, flush=True)


def annotate(level: str, msg: str) -> None:
    """Print a message, plus a GitHub Actions annotation when running there."""
    print(f"{level.upper()}: {msg}", flush=True)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level}::{msg.replace(chr(10), ' ')}", flush=True)


# --------------------------------------------------------------------------- args
def parse_bbox(text: str) -> tuple[float, float, float, float]:
    try:
        w, s, e, n = (float(v) for v in text.split(","))
    except ValueError:
        raise UserError(f"bbox must be 'west,south,east,north' in degrees, got {text!r}")
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise UserError(f"bbox {text!r} is not valid: need west<east, south<north")
    return w, s, e, n


def parse_segments(text: str) -> list[int]:
    segs: set[int] = set()
    try:
        for part in text.replace(" ", "").split(","):
            if "-" in part:
                a, b = part.split("-")
                segs.update(range(int(a), int(b) + 1))
            else:
                segs.add(int(part))
    except ValueError:
        raise UserError(f"segments must look like '5-6' or '5,6', got {text!r}")
    if not segs or min(segs) < 1 or max(segs) > N_SEGMENTS:
        raise UserError(f"segments must be between 1 and {N_SEGMENTS}, got {text!r}")
    return sorted(segs)


def parse_scan_time(text: str) -> dt.datetime:
    try:
        t = dt.datetime.strptime(text.strip(), "%Y%m%d%H%M").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        raise UserError(f"datetime_utc must be YYYYMMDDHHMM (UTC), got {text!r}")
    if t.minute % 10:
        raise UserError(f"Himawari full-disk scans are every 10 min (minute ends in 0), got {text!r}")
    return t


# ------------------------------------------------------------------------- bucket
def s3_client():
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config

    return boto3.client(
        "s3",
        region_name="us-east-1",
        config=Config(signature_version=UNSIGNED, retries={"max_attempts": 5, "mode": "standard"}),
    )


def scan_prefix(t: dt.datetime) -> str:
    return f"{PRODUCT}/{t:%Y/%m/%d/%H%M}/"


def list_scan(s3, t: dt.datetime) -> dict[tuple[str, int], str]:
    """Return {(band, segment): key} for B01-B03 of scan ``t`` that exist in the bucket."""
    found: dict[tuple[str, int], str] = {}
    prefix = scan_prefix(t)
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix)
    for page in pages:
        for obj in page.get("Contents", []):
            m = KEY_RE.search(obj["Key"])
            if not m or f"B{m['band']}" not in BANDS or int(m["nseg"]) != N_SEGMENTS:
                continue
            if obj["Size"] == 0:
                continue
            found[(f"B{m['band']}", int(m["seg"]))] = obj["Key"]
    return found


def missing_files(found, segments) -> list[str]:
    return [f"{b} S{seg:02d}" for b in BANDS for seg in segments if (b, seg) not in found]


def only_segments(found, segments):
    return {k: v for k, v in found.items() if k[1] in segments}


def select_scan(s3, segments, requested: dt.datetime | None, lookback_h: float):
    """Pick the scan to process; returns (scan_time, {(band, seg): key})."""
    if requested is not None:
        found = list_scan(s3, requested)
        if not found:
            raise UserError(
                f"No B01/B02/B03 files found in s3://{BUCKET}/{scan_prefix(requested)}\n"
                "Check the date/time (UTC, YYYYMMDDHHMM): the scan may not exist yet "
                "(data lags real time by ~20-30 min) or may be outside the archive."
                + ("\nNote: the 0240 and 1440 UTC full-disk scans are absent from the bucket every day "
                   "(seen daily; presumably satellite housekeeping). Pick the scan before or after."
                   if f"{requested:%H%M}" in ("0240", "1440") else "")
            )
        miss = missing_files(found, segments)
        if miss:
            raise UserError(
                f"Scan {requested:%Y-%m-%d %H%M} UTC is incomplete. Missing: {', '.join(miss)}\n"
                f"(listed s3://{BUCKET}/{scan_prefix(requested)}). Try the previous scan."
            )
        return requested, only_segments(found, segments)

    now = dt.datetime.now(dt.timezone.utc)
    t = now.replace(minute=now.minute // 10 * 10, second=0, microsecond=0)
    oldest = now - dt.timedelta(hours=lookback_h)
    newest_report = None
    n_checked = 0
    while t >= oldest:
        found = list_scan(s3, t)
        n_checked += 1
        miss = missing_files(found, segments)
        if not miss:
            log(f"Newest complete scan: {t:%Y-%m-%d %H%M} UTC (checked {n_checked} scan(s))")
            return t, only_segments(found, segments)
        if found and newest_report is None:
            newest_report = f"newest partial scan {t:%Y%m%d%H%M} is missing {', '.join(miss)}"
        t -= dt.timedelta(minutes=10)
    raise UserError(
        f"No complete scan with {'/'.join(BANDS)} segments {segments} in the last "
        f"{lookback_h:g} h of s3://{BUCKET}/{PRODUCT}/ ({n_checked} scans checked"
        + (f"; {newest_report}" if newest_report else "; none had any B01-B03 files")
        + ").\nThe archive may be lagging; retry in a few minutes or pass datetime_utc."
    )


def download(s3, keys: dict[tuple[str, int], str], dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)

    def fetch(key: str) -> Path:
        path = dest / key.rsplit("/", 1)[1]
        s3.download_file(BUCKET, key, str(path))
        return path

    with ThreadPoolExecutor(max_workers=6) as pool:
        paths = list(pool.map(fetch, keys.values()))
    mb = sum(p.stat().st_size for p in paths) / 1e6
    log(f"Downloaded {len(paths)} files ({mb:.0f} MB)")
    return sorted(paths)


# ---------------------------------------------------------------------- geometry
def bbox_boundary(bbox, n: int = 60):
    """Points along the bbox edges (lon, lat lists)."""
    import numpy as np

    w, s, e, n_ = bbox
    lons = np.concatenate([np.linspace(w, e, n), np.full(n, e), np.linspace(e, w, n), np.full(n, w)])
    lats = np.concatenate([np.full(n, s), np.linspace(s, n_, n), np.full(n, n_), np.linspace(n_, s, n)])
    return lons, lats


def needed_segments(bbox) -> list[int]:
    """Full-disk segments (1-10) that the bbox touches, from projection geometry."""
    import numpy as np
    from pyproj import CRS, Transformer

    lons, lats = bbox_boundary(bbox)
    # also sample the interior so a box straddling a segment line is never missed
    gx, gy = np.meshgrid(np.linspace(bbox[0], bbox[2], 25), np.linspace(bbox[1], bbox[3], 25))
    lons = np.concatenate([lons, gx.ravel()])
    lats = np.concatenate([lats, gy.ravel()])
    tr = Transformer.from_crs("EPSG:4326", CRS.from_dict(GEOS_CRS), always_xy=True)
    x, y = tr.transform(lons, lats)
    if not (np.isfinite(x).all() and np.isfinite(y).all()):
        raise UserError(f"bbox {bbox} is not entirely visible from Himawari-9 (off the full disk).")
    seg_h = 2 * FD_HALF_HEIGHT_M / N_SEGMENTS
    idx = np.floor((FD_HALF_HEIGHT_M - y) / seg_h).astype(int) + 1
    if idx.min() < 1 or idx.max() > N_SEGMENTS:
        raise UserError(f"bbox {bbox} extends beyond the Himawari-9 full disk.")
    return list(range(int(idx.min()), int(idx.max()) + 1))


def check_segments_cover_bbox(bbox, segments: list[int]) -> None:
    need = needed_segments(bbox)
    missing = [s for s in need if s not in segments]
    if missing:
        raise UserError(
            f"bbox {','.join(f'{v:g}' for v in bbox)} needs segments {need[0]}-{need[-1]} "
            f"but only segments {segments} were requested (missing: {missing}). "
            f"Set segments to '{need[0]}-{need[-1]}' or shrink the bbox. "
            "Refusing to produce a cropped or blank image."
        )
    log(f"Bbox needs segments {need}; requested {segments} - OK")


def check_loaded_coverage(area, bbox) -> None:
    """Belt and braces: verify against the real area Satpy built from the files."""
    import numpy as np
    from pyproj import Transformer

    lons, lats = bbox_boundary(bbox)
    tr = Transformer.from_crs("EPSG:4326", area.crs, always_xy=True)
    x, y = tr.transform(lons, lats)
    x0, y0, x1, y1 = area.area_extent
    px = abs(area.pixel_size_x)
    inside = np.isfinite(x) & np.isfinite(y) & (x >= x0 - px) & (x <= x1 + px) & (y >= y0 - px) & (y <= y1 + px)
    if not inside.all():
        raise UserError(
            "The downloaded segments do not cover the whole bbox "
            f"(loaded area y-extent {y0:.0f}..{y1:.0f} m; bbox needs "
            f"{np.nanmin(y):.0f}..{np.nanmax(y):.0f} m). Add the neighbouring segments."
        )


def solar_zenith(t: dt.datetime, lon: float, lat: float) -> float:
    from pyorbital.astronomy import sun_zenith_angle

    return float(sun_zenith_angle(t.replace(tzinfo=None), lon, lat))


# ------------------------------------------------------------------- processing
def make_rgb(files, bbox, resolution, gamma, rayleigh):
    """Return a uint8 (H, W, 3) array for the bbox."""
    import dask
    import numpy as np
    from pyresample.geometry import AreaDefinition
    from satpy import Scene

    dask.config.set({"array.chunk-size": "64MiB"})

    w, s, e, n = bbox
    width = int(round((e - w) / resolution))
    height = int(round((n - s) / resolution))
    area = AreaDefinition(
        "bbox", "bbox lat/lon", "bbox",
        {"proj": "longlat", "datum": "WGS84", "no_defs": None},
        width, height, (w, n - height * resolution, w + width * resolution, n),
    )
    log(f"Target grid: {width} x {height} px at {resolution}\N{DEGREE SIGN}")

    scn = Scene(filenames=[str(f) for f in files], reader="ahi_hsd")
    if rayleigh:
        # The Rayleigh modifier requires sun-zenith-corrected reflectance as input.
        scn.load(list(BANDS), calibration="reflectance", modifiers=("sunz_corrected", "rayleigh_corrected"))
    else:
        scn.load(list(BANDS), calibration="reflectance")

    check_loaded_coverage(scn[BANDS[0]].attrs["area"], bbox)

    resampled = scn.resample(area, resampler="nearest")
    # R = B03, G = B02, B = B01
    chans = [resampled[b].data for b in ("B03", "B02", "B01")]
    rgb = np.stack([np.asarray(dask.compute(c)[0], dtype="float32") for c in chans], axis=-1)

    bad = float(np.isnan(rgb).any(axis=-1).mean())
    if bad > 0.01:
        annotate("warning", f"{bad:.1%} of pixels have no data (shown black)")
    rgb = np.nan_to_num(np.clip(rgb, 0.0, 100.0), nan=0.0) / 100.0
    rgb = np.power(rgb, 1.0 / gamma)
    return np.round(rgb * 255.0).astype("uint8")


def _font(size: int):
    from PIL import ImageFont

    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "LiberationSans-Bold.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)  # Pillow >= 10.1: scalable built-in font
    except TypeError:
        return ImageFont.load_default()


def draw_coastline(img, bbox, resolution: float, width_px: float):
    """Draw Natural Earth 1:10m coastlines in green onto ``img`` (an RGBA lat/lon-grid image)."""
    import shapefile  # pyshp
    from PIL import Image, ImageDraw

    w, s, e, n = bbox
    ss = 2  # draw at 2x and shrink for smooth, anti-aliased lines
    mask = Image.new("L", (img.width * ss, img.height * ss), 0)
    draw = ImageDraw.Draw(mask)
    lw = max(1, round(width_px * ss))
    n_parts = 0
    with shapefile.Reader(str(COASTLINE_ZIP)) as reader:
        for shp in reader.iterShapes():
            x0, y0, x1, y1 = shp.bbox
            if x1 < w or x0 > e or y1 < s or y0 > n:
                continue
            parts = list(shp.parts) + [len(shp.points)]
            for a, b in zip(parts[:-1], parts[1:]):
                # pixel-edge coordinates -> PIL pixel-centre coordinates, at ss x scale
                xy = [((lon - w) / resolution * ss - 0.5, (n - lat) / resolution * ss - 0.5)
                      for lon, lat in shp.points[a:b]]
                if len(xy) > 1:
                    draw.line(xy, fill=255, width=lw, joint="curve")
                    n_parts += 1
    mask = mask.resize(img.size, Image.LANCZOS)
    img.paste(Image.new("RGBA", img.size, COAST_RGBA), mask=mask)
    log(f"Drew {n_parts} coastline segments (Natural Earth 1:10m)")
    return img


def overlay_and_save(rgb, scan: dt.datetime, gamma: float, rayleigh: bool, out: Path,
                     bbox=None, resolution=None, coast_width: float = 0.0,
                     wind=None, wind_spacing: float = 45.0) -> None:
    from PIL import Image, ImageDraw

    img = Image.fromarray(rgb, "RGB").convert("RGBA")
    if wind is not None:  # layer order: image, streamlines, coastlines, text
        img = ecmwf_winds.draw_streamlines(img, wind, bbox, resolution, wind_spacing)
    if coast_width > 0:
        draw_coastline(img, bbox, resolution, coast_width)
    h = rgb.shape[0]
    size = max(12, round(h / 48))
    big, small = _font(size), _font(round(size * 0.8))
    sgt = scan.astimezone(SGT)
    white = (255, 255, 255, 255)
    lines = [
        ("Himawari-9 AHI, JMA / NOAA NODD", big, white),
        (f"{scan:%Y-%m-%d %H:%M} UTC  /  {sgt:%Y-%m-%d %H:%M} SGT", big, white),
        (f"True Color RGB (JMA recipe)  gamma {gamma:g}  Rayleigh {'on' if rayleigh else 'off'}", small, white),
    ]
    if wind is not None:  # the line doubles as the legend: same colour as the streamlines
        lines.append((f"Streamlines: {wind.describe()}", small, (*ecmwf_winds.LINE_RGB, 255)))
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    pad, gap = size // 2, size // 4
    boxes = [d.textbbox((0, 0), t, font=f) for t, f, _ in lines]
    bw = max(b[2] - b[0] for b in boxes) + 2 * pad
    bh = sum(b[3] - b[1] for b in boxes) + gap * (len(lines) - 1) + 2 * pad
    x0, y0 = pad, img.height - bh - pad
    d.rectangle([x0, y0, x0 + bw, y0 + bh], fill=(0, 0, 0, 140))
    y = y0 + pad
    for (text, font, fill), b in zip(lines, boxes):
        d.text((x0 + pad - b[0], y - b[1]), text, font=font, fill=fill)
        y += b[3] - b[1] + gap
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.alpha_composite(img, layer).convert("RGB").save(out, optimize=True)


# --------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--datetime-utc", default="", help="scan time YYYYMMDDHHMM (UTC); blank = newest complete scan")
    p.add_argument("--bbox", default="94.5,-6.5,119.5,7.5", help="west,south,east,north in degrees")
    p.add_argument("--gamma", type=float, default=1.7, help="gamma (1.7 default; 2.0 can help with haze)")
    p.add_argument("--segments", default="5-6", help="full-disk segments to download, e.g. 5-6")
    p.add_argument("--resolution", type=float, default=0.01, help="output pixel size in degrees (0.01 ~ 1 km)")
    p.add_argument("--rayleigh", action=argparse.BooleanOptionalAction, default=True,
                   help="apply Satpy Rayleigh correction (default on; --no-rayleigh for the plain JMA recipe)")
    p.add_argument("--coastlines", action=argparse.BooleanOptionalAction, default=True,
                   help="draw green Natural Earth 1:10m coastlines (default on)")
    p.add_argument("--coast-width", type=float, default=1.5, help="coastline width in output pixels")
    p.add_argument("--winds", action=argparse.BooleanOptionalAction, default=True,
                   help="overlay ECMWF IFS wind streamlines (default on; skipped with a warning if ECMWF data fail)")
    p.add_argument("--wind-level", type=int, default=925, help="pressure level in hPa for the streamlines")
    p.add_argument("--wind-spacing", type=float, default=45.0, help="approximate streamline spacing in output pixels")
    p.add_argument("--outdir", default="output", help="directory for the PNG")
    p.add_argument("--workdir", default=None, help="scratch directory for HSD files (deleted afterwards)")
    p.add_argument("--lookback-hours", type=float, default=3.0, help="how far back to look for the newest scan")
    p.add_argument("--check-only", action="store_true", help="only list the bucket and run the checks")
    p.add_argument("--json-out", default=None, help="write a JSON result file (status, scan time, png path)")
    return p


def run(args) -> dict:
    bbox = parse_bbox(args.bbox)
    segments = parse_segments(args.segments)
    if not 0.001 <= args.resolution <= 1:
        raise UserError(f"resolution must be between 0.001 and 1 degrees, got {args.resolution}")
    if not 0.2 <= args.gamma <= 5:
        raise UserError(f"gamma must be between 0.2 and 5, got {args.gamma}")
    if args.winds and args.wind_level not in ecmwf_winds.PRESSURE_LEVELS:
        raise UserError(f"wind level must be one of {ecmwf_winds.PRESSURE_LEVELS} hPa, got {args.wind_level}")
    if not 10 <= args.wind_spacing <= 400:
        raise UserError(f"wind spacing must be between 10 and 400 px, got {args.wind_spacing}")
    requested = parse_scan_time(args.datetime_utc) if args.datetime_utc.strip() else None

    check_segments_cover_bbox(bbox, segments)

    s3 = s3_client()
    scan, keys = select_scan(s3, segments, requested, args.lookback_hours)
    log(f"Scan {scan:%Y-%m-%d %H:%M} UTC: all {len(keys)} files present "
        f"({', '.join(BANDS)} x segments {segments})")

    result = {
        "status": "ok",
        "scan_utc": f"{scan:%Y%m%d_%H%M}",
        "scan_utc_iso": scan.strftime("%Y-%m-%d %H:%M UTC"),
        "scan_sgt_iso": scan.astimezone(SGT).strftime("%Y-%m-%d %H:%M SGT"),
        "bbox": list(bbox),
        "segments": segments,
        "png": None,
    }

    clon, clat = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    sza = solar_zenith(scan, clon, clat)
    result["sun_zenith_centre"] = round(sza, 1)
    log(f"Solar zenith angle at bbox centre ({clon:g}, {clat:g}): {sza:.1f} deg")
    if sza >= 90:
        result["status"] = "skipped_night"
        annotate("notice", f"Skipped: sun is below the horizon at the bbox centre for scan "
                 f"{result['scan_utc_iso']} ({result['scan_sgt_iso']}), solar zenith {sza:.1f} deg. "
                 "True Color is daytime only.")
        return result
    corners = [solar_zenith(scan, lon, lat) for lon, lat in ((bbox[0], bbox[1]), (bbox[2], bbox[3]))]
    if max(corners) >= 90:
        annotate("warning", f"Sun is below the horizon at part of the bbox "
                 f"(SW/NE corner zenith {corners[0]:.0f}/{corners[1]:.0f} deg): that part will be dark.")

    if args.check_only:
        result["status"] = "check_only"
        return result

    work = Path(tempfile.mkdtemp(prefix="himawari_", dir=args.workdir))
    # Satpy unpacks .bz2 files into the temp dir; keep that inside workdir so it is removed too.
    old_tmp = os.environ.get("TMPDIR")
    os.environ["TMPDIR"] = str(work)
    tempfile.tempdir = None
    try:
        files = download(s3, keys, work / "hsd")
        rgb = make_rgb(files, bbox, args.resolution, args.gamma, args.rayleigh)
        out = Path(args.outdir) / f"{scan:%Y%m%d_%H%M}.png"
        wind = None
        result["winds"] = "off"
        if args.winds:
            # The satellite image is the main product: a wind problem must not lose it.
            try:
                wind = ecmwf_winds.fetch_winds(s3, scan, args.wind_level, bbox)
                result["winds"] = wind.describe()
                log(f"Winds: {wind.describe()}")
            except Exception as exc:  # noqa: BLE001 - deliberately broad, see above
                result["winds"] = f"skipped: {exc}"
                annotate("warning", f"Wind streamlines skipped ({type(exc).__name__}: {exc}); "
                         "the image is published without them.")
        try:
            overlay_and_save(rgb, scan, args.gamma, args.rayleigh, out, bbox, args.resolution,
                             args.coast_width if args.coastlines else 0.0, wind, args.wind_spacing)
        except Exception as exc:  # noqa: BLE001
            if wind is None:
                raise
            result["winds"] = f"skipped: drawing failed: {exc}"
            annotate("warning", f"Drawing wind streamlines failed ({type(exc).__name__}: {exc}); "
                     "publishing without them.")
            overlay_and_save(rgb, scan, args.gamma, args.rayleigh, out, bbox, args.resolution,
                             args.coast_width if args.coastlines else 0.0)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if old_tmp is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = old_tmp
        tempfile.tempdir = None
    log(f"Deleted downloaded HSD files ({work})")
    result["png"] = str(out)
    result["size_px"] = [int(rgb.shape[1]), int(rgb.shape[0])]
    log(f"Wrote {out} ({out.stat().st_size / 1e6:.1f} MB, {rgb.shape[1]}x{rgb.shape[0]})")
    return result


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
        code = 0
    except UserError as exc:
        annotate("error", str(exc))
        result, code = {"status": "error", "message": str(exc)}, 2
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(result, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
