"""Pull board dbg.ndjson into workspace debug-0cf5c2.log for this debug session.

    python pull_dbg.py
    python pull_dbg.py COM5
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "debug-0cf5c2.log"
REMOTE = "dbg.ndjson"


def find_mpremote():
  found = shutil.which("mpremote")
  if found:
    return found
  sys.exit(
    "mpremote was not found.\n"
    "Activate .venv and install requirements: python -m pip install -r requirements.txt"
  )


def main():
  parser = argparse.ArgumentParser(description="Pull dbg.ndjson from the board.")
  parser.add_argument("port", nargs="?", default="auto", help="Serial port (default: auto)")
  args = parser.parse_args()
  mpremote = find_mpremote()
  # mpremote interrupts the running controller to talk over the REPL.
  # Copy the log, then hard-reset so the web server comes back up.
  # If the port is busy, close Thonny first.
  cmd = [mpremote, "connect", args.port, "cp", ":" + REMOTE, str(OUT), "+", "reset"]
  print(" ".join(cmd))
  try:
    subprocess.check_call(cmd)
  except subprocess.CalledProcessError:
    raw = subprocess.check_output(
      [mpremote, "connect", args.port, "cat", ":" + REMOTE],
      stderr=subprocess.STDOUT,
    )
    OUT.write_bytes(raw)
    subprocess.check_call([mpremote, "connect", args.port, "reset"])
  print("Wrote %s (%d bytes); board reset so the controller restarts." % (
    OUT, OUT.stat().st_size if OUT.is_file() else 0))


if __name__ == "__main__":
  main()
