#!/bin/bash
# Start Santa in this Terminal window. Close the window (or press Ctrl+C, or use
# "Quit Santa" in the app) to stop it. The UI opens in your default browser.
VENV="$HOME/Library/Application Support/Santa/venv"
if [ ! -x "$VENV/bin/santa" ]; then
  echo "Santa is not installed yet. Double-click 'Install Santa.command' first."
  read -r -n 1 -p "Press any key to close…"; exit 1
fi
exec "$VENV/bin/santa" "$@"
