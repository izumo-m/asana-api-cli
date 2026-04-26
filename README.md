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
| `ASANA_DEFAULT_WORKSPACE` | No | Default workspace GID for endpoints that require it |

The token can be issued from the
[Asana Developer Console](https://app.asana.com/0/developer-console).
No token is needed for `--help` or argument-error output.

```bash
export ASANA_ACCESS_TOKEN="1/12345..."
export ASANA_DEFAULT_WORKSPACE="12345678"   # optional
```

## Shell completion

`asana-api` is built with Click, which supports dynamic shell completion.
To enable bash completion, add the following line to your `~/.bashrc`:

```bash
eval "$(_ASANA_API_COMPLETE=bash_source asana-api)"
```

Then reload the shell (`source ~/.bashrc` or open a new terminal). Pressing
`<TAB>` after `asana-api` will now complete subcommands and options.

For `zsh` or `fish`, replace `bash_source` with `zsh_source` or `fish_source`
and add the line to `~/.zshrc` or `~/.config/fish/config.fish` respectively.

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

# List tasks (first page only, default)
asana-api tasks get-tasks --project <PROJECT_GID>

# Preview the first few items
asana-api tasks get-tasks --project <PROJECT_GID> --max-items 5

# Fetch up to N items across pages (auto-paginates)
asana-api tasks get-tasks --project <PROJECT_GID> --max-items 250

# Fetch all items (no cap)
asana-api tasks get-tasks --project <PROJECT_GID> --all-items

# Tune per-page request size (advanced)
asana-api tasks get-tasks --project <PROJECT_GID> --page-size 50 --all-items

# Manual pagination: walk pages yourself via --offset (token from
# `next_page.offset` of the previous response)
asana-api tasks get-tasks --project <PROJECT_GID> --page-size 50 --offset <TOKEN>

# Single task (--task instead of positional argument)
asana-api tasks get-task --task <TASK_GID>

# Create a task (body is a JSON string)
asana-api tasks create-task --body '{"data":{"name":"new task","projects":["<PID>"]}}'

# Output formats
asana-api tasks get-tasks --project <PID> --output table
asana-api tasks get-tasks --project <PID> --query '.data' --output csv
```

### Workspace resolution

Many API endpoints require a workspace. For those endpoints (e.g.
`get-projects-for-workspace`), the CLI resolves it in this order:

1. `--workspace <GID>` on the command
2. `ASANA_DEFAULT_WORKSPACE` environment variable

For endpoints where workspace is optional (e.g. `get-tasks`), the env-var
fallback is **not** used — pass `--workspace` explicitly if needed. This
prevents conflicts with other scope parameters like `--project` that are
mutually exclusive with workspace in the Asana API.

## Development

See [docs/development.md](https://github.com/izumo-m/asana-api-cli/blob/main/docs/development.md)
for building from source, project layout, and library usage.

## License

[MIT License](https://github.com/izumo-m/asana-api-cli/blob/main/LICENSE)
