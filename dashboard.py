#!/usr/bin/env python3
"""Running dashboard for the .fit runs in COROS/, HealthFit/ and garmin/, served locally.

Usage:
    python3 dashboard.py               # start the dashboard and open it in your browser
    python3 dashboard.py --port 9000   # use another port (default 8765)
    python3 dashboard.py --no-browser  # just start the server
    python3 dashboard.py --tailscale   # also let your Tailscale devices (your iPhone) open it

Only this Mac and, with --tailscale, devices signed in to your Tailscale account can open it;
anyone else on the same Wi-Fi is refused. install_dashboard.sh runs it this way in the background.

Shows weekly, monthly and yearly mileage plus a list of runs; click a run to open
its map (built by fit_map.py); /explorer maps every tile you've run through, and tapping
a tile lists the runs that passed through it; /heatmap draws every route on one map.
Reload the page to pick up newly synced runs.
Stop the server with Ctrl+C.

Each file's distance is cached in runs_cache.json, so only new files are read.
Weeks run Monday to Sunday. Standard library only.
"""

import argparse
import gzip
import html
import http.server
import ipaddress
import json
import re
import socket
import struct
import subprocess
import urllib.parse
import webbrowser
import zlib
from datetime import datetime, timezone
from pathlib import Path

from explorer import ZOOMS, explorer_data, files_in_tile, new_tiles
from fit_map import build_activity, read_fit, render_page
from heatmap import heat_data
import shoes

HERE = Path(__file__).resolve().parent
RUN_DIRS = [HERE / "COROS", HERE / "HealthFit", HERE / "garmin"]
CACHE_FILE = HERE / "runs_cache.json"
TEMPLATE = HERE / "dashboard_template.html"
EXPLORER_TEMPLATE = HERE / "explorer_template.html"
HEATMAP_TEMPLATE = HERE / "heatmap_template.html"
SHOES_TEMPLATE = HERE / "shoes_template.html"
ICON_FILE = HERE / "icon.png"  # optional: your own Home Screen icon (square PNG, 180×180 or larger)
METERS_PER_MILE = 1609.344
TAILNET = ipaddress.ip_network("100.64.0.0/10")  # the addresses Tailscale gives devices
NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(\d{2})(\d{2})(\d{2})-(.+?)-[^-]+\.fit$")


FIT_EPOCH = 631065600  # FIT timestamps count seconds from 1989-12-31 UTC
SUMMARY_VERSION = 2  # bump when run_summary changes, so cached summaries are re-read


def run_summary(path):
    """Distance (meters), moving time (seconds), sport and local start time of one .fit file."""
    meters = seconds = 0.0
    sport = start = sub_sport = offset = first_record = None
    for num, fields in read_fit(path):
        if num == 18:  # session
            meters += (fields.get(9) or 0) / 100
            seconds += (fields.get(8) or fields.get(7) or 0) / 1000  # timer time; elapsed if missing
            sport = fields.get(5) if sport is None else sport
            sub_sport = fields.get(6) if sub_sport is None else sub_sport
            start = fields.get(2) if start is None else start
        elif num == 34 and fields.get(5) is not None and fields.get(253) is not None:  # activity: local offset
            offset = fields[5] - fields[253]
        elif num == 20 and first_record is None:
            first_record = fields.get(253)
    start = start or first_record
    local = None
    if start is not None:
        local = datetime.fromtimestamp(FIT_EPOCH + start + (offset or 0), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return {"meters": meters, "seconds": seconds, "sport": sport, "subSport": sub_sport, "start": local}


def load_runs():
    """All runs, oldest first, as dicts for the page. The same run in two folders counts once.

    Files named like the sync's (2026-10-18-074144-Outdoor Running-COROS.fit) take their date and
    name from the file name; any other .fit file (e.g. a Garmin export, 12345_ACTIVITY.fit) is
    read for its start time. Files that aren't runs (rides, walks, ...) are skipped."""
    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    runs, fresh = {}, {}
    for folder in RUN_DIRS:
        for path in sorted(folder.glob("*.fit")):
            m = NAME_RE.match(path.name)
            key = f"{folder.name}/{path.name}"
            stamp = f"{path.stat().st_size}:{int(path.stat().st_mtime)}:{SUMMARY_VERSION}"
            entry = cache.get(key)
            if not entry or entry["stamp"] != stamp or (not m and "start" not in entry):
                try:
                    entry = {"stamp": stamp, **run_summary(path)}
                except Exception as e:
                    print(f"  skip {key}: {e}")
                    continue
            fresh[key] = entry
            if entry.get("sport") not in (None, 1):  # 1 = running
                continue
            if m:
                day, hh, mm, ss, kind = m.groups()
            elif entry.get("start"):
                day, clock = entry["start"].split()
                hh, mm, ss = clock.split(":")
                kind = "Indoor Running" if entry.get("subSport") == 1 else "Outdoor Running"
            else:
                continue
            runs.setdefault(day + hh + mm + ss, {
                "file": key, "date": day, "time": f"{hh}:{mm}", "name": kind,
                "miles": round(entry["meters"] / METERS_PER_MILE, 3), "seconds": round(entry["seconds"]),
            })
    CACHE_FILE.write_text(json.dumps(fresh))
    return [runs[k] for k in sorted(runs)]


def tailscale_ip():
    """This Mac's Tailscale address, or None when Tailscale isn't connected."""
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for ip in re.findall(r"inet (\d+\.\d+\.\d+\.\d+) -->", out):  # Tailscale's tunnel is point-to-point
        if ipaddress.ip_address(ip) in TAILNET:
            return ip
    return None


def tailscale_name(ip):
    """This Mac's Tailscale name (e.g. my-mac.tail1234.ts.net), looked up from its Tailscale address."""
    try:
        name = socket.gethostbyaddr(ip)[0]
    except (OSError, TypeError):
        return None
    return name if name.endswith(".ts.net") else None


def message_page(title, heading, body):
    """A plain page in the dashboard's style, for when there's nothing to show yet."""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: light dark; --page: #f9f9f7; --ink: #0b0b0b; --ink-2: #52514e; --card: #fcfcfb;
           --border: rgba(11, 11, 11, 0.10); --link: #256abf; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --page: #0d0d0d; --ink: #f0efec; --ink-2: #c3c2b7; --card: #1a1a19;
           --border: rgba(255, 255, 255, 0.10); --link: #6da7ec; }} }}
  body {{ margin: 0; background: var(--page); color: var(--ink); font: 16px/1.5 system-ui, -apple-system, sans-serif; }}
  main {{ max-width: 640px; margin: 0 auto; padding: 32px 16px; }}
  h1 {{ font-size: 28px; margin: 0 0 8px; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 4px 20px; margin-top: 16px; }}
  p, li {{ color: var(--ink-2); }} li {{ margin: 8px 0; }} code {{ font-size: 14px; }} a {{ color: var(--link); }}
</style></head><body><main><p><a href="/">← Dashboard</a></p><h1>{html.escape(heading)}</h1>{body}</main></body></html>"""


def welcome_page():
    """Shown until the first run arrives: what to do next, given how far setup has got."""
    key = env_value("INTERVALS_API_KEY")
    key_ok = bool(key) and key != "your_key_here"
    steps = [
        ("Add your Intervals.icu API key to <code>.env</code> (see README)." if not key_ok
         else "✓ Your Intervals.icu API key is set."),
        "Download your runs: <code>python3 sync_fit.py --since 2024-01-01</code> (any start date you like). "
        "After that, <code>bash install_schedule.sh</code> keeps them coming every hour.",
        "Or drop <code>.fit</code> files you already have into the <code>COROS/</code>, <code>HealthFit/</code> or "
        "<code>garmin/</code> folder. Any file name works; files that aren't runs are skipped.",
        "Reload this page.",
    ]
    items = "".join(f"<li>{s}</li>" for s in steps)
    return message_page("Running Dashboard", "No runs yet",
                        f"<p>Once some runs are here, this page shows your mileage, maps and more.</p>"
                        f'<div class="card"><ol>{items}</ol></div>')


def env_value(name):
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == name:
                return value.strip().strip('"').strip("'")
    return None


def dashboard_page(port):
    ip = tailscale_ip() if Handler.tailscale else None
    host = tailscale_name(ip) or ip
    phone = f"http://{host}:{port}/" if host else None
    runs = load_runs()
    if not runs:
        return welcome_page()
    data = json.dumps({"runs": runs, "phone": phone, "shoes": shoes.summary(runs)}).replace("</", "<\\/")
    return TEMPLATE.read_text(encoding="utf-8").replace("/*DATA*/null", data)


NO_GPS = ("<p>The explorer map and heatmap need runs with GPS. Treadmill runs and files without a route "
          "don't have any, so there's nothing to draw yet.</p>")


def explorer_page():
    runs = load_runs()
    if not runs:
        return welcome_page()
    result = explorer_data([HERE / r["file"] for r in runs])
    if not result[str(ZOOMS[0])]["tiles"]:
        return message_page("Explorer Tiles", "No GPS runs yet", NO_GPS)
    data = json.dumps(result, separators=(",", ":"))
    return EXPLORER_TEMPLATE.read_text(encoding="utf-8").replace("/*DATA*/null", data)


def shoes_page():
    data = json.dumps(shoes.summary(load_runs())).replace("</", "<\\/")
    return SHOES_TEMPLATE.read_text(encoding="utf-8").replace("/*DATA*/null", data)


def shoe_picker(rel):
    """A "Shoe" menu for a run's page; changing it saves straight away."""
    state = shoes.summary(load_runs())
    current = state["runs"].get(rel, "")
    options = '<option value="">No shoe</option>' + "".join(
        f'<option value="{html.escape(s["id"])}"{" selected" if s["id"] == current else ""}>'
        f'{html.escape(s["name"])} ({s["miles"]:,.0f} mi)</option>' for s in state["shoes"])
    if not state["shoes"]:
        body = '<span class="shoe-note">No shoes yet. <a href="/shoes">Add a shoe</a></span>'
    else:
        body = (f'<label for="shoeSelect">Shoe</label> <select id="shoeSelect">{options}</select>'
                f' <span class="shoe-note" id="shoeSaved" role="status"></span>'
                f' <a class="shoe-note" href="/shoes">Manage shoes</a>')
    return f"""<div class="shoe-picker">{body}</div>
<style>
  .shoe-picker {{ display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; margin: 14px 0 0;
                  font-size: 14px; color: var(--ink-2); }}
  .shoe-picker select {{ font: inherit; font-size: 14px; padding: 5px 8px; border-radius: 8px;
                         border: 1px solid var(--border); background: var(--surface); color: var(--ink); max-width: 100%; }}
  .shoe-note {{ color: var(--muted); font-size: 13px; }}
  .shoe-picker a {{ color: var(--accent); text-decoration: none; }}
</style>
<script>
  document.getElementById("shoeSelect")?.addEventListener("change", async e => {{
    const note = document.getElementById("shoeSaved");
    note.textContent = "Saving…";
    try {{
      const res = await fetch("/api/shoes", {{ method: "POST", headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ action: "assign", file: {json.dumps(rel)}, shoe: e.target.value || null }}) }});
      const out = await res.json();
      note.textContent = res.ok ? "Saved" : (out.error || "Couldn't save");
    }} catch (err) {{ note.textContent = "Couldn't save. Is the dashboard running?"; }}
  }});
</script>"""


def heatmap_page():
    runs = load_runs()
    if not runs:
        return welcome_page()
    routes = heat_data(runs)
    if not routes:
        return message_page("Running Heatmap", "No GPS runs yet", NO_GPS)
    data = json.dumps({"runs": routes}, separators=(",", ":"))
    return HEATMAP_TEMPLATE.read_text(encoding="utf-8").replace("/*DATA*/null", data)


def tile_runs(query):
    """JSON list of the runs through one explorer tile, newest first; None for a bad request."""
    try:
        z, key = int(query.get("z", [""])[0]), int(query.get("k", [""])[0])
    except ValueError:
        return None
    if z not in ZOOMS or not 0 <= key < 4 ** z:
        return None
    files = files_in_tile(z, key)
    runs = [r for r in load_runs() if r["file"] in files]
    return json.dumps({"z": z, "key": key, "runs": runs[::-1]})


# A "← Dashboard" link added to fit_map.py's run page ("← Explorer" when opened from the explorer
# map). Going back in history returns to the same spot in the list it came from; opened directly
# (no history), it goes to the dashboard's front page.
BACK_LINK = """<a class="to-dashboard" href="/" onclick="if (history.length > 1 &&
  document.referrer.startsWith(location.origin)) { history.back(); return false; }">← Dashboard</a>
<script>  // came from the explorer map? say so, since going back returns there
  if (document.referrer.startsWith(location.origin + "/explorer"))
    document.querySelector(".to-dashboard").textContent = "← Explorer";
</script>"""
BACK_STYLE = """<style>
  .to-dashboard { display: inline-block; margin: 0 0 12px; color: var(--accent); text-decoration: none;
                  font-size: 14px; font-weight: 500; }
  .to-dashboard:hover { text-decoration: underline; }
  @media (max-width: 760px) {  /* phones: the map comes first, so the link sits over its top (and scrolls away) */
    .to-dashboard { position: absolute; z-index: 1000; top: calc(10px + env(safe-area-inset-top)); left: 50%;
                    transform: translateX(-50%); margin: 0; padding: 7px 14px; border-radius: 999px;
                    background: var(--surface); border: 1px solid var(--border);
                    box-shadow: 0 2px 10px rgba(0, 0, 0, 0.25); }
  }
</style>
"""


def map_page(rel):
    """fit_map.py's page for one run, or None if rel isn't one of the run files."""
    path = (HERE / rel).resolve()
    if path.suffix.lower() != ".fit" or path.parent not in RUN_DIRS or not path.is_file():
        return None
    try:
        page = render_page(build_activity(path))
        # which of this run's explorer tiles no earlier run had reached (drawn purple on its map)
        history = json.dumps({"newTiles": new_tiles(rel, load_runs())}).replace("</", "<\\/")
        page = page.replace("</head>", f"<script>window.RUN_HISTORY = {history};</script>\n</head>", 1)
        page = page.replace("</head>", BACK_STYLE + "</head>", 1).replace("<aside>", "<aside>\n  " + BACK_LINK, 1)
        return page.replace("</header>", "</header>\n" + shoe_picker(rel), 1)
    except ValueError as e:  # e.g. a treadmill run with no GPS track
        return (f"<!doctype html><head><meta charset='utf-8'><title>No map</title></head>"
                f"<body style='font:16px system-ui;padding:32px'><p>{html.escape(str(e))}</p>"
                f"<p>{BACK_LINK}</p>{shoe_picker(rel)}")


# Home Screen web app: icon and name, added to every page
APP_HEAD = """<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="icon" type="image/png" href="/apple-touch-icon.png">
<meta name="apple-mobile-web-app-title" content="Running">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
"""


def png(width, height, pixel):
    """Encode an RGB image as PNG; pixel(x, y) returns an (r, g, b) tuple."""
    raw = b"".join(b"\0" + bytes(c for x in range(width) for c in pixel(x, y)) for y in range(height))
    chunk = lambda kind, data: (struct.pack(">I", len(data)) + kind + data
                                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def default_icon(size=180):
    """Explorer-style icon: a 4×4 grid of visited tiles (blue) around a 2×2 biggest square (orange)."""
    rgb = lambda h: tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))
    background, empty, blue, orange = rgb("#1a1a19"), rgb("#2c2c2a"), rgb("#3987e5"), rgb("#eb6834")
    pattern = ["BB.B", "BOOB", ".OOB", "BBB."]
    margin, gap = size * 22 // 180, size * 6 // 180
    cell = (size - 2 * margin - 3 * gap) / 4

    def pixel(x, y):
        cx, cy = (x - margin) / (cell + gap), (y - margin) / (cell + gap)
        col, row = int(cx), int(cy)
        if not (0 <= cx < 4 and 0 <= cy < 4) or (cx - col) * (cell + gap) >= cell or (cy - row) * (cell + gap) >= cell:
            # the gaps inside the orange square are filled, so it reads as one block
            inside = margin + cell + gap <= x < margin + 3 * cell + 2 * gap and margin + cell + gap <= y < margin + 3 * cell + 2 * gap
            return orange if inside else background
        return {"B": blue, "O": orange, ".": empty}[pattern[row][col]]
    return png(size, size, pixel)


def icon_bytes():
    return ICON_FILE.read_bytes() if ICON_FILE.exists() else default_icon()


class Handler(http.server.BaseHTTPRequestHandler):
    tailscale = False  # set by main() with --tailscale

    def authorized(self):
        """This Mac always; otherwise only your Tailscale devices, reaching this Mac's Tailscale address."""
        client = self.client_address[0]
        if client in ("127.0.0.1", "::1"):
            return True
        if not self.tailscale:
            return False
        local = self.connection.getsockname()[0]
        return ipaddress.ip_address(client) in TAILNET and local == tailscale_ip()

    def do_GET(self):
        if not self.authorized():
            self.send_error(403, "Only this Mac and your Tailscale devices can open the dashboard")
            return
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/":
            page = dashboard_page(self.server.server_address[1])
        elif url.path == "/explorer":
            page = explorer_page()
        elif url.path == "/heatmap":
            page = heatmap_page()
        elif url.path == "/shoes":
            page = shoes_page()
        elif url.path == "/api/shoes":
            page = json.dumps(shoes.summary(load_runs()))
        elif url.path == "/run":
            page = map_page(urllib.parse.parse_qs(url.query).get("f", [""])[0])
        elif url.path == "/tile":
            page = tile_runs(urllib.parse.parse_qs(url.query))
        elif url.path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
            page = icon_bytes()
        else:
            page = None
        if page is None:
            self.send_error(404)
            return
        if isinstance(page, bytes):
            body, kind = page, "image/png"
        elif url.path in ("/tile", "/api/shoes"):
            body, kind = page.encode("utf-8"), "application/json"
        else:
            body, kind = page.replace("</head>", APP_HEAD + "</head>", 1).encode("utf-8"), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        if kind != "image/png":  # pages and data shrink a lot compressed (the heatmap from ~1.8 MB to ~0.7 MB)
            self.send_header("Vary", "Accept-Encoding")
            if "gzip" in self.headers.get("Accept-Encoding", ""):
                body = gzip.compress(body, 6)
                self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        """Shoe changes. Only from this dashboard's own pages: other websites open in the same
        browser can't send JSON here (the browser would need permission, which this never grants)."""
        def reply(status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        origin = self.headers.get("Origin")
        if (not self.authorized() or urllib.parse.urlsplit(self.path).path != "/api/shoes"
                or self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json"
                or (origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"))):
            reply(403, {"error": "Not allowed"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= 10_000:
            reply(400, {"error": "Bad request"})
            return
        try:
            request = json.loads(self.rfile.read(length))
        except ValueError:
            reply(400, {"error": "Bad request"})
            return
        runs = load_runs()
        error = shoes.change(request, runs) if isinstance(request, dict) else "Bad request"
        if error:
            reply(400, {"error": error})
        else:
            reply(200, shoes.summary(runs))

    def log_message(self, *args):
        pass


def main():
    p = argparse.ArgumentParser(description="Serve the running dashboard locally.")
    p.add_argument("--port", type=int, default=8765, help="port to serve on (default: 8765)")
    p.add_argument("--no-browser", action="store_true", help="don't open a browser automatically")
    p.add_argument("--tailscale", action="store_true",
                   help="also serve to your Tailscale devices (everyone else is refused)")
    args = p.parse_args()

    url = f"http://127.0.0.1:{args.port}/"
    if not args.tailscale:  # already serving (e.g. the background dashboard)? Just open that one.
        try:
            socket.create_connection(("127.0.0.1", args.port), timeout=1).close()
            print(f"The dashboard is already running at {url}")
            if not args.no_browser:
                webbrowser.open(url)
            return
        except OSError:
            pass
    Handler.tailscale = args.tailscale
    try:
        server = http.server.ThreadingHTTPServer(("0.0.0.0" if args.tailscale else "127.0.0.1", args.port), Handler)
    except OSError:
        # most likely the background dashboard (install_dashboard.sh) is already serving this port
        print(f"Port {args.port} is already in use; the dashboard is probably already running at {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return

    print(f"{len(load_runs())} runs loaded")  # fills the caches before the first page load
    explorer_page()
    heatmap_page()
    print(f"Dashboard at {url}  (Ctrl+C to stop)")
    if args.tailscale:
        ip = tailscale_ip()
        host = tailscale_name(ip) or ip
        print(f"On your phone (Tailscale): http://{host}:{args.port}/" if host
              else "Tailscale isn't connected yet; your phone can open the dashboard once it is.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
