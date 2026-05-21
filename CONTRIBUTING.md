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

## Code style

- Format and lint with `ruff`:

  ```bash
  uv run ruff format .
  uv run ruff check .
  ```

- Type-check with `basedpyright`:

  ```bash
  uv run basedpyright
  ```

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
- Update `README.md` and `CHANGELOG.md` when user-visible behavior changes.
- Use [Conventional Commits](https://www.conventionalcommits.org/) style for
  commit messages (e.g. `feat:`, `fix:`, `docs:`, `chore:`).

## Reporting bugs

Please open an issue at
<https://github.com/izumo-m/asana-api-cli/issues> with a minimal
reproduction and the output of `asana-api --version`.
