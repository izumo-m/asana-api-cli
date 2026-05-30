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

## SDK-destination labels

Every option's `--help` ends with a uniform `(<kind>: <name>)` label naming where its value lands in the `python-asana` call, so a reader can map the flag back to the SDK. Square brackets are left to click's own `[required]` / `[default]` metadata, so every asana-api label uses parentheses. The five kinds cover the SDK method's input structure:

| Label | SDK destination |
|---|---|
| `(Configuration: <name>)` | property set on `asana.Configuration` (the global flags) |
| `(SDK arg: <name>)` | positional method argument — `body`, a path GID, or `workspace_gid` |
| `(opts: <name>)` | entry in the method's `opts` dict (a docstring `:param`) |
| `(kwarg: <name>)` | boilerplate `**kwargs` every method accepts — its `all_params` (common per-command options) |
| `(asana-api extension)` | no SDK counterpart (CLI-only; also in `sdk-deviations.md`) |

`cli.py:_sdk_dest()` builds every label `_make_command` derives at runtime — `arg` / `opts` for path / body / docstring params, `kwarg` for the common per-call kwargs, and the extension marker on the deprecated aliases and the upload-only `--multibyte-filenames` flag. The `Configuration` globals carry the matching literal by hand in both `cli.py:main` and `click_ext.py:_make_global_option_params` (kept byte-identical by `test_click_ext.py:TestHelpTextSync`); the CLI-only formatter flags (`--output` / `--query` / `--csv-bom` and the error-path twins `--exception-output` / `--exception-query`) carry theirs in `formatter.py:formatted`. `--workspace` is labeled per endpoint (`SDK arg` when positional, `opts` otherwise). The catalog of which built-in flag maps where lives in [`cli-sdk-mapping.md`](cli-sdk-mapping.md).

## Invocation flow

1. `main` parses global options and writes them into the shared `runtime` singleton; `AsanaSession.__init__` reads them and applies the `Configuration` knobs (host, retry, page_limit, return_page_iterator, ...). Installs the auth redactor on `--debug`; installs the multipart filename patch when `runtime.multibyte_filenames` is set — now driven by the per-command `--multibyte-filenames` flag on upload commands (see step 2), not a global.
2. The resolved command invokes the SDK `*Api` method via `_make_command()`, passing the docstring-derived `opts` and the common per-call kwargs (`item_limit` / `full_payload` / `header_params` / `_request_timeout`) — both are per-command options read from the command's own flags. `_request_timeout` reaches every page request through the SDK `PageIterator`. If the SDK returns a lazy iterator (`isinstance(result, collections.abc.Iterator)` check), it is consumed into a list inside the session context so multi-page HTTP requests stay under the auth redactor.
3. `@formatted` (in `formatter.py`) renders the response, optionally piped through `jq` via `--query`.

## Error handling

`formatter.py:_handle_exception` builds an envelope from any exception raised by the SDK call path (ApiException, urllib3 connection errors, etc.) and routes it through the same `_format_output` used by the success path; the format is picked by `--exception-output {none|json|text|csv|table}` (default `none`), optionally filtered by `--exception-query`. Regardless of format, `_echo_exception_only` first writes the exception to **stderr** in Python's top-level format (`traceback.format_exception_only` — qualified class name + the exception's `__str__`, no traceback frames), applied pre-`--exception-query` so unexpected error shapes stay diagnosable. The `none` default then exits `1` without an envelope (stderr is the only output channel); for `ApiException`, the stderr output is multi-line and includes status / reason / headers / body via `ApiException.__str__`, so the response payload (e.g. the 412 sync-token body in events polling) is readable without opting into an envelope. The other formats also render the envelope on **stdout** and exit `3`, giving scripts a structured channel where `body` is the decoded response *string*. `ApiException` carries full HTTP context (`{exception, status, reason, body, headers}` — five fields); other exceptions collapse to `{exception, reason}`. The `exception` field is the qualified `module.qualname` (e.g. `"asana.rest.ApiException"`, `"urllib3.exceptions.MaxRetryError"`) so SDK users can `import` and `except` the same class. Click's own errors (`ClickException`, `Abort`, `Exit`) are re-raised, not envelope-wrapped. `--exception-query EXPR` paired with the default `none` emits a stderr warning (the filter has no envelope to apply to) but does not block the call, avoiding masking of the underlying exception. See [`exit-codes.md`](exit-codes.md) for the exit code policy.

## Extension point

All changes to how an SDK method becomes a CLI command go through `_make_command()` in `cli.py` — docstring-derived per-method opts, the common per-call kwargs (`all_params`), deprecation aliases, option renames. The `Configuration` knobs are the global flags, declared in `cli.py:main()` + `click_ext.py:_make_global_option_params()` and consumed via `runtime`.

## Surface snapshot guardrail

`tests/test_cli_surface.py` deep-compares `introspect_to_manifest()` against `tests/fixtures/cli_surface.json`. An SDK bump that adds, removes, or renames a docstring-derived option fails this test. Synthetic options (global flags, deprecation aliases) are intentionally outside the manifest.

`tests/test_sdk_boilerplate.py` is the companion guard for the two SDK-uniform input families that the manifest deliberately omits: every method's `all_params` (the boilerplate `**kwargs`) and the settable `asana.Configuration` attributes. An SDK bump that adds a new boilerplate kwarg or Configuration property fails it, forcing a conscious classification — a `Configuration` global flag, or a common per-command `(kwarg: ...)` option — rather than a silent miss. It also pins which methods perform a multipart upload (a whole-SDK source scan for `local_var_files` population), proving the cheap `file`-opt proxy (`_Operation.does_upload`) that gates the per-command `--multibyte-filenames` flag stays exact as the SDK evolves.
