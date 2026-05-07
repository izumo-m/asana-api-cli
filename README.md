# asana-api-cli

A CLI for the Asana API. It thinly wraps the official
[python-asana](https://github.com/Asana/python-asana) SDK with click, exposing
every endpoint as `asana-api <group> <command>`.

## Why asana-api-cli

- **Near-complete SDK coverage.** Almost every method on every `*Api` class in
  python-asana is available as a CLI command. The command tree is built at
  runtime from the installed `asana` package, so new APIs surface as the
  upstream library evolves — no asana-api-cli release required.
- **Tracks the SDK version you actually use.** Because commands are
  introspected from whatever `asana` is installed alongside, the CLI surface
  matches the SDK version pinned in your project. When using asana-api-cli
  as a dev-dependency, `pip install -U asana` updates the CLI's available
  endpoints in lockstep with your application code.
- **SDK-compatible arguments and output.** Command arguments map to
  python-asana method parameters (with minor naming adjustments — hyphens
  become underscores, group names become PascalCase API classes), and JSON
  output matches the SDK's response shape. The CLI makes it easy to iterate:
  try different arguments, inspect the response, and refine until you
  understand the endpoint's behavior. Once verified, port the call into your
  Python app — far fewer surprises on the first integration.

## Installation

```bash
pip install asana-api-cli
```

For best results, install asana-api-cli into the same Python environment
that holds your project's `python-asana` so the CLI surface tracks the
exact SDK version your application uses (see [As a
dev-dependency](#as-a-dev-dependency) below).

### As a dev-dependency

If your project already uses `python-asana`, add asana-api-cli to your dev
group so the CLI tracks the same SDK version your application code uses:

```toml
# pyproject.toml
[project]
dependencies = ["asana>=5.3,<6"]

[dependency-groups]  # uv
dev = ["asana-api-cli"]
```

```toml
# Poetry
[tool.poetry.group.dev.dependencies]
asana-api-cli = "*"
```

After `uv sync` (or equivalent), `asana-api` resolves to the project's
`.venv` and introspects whatever `asana` version is locked there. Tests
prototyped with `asana-api tasks ...` will exactly match the SDK calls in
your app.

### Installing globally with pipx

If you would rather isolate asana-api-cli from any project's dependencies
— for example, when you administer Asana from the shell without writing
Python — install it with [pipx](https://pipx.pypa.io/):

```bash
pipx install asana-api-cli
```

In this setup the CLI uses the `python-asana` version that shipped with
the asana-api-cli release; `pipx upgrade asana-api-cli` updates only
asana-api-cli itself, not the bundled `python-asana`. To pull a newer
`python-asana` into the existing pipx install without reinstalling the
CLI:

```bash
pipx runpip asana-api-cli install -U asana
```

The next `asana-api` run sees the new SDK and any newly added endpoints
automatically.

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
# Version and help
asana-api --version
asana-api --help
asana-api tasks --help
asana-api tasks get-tasks --help

# List workspaces and projects
asana-api workspaces get-workspaces
asana-api projects get-projects-for-workspace
asana-api projects get-projects --workspace <WORKSPACE_GID>

# List tasks (first page only by default)
asana-api tasks get-tasks --project <PROJECT_GID>

# Preview the first few items
asana-api tasks get-tasks --project <PROJECT_GID> --max-items 5

# Fetch every item across pages
asana-api tasks get-tasks --project <PROJECT_GID> --all-items

# Single task
asana-api tasks get-task --task <TASK_GID>

# Create a task (body is a JSON string)
asana-api tasks create-task --body '{"data":{"name":"new task","projects":["<PID>"]}}'

# Output formats
asana-api tasks get-tasks --project <PID> --output table
asana-api tasks get-tasks --project <PID> --query '.data' --output csv
```

See [Pagination](#pagination) for fetching across pages and
[Global options](#global-options) for `--debug`, `--access-token`, etc.

### Workspace resolution

Many API endpoints require a workspace. For those endpoints (e.g.
`get-projects-for-workspace`), the CLI resolves it in this order:

1. `--workspace <GID>` on the command
2. `ASANA_DEFAULT_WORKSPACE` environment variable

For endpoints where workspace is optional (e.g. `get-tasks`), the env-var
fallback is **not** used — pass `--workspace` explicitly if needed. This
prevents conflicts with other scope parameters like `--project` that are
mutually exclusive with workspace in the Asana API.

## Pagination

Listing endpoints (e.g. `tasks get-tasks`) return paginated results. The CLI
provides four ways to control how much is fetched:

| Option | Behavior |
|--------|----------|
| (none) | Fetch a single page (Asana default: 100 items) |
| `--max-items N` | Fetch up to N items, auto-paginating across pages. The last request is automatically capped to the remaining count. |
| `--all-items` | Fetch every page until the server reports no more |
| `--offset <TOKEN>` | Manual pagination: pass the `next_page.offset` token from the previous response |

`--max-items` and `--all-items` are mutually exclusive.

`--page-size N` tunes the per-page request size (Asana API requires 1-100,
default 100). Rarely needed — combine with `--all-items` or `--max-items` only
when the default doesn't suit (e.g. very large rows or rate-limit tuning).

```bash
# Auto-paginate up to 250 items
asana-api tasks get-tasks --project <PID> --max-items 250

# Fetch everything
asana-api tasks get-tasks --project <PID> --all-items

# Manual pagination using the offset token
asana-api tasks get-tasks --project <PID> --offset <TOKEN>
```

## Global options

These options work at any level of the command tree, so the following are
equivalent:

```bash
asana-api --debug tasks get-tasks --project <PID>
asana-api tasks get-tasks --project <PID> --debug
```

When the same option is given at multiple levels, the more specific (later)
one wins.

| Option | Description |
|--------|-------------|
| `--access-token TOKEN` | Asana personal access token (default: `$ASANA_ACCESS_TOKEN`) |
| `--host URL` | Override API base URL (default: `https://app.asana.com/api/1.0`) |
| `--proxy URL` | HTTP/HTTPS proxy URL |
| `--no-verify-ssl` | Disable TLS certificate verification (insecure) |
| `--ca-cert PATH` | Path to a PEM bundle of trusted CA certificates |
| `--retries N` | Number of retries on 429/5xx responses (default: 5) |
| `--timeout SECONDS` | Per-request timeout in seconds |
| `--temp-dir PATH` | Directory for temporary downloads |
| `--debug` | Print HTTP request/response to stderr for troubleshooting |

## Development

See [docs/development.md](https://github.com/izumo-m/asana-api-cli/blob/main/docs/development.md)
for building from source, project layout, and library usage.

## License

[MIT License](https://github.com/izumo-m/asana-api-cli/blob/main/LICENSE)
