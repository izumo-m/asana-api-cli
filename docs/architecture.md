# Architecture

Runtime-introspection wrapper around `python-asana`. The CLI command tree is built at import time; no codegen.

## Modules (`src/asana_api_cli/`)

| File | Role |
|---|---|
| `cli.py` | Runtime introspection + Click command tree |
| `session.py` | SDK client (`Configuration` + `ApiClient`); body / workspace resolution; installs the debug redactor and the multibyte-filename patch |
| `formatter.py` | Output formatting (`json` / `table` / `csv` / `text` / `none`) + the `@formatted` decorator |
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

1. `main` parses global options and writes them into the shared `runtime` singleton; `AsanaSession.__init__` reads them and applies the `Configuration` knobs (host, retry, page_limit, return_page_iterator, ...). Installs the auth redactor on `--debug`; optionally patches multipart filename encoding.
2. The resolved command invokes the SDK `*Api` method via `_make_command()`, forwarding global per-call kwargs (`full_payload` / `item_limit` / `header_params`) from `runtime` and any docstring-derived `opts` from the per-command flags. If the SDK returns a lazy iterator (`isinstance(result, collections.abc.Iterator)` check), it is consumed into a list inside the session context so multi-page HTTP requests stay under the auth redactor.
3. `@formatted` (in `formatter.py`) renders the response, optionally piped through `jq` via `--query`.

## Error handling

`formatter.py:_handle_exception` builds an envelope from any exception raised by the SDK call path (ApiException, urllib3 connection errors, etc.) and routes it through the same `_format_output` used by the success path; the format is picked by `--output-errors {none|json|text|csv|table}` (default `none`), optionally filtered by `--query-errors`. The `none` default skips the envelope entirely — the exception propagates uncaught, Python prints the traceback on stderr, and the process exits `1` (SDK-parity baseline). Any other value catches the exception, renders the envelope on **stdout**, and exits `3`; the exception is also echoed to **stderr** in Python's top-level format (`traceback.format_exception_only` — qualified class name + the exception's `__str__`, no traceback frames), applied pre-`--query-errors` so unexpected error shapes stay diagnosable. `ApiException` carries full HTTP context (`{exception, status, reason, body, headers}` — five fields); other exceptions collapse to `{exception, reason}`. The `exception` field is the qualified `module.qualname` (e.g. `"asana.rest.ApiException"`, `"urllib3.exceptions.MaxRetryError"`) so SDK users can `import` and `except` the same class. Click's own errors (`ClickException`, `Abort`, `Exit`) are re-raised, not envelope-wrapped. `--query-errors EXPR` paired with the default `none` emits a stderr warning (the filter has no envelope to apply to) but does not block the call, avoiding masking of the underlying exception. See [`exit-codes.md`](exit-codes.md) for the exit code policy.

## Extension point

All changes to how an SDK method becomes a CLI command go through `_make_command()` in `cli.py` — docstring-derived per-method opts, deprecation aliases, option renames. SDK-uniform inputs (boilerplate kwargs / `Configuration` knobs) are added as global flags in `cli.py:main()` + `click_ext.py:_make_global_option_params()` and consumed via `runtime`.

## Surface snapshot guardrail

`tests/test_cli_surface.py` deep-compares `introspect_to_manifest()` against `tests/fixtures/cli_surface.json`. An SDK bump that adds, removes, or renames a docstring-derived option fails this test. Synthetic options (global flags, deprecation aliases) are intentionally outside the manifest.
