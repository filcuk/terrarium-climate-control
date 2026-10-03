"""Serve the controller page on this computer so it can be checked without flashing.

    python preview.py

Then open http://127.0.0.1:8080/
"""
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = 8080
# MicroPython on this board counts seconds from 2000-01-01.
EPOCH = 946684800
DAY = 86400


def page_html():
    return (ROOT / "index.html").read_text(encoding="utf-8")


def _read_hist(name):
    path = ROOT / "device" / name
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 2:
            rows.append(parts)
    return rows


def climate_points(now):
    """Browser timestamps. span 0 is a raw reading; larger spans are averages."""
    raw = []
    for parts in _read_hist("climate.hist"):
        if len(parts) < 3:
            continue
        try:
            ts = int(parts[0]) + EPOCH
            raw.append((ts, float(parts[1]), float(parts[2])))
        except ValueError:
            continue
    if not raw:
        t = now - 30 * DAY
        while t < now:
            age = now - t
            if age > 7 * DAY:
                step, span = 12 * 3600, 12 * 3600
            elif age > 3 * DAY:
                step, span = 4 * 3600, 4 * 3600
            elif age > DAY:
                step, span = 3600, 3600
            else:
                step, span = 15 * 60, 0
            temp = 23 + (t % 86400) / 86400 * 2
            raw.append((t, round(temp, 1), round(55 - (t % 86400) / 86400 * 8, 1), span))
            t += step
        return raw
    first = raw[0][0]
    older = []
    t = first - 30 * DAY
    while t < first - DAY:
        age = first - t
        if age > 7 * DAY:
            step, span = 12 * 3600, 12 * 3600
        elif age > 3 * DAY:
            step, span = 4 * 3600, 4 * 3600
        else:
            step, span = 3600, 3600
        older.append((t, 22.5, 60.0, span))
        t += step
    points = older + [(ts, temp, hum, 0) for ts, temp, hum in raw]
    return [p for p in points if now - p[0] <= 30 * DAY]


def fan_runs(now):
    runs = []
    for parts in _read_hist("fan.hist"):
        try:
            start = int(parts[0]) + EPOCH
            end = int(parts[1]) + EPOCH
            manual = int(parts[2]) if len(parts) > 2 else (1 if end - start < 600 else 0)
        except ValueError:
            continue
        if now - end <= 7 * DAY:
            runs.append([start, end, manual])
    if not runs:
        runs.append([now - 3 * 3600, now - 3 * 3600 + 6 * 3600, 0])
        runs.append([now - 26 * 3600, now - 26 * 3600 + 8 * 60, 1])
    return runs


POINTS = None
FANS = None
MAINT = None
MAINT_REV = 0
BOOT = int(time.time())
CATEGORIES = ["Mist", "Feed", "Soil", "Deco"]
SENSOR_AT = None
PREVIEW_TZ = None


def ensure_data():
    global POINTS, FANS, MAINT
    now = int(time.time())
    if POINTS is None:
        POINTS = climate_points(now)
        FANS = fan_runs(now)
        MAINT = [
            [now - 2 * 3600, "Mist", ""],
            [now - 26 * 3600, "Feed", ""],
            [now - 3 * DAY, "note", "Rinsed the water dish"],
            [now - 6 * DAY, "Soil", ""],
            [now - 8 * DAY, "Mist", ""],
            [now - 12 * DAY, "Deco", ""],
            [now - 20 * DAY, "Feed", ""],
        ]


def averages():
    wt = wh = w = 0.0
    for ts, temp, hum, span in POINTS:
        dur = span or 15 * 60
        wt += temp * dur
        wh += hum * dur
        w += dur
    if w <= 0:
        return None, None
    return wt / w, wh / w


def parse_categories(text):
    names = []
    seen = []
    for part in str(text).split(","):
        name = " ".join(part.split())
        if not name or name.lower() == "note":
            continue
        if len(name) > 24:
            name = name[:24].rstrip()
        if not name or name.lower() in seen:
            continue
        seen.append(name.lower())
        names.append(name)
        if len(names) >= 8:
            break
    return names


def download_filename(name):
    stamp = time.strftime("%Y-%m-%d-%H%M%S")
    dot = name.rfind(".")
    if dot < 0:
        return "%s-%s" % (name, stamp)
    return "%s-%s%s" % (name[:dot], stamp, name[dot:])


def download_body(which):
    log = log_text() + "\n"
    climate = "".join("%d,%.1f,%.1f\n" % (p[0], p[1], p[2]) for p in POINTS)
    fan = "".join("%d,%d,%d\n" % (r[0], r[1], r[2]) for r in FANS)
    maint = "".join("%d,%s%s\n" % (row[0], row[1], ("," + row[2]) if row[2] else "") for row in MAINT)
    if which == "log":
        return log, download_filename("events.log")
    if which == "maintenance":
        return maint, download_filename("maintenance.hist")
    if which == "history":
        return "# climate.hist\n" + climate + "# fan.hist\n" + fan, download_filename("climate.txt")
    if which == "all":
        body = "# events.log\n" + log + "# climate.hist\n" + climate + "# fan.hist\n" + fan + "# maintenance.hist\n" + maint
        return body, download_filename("terrarium.txt")
    return None, None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def _send(self, code, content_type, body, headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _qs(self):
        if "?" not in self.path:
            return {}
        out = {}
        for part in self.path.split("?", 1)[1].split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k] = v
        return out

    def do_GET(self):
        ensure_data()
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, "text/html; charset=utf-8", page_html())
            return
        if path == "/api/status":
            self._send(200, "application/json", json.dumps(status_body()))
            return
        if path == "/api/history":
            self._send(200, "application/json", json.dumps(history_body(self._qs())))
            return
        if path == "/api/logs":
            self._send(200, "text/plain; charset=utf-8", log_text())
            return
        if path == "/api/maintenance":
            self._send(200, "application/json", json.dumps({"events": MAINT}))
            return
        if path == "/api/download":
            which = self._qs().get("which", "")
            body, filename = download_body(which)
            if body is None:
                self._send(404, "text/plain", "Not found")
                return
            self._send(200, "text/plain; charset=utf-8", body, {
                "Content-Disposition": 'attachment; filename="%s"' % filename,
            })
            return
        self._send(404, "text/plain", "Not found")

    def do_POST(self):
        global MAINT_REV, CATEGORIES, PREVIEW_TZ, SENSOR_AT, POINTS
        ensure_data()
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(min(length, 4096)) if length else b""
        data = {}
        if raw:
            try:
                data = json.loads(raw.decode() or "{}")
            except json.JSONDecodeError:
                data = None
        if path == "/api/maintenance":
            if data is None:
                self._send(400, "text/plain", "Invalid maintenance.")
                return
            kind = data.get("kind")
            text = data.get("text") or ""
            allowed = [name.lower() for name in CATEGORIES]
            if str(kind).lower() == "note":
                kind = "note"
            elif str(kind).lower() in allowed:
                kind = CATEGORIES[allowed.index(str(kind).lower())]
            else:
                self._send(400, "text/plain", "Unknown maintenance kind.")
                return
            note = ""
            if kind == "note":
                note = " ".join(str(text).split())[:120]
                if not note:
                    self._send(400, "text/plain", "Note is empty.")
                    return
            MAINT.insert(0, [int(time.time()), kind, note])
            del MAINT[100:]
            MAINT_REV += 1
            self._send(200, "application/json", json.dumps({"events": MAINT}))
            return
        if path == "/api/settings":
            if data is None:
                self._send(400, "text/plain", "Invalid settings.")
                return
            zone = str(data.get("timezone") or "").strip()
            zones = ("UTC", "Europe/London", "Europe/Paris", "America/New_York",
                     "America/Chicago", "America/Denver", "America/Los_Angeles")
            if zone not in zones:
                self._send(400, "text/plain", "Unknown timezone.")
                return
            PREVIEW_TZ = zone
            CATEGORIES = parse_categories(str(data.get("maint_categories") or ""))
            self._send(200, "application/json", json.dumps(status_body()))
            return
        if path == "/api/sensor":
            SENSOR_AT = int(time.time())
            self._send(200, "application/json", json.dumps(status_body()))
            return
        if path == "/api/purge-maintenance":
            MAINT[:] = []
            MAINT_REV += 1
        elif path == "/api/purge-history":
            POINTS = []
        elif path == "/api/purge-all":
            MAINT[:] = []
            MAINT_REV += 1
            POINTS = []
        self._send(200, "application/json", json.dumps(status_body()))


def preview_timezone():
    path = ROOT / "wifi.cfg"
    name = ""
    fixed = ""
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("timezone="):
                name = line.split("=", 1)[1].strip()
            elif line.startswith("utc_offset_hours="):
                fixed = line.split("=", 1)[1].strip()
    if name:
        return name
    if fixed:
        return "UTC%s" % fixed
    return "Europe/London"


def status_body():
    global SENSOR_AT
    now = int(time.time())
    if SENSOR_AT is None:
        SENSOR_AT = now - 8 * 60
    last = POINTS[-1] if POINTS else (now, 23.0, 55.0, 0)
    temp_avg, hum_avg = averages()
    segs = "0" * 16 + "1" * 12 + "0" * 20
    return {
        "fan_on": False,
        "mode": "Idle",
        "remaining_s": 0,
        "next_run_s": 2 * 3600 + 15 * 60,
        "manual_min": 5,
        "segments": segs,
        "weekdays": "1111111",
        "schedule": "08:00-14:00",
        "temp_c": last[1],
        "humidity": last[2],
        "temp_avg": temp_avg,
        "hum_avg": hum_avg,
        "sensor_ok": True,
        "sensor_error": None,
        "sensor_at": SENSOR_AT,
        "sensor_interval_min": 15,
        "categories": CATEGORIES,
        "log_keep": 100,
        "time_synced": True,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": PREVIEW_TZ or preview_timezone(),
        "mem_used": 120304,
        "mem_free": 84216,
        "storage_used": 180224,
        "storage_free": 1400832,
        "boot": BOOT,
        "log_rev": 0,
        "maint_rev": MAINT_REV,
    }


def history_body(qs):
    try:
        days = int(qs.get("days", "7"))
    except ValueError:
        days = 7
    if days not in (1, 3, 7, 30):
        days = 7
    now = int(time.time())
    window = [p for p in POINTS if p[0] >= now - days * DAY]
    if "page" in qs:
        try:
            page = max(0, int(qs.get("page", "0")))
        except ValueError:
            page = 0
        rows = list(reversed(window))
        chunk = rows[page * 50:(page + 1) * 50]
        return {
            "days": days,
            "total": len(rows),
            "page": page,
            "samples": [list(p) for p in chunk],
        }
    pts = window
    if len(pts) > 400:
        step = (len(pts) + 399) // 400
        pts = pts[::step]
    fan = None
    if days <= 7:
        cutoff = now - days * DAY
        fan = [r for r in FANS if r[1] >= cutoff]
    return {"days": days, "count": len(window), "points": [list(p) for p in pts], "fan": fan}


def log_text():
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    return "\n".join([
        "[%s] Terrarium Climate Controller starting (preview)." % now,
        "[%s] Memory: free=90000 alloc=40000 history=%d fan=%d log=4 maint=%d" % (
            now, len(POINTS), len(FANS), len(MAINT)),
        "[%s] Sensor: read failed (preview example)." % now,
    ])


def main():
    ensure_data()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("Preview at http://127.0.0.1:%d/" % PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
