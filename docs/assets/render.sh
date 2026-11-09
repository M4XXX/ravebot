#!/usr/bin/env bash
# Re-render the README images from docs/assets/src and the SVG sources.
# Usage (repository root):  CHROME=/path/to/chrome-headless-shell bash docs/assets/render.sh
# Any Chromium based headless browser works (chrome-headless-shell, chromium --headless).
set -euo pipefail
CHROME="${CHROME:?set CHROME to a headless Chromium binary}"
HERE="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
shot() { # name width height source
  "$CHROME" --no-sandbox --disable-gpu --hide-scrollbars --force-device-scale-factor=2 \
    --screenshot="$HERE/$1.png" --window-size="$2,$3" "$4" >/dev/null 2>&1
}
shot onboarding 760 990 "file://$HERE/src/onboarding.html"
shot lineup-post 760 390 "file://$HERE/src/lineup.html"
shot setup-wizard 760 860 "file://$HERE/src/setup.html"
shot admin-log 760 800 "file://$HERE/src/log.html"
PYTHONPATH="$HERE/../.." uv run python "$HERE/src/render_admin_page.py" "$TMP/admin.html"
shot admin-page 900 650 "file://$TMP/admin.html"
shot logo 512 512 "file://$HERE/logo.svg"
rm -rf "$TMP"
echo "done"
