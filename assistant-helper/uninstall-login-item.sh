#!/bin/bash
# Stop the helper and remove its LaunchAgent.
set -euo pipefail
LABEL="com.cp.assistant.helper"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
rm -rf "$HOME/Library/Application Support/CP Assistant/UA Assistive Intelligence Helper.app"
echo "Removed $LABEL (logs left in ~/Library/Logs/CP Assistant)"
