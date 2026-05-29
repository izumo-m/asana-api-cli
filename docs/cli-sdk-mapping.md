# Built-in CLI options ↔ python-asana SDK mapping

Reference catalog of every **built-in (manually declared)** CLI option and
where it lands in the `python-asana` SDK. The companion of
[`sdk-deviations.md`](sdk-deviations.md): that file documents the gaps,
this one documents the parity points.

## Scope

**Included** — options that are written by hand in this codebase:

- Global options declared on `cli.py:main` (`@click.option(...)` decorators).
- The `formatted` decorator's options in `formatter.py`.
- Common per-command options injected by `_make_command` in `cli.py`: the
  boilerplate per-call kwargs (`_make_per_call_kwarg_options`) on **every**
  command, the pagination deprecation aliases on paginatable commands only,
  and the `--multibyte-filenames` extension on upload commands only.

**Excluded** — options that are *generated at runtime* by introspecting
each SDK method's docstring `:param` lines (`opt_fields`, `workspace`,
positional args, paginatable `limit` / `offset`, etc.). These are
SDK-derived by construction and are tracked by the snapshot fixture
(`tests/fixtures/cli_surface.json`); see [`architecture.md`](architecture.md#surface-snapshot-guardrail).

## Notation

In the **"SDK destination"** column:

- *Direct property* — assigned to a field on `asana.Configuration` once,
  during session construction (`session.py:AsanaSession.__init__`).
- *Struct member* — passed into a nested constructor that `Configuration`
  consumes (e.g. `Configuration.retry_strategy` is built from a
  `urllib3.util.retry.Retry` instance).
- *Per-call kwarg* — forwarded through `_make_command` as a method kwarg
  on the SDK call (the common per-command `(kwarg: ...)` options).
- *Patch* — a monkey-patch on an SDK-adjacent module
  (e.g. `urllib3.fields.RequestField.make_multipart`) installed for
  the lifetime of the session.
- *(none)* — no SDK counterpart. The behavior lives entirely in this
  codebase. These flags are also cataloged in
  [`sdk-deviations.md`](sdk-deviations.md).

Every built-in non-extension flag below matches the SDK input name 1:1.
That parity is the active rule per
[constitution #1](principles.md#constitution): renaming or adding a
flag must keep it isomorphic to the underlying `Configuration`
property (or document the deviation in `sdk-deviations.md`).

These destinations are also surfaced in each option's `--help` as a
trailing `(<kind>: <name>)` label — `(Configuration: <name>)`,
`(SDK arg: <name>)`, `(opts: <name>)`, `(kwarg: <name>)`, or
`(asana-api extension)` — so the mapping is visible at the point of use,
not only here. See [`architecture.md`](architecture.md#sdk-destination-labels).

## Global options (`cli.py:main`)

| Flag | SDK destination | Mapping mechanism |
|---|---|---|
| `--host URL` | `Configuration.host` | Direct property |
| `--proxy URL` | `Configuration.proxy` | Direct property |
| `--verify-ssl / --no-verify-ssl` | `Configuration.verify_ssl` | Direct property. Tri-state toggle: unspecified leaves the SDK default (True) intact |
| `--ssl-ca-cert PATH` | `Configuration.ssl_ca_cert` | Direct property |
| `--cert-file PATH` | `Configuration.cert_file` | Direct property (client TLS cert for mTLS) |
| `--key-file PATH` | `Configuration.key_file` | Direct property (client TLS key for mTLS) |
| `--assert-hostname / --no-assert-hostname` | `Configuration.assert_hostname` | Direct property. Tri-state toggle |
| `--retry-strategy VALUE` | `Configuration.retry_strategy` | Struct member: parsed via `parse_structured_arg` with `RETRY_FIELD_SCHEMA`; applied with `Configuration.retry_strategy.new(**overrides)` so unspecified fields keep the SDK defaults |
| `--connection-pool-maxsize N` | `Configuration.connection_pool_maxsize` | Direct property |
| `--access-token TOKEN` | `Configuration.access_token` | Direct property. Default source is `$ASANA_ACCESS_TOKEN` |
| `--temp-folder-path PATH` | `Configuration.temp_folder_path` | Direct property |
| `--safe-chars-for-path-param S` | `Configuration.safe_chars_for_path_param` | Direct property |
| `--logger-format FMT` | `Configuration.logger_format` | Direct property |
| `--logger-file PATH` | `Configuration.logger_file` | Direct property |
| `--debug` | `Configuration.debug = True` | Direct property. Also installs `HttpClientAuthRedactor` — a security override per [constitution #2](principles.md#constitution); see [`sdk-deviations.md`](sdk-deviations.md) "Personal access token in --debug output" |
| `--output-errors {none\|json\|text\|csv\|table}` | *(none)* | CLI-only. The exception is always echoed to **stderr** in Python's top-level format (no traceback) — for `ApiException` this includes status / reason / headers / body. Default `none` then exits `1` with no envelope; any other format additionally renders on **stdout** and exits `3`, reusing the success-path `_format_output`. See [`sdk-deviations.md`](sdk-deviations.md) for the schema and [`exit-codes.md`](exit-codes.md) for exit codes |
| `--query-errors EXPR` | *(none)* | CLI-only. Applies a `jq` filter to the error envelope; each yield is rendered per `--output-errors`. Pairing with the default `none` warns to stderr (the filter is a no-op) but does not block the call |

### Structured value format

`--retry-strategy` and `--header-params` share a single
VALUE format dispatched by the first character:

- `{...}` — parse as a JSON object.
- `@<path>` — read the file at `<path>` and parse it as a JSON object.
- otherwise — parse as shorthand `key=value[,key=value...]`.

Shorthand supports scalar values only (`int` / `float` / `bool` /
`str`). Fields whose declared type is a list — for `--retry-strategy`
those are `allowed_methods`, `status_forcelist`, and
`remove_headers_on_redirect` — must be passed via the JSON or `@file`
form, since commas inside list values would collide with the
shorthand pair separator. Bool values in shorthand accept only `true`
or `false` (case-insensitive); `1` / `0` are intentionally rejected so
they cannot be confused with int fields.

Implementation lives in `src/asana_api_cli/structured_arg.py`.

The inert auth fields `Configuration.username` / `password` / `api_key` /
`api_key_prefix` are **not** exposed (Asana auth is Bearer-token only); see
[`sdk-deviations.md`](sdk-deviations.md).

### stdin (`-`) restriction

Only `--body` is wired to read JSON from stdin (`-`). The structured
options (`--retry-strategy`, `--header-params`) intentionally do *not*
accept `-` because multiple options requesting stdin from a single
invocation produces evaluation-order-dependent silent bugs. To pipe a
structured value, use bash process substitution:
`--retry-strategy @<(echo '{"total":3}')`.

## Output formatter options (`formatter.py:formatted`)

| Flag | SDK destination | Mapping mechanism |
|---|---|---|
| `--output FORMAT` | *(none)* | CLI-only. `json` / `table` / `csv` / `text` rendering by `_format_output`; `none` suppresses output (use when only the exit code matters). Default `json` is canonical/lossless. Symmetric with `--output-errors none` |
| `--query EXPR` | *(none)* | CLI-only. Pipes the response through `jq` (`jqlib.all`). Runs and validates even under `--output none` so jq errors stay observable (exit 2) regardless of the format flag |
| `--csv-bom` | *(none)* | CLI-only. Prepends UTF-8 BOM in `_print_csv` |

All three are also cataloged in [`sdk-deviations.md`](sdk-deviations.md).

## Iteration controls

These divide by SDK scope. ``Configuration`` properties are **global flags**
(client-wide, set once). The boilerplate per-call ``**kwargs`` — the SDK's
``all_params``, accepted uniformly by every method — are **common
per-command options** present on every command (labeled `(kwarg: ...)`),
because they are method inputs, not client config.

### Configuration globals (`cli.py:main`)

| Flag | SDK destination | Mapping mechanism |
|---|---|---|
| `--return-page-iterator / --no-return-page-iterator` | `Configuration.return_page_iterator` | Written to `runtime` and applied in `AsanaSession.__init__`. Tri-state toggle: unspecified leaves the SDK default (True) intact |
| `--page-limit N` | `Configuration.page_limit` | Written to `runtime` and applied in `AsanaSession.__init__` |

### Common per-command kwargs (`_make_command`)

Present on every command (the SDK accepts them on every method) and forwarded
straight to the SDK method call as a kwarg — no `runtime` round-trip.

| Flag | SDK destination | Mapping mechanism |
|---|---|---|
| `--item-limit N` | per-call kwarg `item_limit` | Forwarded as a method kwarg by `_make_command` |
| `--full-payload` | per-call kwarg `full_payload=True` | Forwarded as a method kwarg by `_make_command` |
| `--header-params VALUE` | per-call kwarg `header_params` | Parsed by `structured_arg` (`'k=v,...'` / JSON / `@path`), forwarded as a method kwarg. **Not redacted in `--debug` — see SECURITY.md** |
| `--request-timeout SECONDS` | per-call kwarg `_request_timeout` | Forwarded as a method kwarg by `_make_command`; the SDK `PageIterator` propagates it to every page request |

The `--limit` / `--offset` flags themselves are docstring-derived (per-method)
and appear only on commands whose SDK method declares them — same category
as `--sync` / `--assignee` / other per-method opts.

### v2 deprecation aliases (paginatable, deprecated in v3.0)

Each emits a stderr warning and forwards to the equivalent v3 flag.
Scheduled for removal in a future release.

| Flag | Forwards to | SDK destination (transitive) |
|---|---|---|
| `--all-items` | *(no-op)* | *(none)* — walking every page is now the default; was a CLI-only feature in v2 with no SDK counterpart |
| `--page-size N` | `--limit N` | SDK `opts["limit"]` (auto-generated from docstring) |
| `--max-items N` | `--item-limit N` | per-call kwarg `item_limit` |

## Upload-command extension (`_make_command`)

Present only on commands that perform a multipart file upload — detected at
runtime by `_Operation.does_upload` (an op declaring a `file` opt; the sole
such command today is `attachments create-attachment-for-object`). Off by
default to preserve strict SDK parity; the proxy is held exact by
`tests/test_sdk_boilerplate.py` (a whole-SDK source scan for `local_var_files`
population).

| Flag | SDK destination | Mapping mechanism |
|---|---|---|
| `--multibyte-filenames` | *(none)* | CLI-only. *Patch*: installs `MultibyteFilenameSupport`, patching `urllib3.fields.RequestField.make_multipart` to emit RFC 5987 `filename*=UTF-8''<percent-encoded>` for non-ASCII basenames. Set via `runtime` in the upload command's callback; applied in `AsanaSession.__init__`. Cataloged in [`sdk-deviations.md`](sdk-deviations.md) |
