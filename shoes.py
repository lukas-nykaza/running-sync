"""Shoe tracking: your shoes, which shoe each run used, and each shoe's mileage.

Stored in shoes.json:
    {"shoes": [{"id": "s1", "name": "Pegasus 41", "adjust": 12.5}, ...],
     "runs": {"COROS/2026-10-14-...fit": "s1", ...}}

A shoe's mileage is its runs' miles plus `adjust`. Setting the mileage by hand (or giving
starting miles when adding a shoe) just changes `adjust`, so assigning more runs keeps adding.
"""

import json
import math
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHOES_FILE = HERE / "shoes.json"
_lock = threading.Lock()  # the dashboard serves requests on several threads


def _load():
    if SHOES_FILE.exists():
        data = json.loads(SHOES_FILE.read_text(encoding="utf-8"))
        data.setdefault("shoes", [])
        data.setdefault("runs", {})
        return data
    return {"shoes": [], "runs": {}}


def _save(data):
    tmp = SHOES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(SHOES_FILE)  # all at once, so a crash can't leave half a file


def summary(runs):
    """Every shoe with its mileage and runs, plus which shoe each run used.

    `runs` is load_runs() from dashboard.py (file, date, miles, ...)."""
    with _lock:
        data = _load()
    by_file = {r["file"]: r for r in runs}
    shoes = []
    for shoe in data["shoes"]:
        used = sorted((by_file[f] for f, sid in data["runs"].items() if sid == shoe["id"] and f in by_file),
                      key=lambda r: r["date"] + r["time"])
        run_miles = sum(r["miles"] for r in used)
        shoes.append({"id": shoe["id"], "name": shoe["name"],
                      "miles": round(run_miles + shoe.get("adjust", 0), 1),
                      "runMiles": round(run_miles, 1), "runs": len(used),
                      "first": used[0]["date"] if used else None, "last": used[-1]["date"] if used else None})
    return {"shoes": shoes, "runs": {f: sid for f, sid in data["runs"].items() if f in by_file}}


def change(request, runs):
    """Apply one change from the shoes page or a run page. Returns an error message, or None."""
    action = request.get("action")
    with _lock:
        data = _load()
        shoes = {s["id"]: s for s in data["shoes"]}
        run_miles = lambda sid: sum(r["miles"] for r in runs if data["runs"].get(r["file"]) == sid)
        try:
            if action == "add":
                name = str(request.get("name", "")).strip()[:80]
                if not name:
                    return "Give the shoe a name."
                new_id = f"s{max([int(s['id'][1:]) for s in data['shoes']] + [0]) + 1}"
                start = float(request.get("miles") or 0)
                if not math.isfinite(start) or start < 0:
                    return "Mileage must be a number of miles."
                data["shoes"].append({"id": new_id, "name": name, "adjust": round(start, 2)})
            elif action == "rename":
                name = str(request.get("name", "")).strip()[:80]
                if request.get("id") not in shoes or not name:
                    return "Unknown shoe or empty name."
                shoes[request["id"]]["name"] = name
            elif action == "set_miles":
                if request.get("id") not in shoes:
                    return "Unknown shoe."
                total = float(request.get("miles"))
                if not math.isfinite(total) or total < 0:
                    return "Mileage can't be negative."
                shoes[request["id"]]["adjust"] = round(total - run_miles(request["id"]), 2)
            elif action == "remove":
                if request.get("id") not in shoes:
                    return "Unknown shoe."
                data["shoes"] = [s for s in data["shoes"] if s["id"] != request["id"]]
                data["runs"] = {f: sid for f, sid in data["runs"].items() if sid != request["id"]}
            elif action == "assign":
                file, sid = request.get("file"), request.get("shoe")
                if file not in {r["file"] for r in runs}:
                    return "Unknown run."
                if sid:
                    if sid not in shoes:
                        return "Unknown shoe."
                    data["runs"][file] = sid
                else:
                    data["runs"].pop(file, None)
            else:
                return "Unknown action."
        except (TypeError, ValueError):
            return "Mileage must be a number."
        _save(data)
    return None
