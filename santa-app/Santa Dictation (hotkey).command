#!/bin/bash
# Santa dictation into ANY app: hold Fn+Ctrl, speak, release — the text is pasted
# where your cursor is (auto-send/Enter stays off unless you hold Option while
# releasing). Uses the Santa server for recognition, so the language you pick in
# the Santa window (incl. Hinglish) applies here too. Keep this window open while
# you dictate; close it (or Ctrl+C) to stop.
# First run: macOS asks to allow Terminal under Privacy & Security ›
# Microphone, Accessibility and Input Monitoring — all three are needed.
VENV="$HOME/Library/Application Support/Santa/venv"
if [ ! -x "$VENV/bin/santa-dictation" ]; then
  echo "Santa is not installed yet (or needs updating). Double-click 'Install Santa.command' first."
  read -r -n 1 -p "Press any key to close…"; exit 1
fi
exec "$VENV/bin/santa-dictation"
