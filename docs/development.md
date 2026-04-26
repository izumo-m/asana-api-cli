# Development

For contributor setup (cloning, `uv sync`, running tests, code style, PR
rules), see [`CONTRIBUTING.md`](../CONTRIBUTING.md). This document covers
the internal architecture and how to use `asana-api-cli` as a library.

## Install from source

```bash
pipx install .
```

## Project layout

The CLI modules are auto-generated from the SDK by
[`tools/codegen.py`](../tools/README.md). When the SDK version is bumped, a
single regeneration picks up any new endpoints.

```
src/asana_api_cli/
├── __init__.py           # Re-exports AsanaSession
├── session.py            # Thin wrapper around asana.ApiClient (hand-written)
├── formatter.py          # CLI output formatting (@formatted decorator, hand-written)
└── cli/                  # CLI layer (auto-generated)
    ├── __init__.py       # Main group + add_command for each tag
    ├── tasks.py          # click commands wrapping TasksApi
    ├── projects.py
    └── ...               # One file per SDK *Api class
```

- **`session.py`** — hand-written. Builds `asana.Configuration` + `ApiClient`
  and toggles `return_page_iterator` for `--paginate`.
- **`formatter.py`** — hand-written. Supports `json` / `table` / `csv` / `text`
  output and `--query` (jq).
- **`cli/`** — auto-generated. Walks the official SDK's `*Api` classes and
  emits click command groups.

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
is the normal path:

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
