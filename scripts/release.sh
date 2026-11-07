#!/usr/bin/env bash
# Prepare a release locally: bump the version, close the changelog section, commit, tag.
# Nothing is pushed. The last line tells you the command that starts the release build.
#
# Usage:  scripts/release.sh 2.0.0 [--no-tests]
set -euo pipefail

version="${1:-}"
skip_tests="${2:-}"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]]; then
  echo "usage: scripts/release.sh MAJOR.MINOR.PATCH[-prerelease] [--no-tests]" >&2
  exit 2
fi
tag="v$version"

cd "$(git rev-parse --show-toplevel)"
branch="$(git branch --show-current)"
if [ "$branch" != "main" ]; then
  echo "error: releases are made from main (current branch: $branch)" >&2
  exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
  echo "error: the working tree is not clean" >&2
  exit 1
fi
if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
  echo "error: tag $tag already exists" >&2
  exit 1
fi
if grep -q "^## \[$version\]" CHANGELOG.md; then
  echo "error: CHANGELOG.md already has a section for $version" >&2
  exit 1
fi
if ! grep -q "^## \[Unreleased\]" CHANGELOG.md; then
  echo "error: CHANGELOG.md has no '## [Unreleased]' section" >&2
  exit 1
fi

if [ "$skip_tests" != "--no-tests" ]; then
  uv run pytest -q
fi

today="$(date +%Y-%m-%d)"
# New empty Unreleased section on top, the old one becomes the release section.
sed -i "s/^## \[Unreleased\]/## [Unreleased]\n\n## [$version] - $today/" CHANGELOG.md
sed -i "s/^version = \".*\"/version = \"$version\"/" pyproject.toml
uv lock >/dev/null 2>&1 || true

git add CHANGELOG.md pyproject.toml uv.lock
git commit -m "Release $tag"
git tag -a "$tag" -m "RaveBot $tag"

echo
echo "Release $tag is prepared. To build and publish it, run:"
echo "  git push origin main $tag"
