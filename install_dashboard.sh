#!/bin/bash
# Keeps the dashboard running in the background (macOS launchd), reachable from your
# iPhone through Tailscale. Starts at login and restarts if it ever stops.
# Install:   bash install_dashboard.sh
# Uninstall: bash install_dashboard.sh --remove
set -e
LABEL="com.running-sync.dashboard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3)"

if [ "$1" = "--remove" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed background dashboard."
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$DIR/dashboard.py</string><string>--tailscale</string><string>--no-browser</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$DIR/dashboard.out</string>
  <key>StandardErrorPath</key><string>$DIR/dashboard.err</string>
</dict>
</plist>
PL
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Background dashboard installed (python: $PY)."
echo "On this Mac: http://127.0.0.1:8765/"
TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
NAME="$("$TS" status --json 2>/dev/null | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null)"
if [ -n "$NAME" ]; then
  echo "On your iPhone (Tailscale on): http://$NAME:8765/"
else
  echo "Tailscale isn't connected; open the Tailscale app and log in, then your iPhone can reach it."
fi
