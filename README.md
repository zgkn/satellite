# satellite

## Himawari-9 RGB: True Color, Natural Fire Color and Day Fire (Sumatra, Malay Peninsula, Singapore, Borneo)

A GitHub Actions workflow builds RGB images from Himawari-9 AHI full-disk data in the public NOAA NODD bucket
(`s3://noaa-himawari9/AHI-L1b-FLDK/`, anonymous access, no AWS account needed). Each channel is scaled between a
minimum and maximum (reflectance 0-100 %, or brightness temperature for the thermal band), clipped to 0-1, then
raised to `1/gamma`. The images are built by hand with Satpy, not with its built-in composites (which add extra
corrections). Daytime only.

| Product | R | G | B | Gamma (R/G/B) | Recipe | Use |
|---|---|---|---|---|---|---|
| `true_color` | B03 0.64 µm | B02 0.51 µm | B01 0.47 µm | 1.7 | JMA | Natural-looking picture |
| `natural_fire_color` | B06 2.3 µm | B04 0.86 µm | B03 0.64 µm | 1.0 | JMA | Fire hotspots, burn scars, smoke, vegetation |
| `day_fire` | B07 3.9 µm (27-60 °C) | B04 0.86 µm | B03 0.64 µm | 0.4 / 1 / 1 | NOAA/CIRA (VIIRS Day Fire RGB, on AHI bands; red minimum raised from 0 to 27 °C for the tropics; cold cloud shown grey) | Thermal hotspots |

### How to run it

**GitHub web / mobile app:** open the repo → **Actions** → **Himawari RGB** → **Run workflow**. Every input has a
default, so you can just tap *Run workflow*. It takes about 4-5 minutes for all three products.

**Where the results go**

- Workflow artifact `himawari-rgb-<run id>` (all PNGs of the run).
- Orphan branch `himawari-output`, newest 48 images per product kept, rewritten as a single commit on every run so it
  never accumulates history:
  - True Color: `YYYYMMDD_HHMM.png` and `latest.png` at the top level, e.g.
    `https://github.com/<owner>/satellite/blob/himawari-output/latest.png` (works in the mobile app).
  - Natural Fire Color: the same names inside `naturalfire/`, e.g. `.../blob/himawari-output/naturalfire/latest.png`.
  - Day Fire: the same names inside `dayfire/`.
- The job summary shows the scan time, the image paths and links.

### Website (GitHub Pages)

Each run also regenerates `index.html` on the `himawari-output` branch via `scripts/build_site.py`: one section per
product with the latest image, download link and thumbnails of recent scans (works on phones), plus the colour key for
Fire Color. To serve it, enable Pages once:
**Settings → Pages → Build and deployment → Source: Deploy from a branch → Branch: `himawari-output`, folder `/ (root)` → Save.**
The site is then at `https://<owner>.github.io/satellite/` and updates after every run (allow a minute or two for Pages
to redeploy). Note that Pages sites are public, and on a private repository Pages needs a paid GitHub plan.

### Inputs

| Input | Default | Meaning |
|---|---|---|
| `datetime_utc` | blank | Scan time **in UTC**, `YYYYMMDDHHMM`, minute ending in 0 (scans are every 10 min). Blank = newest complete scan from the last ~3 h. Examples: 08:00 SGT = `0000` UTC, 10:00 SGT = `0200` UTC, so `202610210200`. |
| `product` | `all` | `true_color`, `natural_fire_color`, `day_fire` or `all`. With more than one, shared files (B03, B04) and the winds are fetched once. |
| `bbox` | `94.5,-6.5,119.5,7.5` | `west,south,east,north` in degrees. Default covers Sumatra, Peninsular Malaysia, Singapore and Borneo. A smaller box gives a smaller image at the same pixel size; add a finer `resolution` (e.g. `0.005`) to enlarge a fire area. B06 itself only has 2 km pixels. |
| `gamma` | blank | Override for `true_color` and `natural_fire_color`. Blank = each product's default (1.7 and 1.0, as in the JMA guides). Try `2.0` for hazy true color. `day_fire` ignores it (its recipe has different gammas per channel). |
| `segments` | `5-6` | Full-disk segments to download (1 = north … 10 = south). 5-6 cover roughly 10°N-10°S. |
| `resolution` | `0.01` | Output pixel size in degrees (0.01° ≈ 1 km, giving about 2500 × 1400 px for the default bbox). |
| `winds` | **on** | Overlay thin orange streamlines of ECMWF IFS 925 hPa wind (see below). If the ECMWF data cannot be fetched the run still publishes the images, without winds, and prints a warning. Locally: `--no-winds`, `--wind-level` (hPa), `--wind-spacing` (px). |
| `coastlines` | **on** | Draw coastlines from Natural Earth 1:10m (public domain, stored in `data/ne_10m_coastline.zip`) at 1.5 px width: green on True Color, magenta on the two fire products (green would be confused with the vegetation colour). Untick for clean images. Locally: `--no-coastlines`, `--coast-width`. |
| `cloud_cue` | **on** | **Day Fire only.** Show cold cloud (10.4 µm below ~290 K) in grey so it stands apart from smoke (see Day Fire). Locally: `--no-cloud-cue`, `--cloud-bt`. |
| `rayleigh` | **on** | **True Color only.** Apply Satpy's Rayleigh correction, which removes the blue atmospheric haze. It also applies the sun-zenith correction the modifier requires. Untick it for the plain JMA recipe, which is a bit faster. The first run downloads look-up tables; later runs reuse the cache. Fire Color never uses it (the JMA guide specifies none). |

Scan times are always UTC. Himawari data appear in the bucket roughly 20-30 min after the scan. The **0240 and 1440
UTC** full-disk scans are missing from the bucket every day (10:40 and 22:40 SGT; presumably satellite housekeeping),
so asking for those exact times fails with a "no files found" message; use the scan before or after. A blank
`datetime_utc` handles this automatically by walking back to the newest complete scan.

### Natural Fire Color

Recipe from the JMA MSC *Himawari Natural Fire Color RGB Quick Guide*: the same as Natural Color but with the 2.3 µm
band (B06) in red instead of 1.6 µm (B05). Min 0 %, max 100 %, gamma 1.0 for all three channels.

| Colour | Meaning |
|---|---|
| Red | Fire hotspots |
| Dark green | Healthy vegetation |
| Brown | Burn scars |
| Dark teal | Smoke |
| Cyan | Clouds |
| Black | Sea |

Limits (from the guide): daytime only; water and ice clouds are not distinguished (both cyan); cloud colour resembles
snow/ice. B06 has 2 km pixels, so a hotspot is a block of about 2 output pixels: it is a small red dot on a phone
screen. Open the full-size image, or use a smaller `bbox`. The reflectance is Satpy's plain albedo (radiance × the
file's albedo coefficient, no division by the cosine of the sun angle), which is what the guide uses, so the image
gets darker as the sun gets lower (late afternoon, the east/west edges).

### Day Fire

Recipe from the NOAA/CIRA *VIIRS Day Fire RGB Quick Guide*, run on Himawari bands: R = 3.9 µm brightness temperature
with gamma 0.4, G = 0.86 µm and B = 0.64 µm scaled 0-100 % with gamma 1. Gamma is applied as `value^(1/gamma)`, as
in the other products, so the red channel stays dark except over genuinely hot pixels.

**One deliberate change from the guide:** the red channel is scaled **27 to 60 °C**, not 0 to 60 °C. In the tropics
the sea and land are already about 27 °C and warmer at 3.9 µm, so with a 0 °C minimum every water pixel came out dark
red instead of the guide's "nearly black" (tested on the 08:00 UTC scan on 2026-10-21: sea red 35/255 → 0, the weak
red tint over land and sea dropped by ~98 %, and the saturated hotspots were unchanged). The caption on the image
shows the range (`R B07 27-60C`). To get the guide's exact scaling, set the minimum back to `273.15` in
`PRODUCTS["day_fire"]` in `scripts/himawari_truecolor.py`.

| Colour | Meaning |
|---|---|
| Red | Fire / hot spot |
| Reddish brown | New burn scar |
| Blue | Smoke |
| Cyan | Clouds |
| Green | Healthy vegetation |
| Brown | Bare ground, old burn scar, urban |
| Nearly black | Water |

**Cold cloud shown grey (an addition, not in the guide).** Over vegetation, smoke and cloud both come out the same
teal (measured on the 08:00 UTC scan: hue ~170° for both, only brightness differs), because the 0.86 µm channel is
already high over forest. So pixels whose 10.4 µm brightness temperature (B13) is below **290 K** are blended to grey,
fully grey by 280 K. Thick or high cloud is that cold; thin low-level smoke is about as warm as the ground and keeps
its colour. Checked on that scan: bright clouds (median 212 K) went fully grey, smoke patches over land were unchanged,
and 290 K was the best of 285/290/295 K (295 K started to grey hazy sea, 285 K left cloud tinted). Limits: low,
warm cloud (shallow cumulus) is still teal and can still look like smoke. Hot 3.9 µm pixels (above about 49 °C) are
never greyed, so a fire under cloud keeps its red. The caption says when the cue is on. Switch it off with the
`cloud_cue` input (`--no-cloud-cue` locally) or move the threshold with `--cloud-bt` (K). It needs one more small
band (B13, 2 km).

- **Not VIIRS resolution:** the guide's 3.7 µm channel is 375 m on VIIRS. Himawari's closest band, B07 (3.9 µm), has 2 km
  pixels, so only larger fires show. Himawari does cover the area every 10 minutes.
- **Limits (from the guide):** thick cloud hides fires; warm ground can look red like a small fire. At 3.9 µm the
  daytime signal also includes reflected sunlight, so sunlit, warm land can be redder than in the guide's examples.
- Brightness temperature is Satpy's calibration of B07. Each run logs each channel's 1st/50th/99th percentile so the
  scaling can be checked.

### Wind streamlines

Source: ECMWF open data (IFS, 0.25° ≈ 28 km) from the public bucket `s3://ecmwf-forecasts` (anonymous; only the
u, v and surface-pressure messages are fetched with byte-range requests, ~2 MB per forecast step).

- **Which forecast:** the newest published run (00/06/12/18 UTC) that started at or before the scan time. For a live
  scan that is a run from roughly 6-12 h earlier (publication lag); for an older scan it is a run just before it, so the
  forecast step is short. Winds are linearly interpolated in time between the two 3-hourly steps that bracket the
  scan time. The caption line shows the run and steps used, e.g. `run 21 Oct 00Z, +0/+3 h`.
- **Mountains:** where 925 hPa (~750 m) is below the ground (parts of the Barisan range, Borneo highlands) the model
  values are extrapolated, so streamlines are left out there instead of showing made-up winds.
- **Look:** orange lines with a faint dark edge, about 1-2 px wide (stronger wind = slightly thicker), about 45 px
  apart, with arrowheads showing direction. Layer order: image, streamlines, coastlines, caption.
- ECMWF data are CC-BY-4.0. It is a model field, not an observation, so streamlines can disagree with what the
  clouds show.

### What it checks

1. Lists the bucket first and fails with a clear message naming any missing band/segment (the bands of the chosen
   products × the requested segments: 6 files for one product, 14 for all three including the cloud-cue band). With a blank time it walks back from now
   and picks the newest scan in which all of those files exist.
2. Checks that the bbox lies inside the requested segments and, if not, fails telling you which segments are needed
   (e.g. a bbox reaching 15°N needs `4-6`) instead of producing a cropped or blank image.
3. Night check: if the sun is below the horizon at the bbox centre (`pyorbital` solar zenith ≥ 90°), the run is
   skipped with a notice (job stays green, no image). Near sunrise/sunset the east or west edge of the bbox can still be
   dark; a warning is printed then. Borneo and Sumatra are ~1.7 h of local time apart.

Downloaded HSD files are deleted after processing.

### Notes on image quality

- Aceh and the west coast of Sumatra are viewed at a much steeper angle than Borneo (they are ~45° of longitude
  from the sub-satellite point at 140.7°E), so detail there is a little coarser. This is geometry, not a bug.
- True Color: Rayleigh correction is on by default. With it off (the plain JMA recipe) the image has a bluish/grey
  veil, especially toward the edges and over haze. `gamma: 2.0` can help with smoke haze either way.
- Output is nearest-neighbour resampled to a lat/lon grid. Coastlines are Natural Earth 1:10m, which matches the
  ~1 km pixels; very small islets and reefs may be missing, and lines are drawn over clouds.

### Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # needs the `bzip2` command (Satpy uses it to read .bz2 files)
python scripts/himawari_truecolor.py --datetime-utc 202610210200 --outdir output
python scripts/himawari_truecolor.py --product natural_fire_color --no-winds --help
```

Exit status: `0` success or deliberate night skip (see `status` in `--json-out`), `2` expected failure (missing data,
bbox outside segments, bad input), `1` unexpected error. `--check-only` lists the bucket and runs the checks without
downloading anything. `--json-out` lists every image in `outputs`.
