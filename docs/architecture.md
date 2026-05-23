# Architecture

Runtime-introspection wrapper around `python-asana`. The CLI command tree is built at import time; no codegen.

## Modules (`src/asana_api_cli/`)

| File | Role |
|---|---|
| `cli.py` | Runtime introspection + Click command tree |
| `session.py` | SDK client (`Configuration` + `ApiClient`); body / workspace resolution; installs the debug redactor and the multibyte-filename patch |
| `formatter.py` | Output formatting (`json` / `table` / `csv` / `text`) + the `@formatted` decorator |
| `click_ext.py` | `LazyGroup` for cheap top-level `--help`; mixins propagating global options to subgroups |
| `redactor.py` | `HttpClientAuthRedactor` — masks `Authorization` headers in `http.client` debug output |
| `structured_arg.py` | Hybrid value parser for structured options (`k=v,k=v` / JSON object / `@path`) |
| `version.py` | `version_string()` used by `--version` |

## Command construction (import time)

`cli.py` walks every `*Api` class on the installed `asana` package and produces:

1. One Click subgroup per `*Api` class (`TasksApi` → `tasks`).
2. One Click command per method (`get_tasks` → `get-tasks`).
3. Click options per docstring `:param:` line; `snake_case` → `kebab-case`, trailing `_gid` stripped (`task_gid` → `--task`).

Method-level introspection is deferred per group so top-level `--help` cost stays flat as the SDK grows.

## Invocation flow

1. `main` parses global options and constructs `AsanaSession` (`Configuration` + `ApiClient`; installs the auth redactor on `--debug`; optionally patches multipart filename encoding).
2. The resolved command invokes the SDK `*Api` method via `_make_command()`, forwarding pagination kwargs and materializing the page iterator inside the session context.
3. `@formatted` (in `formatter.py`) renders the response, optionally piped through `jq` via `--query`.

## Extension point

All changes to how an SDK method becomes a CLI command go through `_make_command()` in `cli.py` — pagination flags, hidden params, deprecation aliases, option renames.

## Surface snapshot guardrail

`tests/test_cli_surface.py` deep-compares `introspect_to_manifest()` against `tests/fixtures/cli_surface.json`. An SDK bump that adds, removes, or renames a docstring-derived option fails this test. Synthetic options injected inside `_make_command` (pagination flags, deprecation aliases) are intentionally outside the manifest.
