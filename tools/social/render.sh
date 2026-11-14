#!/usr/bin/env bash
# Render the README banner / GitHub social preview and the README's card and app picture.
#
#   tools/social/render.sh
#
# Writes assets/spotnav-social-preview.png (1280x640) and docs/images/card-and-app.png (900 px wide) from
# banner.html and card-and-app.html with headless chromium. No Home Assistant needed: the pages use two
# pictures kept in this folder. Refresh them when the card or the app changes, then run this again:
#
#   card-light.png  the card in Home Assistant's light theme at twice the pixels:
#                   tools/docs_screenshots/run.sh social-card
#   app-main.png    the app's English store screenshot, copied from the app repository
#                   (fastlane/metadata/android/en-US/images/phoneScreenshots/1_main.png)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
CHROMIUM="${CHROMIUM:-chromium}"
command -v "$CHROMIUM" >/dev/null 2>&1 || { echo "missing: $CHROMIUM" >&2; exit 1; }

PROFILE="$(mktemp -d)"
trap 'rm -rf "$PROFILE"' EXIT

render() { # page width height output
  "$CHROMIUM" --headless=new --disable-gpu --no-sandbox --hide-scrollbars --force-color-profile=srgb \
    --allow-file-access-from-files --user-data-dir="$PROFILE" --default-background-color=00000000 \
    --window-size="$2,$3" --virtual-time-budget=3000 --screenshot="$4" "file://$HERE/$1" 2>/dev/null
  echo "   $4"
}

render banner.html 1280 640 "$REPO/assets/spotnav-social-preview.png"
render card-and-app.html 900 580 "$REPO/docs/images/card-and-app.png"
