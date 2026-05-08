#!/usr/bin/env bash
# Build and publish the package to PyPI.
#
# Runs `python -m build` to create sdist + wheel, then uploads with twine.
# Username and password are entered interactively (use __token__ + API token).
#
# Usage:
#   bash tools/publish_pypi.sh          # upload to PyPI (production)
#   bash tools/publish_pypi.sh --test   # upload to TestPyPI
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYPROJECT="${ROOT}/pyproject.toml"
DIST="${ROOT}/dist"
BUILD="${ROOT}/build"
EGG_INFO_GLOB="${ROOT}/src/*.egg-info"

# Read current version from pyproject.toml
version=$(grep -oP '^version\s*=\s*"\K[^"]+' "$PYPROJECT")
if [[ -z "$version" ]]; then
  echo "error: could not find version in pyproject.toml" >&2
  exit 1
fi

# Determine upload target
repo_url=""
repo_label="PyPI"
if [[ "${1:-}" == "--test" ]]; then
  repo_url="https://test.pypi.org/legacy/"
  repo_label="TestPyPI"
fi

echo "Package : asana-api-cli"
echo "Version : ${version}"
echo "Target  : ${repo_label}"
echo ""

# Run tests before publishing
echo "--- Running tests ---"
(cd "$ROOT" && uv run pytest -q) || {
  echo "error: tests failed, aborting publish" >&2
  exit 1
}
echo ""

# Clean previous build artifacts. setuptools reuses build/ as a staging dir,
# so leftover files from a prior layout (e.g. before the v2.0.0 refactor) can
# end up in the new wheel. Wipe build/, dist/, and egg-info to force a clean
# build every time.
rm -rf "$DIST" "$BUILD"
# shellcheck disable=SC2086
rm -rf $EGG_INFO_GLOB

# Build
echo "--- Building ---"
(cd "$ROOT" && python -m build) || {
  echo "error: build failed" >&2
  exit 1
}
echo ""

# Show what will be uploaded
echo "--- Artifacts ---"
ls -lh "$DIST"
echo ""

# Confirm
read -rp "Upload to ${repo_label}? [y/N] " answer
if [[ "$answer" != [yY] ]]; then
  echo "aborted"
  exit 0
fi

# Upload
echo ""
if [[ -n "$repo_url" ]]; then
  python -m twine upload --repository-url "$repo_url" "$DIST"/*
else
  python -m twine upload "$DIST"/*
fi
