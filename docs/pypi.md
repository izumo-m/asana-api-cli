# asana-api-cli

A CLI tool for the Asana API. It thinly wraps the official
[python-asana](https://github.com/Asana/python-asana) SDK with click, exposing
every API endpoint from the command line via `asana-api <group> <command>`.

## Installation

```bash
pip install asana-api-cli

# or, to install as an isolated CLI tool
pipx install asana-api-cli
```

## Environment variables

| Name | Required | Description |
|------|----------|-------------|
| `ASANA_ACCESS_TOKEN` | Yes (at runtime only) | Asana Personal Access Token |
| `ASANA_DEFAULT_WORKSPACE` | No | Default workspace GID used when `--workspace` is omitted |

The token can be issued from the
[Asana Developer Console](https://app.asana.com/0/developer-console).
No token is needed for `--help` or argument-error output.

```bash
export ASANA_ACCESS_TOKEN="1/12345..."
export ASANA_DEFAULT_WORKSPACE="12345678"   # optional
```

## Usage

```bash
# Show version
asana-api --version

# List commands
asana-api --help
asana-api tasks --help
asana-api tasks get-tasks --help

# List workspaces
asana-api workspaces get-workspaces

# List projects (workspace resolved from ASANA_DEFAULT_WORKSPACE)
asana-api projects get-projects-for-workspace
asana-api projects get-projects --workspace <WORKSPACE_GID>

# Explicitly skip workspace even when a default is configured
asana-api projects get-projects --no-workspace

# List tasks (first page)
asana-api tasks get-tasks --project <PROJECT_GID>

# Auto-fetch all pages
asana-api tasks get-tasks --project <PROJECT_GID> --paginate

# Single task (--task instead of positional argument)
asana-api tasks get-task --task <TASK_GID>

# Create a task (body is a JSON string)
asana-api tasks create-task --body '{"data":{"name":"new task","projects":["<PID>"]}}'

# Output formats
asana-api tasks get-tasks --project <PID> --output table
asana-api tasks get-tasks --project <PID> --query '.data' --output csv
```

### Workspace resolution

Many API endpoints require a workspace. The CLI resolves it in this order:

1. `--workspace <GID>` on the command
2. `ASANA_DEFAULT_WORKSPACE` environment variable

For endpoints where workspace is truly optional (query parameter, not path
parameter), `--no-workspace` suppresses any default so the parameter is not
sent at all.

## License

[Apache License 2.0](https://github.com/izumo-m/asana-api-cli/blob/main/LICENSE)
