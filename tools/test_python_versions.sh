#!/usr/bin/env bash
# Run the test suite on several Python versions.
#
# Each version gets a throwaway virtualenv (removed on exit), so the project's
# .venv and uv.lock are left untouched. Dependencies are installed at the
# versions pinned in uv.lock (--locked), and the interpreter is always a
# uv-managed CPython (--managed-python; downloaded on first use), so results do
# not depend on whatever python happens to be on PATH.
#
# Every version runs even if an earlier one fails; the exit status is non-zero
# if any version failed.
#
# Usage:
#   bash tools/test_python_versions.sh                     # 3.10, 3.12, 3.14
#   bash tools/test_python_versions.sh 3.11 3.13           # chosen versions
#   bash tools/test_python_versions.sh -- -x tests/test_codegen.py
#                                                          # extra pytest args
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEFAULT_VERSIONS=(3.10 3.12 3.14)

versions=()
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--" ]]; then
    shift
    break
  fi
  versions+=("$1")
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

results=()
failed=0
for v in "${versions[@]}"; do
  echo "=== Python ${v} ==="
  env_dir="$(mktemp -d)"
  envs+=("$env_dir")
  # UV_LINK_MODE=copy: the throwaway env usually sits on another filesystem
  # than uv's cache, where uv would warn and fall back to copying anyway.
  if (cd "$ROOT" && UV_PROJECT_ENVIRONMENT="$env_dir" UV_LINK_MODE="${UV_LINK_MODE:-copy}" \
      uv run --python "$v" --locked --managed-python \
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
