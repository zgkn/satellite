#!/usr/bin/env python3
"""Build Himawari-9 RGB images (JMA / NOAA-CIRA recipes) for a lat/lon bbox.

Data come from the public NOAA NODD bucket (anonymous access):
    s3://noaa-himawari9/AHI-L1b-FLDK/YYYY/MM/DD/HHMM/

Products (each channel: scaled min..max, clip, ** (1/gamma)):
    true_color          JMA True Color RGB Quick Guide: R = B03 (0.64 um), G = B02 (0.51 um),
                        B = B01 (0.47 um), reflectance 0-100 %, gamma 1.7
    day_fire            R = B07 (3.9 um brightness temperature, 27..60 C, gamma 0.4), G = B04, B = B03
                        (NOAA/CIRA VIIRS Day Fire RGB recipe on AHI bands: fire red, burn scar reddish
                        brown, smoke blue, clouds cyan, vegetation green; B07 is 2 km, VIIRS I4 375 m;
                        the guide's red minimum is 0 C, raised to 27 C for the tropics; cold cloud, B13 below
                        ~290 K, is blended to grey so it stands apart from smoke, and the 0.86/0.64 um
                        channels are divided by cos(sun zenith), capped at 80 deg, so brightness does not
                        follow the sun - all three are additions to the guide)
    ir_enhanced         True Color with a translucent colour overlay of the 10.4 um band (B13, brightness
                        temperature) in the CIMSS Clean IR Window colours (scripts/ir_scale.py); where the sun is
                        down it is IR only: grey for warm surfaces, opaque colour for cold cloud. The two are
                        blended per pixel by sun angle, so dawn and dusk fade smoothly. Runs day and night.

The Satpy built-in composites are deliberately NOT used, because they apply extra
corrections.  The bands are loaded, resampled and stacked by hand.  The reflectance is
Satpy's plain albedo (radiance x the file's albedo coefficient, no sun-angle division),
as in the JMA guide.

Exit status: 0 on success or on a deliberate night-time skip (see the ``status``
field of the --json-out file; at night only ir_enhanced still runs), 2 for expected failures (missing data, bbox outside
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
import ir_scale  # noqa: E402

BUCKET = "noaa-himawari9"
PRODUCT = "AHI-L1b-FLDK"
SATELLITE = "H09"
# channels = (R, G, B), each (band, calibration, min, max, gamma): reflectance is in %, brightness
# temperature in K; the value is scaled min..max -> 0..1 and then raised to 1/gamma.
# uniform_gamma = whether --gamma may override it (False when the recipe has different gammas per channel).
# rayleigh = whether the --rayleigh option applies; subdir = folder below --outdir ("" keeps the original
# True Color file names/links unchanged); coast_rgba = coastline colour, chosen to be absent from the
# product's own colour key (green would clash with the vegetation in Day Fire).
_REFL, _BT = "reflectance", "brightness_temperature"
PRODUCTS = {
    "true_color": dict(
        label="True Color RGB", recipe="JMA recipe", uniform_gamma=True, rayleigh=True,
        channels=(("B03", _REFL, 0, 100, 1.7), ("B02", _REFL, 0, 100, 1.7), ("B01", _REFL, 0, 100, 1.7)),
        # fire_overlay: pixels with a hot 3.9 um signal are marked red on top of the picture (see make_fire_mask).
        # A pixel is a fire when all hold: B07 above min_k, B07 more than anomaly_k above the median of its
        # surroundings (window_deg wide, so hot bare ground and warm land are not marked), B13 at least cold_k
        # (sunlit cold cloud top reflects 3.9 um and looks hot), and B07 - B13 at least min_diff_k. Tested on
        # one fire-season scan per region: the 325 K / +8 K rule marked the cores of plumes seen in True Color
        # (Sumatra 21 Oct 08:00 UTC: 435 px, 53 clusters, 4 px over sea; Mekong 15 Mar 06:00 UTC: 330 px, 29
        # clusters; 27 Oct 05:00 UTC: none). An addition: not an official fire product.
        fire_overlay=dict(bands=("B07", "B13"), min_k=325.0, anomaly_k=8.0, cold_k=280.0, min_diff_k=15.0,
                          window_deg=0.15),
        subdir="", coast_rgba=(40, 255, 70, 255)),
    # NOAA/CIRA "VIIRS Day Fire RGB" quick guide, with AHI band 7 (3.9 um, 2 km) for VIIRS 3.7 um (375 m).
    # The guide scales the red channel 0..60 C; here the minimum is raised to 27 C (300.15 K) because tropical
    # sea and land sit at ~27 C and above at 3.9 um, which turned all water dark red (tested on a real scan:
    # sea red 35 -> 0, hotspots unchanged). Everything else is as in the guide.
    # cloud_cue: smoke and cloud come out the same teal hue over vegetation, so pixels colder than `warm` K at
    # 10.4 um (B13) are blended to grey, fully by `cold` K. Clouds with tops above ~1.5-3 km are cold; thin
    # low-level smoke is as warm as the ground. This is an addition to the guide, not part of it.
    "day_fire": dict(
        label="Day Fire RGB", recipe="NOAA/CIRA recipe, AHI bands", uniform_gamma=False, rayleigh=False,
        channels=(("B07", _BT, 300.15, 333.15, 0.4), ("B04", _REFL, 0, 100, 1.0), ("B03", _REFL, 0, 100, 1.0)),
        cloud_cue=dict(band="B13", warm=290.0, cold=280.0),
        # sun_correction: plain albedo scales with cos(sun zenith), so the picture dims through the afternoon
        # (cos 0.97 at 12:00 SGT, 0.62 at 16:00 at the centre of the default box, 0.45 at its east edge). The
        # reflective channels are divided by cos(zenith) per pixel, capped at `limit` degrees (factor <= 5.8)
        # so noise near the horizon is not blown up. Also an addition to the guide, whose examples are near noon.
        sun_correction=dict(limit=80.0),
        subdir="dayfire", coast_rgba=(255, 60, 255, 255)),
    # True Color (channels = its recipe) + B13 10.4 um brightness temperature (extra_bands). ir: built by
    # make_ir_enhanced, not make_rgb. night_ok: still runs when the sun is down at the bbox centre (True Color
    # bands are then only downloaded if some part of the box is sunlit). wind_rgb: white streamlines, as orange
    # is lost against the red/yellow of cold cloud. The coastline stays green like True Color.
    "ir_enhanced": dict(
        label="IR Enhanced", recipe="True Color + 10.4 um IR", uniform_gamma=True, rayleigh=True,
        channels=(("B03", _REFL, 0, 100, 1.7), ("B02", _REFL, 0, 100, 1.7), ("B01", _REFL, 0, 100, 1.7)),
        extra_bands=("B13",), ir=True, night_ok=True,
        subdir="ir", coast_rgba=(40, 255, 70, 255), wind_rgb=(255, 255, 255)),
}
for _p in PRODUCTS.values():
    _p["bands"] = tuple(c[0] for c in _p["channels"])  # (R, G, B) bands
# Regions: default bbox and segments, the folder below --outdir ("" = the original top-level layout, so the
# Sumatra-Borneo links keep working) and the caption corner that keeps the box off the land that matters.
# Mekong: the caption sits top-right (southern China), as bottom-right would cover the Mekong delta and
# bottom-left southern Thailand.
REGIONS = {
    "sumatra_borneo": dict(label="Sumatra, Malay Peninsula, Singapore and Borneo", bbox="94.5,-6.5,119.5,7.5",
                           segments="5-6", subdir="", caption_corner="bottom-right"),
    "mekong": dict(label="Mekong sub-region", bbox="92,6,110.5,29", segments="3-5", subdir="mekong",
                   caption_corner="top-right"),
}
N_SEGMENTS = 10  # full disk is split into 10 equal N-S segments, 01 = north
# Full-disk extent in geostationary projection y (metres, +north).  Verified
# against the area extents Satpy reports for real data (see check_loaded_coverage).
FD_HALF_HEIGHT_M = 5.5e6
GEOS_CRS = dict(proj="geos", h=35785863, lon_0=140.7, sweep="y", a=6378137, b=6356752.3)

COASTLINE_ZIP = Path(__file__).resolve().parent.parent / "data" / "ne_10m_coastline.zip"

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


def list_scan(s3, t: dt.datetime, bands) -> dict[tuple[str, int], str]:
    """Return {(band, segment): key} for ``bands`` of scan ``t`` that exist in the bucket."""
    found: dict[tuple[str, int], str] = {}
    prefix = scan_prefix(t)
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix)
    for page in pages:
        for obj in page.get("Contents", []):
            m = KEY_RE.search(obj["Key"])
            if not m or f"B{m['band']}" not in bands or int(m["nseg"]) != N_SEGMENTS:
                continue
            if obj["Size"] == 0:
                continue
            found[(f"B{m['band']}", int(m["seg"]))] = obj["Key"]
    return found


def missing_files(found, segments, bands) -> list[str]:
    return [f"{b} S{seg:02d}" for b in bands for seg in segments if (b, seg) not in found]


def only_segments(found, segments):
    return {k: v for k, v in found.items() if k[1] in segments}


def select_scan(s3, segments, requested: dt.datetime | None, lookback_h: float, bands):
    """Pick the scan to process; returns (scan_time, {(band, seg): key})."""
    if requested is not None:
        found = list_scan(s3, requested, bands)
        if not found:
            raise UserError(
                f"No {'/'.join(bands)} files found in s3://{BUCKET}/{scan_prefix(requested)}\n"
                "Check the date/time (UTC, YYYYMMDDHHMM): the scan may not exist yet "
                "(data lags real time by ~20-30 min) or may be outside the archive."
                + ("\nNote: the 0240 and 1440 UTC full-disk scans are absent from the bucket every day "
                   "(seen daily; presumably satellite housekeeping). Pick the scan before or after."
                   if f"{requested:%H%M}" in ("0240", "1440") else "")
            )
        miss = missing_files(found, segments, bands)
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
        found = list_scan(s3, t, bands)
        n_checked += 1
        miss = missing_files(found, segments, bands)
        if not miss:
            log(f"Newest complete scan: {t:%Y-%m-%d %H%M} UTC (checked {n_checked} scan(s))")
            return t, only_segments(found, segments)
        if found and newest_report is None:
            newest_report = f"newest partial scan {t:%Y%m%d%H%M} is missing {', '.join(miss)}"
        t -= dt.timedelta(minutes=10)
    raise UserError(
        f"No complete scan with {'/'.join(bands)} segments {segments} in the last "
        f"{lookback_h:g} h of s3://{BUCKET}/{PRODUCT}/ ({n_checked} scans checked"
        + (f"; {newest_report}" if newest_report else f"; none had any {'/'.join(bands)} files")
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
def band_of(path: Path) -> str | None:
    m = KEY_RE.search(Path(path).name)
    return f"B{m['band']}" if m else None


def target_area(bbox, resolution):
    """The lat/lon pyresample area (and its width, height) that every product is resampled to."""
    from pyresample.geometry import AreaDefinition

    w, s, e, n = bbox
    width = int(round((e - w) / resolution))
    height = int(round((n - s) / resolution))
    area = AreaDefinition(
        "bbox", "bbox lat/lon", "bbox",
        {"proj": "longlat", "datum": "WGS84", "no_defs": None},
        width, height, (w, n - height * resolution, w + width * resolution, n),
    )
    return area, width, height


def zenith_grid(scan: dt.datetime, bbox, resolution, width, height):
    """Sun zenith angle (degrees) at the centre of every output pixel at ``scan``."""
    import numpy as np
    from pyorbital.astronomy import sun_zenith_angle

    w, _, _, n = bbox
    lons = np.broadcast_to((w + (np.arange(width) + 0.5) * resolution)[None, :], (height, width))
    lats = np.broadcast_to((n - (np.arange(height) + 0.5) * resolution)[:, None], (height, width))
    return sun_zenith_angle(scan.replace(tzinfo=None), lons, lats)


def any_sunlit(scan: dt.datetime, bbox, n: int = 25) -> bool:
    """True if the sun is above the horizon anywhere in the bbox (coarse n x n sample)."""
    import numpy as np
    from pyorbital.astronomy import sun_zenith_angle

    gx, gy = np.meshgrid(np.linspace(bbox[0], bbox[2], n), np.linspace(bbox[1], bbox[3], n))
    return bool((sun_zenith_angle(scan.replace(tzinfo=None), gx, gy) < 90.0).any())


def make_rgb(files, bbox, resolution, product, gammas, rayleigh, cue=None, sun=None):
    """Return a uint8 (H, W, 3) array for ``product`` (a PRODUCTS entry) over the bbox.

    ``gammas`` is the (R, G, B) gamma triple.  ``cue`` is None or (band, warm_K, cold_K): pixels whose
    brightness temperature in ``band`` is below ``warm_K`` are blended to grey, fully grey at ``cold_K``.
    ``sun`` is None or (scan_time, limit_deg): reflectance channels are divided by cos(sun zenith) per
    pixel, with the zenith capped at ``limit_deg``.
    """
    import dask
    import numpy as np
    from satpy import Scene

    dask.config.set({"array.chunk-size": "64MiB"})

    area, width, height = target_area(bbox, resolution)
    log(f"Target grid: {width} x {height} px at {resolution}\N{DEGREE SIGN}")

    bands = product["bands"]  # (R, G, B)
    channels = product["channels"]
    # Only this product's files, so each Scene decompresses just what it needs.
    wanted = set(bands) | ({cue[0]} if cue else set())
    scn = Scene(filenames=[str(f) for f in files if band_of(f) in wanted], reader="ahi_hsd")
    if rayleigh:
        # The Rayleigh modifier requires sun-zenith-corrected reflectance as input.
        scn.load(list(bands), calibration="reflectance", modifiers=("sunz_corrected", "rayleigh_corrected"))
    else:
        for cal in dict.fromkeys(c[1] for c in channels):  # one load per calibration, in channel order
            scn.load([c[0] for c in channels if c[1] == cal], calibration=cal)
    if cue:
        scn.load([cue[0]], calibration=_BT)

    check_loaded_coverage(scn[bands[0]].attrs["area"], bbox)

    resampled = scn.resample(area, resampler="nearest")
    chans = [resampled[b].data for b in bands]
    rgb = np.stack([np.asarray(dask.compute(c)[0], dtype="float32") for c in chans], axis=-1)
    cue_bt = np.asarray(dask.compute(resampled[cue[0]].data)[0], dtype="float32") if cue else None

    bad = float(np.isnan(rgb).any(axis=-1).mean())
    if bad > 0.01:
        annotate("warning", f"{bad:.1%} of pixels have no data (shown black)")
    if sun:
        scan_time, limit = sun
        zen = zenith_grid(scan_time, bbox, resolution, width, height)
        cos_limit = float(np.cos(np.radians(limit)))
        factor = (1.0 / np.maximum(np.cos(np.radians(zen)), cos_limit)).astype("float32")
        refl = [band for band, cal, *_ in channels if cal == _REFL]
        for i, (_, cal, *_) in enumerate(channels):
            if cal == _REFL:
                rgb[..., i] *= factor
        log(f"  sun-angle correction: {'/'.join(refl)} / cos(zenith), capped at {limit:g} deg (max x{1 / cos_limit:.1f}); "
            f"factor min {float(factor.min()):.2f} median {float(np.median(factor)):.2f} max {float(factor.max()):.2f}, "
            f"{float((zen >= limit).mean()):.1%} of pixels at or beyond the cap")
    out = np.empty_like(rgb)
    for i, (band, cal, vmin, vmax, _) in enumerate(channels):
        p1, p50, p99 = np.nanpercentile(rgb[..., i], [1, 50, 99])
        log(f"  {band} {'K' if cal == _BT else '%'}: p1 {p1:.1f}  median {p50:.1f}  p99 {p99:.1f}  "
            f"(scaled {vmin:g}..{vmax:g})")
    for i, (_, _, vmin, vmax, _) in enumerate(channels):
        scaled = np.nan_to_num(np.clip((rgb[..., i] - vmin) / (vmax - vmin), 0.0, 1.0), nan=0.0)
        out[..., i] = np.power(scaled, 1.0 / gammas[i])
    if cue:
        # Cold (= high/thick) cloud -> grey, keeping its brightness texture; smoke and ground stay as they are.
        band, warm, cold = cue
        weight = np.nan_to_num(np.clip((warm - cue_bt) / (warm - cold), 0.0, 1.0), nan=0.0)
        # Never grey out a hotspot: red (scaled 3.9 um) above 0.35 starts to protect, from 0.65 fully
        # (0.35 ~ 49 C on the 27-60 C scale), even if the 10.4 um temperature there is cold (cloud or plume above).
        weight = (weight * (1.0 - np.clip((out[..., 0] - 0.35) / 0.30, 0.0, 1.0)))[..., None]
        grey = np.maximum(out[..., 1], out[..., 2])[..., None]
        out = (1.0 - weight) * out + weight * grey
        log(f"  cloud cue: {band} < {warm:g} K blends to grey, fully by {cold:g} K "
            f"({float((weight > 0).mean()):.1%} of pixels touched, {float((weight > 0.5).mean()):.1%} mostly grey; "
            f"{band} K p1 {np.nanpercentile(cue_bt, 1):.0f} median {np.nanmedian(cue_bt):.0f})")
    return np.round(out * 255.0).astype("uint8")


def make_bt(files, bbox, resolution, band: str):
    """Brightness temperature (K) of ``band`` on the output grid, float32 (H, W)."""
    import dask
    import numpy as np
    from satpy import Scene

    dask.config.set({"array.chunk-size": "64MiB"})
    area, _, _ = target_area(bbox, resolution)
    scn = Scene(filenames=[str(f) for f in files if band_of(f) == band], reader="ahi_hsd")
    scn.load([band], calibration=_BT)
    check_loaded_coverage(scn[band].attrs["area"], bbox)
    bt = np.asarray(dask.compute(scn.resample(area, resampler="nearest")[band].data)[0], dtype="float32")
    log(f"  {band} K: min {np.nanmin(bt):.0f}  median {np.nanmedian(bt):.0f}  max {np.nanmax(bt):.0f}")
    return bt


def make_fire_mask(files, bbox, resolution, cfg):
    """Boolean (H, W) mask of fire pixels from B07 / B13 brightness temperatures, plus the number of clusters.

    cfg is a PRODUCTS[...]["fire_overlay"] entry: B07 above min_k, more than anomaly_k above the median of the
    surrounding window_deg, B13 at least cold_k and B07 - B13 at least min_diff_k.
    """
    import numpy as np
    from scipy.ndimage import label, median_filter

    b7 = make_bt(files, bbox, resolution, "B07")
    b13 = make_bt(files, bbox, resolution, "B13")
    size = max(3, round(cfg["window_deg"] / resolution) // 2 * 2 + 1)  # odd, in output pixels
    anomaly = b7 - median_filter(np.nan_to_num(b7, nan=300.0), size=size)
    mask = ((b7 > cfg["min_k"]) & (anomaly > cfg["anomaly_k"]) & (b13 >= cfg["cold_k"])
            & ((b7 - b13) >= cfg["min_diff_k"]))
    clusters = int(label(mask, structure=np.ones((3, 3)))[1])
    log(f"  fire pixels: {int(mask.sum())} output px in {clusters} cluster(s) "
        f"(B07 > {cfg['min_k']:g} K and > {cfg['anomaly_k']:g} K above its {size}-px surroundings, B13 >= {cfg['cold_k']:g} K)")
    return mask, clusters


def draw_fire(img, mask, resolution: float):
    """Red fire markers on the RGBA image: each detected 2 km pixel is grown by 1 px so it shows, with a dark edge."""
    import numpy as np
    from PIL import Image
    from scipy.ndimage import binary_dilation

    core = binary_dilation(mask, iterations=1)
    edge = binary_dilation(core, iterations=1) & ~core
    layer = np.zeros(mask.shape + (4,), dtype="uint8")
    layer[edge] = (0, 0, 0, 170)
    layer[core] = (255, 0, 0, 255)
    return Image.alpha_composite(img, Image.fromarray(layer, "RGBA"))


def _smoothstep(x):
    import numpy as np

    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def _smooth_bt(bt, sigma: float = 0.9):
    """B13 has 2 km pixels: a light Gaussian blur (NaN-safe) hides the blocks on the 1 km grid."""
    import numpy as np
    from scipy.ndimage import gaussian_filter

    ok = np.isfinite(bt)
    num = gaussian_filter(np.where(ok, bt, 0.0), sigma)
    den = gaussian_filter(ok.astype("float32"), sigma)
    return np.where(den > 0.05, num / np.maximum(den, 1e-6), np.nan).astype("float32")


def ir_colours(temp_c):
    """CIMSS colour scale (ir_scale.CIMSS_TABLE) at ``temp_c`` degrees C: float32 (..., 3) in 0..1."""
    import numpy as np

    t = np.array([row[0] for row in ir_scale.CIMSS_TABLE][::-1], dtype="float64")  # ascending for interp
    c = np.array([row[1] for row in ir_scale.CIMSS_TABLE][::-1], dtype="float64") / 255.0
    return np.stack([np.interp(temp_c, t, c[:, i]) for i in range(3)], axis=-1).astype("float32")


def make_ir_enhanced(tc_rgb, bt, zen):
    """IR Enhanced: (uint8 (H, W, 3) array, sunlit fraction of the area).

    ``tc_rgb`` is the True Color picture (None if no part of the box is sunlit), ``bt`` the 10.4 um brightness
    temperature in K and ``zen`` the per-pixel sun zenith. The base is True Color where the sun is up and a grey
    IR ramp where it is down, blended by sun angle (fully day up to ir_scale.FULL_DAY_ZENITH, fully night at
    ir_scale.NIGHT_ZENITH, smooth-step between). On top goes the
    CIMSS colour: translucent in daylight (see ir_scale), opaque in darkness wherever colder than -20 C.
    """
    import numpy as np

    temp = np.nan_to_num(_smooth_bt(bt) - 273.15, nan=ir_scale.GREY_BLACK_C)  # no data: treated as warm
    day = _smoothstep((ir_scale.NIGHT_ZENITH - zen) / (ir_scale.NIGHT_ZENITH - ir_scale.FULL_DAY_ZENITH))
    day = day.astype("float32")[..., None]
    base_day = tc_rgb.astype("float32") / 255.0 if tc_rgb is not None else np.zeros(temp.shape + (3,), "float32")
    span = ir_scale.GREY_BLACK_C - ir_scale.WARM_C
    grey = (np.clip((ir_scale.GREY_BLACK_C - temp) / span, 0.0, 1.0) * ir_scale.GREY_AT_WARM_C)[..., None]
    base = day * base_day + (1.0 - day) * grey
    cold = (temp < ir_scale.WARM_C).astype("float32")
    day_alpha = ir_scale.DAY_ALPHA * _smoothstep((ir_scale.WARM_C - temp) / (ir_scale.WARM_C - ir_scale.DAY_FULL_C))
    alpha = (day[..., 0] * day_alpha + (1.0 - day[..., 0]) * cold)[..., None]
    out = (1.0 - alpha) * base + alpha * ir_colours(temp)
    return np.round(np.clip(out, 0.0, 1.0) * 255.0).astype("uint8"), float(day.mean())


def ir_legend() -> dict:
    """Colour bar for the caption: the CIMSS scale from -20 C (left) to -92 C (right)."""
    span = ir_scale.WARM_C - ir_scale.COLD_C
    return dict(
        title="Cloud-top temperature, 10.4 um (C); warmer than -20 = grey",
        colours=lambda x: ir_colours(ir_scale.WARM_C - x * span),
        ticks=[((ir_scale.WARM_C - t) / span, str(t)) for t in ir_scale.LEGEND_TICKS_C],
    )


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


def draw_coastline(img, bbox, resolution: float, width_px: float, rgba):
    """Draw Natural Earth 1:10m coastlines in colour ``rgba`` onto ``img`` (an RGBA lat/lon-grid image)."""
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
    img.paste(Image.new("RGBA", img.size, tuple(rgba)), mask=mask)
    log(f"Drew {n_parts} coastline segments (Natural Earth 1:10m)")
    return img


def cloud_cue_for(prod: dict, args):
    """(band, warm_K, cold_K) for the cold-cloud cue of ``prod``, or None if it has none or it is off."""
    cue = prod.get("cloud_cue")
    if not cue or not args.cloud_cue:
        return None
    warm = args.cloud_bt if args.cloud_bt is not None else cue["warm"]
    return cue["band"], warm, warm - (cue["warm"] - cue["cold"])


def gamma_text(gammas) -> str:
    return f"{gammas[0]:g}" if len(set(gammas)) == 1 else "/".join(f"{g:g}" for g in gammas)


GRATICULE_LINE_ALPHA = 46    # of 255: faint, so the picture underneath is barely changed
GRATICULE_LABEL_ALPHA = 150


def draw_graticule(img, bbox, resolution: float, step: float):
    """Faint lat/lon lines every ``step`` degrees, labelled on the top (longitude) and left (latitude) edges."""
    import math

    from PIL import Image, ImageDraw

    w, s, e, n = bbox
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    size = max(11, round(min(img.height, 0.56 * img.width) / 70))  # text size follows the width for tall boxes
    font = _font(size)
    label = (255, 255, 255, GRATICULE_LABEL_ALPHA)
    halo = (0, 0, 0, 120)
    line = (255, 255, 255, GRATICULE_LINE_ALPHA)

    def ticks(lo, hi):
        k = math.ceil(lo / step - 1e-9)
        while k * step <= hi + 1e-9:
            yield k * step
            k += 1

    n_lines = 0
    for lon in ticks(w, e):
        x = (lon - w) / resolution
        d.line([(x, 0), (x, img.height)], fill=line, width=1)
        txt = f"{abs(lon):g}\N{DEGREE SIGN}{'E' if lon >= 0 else 'W'}"
        d.text((x + 4, 3), txt, font=font, fill=label, stroke_width=1, stroke_fill=halo)
        n_lines += 1
    for lat in ticks(s, n):
        y = (n - lat) / resolution
        d.line([(0, y), (img.width, y)], fill=line, width=1)
        txt = f"{abs(lat):g}\N{DEGREE SIGN}{'N' if lat >= 0 else 'S'}" if lat != 0 else "0\N{DEGREE SIGN}"
        d.text((4, y + 3), txt, font=font, fill=label, stroke_width=1, stroke_fill=halo)
        n_lines += 1
    log(f"Drew {n_lines} graticule lines every {step:g}\N{DEGREE SIGN}")
    return Image.alpha_composite(img, layer)


def overlay_and_save(rgb, scan: dt.datetime, product: dict, gammas, rayleigh: bool, out: Path,
                     bbox=None, resolution=None, coast_width: float = 0.0,
                     wind=None, wind_spacing: float = 45.0, notes=(),
                     corner: str = "bottom-right", grid_step: float = 0.0,
                     recipe_line: str | None = None, legend: dict | None = None, fire_mask=None) -> None:
    """Draw graticule, streamlines, coastlines and the caption onto ``rgb`` and save it as ``out``.

    ``recipe_line`` replaces the caption's third line (the recipe summary); ``legend`` is a colour bar drawn at the
    bottom of the caption box: dict(title, colours=f(0..1 across the bar) -> (n, 3) floats in 0..1, ticks=[(frac, text)]).
    ``fire_mask`` is a boolean (H, W) array of fire pixels, drawn in red above the coastlines.
    """
    import numpy as np
    from PIL import Image, ImageDraw

    img = Image.fromarray(rgb, "RGB").convert("RGBA")
    if grid_step > 0:  # layer order: image, graticule, streamlines, coastlines, text
        img = draw_graticule(img, bbox, resolution, grid_step)
    if wind is not None:
        img = ecmwf_winds.draw_streamlines(img, wind, bbox, resolution, wind_spacing, product.get("wind_rgb"))
    if coast_width > 0:
        draw_coastline(img, bbox, resolution, coast_width, product["coast_rgba"])
    if fire_mask is not None and fire_mask.any():
        img = draw_fire(img, fire_mask, resolution)
    h = min(rgb.shape[0], 0.56 * rgb.shape[1])  # the default 25 x 14 box gives h = height; tall boxes keep the text size
    size = max(12, round(h / 48))
    big, small = _font(size), _font(round(size * 0.8))
    sgt = scan.astimezone(SGT)
    white = (255, 255, 255, 255)
    lines = [
        ("Himawari-9 AHI, JMA / NOAA NODD", big, white),
        (f"{scan:%Y-%m-%d %H:%M} UTC  /  {sgt:%Y-%m-%d %H:%M} SGT", big, white),
        (recipe_line or
         f"{product['label']} ({product['recipe']})  gamma {gamma_text(gammas)}"
         + (f"  Rayleigh {'on' if rayleigh else 'off'}" if product["rayleigh"] else
            "  " + " ".join(f"{k} {band}" + (f" {vmin - 273.15:.0f}-{vmax - 273.15:.0f}C" if cal == _BT else "")
                             for k, (band, cal, vmin, vmax, _) in zip("RGB", product["channels"]))), small, white),
    ]
    for note in notes:  # a note is text, or (text, rgba) to override the grey
        text, fill = note if isinstance(note, tuple) else (note, (205, 205, 205, 255))
        lines.append((text, small, fill))
    if wind is not None:  # the line doubles as the legend: same colour as the streamlines
        lines.append((f"Streamlines: {wind.describe()}", small, (*(product.get("wind_rgb") or ecmwf_winds.LINE_RGB), 255)))
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    pad, gap = size // 2, size // 4
    boxes = [d.textbbox((0, 0), t, font=f) for t, f, _ in lines]
    text_w = max(b[2] - b[0] for b in boxes)
    bh = sum(b[3] - b[1] for b in boxes) + gap * (len(lines) - 1) + 2 * pad
    if legend:
        lt_box = d.textbbox((0, 0), legend["title"], font=small)
        text_w = max(text_w, lt_box[2] - lt_box[0])
        bar_h = max(8, round(size * 0.75))
        tick_h = max(d.textbbox((0, 0), t, font=small)[3] for _, t in legend["ticks"])
        bh += gap * 2 + (lt_box[3] - lt_box[1]) + gap + bar_h + 6 + gap + tick_h
    bw = text_w + 2 * pad
    x0 = pad if corner.endswith("left") else img.width - bw - pad
    y0 = pad if corner.startswith("top") else img.height - bh - pad
    d.rectangle([x0, y0, x0 + bw, y0 + bh], fill=(0, 0, 0, 140))
    y = y0 + pad
    for (text, font, fill), b in zip(lines, boxes):
        d.text((x0 + pad - b[0], y - b[1]), text, font=font, fill=fill)
        y += b[3] - b[1] + gap
    if legend:
        x_bar, bar_w = x0 + pad, bw - 2 * pad
        y += gap
        d.text((x_bar - lt_box[0], y - lt_box[1]), legend["title"], font=small, fill=(205, 205, 205, 255))
        y += lt_box[3] - lt_box[1] + gap
        row = np.round(np.clip(legend["colours"]((np.arange(bar_w) + 0.5) / bar_w), 0, 1) * 255).astype("uint8")
        layer.paste(Image.fromarray(np.repeat(row[None], bar_h, axis=0), "RGB"), (x_bar, y))
        for frac, text in legend["ticks"]:
            tx = x_bar + frac * bar_w
            d.line([(tx, y + bar_h), (tx, y + bar_h + 5)], fill=white, width=1)
            tb = d.textbbox((0, 0), text, font=small)
            d.text((tx - (tb[2] - tb[0]) / 2 - tb[0], y + bar_h + 6 - tb[1]), text, font=small, fill=white)
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.alpha_composite(img, layer).convert("RGB").save(out, optimize=True)


# --------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--datetime-utc", default="", help="scan time YYYYMMDDHHMM (UTC); blank = newest complete scan")
    p.add_argument("--region", choices=tuple(REGIONS), default="sumatra_borneo",
                   help="preset for bbox, segments, output folder and caption corner (default sumatra_borneo); "
                        "--bbox, --segments and --caption-corner override its values")
    p.add_argument("--bbox", default=None, help="west,south,east,north in degrees (default: the region's)")
    p.add_argument("--product", choices=(*PRODUCTS, "all"), default="all",
                   help="which image(s) to build (default: all). At night only ir_enhanced is made")
    p.add_argument("--gamma", type=float, default=None,
                   help="gamma override for true_color and the True Color base of ir_enhanced (day_fire has per-channel gammas 0.4/1/1 and ignores "
                        "it); default 1.7 - 2.0 can help with haze")
    p.add_argument("--segments", default=None, help="full-disk segments to download, e.g. 5-6 (default: the region's)")
    p.add_argument("--resolution", type=float, default=0.01, help="output pixel size in degrees (0.01 ~ 1 km)")
    p.add_argument("--rayleigh", action=argparse.BooleanOptionalAction, default=True,
                   help="apply Satpy Rayleigh correction to true_color and the True Color base of ir_enhanced (default "
                        "on; --no-rayleigh for the plain JMA recipe); day_fire never uses it")
    p.add_argument("--coastlines", action=argparse.BooleanOptionalAction, default=True,
                   help="draw Natural Earth 1:10m coastlines (default on; green, magenta on day_fire)")
    p.add_argument("--coast-width", type=float, default=1.5, help="coastline width in output pixels")
    p.add_argument("--graticule", action=argparse.BooleanOptionalAction, default=True,
                   help="draw faint, labelled lat/lon lines (default on)")
    p.add_argument("--grid-step", type=float, default=5.0, help="graticule spacing in degrees (default 5)")
    p.add_argument("--fire", action=argparse.BooleanOptionalAction, default=True,
                   help="true_color only: mark fire pixels (hot 3.9 um signal, see PRODUCTS['true_color']['fire_overlay']) "
                        "in red (default on; an addition, not an official fire product)")
    p.add_argument("--cloud-cue", action=argparse.BooleanOptionalAction, default=True,
                   help="day_fire only: show cold cloud (10.4 um brightness temperature below ~290 K) in grey so it "
                        "stands apart from smoke (default on; an addition to the NOAA/CIRA recipe)")
    p.add_argument("--cloud-bt", type=float, default=None,
                   help="temperature in K where the cold-cloud grey starts (default 290); fully grey 10 K colder")
    p.add_argument("--sun-correction", action=argparse.BooleanOptionalAction, default=True,
                   help="day_fire only: divide the 0.86/0.64 um channels by cos(sun zenith), capped at 80 deg, so "
                        "brightness does not follow the sun (default on; an addition to the NOAA/CIRA recipe)")
    p.add_argument("--low-sun-zenith", type=float, default=65.0,
                   help="warn (log, annotation, caption note) when the sun zenith at the bbox centre or its west/east "
                        "edge exceeds this many degrees; the sun-angle correction overshoots and noise grows there "
                        "(default 65)")
    p.add_argument("--caption-corner", choices=("bottom-right", "bottom-left", "top-right", "top-left"),
                   default=None,
                   help="where the caption box goes (default: the region's; for Sumatra-Borneo bottom-right, open sea "
                        "that keeps southern Sumatra visible)")
    p.add_argument("--winds", action=argparse.BooleanOptionalAction, default=True,
                   help="overlay ECMWF IFS wind streamlines (default on; skipped with a warning if ECMWF data fail)")
    p.add_argument("--wind-level", type=int, default=925, help="pressure level in hPa for the streamlines")
    p.add_argument("--wind-spacing", type=float, default=45.0, help="approximate streamline spacing in output pixels")
    p.add_argument("--outdir", default="output",
                   help="directory for the PNGs (day_fire goes in its 'dayfire' subfolder, ir_enhanced in 'ir'; "
                        "a region other than sumatra_borneo adds its own folder first, e.g. mekong/dayfire)")
    p.add_argument("--workdir", default=None, help="scratch directory for HSD files (deleted afterwards)")
    p.add_argument("--lookback-hours", type=float, default=3.0, help="how far back to look for the newest scan")
    p.add_argument("--check-only", action="store_true", help="only list the bucket and run the checks")
    p.add_argument("--json-out", default=None, help="write a JSON result file (status, scan time, outputs)")
    return p


def run(args) -> dict:
    region = REGIONS[args.region]
    bbox = parse_bbox(args.bbox or region["bbox"])
    segments = parse_segments(args.segments or region["segments"])
    caption_corner = args.caption_corner or region["caption_corner"]
    if not 0.001 <= args.resolution <= 1:
        raise UserError(f"resolution must be between 0.001 and 1 degrees, got {args.resolution}")
    if args.gamma is not None and not 0.2 <= args.gamma <= 5:
        raise UserError(f"gamma must be between 0.2 and 5, got {args.gamma}")
    if args.cloud_bt is not None and not 250 <= args.cloud_bt <= 320:
        raise UserError(f"cloud-bt must be between 250 and 320 K, got {args.cloud_bt}")
    if args.graticule and not 0.5 <= args.grid_step <= 30:
        raise UserError(f"grid-step must be between 0.5 and 30 degrees, got {args.grid_step}")
    if not 30 <= args.low_sun_zenith <= 90:
        raise UserError(f"low-sun-zenith must be between 30 and 90 degrees, got {args.low_sun_zenith}")
    products = list(PRODUCTS) if args.product == "all" else [args.product]
    bands = sorted({b for p in products for b in PRODUCTS[p]["bands"] + PRODUCTS[p].get("extra_bands", ())}
                   | {cue[0] for p in products if (cue := cloud_cue_for(PRODUCTS[p], args))}
                   | {b for p in products if args.fire and PRODUCTS[p].get("fire_overlay")
                      for b in PRODUCTS[p]["fire_overlay"]["bands"]})
    if args.winds and args.wind_level not in ecmwf_winds.PRESSURE_LEVELS:
        raise UserError(f"wind level must be one of {ecmwf_winds.PRESSURE_LEVELS} hPa, got {args.wind_level}")
    if not 10 <= args.wind_spacing <= 400:
        raise UserError(f"wind spacing must be between 10 and 400 px, got {args.wind_spacing}")
    requested = parse_scan_time(args.datetime_utc) if args.datetime_utc.strip() else None

    check_segments_cover_bbox(bbox, segments)

    s3 = s3_client()
    scan, keys = select_scan(s3, segments, requested, args.lookback_hours, bands)
    log(f"Scan {scan:%Y-%m-%d %H:%M} UTC: all {len(keys)} files present "
        f"({', '.join(bands)} x segments {segments}) for {', '.join(products)}")

    result = {
        "status": "ok",
        "region": args.region,
        "scan_utc": f"{scan:%Y%m%d_%H%M}",
        "scan_utc_iso": scan.strftime("%Y-%m-%d %H:%M UTC"),
        "scan_sgt_iso": scan.astimezone(SGT).strftime("%Y-%m-%d %H:%M SGT"),
        "bbox": list(bbox),
        "segments": segments,
        "products": products,
        "bands": bands,
        "outputs": [],
    }

    clon, clat = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    sza = solar_zenith(scan, clon, clat)
    result["sun_zenith_centre"] = round(sza, 1)
    log(f"Solar zenith angle at bbox centre ({clon:g}, {clat:g}): {sza:.1f} deg")
    if sza >= 90:
        # Night: only products that work in the dark (IR Enhanced) still run; the rest are skipped.
        skipped = [p for p in products if not PRODUCTS[p].get("night_ok")]
        products = [p for p in products if p not in skipped]
        when = f"scan {result['scan_utc_iso']} ({result['scan_sgt_iso']}), solar zenith {sza:.1f} deg"
        if not products:
            result["status"] = "skipped_night"
            annotate("notice", f"Skipped: sun is below the horizon at the bbox centre for {when}. "
                     "These RGB products are daytime only.")
            return result
        if skipped:
            result["skipped_products"] = skipped
            annotate("notice", f"Night at the bbox centre ({when}): skipped {', '.join(skipped)} (daytime only); "
                     f"making {', '.join(products)}.")
        result["products"] = products
    else:
        corners = [solar_zenith(scan, lon, lat) for lon, lat in ((bbox[0], bbox[1]), (bbox[2], bbox[3]))]
        if max(corners) >= 90:
            annotate("warning", f"Sun is below the horizon at part of the bbox "
                     f"(SW/NE corner zenith {corners[0]:.0f}/{corners[1]:.0f} deg): that part will be dark"
                     + (" (IR Enhanced shows it as infrared)." if "ir_enhanced" in products else "."))

    # Low-sun warning: the picture is dim and noisy wherever the sun is low, even if it is above the horizon.
    # It concerns the products that use reflected sunlight alone (not IR Enhanced, which fades to IR there).
    low_sun_note = None
    if sza < 90 and any(not PRODUCTS[p].get("ir") for p in products):
        zen_w, zen_e = solar_zenith(scan, bbox[0], clat), solar_zenith(scan, bbox[2], clat)
        worst = max(zen_w, sza, zen_e)
        if worst > args.low_sun_zenith:
            side = "west" if worst == zen_w else "east" if worst == zen_e else "centre"
            result["low_sun"] = (f"sun zenith {zen_w:.0f}/{sza:.0f}/{zen_e:.0f} deg (west/centre/east), above "
                                 f"{args.low_sun_zenith:g}: the {side} side is dim or over-corrected and noisy")
            annotate("warning", "Low sun: " + result["low_sun"] + ". Consider a scan nearer local noon.")
            low_sun_note = (f"LOW SUN: zenith {worst:.0f}\N{DEGREE SIGN} on the {side} side - unreliable brightness there",
                            (255, 190, 60, 255))

    # Only the bands the remaining products need: at night IR Enhanced is B13 alone, unless part of the box is sunlit.
    sunlit = sza < 90 or any_sunlit(scan, bbox)
    needed = {b for p in products for b in PRODUCTS[p]["bands"] if sunlit or not PRODUCTS[p].get("night_ok")}
    needed |= {b for p in products for b in PRODUCTS[p].get("extra_bands", ())}
    needed |= {cue[0] for p in products if (cue := cloud_cue_for(PRODUCTS[p], args))}
    needed |= {b for p in products if args.fire and PRODUCTS[p].get("fire_overlay")
               for b in PRODUCTS[p]["fire_overlay"]["bands"]}
    keys = {k: v for k, v in keys.items() if k[0] in needed}
    result["bands"] = sorted(needed)

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
        wind = None
        result["winds"] = "off"
        if args.winds:
            # The satellite images are the main product: a wind problem must not lose them.
            try:
                wind = ecmwf_winds.fetch_winds(s3, scan, args.wind_level, bbox)
                result["winds"] = wind.describe()
                log(f"Winds: {wind.describe()}")
            except Exception as exc:  # noqa: BLE001 - deliberately broad, see above
                result["winds"] = f"skipped: {exc}"
                annotate("warning", f"Wind streamlines skipped ({type(exc).__name__}: {exc}); "
                         "the images are published without them.")
        coast_width = args.coast_width if args.coastlines else 0.0
        grid_step = args.grid_step if args.graticule else 0.0
        for name in products:
            prod = PRODUCTS[name]
            gammas = tuple(c[4] for c in prod["channels"])
            if args.gamma is not None:
                if prod["uniform_gamma"]:
                    gammas = (args.gamma,) * 3
                else:
                    log(f"--gamma ignored for {prod['label']}: its recipe has per-channel gammas {gamma_text(gammas)}")
            use_rayleigh = bool(args.rayleigh and prod["rayleigh"])
            log(f"--- {prod['label']}: R/G/B = {'/'.join(prod['bands'])}, gamma {gamma_text(gammas)}"
                + (f", Rayleigh {'on' if use_rayleigh else 'off'}" if prod["rayleigh"] else ""))
            cue = cloud_cue_for(prod, args)
            sun = (scan, prod["sun_correction"]["limit"]) if prod.get("sun_correction") and args.sun_correction else None
            notes, recipe_line, legend, day_frac = [], None, None, None
            if prod.get("ir"):
                _, width, height = target_area(bbox, args.resolution)
                zen = zenith_grid(scan, bbox, args.resolution, width, height)
                tc = make_rgb(files, bbox, args.resolution, prod, gammas, use_rayleigh) if sunlit else None
                rgb, day_frac = make_ir_enhanced(tc, make_bt(files, bbox, args.resolution, "B13"), zen)
                legend = ir_legend()
                if day_frac <= 0.005:
                    recipe_line = "IR Enhanced: B13 10.4 um only (night)"
                    notes.append("Night: warm surfaces in grey, cold cloud in colour")
                else:
                    recipe_line = (f"IR Enhanced: True Color (gamma {gamma_text(gammas)}, "
                                   f"Rayleigh {'on' if use_rayleigh else 'off'}) + B13 10.4 um")
                    notes.append(f"Cold cloud over True Color: clear at {ir_scale.WARM_C} C, "
                                 f"{ir_scale.DAY_ALPHA:.0%} opaque from {ir_scale.DAY_FULL_C:.0f} C")
                    if day_frac < 0.995:
                        notes.append(f"True Color over {day_frac:.0%} of the area; the rest fades to grey IR with opaque colour")
                log(f"  IR Enhanced: True Color weight {day_frac:.1%} of the area")
            else:
                rgb = make_rgb(files, bbox, args.resolution, prod, gammas, use_rayleigh, cue, sun)
            fire_mask = None
            if args.fire and prod.get("fire_overlay"):
                fire_mask, fire_clusters = make_fire_mask(files, bbox, args.resolution, prod["fire_overlay"])
                fcfg = prod["fire_overlay"]
                notes.append((f"Red: {fire_clusters} fire cluster(s), 3.9 um above {fcfg['min_k'] - 273.15:.0f} C and "
                              f"{fcfg['anomaly_k']:g} K over surroundings (not an official product)", (255, 120, 120, 255)))
            if cue:
                notes.append(f"Cold cloud ({cue[0]} < {cue[1]:g} K) shown grey; smoke and warm cloud keep their colour")
            if sun:
                refl = "/".join(c[0] for c in prod["channels"] if c[1] == _REFL)
                notes.append(f"{refl} divided by cos(sun zenith), capped at {sun[1]:g}\N{DEGREE SIGN}")
            if low_sun_note and not prod.get("ir"):
                notes.append(low_sun_note)
            folder = "/".join(p for p in (region["subdir"], prod["subdir"]) if p)  # "" = top level
            out = Path(args.outdir) / folder / f"{scan:%Y%m%d_%H%M}.png"
            try:
                overlay_and_save(rgb, scan, prod, gammas, use_rayleigh, out, bbox, args.resolution,
                                 coast_width, wind, args.wind_spacing, notes, caption_corner, grid_step,
                                 recipe_line, legend, fire_mask)
            except Exception as exc:  # noqa: BLE001
                if wind is None:
                    raise
                result["winds"] = f"skipped: drawing failed: {exc}"
                annotate("warning", f"Drawing wind streamlines failed ({type(exc).__name__}: {exc}); "
                         "publishing without them.")
                wind = None  # later products skip them too
                overlay_and_save(rgb, scan, prod, gammas, use_rayleigh, out, bbox, args.resolution, coast_width,
                                 None, args.wind_spacing, notes, caption_corner, grid_step, recipe_line, legend, fire_mask)
            result["outputs"].append({
                "product": name, "label": prod["label"], "png": str(out), "subdir": folder,
                "size_px": [int(rgb.shape[1]), int(rgb.shape[0])], "gamma": gamma_text(gammas),
                "rayleigh": use_rayleigh,
                **({"sunlit_fraction": round(day_frac, 3)} if day_frac is not None else {}),
                **({"fire_clusters": fire_clusters, "fire_pixels": int(fire_mask.sum())} if fire_mask is not None else {}),
            })
            log(f"Wrote {out} ({out.stat().st_size / 1e6:.1f} MB, {rgb.shape[1]}x{rgb.shape[0]})")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if old_tmp is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = old_tmp
        tempfile.tempdir = None
    log(f"Deleted downloaded HSD files ({work})")
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
