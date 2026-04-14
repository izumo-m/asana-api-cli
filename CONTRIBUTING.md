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

- Messages printed to the console (errors, help text, etc.) must be written
  in English.

## Generated code

The CLI command modules under `src/asana_api_cli/cli/` are **auto-generated**
by `tools/codegen.py` and must not be edited by hand. To change CLI behavior,
edit `tools/codegen.py` and regenerate:

```bash
uv run python tools/codegen.py
```

See [`docs/development.md`](docs/development.md) for the project layout and
which modules are hand-written.

## Pull requests

- Keep changes focused and small.
- Update `README.md` and `CHANGELOG.md` when user-visible behavior changes.
- Use [Conventional Commits](https://www.conventionalcommits.org/) style for
  commit messages (e.g. `feat:`, `fix:`, `docs:`, `chore:`).

## Reporting bugs

Please open an issue at
<https://github.com/izumo-m/asana-api-cli/issues> with a minimal
reproduction and the output of `asana-api --version`.
