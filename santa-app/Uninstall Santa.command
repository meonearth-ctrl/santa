#!/bin/bash
# Uninstall Santa. Removes ONLY what Santa created; asks before each step.
# The code folder itself (this repository) is left for you to delete in Finder.
SUPPORT="$HOME/Library/Application Support/Santa"
APP="$HOME/Applications/Santa.app"
ASSIST="$HOME/Applications/Santa Assistant.app"
ask() { read -r -p "$1 [y/N] " a; [[ "$a" == "y" || "$a" == "Y" ]]; }

echo "== Stopping Santa if it is running"
curl -s -X POST -H 'X-Santa: 1' http://127.0.0.1:8765/api/shutdown >/dev/null 2>&1 && echo "stopped" || echo "not running"

pkill -f santa-dictation 2>/dev/null; sleep 2; pkill -9 -f santa-dictation 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/local.santa.assistant.plist"   # "Start at login", if it was on
if [ -d "$APP" ] && ask "Remove the Santa app launchers ($APP and Santa Assistant.app)?"; then rm -rf "$APP" "$ASSIST"; echo removed; fi
if [ -d "$SUPPORT" ] && ask "Remove Santa's Python environment, settings, history and logs ($SUPPORT)?"; then
  rm -rf "$SUPPORT"; echo removed
fi
HUB="$HOME/.cache/huggingface/hub"
MODELS=$(ls -d "$HUB"/models--Systran--faster-whisper-* "$HUB"/models--mobiuslabsgmbh--faster-whisper-large-v3-turbo "$HUB"/models--ggerganov--whisper.cpp "$HOME/Library/Application Support/pywhispercpp" 2>/dev/null)
if [ -n "$MODELS" ]; then
  echo "Downloaded Whisper models (shared with Whisper Local if you use it):"; du -sh $MODELS 2>/dev/null
  if ask "Delete these model downloads?"; then rm -rf $MODELS; echo removed; fi
fi
if [ -d "$HOME/.whisperkey" ] && ask "Remove Whisper Local desktop-dictation settings (~/.whisperkey)? (only if you used 'Santa Dictation')"; then
  "$SUPPORT/venv/bin/whisper-local" --disable-autostart >/dev/null 2>&1
  rm -rf "$HOME/.whisperkey"; echo removed
fi
echo
echo "Left in place (shared tools you may use elsewhere):"
echo "  uv + Python 3.12 in ~/.local — to remove:  ~/.local/bin/uv python uninstall 3.12 && rm ~/.local/bin/uv ~/.local/bin/uvx"
echo "  The code folder: $(cd "$(dirname "$0")/.." && pwd) — delete it in Finder if you no longer want it."
echo "Done."
read -r -n 1 -p "Press any key to close…"
