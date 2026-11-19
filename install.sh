#!/bin/sh
# chatwatch for macOS: runs at login, page on http://127.0.0.1:7731, alerts every 5 minutes.
# Run it again after updating. To remove it: ./install.sh --uninstall
set -e
LABEL=com.chatwatch.server
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
HERE=$(cd "$(dirname "$0")" && pwd)

if [ "$1" = "--uninstall" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  "$HERE/mac/build.sh" --uninstall 2>/dev/null || true
  echo "chatwatch removed. Its files in ~/.claude (chatwatch-*.json*, handoffs/) are left in place."
  exit 0
fi

[ "$(uname)" = "Darwin" ] || { echo "This installer is for macOS. On Linux, run: python3 $HERE/chatwatch.py serve"; exit 1; }
PY=$(command -v python3 || true)
[ -n "$PY" ] && "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>/dev/null \
  || { echo "chatwatch needs Python 3.8 or newer. Install it from python.org, then run this again."; exit 1; }

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<P
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$HERE/chatwatch.py</string><string>serve</string></array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/.claude/chatwatch.log</string>
  <key>StandardErrorPath</key><string>$HOME/.claude/chatwatch.log</string>
</dict></plist>
P
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 2
echo "chatwatch is running: http://127.0.0.1:7731"
if command -v swiftc >/dev/null; then
  "$HERE/mac/build.sh"
else
  echo "For the menu bar app, install Xcode's command line tools (xcode-select --install) and run mac/build.sh."
fi
command -v terminal-notifier >/dev/null || echo "Tip: 'brew install terminal-notifier' makes alerts open the page when clicked."
open "http://127.0.0.1:7731" 2>/dev/null || true
