# Architecture

Runtime-introspection wrapper around `python-asana`. API group stubs are registered at import time; each group's commands and options are built lazily on first access. No codegen.

## Modules (`src/asana_api_cli/`)

| File | Role |
|---|---|
| `cli.py` | Runtime introspection + Click command tree; body / workspace input resolution |
| `session.py` | SDK client (`Configuration` + `ApiClient`); on context-manager entry (`open`) installs the `--debug` side effects (the `http.client` debuglevel + asana/urllib3 logger flips and the `Authorization` redactor), reverses them on exit (`close`) |
| `formatter.py` | Output formatting (`json` / `table` / `csv` / `text` / `none`) + the `@formatted` decorator |
| `click_ext.py` | `LazyGroup` for cheap top-level `--help`; the `_GlobalOptionsMixin` mixin and its concrete subclasses propagating global options to subgroups (`GroupWithGlobalOptions`) and leaf commands (`CommandWithGlobalOptions`) |
| `redactor.py` | `HttpClientAuthRedactor` — masks `Authorization` headers in `http.client` debug output |
| `multibyte_filename.py` | `MultibyteFilenameSupport` — patches `urllib3` multipart encoding to add the RFC 5987 `filename*=` parameter for non-ASCII filenames; backs the upload-only `--multibyte-filenames` flag |
| `structured_arg.py` | Hybrid value parser for structured options (`k=v,k=v` / JSON object / `@path`) |
| `version.py` | `version_string()` used by `--version` |

## Command construction (lazy, per group)

`cli.py` walks every `*Api` class on the installed `asana` package and produces:

1. One Click subgroup per `*Api` class (`TasksApi` → `tasks`).
2. One Click command per method (`get_tasks` → `get-tasks`).
3. Click options per docstring `:param:` line; `snake_case` → `kebab-case`, trailing `_gid` stripped (`task_gid` → `--task`).

Method-level introspection is deferred per group so top-level `--help` cost stays flat as the SDK grows.

## SDK-destination labels

Every option's `--help` ends with a uniform `(<kind>: <name>)` label naming where its value lands in the `python-asana` call. The full label vocabulary (the six kinds) lives in [`cli-sdk-mapping.md`](cli-sdk-mapping.md#sdk-destination-help-labels); this section covers how the labels are produced and kept in sync with the code.

`cli.py:_sdk_dest()` builds every label `_make_command` derives at runtime — `args` / `opts` for path / body / docstring params, `kwargs` for the common per-call kwargs, and the extension marker on the deprecated aliases and the upload-only `--multibyte-filenames` flag. The `Configuration` globals (and the two `ApiClient`-instance globals `--user-agent` / `--set-default-header`) carry the matching `(Configuration: …)` / `(ApiClient: …)` literal in their single declaration in `click_ext.py:_global_option_sections` — the one source every command's global options are built from, so the label (and the rest of the option) is identical at the root and at any subcommand by construction. The CLI-only formatter flags (`--output` / `--query` / `--csv-bom` and the error-path twins `--exception-output` / `--exception-query`) carry theirs in `formatter.py:make_formatter_options`. `--workspace` is labeled per endpoint (`args` when positional, `opts` otherwise).

## Invocation flow

1. The root group and every subcommand accept the global options (from the single `click_ext.py:_global_option_sections` source) and write the command-line ones into the shared `runtime` singleton via `_consume_global_options`; `AsanaSession.__init__` reads them, applies the `Configuration` knobs (host, retry, page_limit, return_page_iterator, ...), and builds the `ApiClient` — touching no process globals. Entering the session (`open()`, via `__enter__`) installs the global side effects: under `--debug`, the SDK `debug` flag (which flips `http.client` debuglevel to 1 and raises the asana / urllib3 loggers to DEBUG) plus the `Authorization` redactor that masks the resulting wire trace. `close()` (via `__exit__`) reverses them — restoring the prior debuglevel and logger levels before removing the mask — so these globals live only for the `with` block. (The upload-only multipart filename patch is *not* a session concern — see step 2.)
2. The resolved command invokes the SDK `*Api` method via `_make_command()`, passing the docstring-derived `opts` and the common per-call kwargs (`item_limit` / `full_payload` / `header_params` / `_request_timeout`) — both are per-command options read from the command's own flags. `_request_timeout` reaches every page request through the SDK `PageIterator`. If the SDK returns a lazy iterator (`isinstance(result, collections.abc.Iterator)` check), it is consumed into a list inside the session context so multi-page HTTP requests stay under the auth redactor. The upload-only `--multibyte-filenames` flag installs the RFC 5987 multipart patch (`MultibyteFilenameSupport`) from its own option callback via `ctx.with_resource`, scoping it to the command's context teardown — not routed through `runtime` or the session.
3. `@formatted` (in `formatter.py`) renders the response, optionally piped through `jq` via `--query`.

## Error handling

`formatter.py:_handle_exception` builds an envelope from any exception raised by the SDK call path (ApiException, urllib3 connection errors, etc.) and routes it through the same `_format_output` used by the success path. `_echo_exception_only` first writes the exception to **stderr** (`traceback.format_exception_only` — qualified class name + `__str__`, no frames), applied pre-`--exception-query` so unexpected error shapes stay diagnosable. Click's own errors (`ClickException`, `Abort`, `Exit`) are re-raised, not envelope-wrapped. The user-facing contract — exit codes, the 5/2-field envelope schema, and the stderr echo — is documented in [`usage.md`](usage.md#error-handling) and [`usage.md`](usage.md#exit-codes).

## Extension point

All changes to how an SDK method becomes a CLI command go through `_make_command()` in `cli.py` — docstring-derived per-method opts, the common per-call kwargs (`all_params`), deprecation aliases, option renames. The `Configuration` knobs are the global flags, declared once in `click_ext.py:_global_option_sections()`, appended to every command (the root group, subgroups, and leaves) and consumed into `runtime` by `_consume_global_options`.

## Surface snapshot guardrail

`tests/test_cli_surface.py` deep-compares `introspect_to_manifest()` against `tests/fixtures/cli_surface.json`. An SDK bump that adds, removes, or renames a docstring-derived option fails this test. Synthetic options (global flags, deprecation aliases) are intentionally outside the manifest.

`tests/test_sdk_boilerplate.py` is the companion guard for the two SDK-uniform input families that the manifest deliberately omits: every method's `all_params` (the boilerplate `**kwargs`) and the settable `asana.Configuration` attributes. An SDK bump that adds a new boilerplate kwarg or Configuration property fails it, forcing a conscious classification — a `Configuration` global flag, or a common per-command `(kwargs: ...)` option — rather than a silent miss. It also pins which methods perform a multipart upload (a whole-SDK source scan for `local_var_files` population), proving the cheap `file`-opt proxy (`_Operation.does_upload`) that gates the per-command `--multibyte-filenames` flag stays exact as the SDK evolves.
