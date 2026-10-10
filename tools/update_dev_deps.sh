#!/usr/bin/env bash
# Bring the development-side dependencies in uv.lock up to date.
#
# Upgrades every package the dev group reaches but the runtime dependencies do
# not — the group's own entries (ruff, pytest, ...) and the packages only they
# pull in (cryptography, nodejs-wheel-binaries, readme-renderer, ...), which
# `uv sync --upgrade-group dev` alone leaves at their locked versions. Anything
# the runtime dependencies reach (asana, click, urllib3, certifi, ...) stays
# put, even when a dev entry also needs it — the released package resolves
# those at install time, and an asana upgrade goes through
# docs/development.md §Bumping the asana SDK.
#
# Rewrites uv.lock and syncs .venv, then lists the dev group's entries that a
# pyproject.toml bound still holds below their latest release (e.g. a new major
# behind `<7`). Review the resolver output (e.g. a yanked package warning) and
# the uv.lock diff before committing.
#
# Usage:
#   bash tools/update_dev_deps.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Names of the locked packages `uv export` selects with the given options,
# across all platforms and Python versions (markers are dropped).
locked_names() {
  uv export --frozen --no-emit-project --no-hashes --no-header --no-annotate "$@" |
    sed -n 's/^\([A-Za-z0-9._-]*\)==.*/\1/p' |
    LC_ALL=C sort -u
}

# Plain command substitutions, so a failing `uv export` stops the script
# (a failure inside a process substitution would go unnoticed).
dev=$(locked_names --only-group dev)
runtime=$(locked_names --no-dev)
if [[ -z "$dev" || -z "$runtime" ]]; then
  echo "error: uv export listed no dev or no runtime packages" >&2
  exit 1
fi
dev_only=$(LC_ALL=C comm -23 <(printf '%s\n' "$dev") <(printf '%s\n' "$runtime"))

args=(sync)
count=0
for pkg in $dev_only; do
  args+=(--upgrade-package "$pkg")
  count=$((count + 1))
done

echo "Upgrading $count dev-only packages; runtime dependencies stay put"
uv "${args[@]}"

# Direct dev entries still behind their latest release after the upgrade are
# held there by a bound in pyproject.toml.
held=$(uv tree --quiet --outdated --only-group dev --depth 1 --frozen |
  sed -n 's/^[^a-z]*\(.* (group: dev) (latest: .*)\)$/  \1/p')
if [[ -n "$held" ]]; then
  echo
  echo "Held below their latest release by a pyproject.toml bound (raise it deliberately):"
  echo "$held"
fi
