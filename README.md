# asana-api-cli

Call any Asana endpoint from your shell — no throwaway Python script
needed. `asana-api <group> <command>` turns **every method of the official
[`python-asana`](https://github.com/Asana/python-asana) SDK** into a
command, so you can read, create, and inspect Asana data one line at a time.

And because each command maps 1:1 to an SDK method, the call you work out in
the shell is the call you write in Python:

```bash
# Work it out interactively...
asana-api tasks get-tasks --project 123 --opt-fields name,assignee.name
```

```python
# ...then drop the same call into your app:
asana.TasksApi(client).get_tasks({"project": "123", "opt_fields": "name,assignee.name"})
```

## Why asana-api-cli

- **Explore the whole API from the shell.** Every method of every `*Api`
  class is a command — list tasks, create a project, poll events — with no
  script and no boilerplate.
- **What you learn transfers to Python.** Flags map to SDK method
  parameters, and JSON output matches the SDK's response shape — so a
  working shell call becomes a working SDK call. Each option's `--help`
  even names where its value lands in the SDK.
- **Always matches your SDK version.** The command tree is built at startup
  by introspecting the installed `asana` package. Install it beside your
  project's SDK and the two stay in lock-step — new upstream methods appear
  the moment `pip install -U asana` lands, with no `asana-api-cli` release
  and no stale docs.
- **Shell-native ergonomics.** JSON / table / CSV / text output, `jq`
  filtering (`--query`), automatic pagination, structured error envelopes,
  and `--debug` request tracing with the auth token masked.

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
export ASANA_ACCESS_TOKEN="2/12345..."
export ASANA_DEFAULT_WORKSPACE="12345678"   # optional
```

On Windows PowerShell:

```powershell
$env:ASANA_ACCESS_TOKEN = "2/12345..."
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

# List workspaces
asana-api workspaces get-workspaces

# List up to 50 projects
asana-api projects get-projects-for-workspace --item-limit 50
asana-api projects get-projects --workspace <WORKSPACE_GID> --item-limit 50

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

# --output none suppresses the success payload — handy for side-effect-only
# calls (delete/update) where only the exit code matters. The `--query` pass
# still runs, so jq syntax errors are caught even when output is silenced.
asana-api tasks delete-task --task <GID> --output none
```

See [Pagination](#pagination) for fetching across pages and
[Global options](#global-options) for `--debug`, `--access-token`, etc.

## Workspace resolution

A subset of endpoints — those whose SDK signature takes a positional
`workspace_gid` (e.g. `projects get-projects-for-workspace`,
`tags create-tag-for-workspace`) — require a workspace. For those
commands, the CLI resolves it in this order:

1. `--workspace <GID>` on the command
2. `ASANA_DEFAULT_WORKSPACE` environment variable

For commands where workspace is an optional filter (e.g.
`tasks get-tasks`, `goals get-goals`), the env-var fallback is
**not** used — pass `--workspace` explicitly if needed. This avoids
ambiguity with alternative scope parameters like `--project` that
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
| `--page-limit N` | `Configuration.page_limit` | Same as `--limit` via Configuration (default: 100). Silently ignored when `--no-return-page-iterator` or `--full-payload` is set |
| `--item-limit N` | kwarg `item_limit=N` | Stop after N items have been collected. Silently ignored when `--no-return-page-iterator` or `--full-payload` is set |
| `--return-page-iterator` / `--no-return-page-iterator` | `Configuration.return_page_iterator` | Toggle the SDK page iterator (default: enabled). `--no-return-page-iterator` disables auto-pagination — the command runs one HTTP request and outputs the raw `{data, next_page}` dict |
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

If both a deprecated alias and its replacement are given (e.g.
`--page-size 50 --limit 100`), the replacement takes precedence and
the deprecated value is ignored.

## Global options

Global options work at any level of the command tree, and the later
one wins when the same option appears more than once:

```bash
asana-api --debug tasks get-tasks --project <PID>
asana-api tasks get-tasks --project <PID> --debug
```

Run `asana-api --help` for the full list with defaults, or see
[`docs/cli-sdk-mapping.md`](https://github.com/izumo-m/asana-api-cli/blob/main/docs/cli-sdk-mapping.md)
for the SDK `Configuration` property each flag maps to.

Coverage at a glance:

- **Auth**: `--access-token` (defaults to `$ASANA_ACCESS_TOKEN`)
- **Endpoint / network**: `--host`, `--proxy`, `--connection-pool-maxsize`
- **TLS / mTLS**: `--verify-ssl` / `--no-verify-ssl`, `--ssl-ca-cert`, `--cert-file`, `--key-file`, `--assert-hostname`
- **Retry**: `--retry-strategy` (shorthand, JSON object, or `@file`)
- **Logging / debug**: `--debug`, `--logger-format`, `--logger-file`
- **File handling**: `--temp-folder-path`, `--safe-chars-for-path-param`
- **Structured errors**: `--exception-output {none|json|text|csv|table}`, `--exception-query EXPR` — see [`docs/exit-codes.md`](https://github.com/izumo-m/asana-api-cli/blob/main/docs/exit-codes.md)

`--request-timeout`, `--header-params`, `--item-limit`, and `--full-payload`
are **not** global: they are per-call SDK kwargs (the method `all_params`),
exposed as options on every command and forwarded straight to the SDK call.

`--multibyte-filenames` is likewise **not** global: it is an asana-api
extension exposed only on file-upload commands (e.g. `attachments
create-attachment-for-object`). Pass it when an attachment's filename
contains non-ASCII characters so the upload sends the RFC 5987 `filename*=`
parameter; off by default to match the SDK.

A few worth highlighting:

```bash
# Print HTTP request/response traces to stderr (Authorization masked)
asana-api --debug tasks get-tasks --project <PID>

# Tune the SDK retry policy from the shell
asana-api --retry-strategy 'total=5,backoff_factor=1.5' tasks get-tasks --project <PID>

# Catch SDK exceptions as a structured envelope on stdout (exit 3)
asana-api tasks get-task --task <GID> --exception-output json
```

Asana only accepts Bearer-token authentication (personal access token,
Service Account, or OAuth), so authenticate with `--access-token` or
`$ASANA_ACCESS_TOKEN`. The SDK's inert `username` / `password` / `api_key` /
`api_key_prefix` Configuration fields (HTTP basic auth / deprecated API keys,
which Asana does not use) are **not** exposed — see
[`docs/sdk-deviations.md`](https://github.com/izumo-m/asana-api-cli/blob/main/docs/sdk-deviations.md).

## Development

See [docs/development.md](https://github.com/izumo-m/asana-api-cli/blob/main/docs/development.md)
for building from source and project layout.

## License

[MIT License](https://github.com/izumo-m/asana-api-cli/blob/main/LICENSE)
