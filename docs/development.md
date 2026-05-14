# Development

For contributor setup (cloning, `uv sync`, running tests, code style, PR
rules), see [`CONTRIBUTING.md`](../CONTRIBUTING.md). This document covers
the internal architecture and how to use `asana-api-cli` as a library.

## Install from source

```bash
pipx install .
```

## Project layout

The CLI command tree is built **at runtime** by introspecting the installed
`python-asana` SDK. There is no codegen step. When the SDK is upgraded, the
CLI surface follows automatically; the snapshot test guards against silent
breaking changes.

```
src/asana_api_cli/
├── __init__.py           # Re-exports AsanaSession
├── session.py            # Thin wrapper around asana.ApiClient
├── formatter.py          # CLI output formatting (@formatted decorator)
├── click_ext.py          # LazyGroup + global-options propagation mixins
├── version.py            # version_string()
└── cli.py                # Runtime introspection + click command tree

tests/
├── test_cli.py                 # Helpers + built-command shape tests
├── test_cli_surface.py         # Snapshot test (compares against fixture)
└── fixtures/
    └── cli_surface.json        # Canonical CLI surface for the bundled SDK
```

- **`session.py`** — builds `asana.Configuration` + `ApiClient`, toggles
  `return_page_iterator` for `--all-items`, and exposes `resolve_body` /
  `resolve_workspace`.
- **`formatter.py`** — supports `json` / `table` / `csv` / `text` output and
  `--query` (jq).
- **`click_ext.py`** — `LazyGroup` for cheap top-level help, plus the
  `GroupWithGlobalOptions` / `CommandWithGlobalOptions` pair that lets
  `--debug`, `--access-token`, etc. work at any level of the tree.
- **`cli.py`** — introspects every `*Api` class on the installed `asana`
  package and builds click commands per method. Method-level introspection
  is deferred per group so top-level `--help` is cheap.

## Bumping the SDK

1. Edit `dependencies` in `pyproject.toml` to raise the lower bound of the
   `asana` constraint (e.g. from `asana>=5.2,<6` to `asana>=5.3,<6` once
   5.3 ships).
2. `uv sync` to install the new SDK.
3. `uv run pytest` — if the SDK surface changed, `test_cli_surface.py`
   fails with the diff against the recorded fixture.
4. Review the diff. Note user-visible changes in `CHANGELOG.md`.
5. Regenerate the fixture (see the docstring at the top of
   `tests/test_cli_surface.py` for the exact command).
6. Commit `pyproject.toml`, `uv.lock`, `tests/fixtures/cli_surface.json`,
   and `CHANGELOG.md` together.

## Trying shell completion locally

`asana-api` is built with click, which generates dynamic completion scripts.
To experiment with completion without touching your real shell config, spawn
an isolated sub-shell via `uv run` and install completion only inside it:

```bash
uv run $SHELL
```

`uv run $SHELL` puts `.venv/bin` on `PATH` so `asana-api` is callable
directly. Inside the sub-shell, evaluate the appropriate completion source
for your shell:

```bash
# bash
eval "$(_ASANA_API_COMPLETE=bash_source asana-api)"

# zsh
eval "$(_ASANA_API_COMPLETE=zsh_source asana-api)"

# fish
_ASANA_API_COMPLETE=fish_source asana-api | source
```

Then try interactive completion:

```text
asana-api tasks get-tasks --<TAB><TAB>      # all options, including --debug etc.
asana-api tasks get-tasks --de<TAB>         # completes to --debug
asana-api tasks --<TAB><TAB>                # global options also work on subgroups
asana-api tasks get-tasks --ca-cert <TAB>   # path completion for FILE-typed options
```

Exit with `exit` (or Ctrl-D) to drop completion and return to your normal
shell. Nothing is persisted.

For a quick non-interactive smoke test that doesn't need a sub-shell, drive
the bash completion protocol directly:

```bash
COMP_WORDS="asana-api tasks get-tasks --" COMP_CWORD=3 \
  _ASANA_API_COMPLETE=bash_complete uv run asana-api
```

This prints the candidate list as `type,value` lines.

## Using as a library

This project exists to provide a CLI, but calling the SDK directly from Python
is the normal approach:

```python
import asana

config = asana.Configuration()
config.access_token = "1/12345..."
client = asana.ApiClient(config)

tasks_api = asana.TasksApi(client)
for task in tasks_api.get_tasks({"project": "123", "limit": 50}):
    print(task)
```

You can also go through `AsanaSession`:

```python
from asana_api_cli import AsanaSession
import asana

session = AsanaSession(token="1/12345...", paginate=True)
tasks_api = asana.TasksApi(session.client)
for task in tasks_api.get_tasks({"project": "123"}):
    print(task)
```
