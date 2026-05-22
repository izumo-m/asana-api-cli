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
(`tests/fixtures/cli_surface.json`); see [`architecture.md`](architecture.md#cli-surface-snapshot-test).

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

The **"Flag-name parity"** column flags rows where the CLI name does
*not* match the SDK input name. Used as input to future renaming
decisions — current state only; this doc does not propose changes.

## Global options (`cli.py:main`)

| Flag | SDK destination | Mapping mechanism | Flag-name parity |
|---|---|---|---|
| `--host URL` | `asana.Configuration.host` | Direct property (`session.py:189`) | ✓ matches `host` |
| `--proxy URL` | `asana.Configuration.proxy` | Direct property (`session.py:191`) | ✓ matches `proxy` |
| `--no-verify-ssl` | `asana.Configuration.verify_ssl` | Direct property, negated (`session.py:193`) | △ negated form of `verify_ssl` (Click convention for booleans) |
| `--ca-cert PATH` | `asana.Configuration.ssl_ca_cert` | Direct property (`session.py:195`) | ✗ SDK name is `ssl_ca_cert` |
| `--retries N` | `asana.Configuration.retry_strategy.total` | Struct member: builds a `urllib3.util.retry.Retry(total=N, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])` (`session.py:201-205`); `backoff_factor` and `status_forcelist` are hard-coded CLI choices, not exposed | △ surfaces only the `total` field of a struct; full SDK surface (`retry_strategy=Retry(...)`) has no clean flag name |
| `--timeout SECONDS` | per-call kwarg `_request_timeout` | Per-call kwarg: `ApiClient.call_api` is wrapped to inject `_request_timeout=N` on every invocation (`session.py:243-252`) | △ SDK kwarg name is `_request_timeout` (leading underscore is SDK-internal); the flag drops both the underscore and the `request_` prefix |
| `--access-token TOKEN` | `asana.Configuration.access_token` | Direct property (`session.py:175`). Default source is `$ASANA_ACCESS_TOKEN` (`session.py:284`) | ✓ matches `access_token` |
| `--temp-dir PATH` | `asana.Configuration.temp_folder_path` | Direct property (`session.py:197`) | ✗ SDK name is `temp_folder_path` |
| `--debug` | `asana.Configuration.debug = True` | Direct property (`session.py:215`). Also installs `HttpClientAuthRedactor` (`session.py:216-217`) — a security override per [constitution #2](principles.md#constitution); see [`sdk-deviations.md`](sdk-deviations.md) "Personal access token in --debug output" | ✓ matches `debug` |
| `--multibyte-filenames` | *(none)* | CLI-only. Installs `MultibyteFilenameSupport` which patches `urllib3.fields.RequestField.make_multipart` to emit RFC 5987 `filename*=UTF-8''<percent-encoded>` (`session.py:31-89`). Cataloged in [`sdk-deviations.md`](sdk-deviations.md) | n/a (extension) |

## Output formatter options (`formatter.py:formatted`)

| Flag | SDK destination | Mapping mechanism | Flag-name parity |
|---|---|---|---|
| `--output FORMAT` | *(none)* | CLI-only. `json` / `table` / `csv` / `text` rendering by `_format_output`. Default `json` is canonical/lossless | n/a (extension) |
| `--query EXPR` | *(none)* | CLI-only. Pipes the response through `jq` (`jqlib.all`) | n/a (extension) |
| `--csv-bom` | *(none)* | CLI-only. Prepends UTF-8 BOM in `_print_csv` | n/a (extension) |

All three are also cataloged in [`sdk-deviations.md`](sdk-deviations.md).

## Paginatable command extras (`cli.py:_make_command`)

Injected only when the SDK method is paginatable (has a `:param limit:`
in its docstring). For the auto-generated `--limit` / `--offset` (which
come from the docstring, not from `_make_command`), see
[`architecture.md` §Pagination](architecture.md#pagination).

> The pagination subset of this table is duplicated in
> [`architecture.md` §Pagination](architecture.md#pagination). When
> editing one, keep the other in sync; a follow-up may replace
> architecture.md's table with a link to here.

| Flag | SDK destination | Mapping mechanism | Flag-name parity |
|---|---|---|---|
| `--no-return-page-iterator` | `asana.Configuration.return_page_iterator = False` | Forwarded through `AsanaSession(return_page_iterator=False)` (`session.py:183`) | △ negated form of `return_page_iterator` (Click convention for booleans) |
| `--page-limit N` | `asana.Configuration.page_limit` | Forwarded through `AsanaSession(page_limit=N)` (`session.py:185`) | ✓ matches `page_limit` |
| `--item-limit N` | per-call kwarg `item_limit` | Forwarded as a method kwarg by `_make_command` | ✓ matches `item_limit` |
| `--full-payload` | per-call kwarg `full_payload=True` | Forwarded as a method kwarg by `_make_command` | ✓ matches `full_payload` |

### v2 deprecation aliases (paginatable, deprecated in v3.0)

Each emits a stderr warning and forwards to the equivalent v3 flag.
Scheduled for removal in a future release.

| Flag | Forwards to | SDK destination (transitive) |
|---|---|---|
| `--all-items` | *(no-op)* | *(none)* — walking every page is now the default; was a CLI-only feature in v2 with no SDK counterpart |
| `--page-size N` | `--limit N` | SDK `opts["limit"]` (auto-generated from docstring) |
| `--max-items N` | `--item-limit N` | per-call kwarg `item_limit` |

## Naming parity status (summary)

Rows above marked **✗** in "Flag-name parity":

- `--ca-cert` → SDK `ssl_ca_cert`
- `--temp-dir` → SDK `temp_folder_path`

Rows marked **△** (parity-debatable):

- `--no-verify-ssl` / `--no-return-page-iterator` — `--no-*` boolean
  negation is a Click convention; the SDK property name itself is
  preserved.
- `--retries` — surfaces only one field of `Configuration.retry_strategy`
  (`Retry.total`); SDK's full struct has no clean single-flag mapping.
- `--timeout` — SDK kwarg `_request_timeout`. The leading underscore is
  an SDK-internal convention; the public alternative `--request-timeout`
  would track the SDK more closely.

A future revision decides which of these to rename (parity-first stance
per [constitution #1](principles.md#constitution)). This document
records current state only.
