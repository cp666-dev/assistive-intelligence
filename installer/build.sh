#!/bin/bash
# Build the release assets from this checkout's last commit, after both test suites pass.
#   installer/build.sh [output folder, default dist/]
# Writes, for a GitHub release (asset names have no spaces; GitHub would turn them into dots):
#   UA-Assistive-Intelligence.lunaaddon          the Add On alone: what Universe and LUNA install
#   UA-Assistive-Intelligence-Installer.zip      the Add On, the helper, Install/Uninstall and READ ME FIRST
# and the unzipped installer folder, "UA Assistive Intelligence/".
set -euo pipefail
REPO="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
# The SDK installed with LUNA Builder Tools if there is one (its pack checks are the newest), else the
# one npm installed for the tests.
CLI="$HOME/Library/Application Support/LUNA Builder SDK/dist/cli.js"
[ -f "$CLI" ] || CLI="$REPO/assistant/node_modules/@uaudio/luna-builder-sdk/dist/cli.js"
OUT="${1:-$REPO/dist}"
NAME="UA Assistive Intelligence"
DEST="$OUT/$NAME"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
source "$HOME/.nvm/nvm.sh" && nvm use 22 >/dev/null

# What gets tested is what gets packed: refuse uncommitted changes.
git -C "$REPO" diff --quiet HEAD -- assistant assistant-helper installer LICENSE NOTICE \
  || { echo "Commit your changes to assistant/, assistant-helper/, installer/, LICENSE and NOTICE first."; exit 1; }

echo "Testing the Add On ..."
(cd "$REPO/assistant" && { [ -d node_modules ] || npm ci --no-audit --no-fund >/dev/null; } && npx vitest run >/dev/null) \
  || { echo "Add On tests failed: run npm test in assistant/"; exit 1; }
echo "Testing the helper ..."
(cd "$REPO/assistant-helper" && uv sync --frozen -q && uv run --frozen pytest -q >/dev/null 2>&1) \
  || { echo "Helper tests failed: run uv run pytest in assistant-helper/"; exit 1; }

mkdir -p "$WORK/src" "$WORK/stage"
git -C "$REPO" archive HEAD assistant assistant-helper installer LICENSE NOTICE | tar -x -C "$WORK/src"

if [ -e "$DEST" ] && [ ! -f "$DEST/Install.command" ]; then
  echo "$DEST exists and isn't an earlier build; move it away first."; exit 1
fi
ADDON_ASSET="$OUT/UA-Assistive-Intelligence.lunaaddon"
ZIP_ASSET="$OUT/UA-Assistive-Intelligence-Installer.zip"
rm -rf "$DEST" "$ADDON_ASSET" "$ZIP_ASSET"
mkdir -p "$DEST"

# The Add On. Its folder inside the .lunaaddon is the manifest's package_name ("CP Assistant").
pkg="$(node -p "require('$WORK/src/assistant/manifest.json').package_name")"
ditto "$WORK/src/assistant" "$WORK/stage/$pkg"
rm -rf "$WORK/stage/$pkg/tests" "$WORK/stage/$pkg/vitest.config.js" "$WORK/stage/$pkg/package-lock.json" "$WORK/stage/$pkg/package.json"
cp "$WORK/src/LICENSE" "$WORK/src/NOTICE" "$WORK/stage/$pkg/"
# --no-vendor ships the SDK bundle the tests ran against, not whichever SDK is installed.
(cd "$WORK/stage" && node "$CLI" pack "$WORK/stage/$pkg" --no-vendor --out "$DEST/$NAME.lunaaddon" >/dev/null)
listing="$(unzip -l "$DEST/$NAME.lunaaddon")"  # not piped into grep -q: SIGPIPE + pipefail
grep -q " $pkg/manifest.json" <<<"$listing" || { echo "The .lunaaddon has no $pkg/manifest.json"; exit 1; }

# The helper, without its tests.
ditto "$WORK/src/assistant-helper" "$DEST/helper"
rm -rf "$DEST/helper/tests" "$DEST/helper/smoke" "$DEST/helper/.gitignore"

cp "$WORK/src/installer/Install.command" "$WORK/src/installer/Uninstall.command" "$WORK/src/installer/READ ME FIRST.md" "$WORK/src/LICENSE" "$WORK/src/NOTICE" "$DEST/"
chmod +x "$DEST/Install.command" "$DEST/Uninstall.command"
find "$DEST" -name .DS_Store -delete

ditto -c -k --norsrc --noextattr --keepParent "$DEST" "$ZIP_ASSET"
cp "$DEST/$NAME.lunaaddon" "$ADDON_ASSET"
(cd "$OUT" && shasum -a 256 "$(basename "$ADDON_ASSET")" "$(basename "$ZIP_ASSET")" > SHA256SUMS.txt)
echo "Built, from $(git -C "$REPO" log -1 --format='%h %s'):"
echo "  $ADDON_ASSET ($(du -h "$ADDON_ASSET" | cut -f1))"
echo "  $ZIP_ASSET ($(du -h "$ZIP_ASSET" | cut -f1))"
