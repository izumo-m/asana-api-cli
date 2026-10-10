#!/usr/bin/env bash
# Bring the development-side dependencies in uv.lock up to date.
#
# `uv sync --upgrade-group dev` alone moves only the dev group's direct entries
# (ruff, pytest, ...); packages that only the dev group pulls in
# (cryptography, nodejs-wheel-binaries, readme-renderer, ...) stay at their
# locked versions. This script also upgrades those: every package the dev
# group reaches but the runtime dependencies do not. Anything the runtime
# dependencies reach (asana, click, urllib3, certifi, ...) stays put — the
# released package resolves those at install time, and an asana upgrade goes
# through docs/development.md §Bumping the asana SDK.
#
# Rewrites uv.lock and syncs .venv. Review the resolver output (e.g. a yanked
# package warning) and the uv.lock diff before committing.
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

dev_only=$(LC_ALL=C comm -23 <(locked_names --only-group dev) <(locked_names --no-dev))

args=(--upgrade-group dev)
count=0
for pkg in $dev_only; do
  args+=(--upgrade-package "$pkg")
  count=$((count + 1))
done

echo "Upgrading the dev group and its $count dev-only packages (runtime dependencies stay put)"
uv sync "${args[@]}"
