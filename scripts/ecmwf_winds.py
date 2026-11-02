"""ECMWF open-data (IFS 0.25 deg) wind fields and a streamline overlay for the Himawari image.

Data: s3://ecmwf-forecasts/YYYYMMDD/HHz/ifs/0p25/oper/YYYYMMDDHH0000-{step}h-oper-fc.grib2
(anonymous access).  Every GRIB file has a small ``.index`` file next to it, so only the
three messages we need (u, v at the chosen pressure level, and surface pressure) are fetched
with HTTP range requests (~2 MB per step instead of ~100 MB).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from dataclasses import dataclass

BUCKET = "ecmwf-forecasts"
RUN_HOURS = 6  # runs at 00/06/12/18 UTC
STEP_HOURS = 3  # 3-hourly steps are available for every run up to +90 h
MAX_STEP = 90
MAX_RUNS_BACK = 8  # look back at most 48 h for a published run
MAX_RUNS_TRIED = 2  # newest run first; if its data cannot be fetched, fall back to the previous one
RETRY_DELAYS = (10, 30, 60)  # seconds to wait before the 2nd, 3rd and 4th try of a throttled/failed request
WIND_BUDGET_S = 240  # stop retrying and give up on winds after about this long (the images matter more)
PRESSURE_LEVELS = (1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100, 50)

LINE_RGB = (255, 170, 30)  # orange: shows over dark ocean/land and over white cloud
CASING_RGB = (0, 0, 0)
DPI = 100
MAX_LENGTH = 0.22  # longest streamline, in axes widths; shorter lines mean more arrowheads


class WindError(Exception):
    """Winds could not be fetched or drawn (the caller decides whether that is fatal)."""


@dataclass
class WindField:
    lon: "np.ndarray"  # 1-D, ascending, degrees east (0-360 domain)
    lat: "np.ndarray"  # 1-D, ascending
    u: "np.ndarray"  # m/s, shape (lat, lon), time-interpolated to the scan time
    v: "np.ndarray"
    below_ground: "np.ndarray"  # True where the pressure level is below the surface
    level: int
    init: dt.datetime
    step_lo: int
    step_hi: int
    note: str = ""  # e.g. "newest run 01 Nov 18Z unavailable: ..." when an older run had to be used

    def describe(self) -> str:
        steps = f"+{self.step_lo} h" if self.step_lo == self.step_hi else f"+{self.step_lo}/+{self.step_hi} h"
        return f"ECMWF IFS {self.level} hPa wind, run {self.init:%d %b %HZ}, {steps}"


# ------------------------------------------------------------------------- fetching
def _key(init: dt.datetime, step: int, ext: str) -> str:
    return f"{init:%Y%m%d}/{init:%H}z/ifs/0p25/oper/{init:%Y%m%d%H}0000-{step}h-oper-fc.{ext}"


def _note(msg: str) -> None:
    print(msg, flush=True)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::{msg}", flush=True)


def _retry(what: str, fn, deadline: float | None = None):
    """Call ``fn()``; on any error (S3 'SlowDown' throttling most often) wait and try again, as RETRY_DELAYS says.

    Gives up early when the next wait would end after ``deadline`` (a time.monotonic() value).
    """
    for delay in (*RETRY_DELAYS, None):
        try:
            return fn()
        except WindError:
            raise  # a permanent problem (unexpected file layout), waiting will not help
        except Exception as exc:  # noqa: BLE001 - the caller decides what a final failure means
            if delay is None or (deadline is not None and time.monotonic() + delay > deadline):
                raise
            print(f"ECMWF {what}: {type(exc).__name__}: {str(exc)[:160]}; retrying in {delay} s", flush=True)
            time.sleep(delay)


def _exists(s3, key: str) -> bool:
    from botocore.exceptions import ClientError

    def head() -> bool:
        try:
            s3.head_object(Bucket=BUCKET, Key=key)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "403", "NoSuchKey", "NotFound"):
                return False
            raise

    return _retry(f"existence check of {key.rsplit('/', 1)[-1]}", head)


def candidate_runs(s3, scan: dt.datetime):
    """Yield (init, step_lo, step_hi) for each published run whose start is not after ``scan``, newest first."""
    init = scan.replace(hour=scan.hour // RUN_HOURS * RUN_HOURS, minute=0, second=0, microsecond=0)
    for _ in range(MAX_RUNS_BACK):
        hours = (scan - init).total_seconds() / 3600
        lo = int(hours // STEP_HOURS) * STEP_HOURS
        hi = lo if hours == lo else lo + STEP_HOURS
        if hi <= MAX_STEP and all(
            _exists(s3, _key(init, s, ext)) for s in {lo, hi} for ext in ("grib2", "index")
        ):
            yield init, lo, hi
        init -= dt.timedelta(hours=RUN_HOURS)


def find_run(s3, scan: dt.datetime):
    """Newest published run whose start is not after ``scan``; returns (init, step_lo, step_hi)."""
    for run in candidate_runs(s3, scan):
        return run
    raise WindError(
        f"no published ECMWF open-data run found within {MAX_RUNS_BACK * RUN_HOURS} h before "
        f"{scan:%Y-%m-%d %H:%M} UTC in s3://{BUCKET}/"
    )


def _fetch_messages(s3, init: dt.datetime, step: int, level: int) -> bytes:
    """Byte-range fetch of u, v (at ``level``) and sp for one step; returns concatenated GRIB."""
    index = s3.get_object(Bucket=BUCKET, Key=_key(init, step, "index"))["Body"].read().decode()
    wanted = []
    for line in index.splitlines():
        rec = json.loads(line)
        is_wind = rec.get("levtype") == "pl" and rec.get("levelist") == str(level) and rec["param"] in ("u", "v")
        is_sp = rec.get("levtype") == "sfc" and rec["param"] == "sp"
        if is_wind or is_sp:
            wanted.append(rec)
    if len(wanted) != 3:
        raise WindError(
            f"expected u, v @ {level} hPa and sp in the ECMWF index for step {step}, found "
            f"{sorted(r['param'] for r in wanted)}"
        )
    chunks = []
    for rec in sorted(wanted, key=lambda r: r["_offset"]):
        rng = f"bytes={rec['_offset']}-{rec['_offset'] + rec['_length'] - 1}"
        chunks.append(s3.get_object(Bucket=BUCKET, Key=_key(init, step, "grib2"), Range=rng)["Body"].read())
    return b"".join(chunks)


def _decode(data: bytes, bbox, margin: float):
    """Decode GRIB messages -> ({shortName: 2-D array (lat asc, lon asc)}, lon, lat), cropped to bbox."""
    import eccodes
    import numpy as np

    fields, lon, lat = {}, None, None
    pos = 0
    while pos < len(data):
        # each message starts with 'GRIB' and carries its total length in bytes 8-16
        total = int.from_bytes(data[pos + 8:pos + 16], "big")
        handle = eccodes.codes_new_from_message(data[pos:pos + total])
        pos += total
        try:
            name = eccodes.codes_get(handle, "shortName")
            ni, nj = eccodes.codes_get(handle, "Ni"), eccodes.codes_get(handle, "Nj")
            vals = eccodes.codes_get_values(handle).reshape(nj, ni)
            lats = np.linspace(eccodes.codes_get(handle, "latitudeOfFirstGridPointInDegrees"),
                               eccodes.codes_get(handle, "latitudeOfLastGridPointInDegrees"), nj)
            # the global grid may start mid-way (e.g. at 180E, wrapping through 0), so build the
            # longitudes from the increment rather than from the first/last values
            lons = (eccodes.codes_get(handle, "longitudeOfFirstGridPointInDegrees")
                    + np.arange(ni) * eccodes.codes_get(handle, "iDirectionIncrementInDegrees")) % 360
        finally:
            eccodes.codes_release(handle)
        if lats[0] > lats[-1]:  # north-to-south -> south-to-north
            lats, vals = lats[::-1], vals[::-1]
        fields[name] = (vals, lons, lats)

    vals, lons, lats = next(iter(fields.values()))
    w, s, e, n = bbox
    w, e = w % 360, e % 360
    if e <= w:
        raise WindError("bbox crosses the antimeridian; winds are not supported there")
    if np.any(np.diff(lons) <= 0):
        order = np.argsort(lons)
        lons = lons[order]
        fields = {k: (v[:, order], lons, lats) for k, (v, _, _) in fields.items()}
    ix = np.where((lons >= w - margin) & (lons <= e + margin))[0]
    iy = np.where((lats >= s - margin) & (lats <= n + margin))[0]
    if ix.size < 3 or iy.size < 3:
        raise WindError("bbox is too small or outside the ECMWF grid")
    crop = {k: v[np.ix_(iy, ix)].astype("float64") for k, (v, _, _) in fields.items()}
    return crop, lons[ix], lats[iy]


def fetch_winds(s3, scan: dt.datetime, level: int, bbox, margin: float = 1.0) -> WindField:
    """Winds at ``level`` hPa linearly interpolated in time to ``scan`` (UTC).

    The newest published run is used. Each download is retried (the open-data bucket throttles with 'SlowDown',
    especially just after a new run appears; observed in bursts that affect every run); if it still fails, the
    previous run is tried, up to MAX_RUNS_TRIED. The whole attempt is limited to about WIND_BUDGET_S seconds.
    """
    import numpy as np

    if level not in PRESSURE_LEVELS:
        raise WindError(f"wind level must be one of {PRESSURE_LEVELS}, got {level}")
    failures: list[str] = []
    deadline = time.monotonic() + WIND_BUDGET_S
    for init, lo, hi in candidate_runs(s3, scan):
        if len(failures) >= MAX_RUNS_TRIED or (failures and time.monotonic() > deadline):
            break
        try:
            stacks = []
            for step in sorted({lo, hi}):
                fields, lon, lat = _retry(
                    f"download of run {init:%d %b %HZ} step {step} h",
                    lambda step=step: _decode(_fetch_messages(s3, init, step, level), bbox, margin),
                    deadline,
                )
                stacks.append(fields)
        except Exception as exc:  # noqa: BLE001 - try the previous run
            failures.append(f"run {init:%d %b %HZ}: {type(exc).__name__}: {str(exc)[:200]}")
            _note(f"ECMWF run {init:%d %b %HZ} could not be fetched ({failures[-1].split(': ', 1)[1]}); "
                  "trying the previous run")
            continue
        weight = 0.0 if hi == lo else (scan - (init + dt.timedelta(hours=lo))).total_seconds() / (STEP_HOURS * 3600)
        a, b = stacks[0], stacks[-1]
        mix = lambda k: (1 - weight) * a[k] + weight * b[k]  # noqa: E731
        sp = np.minimum(a["sp"], b["sp"])  # mask if either bracketing step is underground
        return WindField(
            lon=lon, lat=lat, u=mix("u"), v=mix("v"),
            below_ground=sp < level * 100.0,
            level=level, init=init, step_lo=lo, step_hi=hi,
            note=f"newest run unavailable, used an older one: {'; '.join(failures)}" if failures else "",
        )
    raise WindError("; ".join(failures) if failures else
                    f"no published ECMWF open-data run found within {MAX_RUNS_BACK * RUN_HOURS} h before "
                    f"{scan:%Y-%m-%d %H:%M} UTC in s3://{BUCKET}/")


# --------------------------------------------------------------------------- drawing
def draw_streamlines(img, wind: WindField, bbox, resolution: float, spacing_px: float = 45.0, line_rgb=None):
    """Composite thin streamlines for ``wind`` (orange, or ``line_rgb``) onto the RGBA lat/lon image ``img``."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from PIL import Image

    width, height = img.size
    w, s, e, n = bbox
    w = w % 360
    x0, x1 = w, w + width * resolution
    y1, y0 = n, n - height * resolution

    speed = np.hypot(wind.u, wind.v)
    # Wind components are m/s; on a plate-carree canvas 1 deg of longitude is drawn as long as
    # 1 deg of latitude, so scale u by 1/cos(lat) to keep the drawn direction true.
    cos_lat = np.cos(np.deg2rad(wind.lat))[:, None]
    u = np.ma.masked_where(wind.below_ground, wind.u / cos_lat)
    v = np.ma.masked_where(wind.below_ground, wind.v)
    linewidth = 0.7 + 0.9 * np.clip(speed / 12.0, 0.0, 1.0)  # points; 1 pt = 1.39 px at 100 dpi
    density = (width / (30.0 * spacing_px), height / (30.0 * spacing_px))

    fig = plt.figure(figsize=(width / DPI, height / DPI), dpi=DPI)
    try:
        fig.patch.set_alpha(0)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_axis_off()
        ax.patch.set_alpha(0)
        ax.set_xlim(x0, x1)
        ax.set_ylim(y0, y1)
        for rgb, extra, alpha in ((CASING_RGB, 1.0, 0.30), (line_rgb or LINE_RGB, 0.0, 0.92)):
            sset = ax.streamplot(
                wind.lon, wind.lat, u, v, density=density, color=tuple(c / 255 for c in rgb),
                linewidth=linewidth + extra, arrowsize=1.9, maxlength=MAX_LENGTH, broken_streamlines=True,
            )
            sset.lines.set_alpha(alpha)
            sset.arrows.set_alpha(alpha)
        fig.canvas.draw()
        layer = np.asarray(fig.canvas.buffer_rgba()).copy()
    finally:
        plt.close(fig)
    if layer.shape[:2] != (height, width):
        raise WindError(f"streamline layer is {layer.shape[1]}x{layer.shape[0]}, expected {width}x{height}")
    return Image.alpha_composite(img, Image.fromarray(layer, "RGBA"))
