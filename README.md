# asana-api-cli

A CLI tool for the Asana API. It thinly wraps the official
[python-asana](https://github.com/Asana/python-asana) SDK with click, exposing
every API endpoint from the command line via `asana-api <group> <command>`.

The CLI modules are auto-generated from the SDK by
[`tools/codegen.py`](tools/README.md). When the SDK version is bumped, a single
regeneration picks up any new endpoints.

## Install from source

```bash
# For development (run inside a virtualenv)
uv sync

# Install into ~/.local/bin
pipx install .
```

## Environment variables

| Name | Required | Description |
|------|----------|-------------|
| `ASANA_ACCESS_TOKEN` | Yes (at runtime only) | Asana Personal Access Token |

The token can be issued from the
[Asana Developer Console](https://app.asana.com/0/developer-console).
No token is needed for `--help` or argument-error output.

```bash
export ASANA_ACCESS_TOKEN="1/12345..."
```

## Usage (examples)

```bash
# Show version
asana-api --version

# List commands
asana-api --help
asana-api tasks --help
asana-api tasks get-tasks --help

# List workspaces
asana-api workspaces get-workspaces

# List projects
asana-api projects get-projects --workspace <WORKSPACE_GID>

# List tasks (first page)
asana-api tasks get-tasks --project <PROJECT_GID>

# Auto-fetch all pages
asana-api tasks get-tasks --project <PROJECT_GID> --paginate

# Single task
asana-api tasks get-task <TASK_GID>

# Create a task (body is a JSON string)
asana-api tasks create-task --body '{"data":{"name":"new task","projects":["<PID>"]}}'

# Output formats
asana-api tasks get-tasks --project <PID> --output table
asana-api tasks get-tasks --project <PID> --query '.data' --output csv
```

## Project layout

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

## License

[Apache License 2.0](LICENSE)
