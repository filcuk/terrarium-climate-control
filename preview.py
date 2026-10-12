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


def _synthetic_climate(start, end):
    """Age-tiered fake readings from start (inclusive) up to end (exclusive)."""
    raw = []
    t = start
    while t < end:
        age = end - t
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
        return _synthetic_climate(now - 30 * DAY, now)
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
    points = [p for p in points if now - p[0] <= 30 * DAY]
    # A stale device/climate.hist dump leaves the 7-day window empty; fill to now.
    if points and now - points[-1][0] > 3600:
        points = points + _synthetic_climate(points[-1][0] + 15 * 60, now)
    return points


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
        # Synthetic week of runs for the samples UI: scheduled half-hours,
        # short on-demand taps, and edges that fall between 15-minute readings.
        runs = [
            [now - 45 * 60, now - 45 * 60 + 8 * 60, 1],           # on-demand, recent
            [now - 2 * 3600 - 7 * 60, now - 2 * 3600 + 23 * 60, 0],  # scheduled, mid-slot edges
            [now - 5 * 3600, now - 5 * 3600 + 30 * 60, 0],
            [now - 9 * 3600 - 3 * 60, now - 9 * 3600 + 12 * 60, 1],
            [now - 26 * 3600, now - 26 * 3600 + 8 * 60, 1],
            [now - 2 * DAY - 4 * 3600, now - 2 * DAY - 4 * 3600 + 30 * 60, 0],
            [now - 3 * DAY - 10 * 60, now - 3 * DAY + 20 * 60, 0],
            [now - 4 * DAY - 2 * 3600, now - 4 * DAY - 2 * 3600 + 15 * 60, 1],
            [now - 5 * DAY, now - 5 * DAY + 30 * 60, 0],
            [now - 6 * DAY - 90 * 60, now - 6 * DAY - 90 * 60 + 30 * 60, 0],
        ]
    return runs


POINTS = None
FANS = None
MAINT = None
MAINT_REV = 0
BOOT = int(time.time())
CATEGORIES = ["Mist", "Feed", "Soil", "Deco"]
SENSOR_AT = None
PREVIEW_TZ = None
SAMPLE_PAUSE = None
RECORDING = True
PAUSE_SINCE = 0
PAUSES = []
HUMIDITY_LIMIT = 0
HUMIDITY_GATE = 5
DAILY_RESET = 1
DAILY_RESET_HOUR = 3
DAILY_RESET_MINUTE = 0


def ensure_data():
    global POINTS, FANS, MAINT, SAMPLE_PAUSE
    now = int(time.time())
    if POINTS is None:
        POINTS = climate_points(now)
        FANS = fan_runs(now)
        SAMPLE_PAUSE = (now - 20 * 3600, now - 18 * 3600)
        POINTS = [p for p in POINTS if p[0] < SAMPLE_PAUSE[0] or p[0] > SAMPLE_PAUSE[1]]
        # Land one fan start on a climate timestamp so a flags=both row exists too.
        if POINTS and FANS:
            climate_ts = set(int(p[0]) for p in POINTS)
            if int(FANS[0][0]) not in climate_ts:
                anchor = int(POINTS[-3][0]) if len(POINTS) >= 3 else int(POINTS[-1][0])
                FANS[0] = [anchor, anchor + 8 * 60, int(FANS[0][2])]
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
    pause_rows = list(PAUSES)
    if SAMPLE_PAUSE:
        pause_rows = [SAMPLE_PAUSE] + pause_rows
    pause = "".join("%d,%d\n" % (row[0], row[1]) for row in pause_rows)
    maint = "".join("%d,%s%s\n" % (row[0], row[1], ("," + row[2]) if row[2] else "") for row in MAINT)
    if which == "log":
        return log, download_filename("events.log")
    if which == "maintenance":
        return maint, download_filename("maintenance.hist")
    if which == "fan":
        return fan, download_filename("fan.hist")
    if which == "history":
        return "# climate.hist\n" + climate + "# fan.hist\n" + fan + "# pause.hist\n" + pause, download_filename("climate.txt")
    if which == "all":
        body = "# events.log\n" + log + "# climate.hist\n" + climate + "# fan.hist\n" + fan + "# pause.hist\n" + pause + "# maintenance.hist\n" + maint
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
        if path == "/icon.webp":
            data = (ROOT / "res" / "icon.webp").read_bytes()
            self._send(200, "image/webp", data)
            return
        if path == "/favicon.ico":
            data = (ROOT / "res" / "favicon.ico").read_bytes()
            self._send(200, "image/x-icon", data)
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
        global MAINT_REV, CATEGORIES, PREVIEW_TZ, SENSOR_AT, POINTS, FANS
        global RECORDING, PAUSE_SINCE, PAUSES, SAMPLE_PAUSE, HUMIDITY_LIMIT, HUMIDITY_GATE
        global DAILY_RESET, DAILY_RESET_HOUR, DAILY_RESET_MINUTE
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
            try:
                record = int(data.get("record", 1))
            except (TypeError, ValueError):
                record = 1
            try:
                daily_reset = int(data.get("daily_reset", 1))
                daily_reset_hour = int(data.get("daily_reset_hour", 3))
                daily_reset_minute = int(data.get("daily_reset_minute", 0))
            except (TypeError, ValueError):
                self._send(400, "text/plain", "Invalid daily reset.")
                return
            if daily_reset not in (0, 1) or not 0 <= daily_reset_hour <= 23 or not 0 <= daily_reset_minute <= 59:
                self._send(400, "text/plain", "Invalid daily reset.")
                return
            DAILY_RESET = daily_reset
            DAILY_RESET_HOUR = daily_reset_hour
            DAILY_RESET_MINUTE = daily_reset_minute
            now = int(time.time())
            if record:
                if not RECORDING and PAUSE_SINCE:
                    PAUSES.append((PAUSE_SINCE, now))
                PAUSE_SINCE = 0
                RECORDING = True
            else:
                if RECORDING and not PAUSE_SINCE:
                    PAUSE_SINCE = now
                RECORDING = False
            self._send(200, "application/json", json.dumps(status_body()))
            return
        if path == "/api/schedule":
            if data is None:
                self._send(400, "text/plain", "Invalid schedule.")
                return
            try:
                limit = int(data.get("humidity_limit", 0))
                gate = int(data.get("humidity_gate", 5))
            except (TypeError, ValueError):
                self._send(400, "text/plain", "Invalid schedule.")
                return
            if not 0 <= limit <= 100:
                self._send(400, "text/plain", "Humidity limit must be 0-100.")
                return
            if not 1 <= gate <= 20:
                self._send(400, "text/plain", "Humidity gate must be 1-20.")
                return
            HUMIDITY_LIMIT = limit
            HUMIDITY_GATE = gate
            self._send(200, "application/json", json.dumps(status_body()))
            return
        if path == "/api/history-delete":
            if data is None:
                self._send(400, "text/plain", "Sample not found.")
                return
            try:
                ts = int(data.get("ts"))
            except (TypeError, ValueError):
                self._send(400, "text/plain", "Sample not found.")
                return
            which = data.get("which")

            def fan_match(start, end):
                return start == ts or end == ts or (start <= ts < end)

            has_climate = any(int(p[0]) == ts for p in POINTS)
            has_fan = any(fan_match(int(r[0]), int(r[1])) for r in FANS)
            if which is None:
                if has_climate and has_fan:
                    self._send(400, "text/plain", "Choose climate or fan.")
                    return
                which = "climate" if has_climate else "fan" if has_fan else None
            if which == "climate":
                kept = [p for p in POINTS if int(p[0]) != ts]
                if len(kept) == len(POINTS):
                    self._send(400, "text/plain", "Sample not found.")
                    return
                POINTS = kept
            elif which == "fan":
                kept = []
                removed = False
                for r in FANS:
                    if not removed and fan_match(int(r[0]), int(r[1])):
                        removed = True
                        continue
                    kept.append(r)
                if not removed:
                    self._send(400, "text/plain", "Fan run not found.")
                    return
                FANS = kept
            else:
                self._send(400, "text/plain", "Sample not found.")
                return
            self._send(200, "application/json", json.dumps(status_body()))
            return
        if path == "/api/maintenance-delete":
            if data is None:
                self._send(400, "text/plain", "Record not found.")
                return
            try:
                ts = int(data.get("ts"))
            except (TypeError, ValueError):
                self._send(400, "text/plain", "Record not found.")
                return
            kind = str(data.get("kind") or "")
            text = data.get("text") or ""
            if kind.lower() == "note":
                kind = "note"
                text = " ".join(str(text).split())[:120]
            for i, row in enumerate(MAINT):
                if row[0] == ts and row[1] == kind and (row[2] or "") == text:
                    del MAINT[i]
                    MAINT_REV += 1
                    self._send(200, "application/json", json.dumps({"events": MAINT}))
                    return
            self._send(400, "text/plain", "Record not found.")
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
            PAUSES = []
            SAMPLE_PAUSE = None
            PAUSE_SINCE = 0
        elif path == "/api/purge-all":
            MAINT[:] = []
            MAINT_REV += 1
            POINTS = []
            PAUSES = []
            SAMPLE_PAUSE = None
            PAUSE_SINCE = 0
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
    held = bool(RECORDING and HUMIDITY_LIMIT) and last[2] <= HUMIDITY_LIMIT
    return {
        "fan_on": False,
        "mode": "Humidity hold" if held else "Idle",
        "remaining_s": 0,
        "next_run_s": None if held else 2 * 3600 + 15 * 60,
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
        "record": 0 if not RECORDING else 1,
        "daily_reset": DAILY_RESET,
        "daily_reset_hour": DAILY_RESET_HOUR,
        "daily_reset_minute": DAILY_RESET_MINUTE,
        "humidity_limit": HUMIDITY_LIMIT,
        "humidity_gate": HUMIDITY_GATE,
        "humidity_hold": held,
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


def preview_sample_events(window, cutoff, include_fan):
    runs = [r for r in FANS if r[1] >= cutoff] if include_fan else []
    edges = {}
    if include_fan:
        for start, end, _manual in runs:
            if start >= cutoff:
                edges[start] = 1
            if end >= cutoff:
                edges[end] = 0

    def fan_on_at(ts):
        for start, end, _manual in runs:
            if start <= ts < end:
                return 1
        return 0

    events = []
    for p in window:
        ts = int(p[0])
        if ts in edges:
            fan = edges.pop(ts)
            events.append([ts, p[1], p[2], p[3] if len(p) > 3 else 0, fan, 3])
        else:
            fan = fan_on_at(ts) if include_fan else 0
            events.append([ts, p[1], p[2], p[3] if len(p) > 3 else 0, fan, 1])
    for ts, fan in edges.items():
        events.append([ts, None, None, 0, fan, 2])
    if edges:
        events.sort(key=lambda row: row[0])
    return events


def history_body(qs):
    try:
        days = int(qs.get("days", "7"))
    except ValueError:
        days = 7
    if days not in (1, 3, 7, 30):
        days = 7
    now = int(time.time())
    cutoff = now - days * DAY
    window = [p for p in POINTS if p[0] >= cutoff]
    include_fan = days <= 7
    if "page" in qs:
        try:
            page = max(0, int(qs.get("page", "0")))
        except ValueError:
            page = 0
        events = preview_sample_events(window, cutoff, include_fan)
        rows = list(reversed(events))
        chunk = rows[page * 50:(page + 1) * 50]
        return {
            "days": days,
            "total": len(rows),
            "page": page,
            "samples": chunk,
        }
    pts = window
    if len(pts) > 400:
        step = (len(pts) + 399) // 400
        pts = pts[::step]
    fan = None
    if include_fan:
        fan = [r for r in FANS if r[1] >= cutoff]
    pauses = []
    if SAMPLE_PAUSE and SAMPLE_PAUSE[1] > cutoff:
        pauses.append([max(SAMPLE_PAUSE[0], cutoff), min(SAMPLE_PAUSE[1], now)])
    for start, end in PAUSES:
        if end > cutoff:
            pauses.append([max(start, cutoff), end])
    if not RECORDING and PAUSE_SINCE:
        pauses.append([max(PAUSE_SINCE, cutoff), now])
    return {"days": days, "count": len(window), "points": [list(p) for p in pts], "fan": fan, "pauses": pauses}


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
