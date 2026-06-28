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

Or skip the translation entirely: `asana-api --generate-python …` writes that
Python for you (see [Usage](#usage)).

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
No token is needed for `--help` or command-line parsing errors. (A `--query`
jq filter is validated against the response, so it surfaces errors only after
the API call — which does need a token.)

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

# List up to 50 projects (workspace comes from $ASANA_DEFAULT_WORKSPACE)
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
asana-api tasks create-task --body '{"data":{"name":"new task","projects":["<PROJECT_GID>"]}}'

# Output formats — non-JSON formats render one row per item. The default
# auto-paginating output is a flat list, so it is directly rowable; under
# --full-payload, unwrap the `{"data": [...]}` envelope first with `--query '.data'`.
asana-api tasks get-tasks --project <PROJECT_GID> --output table
asana-api tasks get-tasks --project <PROJECT_GID> --full-payload --query '.data' --output csv

# CSV output is UTF-8 without a BOM by default. Pass --csv-bom for Excel on
# Windows, which otherwise displays non-ASCII characters as garbled text.
asana-api tasks get-tasks --project <PROJECT_GID> --output csv --csv-bom > tasks.csv

# --output none suppresses the success payload — handy for side-effect-only
# calls (delete/update) where only the exit code matters. The `--query` pass
# still runs, so jq syntax errors are caught even when output is silenced.
asana-api tasks delete-task --task <TASK_GID> --output none
```

Don't hand-translate the call yourself — `--generate-python` does it for you. It
prints a standalone `python-asana` script equivalent to the command instead of
running it (no token, no network), turning a working shell call into
ready-to-paste SDK code:

```bash
asana-api --generate-python tasks get-task --task <TASK_GID>
```

```python
# Generated equivalent (header and stdio-setup boilerplate trimmed):
import json
import os

import asana

configuration = asana.Configuration()
configuration.access_token = os.environ['ASANA_ACCESS_TOKEN']
api_client = asana.ApiClient(configuration)

api_instance = asana.TasksApi(api_client)
opts = {}

result = api_instance.get_task('<TASK_GID>', opts)

print(json.dumps(result, indent=2, ensure_ascii=False))
```

Redirect it to a file (`asana-api … --generate-python > fetch_task.py`) to keep
the script. It reproduces your configuration, output format, `--query`, `--debug`
(with the auth token masked), and uploads — see
[Generating Python code](https://github.com/izumo-m/asana-api-cli/blob/main/docs/usage.md#generating-python-code)
for the full behavior.

For the complete option reference — global options, pagination, output formats,
workspace resolution, error handling, and exit codes — see
[`docs/usage.md`](https://github.com/izumo-m/asana-api-cli/blob/main/docs/usage.md).

Asana only accepts Bearer-token authentication (personal access token, Service
Account, or OAuth), so authenticate with `--access-token` or
`$ASANA_ACCESS_TOKEN`.

## Development

See [docs/development.md](https://github.com/izumo-m/asana-api-cli/blob/main/docs/development.md)
for building from source and project layout.

## License

[MIT License](https://github.com/izumo-m/asana-api-cli/blob/main/LICENSE)
