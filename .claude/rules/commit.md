# Commit checklist

## Commit subject

Use Conventional Commits prefixes (`feat:` / `fix:` / `docs:` / `chore:` / `refactor:` / `test:` …).

## Pre-commit checks

For every change category that applies, run the checks below and fix everything reported.

### Python sources (`*.py`) changed

1. `uv run basedpyright` (no path args — must scan the whole project). Resolve every error.
2. `uv run ruff check --fix .`.
3. `uv run ruff format .`.
