# Contributing

Thanks for your interest in contributing to `asana-api-cli`.

## Development setup

This project uses [uv](https://docs.astral.sh/uv/) for dependency management.

```bash
git clone https://github.com/izumo-m/asana-api-cli.git
cd asana-api-cli
uv sync
```

## Running tests

```bash
uv run pytest
```

End-to-end tests under `tests/e2e/` replay against committed VCR cassettes
by default (no network or Asana account needed); running them live against
the real Asana API is opt-in. See [`tests/e2e/README.md`](tests/e2e/README.md)
for the live / replay workflow and the one-time workspace provisioning step.

## Pre-commit checks

Before creating a git commit, run the checks below for every change category
that applies and resolve everything reported.

### Python sources (`*.py`) changed

```bash
uv run basedpyright            # no path args — must scan the whole project
uv run ruff check --fix .
uv run ruff format .
```

## Code style

- Messages printed to the console (errors, help text, etc.) must be written
  in English.

## CLI surface

The CLI command tree is built **at runtime** by introspecting the installed
`python-asana` SDK in `src/asana_api_cli/cli.py`. There is no codegen step.
To change CLI behavior (option naming, pagination wiring, error handling,
etc.) edit `cli.py` directly.

When the bundled `asana` SDK version changes (`pyproject.toml`), the CLI
surface may shift. The snapshot test at `tests/test_cli_surface.py` pins
the expected shape; an SDK bump that adds, removes, or renames endpoints
will fail the test until the fixture at `tests/fixtures/cli_surface.json`
is regenerated and the changes are reflected in `CHANGELOG.md`.

See [`docs/development.md`](docs/development.md) for the project layout.

## Pull requests

- Keep changes focused and small.
- Write `README.md` and `CHANGELOG.md` from the user's perspective.
- Use [Conventional Commits](https://www.conventionalcommits.org/) style for
  commit messages (e.g. `feat:`, `fix:`, `docs:`, `chore:`).

## Reporting bugs

Please open an issue at
<https://github.com/izumo-m/asana-api-cli/issues> with a minimal
reproduction and the output of `asana-api --version`.
