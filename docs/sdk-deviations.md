# SDK deviations

Catalog of every intentional gap between `asana-api-cli` and the
`python-asana` SDK — unexposed SDK features, behavior changes, and
CLI-only additions — framed by [Constitution #1 (parity is the top
priority) and #2 (security overrides parity)](principles.md#constitution).

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
(e.g. `--timeout` remapping the SDK's `_request_timeout` per-call kwarg
to a global flag) are not marked — the underlying feature exists in the
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

## CLI behavior that differs from raw SDK behavior

| Area | SDK behavior | CLI behavior | Reason | Code anchor |
|---|---|---|---|---|
| Personal access token in `--debug` output (`Authorization: Bearer <token>` / `Basic <token>`) | Printed verbatim by `http.client`'s wire-level `print()` when debug level is set; the SDK's own loggers do not log headers, but `http.client` does | The token is masked before the line is written (mask format: see `_default_mask_token`) | Constitution #2 (security overrides parity). Personal access tokens must never appear in user-visible output, so debug logs stay safe to paste into bug reports | `HttpClientAuthRedactor` and `_default_mask_token` in `redactor.py`; installed by `AsanaSession.__init__` when `--debug` is set |
| `_request_timeout` per-call kwarg | Per-call kwarg on each SDK method | Surfaced as the global `--timeout` flag; the session wraps `ApiClient.call_api` to inject the kwarg | A CLI invocation is a single API call from the user's perspective, so a global flag is more ergonomic than a per-method surface | `--timeout` option in `cli.py:main`; `AsanaSession._install_timeout` in `session.py` |
| Multipart filename with non-ASCII characters | Emits `filename="..."` only; non-ASCII bytes round-trip is undefined | Emits the RFC 5987 `filename*=UTF-8''<percent-encoded>` parameter alongside `filename=` when `--multibyte-filenames` is set | SDK gap: the Asana API needs RFC 5987 to round-trip non-ASCII attachment names. Opt-in so default upload semantics do not change silently | `--multibyte-filenames` option in `cli.py:main`; `MultibyteFilenameSupport` in `session.py` |
| Default pagination return shape | `Configuration.return_page_iterator = True` produces a `PageIterator` (iterator object) | The iterator is walked to completion (`list(result)`) inside the session context and printed as a flat list of items | (1) An unwalked iterator cannot be serialized to stdout — the CLI must materialize a complete payload. (2) Materializing inside the session context keeps `HttpClientAuthRedactor` active for every page request; lazy iteration after the session exits would leak `Authorization` headers on pages 2..N (constitution #2 tie-in) | `_make_command` in `cli.py` (`paginatable and not no_return_page_iterator and not full_payload` branch) |
| `--output FORMAT` | (Not in SDK — SDK returns Python objects) | Renders the response into one of `json` / `table` / `csv` / `text` (default `json` — canonical, lossless) | Shell ergonomics: CLI output must be text. The four formats serve different consumer types (machines via JSON, humans via table, spreadsheets via CSV, scripts via text) | `--output` option and `_format_output` in `formatter.py` |
| `--query EXPR` | (Not in SDK — SDK returns Python objects) | Pipes the response through jq with the given expression; each yield is rendered separately according to the chosen output format | Shell ergonomics: extracts fields / items inline without spawning a separate `jq` process. Mirrors `aws --query` | `--query` option and the `jqlib.all` call in `_format_output` in `formatter.py` |
| `--csv-bom` | (Not in SDK — SDK returns Python objects) | Prepends a UTF-8 BOM to CSV output when set | Windows ergonomics: Excel on Windows needs the BOM to decode UTF-8 CSV correctly. Opt-in so Unix pipelines stay clean | `--csv-bom` option and `_print_csv` in `formatter.py` |
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
