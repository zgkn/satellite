# satellite

## Himawari-9 True Color (Sumatra, Malay Peninsula, Singapore, Borneo)

A GitHub Actions workflow builds a True Color RGB image from Himawari-9 AHI full-disk data in the public
NOAA NODD bucket (`s3://noaa-himawari9/AHI-L1b-FLDK/`, anonymous access, no AWS account needed).

Recipe (JMA True Color RGB Quick Guide): R = B03 (0.64 µm), G = B02 (0.51 µm), B = B01 (0.47 µm); each band is
reflectance 0-100 %, clipped, divided by 100, then raised to `1/gamma`. It is built by hand with Satpy, not with
the built-in `true_color` composite (which adds extra corrections). Daytime only.

### How to run it

**GitHub web / mobile app:** open the repo → **Actions** → **Himawari True Color** → **Run workflow**. Every input
has a default, so you can just tap *Run workflow*. It takes about 2-3 minutes.

**Where the result goes**

- Workflow artifact `himawari-truecolor-<run id>` (the PNG).
- Orphan branch `himawari-output`: `YYYYMMDD_HHMM.png` plus `latest.png`, newest 48 images kept. Open
  `https://github.com/<owner>/satellite/blob/himawari-output/latest.png` (works in the mobile app).
  The branch is rewritten as a single commit on every run, so it never accumulates history.
- The job summary shows the scan time, the image path and links.

### Website (GitHub Pages)

Each run also regenerates `index.html` (latest image, download link, thumbnails of recent scans; works on phones) on
the `himawari-output` branch via `scripts/build_site.py`. To serve it, enable Pages once:
**Settings → Pages → Build and deployment → Source: Deploy from a branch → Branch: `himawari-output`, folder `/ (root)` → Save.**
The site is then at `https://<owner>.github.io/satellite/` and updates after every run (allow a minute or two for Pages
to redeploy). Note that Pages sites are public, and on a private repository Pages needs a paid GitHub plan.

### Inputs

| Input | Default | Meaning |
|---|---|---|
| `datetime_utc` | blank | Scan time **in UTC**, `YYYYMMDDHHMM`, minute ending in 0 (scans are every 10 min). Blank = newest complete scan from the last ~3 h. Examples: 08:00 SGT = `0000` UTC, 10:00 SGT = `0200` UTC, so `202610210200`. |
| `bbox` | `94.5,-6.5,119.5,7.5` | `west,south,east,north` in degrees. Default covers Sumatra, Peninsular Malaysia, Singapore and Borneo. |
| `gamma` | `1.7` | Gamma. Try `2.0` for hazy scenes. |
| `segments` | `5-6` | Full-disk segments to download (1 = north … 10 = south). 5-6 cover roughly 10°N-10°S. |
| `resolution` | `0.01` | Output pixel size in degrees (0.01° ≈ 1 km, giving about 2500 × 1400 px for the default bbox). |
| `coastlines` | **on** | Draw green coastlines from Natural Earth 1:10m (public domain, stored in `data/ne_10m_coastline.zip`) at 1.5 px width. Untick for a clean image. Locally: `--no-coastlines`, `--coast-width`. |
| `rayleigh` | **on** | Apply Satpy's Rayleigh correction, which removes the blue atmospheric haze. It also applies the sun-zenith correction the modifier requires. Untick it for the plain JMA recipe (no Rayleigh, as in the quick guide), which is a bit faster. The first run downloads look-up tables; later runs reuse the cache. |

Scan times are always UTC. Himawari data appear in the bucket roughly 20-30 min after the scan.

### What it checks

1. Lists the bucket first and fails with a clear message naming any missing band/segment (B01/B02/B03 × the
   requested segments = 6 files for the default). With a blank time it walks back from now and picks the newest scan
   in which all of those files exist.
2. Checks that the bbox lies inside the requested segments and, if not, fails telling you which segments are needed
   (e.g. a bbox reaching 15°N needs `4-6`) instead of producing a cropped or blank image.
3. Night check: if the sun is below the horizon at the bbox centre (`pyorbital` solar zenith ≥ 90°), the run is
   skipped with a notice (job stays green, no image). Near sunrise/sunset the east or west edge of the bbox can still be
   dark; a warning is printed then. Borneo and Sumatra are ~1.7 h of local time apart.

Downloaded HSD files are deleted after processing.

### Notes on image quality

- Aceh and the west coast of Sumatra are viewed at a much steeper angle than Borneo (they are ~45° of longitude
  from the sub-satellite point at 140.7°E), so detail there is a little coarser. This is geometry, not a bug.
- Rayleigh correction is on by default. With it off (the plain JMA recipe) the image has a bluish/grey veil,
  especially toward the edges and over haze. `gamma: 2.0` can help with smoke haze either way.
- Output is nearest-neighbour resampled to a lat/lon grid. Coastlines are Natural Earth 1:10m, which matches the
  ~1 km pixels; very small islets and reefs may be missing, and lines are drawn over clouds.

### Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # needs the `bzip2` command (Satpy uses it to read .bz2 files)
python scripts/himawari_truecolor.py --datetime-utc 202610210200 --outdir output
python scripts/himawari_truecolor.py --help
```

Exit status: `0` success or deliberate night skip (see `status` in `--json-out`), `2` expected failure (missing data,
bbox outside segments, bad input), `1` unexpected error. `--check-only` lists the bucket and runs the checks without
downloading anything.
