# SDK deviations

Catalog of every intentional gap between `asana-api-cli` and the
`python-asana` SDK — unexposed SDK features, behavior changes, and
CLI-only additions — framed by [Constitution #1 (parity is the top
priority) and #2 (security overrides parity)](principles.md#constitution).
Companion of [`cli-sdk-mapping.md`](cli-sdk-mapping.md), which catalogs
the *parity* side (which built-in option maps to which SDK input).

This document is **reference**, not required reading for code changes.
The CLI surface snapshot test (`tests/test_cli_surface.py`) and per-feature
tests are the source of truth for what is wired in code. Treat this file
as the **why** behind the gaps those tests encode, so future maintainers
can tell intentional non-parity from an oversight.

Code anchors below name functions/classes (not line numbers) so the
references stay valid across edits.

## Convention: `[asana-api extension]` marker

Every CLI-only flag cataloged below (the rows whose "SDK behavior" column
is "Not in SDK") ends its `--help` text with the `[asana-api extension]`
marker so users can tell at a glance which options have no SDK
counterpart. The catalog and the marker are kept in sync: adding a new
CLI-only flag means adding both a row here and the marker to the help
text; removing one without the other is a bug.

Flags that *remap* an existing SDK input to a different CLI surface
(e.g. `--request-timeout` remapping the SDK's `_request_timeout`
per-call kwarg to a global flag) are not marked — the underlying feature exists in the
SDK, only the surface differs. Behavior-only deviations such as the
`--debug` token redaction (constitution #2) are likewise not marked,
because the flag itself is SDK-derived; the deviation is enforced by a
stronger principle.

## SDK features the CLI does not expose

| SDK feature | CLI status | Reason | Code anchor |
|---|---|---|---|
| `async_req` kwarg | Filtered out at command construction time | The SDK returns a `threading.Thread`; a shell cannot consume one. The CLI is synchronous-only by design | `_parse_params` in `cli.py` (`params.pop("async_req", None)`) |
| `_return_http_data_only` kwarg | Not exposed (v3.0) | The `(data, status, headers)` tuple shape is incompatible with the formatter pipeline (dict / list only). `--debug` exposes HTTP wire info as a workaround. Re-evaluation deferred to a future minor release | — |
| `_preload_content` kwarg | Not exposed (v3.0) | Raw `urllib3.HTTPResponse` cannot be rendered by the formatter pipeline. Same reasoning as `_return_http_data_only` | — |

## Configuration properties that cannot be set from the CLI

Every other `asana.Configuration` property has a 1:1 CLI flag (see
[`cli-sdk-mapping.md`](cli-sdk-mapping.md)). The properties below have
no flag because their values are Python objects that cannot be
constructed from a command-line string.

| SDK property | Type | Reason |
|---|---|---|
| `Configuration.refresh_api_key_hook` | Python callable | Functions cannot be supplied as a CLI argument |
| `Configuration.logger` | `dict[str, logging.Logger]` | Live `Logger` instances cannot be constructed from a CLI argument; tuning the log format / file is still possible via `--logger-format` / `--logger-file` |
| `Configuration.logger_stream_handler` | `logging.StreamHandler` | Handler instance |
| `Configuration.logger_file_handler` | `logging.FileHandler` | Handler instance |
| `urllib3.util.retry.Retry.history` | `tuple[RequestHistory, ...]` | Retry's internal execution log, not a configuration knob — explicitly excluded from `--retry-strategy`'s field set |

## Personal-access-token leakage and the no-op auth properties

`--username`, `--password`, `--api-key`, and `--api-key-prefix` exist for
1:1 parity with `Configuration.username` / `password` / `api_key` /
`api_key_prefix`, but as of python-asana 5.2.4 they are **inert in the
request path**: every generated `*Api` method passes
`auth_settings = ['personalAccessToken']`, so only `Configuration.access_token`
actually emits a header. The `--help` text of each of those four flags
calls this out explicitly with a python-asana version pin so the
disclosure is re-verified when the SDK is bumped (see
[`development.md`](development.md#bumping-the-asana-sdk)).

## CLI behavior that differs from raw SDK behavior

| Area | SDK behavior | CLI behavior | Reason | Code anchor |
|---|---|---|---|---|
| Personal access token in `--debug` output (`Authorization: Bearer <token>` / `Basic <token>`) | Printed verbatim by `http.client`'s wire-level `print()` when debug level is set; the SDK's own loggers do not log headers, but `http.client` does | The token is masked before the line is written (mask format: see `_default_mask_token`) | Constitution #2 (security overrides parity). Personal access tokens must never appear in user-visible output, so debug logs stay safe to paste into bug reports | `HttpClientAuthRedactor` and `_default_mask_token` in `redactor.py`; installed by `AsanaSession.__init__` when `--debug` is set |
| `_request_timeout` per-call kwarg | Per-call kwarg on each SDK method | Surfaced as the global `--request-timeout` flag; the session wraps `ApiClient.call_api` to inject the kwarg | A CLI invocation is a single API call from the user's perspective, so a global flag is more ergonomic than a per-method surface | `--request-timeout` option in `cli.py:main`; `AsanaSession._install_timeout` in `session.py` |
| Multipart filename with non-ASCII characters | Emits `filename="..."` only; non-ASCII bytes round-trip is undefined | Emits the RFC 5987 `filename*=UTF-8''<percent-encoded>` parameter alongside `filename=` when `--multibyte-filenames` is set | SDK gap: the Asana API needs RFC 5987 to round-trip non-ASCII attachment names. Opt-in so default upload semantics do not change silently | `--multibyte-filenames` option in `cli.py:main`; `MultibyteFilenameSupport` in `session.py` |
| Default pagination return shape | `Configuration.return_page_iterator = True` produces a lazy iterator (`PageIterator` / `EventIterator`) | Any return value that is an `isinstance(result, collections.abc.Iterator)` is walked to completion (`list(result)`) inside the session context and printed as a flat list of items | (1) An unwalked iterator cannot be serialized to stdout — the CLI must materialize a complete payload. (2) Materializing inside the session context keeps `HttpClientAuthRedactor` active for every page request; lazy iteration after the session exits would leak `Authorization` headers on pages 2..N (constitution #2 tie-in) | post-judge `isinstance` check in `_make_command`'s inner callback in `cli.py` (Layer B) |
| `--output FORMAT` | (Not in SDK — SDK returns Python objects) | Renders the response into one of `json` / `table` / `csv` / `text` / `none` (default `json` — canonical, lossless). `none` suppresses the success payload entirely for side-effect-only operations (delete, update) where only the exit code matters; symmetric with `--output-errors none`. Under `none` the `--query` pass still runs so jq syntax / runtime errors keep surfacing as exit 2 — value-level validation is independent of the chosen format, so flipping a script from `--output json` to `--output none` cannot mask a broken jq expression | Shell ergonomics: CLI output must be text. The four rendered formats serve different consumer types (machines via JSON, humans via table, spreadsheets via CSV, scripts via text); `none` covers the "exit code only" case without forcing users to remember the per-shell redirect (`> /dev/null` / `> $null` / `> NUL`) | `--output` option and `_format_output` in `formatter.py` |
| `--query EXPR` | (Not in SDK — SDK returns Python objects) | Pipes the response through jq with the given expression; each yield is rendered separately according to the chosen output format. With `--output none`, jq still runs (errors surface as exit 2) but its results are discarded | Shell ergonomics: extracts fields / items inline without spawning a separate `jq` process. Mirrors `aws --query` | `--query` option and the `jqlib.all` call in `_format_output` in `formatter.py` |
| `--csv-bom` | (Not in SDK — SDK returns Python objects) | Prepends a UTF-8 BOM to CSV output when set | Windows ergonomics: Excel on Windows needs the BOM to decode UTF-8 CSV correctly. Opt-in so Unix pipelines stay clean | `--csv-bom` option and `_print_csv` in `formatter.py` |
| `--output-errors` / `--query-errors` (v3.1+) | (Not in SDK — Python raises an exception per HTTP error) | `--output-errors {none\|json\|text\|csv\|table}` (default `none`). `none` lets the exception propagate uncaught — Python's default handler prints the traceback on stderr and the process exits `1` (the SDK-parity baseline). `json` / `text` / `csv` / `table` catch the exception, render an envelope on **stdout**, and exit `3`; the exception is *also* echoed to **stderr** in Python's top-level format (`traceback.format_exception_only` — qualified class name + the exception's `__str__`, no traceback frames) so an unexpected error shape stays diagnosable even when `--query-errors` strips it from stdout. `ApiException` produces a 5-field envelope `{exception, status, reason, body, headers}` where `exception` is the qualified `module.qualname` (e.g. `"asana.rest.ApiException"`), `body` is the UTF-8 decoded response *string* (or `null`), and `headers` is the response header dict. Other exceptions raised from the SDK call path (e.g. `urllib3.exceptions.MaxRetryError`) collapse to a 2-field `{exception, reason}` — no HTTP context. The `exception` field always uses the qualified import path so SDK users can `from <module> import <class>` to handle the same error in their own code. `--query-errors EXPR` applies a `jq` filter to the envelope and renders each yield per `--output-errors`; passing it with the default `none` emits a stderr warning ("the filter is being ignored") but does not block the call — preserving the underlying error rather than masking it with a usage error. Click's own errors (`ClickException`, `Abort`, `Exit`) are not wrapped — they keep Click's standard handling. Same renderer as `--output`; nested dict / list values in text/csv/table render as JSON (`{"a":"b"}`), not Python repr. Envelope on stdout (rather than stderr) means scripts can rely on `case $? in 0) ... ;; 3) ... ;; esac` and consume stdout uniformly. The stderr echo is informational, applied pre-`--query-errors`, and silent-able with `2>/dev/null` for scripts that prefer the stdout-only contract | `none` default keeps SDK parity for casual one-off use; opting into an envelope is an opt-in scripts make explicitly. Stdout output + the `exit_code == 3` discriminator gives a clean machine-readable error channel; the stderr echo prevents `--query-errors` from silently swallowing diagnostic info on unexpected error shapes | `_handle_exception` + `_format_output` in `formatter.py` |
| Exit code on failure | Python defaults | `0` success, `1` uncaught exception from the SDK call (default `--output-errors=none`), `2` user-input invalid, `3` envelope-rendered API / connection error. See [`exit-codes.md`](exit-codes.md) | Lets scripts distinguish kinds of failures | `_handle_exception` in `formatter.py`; `sys.exit(2)` sites in `session.py` |
| `--all-items` / `--page-size` / `--max-items` | (Not in SDK) | Accepted as v2-era deprecation aliases of the new flags. `--all-items` is a no-op (the new default already walks every page); `--page-size N` aliases `--limit N`; `--max-items N` aliases `--item-limit N`. Each emits a stderr warning, and the wrapper rejects passing both an alias and its replacement | v2 → v3 migration path; aliases scheduled for removal in v3.1+ | `_make_command` in `cli.py` (deprecation branches around `all_items` / `page_size` / `max_items`) |

## Decisions deferred (v3.0)

Entries marked "Not exposed (v3.0)" above are explicitly provisional.
Recording the original context so a future re-evaluation has it on hand:

- **`_return_http_data_only` / `_preload_content`** — both are
  transport-level kwargs orthogonal to the v3 pagination scope. They
  remain unexposed in v3.0 to keep the formatter contract simple
  (dict / list only). HTTP status codes and headers can still be
  observed with `--debug` in the meantime. Adding either in v3.x is a
  non-breaking minor bump; removing them later would be breaking, so
  the conservative direction is "do not add until there is a clear
  user need".
- **Audit log endpoint infinite-loop guard** — the `PageIterator` of
  the audit log events endpoint can spin forever when no events match
  the filter (the next-page cursor never empties). The CLI takes no
  protective action in v3.0, matching raw SDK behavior; Ctrl-C remains
  the only way to stop. A future revisit may refuse the iterator path
  on this endpoint and require `--full-payload`.
