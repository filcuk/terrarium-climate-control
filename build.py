"""Build the files to copy onto the ESP32-C3.

    python build.py

Writes dist/main.py (a short launcher), dist/terrarium.mpy (the compiled
controller), dist/index.html, and dist/index.html.gz.

The board runs main.py only. Importing terrarium.mpy skips compiling the
controller on the device, which is what frees the memory Wi-Fi needs.

mpy-cross must match the MicroPython version on the board. A mismatch makes
the board raise: incompatible .mpy file

    python -m venv .venv
    # activate .venv, then:
    python -m pip install -r requirements.txt

Or set MPY_CROSS to the executable. This compile does not use native code,
so no -march flag is required.
"""
import gzip
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"


def find_mpy_cross():
    env = os.environ.get("MPY_CROSS", "").strip()
    if env:
        return env
    found = shutil.which("mpy-cross")
    if found:
        return found
    sys.exit(
        "mpy-cross was not found.\n"
        "Install the build that matches the MicroPython version on the ESP32-C3\n"
        "into this folder's virtual environment:\n"
        "  python -m venv .venv\n"
        "  activate .venv, then: python -m pip install -r requirements.txt\n"
        "Or set MPY_CROSS to the executable path.\n"
        "A mismatched version makes the board raise: incompatible .mpy file"
    )


def main():
    cross = find_mpy_cross()
    DIST.mkdir(exist_ok=True)
    out = DIST / "terrarium.mpy"
    subprocess.check_call([cross, "-o", str(out), "-s", "terrarium.py", str(ROOT / "main.py")])
    (DIST / "main.py").write_text("import terrarium\n", encoding="utf-8")
    html = (ROOT / "index.html").read_bytes()
    (DIST / "index.html").write_bytes(html)
    packed = gzip.compress(html, compresslevel=9)
    (DIST / "index.html.gz").write_bytes(packed)
    (ROOT / "index.html.gz").write_bytes(packed)
    print("Wrote %s" % DIST)


if __name__ == "__main__":
    main()
