#!/usr/bin/env python3
"""Ad-hoc True Color plot with ECMWF winds AND a hotspot overlay (separate copy of adhoc_truecolor_winds.py).

As adhoc_truecolor_winds.py (one Himawari-9 scan, ECMWF IFS wind from a chosen run and step, nothing from the
operational code is modified), plus the VIIRS daytime hotspots from a CSV (lat, lon, frp_mw) drawn as the same red
markers the Day Fire overlay uses.  Used for 13 Oct 2026: the CSV holds the high-confidence (FireMask 9) pixels of that
UTC day, taken from the 'data' branch of zgkn/hotspot_aod_plots (fires_20261013.npz).

    python scripts/adhoc_truecolor_winds_hotspots.py --scan 202610130600 --wind-run 2026101306 --wind-step 0 \
        --hotspots data/hotspots_highconf_20261013.csv --hotspot-label "13 Oct 2026" --outdir out
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ecmwf_winds as ew  # noqa: E402
import himawari_truecolor as h  # noqa: E402


def hotspot_mask(path: Path, bbox, resolution: float, shape) -> tuple["numpy.ndarray", int, int]:
    """Boolean (H, W) mask of the hotspots on the output grid, the number inside the box and the number in the file."""
    import numpy as np

    west, _, _, north = bbox
    height, width = shape
    mask = np.zeros(shape, dtype=bool)
    total = inside = 0
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            total += 1
            col = int((float(row["lon"]) - west) // resolution)
            r = int((north - float(row["lat"])) // resolution)
            if 0 <= col < width and 0 <= r < height:
                mask[r, col] = True
                inside += 1
    return mask, inside, total


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan", required=True, help="Himawari scan time YYYYMMDDHHMM (UTC)")
    ap.add_argument("--wind-run", required=True, help="ECMWF run YYYYMMDDHH (UTC; hours 00/06/12/18)")
    ap.add_argument("--wind-step", type=int, default=0, help="forecast step in hours (0 = analysis time; multiples of 3)")
    ap.add_argument("--wind-level", type=int, default=925, help="pressure level in hPa")
    ap.add_argument("--hotspots", default="", help="CSV with lat, lon, frp_mw columns (optional)")
    ap.add_argument("--hotspot-label", default="", help="text for the caption, e.g. '13 Oct 2026'")
    ap.add_argument("--bbox", default="94.5,-6.5,119.5,7.5")
    ap.add_argument("--segments", default="5-6")
    ap.add_argument("--resolution", type=float, default=0.01)
    ap.add_argument("--rayleigh", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--outdir", default="output-adhoc")
    args = ap.parse_args(argv)

    try:
        bbox = h.parse_bbox(args.bbox)
        segments = h.parse_segments(args.segments)
        scan = h.parse_scan_time(args.scan)
        try:
            init = dt.datetime.strptime(args.wind_run, "%Y%m%d%H")
        except ValueError:
            raise h.UserError(f"wind-run must be YYYYMMDDHH, got {args.wind_run!r}")
        if init.hour % ew.RUN_HOURS:
            raise h.UserError(f"ECMWF runs start at 00/06/12/18 UTC, got hour {init.hour:02d}")
        if args.wind_step % ew.STEP_HOURS or not 0 <= args.wind_step <= ew.MAX_STEP:
            raise h.UserError(f"wind-step must be a multiple of {ew.STEP_HOURS} between 0 and {ew.MAX_STEP}")
        if args.wind_level not in ew.PRESSURE_LEVELS:
            raise h.UserError(f"wind-level must be one of {ew.PRESSURE_LEVELS}")
        if args.hotspots and not Path(args.hotspots).is_file():
            raise h.UserError(f"hotspot file not found: {args.hotspots}")
        h.check_segments_cover_bbox(bbox, segments)
        clon, clat = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
        sza = h.solar_zenith(scan, clon, clat)
        h.log(f"Solar zenith angle at bbox centre: {sza:.1f} deg")
        if sza >= 90:
            raise h.UserError(f"The sun is below the horizon at the bbox centre at {scan:%Y-%m-%d %H:%M} UTC; True Color is daytime only.")
    except h.UserError as exc:
        h.annotate("error", str(exc))
        return 2

    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="adhoc_", dir=os.environ.get("RUNNER_TEMP")))
    os.environ["TMPDIR"] = str(work)
    tempfile.tempdir = None
    try:
        s3 = h.s3_client()

        try:
            fields, lon, lat = ew._decode(ew._fetch_messages(s3, init, args.wind_step, args.wind_level), bbox, 1.0)
        except Exception as exc:  # noqa: BLE001
            h.annotate("error", f"Could not read ECMWF run {init:%Y-%m-%d %HZ} step {args.wind_step} h: "
                       f"{type(exc).__name__}: {exc}")
            return 2
        wind = ew.WindField(lon=lon, lat=lat, u=fields["u"], v=fields["v"], below_ground=fields["sp"] < args.wind_level * 100.0,
                            level=args.wind_level, init=init, step_lo=args.wind_step, step_hi=args.wind_step)
        valid = init + dt.timedelta(hours=args.wind_step)
        offset_h = (valid - scan.replace(tzinfo=None)).total_seconds() / 3600
        what = "analysis" if args.wind_step == 0 else f"+{args.wind_step} h forecast"
        side = "after" if offset_h > 0 else "before"
        wind.describe = lambda: (f"ECMWF IFS {args.wind_level} hPa wind, {init:%d %b %HZ} {what} "
                                 f"(valid {valid:%H}Z, {abs(offset_h):g} h {side} scan)")
        h.log(f"Winds: {wind.describe()}")

        scan_found, keys = h.select_scan(s3, segments, scan, 3, ["B01", "B02", "B03"])
        files = h.download(s3, keys, work / "hsd")
        prod = h.PRODUCTS["true_color"]
        gammas = tuple(c[4] for c in prod["channels"])
        rgb = h.make_rgb(files, bbox, args.resolution, prod, gammas, args.rayleigh)
        notes = [f"Ad-hoc plot: winds are not time-matched to the scan; Rayleigh {'on' if args.rayleigh else 'off'}"]

        fire = None
        tag = ""
        if args.hotspots:
            fire, inside, total = hotspot_mask(Path(args.hotspots), bbox, args.resolution, rgb.shape[:2])
            h.log(f"Hotspots: {inside} of {total} inside the box")
            label = f" {args.hotspot_label}" if args.hotspot_label else ""
            notes.append((f"Red: VIIRS high-confidence daytime hotspots{label} (NOAA-20/21, all passes that day): {inside}",
                          (255, 110, 110, 255)))
            tag = "_hotspots"

        png = out / f"{scan:%Y%m%d_%H%M}_truecolor_winds{init:%Y%m%d%H}_s{args.wind_step:02d}{tag}{'' if args.rayleigh else '_noray'}.png"
        h.overlay_and_save(rgb, scan, prod, gammas, args.rayleigh, png, bbox, args.resolution, 1.5, wind, 45.0,
                           notes, "bottom-right", 5.0, fire_mask=fire)
        h.log(f"Wrote {png} ({png.stat().st_size / 1e6:.1f} MB)")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
