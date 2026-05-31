# Built-in CLI options ↔ python-asana SDK mapping

How every **built-in (manually declared)** CLI option corresponds to the
`python-asana` SDK, plus the rules that generate the runtime-derived options. For
what each option *does* (behavior, examples), see [`usage.md`](usage.md); for
*why* the CLI-only flags exist, see [`sdk-deviations.md`](sdk-deviations.md).

## Scope

**Included** — options written by hand in this codebase:

- Global options declared on `cli.py:main` (`@click.option(...)` decorators).
- The `formatted` decorator's options in `formatter.py`.
- Common per-command options injected by `_make_command` in `cli.py`: the
  boilerplate per-call kwargs (`_make_per_call_kwarg_options`) on **every**
  command, the pagination deprecation aliases on paginatable commands only, and
  the `--multibyte-filenames` extension on upload commands only.

**Excluded** — options *generated at runtime* by introspecting each SDK method's
docstring `:param` lines (`opt_fields`, `workspace`, positional args,
paginatable `limit` / `offset`, etc.). These are SDK-derived by construction and
tracked by the snapshot fixture (`tests/fixtures/cli_surface.json`); see
[`architecture.md`](architecture.md#surface-snapshot-guardrail). The rules that
produce them are in [Conversion rules](#conversion-rules).

## Notation

In the **"SDK destination"** column:

- *Direct property* — assigned to a field on `asana.Configuration` once, during
  session construction (`session.py:AsanaSession.__init__`).
- *Struct member* — passed into a nested constructor that `Configuration`
  consumes (e.g. `Configuration.retry_strategy` is built from a
  `urllib3.util.retry.Retry`).
- *Per-call kwarg* — forwarded through `_make_command` as a method kwarg.
- *Patch* — a monkey-patch on an SDK-adjacent module, installed for the lifetime
  of the session.
- *(none)* — no SDK counterpart (CLI-only; cataloged in
  [`sdk-deviations.md`](sdk-deviations.md), behavior in [`usage.md`](usage.md)).

## Global options (`cli.py:main`)

| Flag | SDK destination | Mapping mechanism |
|---|---|---|
| `--host URL` | `Configuration.host` | Direct property |
| `--proxy URL` | `Configuration.proxy` | Direct property |
| `--verify-ssl / --no-verify-ssl` | `Configuration.verify_ssl` | Direct property; tri-state (unspecified keeps the SDK default) |
| `--ssl-ca-cert PATH` | `Configuration.ssl_ca_cert` | Direct property |
| `--cert-file PATH` | `Configuration.cert_file` | Direct property (client TLS cert) |
| `--key-file PATH` | `Configuration.key_file` | Direct property (client TLS key) |
| `--assert-hostname / --no-assert-hostname` | `Configuration.assert_hostname` | Direct property; tri-state |
| `--retry-strategy VALUE` | `Configuration.retry_strategy` | Struct member: parsed by `structured_arg` (`RETRY_FIELD_SCHEMA`), applied as `retry_strategy.new(**overrides)` so unspecified fields keep the SDK defaults |
| `--connection-pool-maxsize N` | `Configuration.connection_pool_maxsize` | Direct property |
| `--access-token TOKEN` | `Configuration.access_token` | Direct property; default source `$ASANA_ACCESS_TOKEN` |
| `--temp-folder-path PATH` | `Configuration.temp_folder_path` | Direct property |
| `--safe-chars-for-path-param S` | `Configuration.safe_chars_for_path_param` | Direct property |
| `--logger-format FMT` | `Configuration.logger_format` | Direct property |
| `--logger-file PATH` | `Configuration.logger_file` | Direct property |
| `--debug` | `Configuration.debug = True` | Direct property; also installs `HttpClientAuthRedactor` (security override, constitution #2 — see [`sdk-deviations.md`](sdk-deviations.md)) |
| `--return-page-iterator / --no-return-page-iterator` | `Configuration.return_page_iterator` | Via `runtime`, applied in `AsanaSession.__init__`; tri-state |
| `--page-limit N` | `Configuration.page_limit` | Via `runtime`, applied in `AsanaSession.__init__` |

`--retry-strategy` and `--header-params` accept a shared structured-value format
(`k=v` / JSON / `@file`); the syntax is documented in
[`usage.md`](usage.md#structured-values). The inert auth fields `username` /
`password` / `api_key` / `api_key_prefix` are **not** exposed — see
[`sdk-deviations.md`](sdk-deviations.md).

## Common per-command kwargs (`_make_command`)

Present on every command (the SDK's `all_params`, accepted by every method) and
forwarded straight to the SDK call as a kwarg — no `runtime` round-trip.

| Flag | SDK destination | Mapping mechanism |
|---|---|---|
| `--item-limit N` | per-call kwarg `item_limit` | Forwarded by `_make_command` |
| `--full-payload` | per-call kwarg `full_payload=True` | Forwarded by `_make_command` |
| `--header-params VALUE` | per-call kwarg `header_params` | Parsed by `structured_arg`; **not redacted in `--debug`** (see SECURITY.md) |
| `--request-timeout SECONDS` | per-call kwarg `_request_timeout` | Forwarded by `_make_command`; propagated to every page request by the SDK `PageIterator` |

The `--limit` / `--offset` flags are docstring-derived (per-method) and appear
only on commands whose SDK method declares them — same category as `--sync` /
`--assignee` / other per-method opts.

## Output formatter options (`formatter.py:formatted`)

CLI-only — no SDK destination. Behavior in
[`usage.md`](usage.md#output-formatting) and [`usage.md`](usage.md#error-output);
rationale in [`sdk-deviations.md`](sdk-deviations.md). Because they bind to the
single SDK method call (not the client `Configuration`), they are per-command
leaf options — placing them before the command path is a usage error.

| Flag | Implementation |
|---|---|
| `--output {json\|table\|csv\|text\|none}` | `_format_output` |
| `--query EXPR` | `jqlib.all` in `_format_output` |
| `--csv-bom` | `_print_csv` |
| `--exception-output {none\|json\|text\|csv\|table}` | `_handle_exception` + `_format_output` |
| `--exception-query EXPR` | `_handle_exception` + `_format_output` |

## v2 deprecation aliases (paginatable; deprecated in v3.0)

Each emits a stderr warning and forwards to its v3 flag. Behavior in
[`usage.md`](usage.md#deprecated-aliases).

| Flag | Forwards to | SDK destination (transitive) |
|---|---|---|
| `--all-items` | *(no-op)* | *(none)* — walking every page is now the default |
| `--page-size N` | `--limit N` | `opts["limit"]` (docstring-derived) |
| `--max-items N` | `--item-limit N` | per-call kwarg `item_limit` |

## Upload-command extension (`_make_command`)

Present only on commands that perform a multipart file upload — detected at
runtime by `_Operation.does_upload` (the sole such command today is
`attachments create-attachment-for-object`). Off by default; held exact by
`tests/test_sdk_boilerplate.py`.

| Flag | SDK destination | Mapping mechanism |
|---|---|---|
| `--multibyte-filenames` | *(none)* | *Patch*: installs `MultibyteFilenameSupport` (RFC 5987 `filename*=`), set via `runtime` and applied in `AsanaSession.__init__`. Behavior in [`usage.md`](usage.md#file-uploads); rationale in [`sdk-deviations.md`](sdk-deviations.md) |

## Conversion rules

How the CLI surface is derived from the SDK at runtime:

- **Naming.** `*Api` classes become groups (`TasksApi` → `tasks`), methods become
  commands (`get_tasks` → `get-tasks`), and each docstring `:param:` becomes an
  option: `snake_case` → `kebab-case`, trailing `_gid` stripped (`task_gid` →
  `--task`).
- **Role.** A *required* parameter (no default) maps to a positional SDK argument
  (`body`, a path GID, `workspace_gid`); an *optional* one to an `opts` entry.
  `body` is always the first argument when present.
- **Classification.** Settable `Configuration` properties become global flags;
  the SDK's uniform `all_params` `**kwargs` become common per-command options.
  An SDK bump that adds either fails `tests/test_sdk_boilerplate.py`, forcing a
  conscious classification.
- **Collision.** When a runtime-derived SDK arg/opt name would collide with a
  built-in flag, it is exposed as `--sdk-<name>` (the built-in keeps its bare
  name; the label still shows the real SDK name) — e.g.
  `typeahead-for-workspace --sdk-query`. `tests/test_cli.py::TestFlagCollisions`
  fails on any duplicate flag.
- **Parity.** Every built-in non-extension flag matches the SDK input name 1:1
  ([constitution #1](principles.md#constitution)): adding or renaming one must
  keep it isomorphic to the underlying SDK input, or the deviation is documented
  in [`sdk-deviations.md`](sdk-deviations.md).

## SDK-destination help labels

Every option's `--help` ends with a `(<kind>: <name>)` label naming where its
value lands in the SDK, so the mapping is visible at the point of use. Parentheses
(not brackets) keep these distinct from click's own `[required]` / `[default]`
metadata.

| Label | SDK destination |
|---|---|
| `(Configuration: <name>)` | property set on `asana.Configuration` (the global flags) |
| `(args: <name>)` | positional method argument — `body`, a path GID, or `workspace_gid` |
| `(opts: <name>)` | entry in the method's `opts` dict (a docstring `:param`) |
| `(kwargs: <name>)` | boilerplate `**kwargs` every method accepts (its `all_params`) |
| `(asana-api: extension)` | no SDK counterpart (CLI-only; also in `sdk-deviations.md`) |

`cli.py:_sdk_dest()` builds the labels at runtime; `--workspace` is labeled per
endpoint (`args` when positional, `opts` otherwise). See
[`architecture.md`](architecture.md#sdk-destination-labels) for how the literals
are kept in sync with the code.
