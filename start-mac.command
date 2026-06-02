#!/bin/bash
# RealHands — start the local bridge. Double-click this file in Finder.
# First run sets up a small Python environment (one minute). After that it's instant.
# Requires Python 3.10+ (preinstalled on most Macs; otherwise: https://www.python.org/downloads/).

cd "$(dirname "$0")/bridge" || { echo "Could not find the bridge folder."; read -r; exit 1; }

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required. Install it from https://www.python.org/downloads/ and try again."
  read -r; exit 1
fi

if [ ! -d .venv ]; then
  echo "First run — setting things up (about a minute)…"
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt || {
    echo "Setup failed. Make sure you have an internet connection and try again."; read -r; exit 1; }
fi

echo ""
echo "  RealHands is running."
echo "  Bridge:  http://localhost:7878"
echo "  Leave this window open while you use it. Press Ctrl+C to stop."
echo ""
exec .venv/bin/uvicorn bridge:app --host 127.0.0.1 --port 7878
