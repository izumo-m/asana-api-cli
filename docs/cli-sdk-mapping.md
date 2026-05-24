# Built-in CLI options ↔ python-asana SDK mapping

Reference catalog of every **built-in (manually declared)** CLI option and
where it lands in the `python-asana` SDK. The companion of
[`sdk-deviations.md`](sdk-deviations.md): that file documents the gaps,
this one documents the parity points.

## Scope

**Included** — options that are written by hand in this codebase:

- Global options declared on `cli.py:main` (`@click.option(...)` decorators).
- The `formatted` decorator's options in `formatter.py`.
- Pagination control options injected by `_make_command` in `cli.py`
  when the SDK method is paginatable.

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
- *Per-call kwarg* — injected on every SDK method call by wrapping
  `ApiClient.call_api` (`session.py:_install_timeout`) or by being
  forwarded through `_make_command` as a method kwarg.
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
| `--request-timeout SECONDS` | per-call kwarg `_request_timeout` | Per-call kwarg: `ApiClient.call_api` is wrapped to inject `_request_timeout=N` on every invocation (`session.py:_install_timeout`) |
| `--connection-pool-maxsize N` | `Configuration.connection_pool_maxsize` | Direct property |
| `--access-token TOKEN` | `Configuration.access_token` | Direct property. Default source is `$ASANA_ACCESS_TOKEN` |
| `--username USER` | `Configuration.username` | Direct property. **No-op as of python-asana 5.2.4** — see [no-op disclosure](#no-op-auth-properties) below |
| `--password PASS` | `Configuration.password` | Direct property. **No-op as of python-asana 5.2.4** |
| `--api-key VALUE` | `Configuration.api_key` | Direct property (dict). Value parsed by `parse_structured_arg`. **No-op as of python-asana 5.2.4** |
| `--api-key-prefix VALUE` | `Configuration.api_key_prefix` | Direct property (dict). **No-op as of python-asana 5.2.4** |
| `--temp-folder-path PATH` | `Configuration.temp_folder_path` | Direct property |
| `--safe-chars-for-path-param S` | `Configuration.safe_chars_for_path_param` | Direct property |
| `--logger-format FMT` | `Configuration.logger_format` | Direct property |
| `--logger-file PATH` | `Configuration.logger_file` | Direct property |
| `--debug` | `Configuration.debug = True` | Direct property. Also installs `HttpClientAuthRedactor` — a security override per [constitution #2](principles.md#constitution); see [`sdk-deviations.md`](sdk-deviations.md) "Personal access token in --debug output" |
| `--multibyte-filenames` | *(none)* | CLI-only. Installs `MultibyteFilenameSupport` which patches `urllib3.fields.RequestField.make_multipart` to emit RFC 5987 `filename*=UTF-8''<percent-encoded>`. Cataloged in [`sdk-deviations.md`](sdk-deviations.md) |

### Structured value format

`--api-key`, `--api-key-prefix`, and `--retry-strategy` share a single
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

### No-op auth properties

`--username`, `--password`, `--api-key`, and `--api-key-prefix` are
exposed for parity with `asana.Configuration` but are **inert in the
request path** as of python-asana 5.2.4: every generated `*Api` method
calls `update_params_for_auth` with `auth_settings = ['personalAccessToken']`
only, and `Configuration.auth_settings()` returns only the `token`
entry. The four properties remain on the Configuration object but no
header is ever emitted from them. The CLI surfaces them so future SDK
versions that wire them up start working without a CLI release; the
`--help` text pins the python-asana version so the disclosure is
re-verified whenever the SDK is bumped (see [`development.md`](development.md#bumping-the-asana-sdk)).

### stdin (`-`) restriction

Only `--body` is wired to read JSON from stdin (`-`). The structured
config options (`--api-key`, `--api-key-prefix`, `--retry-strategy`)
intentionally do *not* accept `-` because multiple options requesting
stdin from a single invocation produces evaluation-order-dependent
silent bugs. To pipe a structured config value, use bash process
substitution: `--api-key @<(echo '{"k":"v"}')`.

## Output formatter options (`formatter.py:formatted`)

| Flag | SDK destination | Mapping mechanism |
|---|---|---|
| `--output FORMAT` | *(none)* | CLI-only. `json` / `table` / `csv` / `text` rendering by `_format_output`. Default `json` is canonical/lossless |
| `--query EXPR` | *(none)* | CLI-only. Pipes the response through `jq` (`jqlib.all`) |
| `--csv-bom` | *(none)* | CLI-only. Prepends UTF-8 BOM in `_print_csv` |

All three are also cataloged in [`sdk-deviations.md`](sdk-deviations.md).

## Pagination / iteration globals (v3.1+)

As of v3.1 these are global flags (available on every command) — they map
1:1 to ``Configuration`` properties or boilerplate ``**kwargs`` that the
SDK accepts uniformly on every method. The CLI no longer pre-judges which
endpoint they apply to.

| Flag | SDK destination | Mapping mechanism |
|---|---|---|
| `--return-page-iterator / --no-return-page-iterator` | `Configuration.return_page_iterator` | Written to `runtime` and applied in `AsanaSession.__init__`. Tri-state toggle: unspecified leaves the SDK default (True) intact |
| `--page-limit N` | `Configuration.page_limit` | Written to `runtime` and applied in `AsanaSession.__init__` |
| `--item-limit N` | per-call kwarg `item_limit` | Forwarded from `runtime` as a method kwarg by `_make_command` |
| `--full-payload` | per-call kwarg `full_payload=True` | Forwarded from `runtime` as a method kwarg by `_make_command` |
| `--header-params VALUE` | per-call kwarg `header_params` | Parsed by `structured_arg` (`'k=v,...'` / JSON / `@path`), forwarded from `runtime`. **Not redacted in `--debug` — see SECURITY.md** |

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
