#!/usr/bin/env python3
"""Plan a backfill: which scans of a product are missing on the output branch, per region, in chunks.

Reads a file listing the output branch (``git ls-tree -r --name-only``) and prints a JSON list of
{"region": ..., "scans": "YYYYMMDDHHMM,YYYYMMDDHHMM,..."} entries for a GitHub Actions matrix. A region's scans are the
True Color scans it already has (True Color sits in the region's own folder, or the root for the first region) that
the product's folder lacks, so the product gets the same time series as True Color. Newest first within a chunk
order does not matter; chunks are filled oldest first.

    git ls-tree -r --name-only origin/himawari-output > tree.txt
    python scripts/backfill_plan.py tree.txt --product smoke --regions sumatra_borneo,mekong,papua
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import himawari_truecolor as h  # noqa: E402

NAME_RE = re.compile(r"^(\d{8})_(\d{4})\.png$")


def scans_in(tree: list[str], folder: str) -> set[str]:
    """Scan names (YYYYMMDD_HHMM) of the PNGs directly inside ``folder`` ('' = the root)."""
    out = set()
    prefix = f"{folder}/" if folder else ""
    for line in tree:
        if not line.startswith(prefix):
            continue
        rest = line[len(prefix):]
        m = NAME_RE.match(rest)  # a name without further slashes = directly in the folder
        if m:
            out.add(rest[:-4])
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tree", type=Path, help="file with one output-branch path per line")
    ap.add_argument("--product", default="smoke", choices=[k for k in h.PRODUCTS if h.PRODUCTS[k]["subdir"]])
    ap.add_argument("--regions", default=",".join(h.REGIONS))
    ap.add_argument("--chunk", type=int, default=12, help="scans per matrix job")
    ap.add_argument("--max-scans", type=int, default=200, help="most scans to plan per region (newest kept)")
    args = ap.parse_args(argv)

    tree = [l.strip() for l in args.tree.read_text().splitlines() if l.strip()]
    sub = h.PRODUCTS[args.product]["subdir"]
    matrix = []
    for region in (r.strip() for r in args.regions.split(",") if r.strip()):
        if region not in h.REGIONS:
            print(f"unknown region {region}", file=sys.stderr)
            return 2
        rdir = h.REGIONS[region]["subdir"]
        have_tc = scans_in(tree, rdir)  # True Color: the region folder itself ('' for the root)
        have_prod = scans_in(tree, "/".join(p for p in (rdir, sub) if p))
        missing = sorted(have_tc - have_prod)[-args.max_scans:]
        for i in range(0, len(missing), args.chunk):
            chunk = missing[i:i + args.chunk]
            matrix.append({"region": region, "scans": ",".join(n.replace("_", "") for n in chunk)})
        print(f"{region}: {len(have_tc)} True Color scans, {len(have_prod)} {args.product}, {len(missing)} to build",
              file=sys.stderr)
    print(json.dumps(matrix))
    return 0


if __name__ == "__main__":
    sys.exit(main())
