# Running Sync

A private running dashboard that runs on your own Mac. It keeps a copy of every run as the
original `.fit` file and turns them into:

- **Weekly, monthly and yearly mileage** charts with summary tiles
- **Every run on a map**: splits in miles or km, pace / heart rate / elevation charts, route
  colored by pace or heart rate, steps and temperature
- **Explorer tiles**: every map tile you've run through, your biggest fully-visited square,
  and the new tiles each run discovered
- **A heatmap** of every route you've run
- **Shoe tracking**: assign a shoe to each run and watch its mileage add up

Your data never leaves your computer: no accounts on this project, no cloud. You can also open
the dashboard on your phone (see [On your phone](#on-your-phone)).

New runs arrive on their own: watch → your watch's app → [Intervals.icu](https://intervals.icu)
(free) → this project, every hour.

## What you need

- A Mac (the background jobs use macOS's `launchd`)
- Python 3.9 or newer. macOS includes it: run `python3 --version` in Terminal to check. There's
  nothing else to install.
- A free [Intervals.icu](https://intervals.icu) account connected to your watch. Built and
  tested with COROS; Intervals.icu also connects to Garmin, Polar, Suunto and Wahoo, which
  should work the same way.

## Setup

1. **Get the code.** In Terminal:
   ```
   git clone https://github.com/lukas-nykaza/running-sync.git
   cd running-sync
   ```
2. **Connect your watch to Intervals.icu.** Sign up at https://intervals.icu, then in
   Settings connect your watch (e.g. COROS). Intervals.icu imports your recent history.
3. **Add your API key.** In Intervals.icu Settings → **Developer Settings**, generate an API
   key. Then:
   ```
   cp .env.example .env
   open -e .env
   ```
   Replace `your_key_here` with the key and save. (`.env` is never uploaded anywhere.)
4. **Download your runs.** Pick how far back to go:
   ```
   python3 sync_fit.py --since 2024-01-01
   ```
   Runs are saved as `.fit` files in `COROS/`. Walks, rides and other activities are skipped.
5. **Keep them coming every hour**, in the background:
   ```
   bash install_schedule.sh
   ```
6. **Start the dashboard** (it runs in the background and starts when you log in):
   ```
   bash install_dashboard.sh
   ```
   Then open **http://127.0.0.1:8765** in your browser. The first start reads every file,
   which takes about 30 seconds for a few thousand runs; after that it's instant.

Tip: in Safari, **File → Add to Dock** makes the dashboard its own app on your Mac.

### Already have `.fit` files?

Drop them into `COROS/`, `HealthFit/` or `garmin/` (create the folder if needed) and reload
the dashboard. Any file name works, e.g. Garmin's `12345678_ACTIVITY.fit` or HealthFit exports;
files that aren't runs are skipped. If the same run is in two folders with the same start time,
it's counted once.

## On your phone

The dashboard works on a phone too, and you can add it to your Home Screen like an app. The
safe way to reach your Mac from your phone is [Tailscale](https://tailscale.com) (free), which
links your own devices privately:

1. Install Tailscale on the Mac and the phone, and log in to the same account on both.
2. Run `bash install_dashboard.sh` again; it prints your phone address, something like
   `http://your-mac.tailXXXX.ts.net:8765`. The dashboard's footer on the Mac shows it too.
3. On the phone, with Tailscale on, open that address in Safari, then
   Share → **Add to Home Screen**.

Only your Mac and devices signed in to your Tailscale account can open the dashboard; anyone
else on the same Wi-Fi is refused. Your Mac needs to be awake.

To use your own Home Screen icon, save a square image as `icon.png` in this folder, then remove
the Home Screen icon and add it again (iOS only reads the icon when it's added).

## Everyday use

| | |
|---|---|
| Open the dashboard | http://127.0.0.1:8765 (or `python3 dashboard.py`) |
| Download older runs | `python3 sync_fit.py --since 2023-01-01` |
| Check the hourly sync | `sync.log` gets a line every hour |
| Map one run on its own | `python3 fit_map.py 2026-10-18` (any words from the file name) |
| Stop the hourly sync | `bash install_schedule.sh --remove` |
| Stop the dashboard | `bash install_dashboard.sh --remove` |

After changing any `.py` file, restart the dashboard with `bash install_dashboard.sh`. Changes to
the `*_template.html` pages show up on the next reload.

If you move this folder, run both install scripts again so the background jobs point to the new
place.

## Your data

Everything stays in this folder and is listed in `.gitignore`, so it's never committed:

| File / folder | What it is |
|---|---|
| `COROS/`, `HealthFit/`, `garmin/` | Your runs (`.fit` files). Back these up. |
| `shoes.json` | Your shoes and which run used which. Back this up too. |
| `.env` | Your Intervals.icu API key |
| `state.json` | Which runs the sync already downloaded, so nothing downloads twice |
| `*_cache.json` | Distances, tiles and simplified routes; rebuilt automatically if deleted |
| `sync.log`, `*.out`, `*.err` | Logs |

## How it's built

Standard-library Python only, no packages to install. The pages are plain HTML and JavaScript,
with maps from [Leaflet](https://leafletjs.com) and map images from OpenStreetMap and Esri
(loaded from the internet when you view a map).

| File | What it does |
|---|---|
| `sync_fit.py` | Downloads new runs from Intervals.icu |
| `dashboard.py` | The local web server and the dashboard pages |
| `fit_map.py` | Reads `.fit` files; the single-run map page (also works on its own) |
| `explorer.py` | Explorer tiles, the biggest square and new tiles per run |
| `heatmap.py` | Simplified routes for the heatmap |
| `shoes.py` | Shoe mileage |
| `*_template.html` | The page layouts |
| `install_schedule.sh`, `install_dashboard.sh` | Set up the background jobs |

## Troubleshooting

- **"No runs yet"**: check `.env` has your key, then run `python3 sync_fit.py --dry-run`.
- **SSL certificate error** with Python from python.org: run "Install Certificates.command" from
  the Python folder in Applications.
- **HTTP 401/403 from Intervals.icu**: the key in `.env` is wrong; use the API key from
  Developer Settings, not your athlete ID.
- **Dashboard won't load**: run `bash install_dashboard.sh` again and check `dashboard.err`.

## License

MIT; see [LICENSE](LICENSE).
