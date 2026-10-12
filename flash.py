"""Build (optional) and copy firmware files to the board for day-to-day updates.

Does not touch wifi.cfg. With the venv active:

    python flash.py
    python flash.py COM5
    python flash.py --no-build
    python flash.py COM5 --no-build

Default port is auto. Runs build.py first unless --no-build is set.
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"

# Local path -> path on the board. wifi.cfg is intentionally omitted.
FILES = (
  ("main.py", "main.py"),
  ("terrarium.mpy", "terrarium.mpy"),
  ("index.html", "index.html"),
  ("index.html.gz", "index.html.gz"),
  ("icon.webp", "icon.webp"),
  ("favicon.ico", "favicon.ico"),
)


def find_mpremote():
  found = shutil.which("mpremote")
  if found:
    return found
  sys.exit(
    "mpremote was not found.\n"
    "Activate .venv and install requirements: python -m pip install -r requirements.txt"
  )


def main():
  parser = argparse.ArgumentParser(description="Flash dist/ to the board (not wifi.cfg).")
  parser.add_argument(
    "port",
    nargs="?",
    default="auto",
    help="Serial port (default: auto). Example: COM5 or /dev/ttyACM0",
  )
  parser.add_argument(
    "--no-build",
    action="store_true",
    help="Skip python build.py and flash whatever is already in dist/",
  )
  args = parser.parse_args()

  if not args.no_build:
    subprocess.check_call([sys.executable, str(ROOT / "build.py")])

  missing = [name for name, _ in FILES if not (DIST / name).is_file()]
  if missing:
    sys.exit("Missing in dist/: %s. Run python build.py first." % ", ".join(missing))

  mpremote = find_mpremote()
  # One connection: copy each file, then reset. "+" keeps the same session.
  cmd = [mpremote, "connect", args.port]
  for local, remote in FILES:
    cmd.extend(["+", "cp", str(DIST / local), ":" + remote])
  cmd.extend(["+", "reset"])
  print(" ".join(cmd))
  subprocess.check_call(cmd)
  print("Flashed %d files to %s and reset." % (len(FILES), args.port))


if __name__ == "__main__":
  main()
