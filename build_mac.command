#!/bin/bash
# macOS build — double-click in Finder, or run: bash build_mac.command
# Produces dist/"DJ Copyright Prep.app" (no Terminal window when launched).
#
# IMPORTANT: place a macOS ffmpeg binary (named "ffmpeg", no extension) in the bin/
# folder before building. If it is missing, the app falls back to an ffmpeg already
# installed on the user's PATH (e.g. `brew install ffmpeg`).
set -e
cd "$(dirname "$0")"

PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then echo "Python 3 not found. Install it from python.org."; exit 1; fi

echo "Installing build dependencies..."
"$PY" -m pip install --quiet flask pywebview pyinstaller

echo "Building macOS app (a couple of minutes)..."
"$PY" -m PyInstaller --windowed --clean \
  --name "DJ Copyright Prep" \
  --add-data "bin:bin" \
  --collect-all webview \
  --exclude-module tkinter \
  app.py

echo
echo "DONE -> dist/DJ Copyright Prep.app"
echo "Drag it to /Applications. No Python needed on the target Mac."
