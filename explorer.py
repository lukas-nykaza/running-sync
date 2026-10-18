"""Explorer tiles across every run: the OpenStreetMap tile grid at zoom 14 and 17, as used
by VeloViewer, StatsHunters and Squadrats (and by fit_map.py for a single run).

A tile counts once a GPS track enters it. Like fit_map.py, every tile a segment between two
points crosses is counted, and a jump over 500 m is treated as a GPS gap, not ground covered.
Each file's tiles are cached in tiles_cache.json, so only new files are read.
"""

import bisect
import json
import math
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from fit_map import SEMICIRCLE_TO_DEG, _haversine, read_fit

HERE = Path(__file__).resolve().parent
CACHE_FILE = HERE / "tiles_cache.json"
ZOOMS = (14, 17)
MAX_GAP_M = 500


def _to_tile(lat, lon, z):
    n = 2 ** z
    return (lon + 180) / 360 * n, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n


def _walk(points, z):
    """Tile keys (x * 2**z + y) visited by a track of (lat, lon) points."""
    n, seen, prev, prev_ll = 2 ** z, set(), None, None
    for lat, lon in points:
        p = _to_tile(lat, lon, z)
        cx, cy = math.floor(p[0]), math.floor(p[1])
        if prev and _haversine(*prev_ll, lat, lon) <= MAX_GAP_M:
            x, y = math.floor(prev[0]), math.floor(prev[1])
            dx, dy = p[0] - prev[0], p[1] - prev[1]
            sx, sy = (dx > 0) - (dx < 0), (dy > 0) - (dy < 0)
            tx = ((x + 1 if sx > 0 else x) - prev[0]) / dx if dx else math.inf
            ty = ((y + 1 if sy > 0 else y) - prev[1]) / dy if dy else math.inf
            for _ in range(abs(cx - x) + abs(cy - y)):
                if tx < ty:
                    x += sx
                    tx += sx / dx
                else:
                    y += sy
                    ty += sy / dy
                seen.add(x * n + y)
        seen.add(cx * n + cy)
        prev, prev_ll = p, (lat, lon)
    return seen


def file_tiles(path):
    """{zoom: sorted tile keys} for one .fit file; empty for runs without GPS."""
    points = [(r[0] * SEMICIRCLE_TO_DEG, r[1] * SEMICIRCLE_TO_DEG)
              for num, r in read_fit(path)
              if num == 20 and r.get(0) is not None and r.get(1) is not None]
    return {str(z): sorted(_walk(points, z)) for z in ZOOMS}


def _safe_file_tiles(path):
    try:
        return file_tiles(path)
    except Exception as e:
        print(f"  skip {path.name}: {e}")
        return None


def max_square(tiles, z):
    """Largest n×n block of visited tiles: (n, x, y) of its top-left tile."""
    n_side, sizes, best = 2 ** z, {}, (0, 0, 0)
    for key in sorted(tiles, key=lambda k: (k % n_side, k // n_side)):  # row by row, left to right
        x, y = divmod(key, n_side)
        n = 1 + min(sizes.get((x - 1, y), 0), sizes.get((x, y - 1), 0), sizes.get((x - 1, y - 1), 0))
        sizes[(x, y)] = n
        if n > best[0]:
            best = (n, x - n + 1, y - n + 1)
    return best


def _refresh(paths):
    """{"folder/name.fit": {"stamp", "tiles"}} for the given files, reading only new or changed ones."""
    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    fresh, todo = {}, []
    for path in paths:
        key = f"{path.parent.name}/{path.name}"
        stamp = f"{path.stat().st_size}:{int(path.stat().st_mtime)}"
        if key in cache and cache[key]["stamp"] == stamp:
            fresh[key] = cache[key]
        else:
            todo.append((key, stamp, path))
    if todo:
        print(f"Reading explorer tiles from {len(todo)} new file(s)...")
        with ProcessPoolExecutor() as pool:
            for (key, stamp, _), tiles in zip(todo, pool.map(_safe_file_tiles, [t[2] for t in todo], chunksize=16)):
                if tiles is not None:
                    fresh[key] = {"stamp": stamp, "tiles": tiles}
        CACHE_FILE.write_text(json.dumps(fresh, separators=(",", ":")))
    return fresh


def explorer_data(paths):
    """Visited tiles and the biggest square at each zoom, across all of the given .fit files."""
    fresh = _refresh(paths)
    result = {"runs": sum(1 for e in fresh.values() if e["tiles"][str(ZOOMS[0])])}
    for z in ZOOMS:
        visited = set()
        for entry in fresh.values():
            visited.update(entry["tiles"][str(z)])
        n, x, y = max_square(visited, z)
        result[str(z)] = {"tiles": sorted(visited), "square": {"n": n, "x": x, "y": y}}
    return result


def files_in_tile(z, key):
    """The run files ("folder/name.fit") whose tracks pass through one tile, from the cache."""
    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    found = set()
    for name, entry in cache.items():
        tiles = entry["tiles"].get(str(z), [])
        i = bisect.bisect_left(tiles, key)  # each file's tiles are stored sorted
        if i < len(tiles) and tiles[i] == key:
            found.add(name)
    return found


def new_tiles(rel, runs, here=HERE):
    """{zoom: [tile keys]} that run `rel` visited first: no run that started before it went there.

    `runs` is load_runs() from dashboard.py, oldest first (same run in two folders counted once)."""
    fresh = _refresh([here / r["file"] for r in runs])
    target = fresh.get(rel)
    if not target:
        return None
    order = [r["file"] for r in runs]
    earlier = order[:order.index(rel)] if rel in order else []
    result = {}
    for z in ZOOMS:
        seen = set()
        for f in earlier:
            if f in fresh:
                seen.update(fresh[f]["tiles"][str(z)])
        result[str(z)] = [k for k in target["tiles"][str(z)] if k not in seen]
    return result
