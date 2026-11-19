#!/bin/sh
# Builds Chatwatch.app (the menu bar app) into ~/Applications and starts it at login.
# Needs Xcode's command line tools: xcode-select --install
# To remove it: mac/build.sh --uninstall
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
APP="$HOME/Applications/Chatwatch.app"
LABEL=com.chatwatch.menubar
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ "$1" = "--uninstall" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -rf "$APP" "$PLIST"
  echo "Chatwatch menu bar app removed."
  exit 0
fi

command -v swiftc >/dev/null || { echo "Needs Xcode's command line tools: run 'xcode-select --install', then this again."; exit 1; }
mkdir -p "$APP/Contents/MacOS" "$HOME/Library/LaunchAgents"
swiftc -O -swift-version 5 -parse-as-library -target "$(uname -m)-apple-macos13.0" \
  -o "$APP/Contents/MacOS/Chatwatch" "$HERE/Chatwatch.swift"
cat > "$APP/Contents/Info.plist" <<P
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Chatwatch</string>
  <key>CFBundleIdentifier</key><string>com.chatwatch.menubar</string>
  <key>CFBundleExecutable</key><string>Chatwatch</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
P
codesign --force -s - "$APP" >/dev/null 2>&1 || true

cat > "$PLIST" <<P
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$APP/Contents/MacOS/Chatwatch</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Interactive</string>
</dict></plist>
P
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Chatwatch is in your menu bar (look for the ring with a percentage)."
