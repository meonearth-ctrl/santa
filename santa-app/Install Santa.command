#!/bin/bash
# Install Santa — creates a private Python 3.12 environment for Santa and installs
# this repository into it. No admin rights; nothing is installed system-wide.
#   code:                      this repository (branch "santa")
#   env, settings, logs:       ~/Library/Application Support/Santa
#   app launcher:              ~/Applications/Santa.app
#   Python + uv (user-only):   ~/.local/bin/uv, ~/.local/share/uv
# Safe to run again (it updates in place).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SUPPORT="$HOME/Library/Application Support/Santa"
VENV="$SUPPORT/venv"
mkdir -p "$SUPPORT/logs"
LOG="$SUPPORT/logs/install.log"
exec > >(tee "$LOG") 2>&1
echo "== Santa install  $(date)"
echo "repo: $REPO"

# 1) uv (small, self-contained Python manager from Astral) into ~/.local/bin
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "== Installing uv into ~/.local/bin (user only; shell profile untouched)"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh
fi
uv --version

# 2) Python 3.12 (repo supports 3.11–3.13) + private virtual environment
uv python install 3.12
if [ ! -x "$VENV/bin/python" ]; then
  uv venv --python 3.12 "$VENV"
fi
"$VENV/bin/python" --version

# 3) Install the repo (editable) + test runner
# [santa-gpu] adds whisper.cpp (Metal) for fast short clips on Apple Silicon.
uv pip install --python "$VENV/bin/python" -e "$REPO[santa-gpu]" pytest
"$VENV/bin/python" -c "import faster_whisper, ctranslate2; print('faster-whisper', faster_whisper.__version__, '/ ctranslate2', ctranslate2.__version__)"

# 4) ~/Applications/Santa.app — double-clickable, no Terminal window
APP="$HOME/Applications/Santa.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Santa</string>
  <key>CFBundleDisplayName</key><string>Santa</string>
  <key>CFBundleIdentifier</key><string>local.santa.launcher</string>
  <key>CFBundleVersion</key><string>0.1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>Santa</string>
  <key>CFBundleIconFile</key><string>Santa</string>
  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Santa listens only while you record, and transcribes on this Mac.</string>
</dict></plist>
PLIST
cat > "$APP/Contents/MacOS/Santa" <<'LAUNCH'
#!/bin/bash
# Starts the Santa server in the background (or just opens the browser if it is
# already running). Stop it with "Quit Santa" in the web page.
SUPPORT="$HOME/Library/Application Support/Santa"
exec "$SUPPORT/venv/bin/santa" >> "$SUPPORT/logs/launcher.log" 2>&1
LAUNCH
chmod +x "$APP/Contents/MacOS/Santa"
# App icon: drawn by dev/make-icon.py, converted with macOS's own sips + iconutil
[ -f "$REPO/santa-app/icon-1024.png" ] || "$VENV/bin/python" "$REPO/santa-app/dev/make-icon.py" "$REPO/santa-app/icon-1024.png" || true
if [ -f "$REPO/santa-app/icon-1024.png" ]; then
  ICONSET="$(mktemp -d)/Santa.iconset"; mkdir -p "$ICONSET"
  for s in 16 32 128 256 512; do
    sips -z $s $s "$REPO/santa-app/icon-1024.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
    sips -z $((s*2)) $((s*2)) "$REPO/santa-app/icon-1024.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/Santa.icns" && rm -rf "$(dirname "$ICONSET")"
fi
touch "$APP"
echo "Created $APP"

# 5) ~/Applications/Santa Assistant.app — the floating always-on-top dictation
#    pill (click or hold Fn+Ctrl, text is pasted into the app you are using).
ASSIST="$HOME/Applications/Santa Assistant.app"
mkdir -p "$ASSIST/Contents/MacOS" "$ASSIST/Contents/Resources"
cat > "$ASSIST/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Santa Assistant</string>
  <key>CFBundleDisplayName</key><string>Santa Assistant</string>
  <key>CFBundleIdentifier</key><string>local.santa.assistant</string>
  <key>CFBundleVersion</key><string>0.2.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>SantaAssistant</string>
  <key>CFBundleIconFile</key><string>Santa</string>
  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Santa listens only while you dictate, and transcribes on this Mac.</string>
</dict></plist>
PLIST
cat > "$ASSIST/Contents/MacOS/SantaAssistant" <<'LAUNCH'
#!/bin/bash
SUPPORT="$HOME/Library/Application Support/Santa"
exec "$SUPPORT/venv/bin/santa-dictation" >> "$SUPPORT/logs/assistant.log" 2>&1 < /dev/null
LAUNCH
chmod +x "$ASSIST/Contents/MacOS/SantaAssistant"
[ -f "$APP/Contents/Resources/Santa.icns" ] && cp "$APP/Contents/Resources/Santa.icns" "$ASSIST/Contents/Resources/Santa.icns"
touch "$ASSIST"
echo "Created $ASSIST"
echo "== INSTALL OK $(date)"
echo "Start Santa from ~/Applications/Santa.app (Spotlight: 'Santa') or 'Santa.command'."
