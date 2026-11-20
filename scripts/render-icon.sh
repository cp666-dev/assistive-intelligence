#!/bin/bash
# Renders assistant/icon.svg to assistant/icon.png (1024 x 1024) with headless Chrome.
#   scripts/render-icon.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cat > "$WORK/icon.html" <<HTML
<!doctype html><html><body style="margin:0;background:#000">
<img src="file://$ROOT/assistant/icon.svg" width="1024" height="1024" style="display:block">
</body></html>
HTML
"$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
  --allow-file-access-from-files --window-size=1024,1024 \
  --screenshot="$ROOT/assistant/icon.png" "file://$WORK/icon.html" >/dev/null 2>&1
file "$ROOT/assistant/icon.png"
