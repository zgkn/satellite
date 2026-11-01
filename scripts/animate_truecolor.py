#!/usr/bin/env python3
"""True Color animation: one frame per Himawari-9 scan between two times, with nothing but coastlines drawn on it.

Separate from the operational pipeline: himawari_truecolor.py is imported as it is (not modified) and each frame is
its True Color array (JMA recipe, gamma 1.7, Rayleigh on by default) with the green Natural Earth coastlines of the
operational images (--no-coastlines for the bare picture), and no caption, graticule, streamlines or fire marks.  Writes an MP4 (full size) and a GIF (reduced until it fits --max-mb), plus
frames.txt listing the scan time of every frame.

    python scripts/animate_truecolor.py --start 202610140600 --end 202610140800 --outdir out
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import himawari_truecolor as h  # noqa: E402

GIF_WIDTHS = (1250, 1000, 800, 640, 480)  # tried in order until the GIF is small enough


def run_ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], capture_output=True, text=True)
    if proc.returncode:
        raise h.UserError(f"ffmpeg failed: {proc.stderr.strip()[-600:]}")


def make_mp4(frames: Path, fps: float, out: Path) -> None:
    run_ffmpeg(["-framerate", f"{fps:g}", "-i", str(frames / "frame_%03d.png"),
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "slow", "-crf", "20",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)])


def make_gif(frames: Path, fps: float, out: Path, max_bytes: int) -> int:
    """Smallest-effort GIF that fits ``max_bytes``: shrink the width until it does. Returns the width used."""
    for width in GIF_WIDTHS:
        palette = frames / "palette.png"
        scale = f"fps={fps:g},scale='min({width},iw)':-1:flags=lanczos"  # never upscale a small box
        run_ffmpeg(["-framerate", f"{fps:g}", "-i", str(frames / "frame_%03d.png"),
                    "-vf", f"{scale},palettegen=stats_mode=diff", str(palette)])
        run_ffmpeg(["-framerate", f"{fps:g}", "-i", str(frames / "frame_%03d.png"), "-i", str(palette),
                    "-lavfi", f"{scale}[x];[x][1:v]paletteuse=dither=sierra2_4a:diff_mode=rectangle", "-loop", "0",
                    str(out)])
        size = out.stat().st_size
        h.log(f"  GIF at width {width}: {size / 1e6:.1f} MB")
        if size <= max_bytes:
            return width
    raise h.UserError(f"the GIF is still above {max_bytes / 1e6:.0f} MB at width {GIF_WIDTHS[-1]}; use fewer frames")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True, help="first scan YYYYMMDDHHMM (UTC)")
    ap.add_argument("--end", required=True, help="last scan YYYYMMDDHHMM (UTC), included")
    ap.add_argument("--step-min", type=int, default=10, help="minutes between frames (multiple of 10, default 10)")
    ap.add_argument("--region", choices=tuple(h.REGIONS), default="sumatra_borneo")
    ap.add_argument("--bbox", default=None, help="west,south,east,north (default: the region's)")
    ap.add_argument("--segments", default=None, help="full-disk segments (default: the region's)")
    ap.add_argument("--resolution", type=float, default=0.01)
    ap.add_argument("--rayleigh", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--coastlines", action=argparse.BooleanOptionalAction, default=True,
                    help="draw the green coastlines (default on; file names then end in _coast)")
    ap.add_argument("--coast-width", type=float, default=1.5, help="coastline width in output pixels")
    ap.add_argument("--gamma", type=float, default=None, help="gamma override (default 1.7, as in True Color)")
    ap.add_argument("--fps", type=float, default=4.0, help="frames per second (default 4)")
    ap.add_argument("--max-mb", type=float, default=19.0, help="size limit for the GIF in MB (default 19)")
    ap.add_argument("--name", default=None, help="output file name without extension (default: from start/end)")
    ap.add_argument("--outdir", default="output-animation")
    args = ap.parse_args(argv)

    try:
        region = h.REGIONS[args.region]
        bbox = h.parse_bbox(args.bbox or region["bbox"])
        segments = h.parse_segments(args.segments or region["segments"])
        h.check_segments_cover_bbox(bbox, segments)
        start, end = h.parse_scan_time(args.start), h.parse_scan_time(args.end)
        if end < start:
            raise h.UserError("end is before start")
        if args.step_min < 10 or args.step_min % 10:
            raise h.UserError("step-min must be a multiple of 10")
        if not shutil.which("ffmpeg"):
            raise h.UserError("ffmpeg is not installed")
        scans = []
        t = start
        while t <= end:
            scans.append(t)
            t += dt.timedelta(minutes=args.step_min)
        if not 2 <= len(scans) <= 60:
            raise h.UserError(f"{len(scans)} frames requested; need between 2 and 60")
    except h.UserError as exc:
        h.annotate("error", str(exc))
        return 2

    prod = h.PRODUCTS["true_color"]
    gammas = tuple(c[4] for c in prod["channels"])
    if args.gamma is not None:
        gammas = (args.gamma,) * 3
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    stem = args.name or (f"truecolor_{'box' if args.bbox else args.region}_{start:%Y%m%d_%H%M}-{end:%H%M}" + ("_coast" if args.coastlines else ""))
    base = os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()  # TMPDIR is pointed at each frame's scratch below
    frames = Path(tempfile.mkdtemp(prefix="frames_", dir=base))
    s3 = h.s3_client()
    done: list[dt.datetime] = []
    try:
        from PIL import Image
        from satpy import config as satpy_config

        for scan in scans:
            work = Path(tempfile.mkdtemp(prefix="anim_", dir=base))
            os.environ["TMPDIR"] = str(work)
            tempfile.tempdir = None
            satpy_config.set(tmp_dir=str(work))  # Satpy reads TMPDIR only once, so point its .bz2 unpacking here
            try:
                try:
                    _, keys = h.select_scan(s3, segments, scan, 3, list(prod["bands"]))
                except h.UserError as exc:
                    h.annotate("warning", f"{scan:%H:%M} UTC skipped: {str(exc).splitlines()[0]}")
                    continue
                h.log(f"--- frame {len(done) + 1}: {scan:%Y-%m-%d %H:%M} UTC")
                rgb = h.make_rgb(h.download(s3, keys, work / "hsd"), bbox, args.resolution, prod, gammas, args.rayleigh)
                img = Image.fromarray(rgb, "RGB")
                if args.coastlines:
                    img = h.draw_coastline(img.convert("RGBA"), bbox, args.resolution, args.coast_width,
                                           prod["coast_rgba"]).convert("RGB")
                img.save(frames / f"frame_{len(done):03d}.png", compress_level=1)
                done.append(scan)
            finally:
                shutil.rmtree(work, ignore_errors=True)
        if len(done) < 2:
            h.annotate("error", f"only {len(done)} usable frame(s); an animation needs at least 2")
            return 2

        (out / f"{stem}_frames.txt").write_text(
            "\n".join(f"{i + 1:3d}  {t:%Y-%m-%d %H:%M} UTC  {t.astimezone(h.SGT):%H:%M} SGT" for i, t in enumerate(done)) + "\n")
        mp4 = out / f"{stem}.mp4"
        make_mp4(frames, args.fps, mp4)
        h.log(f"Wrote {mp4} ({mp4.stat().st_size / 1e6:.1f} MB, {len(done)} frames)")
        gif = out / f"{stem}.gif"
        width = make_gif(frames, args.fps, gif, int(args.max_mb * 1e6))
        h.log(f"Wrote {gif} ({gif.stat().st_size / 1e6:.1f} MB, width {width} px)")
    finally:
        shutil.rmtree(frames, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
