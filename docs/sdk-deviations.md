# SDK deviations

Catalog of every intentional gap between `asana-api-cli` and the
`python-asana` SDK — unexposed SDK features, behavior changes, and
CLI-only additions — framed by [Constitution #1 (parity is the top
priority) and #2 (security overrides parity)](principles.md#constitution).
Companion of [`cli-sdk-mapping.md`](cli-sdk-mapping.md), which catalogs
the *parity* side (which built-in option maps to which SDK input). The
*behavior* of the CLI-only flags below is documented in
[`usage.md`](usage.md); this file records the *why*.

This document is **reference**, not required reading for code changes.
The CLI surface snapshot test (`tests/test_cli_surface.py`) and per-feature
tests are the source of truth for what is wired in code. Treat this file
as the **why** behind the gaps those tests encode, so future maintainers
can tell intentional non-parity from an oversight.

Code anchors below name functions/classes (not line numbers) so the
references stay valid across edits.

## Convention: `(asana-api: extension)` marker

Every CLI-only flag cataloged below (the rows whose "SDK behavior" column
is "Not in SDK") ends its `--help` text with the `(asana-api: extension)`
marker so users can tell at a glance which options have no SDK
counterpart. The catalog and the marker are kept in sync: adding a new
CLI-only flag means adding both a row here and the marker to the help
text; removing one without the other is a bug.

Flags that *remap* an existing SDK input to a different CLI surface
(e.g. `--task` exposing the SDK's `task_gid` positional argument as a
named option) are not marked — the underlying feature exists in the
SDK, only the surface differs. Behavior-only deviations such as the
`--debug` token redaction (constitution #2) are likewise not marked,
because the flag itself is SDK-derived; the deviation is enforced by a
stronger principle.

## SDK features the CLI does not expose

| SDK feature | CLI status | Reason | Code anchor |
|---|---|---|---|
| `async_req` kwarg | Filtered out at command construction time | The SDK returns a `multiprocessing.pool.ApplyResult` from a thread pool; a shell cannot consume one. The CLI is synchronous-only by design | `_parse_params` in `cli.py` (`params.pop("async_req", None)`) |
| `_return_http_data_only` kwarg | Not exposed (v3.0) | The `(data, status, headers)` tuple shape is incompatible with the formatter pipeline (dict / list only). `--debug` exposes HTTP wire info as a workaround. Re-evaluation deferred to a future minor release | — |
| `_preload_content` kwarg | Not exposed (v3.0) | Raw `urllib3.HTTPResponse` cannot be rendered by the formatter pipeline. Same reasoning as `_return_http_data_only` | — |

## Configuration properties that cannot be set from the CLI

Most other `asana.Configuration` properties have a 1:1 CLI flag (see
[`cli-sdk-mapping.md`](cli-sdk-mapping.md)). The properties below have
no flag because their values are Python objects that cannot be
constructed from a command-line string. (The inert auth fields
`username` / `password` / `api_key` / `api_key_prefix` are also not
exposed, for a different reason — see *Inert auth fields* below.)

| SDK property | Type | Reason |
|---|---|---|
| `Configuration.refresh_api_key_hook` | Python callable | Functions cannot be supplied as a CLI argument |
| `Configuration.logger` | `dict[str, logging.Logger]` | Live `Logger` instances cannot be constructed from a CLI argument; tuning the log format / file is still possible via `--logger-format` / `--logger-file` |
| `Configuration.logger_stream_handler` | `logging.StreamHandler` | Handler instance |
| `Configuration.logger_file_handler` | `logging.FileHandler` | Handler instance |
| `urllib3.util.retry.Retry.history` | `tuple[RequestHistory, ...]` | Retry's internal execution log, not a configuration knob — explicitly excluded from `--retry-strategy`'s field set |

## Inert auth fields (not exposed)

`Configuration.username` / `password` / `api_key` / `api_key_prefix` exist on
the SDK's Configuration only as swagger-codegen boilerplate (the HTTP basic
auth + apiKey auth schemes the generator always emits). Asana's API uses
**Bearer tokens only**: `Configuration.auth_settings()` returns just the
`token` entry, and every `*Api` method passes
`auth_settings = ['personalAccessToken']`, so these four fields never emit a
header. Asana does not document basic auth at all, and API keys are officially
deprecated and being shut off (personal access tokens are the replacement).

The CLI therefore does **not** expose `--username` / `--password` /
`--api-key` / `--api-key-prefix` — surfacing inert flags would mislead users
into thinking those auth modes work. This is a deliberate deviation from
"expose every settable `Configuration` property"
([constitution #1](principles.md#constitution)): (a) it removes a misleading
no-op surface, (b) the faithful auth path stays reachable via `--access-token`
(PAT / Service Account token), and (c) it is cataloged here. Use
`--access-token` (or `$ASANA_ACCESS_TOKEN`) to authenticate.

## CLI behavior that differs from raw SDK behavior

The **CLI behavior** column is a summary; the full behavior of each flag is in
[`usage.md`](usage.md). The **Reason** column is the rationale these deviations
exist (constitution #1 (c)).

| Area | SDK behavior | CLI behavior | Reason | Code anchor |
|---|---|---|---|---|
| Personal access token in `--debug` output | Printed verbatim by `http.client`'s wire-level `print()`; the SDK's own loggers do not log headers | The token is masked before the line is written (mask format: `_default_mask_token`) | Constitution #2 (security overrides parity): tokens must never appear in user-visible output, so debug logs stay safe to paste into bug reports | `HttpClientAuthRedactor` / `_default_mask_token` in `redactor.py`; installed on session entry (`AsanaSession.open`) under `--debug` |
| Multipart filename with non-ASCII characters | Emits `filename="..."` only, omitting the RFC 5987 `filename*=` parameter, so non-ASCII names are stored garbled | Emits the RFC 5987 `filename*=` parameter when `--multibyte-filenames` is passed ([`usage.md`](usage.md#file-uploads)) | Works around a `python-asana` bug: without `filename*=` the server cannot decode non-ASCII attachment names. Opt-in so default upload semantics do not change silently; exposed only on upload commands | `--multibyte-filenames` built per-command in `cli.py:_make_command`; `MultibyteFilenameSupport` in `multibyte_filename.py`; guarded by `tests/test_sdk_boilerplate.py` |
| Default pagination return shape | `return_page_iterator=True` yields a lazy iterator (`PageIterator` / `EventIterator`) | The iterator is walked to completion (`list(result)`) inside the session context and printed as a flat list ([`usage.md`](usage.md#pagination)) | (1) An unwalked iterator cannot be serialized to stdout. (2) Materializing inside the session keeps `HttpClientAuthRedactor` active for every page request; lazy iteration after the session exits would leak `Authorization` headers on pages 2..N (constitution #2) | post-judge `isinstance` check in `_make_command`'s inner callback in `cli.py` |
| `--output` / `--query` / `--csv-bom` | (Not in SDK — it returns Python objects) | Render / jq-filter / BOM-prefix the response ([`usage.md`](usage.md#output-formats)) | Shell ergonomics: CLI output must be text, and consumers differ (machines / humans / spreadsheets / scripts); `--csv-bom` covers Excel-on-Windows; `none` covers the "exit code only" case | `_format_output` / `_print_csv` in `formatter.py` |
| `--exception-output` / `--exception-query` (v3.1+) | (Not in SDK — Python raises per HTTP error) | Catch the exception, echo it to stderr, and optionally render a structured envelope on stdout ([`usage.md`](usage.md#error-handling)) | Gives scripts a structured stdout error channel keyed by exit `3`, while the stderr echo keeps the response readable without opting in | `_echo_exception_only` / `_handle_exception` / `_format_output` in `formatter.py` |
| Exit code on failure | Python defaults | `0` success, `1` unhandled error (catch-all, incl. an uncaught SDK exception under default `--exception-output=none`), `2` user-input invalid, `3` envelope-rendered error ([`usage.md`](usage.md#exit-codes)) | Lets scripts distinguish the defined failure kinds (`2` / `3`) from everything else (`1`) | `formatted` (exit 1) / `_handle_exception` (exit 3) in `formatter.py`; exit 2 mostly via `click.BadParameter` in `cli.py` (`resolve_body` / `resolve_workspace`) and `structured_arg.py`, plus `sys.exit(2)` in `session.py` (`from_env`) / `formatter.py` (`_format_output`) |
| `--all-items` / `--page-size` / `--max-items` | (Not in SDK) | v2-era deprecation aliases of the v3 flags ([`usage.md`](usage.md#deprecated-aliases)) | v2 → v3 migration path; scheduled for removal in a future release | `_apply_deprecated_aliases` in `cli.py` (warning + forwarding), called from `inner_callback`; option injection in the `paginatable` block of `_make_command` |
| SDK arg/opt name colliding with a built-in CLI flag | (Not in SDK — no flag namespace) | The colliding SDK arg/opt is exposed as `--sdk-<name>`; the built-in keeps its bare name and the SDK param keeps its real name in dest / call / label | The CLI adds extension flags that share the `--` namespace; on collision the SDK param must stay reachable (constitution #1 (b)), and prefixing the SDK side keeps every built-in flag stable across commands (no per-command polysemy) | `_static_reserved_flags` / `_decls` in `cli.py`; `tests/test_cli.py::TestFlagCollisions` |

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
