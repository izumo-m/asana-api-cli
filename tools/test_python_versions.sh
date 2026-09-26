#!/usr/bin/env bash
# Run the test suite on several Python versions.
#
# Each version gets a throwaway virtualenv (removed on exit), so the project's
# .venv and uv.lock are left untouched. Dependencies are installed at the
# versions pinned in uv.lock (--locked) — or, with --lowest, at the minimum
# versions pyproject.toml declares for the direct dependencies
# (--resolution lowest-direct --isolated). The interpreter is always a
# uv-managed CPython (--managed-python; downloaded on first use), so results do
# not depend on whatever python happens to be on PATH. The installed versions of
# the runtime dependencies are printed before each run.
#
# Every version runs even if an earlier one fails; the exit status is non-zero
# if any version failed.
#
# Usage:
#   bash tools/test_python_versions.sh                     # 3.10, 3.12, 3.14
#   bash tools/test_python_versions.sh 3.11 3.13           # chosen versions
#   bash tools/test_python_versions.sh --lowest            # declared minimums
#   bash tools/test_python_versions.sh -- -x tests/test_codegen.py
#                                                          # extra pytest args
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEFAULT_VERSIONS=(3.10 3.12 3.14)

versions=()
resolution=(--locked)
mode="uv.lock"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --)
      shift
      break
      ;;
    --lowest)
      resolution=(--resolution lowest-direct --isolated)
      mode="lowest direct dependencies"
      ;;
    -*)
      echo "error: unknown option: $1 (pytest options go after --)" >&2
      exit 2
      ;;
    *)
      versions+=("$1")
      ;;
  esac
  shift
done
if [[ ${#versions[@]} -eq 0 ]]; then
  versions=("${DEFAULT_VERSIONS[@]}")
fi
# Expanded as ${pytest_args[@]+"${pytest_args[@]}"} below: a bare
# "${pytest_args[@]}" on an empty array trips `set -u` in bash < 4.4 (macOS).
pytest_args=("$@")

envs=()
# shellcheck disable=SC2329  # invoked via the EXIT trap
cleanup() {
  if [[ ${#envs[@]} -gt 0 ]]; then
    rm -rf "${envs[@]}"
  fi
}
trap cleanup EXIT

# Prints "name=version" for each runtime dependency, read from the installed
# package metadata so it never drifts from pyproject.toml.
SHOW_DEPS='
import re
from importlib.metadata import requires, version
names = [re.split(r"[\s<>=!~;\[]", r, maxsplit=1)[0] for r in requires("asana-api-cli") or []]
print("deps: " + " ".join(f"{n}={version(n)}" for n in names))
'

results=()
failed=0
for v in "${versions[@]}"; do
  echo "=== Python ${v} (${mode}) ==="
  env_dir="$(mktemp -d)"
  envs+=("$env_dir")
  # UV_LINK_MODE=copy: the throwaway env usually sits on another filesystem
  # than uv's cache, where uv would warn and fall back to copying anyway.
  if (cd "$ROOT" && export UV_PROJECT_ENVIRONMENT="$env_dir" UV_LINK_MODE="${UV_LINK_MODE:-copy}" \
      && uv run -q --python "$v" "${resolution[@]}" --managed-python python -c "$SHOW_DEPS" \
      && uv run -q --python "$v" "${resolution[@]}" --managed-python \
        pytest -q ${pytest_args[@]+"${pytest_args[@]}"}); then
    results+=("  ${v}: passed")
  else
    results+=("  ${v}: FAILED")
    failed=1
  fi
  echo ""
done

echo "=== Summary ==="
printf '%s\n' "${results[@]}"
exit "$failed"
