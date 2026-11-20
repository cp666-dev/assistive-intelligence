#!/bin/bash
# UA Assistive Intelligence: remove it from this Mac. Double-click it in Finder.
set -uo pipefail
DATA="$HOME/Library/Application Support/CP Assistant"
ADDONS="/Library/Application Support/Universal Audio/LUNA/Add Ons"
LABEL="com.cp.assistant.helper"

printf '\n\033[1mUA Assistive Intelligence — uninstall\033[0m\n\n'
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "  ✓ Helper stopped, and it no longer starts at login"
rm -rf "$DATA/helper" "$DATA/UA Assistive Intelligence Helper.app"
echo "  ✓ Helper removed"
if [ -L "$ADDONS/CP Assistant" ]; then
  echo "  - The LUNA Add On is a developer link; left as it is"
elif [ -d "$ADDONS/CP Assistant" ]; then
  rm -rf "$ADDONS/CP Assistant" && echo "  ✓ LUNA Add On removed" || echo "  ! Couldn't remove $ADDONS/CP Assistant"
fi

read -r -p $'\n  Also delete your saved setups and learned preferences? [y/N] ' a || a=""
if [ "$a" = y ] || [ "$a" = Y ]; then rm -rf "$DATA" "$HOME/Library/Logs/CP Assistant"; echo "  ✓ Deleted"; fi
read -r -p '  Also delete the AI models (about 7 GB)? [y/N] ' a || a=""
if [ "$a" = y ] || [ "$a" = Y ]; then
  rm -rf "$HOME/.cache/huggingface/hub/models--mlx-community--Qwen3-8B-4bit" \
         "$HOME/.cache/huggingface/hub/models--mlx-community--parakeet-tdt-0.6b-v3"
  echo "  ✓ Deleted"
fi
read -r -p $'\nDone. Press Return to close this window. ' _ || true
