#!/usr/bin/env python3
"""Generate a small static site (index.html + thumbnails) from directories of Himawari PNGs.

Each product has a directory holding ``YYYYMMDD_HHMM.png`` files (UTC scan times) plus
``latest.png``: True Color in the root, Day Fire in ``dayfire/``, IR Enhanced in ``ir/``; the same
three inside a region folder (``mekong/``) for each further region, shown as tabs on the page.  Used by the Himawari
workflow on the ``himawari-output`` branch, which is served by GitHub Pages, but it works
on any such directory:

    python scripts/build_site.py path/to/dir
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ir_scale  # noqa: E402

NAME_RE = re.compile(r"^(\d{8})_(\d{4})\.png$")
SGT = dt.timezone(dt.timedelta(hours=8))
THUMB_WIDTH = 480
PREVIEW_MAX = (1500, 1900)  # JPEG previews for the time slider: sharp enough to scrub and play, ~0.3-0.5 MB each

# NOAA/CIRA VIIRS Day Fire RGB Quick Guide, "RGB Interpretation" table.
DAY_FIRE_KEY = (
    ("#e0202b", "Fire / hot spot"),
    ("#8f4a3c", "New burn scar"),
    ("#4a8fd6", "Smoke"),
    ("#4cf5f5", "Low / warm cloud"),
    ("#c8c8c8", "Cold cloud (10.4 &micro;m below ~290 K, added)"),
    ("#2f8f3f", "Healthy vegetation"),
    ("#6b4a2b", "Bare ground / old burn scar / urban"),
    ("#050505", "Water"),
)

# One tab per region; ``dir`` is its folder on the branch ("" = the root, which keeps the original links and
# section ids). A region with no images yet gets no tab.
REGION_TABS = (
    dict(key="sumatra", title="Sumatra &amp; Borneo", dir="",
         sub="Sumatra, Malay Peninsula, Singapore and Borneo"),
    dict(key="mekong", title="Mekong", dir="mekong",
         sub="Mekong sub-region: Myanmar, Thailand, Laos, Cambodia, Vietnam and Yunnan"),
)

SECTIONS = (
    dict(
        key="truecolor", title="True Color", subdir="",
        blurb="JMA True Color RGB (R&nbsp;B03, G&nbsp;B02, B&nbsp;B01), Rayleigh-corrected, gamma 1.7.",
        key_items=(("#ff0000", "Fire pixel (3.9&nbsp;&micro;m, see note)"),),
        note="Red marks are fire pixels from Himawari's 3.9&nbsp;&micro;m band: warmer than 52&nbsp;&deg;C, more than 8&nbsp;K "
             "above their surroundings, not under cold cloud. It is a simple test, not an official fire product: pixels are "
             "2&nbsp;km (the marks are drawn slightly larger so they show), small fires are missed, and sunlit hot ground or "
             "glint can give false marks.",
    ),
    dict(
        key="ir", title="IR Enhanced", subdir="ir",
        blurb="True Color with a translucent colour overlay of the 10.4&nbsp;&micro;m infrared band (B13) by day; "
              "infrared only at night. Runs day and night.",
        key_items=None, gradient=True,
        note="Colours follow the CIMSS/SSEC GOES Clean IR Window enhancement, applied to brightness temperature. "
             "By day the overlay is clear at &minus;20&nbsp;&deg;C and 70&nbsp;% opaque from &minus;40&nbsp;&deg;C and colder, "
             "so the surface stays visible under cloud. Where the sun is down, warm surfaces are grey (black is "
             "about +47&nbsp;&deg;C, an estimate) and everything colder than &minus;20&nbsp;&deg;C is opaque colour. "
             "The two blend by sun angle (True Color in full up to 60&deg; zenith, grey IR at 90&deg;), so dawn and dusk fade smoothly. The infrared band has 2&nbsp;km pixels "
             "(lightly smoothed here). Cold cloud tops are high, thick, usually convective cloud.",
    ),
    dict(
        key="dayfire", title="Day Fire", subdir="dayfire",
        blurb="NOAA/CIRA Day Fire RGB on Himawari bands (R&nbsp;B07 3.9&nbsp;&micro;m thermal, scaled 27&ndash;60&nbsp;&deg;C, "
              "G&nbsp;B04 0.86&nbsp;&micro;m, B&nbsp;B03 0.64&nbsp;&micro;m), gamma 0.4/1/1. Fires show red.",
        key_items=DAY_FIRE_KEY,
        note="Recipe from the VIIRS Day Fire RGB quick guide, with the red minimum raised from 0 to 27&nbsp;&deg;C so "
             "tropical sea stays black. Over forest, smoke and cloud come out the same teal, so cold cloud "
             "(10.4&nbsp;&micro;m below ~290&nbsp;K) is shown grey; smoke and low, warm cloud keep their colour. "
             "The 0.86/0.64&nbsp;&micro;m channels are divided by cos(sun zenith), capped at 80&deg;, so brightness "
             "does not follow the sun (it overshoots when the sun is more than ~60&deg; from vertical; an amber "
             "low-sun note on the image marks that). "
             "Himawari's 3.9&nbsp;&micro;m band has 2&nbsp;km pixels (VIIRS: 375&nbsp;m), so only larger fires show. "
             "Warm, sunlit ground and sun glint on water can also look red, and thick cloud hides fires.",
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


def make_previews(folder: Path, names: list[str]) -> None:
    """Mid-size JPEGs (previews/<scan>.jpg) that the time slider shows while scrubbing or playing."""
    previews = folder / "previews"
    previews.mkdir(exist_ok=True)
    wanted = {n[:-4] + ".jpg" for n in names}
    for old in previews.glob("*.jpg"):
        if old.name not in wanted:
            old.unlink()
    for n in names:
        dest = previews / (n[:-4] + ".jpg")
        if dest.exists():
            continue
        with Image.open(folder / n) as im:
            im = im.convert("RGB")
            im.thumbnail(PREVIEW_MAX)
            im.save(dest, quality=80, optimize=True)


COMPARE_CSS = """
section.compare { margin-top:32px; scroll-margin-top:12px; }
.cmp-controls { display:flex; flex-wrap:wrap; gap:10px 16px; align-items:end; margin:8px 0 12px; }
.cmp-controls label { display:flex; flex-direction:column; gap:2px; font-size:.8rem; color:var(--muted); }
.cmp-controls select { font:inherit; font-size:.95rem; padding:6px 8px; border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--fg); max-width:100%; }
.cmp-zoom { display:flex; align-items:center; gap:6px; margin-left:auto; }
.cmp-zoom button { font:inherit; min-width:36px; height:34px; padding:0 10px; border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--fg); cursor:pointer; }
.cmp-zoom button:hover, .cmp-zoom button:focus-visible { border-color:var(--accent); }
.cmp-zoom .cmp-zl { min-width:3ch; text-align:center; font-size:.9rem; color:var(--muted); }
.cmp-frame { position:relative; margin:0 auto; background:#000; border:1px solid var(--line); border-radius:10px; overflow:hidden; user-select:none; -webkit-user-select:none; touch-action:pan-y; }
.cmp-frame.zoomed { touch-action:none; cursor:grab; }
.cmp-frame.panning { cursor:grabbing; }
.cmp-layer { position:absolute; inset:0; overflow:hidden; }
.cmp-layer.l { clip-path:inset(0 var(--cmp-right,50%) 0 0); }
.cmp-stage { position:absolute; inset:0; transform-origin:0 0; will-change:transform; }
.cmp-stage img { position:absolute; inset:0; width:100%; height:100%; object-fit:contain; display:block; pointer-events:none; }
.cmp-frame.crisp .cmp-stage img { image-rendering:pixelated; }
.cmp-bar { position:absolute; top:0; bottom:0; left:var(--cmp-pos,50%); width:0; border-left:2px solid #fff; box-shadow:0 0 0 1px rgba(0,0,0,.45); pointer-events:none; }
.cmp-bar::after { content:"\\2194"; position:absolute; top:50%; left:-19px; width:36px; height:36px; margin-top:-18px; border-radius:50%; background:#fff; color:#111; font-size:20px; line-height:36px; text-align:center; box-shadow:0 1px 6px rgba(0,0,0,.6); }
.cmp-grab { position:absolute; top:0; bottom:0; left:-22px; width:44px; cursor:ew-resize; pointer-events:auto; touch-action:none; }
.cmp-frame:focus-within .cmp-bar::after { outline:3px solid var(--accent); }
.cmp-kbd { position:absolute; left:0; bottom:0; width:1px; height:1px; opacity:0; pointer-events:none; }
.cmp-tag { position:absolute; top:8px; padding:3px 9px; border-radius:999px; background:rgba(0,0,0,.6); color:#fff; font-size:.8rem; pointer-events:none; z-index:2; }
.cmp-tag.l { left:8px; } .cmp-tag.r { right:8px; }
.cmp-msg { color:var(--muted); font-size:.9rem; margin:6px 0 0; }
.cmp-hint { color:var(--muted); font-size:.8rem; margin:6px 0 0; }
"""

COMPARE_JS = """
document.querySelectorAll("section.compare").forEach(function (box) {
  var data = JSON.parse(box.querySelector("script.cmp-data").textContent);
  var left = box.querySelector(".cmp-p-left"), right = box.querySelector(".cmp-p-right"), scan = box.querySelector(".cmp-scan");
  var frame = box.querySelector(".cmp-frame"), imgL = frame.querySelector(".cmp-left"), imgR = frame.querySelector(".cmp-right");
  var stages = frame.querySelectorAll(".cmp-stage"), range = frame.querySelector("input"), grab = frame.querySelector(".cmp-grab");
  var tagL = frame.querySelector(".l.cmp-tag"), tagR = frame.querySelector(".r.cmp-tag");
  var zl = box.querySelector(".cmp-zl"), msg = box.querySelector(".cmp-msg"), started = false;
  var Z_MAX = 8, CRISP_AT = 4, z = 1, tx = 0, ty = 0, pos = 50;
  var byId = {}; data.products.forEach(function (p) { byId[p.id] = p; });
  data.products.forEach(function (p) {
    [left, right].forEach(function (sel) { var o = document.createElement("option"); o.value = p.id; o.textContent = p.title; sel.appendChild(o); });
  });
  left.value = data.defaults[0]; right.value = data.defaults[1];
  function common() {
    var a = byId[left.value], b = byId[right.value], set = {};
    b.scans.forEach(function (s) { set[s.name] = 1; });
    return a.scans.filter(function (s) { return set[s.name]; });
  }
  function fill() {
    var keep = scan.value, list = common();
    scan.innerHTML = "";
    list.forEach(function (s) { var o = document.createElement("option"); o.value = s.name; o.textContent = s.label; scan.appendChild(o); });
    if (list.some(function (s) { return s.name === keep; })) scan.value = keep;
    msg.textContent = list.length ? "" : "No scan has both products (the other products are daytime only).";
    frame.style.display = list.length ? "" : "none";
  }
  function url(p) { return (p.prefix ? p.prefix + "/" : "") + scan.value + ".png"; }
  function show() {
    if (!started || !scan.value) return;
    imgL.src = url(byId[left.value]); imgR.src = url(byId[right.value]);
    tagL.textContent = byId[left.value].title; tagR.textContent = byId[right.value].title;
    imgL.alt = byId[left.value].title; imgR.alt = byId[right.value].title;
  }
  function setPos(p) {
    pos = Math.max(0, Math.min(100, p));
    frame.style.setProperty("--cmp-pos", pos + "%"); frame.style.setProperty("--cmp-right", (100 - pos) + "%");
    range.value = Math.round(pos);
  }
  function apply() {
    var w = frame.clientWidth, h = frame.clientHeight;
    z = Math.max(1, Math.min(Z_MAX, z));
    tx = Math.min(0, Math.max(w - w * z, tx)); ty = Math.min(0, Math.max(h - h * z, ty));
    var t = "translate(" + tx + "px," + ty + "px) scale(" + z + ")";
    stages.forEach(function (st) { st.style.transform = t; });
    frame.classList.toggle("zoomed", z > 1); frame.classList.toggle("crisp", z >= CRISP_AT);
    zl.textContent = (Math.round(z * 10) / 10) + "\\u00d7";
  }
  function zoomAt(nz, px, py) {   // keep the picture point under (px, py) fixed
    nz = Math.max(1, Math.min(Z_MAX, nz));
    tx = px - (px - tx) * nz / z; ty = py - (py - ty) * nz / z; z = nz; apply();
  }
  function local(e) { var r = frame.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top, r]; }
  var ptrs = {}, mode = null, pan0 = null, pinch0 = null;
  function nPtrs() { return Object.keys(ptrs).length; }
  function startPan(e) { var l = local(e); pan0 = { x: l[0], y: l[1], tx: tx, ty: ty }; mode = "pan"; frame.classList.add("panning"); }
  frame.addEventListener("pointerdown", function (e) {
    if (e.button > 0) return;
    ptrs[e.pointerId] = e; try { frame.setPointerCapture(e.pointerId); } catch (x) {}
    if (nPtrs() === 2) {
      var k = Object.keys(ptrs), a = ptrs[k[0]], b = ptrs[k[1]], r = frame.getBoundingClientRect();
      pinch0 = { d: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) || 1, z: z, mx: (a.clientX + b.clientX) / 2 - r.left, my: (a.clientY + b.clientY) / 2 - r.top, tx: tx, ty: ty };
      mode = "pinch"; frame.classList.remove("panning"); return;
    }
    if (e.target === grab || z === 1) { mode = "divider"; var l = local(e); setPos(l[0] / l[2].width * 100); }
    else startPan(e);
    e.preventDefault();
  });
  frame.addEventListener("pointermove", function (e) {
    if (!ptrs[e.pointerId]) return;
    ptrs[e.pointerId] = e;
    if (mode === "divider") { var l = local(e); setPos(l[0] / l[2].width * 100); }
    else if (mode === "pan" && pan0) { var m = local(e); tx = pan0.tx + (m[0] - pan0.x); ty = pan0.ty + (m[1] - pan0.y); apply(); }
    else if (mode === "pinch" && pinch0) {
      var k = Object.keys(ptrs), a = ptrs[k[0]], b = ptrs[k[1]], r = frame.getBoundingClientRect();
      var d = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) || 1;
      var nz = Math.max(1, Math.min(Z_MAX, pinch0.z * d / pinch0.d));
      tx = pinch0.mx - (pinch0.mx - pinch0.tx) * nz / pinch0.z; ty = pinch0.my - (pinch0.my - pinch0.ty) * nz / pinch0.z; z = nz;
      var mx = (a.clientX + b.clientX) / 2 - r.left, my = (a.clientY + b.clientY) / 2 - r.top;
      tx += mx - pinch0.mx; ty += my - pinch0.my; apply();
    }
  });
  function release(e) {
    delete ptrs[e.pointerId]; frame.classList.remove("panning");
    if (nPtrs() === 1 && z > 1) { var k = Object.keys(ptrs); startPan(ptrs[k[0]]); } else { mode = null; pan0 = null; pinch0 = null; }
  }
  frame.addEventListener("pointerup", release); frame.addEventListener("pointercancel", release);
  frame.addEventListener("dblclick", function (e) {
    if (e.target === grab) return;
    var l = local(e); if (z >= Z_MAX) { z = 1; tx = ty = 0; apply(); } else zoomAt(z * 2, l[0], l[1]);
  });
  frame.addEventListener("wheel", function (e) {   // Ctrl/Cmd + wheel, or a trackpad pinch (which sends Ctrl + wheel)
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault(); var l = local(e); zoomAt(z * Math.exp(-e.deltaY * (e.deltaMode ? 0.05 : 0.0025)), l[0], l[1]);
  }, { passive: false });
  box.querySelectorAll(".cmp-zoom button").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var w = frame.clientWidth, h = frame.clientHeight, a = btn.dataset.z;
      if (a === "reset") { z = 1; tx = ty = 0; apply(); } else zoomAt(a === "in" ? z * 1.5 : z / 1.5, w / 2, h / 2);
    });
  });
  [left, right].forEach(function (sel) { sel.addEventListener("change", function () { fill(); show(); }); });
  scan.addEventListener("change", show);
  range.addEventListener("input", function () { setPos(+range.value); });
  window.addEventListener("resize", apply);
  fill(); setPos(50); apply();
  function start() { if (!started) { started = true; show(); } }
  if ("IntersectionObserver" in window) {
    new IntersectionObserver(function (es, ob) { if (es.some(function (e) { return e.isIntersecting; })) { start(); ob.disconnect(); } }).observe(frame);
  } else { start(); }
});
"""

VIEWER_CSS = """
section.viewer h2 { margin-bottom:4px; }
.vw-controls { display:flex; flex-wrap:wrap; align-items:end; gap:10px 14px; margin:10px 0; }
.vw-controls label { display:flex; flex-direction:column; gap:2px; font-size:.8rem; color:var(--muted); }
.vw-controls select { font:inherit; font-size:.95rem; padding:6px 8px; border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--fg); max-width:100%; }
.vw-row { display:flex; flex-wrap:wrap; align-items:center; gap:8px 10px; margin:0 0 10px; }
.vw-row button { font:inherit; min-width:38px; height:34px; padding:0 12px; border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--fg); cursor:pointer; }
.vw-row button:hover, .vw-row button:focus-visible { border-color:var(--accent); }
.vw-row input[type=range] { width:100%; height:28px; margin:0; display:block; accent-color:var(--accent); cursor:pointer; }
.vw-track { position:relative; flex:1 1 260px; min-width:140px; height:44px; }
.vw-shade { position:absolute; left:0; width:0; top:28px; height:5px; border-radius:3px; background:var(--accent); opacity:.55; pointer-events:none; }
.vw-mk { position:absolute; top:30px; width:0; height:0; border-left:6px solid transparent; border-right:6px solid transparent; border-bottom:10px solid; margin-left:-6px; pointer-events:none; display:none; }
.vw-mk.s { color:#1a9a4a; } .vw-mk.e { color:#d33a3a; }
.vw-mk::after { content:attr(data-t); position:absolute; top:11px; left:-8px; width:22px; text-align:center; font:600 .65rem/1 system-ui,sans-serif; color:var(--muted); }
.vw-loop { color:var(--muted); font-size:.85rem; }
.vw-row .vw-pos { color:var(--muted); font-size:.8rem; min-width:7ch; text-align:right; }
.vw-row .vw-zl { min-width:3ch; text-align:center; font-size:.9rem; color:var(--muted); }
.vw-row .vw-sep { width:1px; height:24px; background:var(--line); margin:0 4px; }
.vw-frame { position:relative; margin:0 auto; background:#000; border:1px solid var(--line); border-radius:10px; overflow:hidden; user-select:none; -webkit-user-select:none; touch-action:pan-y; }
.vw-frame.zoomed { touch-action:none; cursor:grab; }
.vw-frame.panning { cursor:grabbing; }
.vw-stage { position:absolute; inset:0; transform-origin:0 0; will-change:transform; }
.vw-stage img { position:absolute; inset:0; width:100%; height:100%; object-fit:contain; display:block; pointer-events:none; }
.vw-frame.crisp .vw-stage img { image-rendering:pixelated; }
.vw-status { position:absolute; left:8px; bottom:8px; padding:3px 9px; border-radius:999px; background:rgba(0,0,0,.6); color:#fff; font-size:.78rem; pointer-events:none; }
.vw-meta { display:flex; flex-wrap:wrap; justify-content:space-between; gap:6px 18px; margin:8px 2px 0; }
.vw-meta span { color:var(--muted); }
.vw-hint { color:var(--muted); font-size:.8rem; margin:6px 0 0; }
"""

VIEWER_JS = """
document.querySelectorAll("section.viewer").forEach(function (box) {
  var data = JSON.parse(box.querySelector("script.vw-data").textContent);
  var selR = box.querySelector(".vw-region"), selP = box.querySelector(".vw-product");
  var frame = box.querySelector(".vw-frame"), stage = frame.querySelector(".vw-stage"), img = frame.querySelector("img");
  var status = frame.querySelector(".vw-status"), range = box.querySelector(".vw-range"), play = box.querySelector(".vw-play");
  var pos = box.querySelector(".vw-pos"), zl = box.querySelector(".vw-zl"), strong = box.querySelector(".vw-sgt"), utc = box.querySelector(".vw-utc");
  var links = box.querySelectorAll("a.vw-link");
  var shade = box.querySelector(".vw-shade"), mkS = box.querySelector(".vw-mk.s"), mkE = box.querySelector(".vw-mk.e"), loopTxt = box.querySelector(".vw-loop");
  var sName = null, eName = null;   // anchors, kept as scan names so they carry over when the product changes
  var Z_MAX = 8, CRISP_AT = 4, z = 1, tx = 0, ty = 0, idx = 0, tok = 0, timer = null, started = false, reg = null, prod = null;
  var keep = [];   // recently used Image objects (decoded pictures stay ready): the 24 newest
  function remember(im) { keep.push(im); if (keep.length > 24) keep.shift(); }
  function load(url) {
    return new Promise(function (ok, bad) { var im = new Image(); im.onload = function () { remember(im); ok(url); }; im.onerror = bad; im.src = url; });
  }
  data.regions.forEach(function (r, i) { var o = document.createElement("option"); o.value = i; o.textContent = r.title; selR.appendChild(o); });
  function full(i) { return prod.prefix + prod.scans[i].name + ".png"; }
  function prev(i) { return prod.prefix + "previews/" + prod.scans[i].name + ".jpg"; }
  function applyView() {
    var w = frame.clientWidth, h = frame.clientHeight;
    z = Math.max(1, Math.min(Z_MAX, z));
    tx = Math.min(0, Math.max(w - w * z, tx)); ty = Math.min(0, Math.max(h - h * z, ty));
    stage.style.transform = "translate(" + tx + "px," + ty + "px) scale(" + z + ")";
    frame.classList.toggle("zoomed", z > 1); frame.classList.toggle("crisp", z >= CRISP_AT);
    zl.textContent = (Math.round(z * 10) / 10) + "\\u00d7";
  }
  function zoomAt(nz, px, py) { nz = Math.max(1, Math.min(Z_MAX, nz)); tx = px - (px - tx) * nz / z; ty = py - (py - ty) * nz / z; z = nz; applyView(); }
  function setRegion(i) {
    reg = data.regions[i];
    frame.style.aspectRatio = reg.w + " / " + reg.h;
    frame.style.maxWidth = "min(100%, calc(88vh * " + reg.w + " / " + reg.h + "))";
    var keepId = prod ? prod.id : null;
    selP.innerHTML = "";
    reg.products.forEach(function (p, k) { var o = document.createElement("option"); o.value = k; o.textContent = p.title; selP.appendChild(o); });
    var k = 0; reg.products.forEach(function (p, j) { if (p.id === keepId) k = j; });
    selP.value = k; z = 1; tx = ty = 0; applyView(); setProduct(k, true);
  }
  function setProduct(k, newest) {
    var old = prod && prod.scans[idx] ? prod.scans[idx].name : null;
    prod = reg.products[k]; var n = prod.scans.length, j = n - 1;
    if (!newest && old) { j = 0; prod.scans.forEach(function (s, m) { if (s.name <= old) j = m; }); }   // keep the same time
    range.max = n - 1; range.disabled = n < 2;
    drawAnchors(); show(j, false);
  }
  function windowIdx() {   // [first, last] scan index of the loop
    var n = prod.scans.length, lo = 0, hi = n - 1, k;
    if (sName) { for (k = 0; k < n; k++) if (prod.scans[k].name >= sName) { lo = k; break; } }
    if (eName) { for (k = n - 1; k >= 0; k--) if (prod.scans[k].name <= eName) { hi = k; break; } }
    return lo > hi ? [0, n - 1] : [lo, hi];
  }
  function at(frac) { return "calc(8px + (100% - 16px) * " + frac + ")"; }   // where the slider thumb sits (about 16 px wide)
  function drawAnchors() {
    var n = prod.scans.length, w = windowIdx(), has = (sName || eName) && n > 1, d = Math.max(1, n - 1);
    mkS.style.display = sName && n > 1 ? "block" : "none"; mkE.style.display = eName && n > 1 ? "block" : "none";
    mkS.style.left = at(w[0] / d); mkE.style.left = at(w[1] / d);
    shade.style.display = has ? "block" : "none"; shade.style.left = at(w[0] / d); shade.style.width = "calc((100% - 16px) * " + ((w[1] - w[0]) / d) + ")";
    loopTxt.textContent = has ? "Loop: " + prod.scans[w[0]].sgt + " \\u2192 " + prod.scans[w[1]].sgt + " (" + (w[1] - w[0] + 1) + " scans)" : "Loop: all scans";
  }
  function label(i) {
    var s = prod.scans[i];
    strong.textContent = s.sgt; utc.textContent = s.utc; pos.textContent = (i + 1) + " / " + prod.scans.length;
    range.value = i; range.setAttribute("aria-valuetext", s.sgt);
    links.forEach(function (a) { a.href = full(i); });
    img.alt = "Himawari-9 " + prod.title + ", " + s.utc;
  }
  var settle = null;
  function show(i, quick) {
    idx = Math.max(0, Math.min(prod.scans.length - 1, i)); label(idx);
    if (!started) return;
    var my = ++tok, f = full(idx), p = prev(idx), done = false;
    function put(url, what) { if (my !== tok) return; img.src = url; status.textContent = what; status.style.display = what ? "" : "none"; }
    clearTimeout(settle);
    load(p).then(function () { if (!done) put(p, quick || timer ? "" : "loading full size\u2026"); }, function () {});
    if (timer) return;                                   // playing: previews only
    function fetchFull() {
      if (my !== tok) return;
      load(f).then(function () {
        done = true; put(f, "");
        if (my === tok) [idx + 1, idx - 1, idx + 2].forEach(function (k) { if (k >= 0 && k < prod.scans.length) load(full(k)).catch(function () {}); });
      }, function () { if (!done) put(p, "full size unavailable"); });
    }
    if (quick) settle = setTimeout(fetchFull, 450); else fetchFull();   // while scrubbing, wait until the slider rests
  }
  function stop() { if (timer) { clearInterval(timer); timer = null; play.textContent = "\\u25b6 Play"; show(idx, false); } }
  function start() {
    var w = windowIdx();
    play.textContent = "\\u275a\\u275a Pause";
    if (idx < w[0] || idx >= w[1]) show(w[0], true);
    var k = w[0]; (function warm() { if (k > w[1] || !timer) return; var im = new Image(); im.onload = im.onerror = warm; im.src = prev(k++); })();
    timer = setInterval(function () { var u = windowIdx(); show(idx + 1 > u[1] || idx < u[0] ? u[0] : idx + 1, true); }, 400);
  }
  // zoom and pan
  function local(e) { var r = frame.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; }
  var ptrs = {}, mode = null, pan0 = null, pinch0 = null;
  function nPtrs() { return Object.keys(ptrs).length; }
  function startPan(e) { var l = local(e); pan0 = { x: l[0], y: l[1], tx: tx, ty: ty }; mode = "pan"; frame.classList.add("panning"); }
  frame.addEventListener("pointerdown", function (e) {
    if (e.button > 0) return;
    ptrs[e.pointerId] = e; try { frame.setPointerCapture(e.pointerId); } catch (x) {}
    if (nPtrs() === 2) {
      var k = Object.keys(ptrs), a = ptrs[k[0]], b = ptrs[k[1]], r = frame.getBoundingClientRect();
      pinch0 = { d: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) || 1, z: z, mx: (a.clientX + b.clientX) / 2 - r.left, my: (a.clientY + b.clientY) / 2 - r.top, tx: tx, ty: ty };
      mode = "pinch"; frame.classList.remove("panning"); return;
    }
    if (z > 1) { startPan(e); e.preventDefault(); }
  });
  frame.addEventListener("pointermove", function (e) {
    if (!ptrs[e.pointerId]) return; ptrs[e.pointerId] = e;
    if (mode === "pan" && pan0) { var m = local(e); tx = pan0.tx + (m[0] - pan0.x); ty = pan0.ty + (m[1] - pan0.y); applyView(); }
    else if (mode === "pinch" && pinch0) {
      var k = Object.keys(ptrs), a = ptrs[k[0]], b = ptrs[k[1]], r = frame.getBoundingClientRect();
      var d = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) || 1, nz = Math.max(1, Math.min(Z_MAX, pinch0.z * d / pinch0.d));
      tx = pinch0.mx - (pinch0.mx - pinch0.tx) * nz / pinch0.z; ty = pinch0.my - (pinch0.my - pinch0.ty) * nz / pinch0.z; z = nz;
      tx += (a.clientX + b.clientX) / 2 - r.left - pinch0.mx; ty += (a.clientY + b.clientY) / 2 - r.top - pinch0.my; applyView();
    }
  });
  function release(e) {
    delete ptrs[e.pointerId]; frame.classList.remove("panning");
    if (nPtrs() === 1 && z > 1) { var k = Object.keys(ptrs); startPan(ptrs[k[0]]); } else { mode = null; pan0 = null; pinch0 = null; }
  }
  frame.addEventListener("pointerup", release); frame.addEventListener("pointercancel", release);
  frame.addEventListener("dblclick", function (e) { var l = local(e); if (z >= Z_MAX) { z = 1; tx = ty = 0; applyView(); } else zoomAt(z * 2, l[0], l[1]); });
  frame.addEventListener("wheel", function (e) {   // Ctrl/Cmd + wheel, or a trackpad pinch (which sends Ctrl + wheel)
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault(); var l = local(e); zoomAt(z * Math.exp(-e.deltaY * (e.deltaMode ? 0.05 : 0.0025)), l[0], l[1]);
  }, { passive: false });
  box.querySelectorAll(".vw-zoom").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var a = btn.dataset.z, w = frame.clientWidth, h = frame.clientHeight;
      if (a === "reset") { z = 1; tx = ty = 0; applyView(); } else zoomAt(a === "in" ? z * 1.5 : z / 1.5, w / 2, h / 2);
    });
  });
  box.querySelector(".vw-set-s").addEventListener("click", function () { sName = prod.scans[idx].name; if (eName && eName < sName) eName = null; drawAnchors(); });
  box.querySelector(".vw-set-e").addEventListener("click", function () { eName = prod.scans[idx].name; if (sName && sName > eName) sName = null; drawAnchors(); });
  box.querySelector(".vw-clear").addEventListener("click", function () { sName = eName = null; drawAnchors(); });
  selR.addEventListener("change", function () { stop(); setRegion(+selR.value); });
  selP.addEventListener("change", function () { stop(); setProduct(+selP.value, false); });
  range.addEventListener("input", function () { stop(); show(+range.value, true); });
  box.querySelector(".vw-prev").addEventListener("click", function () { stop(); show(idx - 1, true); });
  box.querySelector(".vw-next").addEventListener("click", function () { stop(); show(idx + 1, true); });
  play.addEventListener("click", function () { if (timer) stop(); else start(); });
  window.addEventListener("resize", applyView);
  setRegion(0);
  function begin() { if (!started) { started = true; show(idx, false); } }
  if ("IntersectionObserver" in window) {
    new IntersectionObserver(function (es, ob) { if (es.some(function (e) { return e.isIntersecting; })) { begin(); ob.disconnect(); } }).observe(frame);
  } else { begin(); }
});
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Himawari-9 RGB</title>
<meta name="description" content="Latest Himawari-9 True Color, IR Enhanced and Day Fire images of Sumatra, the Malay Peninsula, Singapore and Borneo.">
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
.scale {{ margin:12px 14px 4px; max-width:640px; }}
.scale div.bar {{ height:14px; border-radius:3px; border:1px solid var(--line); }}
.scale div.ticks {{ position:relative; height:1.2em; font-size:.75rem; color:var(--muted); margin-top:2px; }}
.scale div.ticks span {{ position:absolute; transform:translateX(-50%); }}
.scale p {{ margin:0 0 4px; font-size:.85rem; color:var(--muted); }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:10px; }}
.grid a {{ text-decoration:none; color:inherit; background:var(--card); border:1px solid var(--line); border-radius:8px; overflow:hidden; display:block; }}
.grid a:hover, .grid a:focus-visible {{ border-color:var(--accent); }}
.grid img {{ display:block; width:100%; height:auto; object-fit:cover; background:#000; }}
.hero img {{ max-height:88vh; object-fit:contain; }}
.tabs {{ display:flex; gap:8px; margin:0 0 16px; }}
.tabs button {{ font:inherit; padding:8px 18px; border:1px solid var(--line); border-radius:999px; background:var(--card); color:var(--fg); cursor:pointer; }}
.tabs button[aria-selected="true"] {{ background:var(--accent); border-color:var(--accent); color:#fff; }}
.js section.region:not(.active) {{ display:none; }}
section.region + section.region {{ margin-top:40px; border-top:1px solid var(--line); padding-top:8px; }}
.grid small {{ display:block; padding:6px 8px; color:var(--muted); font-size:.78rem; line-height:1.3; }}
{compare_css}
{viewer_css}
footer {{ margin-top:40px; color:var(--muted); font-size:.85rem; }}
</style>
</head>
<body>
<main>
<h1>Himawari-9 RGB</h1>
<p class="sub">JMA and NOAA/CIRA RGB recipes plus a 10.4&nbsp;&micro;m infrared product &middot; True Color and Day Fire are daytime only, IR Enhanced runs day and night</p>
{tabs}
{body}
<footer>
  Data: Himawari-9 AHI, JMA / NOAA NODD (<code>s3://noaa-himawari9</code>); recipes: JMA MSC Himawari True Color RGB Quick Guide,
  NOAA/CIRA VIIRS Day Fire RGB Quick Guide; IR colour scale after the CIMSS/SSEC GOES Clean IR Window enhancement;
  coastlines: Natural Earth; wind streamlines: ECMWF open data (CC-BY-4.0).
  Scan times are UTC; SGT = UTC+8. Page updated {built} UTC.
</footer>
</main>
<script>
{compare_js}
{viewer_js}
(function () {{
  var regions = document.querySelectorAll("section.region");
  if (regions.length < 2) return;
  document.documentElement.className += " js";
  var tabs = document.querySelectorAll(".tabs button");
  function show(key, keepHash) {{
    var found = false;
    regions.forEach(function (r) {{ if (r.dataset.region === key) found = true; }});
    if (!found) key = regions[0].dataset.region;
    regions.forEach(function (r) {{ r.classList.toggle("active", r.dataset.region === key); }});
    tabs.forEach(function (b) {{ b.setAttribute("aria-selected", b.dataset.region === key ? "true" : "false"); }});
    try {{ localStorage.setItem("himawari-region", key); }} catch (e) {{}}
    if (!keepHash) history.replaceState(null, "", "#" + key);
  }}
  tabs.forEach(function (b) {{ b.addEventListener("click", function () {{ show(b.dataset.region); }}); }});
  var start = null, hash = location.hash.slice(1);
  regions.forEach(function (r) {{
    if (r.dataset.region === hash || r.querySelector('[id="' + hash + '"]')) start = r.dataset.region;
  }});
  if (!start) {{ try {{ start = localStorage.getItem("himawari-region"); }} catch (e) {{}} }}
  show(start || regions[0].dataset.region, true);
}})();
</script>
</body>
</html>
"""


def section_html(sec: dict, folder: Path, names: list[str], rel: str, sec_id: str) -> str:
    prefix = f"{rel}/" if rel else ""
    newest = names[0]
    with Image.open(folder / newest) as im:
        width, height = im.size
    utc, sgt = fmt(scan_time(newest))
    latest = f"{prefix}{newest}?v={newest[:-4]}"  # cache-busting: the file name changes with each scan

    key = ""
    if sec["key_items"]:
        items = "".join(f'<li><i style="background:{c}"></i>{t}</li>' for c, t in sec["key_items"])
        key = f'<ul class="key">{items}</ul>'
    note = f'<p class="note">{sec["note"]}</p>' if sec["note"] else ""
    if sec.get("gradient"):
        span = ir_scale.WARM_C - ir_scale.COLD_C
        ticks = "".join(f'<span style="left:{(ir_scale.WARM_C - t) / span * 100:.1f}%">{t}</span>'
                        for t in ir_scale.LEGEND_TICKS_C)
        key = (f'<div class="scale"><p>Cloud-top temperature, 10.4&nbsp;&micro;m (&deg;C); warmer than '
               f'&minus;20&nbsp;&deg;C is grey</p><div class="bar" style="background:{ir_scale.css_gradient()}"></div>'
               f'<div class="ticks">{ticks}</div></div>')

    cards = []
    for n in names:
        u, s = fmt(scan_time(n))
        cards.append(
            f'<a href="{prefix}{n}"><img src="{prefix}thumbs/{n[:-4]}.jpg" alt="" loading="lazy" style="aspect-ratio:{width}/{height}">'
            f"<small>{html.escape(s)}<br>{html.escape(u)}</small></a>"
        )
    gallery = (
        f'<h3>Recent scans ({len(names)})</h3>\n<div class="grid">\n' + "\n".join(cards) + "\n</div>"
        if len(names) > 1 else ""
    )
    return f"""<section class="product" id="{sec_id}">
<h2>{sec['title']}</h2>
<p class="blurb">{sec['blurb']}</p>
<div class="hero">
  <a href="{latest}"><img src="{latest}" alt="Himawari-9 {sec['title']}, {utc}" width="{width}" height="{height}"></a>
  <div class="meta">
    <div><strong>{sgt}</strong><br><span>{utc}</span></div>
    <div><a href="{latest}" download>Download PNG</a> &middot; <a href="{latest}">Open full size</a></div>
  </div>
</div>
{key}{note}
{gallery}
</section>"""


def compare_html(reg: dict, entries: list, sec_id: str) -> str:
    """Before/after slider for the products of one region: ``entries`` = [(sec, folder, rel, names)]."""
    products = []
    for sec, _, rel, names, _size in entries:
        products.append(dict(id=sec["key"], title=sec["title"], prefix=rel, scans=[
            dict(name=n[:-4], label=" / ".join(fmt(scan_time(n))[::-1])) for n in names]))
    ids = [p["id"] for p in products]
    defaults = [ids[0], ids[1] if len(ids) > 1 else ids[0]]
    if "ir" in ids and "truecolor" in ids:
        defaults = ["truecolor", "ir"]
    width, height = entries[0][4]
    payload = json.dumps(dict(products=products, defaults=defaults)).replace("</", "<\\/")
    return f"""<section class="compare" id="{sec_id}">
<h2>Compare</h2>
<p class="blurb">Compare two products for the same scan: drag the round handle (or use the arrow keys) to move the divider.</p>
<div class="cmp-controls">
  <label>Left<select class="cmp-p-left"></select></label>
  <label>Right<select class="cmp-p-right"></select></label>
  <label>Scan (SGT / UTC)<select class="cmp-scan"></select></label>
  <div class="cmp-zoom" role="group" aria-label="Zoom">
    <button type="button" data-z="out" aria-label="Zoom out">&minus;</button>
    <span class="cmp-zl" aria-live="polite">1&times;</span>
    <button type="button" data-z="in" aria-label="Zoom in">+</button>
    <button type="button" data-z="reset">Reset</button>
  </div>
</div>
<div class="cmp-frame" style="aspect-ratio:{width}/{height}; max-width:min(100%, calc(88vh * {width} / {height}));">
  <div class="cmp-layer r"><div class="cmp-stage"><img class="cmp-right" alt="" decoding="async"></div></div>
  <div class="cmp-layer l"><div class="cmp-stage"><img class="cmp-left" alt="" decoding="async"></div></div>
  <div class="cmp-bar"><div class="cmp-grab"></div></div>
  <span class="cmp-tag l"></span><span class="cmp-tag r"></span>
  <input class="cmp-kbd" type="range" min="0" max="100" value="50" step="1" aria-label="Comparison position">
</div>
<p class="cmp-msg"></p>
<p class="cmp-hint">Zoom with the + / &minus; buttons, Ctrl (or &#8984;) + scroll, a trackpad or two-finger pinch, or double-click; drag the picture to pan when zoomed. Past 4&times; the real satellite pixels are shown.</p>
<script type="application/json" class="cmp-data">{payload}</script>
</section>"""


def viewer_html(vregions: list) -> str:
    """The high-resolution time viewer tab: pick region and product, scrub or play through the scans, zoom and pan."""
    payload = json.dumps(dict(regions=vregions)).replace("</", "<\\/")
    return f"""<section class="region viewer" data-region="viewer" id="viewer">
<h2>Time viewer</h2>
<p class="blurb">Step or play through the kept scans (last 2 days) at high resolution, then zoom and pan. Choose the region and product; the same time is kept when you change product. Set a start and an end anchor on the slider to loop just that stretch.</p>
<div class="vw-controls">
  <label>Region<select class="vw-region"></select></label>
  <label>Product<select class="vw-product"></select></label>
</div>
<div class="vw-row">
  <button type="button" class="vw-prev" aria-label="Previous scan">&#9664;</button>
  <button type="button" class="vw-play" aria-label="Play or pause">&#9654; Play</button>
  <button type="button" class="vw-next" aria-label="Next scan">&#9654;</button>
  <div class="vw-track">
    <input type="range" class="vw-range" min="0" max="0" value="0" step="1" aria-label="Scan time">
    <span class="vw-shade"></span><span class="vw-mk s" data-t="start" title="Start anchor"></span><span class="vw-mk e" data-t="end" title="End anchor"></span>
  </div>
  <span class="vw-pos"></span>
  <span class="vw-sep"></span>
  <button type="button" class="vw-zoom" data-z="out" aria-label="Zoom out">&minus;</button>
  <span class="vw-zl" aria-live="polite">1&times;</span>
  <button type="button" class="vw-zoom" data-z="in" aria-label="Zoom in">+</button>
  <button type="button" class="vw-zoom" data-z="reset">Reset</button>
</div>
<div class="vw-row">
  <button type="button" class="vw-set-s" title="Make the scan on screen the start of the loop">&#9667;| Set start</button>
  <button type="button" class="vw-set-e" title="Make the scan on screen the end of the loop">Set end |&#9657;</button>
  <button type="button" class="vw-clear">Clear</button>
  <span class="vw-loop" aria-live="polite"></span>
</div>
<div class="vw-frame" style="aspect-ratio:25/14;">
  <div class="vw-stage"><img alt="" decoding="async"></div>
  <span class="vw-status" style="display:none"></span>
</div>
<div class="vw-meta">
  <div><strong class="vw-sgt"></strong><br><span class="vw-utc"></span></div>
  <div><a class="vw-link" href="#" download>Download PNG</a> &middot; <a class="vw-link" href="#">Open full size</a></div>
</div>
<p class="vw-hint">Zoom with the + / &minus; buttons, Ctrl (or &#8984;) + scroll, a trackpad or two-finger pinch, or double-click; drag the picture to pan when zoomed. Past 4&times; the real satellite pixels are shown. While you scrub or play, mid-size previews are shown and the full-size image follows when you stop.</p>
<script type="application/json" class="vw-data">{payload}</script>
</section>"""


def build(root: Path) -> None:
    regions, summary, vregions = [], [], []
    for reg in REGION_TABS:
        sections, parts, compare_entries = [], [], []
        for sec in SECTIONS:
            rel = "/".join(p for p in (reg["dir"], sec["subdir"]) if p)
            folder = root / rel if rel else root
            names = sorted((p.name for p in folder.glob("*.png") if NAME_RE.match(p.name)), reverse=True) \
                if folder.is_dir() else []
            if not names:
                continue
            make_thumbs(folder, names)
            make_previews(folder, names)
            with Image.open(folder / names[0]) as im:
                size = im.size
            sections.append((sec, names))
            compare_entries.append((sec, folder, rel, names, size))
            # the first region keeps the original section ids (#truecolor ...), the others are prefixed
            sec_id = sec["key"] if not reg["dir"] else f"{reg['key']}-{sec['key']}"
            parts.append(section_html(sec, folder, names, rel, sec_id))
            summary.append(f"{reg['key']} {sec['title']}: {len(names)} scans (newest {names[0][:-4]})")
        if compare_entries:
            vregions.append(dict(key=reg["key"], title=html.unescape(reg["title"]), w=compare_entries[0][4][0],
                                 h=compare_entries[0][4][1], products=[
                dict(id=sec["key"], title=sec["title"], prefix=f"{rel}/" if rel else "", scans=[
                    dict(name=n[:-4], utc=fmt(scan_time(n))[0], sgt=fmt(scan_time(n))[1]) for n in sorted(names)])
                for sec, _, rel, names, _ in compare_entries]))
        if len(compare_entries) > 1:  # a slider needs two products
            cmp_id = "compare" if not reg["dir"] else f"{reg['key']}-compare"
            parts.append(compare_html(reg, [(a, b, c, d, e) for a, b, c, d, e in compare_entries], cmp_id))
        if parts:
            nav = ""
            if len(sections) > 1:
                ids = [s_["key"] if not reg["dir"] else f"{reg['key']}-{s_['key']}" for s_, _ in sections]
                links = [(i, s_["title"]) for i, (s_, _) in zip(ids, sections)]
                if len(compare_entries) > 1:
                    links.append((cmp_id, "Compare"))
                nav = "<nav>" + "".join(f'<a href="#{i}">{t}</a>' for i, t in links) + "</nav>"
            regions.append((reg, f'<section class="region" data-region="{reg["key"]}">\n'
                                 f'<p class="sub">{reg["sub"]}</p>\n{nav}\n' + "\n".join(parts) + "\n</section>"))
    if not regions:
        raise SystemExit(f"no YYYYMMDD_HHMM.png files in {root}")

    if any(len(p["scans"]) > 1 for r in vregions for p in r["products"]):
        regions.append((dict(key="viewer", title="Time viewer"), viewer_html(vregions)))
    tabs = ""
    if len(regions) > 1:
        tabs = ('<div class="tabs" role="tablist">' + "".join(
            f'<button type="button" role="tab" data-region="{r["key"]}" aria-selected="false">{r["title"]}</button>'
            for r, _ in regions) + "</div>")
    (root / "index.html").write_text(
        PAGE.format(tabs=tabs, body="\n".join(html_ for _, html_ in regions),
                    compare_css=COMPARE_CSS, compare_js=COMPARE_JS, viewer_css=VIEWER_CSS, viewer_js=VIEWER_JS,
                    built=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"))
    )
    (root / ".nojekyll").write_text("")  # serve files as-is, skip Jekyll
    print(f"Wrote {root / 'index.html'} ({'; '.join(summary)})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("directory", type=Path)
    build(ap.parse_args().directory)
