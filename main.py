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
MAX_MANUAL_MIN = 1440
DEFAULT_SENSOR_INTERVAL_MIN = 15  # AM2302 is read on this interval; minimum is 5
MIN_SENSOR_INTERVAL_MIN = 5
MAX_SENSOR_INTERVAL_MIN = 60
SENSOR_RETRY_MS = 30 * SECOND  # after a failed read; the AM2302 needs at least 2 s
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
# Maintenance buttons. Note is always available and is not part of this list.
DEFAULT_CATEGORIES = ("Mist", "Feed", "Soil", "Deco")
MAX_CATEGORIES = 8
MAX_CATEGORY_LEN = 24
NOTE_MAX = 120
# Rows written before categories were names, shown as the default labels.
LEGACY_KINDS = {"mist": "Mist", "feed": "Feed", "soil": "Soil", "deco": "Deco"}

DEBOUNCE_MS = 50  # button must be held this long to count
# After the fan turns ON, ignore the button briefly. Fan motors inject noise into
# nearby GPIO lines; that noise can look like a second press and cancel a start.
EMI_BLANK_MS = 400
# Wait after boot before the fan can switch on, so USB (and Thonny) can connect
# before the fan's startup current dips the shared USB 5V.
STARTUP_DELAY_MS = 3 * SECOND
LOOP_MS = 10
ERROR_RESET_COUNT = 20  # consecutive loop errors before a reset
NTP_RETRY_MS = MINUTE
NTP_RESYNC_MS = 24 * 60 * MINUTE

# Files on the board
WIFI_CFG = "wifi.cfg"
SCHEDULE_CFG = "schedule.cfg"
SETTINGS_CFG = "settings.cfg"
LOG_FILE = "events.log"
HISTORY_FILE = "climate.hist"
FAN_HISTORY_FILE = "fan.hist"
MAINT_FILE = "maintenance.hist"
PAGE_FILE = "index.html"
PAGE_GZ = "index.html.gz"

WEB_PORT = 80
DEFAULT_HOSTNAME = "terrarium"  # reachable as http://terrarium.local/; override with hostname= in wifi.cfg
WIFI_RETRY_MIN_MS = 30 * SECOND
WIFI_RETRY_MAX_MS = 10 * MINUTE
SEND_CHUNK = 1024

# State Variables
manual_ms = DEFAULT_MANUAL_MIN * MINUTE
sensor_interval_ms = DEFAULT_SENSOR_INTERVAL_MIN * MINUTE
log_keep = DEFAULT_LOG_KEEP
maint_categories = list(DEFAULT_CATEGORIES)
settings_timezone = ""  # empty: wifi.cfg timezone is used
settings_tz_rejected = ""

manual_start = 0
manual_running = False
suppressed_until = 0  # device UTC seconds; schedule stays off until then
btn_blank_start = None
fan_was_active = False
# 48 half-hours from local midnight. 1 = fan scheduled on. Default 08:00-14:00.
segments = [0] * N_SLOTS
for _i in range(16, 28):
  segments[_i] = 1
# Monday is 0. Default every day, so an older schedule file keeps running all week.
weekdays = [1] * 7

# Button, set from the pin interrupt. btn_down_at is -1 while released.
btn_down_at = -1
btn_press = False

# WiFi / web state
wlan = None
wifi_cfg = {}
wifi_connected = False
wifi_attempt_at = 0
wifi_retry_ms = WIFI_RETRY_MIN_MS
device_ip = None
hostname = None
server = None
send_buf = bytearray(SEND_CHUNK)
time_synced = False
ntp_at = None  # ticks of the last sync attempt
ntp_failed = False
tz_offset_s = 0
tz_name = "Europe/London"
tz_fixed = False  # True when an old utc_offset_hours is in use (no summer time)
tz_checked_at = 0

# The page refetches the log and maintenance only when these change.
try:
  BOOT_ID = int.from_bytes(os.urandom(3), "big")
except (AttributeError, OSError):
  BOOT_ID = utime.ticks_cpu()
log_rev = 0
maint_rev = 0

# Log state
log_lines = []
log_file_lines = 0

# Climate sensor (AM2302 / DHT22)
dht = None
temp_c = None
humidity = None
sensor_ok = False
sensor_error = None
sensor_due_at = None
sensor_at = None  # device UTC seconds of the last good reading
sensor_read_ticks = None  # ticks of the last measure, so a second read can wait 2 s

# Climate history: (device UTC seconds, temp tenths, humidity tenths, span seconds,
# readings averaged). span 0 = raw reading. Tenths keep these small ints, not floats.
history = []
history_stored_at = 0
# Fan runs: (start, end, manual) device UTC seconds. manual 1 = on-demand.
fan_runs = []
fan_run_start = None
fan_run_manual = 0
fan_file_lines = 0
# Maintenance: (device UTC seconds, kind, note text), oldest first
maintenance = []
mem_logged_at = 0


# ---------------------------------------------------------------- helpers

def fmt_duration(ms):
  # Format a millisecond duration for logs, e.g. 300000 -> '5m', 90000 -> '1m 30s'.
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


def read_lines(path):
  # Yield non-empty lines without the line ending. A missing file yields nothing.
  try:
    f = open(path)
  except OSError:
    return
  with f:
    for line in f:
      line = line.rstrip("\r\n")
      if line:
        yield line


def write_lines(path, lines):
  # Replace the file with these lines. Returns False if the write failed.
  try:
    with open(path, "w") as f:
      for line in lines:
        f.write(line)
        f.write("\n")
    return True
  except OSError:
    return False


def append_line(path, line):
  try:
    with open(path, "a") as f:
      f.write(line)
      f.write("\n")
    return True
  except OSError:
    return False


def read_cfg(path):
  # Read a simple key=value file. Blank lines and lines starting with # are ignored.
  cfg = {}
  for line in read_lines(path):
    line = line.strip()
    if line.startswith("#") or "=" not in line:
      continue
    key, value = line.split("=", 1)
    cfg[key.strip()] = value.strip()
  return cfg


def cfg_int(cfg, key, lo, hi, default):
  # An int from a cfg dict, or default when it is missing, invalid, or out of range.
  try:
    value = int(cfg[key])
  except (KeyError, ValueError):
    return default
  return value if lo <= value <= hi else default


def check_range(value, lo, hi, what):
  if not lo <= value <= hi:
    raise ValueError("%s must be %d-%d." % (what, lo, hi))


def is_bits(text, n):
  return len(text) == n and all(c in "01" for c in text)


def bits_text(bits):
  return "".join("1" if b else "0" for b in bits)


def set_bits(bits, text):
  for i, c in enumerate(text):
    bits[i] = 1 if c == "1" else 0


def trim_rows(rows, col, cutoff, cap):
  # Drop rows whose column col is before cutoff, then keep the newest cap. Oldest first.
  while rows and rows[0][col] < cutoff:
    rows.pop(0)
  if len(rows) > cap:
    del rows[:len(rows) - cap]


def div_round(a, n):
  # a / n rounded to the nearest int, without floats.
  if a < 0:
    return -((-2 * a + n) // (2 * n))
  return (2 * a + n) // (2 * n)


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
  # Days since 1970-01-01 (Howard Hinnant).
  y -= m <= 2
  era = (y if y >= 0 else y - 399) // 400
  yoe = y - era * 400
  doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
  doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
  return era * 146097 + doe - 719468


def civil_from_days(z):
  # Inverse of days_from_civil. Returns (year, month, day).
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
  # Monday is 0. 1970-01-01 was a Thursday.
  return (days_from_civil(y, m, d) + 3) % 7


def last_day(y, m):
  if m == 12:
    return days_from_civil(y + 1, 1, 1) - days_from_civil(y, m, 1)
  return days_from_civil(y, m + 1, 1) - days_from_civil(y, m, 1)


def nth_sunday(y, m, n):
  # n = 1, 2, ... or -1 for the last Sunday. Sunday's Monday-based weekday is 6.
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
  # Recompute the cached local offset. Summer time flips without a restart.
  global tz_offset_s, tz_checked_at
  if tz_fixed:
    return
  now_ms = utime.ticks_ms()
  if not force and tz_checked_at and utime.ticks_diff(now_ms, tz_checked_at) < MINUTE:
    return
  tz_checked_at = now_ms
  tz_offset_s = zone_offset_s(tz_name, utime.time())


def apply_zone(cfg):
  # timezone= wins. An old utc_offset_hours file keeps that fixed offset.
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


def apply_settings_zone(name):
  # A timezone saved from Advanced wins over wifi.cfg. No log when it is unchanged.
  global tz_name, tz_fixed, settings_timezone
  if name not in ZONES:
    raise ValueError("Unknown timezone.")
  settings_timezone = name
  if not tz_fixed and tz_name == name:
    refresh_tz(True)
    return
  tz_name = name
  tz_fixed = False
  refresh_tz(True)
  log("Timezone: %s." % tz_name)


def parse_categories(text):
  # Comma-separated button names. "Note" is reserved and dropped. First spelling wins.
  names = []
  seen = []
  for part in str(text).split(","):
    name = " ".join(part.split())
    if not name or name.lower() == "note":
      continue
    if len(name) > MAX_CATEGORY_LEN:
      name = name[:MAX_CATEGORY_LEN].rstrip()
    if not name:
      continue
    key = name.lower()
    if key in seen:
      continue
    seen.append(key)
    names.append(name)
    if len(names) >= MAX_CATEGORIES:
      break
  return names


def canonical_kind(kind):
  kind = " ".join(str(kind).split())
  if kind.lower() == "note":
    return "note"
  return LEGACY_KINDS.get(kind, kind)


def match_category(kind):
  kind = canonical_kind(kind)
  if kind == "note":
    return "note"
  for name in maint_categories:
    if name.lower() == kind.lower():
      return name
  return None


# ---------------------------------------------------------------- logging

def rewrite_log_file():
  global log_file_lines
  if write_lines(LOG_FILE, log_lines):
    log_file_lines = len(log_lines)


def trim_log_memory():
  if len(log_lines) > log_keep:
    del log_lines[:len(log_lines) - log_keep]


def load_log():
  for line in read_lines(LOG_FILE):
    log_lines.append(line)
    if len(log_lines) > log_keep:
      log_lines.pop(0)
  rewrite_log_file()


def log(msg):
  # Print, keep the last log_keep lines in memory, and append to the rolling log file.
  global log_file_lines, log_rev
  line = "[%s] %s" % (timestamp(), msg)
  print(line)
  log_lines.append(line)
  trim_log_memory()
  log_rev += 1
  if append_line(LOG_FILE, line):
    log_file_lines += 1
  # Let the file grow to 2x before trimming, so flash isn't rewritten on every line
  if log_file_lines > 2 * log_keep:
    rewrite_log_file()


def purge_log():
  log_lines[:] = []
  rewrite_log_file()
  log("Log purged.")


# ---------------------------------------------------------------- settings

def load_settings():
  global sensor_interval_ms, log_keep, maint_categories, settings_timezone, settings_tz_rejected
  cfg = read_cfg(SETTINGS_CFG)
  # sensor_interval_s from older builds is ignored so a saved 10 seconds becomes 15 minutes
  interval = cfg_int(cfg, "sensor_interval_min", MIN_SENSOR_INTERVAL_MIN, MAX_SENSOR_INTERVAL_MIN,
                     DEFAULT_SENSOR_INTERVAL_MIN)
  sensor_interval_ms = interval * MINUTE
  log_keep = cfg_int(cfg, "log_keep", MIN_LOG_KEEP, MAX_LOG_KEEP, DEFAULT_LOG_KEEP)
  if "maint_categories" in cfg:
    maint_categories = parse_categories(cfg["maint_categories"])
  else:
    maint_categories = list(DEFAULT_CATEGORIES)
  name = cfg.get("timezone", "").strip()
  settings_tz_rejected = name if name and name not in ZONES else ""
  settings_timezone = "" if settings_tz_rejected else name


def save_settings(interval, keep, zone, categories):
  global sensor_interval_ms, log_keep, sensor_due_at, maint_categories
  check_range(interval, MIN_SENSOR_INTERVAL_MIN, MAX_SENSOR_INTERVAL_MIN, "Sensor interval (minutes)")
  check_range(keep, MIN_LOG_KEEP, MAX_LOG_KEEP, "Log lines kept")
  zone = str(zone).strip()
  if zone not in ZONES:
    raise ValueError("Unknown timezone.")
  names = parse_categories(categories)
  with open(SETTINGS_CFG, "w") as f:
    f.write("sensor_interval_min=%d\nlog_keep=%d\ntimezone=%s\nmaint_categories=%s\n" % (
      interval, keep, zone, ", ".join(names)))
  sensor_interval_ms = interval * MINUTE
  sensor_due_at = None  # read now; the next one follows the new interval
  log_keep = keep
  trim_log_memory()
  rewrite_log_file()
  maint_categories = names
  apply_settings_zone(zone)
  log("Settings changed: sensor every %d min, keep %d log lines, %d maintenance categories." % (
    interval, keep, len(names)))


# ---------------------------------------------------------------- schedule

def load_schedule():
  global manual_ms
  cfg = read_cfg(SCHEDULE_CFG)
  manual_ms = cfg_int(cfg, "manual_min", 1, MAX_MANUAL_MIN, DEFAULT_MANUAL_MIN) * MINUTE
  # A missing line (including an old period/duration file) keeps the default:
  # 08:00-14:00, every day.
  for key, bits in (("segments", segments), ("weekdays", weekdays)):
    text = cfg.get(key, "")
    if is_bits(text, len(bits)):
      set_bits(bits, text)


def save_schedule(text, manual, days):
  global manual_ms, suppressed_until
  if not is_bits(text, N_SLOTS):
    raise ValueError("Schedule must be 48 half-hour segments of 0 or 1.")
  if not is_bits(days, 7):
    raise ValueError("Weekdays must be 7 values of 0 or 1, Monday first.")
  check_range(manual, 1, MAX_MANUAL_MIN, "On-demand run (minutes)")
  with open(SCHEDULE_CFG, "w") as f:
    f.write("manual_min=%d\nsegments=%s\nweekdays=%s\n" % (manual, text, days))
  manual_ms = manual * MINUTE
  set_bits(segments, text)
  set_bits(weekdays, days)
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


def schedule_enabled():
  return time_synced and any(segments) and any(weekdays)


def local_parts(ts=None):
  # Return (device UTC seconds, local seconds, slot index 0-47, seconds into the slot).
  if ts is None:
    ts = utime.time()
  local = ts + tz_offset_s
  sec_day = int(local % DAY_S)
  return ts, local, sec_day // SLOT_S, sec_day % SLOT_S


def local_weekday(local):
  # Monday is 0. local is device-epoch seconds plus the timezone offset.
  y, m, d = civil_from_days((int(local) + EPOCH_OFFSET_S) // DAY_S)
  return weekday_mon0(y, m, d)


def slot_selected(idx, weekday):
  return segments[idx] and weekdays[weekday % 7]


def stretch_remaining_s(idx, sec_into, weekday):
  # Seconds until this run of selected slots ends. An off day ends it at midnight.
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
  # Seconds from now until the next selected slot on a selected day.
  if not schedule_enabled():
    return None
  now_ts = utime.time()
  origin = suppressed_until if suppressed_until and now_ts < suppressed_until else now_ts
  _, local, idx, sec_into = local_parts(origin)
  weekday = local_weekday(local)
  for step in range(N_SLOTS * 7):
    if step == 0 and sec_into != 0:
      continue
    slot = (idx + step) % N_SLOTS
    wd = (weekday + (idx + step) // N_SLOTS) % 7
    if slot_selected(slot, wd):
      return step * SLOT_S - sec_into + (origin - now_ts)
  return None


def schedule_state():
  # Return (schedule_running, in_selected_slot, seconds_left_in_stretch).
  if not schedule_enabled():
    return False, False, 0
  now_ts, local, idx, sec_into = local_parts()
  weekday = local_weekday(local)
  if not slot_selected(idx, weekday):
    return False, False, 0
  if suppressed_until and now_ts < suppressed_until:
    return False, True, 0
  return True, True, stretch_remaining_s(idx, sec_into, weekday)


def toggle_fan(now):
  # Same action as the physical button: stop if running, otherwise start an on-demand run.
  # Fan on/off is recorded in fan.hist, so it is not written to the log.
  global manual_running, manual_start, suppressed_until, btn_blank_start
  sched_running, _, sched_left = schedule_state()
  if sched_running or manual_running:
    manual_running = False
    if sched_running:
      suppressed_until = utime.time() + sched_left
  else:
    manual_running = True
    manual_start = now
    # Ignore button while the fan spins up (noise looks like another press)
    btn_blank_start = now


def climate_averages():
  # Mean of every stored reading. An averaged point counts once per reading it holds.
  st = sh = w = 0
  for _, t, h, _span, n in history:
    st += t * n
    sh += h * n
    w += n
  if w <= 0:
    return None, None
  return st / w / 10, sh / w / 10


def storage_usage():
  # Used and free bytes on the filesystem. None if the port cannot report it.
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
  return max(0, total - free), free


def status(now):
  sched_running, _, sched_left = schedule_state()
  manual_left = 0
  if manual_running:
    manual_left = max(0, (manual_ms - utime.ticks_diff(now, manual_start)) // SECOND)
  if sched_running:
    mode = "Scheduled run"
  elif manual_running:
    mode = "On-demand run"
  elif not time_synced:
    mode = "Waiting for clock"
  elif not schedule_enabled():
    mode = "Schedule off"
  else:
    mode = "Idle"
  temp_avg, hum_avg = climate_averages()
  store_used, store_free = storage_usage()
  return {
    "fan_on": sched_running or manual_running,
    "mode": mode,
    "remaining_s": max(manual_left, sched_left),
    "next_run_s": None if sched_running else next_run_delay_s(),
    "manual_min": manual_ms // MINUTE,
    "segments": bits_text(segments),
    "weekdays": bits_text(weekdays),
    "schedule": schedule_summary(),
    "temp_c": temp_c,
    "humidity": humidity,
    "temp_avg": temp_avg,
    "hum_avg": hum_avg,
    "sensor_ok": sensor_ok,
    "sensor_error": sensor_error,
    "sensor_at": None if sensor_at is None else sensor_at + EPOCH_OFFSET_S,
    "sensor_interval_min": sensor_interval_ms // MINUTE,
    "categories": maint_categories,
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
    "boot": BOOT_ID,
    "log_rev": log_rev,
    "maint_rev": maint_rev,
  }


# ---------------------------------------------------------------- climate history

def tier_span(age):
  # 0 keeps the reading. Positive is the average bucket. -1 drops the point.
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
  # Fold aged samples into hour, 4-hour, and 12-hour averages. Drop past 30 days.
  if now_ts is None:
    now_ts = utime.time()
  merged = []
  for ts, t, h, span, n in history:
    tier = tier_span(now_ts - ts)
    if tier < 0:
      continue
    # An average never becomes finer again, even if the clock steps back.
    tier = max(tier, span)
    if tier:
      ts = bucket_start(ts, tier)
    last = merged[-1] if merged else None
    if last and last[0] == ts and last[3] == tier:
      last[1] += t * n
      last[2] += h * n
      last[4] += n
    else:
      merged.append([ts, t * n, h * n, tier, n])
  history[:] = [(ts, div_round(st, n), div_round(sh, n), span, n) for ts, st, sh, span, n in merged]
  if len(history) > HISTORY_HARD_CAP:
    del history[:len(history) - HISTORY_HARD_CAP]


def rewrite_history_file():
  write_lines(HISTORY_FILE, ("%d,%.1f,%.1f,%d,%d" % (ts, t / 10, h / 10, span, n)
                             for ts, t, h, span, n in history))


def load_history():
  global history_stored_at
  history[:] = []
  interval_s = max(1, sensor_interval_ms // SECOND)
  for line in read_lines(HISTORY_FILE):
    parts = line.split(",")
    if len(parts) < 3:
      continue
    try:
      span = int(parts[3]) if len(parts) > 3 else 0
      # Files from before the reading count: estimate it from the span.
      n = int(parts[4]) if len(parts) > 4 else max(1, span // interval_s)
      history.append((int(parts[0]), round(float(parts[1]) * 10), round(float(parts[2]) * 10), span, n))
    except ValueError:
      continue
    # Fold as we read so a per-minute file is never held whole
    if len(history) > HISTORY_HARD_CAP + 40:
      compact_history()
  compact_history()
  history_stored_at = history[-1][0] if history else 0
  rewrite_history_file()


def store_history_sample(t, h):
  # Store one raw reading, then fold anything that has aged into a coarser tier.
  global history_stored_at
  if not time_synced:
    return
  now_ts = utime.time()
  gap = sensor_interval_ms // SECOND
  if history_stored_at and now_ts - history_stored_at < gap:
    return
  history.append((now_ts, round(t * 10), round(h * 10), 0, 1))
  history_stored_at = now_ts
  compact_history(now_ts)
  rewrite_history_file()


def rewrite_fan_runs_file():
  global fan_file_lines
  if write_lines(FAN_HISTORY_FILE, ("%d,%d,%d" % run for run in fan_runs)):
    fan_file_lines = len(fan_runs)


def load_fan_runs():
  now_ts = utime.time()
  cutoff = now_ts - FAN_KEEP_S
  fan_runs[:] = []
  for line in read_lines(FAN_HISTORY_FILE):
    parts = line.split(",")
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
    if len(fan_runs) > FAN_RUNS_MAX:
      fan_runs.pop(0)
  trim_rows(fan_runs, 1, cutoff, FAN_RUNS_MAX)
  rewrite_fan_runs_file()


def close_fan_run(now_ts):
  global fan_run_start, fan_file_lines
  if fan_run_start is None:
    return
  run = (fan_run_start, now_ts, fan_run_manual)
  fan_run_start = None
  fan_runs.append(run)
  trim_rows(fan_runs, 1, now_ts - FAN_KEEP_S, FAN_RUNS_MAX)
  if not fan_runs or fan_runs[-1] is not run:
    return
  if append_line(FAN_HISTORY_FILE, "%d,%d,%d" % run):
    fan_file_lines += 1
  # Appending is cheap; rewrite once expired runs make up a good part of the file
  if fan_file_lines > len(fan_runs) + 50:
    rewrite_fan_runs_file()


def track_fan_run(fan_active, manual):
  # Record fan on/off periods. Schedule (manual=0) replaces on-demand mid-run.
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


def point_json(p):
  return [p[0] + EPOCH_OFFSET_S, p[1] / 10, p[2] / 10, p[3]]


def history_json(days, page):
  if days not in (1, 3, 7, 30):
    days = 7
  now_ts = utime.time()
  cutoff = now_ts - days * DAY_S
  window = [p for p in history if p[0] >= cutoff]
  total = len(window)
  if page is not None:
    page = max(0, page)
    # Newest first
    end = total - page * SAMPLE_PAGE
    chunk = window[max(0, end - SAMPLE_PAGE):max(0, end)]
    samples = [point_json(p) for p in reversed(chunk)]
    return {"days": days, "total": total, "page": page, "samples": samples}
  pts = window
  if total > CHART_MAX_POINTS:
    pts = pts[::(total + CHART_MAX_POINTS - 1) // CHART_MAX_POINTS]
  # A window longer than the fan history would show only the recent slice.
  fan = None
  if days * DAY_S <= FAN_KEEP_S:
    runs = [r for r in fan_runs if r[1] >= cutoff]
    if fan_run_start is not None:
      runs.append((fan_run_start, now_ts, fan_run_manual))
    fan = [[s + EPOCH_OFFSET_S, e + EPOCH_OFFSET_S, m] for s, e, m in runs]
  return {"days": days, "count": total, "points": [point_json(p) for p in pts], "fan": fan}


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


def purge_all():
  # One status line. Purging the log last would erase the other two messages.
  global history_stored_at, fan_run_start, maint_rev
  history[:] = []
  fan_runs[:] = []
  history_stored_at = 0
  if fan_run_start is not None:
    fan_run_start = utime.time()
  maintenance[:] = []
  maint_rev += 1
  log_lines[:] = []
  rewrite_history_file()
  rewrite_fan_runs_file()
  rewrite_maintenance_file()
  rewrite_log_file()
  log("All stored history purged.")


# ---------------------------------------------------------------- maintenance

def rewrite_maintenance_file():
  write_lines(MAINT_FILE, ("%d,%s,%s" % row if row[2] else "%d,%s" % row[:2] for row in maintenance))


def load_maintenance():
  global maint_rev
  now_ts = utime.time()
  maintenance[:] = []
  for line in read_lines(MAINT_FILE):
    parts = line.split(",")
    if len(parts) < 2:
      continue
    kind = canonical_kind(parts[1])
    if not kind:
      continue
    text = ""
    if kind == "note":
      text = " ".join(",".join(parts[2:]).split())[:NOTE_MAX]
    try:
      maintenance.append((int(parts[0]), kind, text))
    except ValueError:
      continue
    if len(maintenance) > MAINT_MAX:
      maintenance.pop(0)
  trim_rows(maintenance, 0, now_ts - MAINT_KEEP_S, MAINT_MAX)
  rewrite_maintenance_file()
  maint_rev += 1


def record_maintenance(kind, text=""):
  global maint_rev
  stored = match_category(kind)
  if stored is None:
    raise ValueError("Unknown maintenance kind.")
  if not time_synced:
    raise ValueError("Clock not synced.")
  note = ""
  if stored == "note":
    note = " ".join(str(text).split())
    if not note:
      raise ValueError("Note is empty.")
    note = note[:NOTE_MAX]
  now_ts = utime.time()
  maintenance.append((now_ts, stored, note))
  trim_rows(maintenance, 0, now_ts - MAINT_KEEP_S, MAINT_MAX)
  rewrite_maintenance_file()
  maint_rev += 1


def maintenance_json():
  events = [[ts + EPOCH_OFFSET_S, kind, text] for ts, kind, text in reversed(maintenance)]
  return {"events": events}


def purge_maintenance():
  global maint_rev
  maintenance[:] = []
  rewrite_maintenance_file()
  maint_rev += 1
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


def sensor_measure(now):
  # One AM2302 read. Failures log once until the next success.
  global temp_c, humidity, sensor_ok, sensor_error, sensor_due_at, sensor_at, sensor_read_ticks
  sensor_read_ticks = now
  try:
    dht.measure()
    t = dht.temperature()
    h = dht.humidity()
  except Exception as e:
    sensor_due_at = utime.ticks_add(now, SENSOR_RETRY_MS)
    if sensor_error is None:
      log("Sensor: read failed (%s); retrying every %s." % (e, fmt_duration(SENSOR_RETRY_MS)))
    sensor_ok = False
    sensor_error = str(e) or "read failed"
    return
  if sensor_error is not None:
    log("Sensor: reading again.")
  sensor_due_at = utime.ticks_add(now, sensor_interval_ms)
  temp_c = t
  humidity = h
  sensor_ok = True
  sensor_error = None
  if time_synced:
    sensor_at = utime.time()
  store_history_sample(t, h)


def sensor_poll(now):
  # Read the AM2302 every sensor_interval_ms, retrying sooner after a failure.
  if dht is None:
    return
  if sensor_due_at is not None and utime.ticks_diff(now, sensor_due_at) < 0:
    return
  sensor_measure(now)


def sensor_read_now(now):
  # Page request. Inside the 2 s gap the current values are already the answer.
  if dht is None:
    raise ValueError("Sensor unavailable.")
  if sensor_read_ticks is not None and utime.ticks_diff(now, sensor_read_ticks) < 2 * SECOND:
    return
  sensor_measure(now)


# ---------------------------------------------------------------- button

def btn_irq(_pin):
  # Latch a press on release, so a press counts even while the loop is busy sending.
  # Runs as a hard interrupt where possible: no allocation here.
  global btn_down_at, btn_press
  t = utime.ticks_ms()
  if btn_pin.value() == 0:
    btn_down_at = t
  elif btn_down_at >= 0:
    if utime.ticks_diff(t, btn_down_at) >= DEBOUNCE_MS:
      btn_press = True
    btn_down_at = -1


def button_start():
  trigger = machine.Pin.IRQ_FALLING | machine.Pin.IRQ_RISING
  try:
    btn_pin.irq(trigger=trigger, handler=btn_irq, hard=True)
  except TypeError:
    btn_pin.irq(trigger=trigger, handler=btn_irq)


def button_pressed():
  # True once per press since the last call.
  global btn_press
  if not btn_press:
    return False
  btn_press = False
  return True


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
  # Heap as it actually is: do not collect first.
  log("Memory: free=%d alloc=%d history=%d fan=%d log=%d maint=%d" % (
    gc.mem_free(), gc.mem_alloc(), len(history), len(fan_runs), len(log_lines), len(maintenance)))


def wifi_start(now):
  global wlan, wifi_cfg, hostname
  wifi_cfg = read_cfg(WIFI_CFG)
  apply_zone(wifi_cfg)
  if settings_timezone:
    apply_settings_zone(settings_timezone)
  if not wifi_cfg.get("ssid"):
    log("WiFi: no %s with ssid=... found, web interface disabled." % WIFI_CFG)
    return
  # Hostname must be set before the interface comes up; the ESP32 port's mDNS
  # responder then answers for <hostname>.local
  name = wifi_cfg.get("hostname", DEFAULT_HOSTNAME)
  hostname = None
  try:
    network.hostname(name)
    hostname = name
  except (ValueError, OSError):
    log("WiFi: could not set hostname '%s'; use the IP address instead." % name)
  gc.collect()
  try:
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
  except OSError as e:
    wlan = None
    log("WiFi: could not start (%s)." % e)
    return
  log("WiFi: connecting to '%s'..." % wifi_cfg["ssid"])
  wifi_connect(now)


def sync_time(now):
  # Set the clock from NTP. Only the first failure in a row is logged.
  global time_synced, ntp_at, ntp_failed
  if ntptime is None:
    return
  ntp_at = now
  try:
    ntptime.settime()
  except Exception as e:
    if not ntp_failed:
      log("Clock sync failed (%s); retrying every %s." % (e, fmt_duration(NTP_RETRY_MS)))
    ntp_failed = True
    return
  if not time_synced or ntp_failed:
    log("Clock synced from the internet.")
  time_synced = True
  ntp_failed = False
  refresh_tz(True)


def clock_poll(now):
  # Retry every minute until the clock is set, then re-sync daily against drift.
  # A connected board with no attempt yet syncs immediately.
  if not wifi_connected or ntptime is None:
    return
  if ntp_at is None:
    sync_time(now)
    return
  wait = NTP_RESYNC_MS if time_synced else NTP_RETRY_MS
  if utime.ticks_diff(now, ntp_at) >= wait:
    sync_time(now)


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
        sync_time(now)
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


def send_head(conn, status_line, content_type, length, extra=""):
  conn.sendall(((
    "HTTP/1.1 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n%s"
    "Cache-Control: no-store\r\nConnection: close\r\n\r\n"
  ) % (status_line, content_type, length, extra)).encode())


def send(conn, status_line, content_type, body):
  if isinstance(body, str):
    body = body.encode()
  send_head(conn, status_line, content_type, len(body))
  conn.sendall(body)


def send_json(conn, data):
  send(conn, "200 OK", "application/json", json.dumps(data))


def file_size_mtime(path):
  try:
    st = os.stat(path)
  except OSError:
    return None
  return st[6], st[8]


def send_page(conn):
  # Stream the page from flash. The gzipped copy is used unless index.html is newer.
  plain = file_size_mtime(PAGE_FILE)
  packed = file_size_mtime(PAGE_GZ)
  if packed and (plain is None or packed[1] >= plain[1]):
    path, size, extra = PAGE_GZ, packed[0], "Content-Encoding: gzip\r\n"
  elif plain:
    path, size, extra = PAGE_FILE, plain[0], ""
  else:
    send(conn, "404 Not Found", "text/plain", "%s is missing. Copy it to the board next to main.py." % PAGE_FILE)
    return
  send_head(conn, "200 OK", "text/html; charset=utf-8", size, extra)
  view = memoryview(send_buf)
  with open(path, "rb") as f:
    while True:
      n = f.readinto(send_buf)
      if not n:
        break
      conn.sendall(view[:n])


def stream_file(conn, path):
  view = memoryview(send_buf)
  with open(path, "rb") as f:
    while True:
      n = f.readinto(send_buf)
      if not n:
        break
      conn.sendall(view[:n])


def download_filename(name):
  # Stamp before the extension so a new download does not replace the last one.
  stamp = timestamp().replace(" ", "-").replace(":", "")
  dot = name.rfind(".")
  if dot < 0:
    return "%s-%s" % (name, stamp)
  return "%s-%s%s" % (name[:dot], stamp, name[dot:])


def attachment_header(filename):
  return "Content-Disposition: attachment; filename=\"%s\"\r\n" % filename


def send_attachment(conn, path, filename):
  info = file_size_mtime(path)
  size = info[0] if info else 0
  send_head(conn, "200 OK", "text/plain; charset=utf-8", size, attachment_header(filename))
  if info:
    stream_file(conn, path)


def send_sections(conn, filename, sections):
  # Each section is a header line, the file (if it exists), and a trailing newline.
  prepared = []
  total = 0
  for title, path in sections:
    head = ("# %s\n" % title).encode()
    info = file_size_mtime(path)
    size = info[0] if info else 0
    prepared.append((head, path if info else None))
    total += len(head) + size + 1
  send_head(conn, "200 OK", "text/plain; charset=utf-8", total, attachment_header(filename))
  for head, path in prepared:
    conn.sendall(head)
    if path:
      stream_file(conn, path)
    conn.sendall(b"\n")


def send_download(conn, which):
  if which == "log":
    send_attachment(conn, LOG_FILE, download_filename("events.log"))
  elif which == "maintenance":
    send_attachment(conn, MAINT_FILE, download_filename("maintenance.hist"))
  elif which == "history":
    send_sections(conn, download_filename("climate.txt"), (
      (HISTORY_FILE, HISTORY_FILE),
      (FAN_HISTORY_FILE, FAN_HISTORY_FILE),
    ))
  elif which == "all":
    send_sections(conn, download_filename("terrarium.txt"), (
      (LOG_FILE, LOG_FILE),
      (HISTORY_FILE, HISTORY_FILE),
      (FAN_HISTORY_FILE, FAN_HISTORY_FILE),
      (MAINT_FILE, MAINT_FILE),
    ))
  else:
    send(conn, "404 Not Found", "text/plain", "Not found")


def read_request(conn):
  # Return (method, path, query, body, content type).
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
  ctype = ""
  for line in lines[1:]:
    key, _, value = line.partition(":")
    key = key.strip().lower()
    if key == "content-length":
      length = int(value.strip())
    elif key == "content-type":
      ctype = value.strip().lower()
  while len(body) < min(length, 2048):
    chunk = conn.recv(512)
    if not chunk:
      break
    body += chunk
  return parts[0], path, qs, body, ctype


def query_int(qs, key, default):
  for part in qs.split("&"):
    if part.startswith(key + "="):
      try:
        return int(part.split("=", 1)[1])
      except ValueError:
        return default
  return default


def api_button(data, now):
  toggle_fan(now)


def api_schedule(data, now):
  save_schedule(str(data["segments"]), int(data["manual_min"]), str(data["weekdays"]))


def api_settings(data, now):
  save_settings(int(data["sensor_interval_min"]), int(data["log_keep"]),
                str(data["timezone"]), str(data.get("maint_categories", "")))


def api_sensor(data, now):
  sensor_read_now(now)


def api_maintenance(data, now):
  text = data.get("text")
  record_maintenance(str(data["kind"]), "" if text is None else str(text))
  return maintenance_json()


# Each returns the reply body, or None to reply with the status.
POST_ROUTES = {
  "/api/button": api_button,
  "/api/schedule": api_schedule,
  "/api/settings": api_settings,
  "/api/maintenance": api_maintenance,
  "/api/sensor": api_sensor,
  "/api/purge-log": lambda data, now: purge_log(),
  "/api/purge-history": lambda data, now: purge_history(),
  "/api/purge-maintenance": lambda data, now: purge_maintenance(),
  "/api/purge-all": lambda data, now: purge_all(),
}


def handle(conn, method, path, qs, body, ctype, now):
  if method == "GET":
    if path == "/":
      send_page(conn)
    elif path == "/api/status":
      send_json(conn, status(now))
    elif path == "/api/logs":
      send(conn, "200 OK", "text/plain; charset=utf-8", "\n".join(log_lines))
    elif path == "/api/history":
      page = query_int(qs, "page", 0) if "page=" in qs else None
      send_json(conn, history_json(query_int(qs, "days", 7), page))
    elif path == "/api/maintenance":
      send_json(conn, maintenance_json())
    elif path == "/api/download":
      which = ""
      for part in qs.split("&"):
        if part.startswith("which="):
          which = part.split("=", 1)[1]
          break
      send_download(conn, which)
    else:
      send(conn, "404 Not Found", "text/plain", "Not found")
    return
  route = POST_ROUTES.get(path) if method == "POST" else None
  if route is None:
    send(conn, "404 Not Found", "text/plain", "Not found")
    return
  # Another site can send a plain-text POST here without asking. JSON needs the
  # browser to ask first, and this server never says yes.
  if not ctype.startswith("application/json"):
    send(conn, "415 Unsupported Media Type", "text/plain", "Send JSON.")
    return
  try:
    data = json.loads(body.decode()) if body else {}
    reply = route(data, now)
  except (ValueError, KeyError, TypeError, AttributeError) as e:
    send(conn, "400 Bad Request", "text/plain", str(e) or "Invalid request.")
    return
  send_json(conn, status(now) if reply is None else reply)


def web_poll(now):
  if server is None:
    return
  try:
    conn, _ = server.accept()
  except OSError:
    return
  try:
    conn.settimeout(1)
    method, path, qs, body, ctype = read_request(conn)
    handle(conn, method, path, qs, body, ctype, now)
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

# Safe mode: holding the button during power-up/reset skips the controller and
# leaves the board at the REPL, so Thonny/mpremote can always get back in.
# Checked before any file is read, so a damaged file cannot block it.
if btn_pin.value() == 0:
  log("SAFE MODE: button held at boot, controller NOT started.")
  print("--> Board is idle at the REPL; edit files, then reset to run normally.")
  print("--> (Not holding it? The button is miswired: use the DIAGONAL legs.)")
  sys.exit()

load_settings()
load_log()
load_schedule()
if settings_tz_rejected:
  log("Settings timezone '%s' is not built in; keeping the wifi.cfg timezone." % settings_tz_rejected)

_cause = machine.reset_cause()
log("Terrarium Climate Controller starting (ESP32-C3), reset cause: %s" % RESET_CAUSE_NAMES.get(_cause, _cause))
log("Pins: fan=GPIO%d  button=GPIO%d  sensor=GPIO%d" % (FAN_GPIO, BTN_GPIO, DHT_GPIO))
log("Schedule: %s" % schedule_text())
log("Settings: sensor every %d min, keep %d log lines." % (sensor_interval_ms // MINUTE, log_keep))

button_start()
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

def control_step(now):
  global manual_running, btn_blank_start, fan_was_active, mem_logged_at

  refresh_tz()
  wifi_poll(now)
  clock_poll(now)
  sensor_poll(now)
  if utime.ticks_diff(now, mem_logged_at) >= MEM_LOG_MS:
    log_memory()
    mem_logged_at = now

  # 1. Check Manual Run Expiration. The run itself is stored in fan.hist.
  if manual_running and utime.ticks_diff(now, manual_start) >= manual_ms:
    manual_running = False

  # 2. Handle Button Press (debounced in the interrupt, EMI-blanked here)
  blanked = btn_blank_start is not None and utime.ticks_diff(now, btn_blank_start) < EMI_BLANK_MS
  if not blanked:
    btn_blank_start = None
  if button_pressed() and not blanked:
    toggle_fan(now)

  # 3. Handle one web request, if any
  web_poll(now)

  # 4. Drive Fan Output. Schedule wins, so an on-demand run splits when a slot starts.
  sched_running, _, _ = schedule_state()
  fan_active = sched_running or manual_running
  if fan_active and not fan_was_active:
    # Also blank when a scheduled run starts the fan for the same reason
    btn_blank_start = now
  fan_was_active = fan_active
  fan_pin.value(1 if fan_active else 0)
  track_fan_run(fan_active, manual_running and not sched_running)


errors = 0
last_error = None
error_logged_at = 0
try:
  while True:
    now = utime.ticks_ms()
    try:
      control_step(now)
      errors = 0
    except Exception as e:
      errors += 1
      text = "%s: %s" % (type(e).__name__, e)
      if text != last_error or utime.ticks_diff(now, error_logged_at) >= MINUTE:
        log("Loop error (%d in a row): %s" % (errors, text))
        last_error = text
        error_logged_at = now
      if errors >= ERROR_RESET_COUNT:
        log("Too many loop errors; resetting.")
        fan_pin.value(0)
        machine.reset()
      utime.sleep_ms(500)
    utime.sleep_ms(LOOP_MS)
finally:
  fan_pin.value(0)
  track_fan_run(False, False)
  log("Program stopped: fan OFF.")
