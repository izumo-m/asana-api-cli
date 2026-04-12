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

# Clean previous build artifacts
rm -rf "$DIST"

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
