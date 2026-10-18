"""Simplified GPS routes of every run, for the heatmap.

Each route is simplified (Douglas-Peucker, 3 m tolerance) from ~1,400 points per run to ~65,
so every run fits in one page. A jump over 500 m between points is a GPS gap: the route is
split there instead of drawing a straight line across it. Each file's simplified route is
cached in heat_cache.json, so only new files are read.
"""

import json
import math
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from fit_map import SEMICIRCLE_TO_DEG, _haversine, read_fit

HERE = Path(__file__).resolve().parent
CACHE_FILE = HERE / "heat_cache.json"
TOLERANCE_M = 3
MAX_GAP_M = 500
SCALE = 100000  # coordinates are stored as integers of 1e-5 degrees (about 1 m)


def simplify(points, tolerance=TOLERANCE_M):
    """Douglas-Peucker: drop points that lie within `tolerance` meters of the simplified line."""
    if len(points) < 3:
        return points
    kx = 111320 * math.cos(math.radians(points[0][0]))  # meters per degree, fine at run scale
    ky = 110540
    xy = [(lon * kx, lat * ky) for lat, lon in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = xy[a], xy[b]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        far, far_i = -1.0, -1
        for i in range(a + 1, b):
            px, py = xy[i]
            d = abs(dy * (px - ax) - dx * (py - ay)) / length if length else math.hypot(px - ax, py - ay)
            if d > far:
                far, far_i = d, i
        if far > tolerance:
            keep[far_i] = True
            stack += [(a, far_i), (far_i, b)]
    return [p for p, k in zip(points, keep) if k]


def file_route(path):
    """The run's route as simplified pieces, each a flat list: first lat, lon, then deltas (1e-5°)."""
    points = [(r[0] * SEMICIRCLE_TO_DEG, r[1] * SEMICIRCLE_TO_DEG)
              for num, r in read_fit(path)
              if num == 20 and r.get(0) is not None and r.get(1) is not None]
    pieces, current = [], []
    for p in points:
        if current and _haversine(*current[-1], *p) > MAX_GAP_M:
            pieces.append(current)
            current = []
        current.append(p)
    pieces.append(current)
    encoded = []
    for piece in pieces:
        if len(piece) < 2:
            continue
        flat, last = [], (0, 0)
        for lat, lon in simplify(piece):
            q = (round(lat * SCALE), round(lon * SCALE))
            flat += [q[0] - last[0], q[1] - last[1]]
            last = q
        encoded.append(flat)
    return encoded


def _safe_file_route(path):
    try:
        return file_route(path)
    except Exception as e:
        print(f"  skip {path.name}: {e}")
        return None


def heat_data(runs, here=HERE):
    """[{"d": date, "m": miles, "s": pieces}] for every run with GPS, oldest first. `runs` comes from load_runs()."""
    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    fresh, todo = {}, []
    for run in runs:
        path = here / run["file"]
        stamp = f"{path.stat().st_size}:{int(path.stat().st_mtime)}"
        entry = cache.get(run["file"])
        if entry and entry["stamp"] == stamp:
            fresh[run["file"]] = entry
        else:
            todo.append((run["file"], stamp, path))
    if todo:
        print(f"Simplifying routes from {len(todo)} new file(s) for the heatmap...")
        with ProcessPoolExecutor() as pool:
            for (key, stamp, _), route in zip(todo, pool.map(_safe_file_route, [t[2] for t in todo], chunksize=16)):
                if route is not None:
                    fresh[key] = {"stamp": stamp, "route": route}
        CACHE_FILE.write_text(json.dumps(fresh, separators=(",", ":")))
    return [{"d": r["date"], "m": r["miles"], "s": fresh[r["file"]]["route"]}
            for r in runs if r["file"] in fresh and fresh[r["file"]]["route"]]
