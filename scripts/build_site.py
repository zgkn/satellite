#!/usr/bin/env python3
"""Generate a small static site (index.html + thumbnails) from directories of Himawari PNGs.

Each product has a directory holding ``YYYYMMDD_HHMM.png`` files (UTC scan times) plus
``latest.png``: True Color in the root, Natural Fire Color in ``naturalfire/``.  Used by
the Himawari workflow on the ``himawari-output`` branch, which is served by GitHub Pages,
but it works on any such directory:

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

# JMA Natural Fire Color RGB Quick Guide, "Color interpretation" table.
FIRE_KEY = (
    ("#ff0000", "Fire hotspots"),
    ("#17512d", "Healthy vegetation"),
    ("#5e3b22", "Burn scars"),
    ("#0f5d73", "Smoke"),
    ("#4cf5f5", "Clouds"),
    ("#000000", "Sea surface"),
)

SECTIONS = (
    dict(
        key="truecolor", title="True Color", subdir="",
        blurb="JMA True Color RGB (R&nbsp;B03, G&nbsp;B02, B&nbsp;B01), Rayleigh-corrected, gamma 1.7.",
        key_items=None, note=None,
    ),
    dict(
        key="naturalfire", title="Natural Fire Color", subdir="naturalfire",
        blurb="JMA Natural Fire Color RGB (R&nbsp;B06 2.3&nbsp;&micro;m, G&nbsp;B04 0.86&nbsp;&micro;m, "
              "B&nbsp;B03 0.64&nbsp;&micro;m), gamma 1.0. Highlights fire hotspots, burn scars and smoke.",
        key_items=FIRE_KEY,
        note="Water and ice clouds are not distinguished (both cyan), and cloud colour resembles snow/ice. "
             "Hotspots are single 2&nbsp;km pixels, so they are small dots at this scale: open the full-size image.",
    ),
)


def scan_time(name: str) -> dt.datetime:
    m = NAME_RE.match(name)
    return dt.datetime.strptime(m[1] + m[2], "%Y%m%d%H%M").replace(tzinfo=dt.timezone.utc)


def fmt(t: dt.datetime) -> tuple[str, str]:
    return f"{t:%d %b %Y %H:%M} UTC", f"{t.astimezone(SGT):%d %b %Y %H:%M} SGT"


def make_thumbs(folder: Path, names: list[str]) -> None:
    thumbs = folder / "thumbs"
    thumbs.mkdir(exist_ok=True)
    wanted = {n[:-4] + ".jpg" for n in names}
    for old in thumbs.glob("*.jpg"):
        if old.name not in wanted:
            old.unlink()
    for n in names:
        dest = thumbs / (n[:-4] + ".jpg")
        if dest.exists():
            continue
        with Image.open(folder / n) as im:
            im = im.convert("RGB")
            im.thumbnail((THUMB_WIDTH, THUMB_WIDTH * im.height // im.width))
            im.save(dest, quality=82, optimize=True)


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Himawari-9 RGB</title>
<meta name="description" content="Latest Himawari-9 True Color and Natural Fire Color RGB of Sumatra, the Malay Peninsula, Singapore and Borneo.">
<style>
:root {{ --bg:#f6f7f9; --fg:#14181f; --muted:#5b6573; --card:#fff; --line:#dfe3e8; --accent:#0b63ce; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#0d1117; --fg:#e6edf3; --muted:#8b97a5; --card:#161b22; --line:#2a313a; --accent:#58a6ff; }}
}}
* {{ box-sizing:border-box; }}
html {{ scroll-behavior:smooth; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }}
main {{ max-width:1100px; margin:0 auto; padding:20px 16px 48px; }}
h1 {{ font-size:1.4rem; margin:0 0 4px; }}
h2 {{ font-size:1.2rem; margin:0 0 4px; }}
h3 {{ font-size:1.05rem; margin:24px 0 12px; }}
p.sub, p.blurb {{ margin:0 0 12px; color:var(--muted); }}
a {{ color:var(--accent); }}
nav {{ display:flex; flex-wrap:wrap; gap:8px; margin:0 0 8px; }}
nav a {{ text-decoration:none; padding:6px 12px; border:1px solid var(--line); border-radius:999px; background:var(--card); }}
section.product {{ margin-top:32px; scroll-margin-top:12px; }}
.hero {{ background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
.hero img {{ display:block; width:100%; height:auto; background:#000; }}
.meta {{ display:flex; flex-wrap:wrap; gap:6px 18px; justify-content:space-between; padding:12px 14px; }}
.meta strong {{ font-size:1.05rem; }}
.meta span {{ color:var(--muted); }}
ul.key {{ list-style:none; display:flex; flex-wrap:wrap; gap:6px 16px; margin:12px 0 4px; padding:0; }}
ul.key li {{ display:flex; align-items:center; gap:6px; font-size:.9rem; }}
ul.key i {{ width:16px; height:16px; border-radius:3px; border:1px solid var(--line); display:inline-block; }}
p.note {{ margin:4px 0 0; color:var(--muted); font-size:.85rem; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:10px; }}
.grid a {{ text-decoration:none; color:inherit; background:var(--card); border:1px solid var(--line); border-radius:8px; overflow:hidden; display:block; }}
.grid a:hover, .grid a:focus-visible {{ border-color:var(--accent); }}
.grid img {{ display:block; width:100%; height:auto; aspect-ratio:25/14; object-fit:cover; background:#000; }}
.grid small {{ display:block; padding:6px 8px; color:var(--muted); font-size:.78rem; line-height:1.3; }}
footer {{ margin-top:40px; color:var(--muted); font-size:.85rem; }}
</style>
</head>
<body>
<main>
<h1>Himawari-9 RGB</h1>
<p class="sub">Sumatra, Malay Peninsula, Singapore and Borneo &middot; JMA RGB recipes &middot; daytime scans only</p>
{nav}
{body}
<footer>
  Data: Himawari-9 AHI, JMA / NOAA NODD (<code>s3://noaa-himawari9</code>); recipes: JMA MSC Himawari RGB Quick Guides;
  coastlines: Natural Earth; wind streamlines: ECMWF open data (CC-BY-4.0).
  Scan times are UTC; SGT = UTC+8. Page updated {built} UTC.
</footer>
</main>
</body>
</html>
"""


def section_html(sec: dict, folder: Path, names: list[str]) -> str:
    prefix = f"{sec['subdir']}/" if sec["subdir"] else ""
    newest = names[0]
    utc, sgt = fmt(scan_time(newest))
    latest = f"{prefix}{newest}?v={newest[:-4]}"  # cache-busting: the file name changes with each scan

    key = ""
    if sec["key_items"]:
        items = "".join(f'<li><i style="background:{c}"></i>{html.escape(t)}</li>' for c, t in sec["key_items"])
        key = f'<ul class="key">{items}</ul>'
    note = f'<p class="note">{sec["note"]}</p>' if sec["note"] else ""

    cards = []
    for n in names:
        u, s = fmt(scan_time(n))
        cards.append(
            f'<a href="{prefix}{n}"><img src="{prefix}thumbs/{n[:-4]}.jpg" alt="" loading="lazy">'
            f"<small>{html.escape(s)}<br>{html.escape(u)}</small></a>"
        )
    gallery = (
        f'<h3>Recent scans ({len(names)})</h3>\n<div class="grid">\n' + "\n".join(cards) + "\n</div>"
        if len(names) > 1 else ""
    )
    return f"""<section class="product" id="{sec['key']}">
<h2>{sec['title']}</h2>
<p class="blurb">{sec['blurb']}</p>
<div class="hero">
  <a href="{latest}"><img src="{latest}" alt="Himawari-9 {sec['title']}, {utc}" width="2500" height="1400"></a>
  <div class="meta">
    <div><strong>{sgt}</strong><br><span>{utc}</span></div>
    <div><a href="{latest}" download>Download PNG</a> &middot; <a href="{latest}">Open full size</a></div>
  </div>
</div>
{key}{note}
{gallery}
</section>"""


def build(root: Path) -> None:
    sections, parts = [], []
    for sec in SECTIONS:
        folder = root / sec["subdir"] if sec["subdir"] else root
        names = sorted((p.name for p in folder.glob("*.png") if NAME_RE.match(p.name)), reverse=True) \
            if folder.is_dir() else []
        if not names:
            continue
        make_thumbs(folder, names)
        sections.append((sec, names))
        parts.append(section_html(sec, folder, names))
    if not parts:
        raise SystemExit(f"no YYYYMMDD_HHMM.png files in {root}")

    nav = ""
    if len(sections) > 1:
        nav = "<nav>" + "".join(f'<a href="#{s["key"]}">{s["title"]}</a>' for s, _ in sections) + "</nav>"
    (root / "index.html").write_text(
        PAGE.format(nav=nav, body="\n".join(parts),
                    built=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"))
    )
    (root / ".nojekyll").write_text("")  # serve files as-is, skip Jekyll
    summary = ", ".join(f"{s['title']}: {len(n)} scans (newest {n[0][:-4]})" for s, n in sections)
    print(f"Wrote {root / 'index.html'} ({summary})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("directory", type=Path)
    build(ap.parse_args().directory)
