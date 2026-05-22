# asana-api-cli

A CLI that exposes **every method of the official
[`python-asana`](https://github.com/Asana/python-asana) SDK** as
`asana-api <group> <command>`. The command tree is generated at runtime
from the installed `asana` package, automatically tracking whatever
SDK version is installed in the same environment.

## Why asana-api-cli

- **Complete SDK coverage.** Every method of every `*Api` class in
  `python-asana` becomes a CLI command. Because the tree is introspected
  from the installed `asana` package, new methods surface the moment
  upstream ships them — no `asana-api-cli` release required.
- **Tracks the SDK version you actually use.** Because commands are
  introspected from whatever `asana` is installed in the same environment,
  the CLI surface matches the SDK version pinned in your project. When using
  `asana-api-cli` as a dev-dependency, `pip install -U asana` updates the
  CLI's available commands in lockstep with your application code.
- **SDK-compatible arguments and output.** Command arguments map to
  `python-asana` method parameters (with minor naming adjustments — hyphens
  become underscores, group names map back to PascalCase `*Api` class
  names), and JSON output matches the SDK's response shape. The CLI makes
  it easy to iterate: try different arguments, inspect the response, and
  refine until you understand the endpoint's behavior. Once verified,
  translate the call into the equivalent `python-asana` invocation in your
  app — far fewer surprises on the first integration.

## Installation

```bash
pip install asana-api-cli
```

For best results, install `asana-api-cli` into the same Python environment
that holds your project's `python-asana` so the CLI surface tracks the
exact SDK version your application uses (see [As a
dev-dependency](#as-a-dev-dependency) below).

### As a dev-dependency

If your project already uses `python-asana`, add `asana-api-cli` to your dev
group so the CLI tracks the same SDK version your application code uses:

```toml
# pyproject.toml
[project]
dependencies = ["asana>=5.2,<6"]

[dependency-groups]  # uv
dev = ["asana-api-cli"]
```

```toml
# Poetry
[tool.poetry.group.dev.dependencies]
asana-api-cli = "*"
```

After `uv sync` (or equivalent), `asana-api` resolves to the project's
`.venv` and introspects whatever `asana` version is locked there. Calls
prototyped with `asana-api tasks ...` translate directly to the SDK calls
you'll write in your app.

### Installing globally with pipx

If you would rather isolate `asana-api-cli` from any project's dependencies
— for example, when you administer Asana from the shell without writing
Python — install it with [pipx](https://pipx.pypa.io/):

```bash
pipx install asana-api-cli
```

In this setup the CLI uses the `python-asana` version pipx resolved when
installing `asana-api-cli`; `pipx upgrade asana-api-cli` updates only
`asana-api-cli` itself, not the bundled `python-asana`. To pull a newer
`python-asana` into the existing pipx install without reinstalling the
CLI:

```bash
pipx runpip asana-api-cli install -U asana
```

The next `asana-api` run sees the new SDK and any newly added methods
automatically.

## Environment variables

| Name | Required | Description |
|------|----------|-------------|
| `ASANA_ACCESS_TOKEN` | Yes (at runtime only) | Asana personal access token |
| `ASANA_DEFAULT_WORKSPACE` | No | Default workspace GID for endpoints that require it |

The token can be issued from the
[Asana Developer Console](https://app.asana.com/0/developer-console).
No token is needed for `--help` or argument validation errors.

```bash
export ASANA_ACCESS_TOKEN="1/12345..."
export ASANA_DEFAULT_WORKSPACE="12345678"   # optional
```

On Windows PowerShell:

```powershell
$env:ASANA_ACCESS_TOKEN = "1/12345..."
$env:ASANA_DEFAULT_WORKSPACE = "12345678"   # optional
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

Click does not generate PowerShell completion. Windows users can install
completion under WSL or Git Bash using the `bash_source` line above.

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

# List every task in a project (walks every page by default)
asana-api tasks get-tasks --project <PROJECT_GID>

# Preview the first few items
asana-api tasks get-tasks --project <PROJECT_GID> --item-limit 5

# One HTTP call: return the first page + the next_page cursor
asana-api tasks get-tasks --project <PROJECT_GID> --limit 100 --full-payload

# Single task
asana-api tasks get-task --task <TASK_GID>

# Create a task (body is a JSON string)
asana-api tasks create-task --body '{"data":{"name":"new task","projects":["<PID>"]}}'

# Output formats — pair non-JSON formats with `--query '.data'` to unwrap the
# `{"data": [...]}` envelope into one row per item.
asana-api tasks get-tasks --project <PID> --query '.data' --output table
asana-api tasks get-tasks --project <PID> --query '.data' --output csv

# CSV output is UTF-8 without a BOM by default. Pass --csv-bom for Excel on
# Windows, which otherwise displays non-ASCII characters as garbled text.
asana-api tasks get-tasks --project <PID> --output csv --csv-bom > tasks.csv
```

See [Pagination](#pagination) for fetching across pages and
[Global options](#global-options) for `--debug`, `--access-token`, etc.

### Workspace resolution

Many API endpoints require a workspace. For commands wrapping such
endpoints (e.g. `get-projects-for-workspace`), the CLI resolves it in
this order:

1. `--workspace <GID>` on the command
2. `ASANA_DEFAULT_WORKSPACE` environment variable

For commands where workspace is optional (e.g. `get-tasks`), the env-var
fallback is **not** used — pass `--workspace` explicitly if needed. This
avoids ambiguity with alternative scope parameters like `--project` that
the Asana API accepts in place of workspace.

## Pagination

Paginatable commands like `tasks get-tasks` expose every pagination input
of the `python-asana` SDK as a CLI flag. Each flag maps 1:1 to an SDK
`Configuration` property, `opts` key, or method kwarg, so you can probe
SDK behavior from the shell before writing any Python.

| CLI flag | SDK input | Effect |
|----------|-----------|--------|
| (none) | — | SDK default: walks every page automatically and outputs a flat JSON list of items |
| `--limit N` | `opts["limit"]` | Per-page size sent to the server (Asana API requires 1-100) |
| `--offset <TOKEN>` | `opts["offset"]` | Pagination cursor (the `next_page.offset` from a previous response) |
| `--page-limit N` | `Configuration.page_limit` | Per-page size used when `--limit` is not set (SDK default: 100). Silently ignored when `--no-return-page-iterator` or `--full-payload` is set |
| `--item-limit N` | kwarg `item_limit=N` | Stop after N items have been collected. Silently ignored when `--no-return-page-iterator` or `--full-payload` is set |
| `--no-return-page-iterator` | `Configuration.return_page_iterator = False` | Disables auto-pagination; the command runs one HTTP request and outputs the raw `{data, next_page}` dict |
| `--full-payload` | kwarg `full_payload=True` | Same effect as `--no-return-page-iterator` (per-call kwarg form) |

```bash
# Default: walk every page, return a flat list of items
asana-api tasks get-tasks --project <PID>

# Cap the result to the first 250 items
asana-api tasks get-tasks --project <PID> --item-limit 250

# Single HTTP call: one page + next_page cursor
asana-api tasks get-tasks --project <PID> --limit 100 --no-return-page-iterator

# Resume from a cursor
asana-api tasks get-tasks --project <PID> --offset <TOKEN>
```

### Deprecated flags (v2.x → v3.0)

The following v2 flags are retained as deprecation aliases. Each emits a
stderr warning and forwards to the corresponding v3 flag; they will be
removed in a future release.

| Deprecated | Replacement |
|------------|-------------|
| `--all-items` | (no-op; walking every page is now the default) |
| `--page-size N` | `--limit N` |
| `--max-items N` | `--item-limit N` |

Combining a deprecated alias with its v3 counterpart (e.g. `--page-size`
together with `--limit`) is rejected with a usage error.

## Global options

These options work at any level of the command tree, so the following are
equivalent:

```bash
asana-api --debug tasks get-tasks --project <PID>
asana-api tasks get-tasks --project <PID> --debug
```

When the same option is given at multiple levels, the later one wins.

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
| `--multibyte-filenames` | Emit RFC 5987 `filename*=UTF-8''<percent-encoded>` on multipart uploads so Asana decodes non-ASCII attachment filenames correctly |
| `--debug` | Print HTTP request/response traces for troubleshooting (Authorization values are masked). |

## Development

See [docs/development.md](https://github.com/izumo-m/asana-api-cli/blob/main/docs/development.md)
for building from source and project layout.

## License

[MIT License](https://github.com/izumo-m/asana-api-cli/blob/main/LICENSE)
