#!/usr/bin/env python3
"""View a Garmin/ANT+ .fit activity as an interactive map in your browser.

Usage:
    python3 fit_map.py [search words | path.fit] [--dir FOLDER] [--port N] [--no-browser] [--output page.html]

Examples:
    python3 fit_map.py                  # asks which run to open
    python3 fit_map.py 2024-09-09       # the run from that day
    python3 fit_map.py 2023-05 hiking   # every word must appear in the file name

Search words are matched against the names of all .fit files under --dir (default:
the folder this script is in, including subfolders). If several files match you
pick one from a numbered list. The page is served on 127.0.0.1 until you press
Ctrl+C. No third-party Python packages needed; the browser loads Leaflet and map
tiles from the internet.
"""

import argparse
import bisect
import http.server
import json
import math
import re
import struct
import sys
import unicodedata
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

FIT_EPOCH = 631065600  # 1989-12-31T00:00:00Z as a Unix timestamp
SEMICIRCLE_TO_DEG = 180.0 / 2**31

# base type number -> (struct format, size in bytes, invalid value)
BASE_TYPES = {
    0x00: ("B", 1, 0xFF),  # enum
    0x01: ("b", 1, 0x7F),  # sint8
    0x02: ("B", 1, 0xFF),  # uint8
    0x03: ("h", 2, 0x7FFF),  # sint16
    0x04: ("H", 2, 0xFFFF),  # uint16
    0x05: ("i", 4, 0x7FFFFFFF),  # sint32
    0x06: ("I", 4, 0xFFFFFFFF),  # uint32
    0x07: ("s", 1, None),  # string
    0x08: ("f", 4, None),  # float32
    0x09: ("d", 8, None),  # float64
    0x0A: ("B", 1, 0x00),  # uint8z
    0x0B: ("H", 2, 0x0000),  # uint16z
    0x0C: ("I", 4, 0x00000000),  # uint32z
    0x0D: ("B", 1, 0xFF),  # byte
    0x0E: ("q", 8, 0x7FFFFFFFFFFFFFFF),  # sint64
    0x0F: ("Q", 8, 0xFFFFFFFFFFFFFFFF),  # uint64
    0x10: ("Q", 8, 0x0000000000000000),  # uint64z
}

# Global message numbers from the FIT profile
MSG_SESSION, MSG_LAP, MSG_RECORD, MSG_EVENT, MSG_ACTIVITY = 18, 19, 20, 21, 34
MSG_FIELD_DESCRIPTION = 206
METERS_PER_MILE = 1609.344

SPORTS = {
    0: "Activity", 1: "Run", 2: "Ride", 4: "Workout", 5: "Swim", 10: "Training",
    11: "Walk", 12: "Nordic ski", 13: "Ski", 14: "Snowboard", 15: "Row",
    16: "Mountaineering", 17: "Hike", 19: "Paddle", 21: "E-bike ride",
}
FOOT_SPORTS = {1, 11, 17}  # shown as pace, cadence in steps/min

# HealthFit export names: 2024-09-09-155556-Outdoor Running-Apple Watch.fit
HEALTHFIT_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-(\d{2})(\d{2})\d{2}-(.+)-[^-]+$")


# --------------------------------------------------------------------------- FIT decoding

def _decode_field(data, pos, size, base_type, endian):
    fmt, bsize, invalid = BASE_TYPES.get(base_type & 0x1F, ("B", 1, None))
    raw = data[pos:pos + size]
    if fmt == "s":
        return raw.split(b"\0", 1)[0].decode("utf-8", "replace") or None
    if size % bsize:
        return None
    count = size // bsize
    values = struct.unpack(endian + fmt * count, raw)
    if fmt in "fd":
        values = [None if raw[i * bsize:(i + 1) * bsize] == b"\xff" * bsize else v
                  for i, v in enumerate(values)]
    else:
        values = [None if v == invalid else v for v in values]
    return values[0] if count == 1 else values


def read_fit(path):
    """Return a list of (global message number, {field number: value}).

    Developer fields (extras an app adds, such as COROS's "Effort Pace") are included under
    "dev:<field name>" keys, decoded with the file's own field descriptions."""
    data = Path(path).read_bytes()
    messages = []
    pos = 0
    while pos + 12 <= len(data):  # a file may contain several chained FIT sections
        header_size = data[pos]
        if data[pos + 8:pos + 12] != b".FIT":
            raise ValueError(f"{path} is not a FIT file")
        data_size = struct.unpack_from("<I", data, pos + 4)[0]
        pos += header_size
        end = pos + data_size
        definitions = {}
        dev_described = {}  # (developer data index, field number) -> field_description values
        last_timestamp = None

        while pos < end:
            header = data[pos]
            pos += 1
            compressed_offset = None
            if header & 0x80:  # compressed timestamp header
                local_type = (header >> 5) & 0x03
                compressed_offset = header & 0x1F
            elif header & 0x40:  # definition message
                local_type = header & 0x0F
                endian = ">" if data[pos + 1] == 1 else "<"
                global_num = struct.unpack_from(endian + "H", data, pos + 2)[0]
                num_fields = data[pos + 4]
                pos += 5
                fields = [tuple(data[pos + 3 * i:pos + 3 * i + 3]) for i in range(num_fields)]
                pos += 3 * num_fields
                dev_fields = []
                if header & 0x20:  # developer fields: (field number, size, developer data index)
                    num_dev = data[pos]
                    pos += 1
                    dev_fields = [tuple(data[pos + 3 * i:pos + 3 * i + 3]) for i in range(num_dev)]
                    pos += 3 * num_dev
                definitions[local_type] = (global_num, endian, fields, dev_fields)
                continue
            else:  # data message
                local_type = header & 0x0F

            global_num, endian, fields, dev_fields = definitions[local_type]
            values = {}
            for field_num, size, base_type in fields:
                values[field_num] = _decode_field(data, pos, size, base_type, endian)
                pos += size
            for field_num, size, dev_index in dev_fields:
                desc = dev_described.get((dev_index, field_num))
                if desc and desc.get(3):
                    value = _decode_field(data, pos, size, desc.get(2) or 0x0D, endian)
                    if isinstance(value, (int, float)) and desc.get(6):  # scale and offset, if given
                        value = value / desc[6] - (desc.get(7) or 0)
                    values["dev:" + desc[3]] = value
                pos += size
            if global_num == MSG_FIELD_DESCRIPTION:
                dev_described[(values.get(0), values.get(1))] = values

            if values.get(253) is not None:
                last_timestamp = values[253]
            elif compressed_offset is not None and last_timestamp is not None:
                ts = (last_timestamp & ~0x1F) + compressed_offset
                if compressed_offset < (last_timestamp & 0x1F):
                    ts += 0x20
                values[253] = last_timestamp = ts
            messages.append((global_num, values))
        pos = end + 2  # skip file CRC
    return messages


# --------------------------------------------------------------------------- activity model

def _scaled(value, scale, offset=0.0):
    return None if value is None or isinstance(value, list) else value / scale - offset


def _first(values, *keys):
    for key in keys:
        if values.get(key) is not None:
            return values[key]
    return None


def _haversine(lat1, lon1, lat2, lon2):
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _pretty_title(path):
    healthfit = HEALTHFIT_NAME.match(Path(path).stem)
    if healthfit:
        return healthfit.group(4)
    stem = re.sub(r"[_\-\s]*\d{8,}$", "", Path(path).stem)
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", stem).replace("_", " ").strip()


def _paused_intervals(messages):
    """(stop, start) timestamps of every pause, from the watch's timer stop/start events."""
    paused, stopped_at = [], None
    timer = sorted((v[253], v.get(1)) for g, v in messages
                   if g == MSG_EVENT and v.get(0) == 0 and v.get(253) is not None)
    for ts, kind in timer:
        if kind in (1, 4) and stopped_at is None:  # stop / stop_all
            stopped_at = ts
        elif kind == 0 and stopped_at is not None:  # start again
            paused.append((stopped_at, ts))
            stopped_at = None
    return paused


def _splits(pts, unit_m):
    """Splits every `unit_m` meters (the last one partial), timed on moving time like the watch."""
    d, tt, hr, cad = pts["d"], pts["tt"], pts["hr"], pts["cad"]
    dist, last = [], 0.0
    for v in d:  # carry the last known distance over gaps
        last = v if v is not None else last
        dist.append(last)
    if len(dist) < 2 or dist[-1] < 1:
        return []
    splits, i0, t0, n = [], 0, 0.0, 1  # the clock starts when you press start, not at the first GPS fix
    i = 1
    while i < len(dist):
        target = n * unit_m
        if dist[i] >= target:
            a, b = dist[i - 1], dist[i]
            f = (target - a) / (b - a) if b > a else 1.0
            t_cross = tt[i - 1] + f * (tt[i] - tt[i - 1])
            splits.append((i0, i, target - (n - 1) * unit_m, t_cross - t0))
            i0, t0, n = i, t_cross, n + 1
            continue  # a long gap can cross several boundaries at once
        i += 1
    rest = dist[-1] - (n - 1) * unit_m
    if rest >= 10:  # the last, partial split
        splits.append((i0, len(dist) - 1, rest, tt[-1] - t0))

    def mean(values, a, b):
        vals = [v for v in values[a:b + 1] if v is not None]
        return round(sum(vals) / len(vals)) if vals else None
    return [{"n": k + 1, "range": [a, b], "distance": round(m, 1), "time": round(s, 1),
             "speed": m / s if s > 0 else None, "hr": mean(hr, a, b), "cad": mean(cad, a, b), "ascent": None}
            for k, (a, b, m, s) in enumerate(splits)]


def build_activity(path):
    messages = read_fit(path)
    session = next((v for g, v in messages if g == MSG_SESSION), {})
    activity = next((v for g, v in messages if g == MSG_ACTIVITY), {})
    records = [v for g, v in messages if g == MSG_RECORD]
    laps = [v for g, v in messages if g == MSG_LAP]

    sport_id = session.get(5) or 0
    foot = sport_id in FOOT_SPORTS
    cad_factor = 2 if foot else 1

    rows = [r for r in records if r.get(0) is not None and r.get(1) is not None and r.get(253)]
    if not rows:
        raise ValueError(f"{Path(path).name} has no GPS positions (indoor or manually logged?), nothing to map.")

    start_ts = session.get(2) or rows[0][253]
    pts = {k: [] for k in ("lat", "lon", "t", "d", "alt", "spd", "hr", "cad", "pwr")}
    for r in rows:
        cad = r.get(4)
        if cad is not None:
            cad = (cad + (r.get(53) or 0) / 128) * cad_factor
        alt = _scaled(_first(r, 78, 2), 5, 500)
        spd = _scaled(_first(r, 73, 6), 1000)
        dist = _scaled(r.get(5), 100)
        pts["lat"].append(round(r[0] * SEMICIRCLE_TO_DEG, 6))
        pts["lon"].append(round(r[1] * SEMICIRCLE_TO_DEG, 6))
        pts["t"].append(r[253] - start_ts)
        pts["d"].append(None if dist is None else round(dist, 1))
        pts["alt"].append(None if alt is None else round(alt, 1))
        pts["spd"].append(None if spd is None else round(spd, 3))
        pts["hr"].append(r.get(3))
        pts["cad"].append(None if cad is None else round(cad))
        pts["pwr"].append(r.get(7))

    n = len(pts["t"])
    if all(d is None for d in pts["d"]):  # derive distance from GPS
        total, pts["d"][0] = 0.0, 0.0
        for i in range(1, n):
            total += _haversine(pts["lat"][i - 1], pts["lon"][i - 1], pts["lat"][i], pts["lon"][i])
            pts["d"][i] = round(total, 1)
    if all(s is None for s in pts["spd"]):  # derive speed from distance
        pts["spd"] = [0.0] + [
            round((pts["d"][i] - pts["d"][i - 1]) / max(pts["t"][i] - pts["t"][i - 1], 1), 3)
            for i in range(1, n)
        ]

    # moving time at each point: elapsed time minus the pauses before it
    paused = [(a - start_ts, b - start_ts) for a, b in _paused_intervals(messages)]
    pts["tt"] = [t - sum(max(0, min(t, b) - a) for a, b in paused) for t in pts["t"]]
    splits = {"km": _splits(pts, 1000), "mi": _splits(pts, METERS_PER_MILE)}

    lap_list = []
    for i, lap in enumerate(laps):
        t0 = (lap.get(2) or start_ts) - start_ts
        t1 = (lap.get(253) or start_ts) - start_ts
        i0 = max(bisect.bisect_left(pts["t"], t0) - 1, 0)
        i1 = max(bisect.bisect_right(pts["t"], t1) - 1, i0)
        avg_cad = lap.get(17)
        lap_list.append({
            "n": i + 1,
            "range": [i0, i1],
            "distance": _scaled(lap.get(9), 100),
            "time": _scaled(lap.get(8), 1000),
            "speed": _scaled(_first(lap, 110, 13), 1000),
            "hr": lap.get(15),
            "cad": None if avg_cad is None else avg_cad * cad_factor,
            "ascent": lap.get(21),
        })

    # FIT timestamps are UTC; the activity message carries the local offset
    tz_offset = 0
    if activity.get(5) is not None and activity.get(253) is not None:
        tz_offset = activity[5] - activity[253]
    local = datetime.fromtimestamp(start_ts + FIT_EPOCH, timezone(timedelta(seconds=tz_offset)))
    when = f"{local:%a, %b} {local.day}, {local.year} · {local.hour % 12 or 12}:{local:%M %p}"

    ascent = session.get(22)
    if ascent is None:
        ascent, ref = 0, None
        for a in pts["alt"]:
            if a is None:
                continue
            if ref is None or a < ref:
                ref = a
            elif a - ref >= 3:  # ignore sub-3 m GPS/barometer jitter
                ascent += a - ref
                ref = a

    avg_cad = session.get(18)
    # steps: the watch's stride count (two steps each) when it has one, else added up from cadence
    steps = session.get(10) * 2 if foot and session.get(10) else None
    if steps is None and any(c is not None for c in pts["cad"]):
        steps = 0.0
        for i in range(1, n):
            dt = pts["tt"][i] - pts["tt"][i - 1]
            if pts["cad"][i] is not None and 0 < dt <= 10:
                steps += pts["cad"][i] * dt / 60
        steps = round(steps) if foot else None
    avg_effort = session.get("dev:Effort Pace")  # COROS: pace adjusted for hills and effort, as a speed (m/s)
    return {
        "title": _pretty_title(path) or SPORTS.get(sport_id, "Activity"),
        "file": Path(path).name,
        "sport": SPORTS.get(sport_id, "Activity"),
        "when": when,
        "usePace": foot or sport_id == 0,
        "cadUnit": "spm" if foot else "rpm",
        "summary": {
            "distance": _scaled(session.get(9), 100) or pts["d"][-1],
            "movingTime": _scaled(session.get(8), 1000) or pts["t"][-1],
            "elapsedTime": _scaled(session.get(7), 1000) or pts["t"][-1],
            "avgSpeed": _scaled(_first(session, 124, 14), 1000),
            "maxSpeed": _scaled(_first(session, 125, 15), 1000),
            "ascent": ascent,
            "descent": session.get(23),
            "calories": session.get(11),
            "avgHr": session.get(16),
            "maxHr": session.get(17),
            "avgCad": None if avg_cad is None else avg_cad * cad_factor,
            "avgPower": session.get(20),
            "steps": steps,
            "temperature": session.get(57),  # °C, measured by the watch on the wrist
            "avgEffort": avg_effort if isinstance(avg_effort, float) and avg_effort > 0 else None,
        },
        "points": pts,
        "laps": lap_list,
        "splits": splits,
    }


# --------------------------------------------------------------------------- page

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Activity map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin="">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>
<style>
  :root {
    color-scheme: light;
    --page: #f9f9f7;
    --surface: #fcfcfb;
    --ink: #0b0b0b;
    --ink-2: #52514e;
    --muted: #898781;
    --grid: #e1e0d9;
    --border: rgba(11, 11, 11, 0.10);
    --hover: rgba(11, 11, 11, 0.04);
    --selected: rgba(42, 120, 214, 0.12);
    --accent: #2a78d6;
    --explore: #eb6834;
    --visited: #2a78d6;
    --new-tile: #6250d6;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --page: #0d0d0d;
      --surface: #1a1a19;
      --ink: #ffffff;
      --ink-2: #c3c2b7;
      --muted: #898781;
      --grid: #2c2c2a;
      --border: rgba(255, 255, 255, 0.10);
      --hover: rgba(255, 255, 255, 0.05);
      --selected: rgba(57, 135, 229, 0.20);
      --accent: #3987e5;
      --explore: #d95926;
      --visited: #3987e5;
      --new-tile: #9085e9;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --border: rgba(255, 255, 255, 0.10); --hover: rgba(255, 255, 255, 0.05);
    --selected: rgba(57, 135, 229, 0.20); --accent: #3987e5; --explore: #d95926; --visited: #3987e5; --new-tile: #9085e9;
  }

  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    display: grid;
    grid-template-columns: 380px 1fr;
    background: var(--page);
    color: var(--ink);
    font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  aside {
    overflow-y: auto;
    padding: 20px 20px 28px;
    background: var(--surface);
    border-right: 1px solid var(--border);
  }
  #map { height: 100%; min-height: 320px; background: var(--page); }

  header { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
  .eyebrow { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }
  h1 { font-size: 20px; line-height: 1.25; margin: 2px 0 2px; font-weight: 650; }
  .when { color: var(--ink-2); font-size: 13px; }
  h2 { font-size: 12px; font-weight: 600; color: var(--muted); text-transform: uppercase;
       letter-spacing: 0.06em; margin: 0; }
  section { margin-top: 24px; }
  .section-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
  #lapModes { margin-bottom: 10px; }
  #lapModes[hidden] { display: none; }

  .tiles { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1px; background: var(--border);
           border: 1px solid var(--border); border-radius: 10px; overflow: hidden; margin-top: 18px; }
  .tile { background: var(--surface); padding: 10px 12px; min-width: 0; }
  .tile .value { font-size: 18px; font-weight: 600; white-space: nowrap; }
  .tile .value small { font-size: 12px; font-weight: 500; color: var(--ink-2); margin-left: 2px; }
  .tile .label { font-size: 12px; color: var(--muted); }
  .extra { color: var(--ink-2); font-size: 13px; margin-top: 10px; }

  .seg { display: inline-flex; flex-wrap: wrap; gap: 2px; padding: 2px; border-radius: 8px;
         background: var(--hover); border: 1px solid var(--border); }
  .seg button { font: inherit; font-size: 13px; color: var(--ink-2); background: none; border: 0;
                border-radius: 6px; padding: 4px 10px; cursor: pointer; white-space: nowrap; }
  #metrics { display: flex; width: 100%; }
  #metrics button { flex: 1 1 auto; padding: 4px 6px; }
  .seg button:hover { color: var(--ink); }
  .seg button[aria-pressed="true"] { background: var(--surface); color: var(--ink); font-weight: 600;
                                     box-shadow: 0 0 0 1px var(--border); }

  .legend { margin-top: 14px; }
  .legend .bar { display: flex; gap: 2px; height: 10px; }
  .legend .bar span { flex: 1; }
  .legend .bar span:first-child { border-radius: 4px 0 0 4px; }
  .legend .bar span:last-child { border-radius: 0 4px 4px 0; }
  .legend .ends { display: flex; justify-content: space-between; font-size: 12px; color: var(--ink-2);
                  margin-top: 4px; font-variant-numeric: tabular-nums; }
  .legend .key { display: flex; gap: 16px; font-size: 12px; color: var(--muted); margin-top: 10px; }
  .legend .key i { display: inline-block; width: 10px; height: 10px; border-radius: 50%;
                   vertical-align: -1px; margin-right: 5px; }
  .key .start { background: var(--surface); box-shadow: inset 0 0 0 2.5px var(--ink); }
  .key .finish { background: var(--ink); }

  #explorerStats .tiles { margin-top: 0; }
  .explorer-key { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 12px; color: var(--muted); margin-top: 10px; }
  .explorer-key i { display: inline-block; width: 10px; height: 10px; border-radius: 2px;
                    vertical-align: -1px; margin-right: 5px; }
  .explorer-key .visited { background: color-mix(in srgb, var(--visited) 45%, transparent);
                           box-shadow: inset 0 0 0 1px var(--visited); }
  .explorer-key .new { background: color-mix(in srgb, var(--new-tile) 60%, transparent);
                       box-shadow: inset 0 0 0 1px var(--new-tile); }
  .note { color: var(--muted); font-size: 12px; margin-top: 6px; }

  table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; font-size: 13px; }
  th { text-align: right; font-weight: 500; color: var(--muted); font-size: 12px; padding: 4px 6px;
       border-bottom: 1px solid var(--grid); }
  td { text-align: right; padding: 6px; border-bottom: 1px solid var(--grid); }
  th:first-child, td:first-child { text-align: left; }
  tbody tr { cursor: pointer; }
  tbody tr:hover { background: var(--hover); }
  tbody tr[aria-selected="true"] { background: var(--selected); }
  .link { font: inherit; font-size: 12px; color: var(--accent); background: none; border: 0; cursor: pointer; padding: 0; }

  .tip { position: absolute; z-index: 1000; pointer-events: none; display: none;
         background: var(--surface); color: var(--ink); border: 1px solid var(--border);
         border-radius: 8px; padding: 8px 10px; font-size: 12px; line-height: 1.5;
         box-shadow: 0 4px 16px rgba(0, 0, 0, 0.14); min-width: 150px; }
  .tip .head { font-weight: 600; margin-bottom: 2px; }
  .tip dl { display: grid; grid-template-columns: auto auto; gap: 0 14px; margin: 0; }
  .tip dt { color: var(--muted); }
  .tip dd { margin: 0; text-align: right; font-variant-numeric: tabular-nums; }
  .tip .on { font-weight: 600; color: var(--ink); }

  .dist-marker span { display: grid; place-items: center; width: 20px; height: 20px; border-radius: 50%;
    background: var(--surface); color: var(--ink); border: 1px solid var(--border);
    font: 600 10px/1 system-ui, -apple-system, "Segoe UI", sans-serif; box-shadow: 0 1px 3px rgba(0,0,0,.25); }

  .leaflet-container { font: inherit; }
  .leaflet-bar a, .leaflet-control-layers { background: var(--surface); color: var(--ink); border-color: var(--border); }
  .leaflet-bar a:hover { background: var(--page); }
  .leaflet-control-attribution { background: color-mix(in srgb, var(--surface) 80%, transparent) !important; color: var(--ink-2); }
  .leaflet-control-attribution a { color: var(--ink-2); }
  .leaflet-control-scale-line { background: color-mix(in srgb, var(--surface) 80%, transparent);
                                color: var(--ink); border-color: var(--ink-2); }
  .leaflet-control-layers-list { color: var(--ink); }

  .chart-hint { color: var(--muted); font-size: 12px; }
  .chart { margin-bottom: 10px; touch-action: pan-y; -webkit-user-select: none; user-select: none; }
  .chart-head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; font-size: 13px; }
  .chart-head .name { font-weight: 600; display: flex; align-items: center; gap: 6px; }
  .chart-head .name i { width: 10px; height: 3px; border-radius: 2px; display: inline-block; }
  .chart-head .read { color: var(--ink-2); font-variant-numeric: tabular-nums; }
  .chart svg { display: block; width: 100%; height: 96px; overflow: visible; }
  .chart.last svg { height: 116px; }
  .chart svg text { font-size: 10.5px; fill: var(--muted); font-variant-numeric: tabular-nums; }
  .chart svg text.avg-label { fill: var(--ink); font-weight: 600; paint-order: stroke; stroke: var(--surface); stroke-width: 3px; }
  @media (max-width: 760px) {
    body { display: flex; flex-direction: column; height: auto; }
    #map { order: -1; height: 62vh; flex: none; }
    aside { overflow: visible; border-right: 0; border-top: 1px solid var(--border); padding: 16px; }
    #metrics { overflow-x: auto; }
    .tile .value { font-size: 16px; }
  }
</style>
</head>
<body>
<aside>
  <header>
    <div>
      <div class="eyebrow" id="sport"></div>
      <h1 id="title"></h1>
      <div class="when" id="when"></div>
    </div>
    <div class="seg" id="units" role="group" aria-label="Units"></div>
  </header>
  <div class="tiles" id="tiles"></div>
  <div class="extra" id="extra"></div>

  <section id="chartSection">
    <div class="section-head"><h2>Charts</h2><span class="chart-hint">Drag across a chart</span></div>
    <div id="charts"></div>
  </section>

  <section>
    <div class="section-head"><h2>Color route by</h2></div>
    <div class="seg" id="metrics" role="group" aria-label="Color route by"></div>
    <div class="legend" id="legend"></div>
  </section>

  <section>
    <div class="section-head"><h2>Explorer tiles</h2>
      <div class="seg" id="explorerLevels" role="group" aria-label="Explorer tile level"></div></div>
    <div id="explorerStats"></div>
  </section>

  <section id="lapSection">
    <div class="section-head"><h2 id="lapTitle">Splits</h2><button class="link" id="showAll" hidden>Show whole route</button></div>
    <div class="seg" id="lapModes" role="group" aria-label="Splits or watch laps" hidden></div>
    <table id="laps"></table>
  </section>
</aside>
<div id="map"></div>

<script>
const D = /*__DATA__*/null;
const P = D.points;
const N = P.lat.length;
const LL = P.lat.map((lat, i) => [lat, P.lon[i]]);

const store = {
  get(k) { try { return localStorage.getItem('fitmap.' + k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem('fitmap.' + k, v); } catch {} },
};

// ---- units & formatting
const UNITS = {
  km: { m: 1000, dist: 'km', elev: 'm', elevF: 1, speed: 'km/h', speedF: 3.6 },
  mi: { m: 1609.344, dist: 'mi', elev: 'ft', elevF: 3.28084, speed: 'mph', speedF: 2.236936 },
};
let units = store.get('units') || (/^en-US|^en-LR|^my/.test(navigator.language) ? 'mi' : 'km');
const U = () => UNITS[units];
const pad = n => String(n).padStart(2, '0');
const fmtDuration = s => {
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = s % 60;
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
};
const num = (v, digits = 0) => v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
const distValue = m => num(m / U().m, 2);
const paceValue = mps => (mps > 0.3 ? fmtDuration(U().m / mps) : '–');
const speedValue = mps => num(mps * U().speedF, 1);
const rateValue = mps => (D.usePace ? paceValue(mps) : speedValue(mps));
const rateUnit = () => (D.usePace ? '/' + U().dist : U().speed);
const elevValue = m => num(m * U().elevF);

// ---- metrics available for coloring
function smooth(values, radius) {
  return values.map((_, i) => {
    let sum = 0, n = 0;
    for (let j = Math.max(0, i - radius); j <= Math.min(values.length - 1, i + radius); j++) {
      if (values[j] != null) { sum += values[j]; n++; }
    }
    return n ? sum / n : null;
  });
}
const METRICS = [
  { id: 'speed', label: D.usePace ? 'Pace' : 'Speed', values: smooth(P.spd, 3),
    fmt: v => `${rateValue(v)} ${rateUnit()}` },
  { id: 'hr', label: 'Heart rate', values: P.hr, fmt: v => `${Math.round(v)} bpm` },
  { id: 'alt', label: 'Elevation', values: smooth(P.alt, 2), fmt: v => `${elevValue(v)} ${U().elev}` },
  { id: 'cad', label: 'Cadence', values: P.cad, fmt: v => `${Math.round(v)} ${D.cadUnit}` },
  { id: 'pwr', label: 'Power', values: P.pwr, fmt: v => `${Math.round(v)} W` },
].filter(m => m.values.filter(v => v != null).length > N * 0.5);

function quantile(sorted, q) { return sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))]; }
for (const m of METRICS) {
  const sorted = m.values.filter(v => v != null).sort((a, b) => a - b);
  m.lo = quantile(sorted, 0.05);
  m.hi = quantile(sorted, 0.95);
  if (m.hi - m.lo < 1e-9) m.hi = m.lo + 1;
}
let metric = METRICS.find(m => m.id === store.get('metric')) || METRICS[0];

// ---- route colors: red = slower / harder, green = faster / easier (ColorBrewer RdYlGn, low to high).
// Pace, cadence and power go red -> green as values rise; heart rate and elevation are flipped,
// so a high heart rate or a climb shows red.
const RAMP = ['#a50026', '#d73027', '#f46d43', '#fdae61', '#fee08b', '#d9ef8b', '#a6d96a', '#66bd63', '#1a9850', '#006837'];
const RED_HIGH = new Set(['hr', 'alt']);
const darkQuery = matchMedia('(prefers-color-scheme: dark)');
const isDark = () => darkQuery.matches;
const ramp = () => (RED_HIGH.has(metric.id) ? [...RAMP].reverse() : RAMP);
const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function bucketOf(v) {
  if (v == null) return -1;
  const t = (v - metric.lo) / (metric.hi - metric.lo);
  return Math.max(0, Math.min(9, Math.floor(t * 10)));
}

// ---- map
const map = L.map('map', { preferCanvas: true, zoomSnap: 0.25 });
const ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services';
const canvasUrl = part => `${ESRI}/Canvas/World_${isDark() ? 'Dark' : 'Light'}_Gray_${part}/MapServer/tile/{z}/{y}/{x}`;
const canvasBase = L.tileLayer(canvasUrl('Base'), { maxZoom: 19, maxNativeZoom: 16,
  attribution: 'Tiles &copy; Esri, HERE, Garmin, &copy; OpenStreetMap contributors' });
const canvasLabels = L.tileLayer(canvasUrl('Reference'), { maxZoom: 19, maxNativeZoom: 16 });
const osmAttr = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
const baseLayers = {
  Map: L.layerGroup([canvasBase, canvasLabels]),
  Streets: L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: osmAttr }),
  Satellite: L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    { maxZoom: 19, attribution: 'Imagery &copy; Esri, Maxar, Earthstar Geographics' }),
};
const savedBase = store.get('base');
let baseName = baseLayers[savedBase] ? savedBase : 'Map';
baseLayers[baseName].addTo(map);
L.control.layers(baseLayers, null, { position: 'topright' }).addTo(map);
L.control.scale({ position: 'bottomleft' }).addTo(map);
map.on('baselayerchange', e => { baseName = e.name; store.set('base', e.name); explorerLayer.redraw(); });

// ---- explorer tiles: the OpenStreetMap tile grid at a fixed zoom, as used by VeloViewer,
// StatsHunters and Squadrats. A tile counts once the track enters it.
const EXPLORER_LEVELS = [14, 17];
const savedLevel = store.get('explorer');
let explorerZoom = savedLevel === 'off' ? null : EXPLORER_LEVELS.includes(+savedLevel) ? +savedLevel : 14;
const explorerCache = {};

function toTile(lat, lon, z) {
  const n = 2 ** z;
  return [(lon + 180) / 360 * n, (1 - Math.asinh(Math.tan(lat * Math.PI / 180)) / Math.PI) / 2 * n];
}
function metersBetween([lat1, lon1], [lat2, lon2]) {
  const r = Math.PI / 180, a = Math.sin((lat2 - lat1) * r / 2) ** 2 +
    Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin((lon2 - lon1) * r / 2) ** 2;
  return 2 * 6371008.8 * Math.asin(Math.sqrt(a));
}

function explorerTiles(z) {
  if (explorerCache[z]) return explorerCache[z];
  const seen = new Set(), tiles = [];
  const add = (x, y) => { const k = `${x},${y}`; if (!seen.has(k)) { seen.add(k); tiles.push([x, y]); } };
  let prev = null;
  for (let i = 0; i < N; i++) {
    const p = toTile(P.lat[i], P.lon[i], z), cx = Math.floor(p[0]), cy = Math.floor(p[1]);
    // walk every tile the segment from the previous point crosses; like VeloViewer, a jump
    // over 500 m is treated as a GPS gap rather than ground covered
    if (prev && metersBetween(LL[i - 1], LL[i]) <= 500) {
      let x = Math.floor(prev[0]), y = Math.floor(prev[1]);
      const dx = p[0] - prev[0], dy = p[1] - prev[1], sx = Math.sign(dx), sy = Math.sign(dy);
      let tx = dx ? ((sx > 0 ? x + 1 : x) - prev[0]) / dx : Infinity;
      let ty = dy ? ((sy > 0 ? y + 1 : y) - prev[1]) / dy : Infinity;
      for (let steps = Math.abs(cx - x) + Math.abs(cy - y); steps > 0; steps--) {
        if (tx < ty) { x += sx; tx += sx / dx; } else { y += sy; ty += sy / dy; }
        add(x, y);
      }
    }
    add(cx, cy);
    prev = p;
  }
  // largest n×n block of visited tiles: each tile extends the squares ending left, above and diagonally
  const sizes = new Map(), size = (x, y) => sizes.get(`${x},${y}`) || 0;
  let square = { n: 0 };
  for (const [x, y] of [...tiles].sort((a, b) => a[1] - b[1] || a[0] - b[0])) {
    const n = 1 + Math.min(size(x - 1, y), size(x, y - 1), size(x - 1, y - 1));
    sizes.set(`${x},${y}`, n);
    if (n > square.n) square = { n, x: x - n + 1, y: y - n + 1 };
  }
  return (explorerCache[z] = { tiles, square });
}

// Tiles this run reached before any earlier run, as keys x * 2^z + y. The dashboard fills in
// window.RUN_HISTORY; opened on its own (python3 fit_map.py) there's no history, so none are new.
const NEW_TILES = (window.RUN_HISTORY && window.RUN_HISTORY.newTiles) || null;
const newTileKeys = z => new Set((NEW_TILES && NEW_TILES[z]) || []);

function drawExplorerTile(ctx, { x, y, z }, px, dpr) {
  const E = explorerTiles(explorerZoom), sat = baseName === 'Satellite';
  const scale = 2 ** (explorerZoom - z), s = px / scale;  // explorer tiles per map tile, px per explorer tile
  const x0 = x * scale, y0 = y * scale;
  const dev = v => Math.round(v * dpr);  // snap to device pixels so neighbouring fills don't seam

  // Every tile the run visited is blue; tiles no earlier run had reached are purple. Each visited
  // tile gets its own edge so neighbouring tiles never read as one bigger shape.
  const fresh = newTileKeys(explorerZoom), n = 2 ** explorerZoom;
  const visible = E.tiles.filter(([tx, ty]) => !(tx + 1 <= x0 || ty + 1 <= y0 || tx >= x0 + scale || ty >= y0 + scale));
  const box = (tx, ty) => {
    const l = dev((tx - x0) * s), t = dev((ty - y0) * s);
    return [l, t, Math.max(1, dev((tx + 1 - x0) * s) - l), Math.max(1, dev((ty + 1 - y0) * s) - t)];
  };
  for (const isNew of [false, true]) {
    const color = css(isNew ? '--new-tile' : '--visited');
    const cells = visible.filter(([tx, ty]) => fresh.has(tx * n + ty) === isNew);
    ctx.fillStyle = color;
    ctx.globalAlpha = (sat ? 0.45 : isDark() ? 0.34 : 0.28) + (isNew ? 0.16 : 0);
    for (const [tx, ty] of cells) ctx.fillRect(...box(tx, ty));
    if (s >= 12) {  // an inset edge on every visited tile
      const w = Math.max(1, Math.round(1.5 * dpr));
      ctx.globalAlpha = 0.9;
      ctx.strokeStyle = color;
      ctx.lineWidth = w;
      for (const [tx, ty] of cells) {
        const [l, t, bw, bh] = box(tx, ty);
        ctx.strokeRect(l + w / 2 + dpr, t + w / 2 + dpr, bw - w - 2 * dpr, bh - w - 2 * dpr);
      }
    }
  }
  ctx.globalAlpha = 1;

  if (s >= 8) {  // grid lines; boundaries on a map-tile edge are drawn half by each neighbour
    const w = Math.max(1, Math.round(dpr)), off = w % 2 ? 0.5 : 0, end = px * dpr;
    ctx.lineWidth = w;
    ctx.strokeStyle = sat ? 'rgba(255, 255, 255, 0.55)' : isDark() ? 'rgba(255, 255, 255, 0.24)' : 'rgba(11, 11, 11, 0.24)';
    ctx.beginPath();
    for (let i = Math.ceil(x0); i <= x0 + scale; i++) { const p = dev((i - x0) * s) + off; ctx.moveTo(p, 0); ctx.lineTo(p, end); }
    for (let i = Math.ceil(y0); i <= y0 + scale; i++) { const p = dev((i - y0) * s) + off; ctx.moveTo(0, p); ctx.lineTo(end, p); }
    ctx.stroke();
  }
}

map.createPane('explorer').style.zIndex = 350;  // above the base map, below the route
const ExplorerGrid = L.GridLayer.extend({
  createTile(coords) {
    const tile = document.createElement('canvas'), px = this.getTileSize().x, dpr = window.devicePixelRatio || 1;
    tile.width = tile.height = px * dpr;
    if (explorerZoom != null) drawExplorerTile(tile.getContext('2d'), coords, px, dpr);
    return tile;
  },
});
const explorerLayer = new ExplorerGrid({ pane: 'explorer' }).addTo(map);

const casing = L.polyline(LL, { weight: 9, opacity: 0.85, interactive: false, lineCap: 'round', lineJoin: 'round' }).addTo(map);
const routeGroup = L.layerGroup().addTo(map);
const lapGroup = L.layerGroup().addTo(map);
const markerGroup = L.layerGroup().addTo(map);
const endpoints = L.layerGroup().addTo(map);
let selectedLap = null;

function drawSegments(group, i0, i1, opacity) {
  const colors = ramp(), values = metric.values, muted = css('--muted');
  let cur = null, pts = [];
  const flush = () => {
    if (pts.length > 1) {
      group.addLayer(L.polyline(pts, { color: cur < 0 ? muted : colors[cur], weight: 4.5, opacity,
        interactive: false, lineCap: 'round', lineJoin: 'round' }));
    }
  };
  for (let i = i0; i < i1; i++) {
    const b = bucketOf(values[i + 1] ?? values[i]);
    if (b !== cur) { flush(); cur = b; pts = [LL[i]]; }
    pts.push(LL[i + 1]);
  }
  flush();
}

function renderRoute() {
  routeGroup.clearLayers();
  lapGroup.clearLayers();
  casing.setStyle({ color: css('--surface'), opacity: selectedLap == null ? 0.85 : 0.5 });
  if (selectedLap == null) {
    drawSegments(routeGroup, 0, N - 1, 1);
  } else {
    // one translucent line: per-segment translucent lines would darken where their caps overlap
    routeGroup.addLayer(L.polyline(LL, { color: css('--muted'), weight: 4, opacity: 0.6,
      interactive: false, lineCap: 'round', lineJoin: 'round' }));
    const [a, b] = currentLaps()[selectedLap].range;
    lapGroup.addLayer(L.polyline(LL.slice(a, b + 1), { color: css('--surface'), weight: 9, opacity: 0.95,
      interactive: false, lineCap: 'round', lineJoin: 'round' }));
    drawSegments(lapGroup, a, b, 1);
  }
  endpoints.clearLayers();
  endpoints.addLayer(L.circleMarker(LL[0], { radius: 7, weight: 3, color: css('--ink'), fillColor: css('--surface'), fillOpacity: 1 })
    .bindTooltip('Start', { direction: 'top', offset: [0, -6] }));
  endpoints.addLayer(L.circleMarker(LL[N - 1], { radius: 7, weight: 2, color: css('--surface'), fillColor: css('--ink'), fillOpacity: 1 })
    .bindTooltip('Finish', { direction: 'top', offset: [0, -6] }));
}

function renderDistanceMarkers() {
  markerGroup.clearLayers();
  const unit = U().m, total = P.d[N - 1] || 0;
  const every = total / unit > 60 ? 5 : 1;
  let k = every, i = 0;
  while (k * unit <= total) {
    while (i < N && (P.d[i] ?? 0) < k * unit) i++;
    if (i >= N) break;
    markerGroup.addLayer(L.marker(LL[i], {
      interactive: false, keyboard: false,
      icon: L.divIcon({ className: 'dist-marker', html: `<span>${k}</span>`, iconSize: [20, 20], iconAnchor: [10, 10] }),
    }));
    k += every;
  }
}

// ---- hover: nearest track point within 22px
const tip = document.createElement('div');
tip.className = 'tip';
map.getContainer().appendChild(tip);
const hoverDot = L.circleMarker([0, 0], { radius: 6, weight: 2.5, interactive: false });
let proj = null, projKey = '', pendingEvent = null, frame = 0;

function projected() {
  const o = map.getPixelOrigin(), key = `${map.getZoom()}|${o.x}|${o.y}`;
  if (key !== projKey) {
    proj = LL.map(ll => map.latLngToLayerPoint(ll));
    projKey = key;
  }
  return proj;
}
function hideHover() { tip.style.display = 'none'; hoverDot.remove(); if (!chartPinned) setChartCursor(null, true); }
function showHover() {
  frame = 0;
  const mouse = pendingEvent.layerPoint, pts = projected();
  let best = -1, bestD = 22 * 22;
  for (let i = 0; i < N; i++) {
    const dx = pts[i].x - mouse.x, dy = pts[i].y - mouse.y, d2 = dx * dx + dy * dy;
    if (d2 < bestD) { bestD = d2; best = i; }
  }
  if (best < 0) return hideHover();

  hoverDot.setLatLng(LL[best]).setStyle({ color: css('--surface'), fillColor: css('--ink'), fillOpacity: 1 }).addTo(map);
  setChartCursor(best, true);
  const rows = [];
  for (const m of METRICS) {
    const v = m.values[best];
    if (v != null) rows.push(`<dt class="${m === metric ? 'on' : ''}">${m.label}</dt><dd class="${m === metric ? 'on' : ''}">${m.fmt(v)}</dd>`);
  }
  tip.innerHTML = `<div class="head">${distValue(P.d[best] ?? 0)} ${U().dist} · ${fmtDuration(P.t[best])}</div><dl>${rows.join('')}</dl>`;
  tip.style.display = 'block';
  const pt = map.latLngToContainerPoint(LL[best]), size = map.getSize();
  const w = tip.offsetWidth, h = tip.offsetHeight;
  tip.style.left = `${pt.x + 16 + w > size.x ? pt.x - 16 - w : pt.x + 16}px`;
  tip.style.top = `${Math.max(8, Math.min(size.y - h - 8, pt.y - h / 2))}px`;
}
map.on('mousemove', e => { pendingEvent = e; if (!frame) frame = requestAnimationFrame(showHover); });
map.on('mouseout movestart zoomstart', hideHover);

// ---- charts: pace, heart rate and elevation along the run's distance
const CHART_COLORS = { pace: '#2a78d6', hr: '#e34948', alt: '#1baf7a' };
const chartDist = (() => { let last = 0; return P.d.map(v => (last = v ?? last)); })();  // meters, gaps carried over
// speed over a rolling window: distance covered / moving time, so GPS jitter and pauses don't spike it
function rollingSpeed(seconds) {
  const tt = P.tt || P.t, out = new Array(N).fill(null);
  let a = 0, b = 0;
  for (let i = 0; i < N; i++) {
    while (tt[a] < tt[i] - seconds / 2) a++;
    while (b < N - 1 && tt[b + 1] <= tt[i] + seconds / 2) b++;
    const dt = tt[b] - tt[a], dd = chartDist[b] - chartDist[a];
    if (dt >= seconds * 0.5) out[i] = dd / dt;
  }
  return out;
}
const CHARTS = [
  // pace: speed is plotted, so faster is higher; walking pace and stops (under ~0.8 m/s) are left out
  { id: 'pace', label: D.usePace ? 'Pace' : 'Speed',
    values: rollingSpeed(20).map(v => (v != null && v > 0.8 ? v : null)), avgLine: true,
    average: () => D.summary.avgSpeed || (D.summary.distance / D.summary.movingTime) || null,
    show: v => `${rateValue(v)} ${rateUnit()}`, tick: v => rateValue(v) },
  { id: 'hr', label: 'Heart rate', values: smooth(P.hr, 2), avgLine: true,
    average: () => D.summary.avgHr || null,
    show: v => `${Math.round(v)} bpm`, tick: v => num(v) },
  { id: 'alt', label: 'Elevation', values: smooth(P.alt, 6), area: true,
    show: v => `${elevValue(v)} ${U().elev}`, tick: v => elevValue(v) },
].filter(c => c.values.filter(v => v != null).length > N * 0.5);
let chartCursor = null, chartPinned = false;

function chartRange(c) {
  const sorted = c.values.filter(v => v != null).sort((a, b) => a - b);
  let lo = quantile(sorted, c.id === 'pace' ? 0.02 : 0), hi = quantile(sorted, c.id === 'pace' ? 0.98 : 1);
  if (c.id === 'alt') { const minSpan = 10 / U().elevF; if (hi - lo < minSpan) { lo -= (minSpan - hi + lo) / 2; hi = lo + minSpan; } }
  const padding = (hi - lo || 1) * 0.08;
  return [lo - padding, hi + padding];
}
function chartAverage(c) {  // distance-weighted, so pauses don't count
  let sum = 0, w = 0;
  for (let i = 1; i < N; i++) {
    const v = c.values[i], dd = chartDist[i] - chartDist[i - 1];
    if (v != null && dd > 0) { sum += v * dd; w += dd; }
  }
  return w ? sum / w : null;
}

function renderCharts() {
  const host = document.getElementById('charts');
  document.getElementById('chartSection').hidden = !CHARTS.length;
  host.innerHTML = '';
  const total = chartDist[N - 1] || 1;
  CHARTS.forEach((c, k) => {
    const last = k === CHARTS.length - 1;
    const box = document.createElement('div');
    box.className = 'chart' + (last ? ' last' : '');
    box.innerHTML = `<div class="chart-head"><span class="name"><i style="background:${CHART_COLORS[c.id]}"></i>${c.label}</span>
      <span class="read"></span></div><svg role="img" aria-label="${c.label} along the run"></svg>`;
    host.appendChild(box);
    const svg = box.querySelector('svg'), W = svg.clientWidth || 340, H = last ? 116 : 96;
    const m = { l: 38, r: 4, t: 6, b: last ? 24 : 4 }, w = W - m.l - m.r, h = H - m.t - m.b;
    const [lo, hi] = chartRange(c);
    const X = d => m.l + d / total * w;
    const Y = v => m.t + (1 - (v - lo) / (hi - lo)) * h;
    c.geom = { X, Y, m, w, h, svg, lo, hi, read: box.querySelector('.read'), total };

    // one averaged point per pixel column keeps the path light even for long runs
    const cols = Math.max(2, Math.round(w)), sums = new Float64Array(cols), counts = new Uint32Array(cols);
    for (let i = 0; i < N; i++) {
      const v = c.values[i];
      if (v == null) continue;
      const col = Math.min(cols - 1, Math.floor(chartDist[i] / total * cols));
      sums[col] += Math.max(lo, Math.min(hi, v)); counts[col]++;
    }
    let line = '', area = '', open = false, start = 0, lastX = 0;
    for (let col = 0; col < cols; col++) {
      if (!counts[col]) { if (open && c.area) area += `L${lastX},${m.t + h}L${start},${m.t + h}Z`; open = false; continue; }
      const x = m.l + (col + 0.5) / cols * w, y = Y(sums[col] / counts[col]);
      if (!open) { line += `M${x},${y}`; if (c.area) area += `M${x},${m.t + h}L${x},${y}`; start = x; open = true; }
      else { line += `L${x},${y}`; if (c.area) area += `L${x},${y}`; }
      lastX = x;
    }
    if (open && c.area) area += `L${lastX},${m.t + h}L${start},${m.t + h}Z`;

    // three y labels (top, middle, bottom) with hairline grid
    const grid = [0, 0.5, 1].map(f => {
      const v = lo + (hi - lo) * f, y = m.t + (1 - f) * h;
      return `<line x1="${m.l}" x2="${W - m.r}" y1="${y}" y2="${y}" stroke="var(--grid)"/>` +
             `<text x="${m.l - 6}" y="${y + 3.5}" text-anchor="end">${c.tick(v)}</text>`;
    }).join('');
    // distance labels on the bottom chart
    let xs = '';
    if (last) {
      const unitTotal = total / U().m, step = [0.5, 1, 2, 5, 10, 20].find(s => unitTotal / s <= 6) || 50;
      for (let d = 0; d <= unitTotal + 1e-9; d += step) {
        const x = X(d * U().m);
        xs += `<text x="${x}" y="${H - 6}" text-anchor="${d === 0 ? 'start' : 'middle'}">${num(d, step < 1 ? 1 : 0)}</text>`;
      }
      xs += `<text x="${W - m.r}" y="${H - 6}" text-anchor="end">${U().dist}</text>`;
    }
    // a selected split or lap is shaded
    let band = '';
    if (selectedLap != null) {
      const [a, b] = currentLaps()[selectedLap].range;
      band = `<rect x="${X(chartDist[a])}" y="${m.t}" width="${Math.max(1, X(chartDist[b]) - X(chartDist[a]))}" height="${h}" fill="var(--selected)"/>`;
    }
    const color = CHART_COLORS[c.id];
    const avg = (c.average && c.average()) || chartAverage(c);
    // dotted average line: above it is faster pace / higher heart rate, below is slower / lower
    let avgLine = '';
    if (c.avgLine && avg != null && avg > lo && avg < hi) {
      const y = Y(avg);
      avgLine = `<line x1="${m.l}" x2="${W - m.r}" y1="${y}" y2="${y}" stroke="var(--ink)" stroke-opacity="0.75"
          stroke-width="1.25" stroke-dasharray="2 3" stroke-linecap="round"/>` +
        `<text x="${W - m.r - 2}" y="${y - 4}" text-anchor="end" class="avg-label">avg ${c.tick(avg)}</text>`;
    }
    svg.innerHTML = band + grid + xs +
      (c.area ? `<path d="${area}" fill="${color}" fill-opacity="0.12" stroke="none"/>` : '') +
      `<path d="${line}" fill="none" stroke="${color}" stroke-width="1.75" stroke-linejoin="round" stroke-linecap="round"/>` +
      avgLine +
      `<g class="cursor" visibility="hidden"><line y1="${m.t}" y2="${m.t + h}" stroke="var(--ink)" stroke-width="1"/>` +
      `<circle r="4" fill="${color}" stroke="var(--surface)" stroke-width="2"/></g>`;
    c.geom.avgText = avg == null ? '' : `avg ${c.show(avg)}`;
    c.geom.read.textContent = c.geom.avgText;

    // drag, hover or tap anywhere on a chart
    const at = e => {
      const r = svg.getBoundingClientRect(), d = (e.clientX - r.left - m.l) / w * total;
      let a = 0, b = N - 1;  // binary search: first point at or past that distance
      while (a < b) { const mid = (a + b) >> 1; if (chartDist[mid] < d) a = mid + 1; else b = mid; }
      setChartCursor(a);
    };
    svg.addEventListener('pointermove', at);
    svg.addEventListener('pointerdown', e => { chartPinned = e.pointerType !== 'mouse'; at(e); });
    svg.addEventListener('pointerleave', e => { if (e.pointerType === 'mouse') setChartCursor(null); });
  });
  if (chartCursor != null) setChartCursor(chartCursor);
}

function setChartCursor(i, fromMap = false) {
  chartCursor = i;
  for (const c of CHARTS) {
    if (!c.geom) continue;
    const g = c.geom.svg.querySelector('.cursor');
    if (i == null) { g.setAttribute('visibility', 'hidden'); c.geom.read.textContent = c.geom.avgText; continue; }
    const x = c.geom.X(chartDist[i]), v = c.values[i];
    g.setAttribute('visibility', 'visible');
    g.querySelector('line').setAttribute('x1', x); g.querySelector('line').setAttribute('x2', x);
    const dot = g.querySelector('circle');
    dot.setAttribute('cx', x);
    if (v != null) { dot.setAttribute('cy', c.geom.Y(Math.max(c.geom.lo, Math.min(c.geom.hi, v)))); dot.setAttribute('visibility', 'visible'); }
    else dot.setAttribute('visibility', 'hidden');
    c.geom.read.textContent = `${distValue(chartDist[i])} ${U().dist} · ${v == null ? '–' : c.show(v)}`;
  }
  if (fromMap) return;
  if (i == null) hoverDot.remove();
  else hoverDot.setLatLng(LL[i]).setStyle({ color: css('--surface'), fillColor: css('--ink'), fillOpacity: 1 }).addTo(map);
}
// touch: the crosshair stays after lifting a finger, until a tap elsewhere
document.addEventListener('pointerdown', e => {
  if (chartPinned && !e.target.closest('#charts')) { chartPinned = false; setChartCursor(null); }
});
window.addEventListener('resize', () => renderCharts());

// ---- side panel
function renderHeader() {
  document.title = `${D.title} · map`;
  document.getElementById('sport').textContent = D.sport;
  document.getElementById('title').textContent = D.title;
  document.getElementById('when').textContent = D.when;
}

function renderUnits() {
  const el = document.getElementById('units');
  el.innerHTML = '';
  for (const u of Object.keys(UNITS)) {
    const b = document.createElement('button');
    b.textContent = u;
    b.setAttribute('aria-pressed', u === units);
    b.onclick = () => { units = u; store.set('units', u); selectedLap = null; renderAll(); };
    el.appendChild(b);
  }
}

function renderTiles() {
  const S = D.summary, tiles = [];
  const add = (label, value, unit = '') => tiles.push(`<div class="tile"><div class="value">${value}${unit ? `<small>${unit}</small>` : ''}</div><div class="label">${label}</div></div>`);
  add('Distance', distValue(S.distance), U().dist);
  add('Moving time', fmtDuration(S.movingTime));
  const avgSpeed = S.avgSpeed || (S.distance / S.movingTime);
  add(D.usePace ? 'Avg pace' : 'Avg speed', rateValue(avgSpeed), rateUnit());
  if (S.ascent != null) add('Elevation gain', elevValue(S.ascent), U().elev);
  if (S.avgHr != null) add('Avg heart rate', S.avgHr, 'bpm');
  if (S.calories != null) add('Calories', num(S.calories), 'kcal');
  document.getElementById('tiles').innerHTML = tiles.join('');

  const extra = [`Elapsed ${fmtDuration(S.elapsedTime)}`];
  if (S.maxHr != null) extra.push(`Max HR ${S.maxHr} bpm`);
  if (S.avgCad != null) extra.push(`Cadence ${Math.round(S.avgCad)} ${D.cadUnit}`);
  if (S.avgPower != null) extra.push(`Power ${S.avgPower} W`);
  if (S.descent != null) extra.push(`Descent ${elevValue(S.descent)} ${U().elev}`);
  if (S.steps) extra.push(`${num(S.steps)} steps`);
  // the watch's own sensor, on the wrist: reads warmer than the air
  if (S.temperature != null) extra.push(`Temperature ${units === 'mi' ? `${Math.round(S.temperature * 9 / 5 + 32)} °F` : `${S.temperature} °C`}`);
  if (S.avgEffort) extra.push(`Effort pace ${rateValue(S.avgEffort)} ${rateUnit()}`);
  document.getElementById('extra').textContent = extra.join(' · ');
}

function renderMetricPicker() {
  const el = document.getElementById('metrics');
  el.innerHTML = '';
  for (const m of METRICS) {
    const b = document.createElement('button');
    b.textContent = m.label;
    b.setAttribute('aria-pressed', m === metric);
    b.onclick = () => { metric = m; store.set('metric', m.id); renderMetricPicker(); renderLegend(); renderRoute(); };
    el.appendChild(b);
  }
}

function renderLegend() {
  const colors = ramp();
  const [lo, hi] = [metric.fmt(metric.lo), metric.fmt(metric.hi)];
  const [loNote, hiNote] = metric.id === 'speed' ? [' · slower', ' · faster']
    : metric.id === 'hr' ? [' · easier', ' · harder'] : ['', ''];
  document.getElementById('legend').innerHTML = `
    <div class="bar">${colors.map(c => `<span style="background:${c}"></span>`).join('')}</div>
    <div class="ends"><span>${lo}${loNote}</span><span>${hi}${hiNote}</span></div>
    <div class="key"><span><i class="start"></i>Start</span><span><i class="finish"></i>Finish</span>
      <span>Numbered dots mark each ${U().dist}</span></div>`;
}

function renderExplorer() {
  const picker = document.getElementById('explorerLevels');
  picker.innerHTML = '';
  for (const z of [null, ...EXPLORER_LEVELS]) {
    const b = document.createElement('button');
    b.textContent = z == null ? 'Off' : `Level ${z}`;
    b.setAttribute('aria-pressed', z === explorerZoom);
    b.onclick = () => { explorerZoom = z; store.set('explorer', z ?? 'off'); renderExplorer(); explorerLayer.redraw();
                        if (selectedLap == null) fitAll(); };
    picker.appendChild(b);
  }

  const stats = document.getElementById('explorerStats');
  stats.hidden = explorerZoom == null;
  if (explorerZoom == null) return;
  const E = explorerTiles(explorerZoom), n = E.square.n;
  const meanLat = P.lat.reduce((a, b) => a + b, 0) / N;
  const side = 40075016.686 * Math.cos(meanLat * Math.PI / 180) / 2 ** explorerZoom;
  const sideText = side >= U().m / 2 ? `${num(side / U().m, 2)}<small>${U().dist}</small>`
                                     : `${num(side * U().elevF)}<small>${U().elev}</small>`;
  stats.innerHTML = `
    <div class="tiles">
      <div class="tile"><div class="value">${num(E.tiles.length)}</div><div class="label">Tiles visited</div></div>
      ${NEW_TILES ? `<div class="tile"><div class="value">${num(newTileKeys(explorerZoom).size)}</div><div class="label">New tiles</div></div>`
                  : `<div class="tile"><div class="value">${n}×${n}</div><div class="label">Max square</div></div>`}
      <div class="tile"><div class="value">${sideText}</div><div class="label">Tile size</div></div>
    </div>
    <div class="explorer-key"><span><i class="visited"></i>Visited tile</span>${NEW_TILES ? '<span><i class="new"></i>New tile (first time here)</span>' : ''}</div>
    <div class="note">${explorerZoom === 14
      ? 'Level 14 is the standard explorer tile (VeloViewer, StatsHunters, Squadrats).'
      : 'Level 17 splits each level-14 tile into 8×8 smaller ones.'}</div>`;
}

// Splits are worked out per mile or per kilometer to match the units switch; "Watch laps" are the
// laps the watch recorded (auto-laps or the lap button), shown when there are at least two.
let lapMode = store.get('lapMode') === 'laps' ? 'laps' : 'splits';
const watchLaps = () => D.laps.length >= 2;
const currentLaps = () => lapMode === 'laps' && watchLaps() ? D.laps : ((D.splits || {})[units] || []);

function renderLaps() {
  const section = document.getElementById('lapSection'), laps = currentLaps();
  const modes = document.getElementById('lapModes');
  modes.hidden = !watchLaps();
  modes.innerHTML = '';
  for (const [mode, text] of [['splits', 'Splits'], ['laps', 'Watch laps']]) {
    const b = document.createElement('button');
    b.textContent = text;
    b.setAttribute('aria-pressed', mode === lapMode);
    b.onclick = () => { lapMode = mode; store.set('lapMode', mode); selectedLap = null; renderLaps(); renderRoute(); renderCharts(); fitAll(); };
    modes.append(b);
  }
  const isSplits = laps !== D.laps;
  document.getElementById('lapTitle').textContent = isSplits ? 'Splits' : 'Watch laps';
  if (laps.length < 2 && !watchLaps()) { section.hidden = true; return; }
  section.hidden = false;
  const hasHr = laps.some(l => l.hr != null);
  const first = isSplits ? (units === 'mi' ? 'Mile' : 'Km') : 'Lap';
  const head = `<thead><tr><th>${first}</th><th>Dist (${U().dist})</th><th>Time</th><th>${D.usePace ? 'Pace' : U().speed}</th>${hasHr ? '<th>HR</th>' : ''}</tr></thead>`;
  const rows = laps.map((l, i) => {
    const speed = l.speed || (l.distance && l.time ? l.distance / l.time : 0);
    return `<tr data-i="${i}" aria-selected="${i === selectedLap}"><td>${l.n}</td><td>${l.distance != null ? distValue(l.distance) : '–'}</td>` +
      `<td>${l.time != null ? fmtDuration(l.time) : '–'}</td><td>${rateValue(speed)}</td>${hasHr ? `<td>${l.hr ?? '–'}</td>` : ''}</tr>`;
  }).join('');
  const table = document.getElementById('laps');
  table.innerHTML = head + `<tbody>${rows}</tbody>`;
  table.querySelectorAll('tbody tr').forEach(tr => tr.onclick = () => selectLap(+tr.dataset.i));
  document.getElementById('showAll').hidden = selectedLap == null;
}

function selectLap(i) {
  selectedLap = selectedLap === i ? null : i;
  renderLaps();
  renderRoute();
  renderCharts();
  if (selectedLap == null) fitAll();
  else {
    const [a, b] = currentLaps()[selectedLap].range;
    map.fitBounds(L.latLngBounds(LL.slice(a, b + 1)), { padding: [48, 48], maxZoom: 17 });
  }
}
document.getElementById('showAll').onclick = () => selectLap(selectedLap);

// The whole route, and with explorer tiles on, every visited tile in full (cut-off tiles are easy to misread)
function fitAll() {
  const bounds = casing.getBounds();
  if (explorerZoom != null) {
    const n = 2 ** explorerZoom, lat = y => Math.atan(Math.sinh(Math.PI * (1 - 2 * y / n))) * 180 / Math.PI;
    for (const [x, y] of explorerTiles(explorerZoom).tiles) {
      bounds.extend([lat(y + 1), x / n * 360 - 180]).extend([lat(y), (x + 1) / n * 360 - 180]);
    }
  }
  map.fitBounds(bounds, { padding: [24, 24] });
}

function renderAll() {
  renderUnits();
  renderTiles();
  renderMetricPicker();
  renderLegend();
  renderExplorer();
  renderLaps();
  renderDistanceMarkers();
  renderRoute();
  renderCharts();
}

darkQuery.addEventListener('change', () => {
  canvasBase.setUrl(canvasUrl('Base'));
  canvasLabels.setUrl(canvasUrl('Reference'));
  renderLegend();
  renderRoute();
  explorerLayer.redraw();
});

renderHeader();
renderAll();
fitAll();
</script>
</body>
</html>
"""


def render_page(activity):
    payload = json.dumps(activity, separators=(",", ":")).replace("</", "<\\/")
    return PAGE.replace("/*__DATA__*/null", payload)


# --------------------------------------------------------------------------- CLI

LIST_LIMIT = 25


def _normalize(text):
    # NFKC turns the non-breaking space in "Apple Watch" into a plain one; fold curly quotes too
    return unicodedata.normalize("NFKC", text).casefold().replace("’", "'").replace("‘", "'")


def find_runs(query, root):
    """All .fit files under root whose names contain every word of query, newest first."""
    words = _normalize(query).removesuffix(".fit").split()
    files = [p for p in root.rglob("*") if p.suffix.lower() == ".fit" and p.is_file()]
    # HealthFit names start with the date, so reverse name order is newest first
    files.sort(key=lambda p: p.name, reverse=True)
    return [p for p in files if all(w in _normalize(str(p.relative_to(root))) for w in words)]


def _describe(path):
    m = HEALTHFIT_NAME.match(path.stem)
    if not m:
        return path.name
    day, hour, minute, activity = m.group(1), int(m.group(2)), m.group(3), m.group(4)
    return f"{day}  {hour % 12 or 12:>2}:{minute} {'PM' if hour >= 12 else 'AM'}  {activity}"


def choose_file(query, root):
    """Resolve a path or search words to one .fit file, asking when that's ambiguous."""
    interactive = sys.stdin.isatty()
    while True:
        if query is None:
            if not interactive:
                query = ""
            else:
                query = input("Run to view (part of a file name, e.g. 2024-09-09; Enter for the newest): ").strip()

        path = Path(query).expanduser()
        if query and path.is_file():
            return path
        matches = find_runs(query, root)
        if len(matches) == 1 or (matches and not query):
            return matches[0]

        if not matches:
            message = f'No .fit file under {root} matches "{query}".'
        else:
            shown = matches[:LIST_LIMIT]
            lines = [f"{len(matches)} files match \"{query}\"" +
                     (f", showing the newest {LIST_LIMIT}:" if len(matches) > LIST_LIMIT else ":")]
            lines += [f"  {i:>2}  {_describe(p)}" for i, p in enumerate(shown, 1)]
            message = "\n".join(lines)
        if not interactive:
            sys.exit(message)
        print(message)

        answer = input("Number to open, or new search words: " if matches else "Search again: ").strip()
        if matches and answer.isdigit() and 1 <= int(answer) <= len(shown):
            return shown[int(answer) - 1]
        query = answer or None


def serve(page, port, open_browser):
    body = page.encode("utf-8")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] not in ("/", "/index.html"):
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Serving map at {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="View a .fit activity as a map in your browser.")
    parser.add_argument("run", nargs="*",
                        help="path to a .fit file, or words from its name such as a date (default: ask)")
    parser.add_argument("--dir", type=Path, default=Path(__file__).resolve().parent,
                        help="folder to search for .fit files, including subfolders (default: this script's folder)")
    parser.add_argument("--port", type=int, default=0, help="port to serve on (default: any free port)")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser automatically")
    parser.add_argument("--output", "-o", metavar="HTML", help="write the page to this file instead of serving it")
    args = parser.parse_args()

    query = " ".join(args.run) or None
    try:
        while True:
            path = choose_file(query, args.dir.expanduser())
            try:
                activity = build_activity(path)
                break
            except ValueError as e:  # not a FIT file, or nothing to map
                if not sys.stdin.isatty():
                    sys.exit(str(e))
                print(f"{e}\n")
                query = None
    except (KeyboardInterrupt, EOFError):
        print()
        return
    s = activity["summary"]
    print(f"{activity['title']} · {activity['when']}: {len(activity['points']['t'])} GPS points, "
          f"{s['distance'] / 1000:.2f} km, {len(activity['laps'])} lap{'' if len(activity['laps']) == 1 else 's'}")

    page = render_page(activity)
    if args.output:
        Path(args.output).write_text(page, encoding="utf-8")
        print(f"Wrote {args.output}")
        if not args.no_browser:
            webbrowser.open(Path(args.output).resolve().as_uri())
        return
    serve(page, args.port, not args.no_browser)


if __name__ == "__main__":
    main()
