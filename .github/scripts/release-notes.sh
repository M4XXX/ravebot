#!/usr/bin/env bash
# Print the changelog section "## [VERSION]" (without its heading line).
# Usage: release-notes.sh VERSION [CHANGELOG.md]
# Exit code 1 (and no output) when the section does not exist or is empty.
set -euo pipefail
version="${1:?usage: release-notes.sh VERSION [CHANGELOG.md]}"
file="${2:-CHANGELOG.md}"
notes="$(awk -v v="$version" '
  /^## \[/ { if (found) exit; if (index($0, "## [" v "]") == 1) { found = 1; next } }
  found { print }
' "$file")"
# Trim leading and trailing blank lines.
notes="$(printf '%s\n' "$notes" | sed -e '/./,$!d' | sed -e ':a' -e '/^\n*$/{$d;N;ba' -e '}')"
if [ -z "${notes//[[:space:]]/}" ]; then
  exit 1
fi
printf '%s\n' "$notes"
