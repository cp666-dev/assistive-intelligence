#!/bin/bash
# UA Assistive Intelligence: installer. Double-click it in Finder (or drag it into Terminal and
# press Return). Safe to run again: it repairs whatever isn't set up.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DATA="$HOME/Library/Application Support/CP Assistant"
HELPER="$DATA/helper"
ADDONS="/Library/Application Support/Universal Audio/LUNA/Add Ons"
ADDON_FILE="$HERE/UA Assistive Intelligence.lunaaddon"
URL="http://127.0.0.1:8791"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
note() { printf '  %s\n' "$*"; }
fail() {
  printf '\n\033[31m✗ %s\033[0m\n' "$*"
  printf '\nNothing else was changed. Fix that and run the installer again.\n'
  read -r -p "Press Return to close this window. " _ || true
  exit 1
}
post() { curl -s -m 5 -X POST -H 'Content-Type: application/json' -d "$2" "$URL$1" >/dev/null 2>&1; }
health() { curl -s -m 3 "$URL/health" 2>/dev/null; }

clear 2>/dev/null || true
bold "UA Assistive Intelligence — installer"
note "Everything runs on this Mac. The only downloads are uv and the two AI models (first time only)."

bold "1/6  Checking this Mac"
[ "$(uname -m)" = arm64 ] || fail "This needs a Mac with Apple Silicon (M1 or later)."
mem=$(( $(sysctl -n hw.memsize) / 1073741824 ))
[ "$mem" -ge 16 ] || fail "This needs 16 GB of memory or more; this Mac has $mem GB."
ok "Apple Silicon, $mem GB memory"
[ -d /Applications/LUNA.app ] || fail "LUNA isn't in Applications. Install LUNA 3.1.1 or later first."
luna="$(defaults read /Applications/LUNA.app/Contents/Info.plist CFBundleShortVersionString 2>/dev/null || echo 0)"
[ "$(printf '%s\n3.1.1\n' "$luna" | sort -V | head -1)" = 3.1.1 ] || fail "LUNA $luna is too old: update to 3.1.1 or later."
ok "LUNA $luna"
models="$HOME/.cache/huggingface/hub"
if [ ! -d "$models/models--mlx-community--Qwen3-8B-4bit" ]; then
  free=$(df -g "$HOME" | awk 'NR==2 {print $4}')
  [ "$free" -ge 10 ] || fail "This needs about 10 GB of free disk space for the models; $free GB is free."
  ok "$free GB free for the models"
fi
[ -f "$ADDON_FILE" ] && [ -f "$HERE/helper/run.sh" ] || fail "Files are missing next to this installer. Unzip the whole folder and run it from there."

bold "2/6  uv (installs the helper's Python)"
if command -v uv >/dev/null; then
  ok "uv is installed ($(command -v uv))"
else
  note "Installing uv from astral.sh ..."
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null 2>&1 || fail "Couldn't install uv. Check the internet connection."
  command -v uv >/dev/null || fail "uv didn't install."
  ok "uv installed"
fi

bold "3/6  The helper (listening and planning)"
mkdir -p "$HELPER"
# Keep the Python environment from an earlier install; replace everything else.
rsync -a --delete --exclude .venv "$HERE/helper/" "$HELPER/" || fail "Couldn't copy the helper to $HELPER."
ok "Copied to ~/Library/Application Support/CP Assistant/helper"
if [ ! -d "$models/models--mlx-community--Qwen3-8B-4bit" ] || [ ! -d "$models/models--mlx-community--parakeet-tdt-0.6b-v3" ]; then
  note "Downloading the speech and language models (about 7 GB). This can take 10 to 30 minutes."
fi
log="$(mktemp)"
(cd "$HELPER" && bash setup.sh) >"$log" 2>&1 || { sed 's/^/    /' "$log" | tail -20; fail "Setup failed (see above)."; }
rm -f "$log"
[ -x "$HELPER/.venv/bin/python" ] || fail "The helper's Python environment wasn't created."
ok "Models and Python ready"
bash "$HELPER/install-login-item.sh" >/dev/null 2>&1 || fail "Couldn't install the helper's login item."
ok "The helper starts by itself at login"

bold "4/6  The LUNA Add On"
if [ -L "$ADDONS/CP Assistant" ]; then
  ok "Already installed (a developer link), left as it is"
elif mkdir -p "$ADDONS" 2>/dev/null && [ -w "$ADDONS" ]; then
  rm -rf "$ADDONS/CP Assistant"
  ditto -x -k "$ADDON_FILE" "$ADDONS/" || fail "Couldn't unpack the Add On into LUNA's Add Ons folder."
  [ -f "$ADDONS/CP Assistant/manifest.json" ] || fail "The Add On didn't unpack where LUNA looks for it."
  ok "Installed in LUNA's Add Ons folder"
else
  open "$ADDON_FILE"
  note "LUNA opens and asks to install the Add On: click INSTALL."
  read -r -p "  Press Return once LUNA has installed it. " _ || true
fi

bold "5/6  Starting the helper"
note "Loading the models takes up to a minute the first time ..."
ready=""
for _ in $(seq 1 90); do
  h="$(health)"
  if echo "$h" | grep -q '"stt":"ready"' && echo "$h" | grep -q '"llm":"ready"'; then ready=1; break; fi
  sleep 2
done
[ -n "$ready" ] || fail "The helper didn't start. Its log is in ~/Library/Logs/CP Assistant/helper.err.log."
ok "Helper running"

bold "6/6  Microphone check"
note "macOS asks to let \"UA Assistive Intelligence Helper\" use the microphone: click Allow."
note "Then say a few words so we can hear you ..."
post /listen '{"inputs":[{"name":"Mic check","device":"","channel":0}],"enabled":true}'
heard=""
for i in $(seq 1 45); do
  sleep 2
  h="$(health)"
  if echo "$h" | grep -q '"listening":true' && ! echo "$h" | grep -q 'no sound at all\|not found\|cannot open'; then
    [ "$i" -ge 3 ] && { heard=1; break; }
  fi
  [ "$i" = 8 ] && note "Still silent. If you clicked Don't Allow: System Settings > Privacy & Security > Microphone, turn on UA Assistive Intelligence Helper."
done
post /listen '{"inputs":[],"enabled":false}'
if [ -n "$heard" ]; then
  ok "The microphone works"
else
  note "The helper hears nothing yet. Allow it in System Settings > Privacy & Security > Microphone,"
  note "then restart it: launchctl kickstart -k gui/\$(id -u)/com.cp.assistant.helper"
fi

bold "Done"
cat <<'EOF'
  In LUNA, on the Timeline or Mixer page: Add Ons > Browsers > UA Assistive Intelligence.
  In the panel, click + MIC and choose the mic you'll speak into (or type a request below).
  Speak a request, check the card, press GO. "READ ME FIRST" has phrases to try.
EOF
read -r -p $'\nPress Return to close this window. ' _ || true
