#!/bin/bash
# install_watchdog.sh — install / refresh / remove the board watchdog on THIS
# machine (your laptop or any always-on box that can ssh to the targets).
#
#   bash deploy/install_watchdog.sh             install, or refresh the kit copy
#   bash deploy/install_watchdog.sh uninstall
#
# The kit (deploy.sh, watchdog.sh, targets.conf, board/*.py) is copied to
# KIT_DIR and the scheduler runs it from there: on macOS, launchd jobs cannot
# read ~/Desktop or ~/Documents without Full Disk Access, and a copy also keeps
# the watchdog independent of whatever branch the repo is on. deploy.sh
# refreshes the copy after every successful deploy.
#   macOS: a LaunchAgent, every 5 minutes.
#   Linux: prints the crontab line to add (crontab -e).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CONFIG=${CONFIG:-$HERE/targets.conf}
LABEL=io.github.feishu-gpu-board.watchdog
if [ "$(uname)" = Darwin ]; then KIT_DIR="$HOME/Library/Application Support/gpu-board-watchdog"
else KIT_DIR="$HOME/.local/share/gpu-board-watchdog"; fi
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ "${1:-}" = uninstall ]; then
  if [ "$(uname)" = Darwin ]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed $LABEL (kit and state left in $KIT_DIR)"
  else
    echo "remove the gpu-board line from your crontab (crontab -e); kit is in $KIT_DIR"
  fi
  exit 0
fi

[ -f "$CONFIG" ] || { echo "no $CONFIG yet - configure and deploy first" >&2; exit 1; }
mkdir -p "$KIT_DIR/deploy" "$KIT_DIR/board"
cp "$HERE/deploy.sh" "$HERE/watchdog.sh" "$KIT_DIR/deploy/"
cp "$CONFIG" "$KIT_DIR/deploy/targets.conf"
cp "$HERE"/../board/*.py "$KIT_DIR/board/"

if [ "$(uname)" != Darwin ]; then
  echo "kit copied to $KIT_DIR. Add this line with 'crontab -e':"
  echo "*/5 * * * * /bin/bash '$KIT_DIR/deploy/watchdog.sh'"
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$KIT_DIR/deploy/watchdog.sh</string>
  </array>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/gpu-board-watchdog.err</string>
</dict>
</plist>
EOF
plutil -lint "$PLIST" >/dev/null
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL: every 300 s runs $KIT_DIR/deploy/watchdog.sh"
echo "log: ~/Library/Logs/gpu-board-watchdog.log"
