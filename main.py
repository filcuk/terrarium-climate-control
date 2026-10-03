import gc
import json
import machine
import network
import os
import socket
import sys
import utime

# A soft reboot does not free the ESP32 WiFi driver. Starting it again raises
# OSError: WiFi Out of Memory. Hard-reset once; the next boot has a clean heap.
if machine.reset_cause() == machine.SOFT_RESET:
  print("Soft reboot leaves WiFi memory allocated; hard-resetting.")
  machine.reset()

try:
  import ntptime
except ImportError:
  ntptime = None

try:
  from dht import DHT22
except ImportError:
  DHT22 = None

# ESP32-C3 pin map (see README for wiring)
FAN_GPIO = 21  # drives transistor/MOSFET gate/base (fan low-side switch)
BTN_GPIO = 5  # momentary button to GND
DHT_GPIO = 1  # ASAIR AM2302 / DHT22 data pin

fan_pin = machine.Pin(FAN_GPIO, machine.Pin.OUT, value=0)
btn_pin = machine.Pin(BTN_GPIO, machine.Pin.IN, machine.Pin.PULL_UP)

# Timing Constants (in milliseconds)
SECOND = 1000
MINUTE = 60 * SECOND
DAY_S = 24 * 60 * 60

# Defaults, used until schedule.cfg / settings.cfg are saved from the web page
DEFAULT_MANUAL_MIN = 5  # on-demand run length
DEFAULT_SENSOR_INTERVAL_MIN = 15  # AM2302 is read on this interval; minimum is 5
MIN_SENSOR_INTERVAL_MIN = 5
MAX_SENSOR_INTERVAL_MIN = 60
DEFAULT_LOG_KEEP = 100
MIN_LOG_KEEP = 20
MAX_LOG_KEEP = 250
# Climate history is folded as it ages: raw, 1h, 4h, 12h, then dropped after 30 days.
# 500 is only a guard. A 5-minute readout for 30 days is about 410 points.
HISTORY_HARD_CAP = 500
FAN_KEEP_S = 7 * DAY_S
FAN_RUNS_MAX = 500  # guard for a stuck fan; a normal week stays well under this
CHART_MAX_POINTS = 400
SAMPLE_PAGE = 50
SLOT_S = 30 * 60
N_SLOTS = 48
MEM_LOG_MS = 60 * MINUTE
MAINT_KEEP_S = 30 * DAY_S
MAINT_MAX = 100
MAINT_KINDS = ("mist", "feed", "soil", "deco", "note")
NOTE_MAX = 120

DEBOUNCE_MS = 50  # button must read steadily for this long to count
# After the fan turns ON, ignore the button briefly. Fan motors inject noise into
# nearby GPIO lines; that noise can look like a second press and cancel a start.
EMI_BLANK_MS = 400
# Wait after boot before the fan can switch on, so USB (and Thonny) can connect
# before the fan's startup current dips the shared USB 5V.
STARTUP_DELAY_MS = 3 * SECOND
LOOP_MS = 10

# Files on the board
WIFI_CFG = "wifi.cfg"
SCHEDULE_CFG = "schedule.cfg"
SETTINGS_CFG = "settings.cfg"
LOG_FILE = "events.log"
OLD_LOG_FILE = "fan.log"  # name used by earlier versions; renamed on boot
HISTORY_FILE = "climate.hist"
FAN_HISTORY_FILE = "fan.hist"
MAINT_FILE = "maintenance.hist"

WEB_PORT = 80
DEFAULT_HOSTNAME = "terrarium"  # reachable as http://terrarium.local/; override with hostname= in wifi.cfg
WIFI_RETRY_MIN_MS = 30 * SECOND
WIFI_RETRY_MAX_MS = 10 * MINUTE

# State Variables
manual_ms = DEFAULT_MANUAL_MIN * MINUTE
sensor_interval_ms = DEFAULT_SENSOR_INTERVAL_MIN * MINUTE
log_keep = DEFAULT_LOG_KEEP

manual_start = 0
manual_running = False
suppressed_until = 0  # device UTC seconds; schedule stays off until then
btn_blank_start = None
fan_was_active = False
sched_was_running = False
# 48 half-hours from local midnight. 1 = fan scheduled on. Default 08:00-14:00.
segments = [0] * N_SLOTS
for _i in range(16, 28):
  segments[_i] = 1
# Monday is 0. Default every day, so an older schedule file keeps running all week.
weekdays = [1] * 7

# Button debounce state (1 = released, 0 = pressed)
btn_stable = 1
btn_last_raw = 1
btn_changed_at = utime.ticks_ms()

# WiFi / web state
wlan = None
wifi_cfg = {}
wifi_connected = False
wifi_attempt_at = 0
wifi_retry_ms = WIFI_RETRY_MIN_MS
device_ip = None
hostname = None
server = None
time_synced = False
tz_offset_s = 0
tz_name = "Europe/London"
tz_fixed = False  # True when an old utc_offset_hours is in use (no summer time)
tz_checked_at = 0

# Log state
log_lines = []
log_file_lines = 0

# Climate sensor (AM2302 / DHT22)
dht = None
temp_c = None
humidity = None
sensor_ok = False
sensor_error = None
sensor_read_at = 0

# Climate history: (device UTC seconds, temp, humidity, span seconds). span 0 = raw reading.
history = []
history_stored_at = 0
# Fan runs: (start, end, manual) device UTC seconds. manual 1 = on-demand.
fan_runs = []
fan_run_start = None
fan_run_manual = 0
# Maintenance: (device UTC seconds, kind), oldest first
maintenance = []
mem_logged_at = 0


# ---------------------------------------------------------------- helpers

def fmt_duration(ms):
  """Format a millisecond duration for logs, e.g. 300000 -> '5m', 90000 -> '1m 30s'."""
  total_s = ms // SECOND
  hours = total_s // 3600
  minutes = (total_s % 3600) // 60
  seconds = total_s % 60
  parts = []
  if hours:
    parts.append("%dh" % hours)
  if minutes:
    parts.append("%dm" % minutes)
  if seconds or not parts:
    parts.append("%ds" % seconds)
  return " ".join(parts)


def read_cfg(path):
  """Read a simple key=value file. Blank lines and lines starting with # are ignored."""
  cfg = {}
  try:
    with open(path) as f:
      for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
          continue
        key, value = line.split("=", 1)
        cfg[key.strip()] = value.strip()
  except OSError:
    pass
  return cfg


def timestamp():
  if time_synced:
    t = utime.localtime(utime.time() + tz_offset_s)
    return "%04d-%02d-%02d %02d:%02d:%02d" % t[:6]
  return "uptime %ds" % (utime.ticks_ms() // SECOND)


def timezone_label():
  if tz_name:
    return tz_name
  sign = "+" if tz_offset_s >= 0 else "-"
  minutes = abs(int(tz_offset_s)) // 60
  hours, mins = minutes // 60, minutes % 60
  if mins:
    return "UTC%s%d:%02d" % (sign, hours, mins)
  return "UTC%s%d" % (sign, hours)


# Some MicroPython builds count time from 2000-01-01 instead of 1970; the browser needs 1970
EPOCH_OFFSET_S = 946684800 if utime.gmtime(0)[0] == 2000 else 0


# name -> (standard offset hours, summer offset hours, rule)
# "eu": last Sunday of March 01:00 UTC through last Sunday of October 01:00 UTC
# "us": second Sunday of March 02:00 local standard through first Sunday of November 02:00 local daylight
ZONES = {
  "UTC": (0, 0, ""),
  "Europe/London": (0, 1, "eu"),
  "Europe/Paris": (1, 2, "eu"),
  "America/New_York": (-5, -4, "us"),
  "America/Chicago": (-6, -5, "us"),
  "America/Denver": (-7, -6, "us"),
  "America/Los_Angeles": (-8, -7, "us"),
}


def days_from_civil(y, m, d):
  """Days since 1970-01-01 (Howard Hinnant)."""
  y -= m <= 2
  era = (y if y >= 0 else y - 399) // 400
  yoe = y - era * 400
  doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
  doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
  return era * 146097 + doe - 719468


def civil_from_days(z):
  """Inverse of days_from_civil. Returns (year, month, day)."""
  z += 719468
  era = (z if z >= 0 else z - 146096) // 146097
  doe = z - era * 146097
  yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
  y = yoe + era * 400
  doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
  mp = (5 * doy + 2) // 153
  d = doy - (153 * mp + 2) // 5 + 1
  m = mp + 3 if mp < 10 else mp - 9
  return y + (m <= 2), m, d


def unix_ts(y, m, d, hh=0):
  return days_from_civil(y, m, d) * DAY_S + hh * 3600


def weekday_mon0(y, m, d):
  """Monday is 0. 1970-01-01 was a Thursday."""
  return (days_from_civil(y, m, d) + 3) % 7


def last_day(y, m):
  if m == 12:
    return days_from_civil(y + 1, 1, 1) - days_from_civil(y, m, 1)
  return days_from_civil(y, m + 1, 1) - days_from_civil(y, m, 1)


def nth_sunday(y, m, n):
  """n = 1, 2, ... or -1 for the last Sunday. Sunday's Monday-based weekday is 6."""
  if n < 0:
    ld = last_day(y, m)
    return ld - (weekday_mon0(y, m, ld) - 6) % 7
  first = 1 + (6 - weekday_mon0(y, m, 1)) % 7
  return first + (n - 1) * 7


def zone_offset_s(name, device_ts):
  std_h, dst_h, rule = ZONES.get(name, ZONES["UTC"])
  if not rule:
    return std_h * 3600
  unix = device_ts + EPOCH_OFFSET_S
  y, _, _ = civil_from_days(unix // DAY_S)
  if rule == "eu":
    start = unix_ts(y, 3, nth_sunday(y, 3, -1), 1)
    end = unix_ts(y, 10, nth_sunday(y, 10, -1), 1)
  else:
    start = unix_ts(y, 3, nth_sunday(y, 3, 2), 2) - std_h * 3600
    end = unix_ts(y, 11, nth_sunday(y, 11, 1), 2) - dst_h * 3600
  return (dst_h if start <= unix < end else std_h) * 3600


def refresh_tz(force=False):
  """Recompute the cached local offset. Summer time flips without a restart."""
  global tz_offset_s, tz_checked_at
  if tz_fixed:
    return
  now_ms = utime.ticks_ms()
  if not force and tz_checked_at and utime.ticks_diff(now_ms, tz_checked_at) < MINUTE:
    return
  tz_checked_at = now_ms
  tz_offset_s = zone_offset_s(tz_name, utime.time())


def apply_zone(cfg):
  """timezone= wins. An old utc_offset_hours file keeps that fixed offset."""
  global tz_name, tz_fixed, tz_offset_s
  name = cfg.get("timezone", "").strip()
  if name:
    if name not in ZONES:
      log("Timezone '%s' is not built in; using UTC." % name)
      tz_name = "UTC"
    else:
      tz_name = name
    tz_fixed = False
  elif "utc_offset_hours" in cfg:
    tz_fixed = True
    tz_name = ""
    try:
      tz_offset_s = int(float(cfg["utc_offset_hours"]) * 3600)
    except ValueError:
      tz_offset_s = 0
    log("Timezone: fixed UTC offset %+gh (no summer time)." % (tz_offset_s / 3600.0))
    return
  else:
    tz_name = "Europe/London"
    tz_fixed = False
  refresh_tz(True)
  log("Timezone: %s." % tz_name)


# ---------------------------------------------------------------- logging

def rewrite_log_file():
  global log_file_lines
  try:
    with open(LOG_FILE, "w") as f:
      for line in log_lines:
        f.write(line + "\n")
    log_file_lines = len(log_lines)
  except OSError:
    pass


def trim_log_memory():
  while len(log_lines) > log_keep:
    log_lines.pop(0)


def migrate_log_file():
  files = os.listdir()
  if OLD_LOG_FILE in files and LOG_FILE not in files:
    try:
      os.rename(OLD_LOG_FILE, LOG_FILE)
    except OSError:
      pass


def load_log():
  global log_file_lines
  migrate_log_file()
  lines = []
  try:
    with open(LOG_FILE) as f:
      for line in f:
        lines.append(line.rstrip("\n"))
        if len(lines) > log_keep:
          lines.pop(0)
  except OSError:
    pass
  log_lines.extend(lines)
  rewrite_log_file()


def log(msg):
  """Print, keep the last log_keep lines in memory, and append to the rolling log file."""
  global log_file_lines
  line = "[%s] %s" % (timestamp(), msg)
  print(line)
  log_lines.append(line)
  trim_log_memory()
  try:
    with open(LOG_FILE, "a") as f:
      f.write(line + "\n")
    log_file_lines += 1
  except OSError:
    pass
  # Let the file grow to 2x before trimming, so flash isn't rewritten on every line
  if log_file_lines > 2 * log_keep:
    rewrite_log_file()


def purge_log():
  global log_file_lines
  log_lines[:] = []
  log_file_lines = 0
  try:
    with open(LOG_FILE, "w") as f:
      pass
  except OSError:
    pass
  log("Log purged.")


# ---------------------------------------------------------------- settings

def load_settings():
  global sensor_interval_ms, log_keep
  cfg = read_cfg(SETTINGS_CFG)
  # sensor_interval_s from older builds is ignored so a saved 10 seconds becomes 15 minutes
  try:
    interval = int(cfg["sensor_interval_min"])
  except (KeyError, ValueError):
    interval = DEFAULT_SENSOR_INTERVAL_MIN
  if not MIN_SENSOR_INTERVAL_MIN <= interval <= MAX_SENSOR_INTERVAL_MIN:
    interval = DEFAULT_SENSOR_INTERVAL_MIN
  try:
    keep = int(cfg.get("log_keep", DEFAULT_LOG_KEEP))
  except ValueError:
    keep = DEFAULT_LOG_KEEP
  if keep < MIN_LOG_KEEP:
    keep = MIN_LOG_KEEP
  elif keep > MAX_LOG_KEEP:
    keep = MAX_LOG_KEEP
  sensor_interval_ms = interval * MINUTE
  log_keep = keep


def validate_settings(interval, keep):
  if not MIN_SENSOR_INTERVAL_MIN <= interval <= MAX_SENSOR_INTERVAL_MIN:
    raise ValueError("Sensor interval must be %d-%d minutes." % (MIN_SENSOR_INTERVAL_MIN, MAX_SENSOR_INTERVAL_MIN))
  if not MIN_LOG_KEEP <= keep <= MAX_LOG_KEEP:
    raise ValueError("Log lines kept must be %d-%d." % (MIN_LOG_KEEP, MAX_LOG_KEEP))


def save_settings(interval, keep):
  global sensor_interval_ms, log_keep
  validate_settings(interval, keep)
  with open(SETTINGS_CFG, "w") as f:
    f.write("sensor_interval_min=%d\nlog_keep=%d\n" % (interval, keep))
  sensor_interval_ms = interval * MINUTE
  log_keep = keep
  trim_log_memory()
  rewrite_log_file()
  log("Settings changed: sensor every %d min, keep %d log lines." % (interval, keep))


# ---------------------------------------------------------------- schedule

def load_schedule():
  global manual_ms
  cfg = read_cfg(SCHEDULE_CFG)
  try:
    manual = int(cfg.get("manual_min", DEFAULT_MANUAL_MIN))
  except ValueError:
    manual = DEFAULT_MANUAL_MIN
  if not 1 <= manual <= 1440:
    manual = DEFAULT_MANUAL_MIN
  manual_ms = manual * MINUTE
  text = cfg.get("segments", "")
  if len(text) == N_SLOTS and all(c in "01" for c in text):
    for i, c in enumerate(text):
      segments[i] = 1 if c == "1" else 0
  # No segments line (including an old period/duration file) keeps the 08:00-14:00 default.
  days = cfg.get("weekdays", "")
  if len(days) == 7 and all(c in "01" for c in days):
    for i, c in enumerate(days):
      weekdays[i] = 1 if c == "1" else 0
  # No weekdays line keeps every day selected.


def validate_schedule(text, manual, days):
  if len(text) != N_SLOTS or any(c not in "01" for c in text):
    raise ValueError("Schedule must be 48 half-hour segments of 0 or 1.")
  if len(days) != 7 or any(c not in "01" for c in days):
    raise ValueError("Weekdays must be 7 values of 0 or 1, Monday first.")
  if not 1 <= manual <= 1440:
    raise ValueError("On-demand run must be 1-1440 minutes.")


def save_schedule(text, manual, days):
  global manual_ms, suppressed_until
  validate_schedule(text, manual, days)
  with open(SCHEDULE_CFG, "w") as f:
    f.write("manual_min=%d\nsegments=%s\nweekdays=%s\n" % (manual, text, days))
  manual_ms = manual * MINUTE
  for i, c in enumerate(text):
    segments[i] = 1 if c == "1" else 0
  for i, c in enumerate(days):
    weekdays[i] = 1 if c == "1" else 0
  suppressed_until = 0
  log("Schedule changed: %s" % schedule_text())


def slot_clock(i):
  i = i % N_SLOTS
  return "%02d:%02d" % (i // 2, (i % 2) * 30)


def segments_text():
  if not any(segments):
    return "schedule off"
  if all(segments):
    return "00:00-24:00"
  i0 = 0
  guard = 0
  while segments[i0] and guard < N_SLOTS:
    i0 = (i0 + 1) % N_SLOTS
    guard += 1
  parts = []
  start = None
  for step in range(N_SLOTS):
    idx = (i0 + step) % N_SLOTS
    if segments[idx]:
      if start is None:
        start = idx
    elif start is not None:
      parts.append("%s-%s" % (slot_clock(start), slot_clock(idx)))
      start = None
  if start is not None:
    parts.append("%s-%s" % (slot_clock(start), slot_clock(i0)))
  return ", ".join(parts)


def weekdays_text():
  if not any(weekdays):
    return "no days"
  if all(weekdays):
    return ""
  names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
  parts = []
  start = None
  for i in range(8):
    on = i < 7 and weekdays[i]
    if on and start is None:
      start = i
    elif not on and start is not None:
      parts.append(names[start] if start == i - 1 else "%s-%s" % (names[start], names[i - 1]))
      start = None
  return ", ".join(parts)


def schedule_summary():
  picked = weekdays_text()
  base = segments_text()
  if picked:
    return "%s, %s" % (base, picked)
  return base


def schedule_text():
  return "%s | on-demand run: %s" % (schedule_summary(), fmt_duration(manual_ms))


def local_parts():
  """Return (device UTC seconds, local seconds, slot index 0-47, seconds into the slot)."""
  now_ts = utime.time()
  local = now_ts + tz_offset_s
  sec_day = int(local % DAY_S)
  return now_ts, local, sec_day // SLOT_S, sec_day % SLOT_S


def local_weekday(local):
  """Monday is 0. local is device-epoch seconds plus the timezone offset."""
  y, m, d = civil_from_days((int(local) + EPOCH_OFFSET_S) // DAY_S)
  return weekday_mon0(y, m, d)


def slot_selected(idx, weekday):
  return segments[idx] and weekdays[weekday % 7]


def stretch_remaining_s(idx, sec_into, weekday):
  """Seconds until this run of selected slots ends. An off day ends it at midnight."""
  n = 0
  i = idx
  wd = weekday
  while n < N_SLOTS * 7 and slot_selected(i, wd):
    n += 1
    i = (i + 1) % N_SLOTS
    if i == 0:
      wd = (wd + 1) % 7
  return n * SLOT_S - sec_into


def next_run_delay_s():
  """Seconds from now until the next selected slot on a selected day."""
  if not time_synced or not any(segments) or not any(weekdays):
    return None
  now_ts, local, idx, sec_into = local_parts()
  origin = now_ts
  if suppressed_until and now_ts < suppressed_until:
    origin = suppressed_until
    local = origin + tz_offset_s
    sec_day = int(local % DAY_S)
    idx = sec_day // SLOT_S
    sec_into = sec_day % SLOT_S
  weekday = local_weekday(local)
  for step in range(N_SLOTS * 7):
    if step == 0 and sec_into != 0:
      continue
    slot = (idx + step) % N_SLOTS
    wd = (weekday + (idx + step) // N_SLOTS) % 7
    if slot_selected(slot, wd):
      return step * SLOT_S - sec_into + (origin - now_ts)
  return None


def schedule_state(now):
  """Return (schedule_running, in_selected_slot, seconds_left_in_stretch)."""
  if not time_synced or not any(segments) or not any(weekdays):
    return False, False, 0
  now_ts, local, idx, sec_into = local_parts()
  weekday = local_weekday(local)
  if not slot_selected(idx, weekday):
    return False, False, 0
  if suppressed_until and now_ts < suppressed_until:
    return False, True, 0
  return True, True, stretch_remaining_s(idx, sec_into, weekday)


def toggle_fan(now, source):
  """Same action as the physical button: stop if running, otherwise start an on-demand run."""
  global manual_running, manual_start, suppressed_until, btn_blank_start
  sched_running, _, _ = schedule_state(now)
  if sched_running or manual_running:
    manual_running = False
    if sched_running:
      _, local, idx, sec_into = local_parts()
      suppressed_until = utime.time() + stretch_remaining_s(idx, sec_into, local_weekday(local))
    log("%s: fan STOPPED by user." % source)
  else:
    manual_running = True
    manual_start = now
    # Ignore button while the fan spins up (noise looks like another press)
    btn_blank_start = now
    log("%s: on-demand %s run STARTED." % (source, fmt_duration(manual_ms)))


def climate_averages():
  """Time-weighted mean. A 12-hour point counts for 12 hours, not as one row."""
  if not history:
    return None, None
  interval = sensor_interval_ms // SECOND
  if interval < 1:
    interval = 1
  wt = wh = w = 0.0
  for _, t, h, span in history:
    dur = span if span else interval
    wt += t * dur
    wh += h * dur
    w += dur
  if w <= 0:
    return None, None
  return wt / w, wh / w


def last_maint(kind):
  for ts, k, _text in reversed(maintenance):
    if k == kind:
      return ts + EPOCH_OFFSET_S
  return None


def storage_usage():
  """Used and free bytes on the filesystem. None if the port cannot report it."""
  try:
    st = os.statvfs("/")
  except (OSError, AttributeError):
    return None, None
  if len(st) < 5:
    return None, None
  block = st[1] or st[0]
  if not block:
    return None, None
  total = st[2] * block
  free = st[4] * block
  used = total - free
  if used < 0:
    used = 0
  return used, free


def status(now):
  sched_running, _, sched_left = schedule_state(now)
  manual_left = 0
  if manual_running:
    manual_left = max(0, (manual_ms - utime.ticks_diff(now, manual_start)) // SECOND)
  if sched_running:
    mode = "Scheduled run"
  elif manual_running:
    mode = "On-demand run"
  elif not time_synced:
    mode = "Waiting for clock"
  elif not any(segments) or not any(weekdays):
    mode = "Schedule off"
  else:
    mode = "Idle"
  temp_avg, hum_avg = climate_averages()
  next_run = None if sched_running else next_run_delay_s()
  store_used, store_free = storage_usage()
  return {
    "fan_on": sched_running or manual_running,
    "mode": mode,
    "remaining_s": max(manual_left, sched_left if sched_running else 0),
    "next_run_s": next_run,
    "manual_min": manual_ms // MINUTE,
    "segments": "".join("1" if s else "0" for s in segments),
    "weekdays": "".join("1" if d else "0" for d in weekdays),
    "schedule": schedule_summary(),
    "temp_c": temp_c,
    "humidity": humidity,
    "temp_avg": temp_avg,
    "hum_avg": hum_avg,
    "sensor_ok": sensor_ok,
    "sensor_error": sensor_error,
    "sensor_interval_min": sensor_interval_ms // MINUTE,
    "log_keep": log_keep,
    "log_count": len(log_lines),
    "history_points": len(history),
    "time_synced": bool(time_synced),
    "ip": device_ip,
    "hostname": hostname,
    "time": timestamp(),
    "timezone": timezone_label(),
    "mem_used": gc.mem_alloc(),
    "mem_free": gc.mem_free(),
    "storage_used": store_used,
    "storage_free": store_free,
    "last_mist": last_maint("mist"),
    "last_feed": last_maint("feed"),
  }


# ---------------------------------------------------------------- climate history

def tier_span(age):
  """0 keeps the reading. Positive is the average bucket. -1 drops the point."""
  if age <= DAY_S:
    return 0
  if age <= 3 * DAY_S:
    return 3600
  if age <= 7 * DAY_S:
    return 4 * 3600
  if age <= 30 * DAY_S:
    return 12 * 3600
  return -1


def bucket_start(ts, span):
  local = ts + tz_offset_s
  return local - (local % span) - tz_offset_s


def compact_history(now_ts=None):
  """Fold aged samples into hour, 4-hour, and 12-hour averages. Drop past 30 days."""
  if now_ts is None:
    now_ts = utime.time()
  merged = []
  for ts, t, h, _span in history:
    span = tier_span(now_ts - ts)
    if span < 0:
      continue
    if span:
      ts = bucket_start(ts, span)
    if merged and merged[-1][0] == ts and merged[-1][4] == span:
      merged[-1][1] += t
      merged[-1][2] += h
      merged[-1][3] += 1
    else:
      merged.append([ts, t, h, 1, span])
  history[:] = []
  for ts, st, sh, n, span in merged:
    history.append((ts, st / n, sh / n, span))
  while len(history) > HISTORY_HARD_CAP:
    history.pop(0)


def rewrite_history_file():
  try:
    with open(HISTORY_FILE, "w") as f:
      for ts, t, h, span in history:
        f.write("%d,%.1f,%.1f,%d\n" % (ts, t, h, span))
  except OSError:
    pass


def load_history():
  global history_stored_at
  history[:] = []
  try:
    with open(HISTORY_FILE) as f:
      for line in f:
        parts = line.strip().split(",")
        if len(parts) < 3:
          continue
        try:
          span = int(parts[3]) if len(parts) > 3 else 0
          history.append((int(parts[0]), float(parts[1]), float(parts[2]), span))
        except ValueError:
          continue
        # Fold as we read so a per-minute file is never held whole
        if len(history) > HISTORY_HARD_CAP + 40:
          compact_history()
  except OSError:
    pass
  compact_history()
  history_stored_at = history[-1][0] if history else 0
  rewrite_history_file()


def store_history_sample(t, h):
  """Store one raw reading, then fold anything that has aged into a coarser tier."""
  global history_stored_at
  if not time_synced:
    return
  now_ts = utime.time()
  gap = sensor_interval_ms // SECOND
  if history_stored_at and now_ts - history_stored_at < gap:
    return
  history.append((now_ts, t, h, 0))
  history_stored_at = now_ts
  compact_history(now_ts)
  rewrite_history_file()


def prune_fan_runs(now_ts):
  cutoff = now_ts - FAN_KEEP_S
  while fan_runs and fan_runs[0][1] < cutoff:
    fan_runs.pop(0)
  while len(fan_runs) > FAN_RUNS_MAX:
    fan_runs.pop(0)


def rewrite_fan_runs_file():
  try:
    with open(FAN_HISTORY_FILE, "w") as f:
      for start, end, manual in fan_runs:
        f.write("%d,%d,%d\n" % (start, end, manual))
  except OSError:
    pass


def load_fan_runs():
  now_ts = utime.time()
  cutoff = now_ts - FAN_KEEP_S
  fan_runs[:] = []
  try:
    with open(FAN_HISTORY_FILE) as f:
      for line in f:
        parts = line.strip().split(",")
        if len(parts) < 2:
          continue
        try:
          start = int(parts[0])
          end = int(parts[1])
          manual = int(parts[2]) if len(parts) > 2 else 0
        except ValueError:
          continue
        if end < cutoff:
          continue
        fan_runs.append((start, end, 1 if manual else 0))
        while len(fan_runs) > FAN_RUNS_MAX:
          fan_runs.pop(0)
  except OSError:
    pass
  prune_fan_runs(now_ts)
  rewrite_fan_runs_file()


def close_fan_run(now_ts):
  global fan_run_start
  if fan_run_start is None:
    return
  run = (fan_run_start, now_ts, fan_run_manual)
  fan_run_start = None
  fan_runs.append(run)
  prune_fan_runs(now_ts)
  if run not in fan_runs:
    return
  try:
    with open(FAN_HISTORY_FILE, "a") as f:
      f.write("%d,%d,%d\n" % run)
  except OSError:
    pass
  if len(fan_runs) % 50 == 0:
    rewrite_fan_runs_file()


def track_fan_run(fan_active, manual):
  """Record fan on/off periods. Schedule (manual=0) replaces on-demand mid-run."""
  global fan_run_start, fan_run_manual
  kind = 1 if manual else 0
  if fan_active and time_synced:
    if fan_run_start is None:
      fan_run_start = utime.time()
      fan_run_manual = kind
    elif fan_run_manual != kind:
      now_ts = utime.time()
      close_fan_run(now_ts)
      fan_run_start = now_ts
      fan_run_manual = kind
  elif fan_run_start is not None:
    close_fan_run(utime.time())


def history_json(days, page):
  if days not in (1, 3, 7, 30):
    days = 7
  now_ts = utime.time()
  cutoff = now_ts - days * DAY_S
  window = [p for p in history if p[0] >= cutoff]
  if page is not None:
    if page < 0:
      page = 0
    rows = window[::-1]
    chunk = rows[page * SAMPLE_PAGE:(page + 1) * SAMPLE_PAGE]
    samples = [[p[0] + EPOCH_OFFSET_S, p[1], p[2], p[3]] for p in chunk]
    return {"days": days, "total": len(rows), "page": page, "samples": samples}
  pts = window
  if len(pts) > CHART_MAX_POINTS:
    step = (len(pts) + CHART_MAX_POINTS - 1) // CHART_MAX_POINTS
    pts = pts[::step]
  points = [[p[0] + EPOCH_OFFSET_S, p[1], p[2], p[3]] for p in pts]
  # A window longer than the fan history would show only the recent slice.
  fan = None
  if days * DAY_S <= FAN_KEEP_S:
    runs = [r for r in fan_runs if r[1] >= cutoff]
    if fan_run_start is not None:
      runs.append((fan_run_start, now_ts, fan_run_manual))
    fan = [[s + EPOCH_OFFSET_S, e + EPOCH_OFFSET_S, m] for s, e, m in runs]
  return {"days": days, "count": len(window), "points": points, "fan": fan}


def purge_history():
  global history_stored_at, fan_run_start
  history[:] = []
  fan_runs[:] = []
  history_stored_at = 0
  if fan_run_start is not None:
    fan_run_start = utime.time()
  rewrite_history_file()
  rewrite_fan_runs_file()
  log("Climate history purged.")


# ---------------------------------------------------------------- maintenance

def prune_maintenance(now_ts):
  cutoff = now_ts - MAINT_KEEP_S
  while maintenance and maintenance[0][0] < cutoff:
    maintenance.pop(0)
  while len(maintenance) > MAINT_MAX:
    maintenance.pop(0)


def rewrite_maintenance_file():
  try:
    with open(MAINT_FILE, "w") as f:
      for ts, kind, text in maintenance:
        if text:
          f.write("%d,%s,%s\n" % (ts, kind, text))
        else:
          f.write("%d,%s\n" % (ts, kind))
  except OSError:
    pass


def load_maintenance():
  now_ts = utime.time()
  maintenance[:] = []
  try:
    with open(MAINT_FILE) as f:
      for line in f:
        parts = line.strip().split(",")
        if len(parts) < 2 or parts[1] not in MAINT_KINDS:
          continue
        text = ""
        if parts[1] == "note":
          text = " ".join(",".join(parts[2:]).split())[:NOTE_MAX]
        try:
          maintenance.append((int(parts[0]), parts[1], text))
        except ValueError:
          continue
        prune_maintenance(now_ts)
  except OSError:
    pass
  prune_maintenance(now_ts)
  rewrite_maintenance_file()


def record_maintenance(kind, text=""):
  if kind not in MAINT_KINDS:
    raise ValueError("Unknown maintenance kind.")
  if not time_synced:
    raise ValueError("Clock not synced.")
  note = ""
  if kind == "note":
    note = " ".join(str(text).split())
    if not note:
      raise ValueError("Note is empty.")
    note = note[:NOTE_MAX]
  now_ts = utime.time()
  maintenance.append((now_ts, kind, note))
  prune_maintenance(now_ts)
  rewrite_maintenance_file()
  log("Maintenance: %s." % kind)


def maintenance_json():
  events = [[ts + EPOCH_OFFSET_S, kind, text] for ts, kind, text in reversed(maintenance)]
  return {"events": events}


def purge_maintenance():
  maintenance[:] = []
  rewrite_maintenance_file()
  log("Maintenance history purged.")


# ---------------------------------------------------------------- climate sensor

def sensor_start():
  global dht
  if DHT22 is None:
    log("Sensor: dht module missing; climate readings disabled.")
    return
  try:
    dht = DHT22(machine.Pin(DHT_GPIO))
    log("Sensor: AM2302/DHT22 on GPIO%d" % DHT_GPIO)
  except Exception as e:
    dht = None
    log("Sensor: could not init: %s" % e)


def sensor_poll(now):
  """Read the AM2302 every sensor_interval_ms. Only a failed read is written to the log."""
  global temp_c, humidity, sensor_ok, sensor_error, sensor_read_at
  if dht is None:
    return
  if sensor_read_at and utime.ticks_diff(now, sensor_read_at) < sensor_interval_ms:
    return
  sensor_read_at = now
  try:
    dht.measure()
    temp_c = dht.temperature()
    humidity = dht.humidity()
    sensor_ok = True
    sensor_error = None
    store_history_sample(temp_c, humidity)
  except Exception as e:
    sensor_ok = False
    sensor_error = str(e)
    log("Sensor: read failed (%s)" % e)


# ---------------------------------------------------------------- button

def button_pressed(now):
  """Return True once per physical press (released -> held for DEBOUNCE_MS)."""
  global btn_stable, btn_last_raw, btn_changed_at

  raw = btn_pin.value()
  if raw != btn_last_raw:
    btn_last_raw = raw
    btn_changed_at = now
    return False

  if raw != btn_stable and utime.ticks_diff(now, btn_changed_at) >= DEBOUNCE_MS:
    btn_stable = raw
    return btn_stable == 0

  return False


# ---------------------------------------------------------------- wifi

def wifi_connect(now):
  global wifi_attempt_at
  wifi_attempt_at = now
  try:
    wlan.disconnect()
  except OSError:
    pass
  try:
    wlan.connect(wifi_cfg["ssid"], wifi_cfg.get("password", ""))
  except OSError as e:
    log("WiFi: connect error: %s" % e)


def log_memory():
  """Heap as it actually is: do not collect first."""
  log("Memory: free=%d alloc=%d history=%d fan=%d log=%d maint=%d" % (
    gc.mem_free(), gc.mem_alloc(), len(history), len(fan_runs), len(log_lines), len(maintenance)))


def wifi_start(now):
  global wlan, wifi_cfg, hostname
  wifi_cfg = read_cfg(WIFI_CFG)
  if not wifi_cfg.get("ssid"):
    log("WiFi: no %s with ssid=... found, web interface disabled." % WIFI_CFG)
    return
  apply_zone(wifi_cfg)
  # Hostname must be set before the interface comes up; the ESP32 port's mDNS
  # responder then answers for <hostname>.local
  name = wifi_cfg.get("hostname", DEFAULT_HOSTNAME)
  hostname = None
  try:
    network.hostname(name)  # MicroPython 1.20+
    hostname = name
  except (AttributeError, ValueError, OSError):
    pass
  gc.collect()
  try:
    wlan = network.WLAN(network.STA_IF)
    if hostname is None:
      try:
        wlan.config(dhcp_hostname=name)  # older firmware
        hostname = name
      except (ValueError, OSError):
        log("WiFi: could not set hostname '%s'; use the IP address instead." % name)
    wlan.active(True)
  except OSError as e:
    wlan = None
    log("WiFi: could not start (%s)." % e)
    return
  log("WiFi: connecting to '%s'..." % wifi_cfg["ssid"])
  wifi_connect(now)


def sync_time():
  global time_synced
  if ntptime is None:
    return
  try:
    ntptime.settime()
    time_synced = True
    refresh_tz(True)
    log("Clock synced from the internet.")
  except Exception as e:
    log("Clock sync failed (%s); log times show uptime." % e)


def wifi_poll(now):
  global wifi_connected, device_ip, wifi_retry_ms
  if wlan is None:
    return
  if wlan.isconnected():
    if not wifi_connected:
      wifi_connected = True
      wifi_retry_ms = WIFI_RETRY_MIN_MS
      device_ip = wlan.ifconfig()[0]
      if not time_synced:
        sync_time()
      log("WiFi: connected. Device IP: %s  ->  http://%s/" % (device_ip, device_ip))
      if hostname:
        log("Web: also at http://%s.local/" % hostname)
      web_start()
  elif wifi_connected:
    wifi_connected = False
    log("WiFi: connection lost, reconnecting...")
    wifi_connect(now)
  elif utime.ticks_diff(now, wifi_attempt_at) >= wifi_retry_ms:
    log("WiFi: not connected yet, retrying (next retry in %s)." % fmt_duration(wifi_retry_ms))
    wifi_retry_ms = min(wifi_retry_ms * 2, WIFI_RETRY_MAX_MS)
    wifi_connect(now)


# ---------------------------------------------------------------- web

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Terrarium Climate Control</title>
<style>
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:920px;margin:0 auto;padding:16px;background:#eef2ee;color:#1b1f1b}
.title-row{display:flex;justify-content:space-between;align-items:center;gap:12px;margin:4px 0 16px}
h1{font-size:1.45em;margin:0}
.meters{display:flex;flex-direction:column;gap:5px;flex:1;min-width:150px;max-width:340px;margin:0 8px}
.meter{display:grid;grid-template-columns:2.2em minmax(48px,1fr) auto;align-items:center;gap:6px;color:#666;font-size:.72em}
.bar{height:7px;background:#e4ece4;border-radius:99px;overflow:hidden}
.bar div{height:100%;width:0;background:#2d6a4f}
.meter .nums{white-space:nowrap}
.device-time{color:#666;font-size:.85em;text-align:right;white-space:nowrap;line-height:1.35}
h2{font-size:1.05em;margin:0 0 12px}
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px}
@media(max-width:640px){.cards{grid-template-columns:1fr}.device-time{white-space:normal}}
.card{background:#fff;border-radius:14px;padding:16px 18px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.12)}
.cards .card{margin-bottom:0}
button.fan-card{display:block;width:100%;text-align:left;font:inherit;color:#1b1f1b;cursor:pointer}
button.fan-card:hover{background:#f3f8f4}
button.fan-card.stop{background:#fff;box-shadow:inset 0 0 0 2px #c0392b,0 1px 3px rgba(0,0,0,.12)}
button.fan-card:disabled{cursor:default}
.kicker{font-size:.8em;color:#666;text-transform:uppercase;letter-spacing:.04em;margin-bottom:6px}
.big{font-size:1.7em;font-weight:700;line-height:1.15}
.sub{color:#666;font-size:.9em;margin-top:6px;min-height:1.2em}
.on{color:#2d6a4f}.off{color:#777}.err{color:#c0392b}.temp{color:#b35c00}.hum{color:#1d4e89}
.muted{color:#666;font-size:.92em}
button,.btn{font-size:.95em;font-weight:600;padding:10px 16px;border:0;border-radius:10px;background:#2d6a4f;color:#fff;cursor:pointer}
button.stop{background:#c0392b}
button.secondary{background:#dde5dd;color:#1b1f1b}
button:disabled{opacity:.5}
.seg{display:inline-flex;gap:4px;background:#e8eee8;padding:3px;border-radius:10px;flex-wrap:wrap}
.seg button{background:transparent;color:#334;padding:7px 12px;box-shadow:none}
.seg button.active{background:#fff;color:#1b1f1b}
.chart-head{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.legend{display:flex;gap:14px;font-size:.85em;color:#555;flex-wrap:wrap}
.swatch{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:5px;vertical-align:middle}
.hatch{border:1px solid #9cc3ad;background:repeating-linear-gradient(45deg,#7fb08f 0 1px,#e6f1ea 1px 4px),#e6f1ea}
.hatch-x{border:1px solid #9cc3ad;background:repeating-linear-gradient(45deg,#7fb08f 0 1px,transparent 1px 4px),repeating-linear-gradient(-45deg,#7fb08f 0 1px,transparent 1px 4px),#e6f1ea}
.chart-wrap{position:relative}
canvas{width:100%;height:220px;display:block;background:#fafcfa;border-radius:10px;touch-action:manipulation}
.tip{position:absolute;background:#1b1f1b;color:#fff;padding:8px 10px;border-radius:8px;font-size:.8em;pointer-events:none;white-space:pre;z-index:2;display:none}
.note-link{color:#1d4e89;cursor:pointer;text-decoration:underline}
.chart-foot{display:flex;justify-content:space-between;align-items:baseline;gap:8px 16px;margin-top:8px;flex-wrap:wrap}
.avg-tip{margin-left:auto;color:#888;font-size:.78em;white-space:nowrap}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:10px 0}
input{width:100px;padding:8px;font-size:1em;border:1px solid #ccc;border-radius:8px}
pre{background:#111;color:#cfe8cf;padding:12px;border-radius:10px;max-height:360px;overflow:auto;font-size:12px;white-space:pre-wrap;margin:0}
.actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:8px}
.days{display:grid;grid-template-columns:repeat(7,1fr);gap:3px}
.day{height:32px;padding:0;border-radius:4px;background:#e4ece4;color:#1b1f1b;font-size:.72em}
.day.on{background:#2d6a4f;color:#fff}
.hours{display:grid;grid-template-columns:repeat(12,1fr);font-size:.72em;color:#666;margin-top:10px}
.slots{display:grid;grid-template-columns:repeat(24,1fr);gap:3px}
.slot{height:32px;padding:0;border-radius:4px;background:#e4ece4}
.slot.on{background:#2d6a4f}
table{width:100%;border-collapse:collapse;font-size:.9em;margin-top:8px}
th,td{text-align:left;padding:6px 4px;border-bottom:1px solid #e4ece4}
.pager{display:flex;gap:8px;align-items:center;margin-top:8px}
.maint-list{margin-top:14px}
.maint-list div{padding:6px 0;border-bottom:1px solid #e4ece4}
.maint-kind{font-weight:700}
.maint-date{color:#999}
.section-head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin-bottom:12px}
.section-head h2{margin:0}
.note-row{display:flex;gap:8px;flex:1;min-width:0}
.note-row input{width:auto;flex:1;min-width:0}
</style></head><body>
<div class="title-row">
  <h1>Terrarium Climate Control</h1>
  <div class="meters">
    <div class="meter"><span>Mem</span><div class="bar"><div id="memBar"></div></div><span id="memLine" class="nums"></span></div>
    <div class="meter"><span>Sto</span><div class="bar"><div id="storeBar"></div></div><span id="storeLine" class="nums"></span></div>
  </div>
  <div class="device-time"><div id="time"></div><div id="tz"></div></div>
</div>

<div class="cards">
  <button type="button" id="btn" class="card fan-card" onclick="press()" disabled>
    <div class="kicker">Fan</div>
    <div id="fanBig" class="big off">...</div>
    <div id="fanSub" class="sub"></div>
  </button>
  <div class="card">
    <div class="kicker">Temperature</div>
    <div id="tempBig" class="big temp">...</div>
    <div id="tempSub" class="sub"></div>
  </div>
  <div class="card">
    <div class="kicker">Humidity</div>
    <div id="humBig" class="big hum">...</div>
    <div id="humSub" class="sub"></div>
  </div>
</div>

<div class="card">
  <div class="chart-head">
    <h2 style="margin:0">Temperature &amp; humidity</h2>
    <div class="seg">
      <button id="d1" onclick="setDays(1)">1 day</button>
      <button id="d3" onclick="setDays(3)">3 days</button>
      <button id="d7" class="active" onclick="setDays(7)">7 days</button>
      <button id="d30" onclick="setDays(30)">30 days</button>
    </div>
  </div>
  <div class="legend"><span><span class="swatch" style="background:#b35c00"></span>Temp °C</span>
  <span><span class="swatch" style="background:#1d4e89"></span>Humidity %RH</span>
  <span id="fanKey"><span class="swatch hatch"></span>Fan</span>
  <span id="fanKeyOn"><span class="swatch hatch-x"></span>On-demand fan</span></div>
  <div class="chart-wrap">
    <canvas id="chart"></canvas>
    <div id="tip" class="tip"></div>
  </div>
  <div class="chart-foot">
    <div id="chartNote" class="muted"></div>
    <div class="avg-tip">After 1 day: &#216; 1h, 4h, then 12h &#183; Fan 7d</div>
  </div>
  <div id="samples" style="display:none">
    <table><thead><tr><th>Time</th><th>Temperature</th><th>Humidity</th></tr></thead><tbody id="sampleRows"></tbody></table>
    <div class="pager">
      <button type="button" class="secondary" id="samplePrev" onclick="sampleStep(-1)">Newer</button>
      <span id="samplePage" class="muted"></span>
      <button type="button" class="secondary" id="sampleNext" onclick="sampleStep(1)">Older</button>
    </div>
  </div>
</div>

<div class="card">
  <div class="section-head">
    <h2>Maintenance</h2>
    <div class="device-time">Mist <span id="mistAgo"></span> · Feed <span id="feedAgo"></span></div>
  </div>
  <div class="actions">
    <button type="button" onclick="maint('mist')">Mist</button>
    <button type="button" onclick="maint('feed')">Feed</button>
    <button type="button" onclick="maint('soil')">Soil</button>
    <button type="button" onclick="maint('deco')">Deco</button>
    <form class="note-row" onsubmit="event.preventDefault(); addNote();">
      <input id="noteText" type="text" maxlength="120" placeholder="Note" autocomplete="off">
      <button>Add note</button>
    </form>
  </div>
  <div id="maintMsg" class="muted"></div>
  <div id="noteMsg" class="muted"></div>
  <div id="maintList" class="maint-list muted">Loading…</div>
  <div class="pager" id="maintPager" style="display:none">
    <button type="button" class="secondary" id="maintPrev" onclick="maintStep(-1)">Newer</button>
    <span id="maintPage" class="muted"></span>
    <button type="button" class="secondary" id="maintNext" onclick="maintStep(1)">Older</button>
  </div>
</div>

<div class="card">
  <h2>Ventilation Schedule</h2>
  <div id="days" class="days"></div>
  <div id="slots"></div>
  <form onsubmit="event.preventDefault(); saveSchedule();">
    <div class="row"><label for="man">On-demand run (minutes)</label><input id="man" type="number" min="1" required></div>
    <button>Save schedule</button> <span id="schedMsg" class="muted"></span>
  </form>
</div>

<div class="card">
  <h2>Advanced</h2>
  <form onsubmit="event.preventDefault(); saveAdvanced();">
    <div class="row"><label for="sint">Temperature readout every (minutes)</label><input id="sint" type="number" min="5" max="60" required></div>
    <div class="row"><label for="lkeep">Log lines to keep</label><input id="lkeep" type="number" min="20" max="250" required></div>
    <div class="actions">
      <button>Save advanced</button>
      <button type="button" class="secondary" onclick="purgeLog()">Purge log</button>
      <button type="button" class="secondary" onclick="purgeHistory()">Purge climate history</button>
      <button type="button" class="secondary" onclick="purgeMaint()">Purge maintenance</button>
      <span id="advMsg" class="muted"></span>
    </div>
  </form>
</div>

<div class="card">
  <h2>Log (newest first)</h2>
  <pre id="log">Loading…</pre>
</div>

<script>
const $ = id => document.getElementById(id);
const NL = String.fromCharCode(10);
let formLoaded = false, chartDays = 7, chartPoints = [], chartFan = [], chartGeom = null;
let patterns = {}, tipPinned = false, samplesOpen = false, samplePage = 0, sampleTotal = 0;
let maintEvents = [], maintPage = 0;
const PAGE_SIZE = 50, MAINT_PAGE = 5;
function hatchPattern(ctx, cross) {
  const key = cross ? 'x' : 's';
  if (patterns[key]) return patterns[key];
  const p = document.createElement('canvas'), n = 8;
  p.width = p.height = n;
  const g = p.getContext('2d');
  g.fillStyle = 'rgba(45,106,79,0.08)'; g.fillRect(0, 0, n, n);
  g.strokeStyle = 'rgba(45,106,79,0.35)'; g.lineWidth = 1;
  g.beginPath();
  g.moveTo(0, n); g.lineTo(n, 0);
  if (cross) { g.moveTo(0, 0); g.lineTo(n, n); }
  g.stroke();
  return patterns[key] = ctx.createPattern(p, 'repeat');
}
function fmt(s) {
  s = Math.max(0, s | 0);
  const h = s / 3600 | 0, m = (s % 3600) / 60 | 0, x = s % 60;
  return (h ? h + 'h ' : '') + (h || m ? m + 'm ' : '') + x + 's';
}
function ago(ts) {
  if (ts === null || ts === undefined) return 'never';
  const s = Math.max(0, (Date.now() / 1000 - ts) | 0);
  if (s < 60) return 'just now';
  if (s < 3600) return (s / 60 | 0) + ' min ago';
  if (s < 86400) {
    const h = s / 3600 | 0;
    return h + (h === 1 ? ' hr ago' : ' hrs ago');
  }
  const d = s / 86400 | 0;
  return d + (d === 1 ? ' day ago' : ' days ago');
}
function clock(ts) {
  const df = {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'};
  return new Date(ts * 1000).toLocaleString([], df);
}
function mark(p) { return p[3] > 0 ? '\u00d8 ' : ''; }
async function req(url, opts) {
  const r = await fetch(url, opts);
  const t = await r.text();
  if (!r.ok) throw new Error(t);
  return t;
}
function setDays(d) {
  chartDays = d;
  ['d1','d3','d7','d30'].forEach(id => $(id).classList.toggle('active', id === 'd' + d));
  samplePage = 0;
  loadHistory();
  if (samplesOpen) loadSamples();
}
function axisTicks(x0, x1, step) {
  const out = [];
  const d = new Date((x0 + 1) * 1000);
  const push = () => {
    let guard = 0;
    while (d.getTime() / 1000 < x1 && guard++ < 900) {
      const t = d.getTime() / 1000;
      const prev = d.getTime();
      if (t > x0 && t < x1) out.push(t);
      if (step >= 86400) d.setDate(d.getDate() + (step / 86400 | 0));
      else d.setHours(d.getHours() + (step / 3600 | 0));
      if (d.getTime() <= prev) break;
    }
  };
  if (step >= 86400) {
    d.setHours(0, 0, 0, 0);
    if (d.getTime() / 1000 <= x0) d.setDate(d.getDate() + (step / 86400 | 0));
    push();
  } else {
    const hours = step / 3600 | 0;
    d.setMinutes(0, 0, 0);
    const h = d.getHours();
    const add = (hours - (h % hours)) % hours;
    if (add === 0 && d.getTime() / 1000 <= x0) d.setHours(h + hours);
    else if (add) d.setHours(h + add);
    push();
  }
  return out;
}
function axisText(ts, step, span) {
  const d = new Date(ts * 1000);
  if (step >= 86400) return d.toLocaleString([], {month:'short', day:'numeric'});
  const time = d.toLocaleString([], {hour:'2-digit', minute:'2-digit'});
  if (span > 36 * 3600) return d.toLocaleString([], {month:'short', day:'numeric'}) + ' ' + time;
  return time;
}
function drawChart() {
  const c = $('chart'), ctx = c.getContext('2d');
  const dpr = window.devicePixelRatio || 1, W = c.clientWidth, H = c.clientHeight;
  c.width = Math.round(W * dpr); c.height = Math.round(H * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const pad = {l:40, r:40, t:16, b:28};
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = '#fafcfa'; ctx.fillRect(0, 0, W, H);
  const pts = chartPoints;
  chartGeom = null;
  if (!pts.length) {
    ctx.fillStyle = '#888'; ctx.font = '14px system-ui';
    ctx.fillText('No history yet. Samples are stored once the clock is synced.', pad.l, H / 2);
    return;
  }
  let tMin = Infinity, tMax = -Infinity, hMin = Infinity, hMax = -Infinity;
  pts.forEach(p => { tMin = Math.min(tMin, p[1]); tMax = Math.max(tMax, p[1]); hMin = Math.min(hMin, p[2]); hMax = Math.max(hMax, p[2]); });
  if (tMax === tMin) { tMin -= 1; tMax += 1; }
  if (hMax === hMin) { hMin = Math.max(0, hMin - 5); hMax = Math.min(100, hMax + 5); }
  tMin = Math.floor(tMin) - 1; tMax = Math.ceil(tMax) + 1;
  hMin = Math.max(0, Math.floor(hMin) - 5); hMax = Math.min(100, Math.ceil(hMax) + 5);
  const x0 = pts[0][0], x1 = Math.max(pts[pts.length - 1][0], x0 + 1);
  const x = ts => pad.l + (ts - x0) / Math.max(1, x1 - x0) * (W - pad.l - pad.r);
  const yT = v => pad.t + (1 - (v - tMin) / (tMax - tMin)) * (H - pad.t - pad.b);
  const yH = v => pad.t + (1 - (v - hMin) / (hMax - hMin)) * (H - pad.t - pad.b);
  chartGeom = {x0, x1, pad, W, H, x, yT, yH, pts};
  chartFan.forEach(r => {
    if (r[1] < x0 || r[0] > x1) return;
    const a = x(Math.max(r[0], x0)), w = x(Math.min(r[1], x1)) - a;
    const cross = r[2] === 1;
    ctx.fillStyle = w >= 6 ? hatchPattern(ctx, cross) : (cross ? 'rgba(45,106,79,0.45)' : 'rgba(45,106,79,0.28)');
    ctx.fillRect(a, pad.t, Math.max(1, w), H - pad.t - pad.b);
  });
  ctx.strokeStyle = '#e0e6e0'; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const yy = pad.t + i * (H - pad.t - pad.b) / 4;
    ctx.beginPath(); ctx.moveTo(pad.l, yy); ctx.lineTo(W - pad.r, yy); ctx.stroke();
  }
  ctx.font = '11px system-ui'; ctx.fillStyle = '#888';
  ctx.fillText(tMax.toFixed(0) + '\u00b0', 4, pad.t + 4);
  ctx.fillText(tMin.toFixed(0) + '\u00b0', 4, H - pad.b);
  ctx.textAlign = 'right';
  ctx.fillText(hMax.toFixed(0) + '%', W - 4, pad.t + 4);
  ctx.fillText(hMin.toFixed(0) + '%', W - 4, H - pad.b);
  ctx.textAlign = 'left';
  function line(color, yfn, idx) {
    ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath();
    pts.forEach((p, i) => { const X = x(p[0]), Y = yfn(p[idx]); i ? ctx.lineTo(X, Y) : ctx.moveTo(X, Y); });
    ctx.stroke();
  }
  line('#1d4e89', yH, 2);
  line('#b35c00', yT, 1);
  ctx.fillStyle = '#666';
  const startLabel = clock(pts[0][0]), endLabel = clock(pts[pts.length - 1][0]);
  const gap = 16;
  const leftLimit = pad.l + ctx.measureText(startLabel).width + gap;
  const rightLimit = W - pad.r - ctx.measureText(endLabel).width - gap;
  const span = x1 - x0;
  const steps = [3600, 7200, 10800, 21600, 43200, 86400, 172800, 432000, 604800];
  let drawn = [];
  for (let s = 0; s < steps.length && !drawn.length; s++) {
    const step = steps[s];
    if (step >= span * 0.9) continue;
    const placed = [];
    let edge = leftLimit - gap, blocked = false;
    axisTicks(x0, x1, step).forEach(ts => {
      if (blocked) return;
      const text = axisText(ts, step, span);
      const w = ctx.measureText(text).width, cx = x(ts);
      const left = cx - w / 2, right = cx + w / 2;
      if (left < leftLimit || right > rightLimit) return;
      if (left < edge + gap) { blocked = true; return; }
      placed.push({text: text, cx: cx});
      edge = right;
    });
    if (!blocked && placed.length) drawn = placed;
  }
  ctx.fillText(startLabel, pad.l, H - 8);
  ctx.textAlign = 'center';
  drawn.forEach(lab => ctx.fillText(lab.text, lab.cx, H - 8));
  ctx.textAlign = 'right';
  ctx.fillText(endLabel, W - pad.r, H - 8);
  ctx.textAlign = 'left';
}
function nearestPoint(clientX) {
  if (!chartGeom || !chartGeom.pts.length) return null;
  const rect = $('chart').getBoundingClientRect();
  const px = clientX - rect.left;
  const g = chartGeom;
  let best = g.pts[0], bestD = Infinity;
  g.pts.forEach(p => {
    const d = Math.abs(g.x(p[0]) - px);
    if (d < bestD) { best = p; bestD = d; }
  });
  return best;
}
function showTip(e, pin) {
  const p = nearestPoint(e.clientX);
  const tip = $('tip');
  if (!p) { tip.style.display = 'none'; return; }
  const g = chartGeom;
  tip.textContent = clock(p[0]) + NL + mark(p) + p[1].toFixed(1) + ' \u00b0C' + NL + mark(p) + p[2].toFixed(1) + '%';
  const x = g.x(p[0]), y = Math.min(g.yT(p[1]), g.yH(p[2]));
  tip.style.left = Math.min(x + 8, g.W - 140) + 'px';
  tip.style.top = Math.max(4, y - 56) + 'px';
  tip.style.display = 'block';
  if (pin) tipPinned = true;
}
function hideTip() {
  tipPinned = false;
  $('tip').style.display = 'none';
}
async function loadHistory() {
  try {
    const h = JSON.parse(await req('/api/history?days=' + chartDays));
    chartPoints = h.points || [];
    const showFan = chartDays <= 7;
    chartFan = showFan && h.fan ? h.fan : [];
    $('fanKey').style.display = showFan ? '' : 'none';
    $('fanKeyOn').style.display = showFan ? '' : 'none';
    const n = h.count || 0;
    const note = $('chartNote');
    note.textContent = '';
    if (n) {
      const a = document.createElement('a');
      a.href = '#samples';
      a.className = 'note-link';
      a.textContent = n + (n === 1 ? ' sample' : ' samples');
      a.onclick = ev => { ev.preventDefault(); toggleSamples(); };
      note.appendChild(a);
    } else {
      note.textContent = 'No samples in this window yet. History starts after the clock syncs from the internet.';
    }
    drawChart();
  } catch (e) {
    $('chartNote').textContent = 'Could not load history.';
  }
}
function toggleSamples() {
  samplesOpen = !samplesOpen;
  $('samples').style.display = samplesOpen ? '' : 'none';
  if (samplesOpen) { samplePage = 0; loadSamples(); }
}
async function loadSamples() {
  try {
    const h = JSON.parse(await req('/api/history?days=' + chartDays + '&page=' + samplePage));
    sampleTotal = h.total || 0;
    const body = $('sampleRows');
    body.textContent = '';
    (h.samples || []).forEach(p => {
      const tr = document.createElement('tr');
      [clock(p[0]), mark(p) + Number(p[1]).toFixed(1) + ' \u00b0C', mark(p) + Number(p[2]).toFixed(1) + '%'].forEach(text => {
        const td = document.createElement('td');
        td.textContent = text;
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    const pages = Math.max(1, Math.ceil(sampleTotal / PAGE_SIZE));
    $('samplePage').textContent = 'Page ' + (samplePage + 1) + ' of ' + pages;
    $('samplePrev').disabled = samplePage <= 0;
    $('sampleNext').disabled = (samplePage + 1) * PAGE_SIZE >= sampleTotal;
  } catch (e) {
    $('sampleRows').textContent = '';
  }
}
function sampleStep(d) {
  samplePage = Math.max(0, samplePage + d);
  loadSamples();
}
function fmtBytes(n) {
  if (n === null || n === undefined) return '\u2014';
  if (n < 1024) return n + ' B';
  if (n < 1048576) return Math.round(n / 1024) + ' KB';
  const mb = n / 1048576;
  return (mb >= 10 ? Math.round(mb) : mb.toFixed(1)) + ' MB';
}
function paintMeter(barId, textId, used, free) {
  const known = used !== null && used !== undefined && free !== null && free !== undefined;
  const total = known ? used + free : 0;
  $(barId).style.width = total > 0 ? Math.min(100, used / total * 100) + '%' : '0';
  $(textId).textContent = known ? fmtBytes(used) + ' / ' + fmtBytes(total) : '\u2014';
}
function diffText(avg, cur, unit) {
  const d = cur - avg;
  const sign = d >= 0 ? '+' : '';
  return '\u00d8 ' + avg.toFixed(1) + unit + ' \u00b7 ' + sign + d.toFixed(1);
}
function fanLine(s) {
  if (s.fan_on) return s.mode + ' \u00b7 ' + fmt(s.remaining_s) + ' left';
  if (!s.time_synced) return 'Waiting for clock';
  if (s.next_run_s !== null && s.next_run_s !== undefined) return 'Next scheduled run in ' + fmt(s.next_run_s);
  return 'Schedule off';
}
function paintDays(str) {
  const box = $('days');
  box.textContent = '';
  const names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  names.forEach((name, i) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'day' + (!str || str.charAt(i) !== '0' ? ' on' : '');
    b.textContent = name;
    b.onclick = () => b.classList.toggle('on');
    box.appendChild(b);
  });
}
function dayString() {
  let s = '';
  document.querySelectorAll('#days .day').forEach(b => { s += b.classList.contains('on') ? '1' : '0'; });
  return s;
}
function paintSlots(str) {
  const box = $('slots');
  box.textContent = '';
  for (let row = 0; row < 2; row++) {
    const labels = document.createElement('div');
    labels.className = 'hours';
    const grid = document.createElement('div');
    grid.className = 'slots';
    for (let hour = 0; hour < 12; hour++) {
      const lab = document.createElement('span');
      lab.textContent = String(row * 12 + hour).padStart(2, '0');
      labels.appendChild(lab);
      for (let half = 0; half < 2; half++) {
        const i = (row * 12 + hour) * 2 + half;
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'slot' + (str && str.charAt(i) === '1' ? ' on' : '');
        b.setAttribute('aria-label', String(row * 12 + hour).padStart(2, '0') + (half ? ':30' : ':00'));
        b.onclick = () => b.classList.toggle('on');
        grid.appendChild(b);
      }
    }
    box.appendChild(labels);
    box.appendChild(grid);
  }
}
function slotString() {
  let s = '';
  document.querySelectorAll('#slots .slot').forEach(b => { s += b.classList.contains('on') ? '1' : '0'; });
  return s;
}
async function refresh() {
  try {
    const s = JSON.parse(await req('/api/status'));
    $('time').textContent = s.time || '';
    $('tz').textContent = s.timezone || '';
    $('fanBig').textContent = s.fan_on ? 'ON' : 'OFF';
    $('fanBig').className = 'big ' + (s.fan_on ? 'on' : 'off');
    $('fanSub').textContent = fanLine(s);
    if (s.temp_c === null || s.temp_c === undefined) {
      $('tempBig').textContent = '\u2014';
      $('tempSub').textContent = s.sensor_error ? 'unavailable' : 'waiting\u2026';
      $('humBig').textContent = '\u2014';
      $('humSub').textContent = s.sensor_error ? 'unavailable' : 'waiting\u2026';
    } else {
      $('tempBig').textContent = s.temp_c.toFixed(1) + ' \u00b0C';
      $('humBig').textContent = s.humidity.toFixed(1) + '%';
      if (s.temp_avg === null || s.temp_avg === undefined) {
        $('tempSub').textContent = s.sensor_ok ? 'air temperature' : 'stale reading';
        $('humSub').textContent = s.sensor_ok ? 'relative humidity' : 'stale reading';
      } else {
        $('tempSub').textContent = diffText(s.temp_avg, s.temp_c, ' \u00b0C');
        $('humSub').textContent = diffText(s.hum_avg, s.humidity, '%');
      }
    }
    paintMeter('memBar', 'memLine', s.mem_used, s.mem_free);
    paintMeter('storeBar', 'storeLine', s.storage_used, s.storage_free);
    $('mistAgo').textContent = ago(s.last_mist);
    $('feedAgo').textContent = ago(s.last_feed);
    $('btn').setAttribute('aria-label', s.fan_on ? 'Stop fan' : 'Start for ' + s.manual_min + ' min');
    $('btn').className = 'card fan-card' + (s.fan_on ? ' stop' : '');
    $('btn').disabled = false;
    if (!formLoaded) {
      $('man').value = s.manual_min;
      $('sint').value = s.sensor_interval_min;
      $('lkeep').value = s.log_keep;
      paintDays(s.weekdays || '');
      paintSlots(s.segments || '');
      formLoaded = true;
    }
    const logs = await req('/api/logs');
    $('log').textContent = logs.trim() ? logs.trim().split(NL).reverse().join(NL) : 'No log entries yet.';
    const m = JSON.parse(await req('/api/maintenance'));
    maintEvents = m.events || [];
    renderMaint();
  } catch (e) {
    $('fanBig').textContent = '\u2014';
    $('fanBig').className = 'big err';
    $('fanSub').textContent = 'Device unreachable';
    $('btn').disabled = true;
  }
}
async function press() {
  $('btn').disabled = true;
  try { await req('/api/button', {method:'POST'}); } catch (e) {}
  refresh();
}
function maintRow(ev) {
  const names = {mist:'Mist', feed:'Feed', soil:'Soil', deco:'Deco', note:'Note'};
  const row = document.createElement('div');
  const kind = document.createElement('span');
  kind.className = 'maint-kind';
  kind.textContent = names[ev[1]] || ev[1];
  row.appendChild(kind);
  if (ev[1] === 'note' && ev[2]) row.appendChild(document.createTextNode(': ' + ev[2]));
  const date = document.createElement('span');
  date.className = 'maint-date';
  date.textContent = clock(ev[0]);
  row.appendChild(document.createTextNode(' \u00b7 ' + ago(ev[0]) + ' \u00b7 '));
  row.appendChild(date);
  return row;
}
function renderMaint() {
  const list = $('maintList');
  const pager = $('maintPager');
  list.textContent = '';
  const n = maintEvents.length;
  if (!n) {
    list.className = 'maint-list muted';
    list.textContent = 'Nothing recorded yet.';
    pager.style.display = 'none';
    return;
  }
  const pages = Math.ceil(n / MAINT_PAGE);
  if (maintPage >= pages) maintPage = pages - 1;
  if (maintPage < 0) maintPage = 0;
  list.className = 'maint-list';
  maintEvents.slice(maintPage * MAINT_PAGE, maintPage * MAINT_PAGE + MAINT_PAGE).forEach(ev => {
    list.appendChild(maintRow(ev));
  });
  pager.style.display = pages > 1 ? 'flex' : 'none';
  $('maintPage').textContent = 'Page ' + (maintPage + 1) + ' of ' + pages;
  $('maintPrev').disabled = maintPage <= 0;
  $('maintNext').disabled = maintPage + 1 >= pages;
}
function maintStep(d) {
  maintPage = Math.max(0, maintPage + d);
  renderMaint();
}
async function maint(kind) {
  $('maintMsg').textContent = '';
  $('maintMsg').className = 'muted';
  try {
    await req('/api/maintenance', {method:'POST', body:JSON.stringify({kind:kind})});
    maintPage = 0;
    refresh();
  } catch (e) {
    $('maintMsg').textContent = e.message;
    $('maintMsg').className = 'err';
  }
}
async function addNote() {
  $('noteMsg').textContent = '';
  $('noteMsg').className = 'muted';
  try {
    await req('/api/maintenance', {method:'POST', body:JSON.stringify({kind:'note', text:$('noteText').value})});
    $('noteText').value = '';
    maintPage = 0;
    refresh();
  } catch (e) {
    $('noteMsg').textContent = e.message;
    $('noteMsg').className = 'err';
  }
}
async function saveSchedule() {
  $('schedMsg').textContent = 'Saving\u2026'; $('schedMsg').className = 'muted';
  try {
    await req('/api/schedule', {method:'POST', body:JSON.stringify({
      segments: slotString(), weekdays: dayString(), manual_min: +$('man').value})});
    $('schedMsg').textContent = 'Saved.';
    refresh();
  } catch (e) { $('schedMsg').textContent = e.message; $('schedMsg').className = 'err'; }
}
async function saveAdvanced() {
  $('advMsg').textContent = 'Saving\u2026'; $('advMsg').className = 'muted';
  try {
    await req('/api/settings', {method:'POST', body:JSON.stringify({
      sensor_interval_min: +$('sint').value, log_keep: +$('lkeep').value})});
    $('advMsg').textContent = 'Saved.';
    refresh();
    loadHistory();
  } catch (e) { $('advMsg').textContent = e.message; $('advMsg').className = 'err'; }
}
async function purgeHistory() {
  if (!confirm('Delete all climate and fan history on the device? The chart will be empty.')) return;
  $('advMsg').textContent = 'Purging\u2026'; $('advMsg').className = 'muted';
  try {
    await req('/api/purge-history', {method:'POST'});
    $('advMsg').textContent = 'Climate history purged.';
    refresh();
    loadHistory();
  } catch (e) { $('advMsg').textContent = e.message; $('advMsg').className = 'err'; }
}
async function purgeLog() {
  if (!confirm('Delete all log lines on the device?')) return;
  $('advMsg').textContent = 'Purging\u2026'; $('advMsg').className = 'muted';
  try {
    await req('/api/purge-log', {method:'POST'});
    $('advMsg').textContent = 'Log purged.';
    refresh();
  } catch (e) { $('advMsg').textContent = e.message; $('advMsg').className = 'err'; }
}
async function purgeMaint() {
  if (!confirm('Delete all maintenance history on the device?')) return;
  $('advMsg').textContent = 'Purging\u2026'; $('advMsg').className = 'muted';
  try {
    await req('/api/purge-maintenance', {method:'POST'});
    $('advMsg').textContent = 'Maintenance purged.';
    refresh();
  } catch (e) { $('advMsg').textContent = e.message; $('advMsg').className = 'err'; }
}
const chartEl = $('chart');
chartEl.addEventListener('pointermove', e => { if (e.pointerType === 'mouse' && !tipPinned) showTip(e, false); });
chartEl.addEventListener('pointerdown', e => showTip(e, e.pointerType !== 'mouse'));
chartEl.addEventListener('pointerleave', e => { if (e.pointerType === 'mouse' && !tipPinned) hideTip(); });
addEventListener('pointerdown', e => {
  if (tipPinned && e.target !== chartEl) hideTip();
});
refresh();
loadHistory();
setInterval(refresh, 3000);
setInterval(loadHistory, 60000);
addEventListener('resize', drawChart);
</script>
</body></html>
"""


def web_start():
  global server
  if server is not None:
    return
  try:
    addr = socket.getaddrinfo("0.0.0.0", WEB_PORT)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(2)
    s.setblocking(False)
    server = s
  except OSError as e:
    log("Web: could not start server: %s" % e)


def send(conn, status_line, content_type, body):
  if isinstance(body, str):
    body = body.encode()
  header = (
    "HTTP/1.1 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
    "Cache-Control: no-store\r\nConnection: close\r\n\r\n"
  ) % (status_line, content_type, len(body))
  conn.sendall(header.encode())
  conn.sendall(body)


def read_request(conn):
  data = b""
  while data.find(b"\r\n\r\n") < 0 and len(data) < 4096:
    chunk = conn.recv(512)
    if not chunk:
      break
    data += chunk
  split = data.find(b"\r\n\r\n")
  if split < 0:
    raise ValueError("Bad request")
  head = data[:split].decode()
  body = data[split + 4:]
  lines = head.split("\r\n")
  parts = lines[0].split(" ")
  if len(parts) < 2:
    raise ValueError("Bad request")
  raw_path = parts[1]
  if "?" in raw_path:
    path, qs = raw_path.split("?", 1)
  else:
    path, qs = raw_path, ""
  length = 0
  for line in lines[1:]:
    if line.lower().startswith("content-length:"):
      length = int(line.split(":", 1)[1].strip())
  while len(body) < min(length, 2048):
    chunk = conn.recv(512)
    if not chunk:
      break
    body += chunk
  return parts[0], path, qs, body


def query_int(qs, key, default):
  for part in qs.split("&"):
    if part.startswith(key + "="):
      try:
        return int(part.split("=", 1)[1])
      except ValueError:
        return default
  return default


def handle(conn, method, path, qs, body, now):
  if method == "GET" and path == "/":
    send(conn, "200 OK", "text/html; charset=utf-8", PAGE)
  elif method == "GET" and path == "/api/status":
    send(conn, "200 OK", "application/json", json.dumps(status(now)))
  elif method == "GET" and path == "/api/logs":
    send(conn, "200 OK", "text/plain; charset=utf-8", "\n".join(log_lines))
  elif method == "GET" and path == "/api/history":
    days = query_int(qs, "days", 7)
    page = query_int(qs, "page", -1)
    if "page=" not in qs:
      page = None
    send(conn, "200 OK", "application/json", json.dumps(history_json(days, page)))
  elif method == "GET" and path == "/api/maintenance":
    send(conn, "200 OK", "application/json", json.dumps(maintenance_json()))
  elif method == "POST" and path == "/api/button":
    toggle_fan(now, "Web")
    send(conn, "200 OK", "application/json", json.dumps(status(now)))
  elif method == "POST" and path == "/api/schedule":
    try:
      data = json.loads(body.decode())
      save_schedule(str(data["segments"]), int(data["manual_min"]), str(data["weekdays"]))
    except (ValueError, KeyError, TypeError) as e:
      send(conn, "400 Bad Request", "text/plain", str(e) or "Invalid schedule.")
      return
    send(conn, "200 OK", "application/json", json.dumps(status(now)))
  elif method == "POST" and path == "/api/settings":
    try:
      data = json.loads(body.decode())
      save_settings(int(data["sensor_interval_min"]), int(data["log_keep"]))
    except (ValueError, KeyError, TypeError) as e:
      send(conn, "400 Bad Request", "text/plain", str(e) or "Invalid settings.")
      return
    send(conn, "200 OK", "application/json", json.dumps(status(now)))
  elif method == "POST" and path == "/api/maintenance":
    try:
      data = json.loads(body.decode())
      text = data.get("text", "")
      record_maintenance(str(data["kind"]), "" if text is None else str(text))
    except (ValueError, KeyError, TypeError) as e:
      send(conn, "400 Bad Request", "text/plain", str(e) or "Invalid maintenance.")
      return
    send(conn, "200 OK", "application/json", json.dumps(maintenance_json()))
  elif method == "POST" and path == "/api/purge-log":
    purge_log()
    send(conn, "200 OK", "application/json", json.dumps(status(now)))
  elif method == "POST" and path == "/api/purge-history":
    purge_history()
    send(conn, "200 OK", "application/json", json.dumps(status(now)))
  elif method == "POST" and path == "/api/purge-maintenance":
    purge_maintenance()
    send(conn, "200 OK", "application/json", json.dumps(status(now)))
  else:
    send(conn, "404 Not Found", "text/plain", "Not found")


def web_poll(now):
  if server is None:
    return
  try:
    conn, _ = server.accept()
  except OSError:
    return
  try:
    conn.settimeout(1)
    method, path, qs, body = read_request(conn)
    handle(conn, method, path, qs, body, now)
  except Exception as e:
    try:
      send(conn, "500 Internal Server Error", "text/plain", str(e))
    except Exception:
      pass
  finally:
    conn.close()


# ---------------------------------------------------------------- boot

RESET_CAUSE_NAMES = {}
for _name in ("PWRON_RESET", "HARD_RESET", "WDT_RESET", "DEEPSLEEP_RESET", "SOFT_RESET"):
  if hasattr(machine, _name):
    RESET_CAUSE_NAMES[getattr(machine, _name)] = _name

load_settings()
load_log()
load_schedule()

_cause = machine.reset_cause()
log("Terrarium Climate Controller starting (ESP32-C3), reset cause: %s" % RESET_CAUSE_NAMES.get(_cause, _cause))
log("Pins: fan=GPIO%d  button=GPIO%d  sensor=GPIO%d" % (FAN_GPIO, BTN_GPIO, DHT_GPIO))
log("Schedule: %s" % schedule_text())
log("Settings: sensor every %d min, keep %d log lines." % (sensor_interval_ms // MINUTE, log_keep))

# Safe mode: holding the button during power-up/reset skips the controller and
# leaves the board at the REPL, so Thonny/mpremote can always get back in.
if btn_pin.value() == 0:
  log("SAFE MODE: button held at boot, controller NOT started.")
  print("--> Board is idle at the REPL; edit files, then reset to run normally.")
  print("--> (Not holding it? The button is miswired: use the DIAGONAL legs.)")
  sys.exit()

sensor_start()
# WiFi needs one large free block, so the history files stay unloaded until
# after the join. Reading flash while it is associating stalls the radio on
# this single-core chip, and the join then waits until the next retry.
wifi_start(utime.ticks_ms())

print("Waiting %s before enabling the fan..." % fmt_duration(STARTUP_DELAY_MS))
utime.sleep_ms(STARTUP_DELAY_MS)
load_history()
load_fan_runs()
load_maintenance()
log_memory()
mem_logged_at = utime.ticks_ms()

log("Controller ready.")


# ---------------------------------------------------------------- main loop

try:
  while True:
    now = utime.ticks_ms()

    refresh_tz()
    wifi_poll(now)
    sensor_poll(now)
    if utime.ticks_diff(now, mem_logged_at) >= MEM_LOG_MS:
      log_memory()
      mem_logged_at = now

    # 1. Check Manual Run Expiration
    if manual_running and utime.ticks_diff(now, manual_start) >= manual_ms:
      manual_running = False
      log("On-demand %s run completed." % fmt_duration(manual_ms))

    # 2. Handle Button Press (edge-triggered, debounced, EMI-blanked)
    blanked = btn_blank_start is not None and utime.ticks_diff(now, btn_blank_start) < EMI_BLANK_MS
    if not blanked:
      btn_blank_start = None
    if button_pressed(now) and not blanked:
      toggle_fan(now, "Button")

    # 3. Handle one web request, if any
    web_poll(now)

    # 4. Schedule state + logging of scheduled starts/ends
    sched_running, in_slot, _ = schedule_state(now)
    if sched_running and not sched_was_running:
      log("Scheduled run started.")
    elif sched_was_running and not sched_running and not in_slot:
      log("Scheduled run finished.")
    sched_was_running = sched_running

    # 5. Drive Fan Output. Schedule wins, so an on-demand run splits when a slot starts.
    fan_active = sched_running or manual_running
    if fan_active and not fan_was_active:
      # Also blank when a scheduled run starts the fan for the same reason
      btn_blank_start = now
    fan_was_active = fan_active
    fan_pin.value(1 if fan_active else 0)
    track_fan_run(fan_active, manual_running and not sched_running)

    utime.sleep_ms(LOOP_MS)
finally:
  fan_pin.value(0)
  track_fan_run(False, False)
  log("Program stopped: fan OFF.")
