#!/bin/bash
# Install a LaunchAgent so the helper starts at login and is restarted if it exits.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
# From a git checkout, run the built copy instead: switching branches removes this folder from
# the checkout, and the login item would then run nothing (or stale code). The copy follows the
# branch's commits (.git/hooks/sync-built-add-ons).
BUILT="$HOME/LUNA Built Add Ons/branches/assistive-intelligence/assistant-helper"
if git -C "$DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 && [ -f "$BUILT/run.sh" ]; then
  DIR="$BUILT"
  (cd "$DIR" && PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH" uv sync --frozen --quiet)
fi
LABEL="com.cp.assistant.helper"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGS="$HOME/Library/Logs/CP Assistant"
# The helper runs inside a small app so macOS asks for the microphone by a clear name ("UA
# Assistive Intelligence Helper") and remembers the answer. A bare background Python may get
# silence with no prompt at all, and loses the permission whenever Python is updated.
APP="$HOME/Library/Application Support/CP Assistant/UA Assistive Intelligence Helper.app"
mkdir -p "$LOGS" "$HOME/Library/LaunchAgents" "$(dirname "$APP")"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
# bootout returns before the old service is gone; bootstrapping too early fails with error 5.
for _ in $(seq 1 20); do launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || break; sleep 0.5; done

rm -rf "$APP"
osacompile -o "$APP" -e "do shell script \"exec /bin/bash \" & quoted form of \"$DIR/run.sh\" & \" >> \" & quoted form of \"$LOGS/helper.log\" & \" 2>> \" & quoted form of \"$LOGS/helper.err.log\""
INFO="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$INFO"   # no Dock icon
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier $LABEL" "$INFO" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string $LABEL" "$INFO"
/usr/libexec/PlistBuddy -c "Set :CFBundleName UA Assistive Intelligence Helper" "$INFO" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleName string UA Assistive Intelligence Helper" "$INFO"
MIC_TEXT="UA Assistive Intelligence listens to the mics you choose in its LUNA panel, to hear spoken requests. Audio stays on this Mac."
/usr/libexec/PlistBuddy -c "Set :NSMicrophoneUsageDescription $MIC_TEXT" "$INFO" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :NSMicrophoneUsageDescription string $MIC_TEXT" "$INFO"
codesign --force --deep --sign - "$APP" 2>/dev/null || true

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$APP/Contents/MacOS/applet</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardErrorPath</key><string>$LOGS/applet.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
PLIST

launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed $PLIST ($DIR)"
echo "Logs: $LOGS"
echo "macOS asks to allow \"UA Assistive Intelligence Helper\" to use the microphone the first time it listens."
