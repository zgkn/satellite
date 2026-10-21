#!/usr/bin/env python3
"""Generate a small static site (index.html + thumbnails) in a directory of Himawari PNGs.

The directory holds ``YYYYMMDD_HHMM.png`` files (UTC scan times) plus ``latest.png``.
Used by the Himawari workflow on the ``himawari-output`` branch, which is served by
GitHub Pages, but it works on any such directory:

    python scripts/build_site.py path/to/dir
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import re
from pathlib import Path

from PIL import Image

NAME_RE = re.compile(r"^(\d{8})_(\d{4})\.png$")
SGT = dt.timezone(dt.timedelta(hours=8))
THUMB_WIDTH = 480


def scan_time(name: str) -> dt.datetime:
    m = NAME_RE.match(name)
    return dt.datetime.strptime(m[1] + m[2], "%Y%m%d%H%M").replace(tzinfo=dt.timezone.utc)


def fmt(t: dt.datetime) -> tuple[str, str]:
    return f"{t:%d %b %Y %H:%M} UTC", f"{t.astimezone(SGT):%d %b %Y %H:%M} SGT"


def make_thumbs(root: Path, names: list[str]) -> None:
    thumbs = root / "thumbs"
    thumbs.mkdir(exist_ok=True)
    wanted = {n[:-4] + ".jpg" for n in names}
    for old in thumbs.glob("*.jpg"):
        if old.name not in wanted:
            old.unlink()
    for n in names:
        dest = thumbs / (n[:-4] + ".jpg")
        if dest.exists():
            continue
        with Image.open(root / n) as im:
            im = im.convert("RGB")
            im.thumbnail((THUMB_WIDTH, THUMB_WIDTH * im.height // im.width))
            im.save(dest, quality=82, optimize=True)


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Himawari-9 True Color</title>
<meta name="description" content="Latest Himawari-9 True Color RGB of Sumatra, the Malay Peninsula, Singapore and Borneo.">
<style>
:root {{ --bg:#f6f7f9; --fg:#14181f; --muted:#5b6573; --card:#fff; --line:#dfe3e8; --accent:#0b63ce; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#0d1117; --fg:#e6edf3; --muted:#8b97a5; --card:#161b22; --line:#2a313a; --accent:#58a6ff; }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }}
main {{ max-width:1100px; margin:0 auto; padding:20px 16px 48px; }}
h1 {{ font-size:1.4rem; margin:0 0 4px; }}
h2 {{ font-size:1.05rem; margin:32px 0 12px; }}
p.sub {{ margin:0 0 16px; color:var(--muted); }}
a {{ color:var(--accent); }}
.hero {{ background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
.hero img {{ display:block; width:100%; height:auto; background:#000; }}
.meta {{ display:flex; flex-wrap:wrap; gap:6px 18px; justify-content:space-between; padding:12px 14px; }}
.meta strong {{ font-size:1.05rem; }}
.meta span {{ color:var(--muted); }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:10px; }}
.grid a {{ text-decoration:none; color:inherit; background:var(--card); border:1px solid var(--line); border-radius:8px; overflow:hidden; display:block; }}
.grid a:hover, .grid a:focus-visible {{ border-color:var(--accent); }}
.grid img {{ display:block; width:100%; height:auto; aspect-ratio:25/14; object-fit:cover; background:#000; }}
.grid small {{ display:block; padding:6px 8px; color:var(--muted); font-size:.78rem; line-height:1.3; }}
footer {{ margin-top:32px; color:var(--muted); font-size:.85rem; }}
</style>
</head>
<body>
<main>
<h1>Himawari-9 True Color</h1>
<p class="sub">Sumatra, Malay Peninsula, Singapore and Borneo &middot; JMA True Color RGB recipe &middot; daytime scans only</p>

<section class="hero">
  <a href="{latest}"><img src="{latest}" alt="Himawari-9 True Color, {utc}" width="2500" height="1400"></a>
  <div class="meta">
    <div><strong>{sgt}</strong><br><span>{utc}</span></div>
    <div><a href="{latest}" download>Download PNG</a> &middot; <a href="{latest}">Open full size</a></div>
  </div>
</section>
{gallery}
<footer>
  Data: Himawari-9 AHI, JMA / NOAA NODD (<code>s3://noaa-himawari9</code>); coastlines: Natural Earth; wind streamlines: ECMWF open data (CC-BY-4.0).
  Scan times are UTC; SGT = UTC+8.
  Page updated {built} UTC.
</footer>
</main>
</body>
</html>
"""


def build(root: Path) -> None:
    names = sorted((p.name for p in root.glob("*.png") if NAME_RE.match(p.name)), reverse=True)
    if not names:
        raise SystemExit(f"no YYYYMMDD_HHMM.png files in {root}")
    make_thumbs(root, names)

    newest = names[0]
    utc, sgt = fmt(scan_time(newest))
    latest = f"{newest}?v={newest[:-4]}"  # cache-busting: the file name changes with each scan

    cards = []
    for n in names:
        u, s = fmt(scan_time(n))
        cards.append(
            f'<a href="{n}"><img src="thumbs/{n[:-4]}.jpg" alt="" loading="lazy">'
            f"<small>{html.escape(s)}<br>{html.escape(u)}</small></a>"
        )
    gallery = (
        f"<h2>Recent scans ({len(names)})</h2>\n<div class=\"grid\">\n" + "\n".join(cards) + "\n</div>"
        if len(names) > 1
        else ""
    )

    (root / "index.html").write_text(
        PAGE.format(
            latest=latest, utc=utc, sgt=sgt, gallery=gallery,
            built=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"),
        )
    )
    (root / ".nojekyll").write_text("")  # serve files as-is, skip Jekyll
    print(f"Wrote {root / 'index.html'} ({len(names)} scans, newest {newest})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("directory", type=Path)
    build(ap.parse_args().directory)
