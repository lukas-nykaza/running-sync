#!/usr/bin/env python3
"""Download new COROS activities (via Intervals.icu) as original .fit files.

COROS -> Intervals.icu happens automatically once you connect COROS in
Intervals.icu settings. This script pulls the ORIGINAL files from Intervals.icu
into a local folder, skipping anything it already downloaded.

Usage:
    python3 sync_fit.py                      # new runs from the last 30 days
    python3 sync_fit.py --since 2023-01-01   # backfill from a date
    python3 sync_fit.py --dry-run            # show what would be downloaded

Setup: put your Intervals.icu API key in a file named .env next to this script:
    INTERVALS_API_KEY=your_key_here
(or set the INTERVALS_API_KEY environment variable). Standard library only.
"""

import argparse
import base64
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
API = "https://intervals.icu/api/v1"
DEFAULT_OUT = HERE / "COROS"
STATE_FILE = HERE / "state.json"
LOG_FILE = HERE / "sync.log"

# Intervals.icu activity types -> names in the HealthFit style fit_map.py expects
TYPE_NAMES = {
    "Run": "Outdoor Running", "VirtualRun": "Indoor Running", "TrailRun": "Trail Running",
    "Walk": "Walking", "Hike": "Hiking", "Ride": "Outdoor Cycling",
    "VirtualRide": "Indoor Cycling", "Swim": "Swimming", "WeightTraining": "Strength Training",
}
# Only these Intervals.icu activity types are downloaded
RUN_TYPES = {"Run", "VirtualRun", "TrailRun"}


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_api_key():
    key = os.environ.get("INTERVALS_API_KEY")
    env = HERE / ".env"
    if not key and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "INTERVALS_API_KEY":
                key = value.strip().strip('"').strip("'")
    if not key or key == "your_key_here":
        sys.exit("No API key. Add INTERVALS_API_KEY=... to .env (Intervals.icu > Settings > Developer Settings).")
    return key


def request(path, key, params=None):
    url = API + path
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    token = base64.b64encode(f"API_KEY:{key}".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {token}",
        "User-Agent": "running-sync/1.0 (personal FIT backup script)",
        "Accept": "*/*",
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:  # rate limited: back off and retry
                time.sleep(5 * (attempt + 1))
                continue
            raise


def file_name(act):
    start = datetime.fromisoformat(act["start_date_local"].replace("Z", ""))
    kind = TYPE_NAMES.get(act.get("type") or "",
                          re.sub(r"(?<=[a-z])(?=[A-Z])", " ", act.get("type") or "Activity"))
    if act.get("trainer") and kind == "Outdoor Running":
        kind = "Indoor Running"
    kind = re.sub(r'[\\/:*?"<>|-]', " ", kind).strip()
    return f"{start:%Y-%m-%d-%H%M%S}-{kind}-COROS.fit"


def main():
    p = argparse.ArgumentParser(description="Sync COROS .fit files from Intervals.icu.")
    p.add_argument("--since", help="oldest date to check, YYYY-MM-DD (default: 30 days ago)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"folder for .fit files (default: {DEFAULT_OUT})")
    p.add_argument("--dry-run", action="store_true", help="list what would be downloaded, save nothing")
    args = p.parse_args()

    key = load_api_key()
    oldest = args.since or (date.today() - timedelta(days=30)).isoformat()
    newest = (date.today() + timedelta(days=1)).isoformat()

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"downloaded": {}}
    done = state["downloaded"]

    try:
        acts = json.loads(request("/athlete/0/activities", key, {"oldest": oldest, "newest": newest}))
    except urllib.error.HTTPError as e:
        body = e.read()[:400].decode("utf-8", "replace").strip()
        hint = ""
        if e.code in (401, 403):
            hint = ("\nCheck that .env holds the API key from Intervals.icu > Settings > Developer Settings"
                    " (not your athlete ID like i12345), and that you clicked to generate/save it.")
        sys.exit(f"Could not list activities: HTTP {e.code}\nServer said: {body or '(empty)'}{hint}")
    except urllib.error.URLError as e:
        sys.exit(f"Network error: {e.reason}")

    runs = [a for a in acts if a.get("type") in RUN_TYPES]
    new = [a for a in runs if str(a.get("id")) not in done]
    log(f"{len(runs)} runs since {oldest} ({len(acts) - len(runs)} other activities ignored), {len(new)} new")
    args.out.mkdir(parents=True, exist_ok=True)

    saved = 0
    for act in sorted(new, key=lambda a: a.get("start_date_local", "")):
        aid = str(act["id"])
        if act.get("source") == "STRAVA" or not act.get("start_date_local"):
            continue  # Strava-sourced activities have no downloadable file
        name = file_name(act)
        if args.dry_run:
            print(f"  would save {name}")
            continue
        try:
            data = request(f"/activity/{aid}/file", key)
        except urllib.error.HTTPError as e:
            log(f"  skip {aid} ({name}): HTTP {e.code}")
            continue
        if data[:2] == b"\x1f\x8b":  # gzip-compressed
            data = gzip.decompress(data)
        if data[8:12] != b".FIT":
            log(f"  skip {aid} ({name}): original isn't a FIT file")
            done[aid] = "not-fit"
            continue
        (args.out / name).write_bytes(data)
        done[aid] = name
        saved += 1
        log(f"  saved {name}")

    if not args.dry_run:
        STATE_FILE.write_text(json.dumps(state, indent=1))
        log(f"done: {saved} file(s) saved to {args.out}")


if __name__ == "__main__":
    main()
