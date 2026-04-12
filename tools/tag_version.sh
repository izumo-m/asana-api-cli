#!/usr/bin/env bash
# Create a version tag on the current commit.
#
# Reads the version from pyproject.toml and creates an annotated git tag.
# Prompts for confirmation before creating the tag.
#
# Usage:
#   bash tools/tag_version.sh
set -euo pipefail

PYPROJECT="$(cd "$(dirname "$0")/.." && pwd)/pyproject.toml"

# Read current version from pyproject.toml
version=$(grep -oP '^version\s*=\s*"\K[^"]+' "$PYPROJECT")
if [[ -z "$version" ]]; then
  echo "error: could not find version in pyproject.toml" >&2
  exit 1
fi

tag="v${version}"

# Check that the tag doesn't already exist
if git tag -l "$tag" | grep -q "^${tag}$"; then
  echo "error: tag $tag already exists" >&2
  exit 1
fi

# Show context
commit=$(git log -1 --format='%h %s')
echo "Version : $version"
echo "Tag     : $tag"
echo "Commit  : $commit"
echo ""

# Confirm
read -rp "Create tag $tag? [y/N] " answer
if [[ "$answer" != [yY] ]]; then
  echo "aborted"
  exit 0
fi

git tag -a "$tag" -m "Release ${version}"
echo "created tag $tag"
