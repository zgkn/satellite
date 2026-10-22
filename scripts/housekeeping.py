#!/usr/bin/env python3
"""Delete published Himawari images older than a number of days.

Works on the output-branch layout: ``YYYYMMDD_HHMM.png`` files (UTC scan times) in the given
directory and in each of its sub-folders (``dayfire/`` ...).  Age is measured from the scan
time in the file name to now (UTC), not from file dates, which are meaningless after a git
checkout.  ``latest.png`` is never touched, and neither is anything named with --keep (the
workflow passes the images it has just published, so a historical scan older than the limit is
not deleted the moment it is made).

Thumbnails are not handled here: build_site.py rebuilds them and drops those whose PNG is gone.

    python scripts/housekeeping.py path/to/dir [--max-age-days 2] [--keep NAME ...] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

NAME_RE = re.compile(r"^(\d{8})_(\d{4})\.png$")


def scan_time(name: str) -> dt.datetime | None:
    m = NAME_RE.match(name)
    if not m:
        return None
    try:
        return dt.datetime.strptime(m[1] + m[2], "%Y%m%d%H%M").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def folders(root: Path):
    yield root
    for sub in sorted(p for p in root.iterdir() if p.is_dir() and p.name != "thumbs" and not p.name.startswith(".")):
        yield sub


def housekeeping(root: Path, max_age_days: float, now: dt.datetime, keep=(), dry_run: bool = False) -> int:
    """Remove images older than ``max_age_days``; returns how many were (or would be) removed."""
    cutoff = now - dt.timedelta(days=max_age_days)
    protected = set(keep)
    removed = 0
    print(f"Housekeeping: removing images with a scan time before {cutoff:%Y-%m-%d %H:%M} UTC "
          f"(older than {max_age_days:g} days at {now:%Y-%m-%d %H:%M} UTC){' [dry run]' if dry_run else ''}")
    for folder in folders(root):
        kept, gone = [], []
        for p in sorted(folder.glob("*.png")):
            t = scan_time(p.name)
            if t is None:  # latest.png and anything else that is not a timestamped scan
                continue
            if t < cutoff and p.name not in protected:
                gone.append(p)
            else:
                kept.append(p)
        for p in gone:
            if not dry_run:
                p.unlink()
        removed += len(gone)
        label = folder.relative_to(root).as_posix() if folder != root else "."
        newest_kept = kept[-1].name if kept else "-"
        oldest_kept = kept[0].name if kept else "-"
        print(f"  {label:10s} kept {len(kept):3d} (oldest {oldest_kept}, newest {newest_kept}), "
              f"removed {len(gone)}" + (": " + ", ".join(p.name for p in gone) if gone else ""))
    return removed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("directory", type=Path)
    ap.add_argument("--max-age-days", type=float, default=2.0, help="remove images older than this (default 2)")
    ap.add_argument("--keep", nargs="*", default=[], metavar="NAME",
                    help="file names that must not be removed (e.g. the images just published)")
    ap.add_argument("--now", default=None, help="pretend it is this UTC time, YYYYMMDDHHMM (for testing)")
    ap.add_argument("--dry-run", action="store_true", help="show what would be removed, delete nothing")
    args = ap.parse_args(argv)
    if not args.directory.is_dir():
        print(f"ERROR: {args.directory} is not a directory", file=sys.stderr)
        return 2
    if args.max_age_days <= 0:
        print("ERROR: --max-age-days must be positive", file=sys.stderr)
        return 2
    if args.now:
        now = dt.datetime.strptime(args.now, "%Y%m%d%H%M").replace(tzinfo=dt.timezone.utc)
    else:
        now = dt.datetime.now(dt.timezone.utc)
    housekeeping(args.directory, args.max_age_days, now, args.keep, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
