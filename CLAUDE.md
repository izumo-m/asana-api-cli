# asana-api-cli

Python CLI wrapping the `python-asana` SDK. The command tree is built at runtime by introspecting the installed `asana` package — no codegen.

## Project constitution & terminology

@docs/principles.md

## Commands

- Sync deps: `uv sync`
- Run the CLI from source: `uv run asana-api ...`
- Tests: see `tests/README.md`

## Editing committed files

- Use English.
- Write `README.md` and `CHANGELOG.md` from the user's perspective.

## Read before working in these areas

- Editing `src/asana_api_cli/*.py` or any CLI surface change → `docs/architecture.md`
- Adding / renaming / changing the SDK mapping of a built-in CLI option → also update `docs/cli-sdk-mapping.md`
- Publishing to PyPI (`chore: release X.Y.Z`) → `docs/release.md`
- Bumping the `asana` SDK version → `docs/development.md` §Bumping the asana SDK
