# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **New `--generate-python` mode prints equivalent `python-asana` code instead
  of running the call.** A global flag (valid anywhere in the command path) that
  emits a self-contained script — config, the SDK call, and your `--output` /
  `--query` / `--exception-output` / `--debug` / `--multibyte-filenames`
  choices — making no network call and needing no token, so a working
  invocation becomes copy-pasteable SDK code. Credential-bearing values are
  masked in the emitted script (see the Security entry below). See
  [docs/usage.md](docs/usage.md#generating-python-code).

### Security

- **`--generate-python` masks credentials in the emitted script.** A non-empty
  `--access-token`, an `Authorization` / `Proxy-Authorization` header given
  via `--set-default-header` / `--header-params`, and the password in a
  `--proxy` URL never appear in the generated text — neither in the
  configuration lines nor in the `# Equivalent to:` comment. Tokens keep only
  their last 6 characters (`...abc123`), matching the `--debug` trace mask;
  `Basic` credentials and proxy passwords are fully masked (no tail reveal);
  a value too short to be a real token (a dummy) stays verbatim. See
  SECURITY.md.
- **`--debug` now masks `Authorization` / `Proxy-Authorization` headers of any
  scheme.** Previously only `Authorization: Bearer` / `Basic` values were
  masked in the wire trace; an `Authorization` header carrying a custom
  scheme or a bare token (e.g. set via `--set-default-header`) passed through
  verbatim, and `Proxy-Authorization` masking is now an explicit, tested
  contract (including the proxy `CONNECT` tunnel chunk). A `Basic` credential
  is now fully masked with no last-6 reveal: its value is base64 of
  `user:password`, so a tail reveal would expose password characters.

## [3.1.3] - 2026-06-06

### Fixed

- **CSV output is now RFC 4180-compliant and byte-identical on every platform.**
  Each record is terminated with CRLF (`\r\n`), while a newline inside a quoted
  field is preserved verbatim as LF (`\n`). Previously records were terminated
  with LF, and on Windows a newline inside a field was corrupted into CRLF — so
  the same call could produce different bytes on different platforms.

## [3.1.2] - 2026-06-05

### Fixed

- **`--access-token` now follows the same last-wins rule as every other global
  option.** Previously an explicit empty `--access-token ""` was silently
  ignored, so it could not override a token set earlier on the command line
  (for example one injected by a shell alias). Now the later occurrence wins —
  empty included — so `--access-token ""` clears the earlier token and
  authentication falls back to `$ASANA_ACCESS_TOKEN`. A non-empty
  `--access-token` already took precedence and is unchanged.

## [3.1.1] - 2026-06-01

### Fixed

- The session-wide HTTP-header global, added in 3.1.0 as `--default-header`,
  is now **`--set-default-header`** — matching the SDK method it drives
  (`ApiClient.set_default_header`) and this project's convention of naming a
  flag after its SDK destination. If you used `--default-header` with 3.1.0,
  switch to `--set-default-header`.

## [3.1.0] - 2026-05-31

### Added

- **`--user-agent VALUE`** and **`--default-header NAME=VALUE`** (repeatable) —
  session-wide global options that set the `ApiClient`'s `user_agent` and
  default request headers, sent on **every** request (unlike the per-call
  `--header-params`). On a key collision the session-wide default wins over
  `--header-params` (the SDK merges defaults on top). Custom headers are **not**
  redacted in `--debug` output — see [SECURITY.md](SECURITY.md). See
  [`docs/usage.md`](docs/usage.md#global-options) and
  [`docs/cli-sdk-mapping.md`](docs/cli-sdk-mapping.md).

- **Structured error handling** — new per-command options `--exception-output
  {none|json|text|csv|table}` and `--exception-query EXPR` (the error-path twins of
  `--output` / `--query`, on every command). The SDK exception is
  always echoed to **stderr** in Python's top-level format (no traceback; for
  `ApiException` this already includes status / reason / headers / body). The
  default `none` exits `1` with no envelope, so the response payload (e.g. the
  412 sync-token body in events polling) is readable from stderr without extra
  flags. The other formats also render a `{exception, status, reason, body,
  headers}` envelope on **stdout** and exit `3`; `--exception-query` filters that
  envelope through `jq`. See [`docs/usage.md`](docs/usage.md#error-handling) and
  [`docs/sdk-deviations.md`](docs/sdk-deviations.md).

- **`--output none`** suppresses the success payload for side-effect-only
  operations (delete, update) where only the exit code matters. `--query` still
  runs, so a broken jq expression still surfaces as exit `2`. Symmetric with
  `--exception-output none`.

- **`--header-params VALUE`** (on every command) sends arbitrary HTTP request
  headers. Accepts shorthand `'k1=v1,k2=v2,...'`, a JSON object, or
  `@path/to/file.json` (same format as `--retry-strategy`). **Not redacted in
  `--debug` output** — see [`SECURITY.md`](SECURITY.md).

### Changed

- **SDK parameters that collide with a built-in flag are exposed as
  `--sdk-<name>`.** `typeahead-for-workspace`'s SDK `query` (the search string)
  was shadowed by the built-in `--query` (jq filter) and unreachable; it is now
  `--sdk-query`, while `--query` keeps its jq meaning on every command. The SDK
  param's `(opts: <name>)` help label still shows the real name.

- **Per-command options now follow the SDK method, not alphabetical order.**
  Path / body positionals list in function-signature order (e.g. `update-task`
  shows `--body` then `--task`), and the remaining options follow the SDK
  docstring's `:param` order (e.g. `--limit` / `--offset`, then the filters,
  then `--opt-fields`) instead of an alphabetical scatter — both mirror how the
  API documents the method.

- **Option `--help` now names the SDK destination.** Every option ends with a
  label showing where its value lands in `python-asana`: `(Configuration:
  <name>)` for client config (global flags), `(ApiClient: <name>)` for
  ApiClient-instance settings (`--user-agent`, `--default-header`), `(args:
  <name>)` for positional arguments (`--body`, path GIDs, `--workspace` when
  positional), `(opts: <name>)` for the method's `opts` dict (`--assignee`,
  `--opt-fields`, ...), `(kwargs: <name>)` for per-call kwargs, and
  `(asana-api: extension)` for CLI-only flags. (Path GIDs that 2.1.1 labeled
  `(SDK kwarg: ...)` are now `(args: ...)` — they are positional arguments,
  not kwargs.)

- **Iteration / per-call controls grouped by SDK scope.** The `Configuration`
  knobs `--page-limit` and `--return-page-iterator / --no-return-page-iterator`
  stay **global**. The per-call kwargs `--item-limit`, `--full-payload`,
  `--header-params`, and `--request-timeout` are now **options on every command**
  (the SDK accepts them on every method). `--request-timeout` was previously a
  global flag.

- **`--multibyte-filenames` is now a per-command option on file-upload commands**
  (e.g. `attachments create-attachment-for-object`), not a global flag — it only
  ever affected multipart uploads. Still opt-in, off by default.

- **Failure exit codes changed** — `1` / `2` / `3` by error class. See
  [`docs/usage.md`](docs/usage.md#exit-codes).

- **Nested values in `--output text` / `csv` / `table`** now render as JSON
  (`{"a":"b"}`) rather than Python `repr` (`{'a': 'b'}`). Same for
  `--exception-output`.

- **Auto-iteration is driven by the SDK return type.** Paged responses are
  walked when the SDK returns an iterator, rather than by a per-method
  pre-judgement in the CLI. Behavior is unchanged for existing methods, and the
  CLI no longer needs an update when an SDK release changes a method's
  pagination shape.

- **Deprecation aliases now combine with their replacements.** `--page-size` /
  `--max-items` no longer error when given alongside `--limit` / `--item-limit`;
  the replacement wins and the deprecation warning still fires.

- **Documentation reorganized.**

### Removed

- **Removed the inert auth flags** `--username`, `--password`, `--api-key`, and
  `--api-key-prefix` — Asana authenticates with Bearer tokens only, so they
  never did anything. Use `--access-token` (or `$ASANA_ACCESS_TOKEN`).

### Fixed

- **`--debug` no longer leaves `http.client` wire-level tracing globally
  enabled after the session closes.** The SDK's debug setter flips a process
  global (`http.client.HTTPConnection.debuglevel`); the CLI uninstalled its
  `Authorization`-masking redactor on exit but left that global on. Harmless
  for the one-shot CLI (the process exits), but a library that reuses
  `AsanaSession` across calls could print an unmasked `Authorization` header
  in a later non-debug session. `AsanaSession.close()` now restores the
  debuglevel together with the redactor.

## [3.0.0] - 2026-05-23

### Breaking changes (v2 → v3)

Several global flags renamed for 1:1 parity with `asana.Configuration`
property names. No deprecation aliases:

- `--temp-dir` → `--temp-folder-path`
- `--ca-cert` → `--ssl-ca-cert`
- `--timeout` → `--request-timeout`
- `--retries N` → `--retry-strategy total=N`

Default behavior of paginatable commands (e.g. `tasks get-tasks`) changed:
**now walks every page automatically** and returns a flat JSON list of
items. Previously the default was a single page. To restore the
single-page behavior, pass `--full-payload`.

### Deprecated

The following v2.x flags still work but emit a stderr warning and will
be removed in a future release:

- `--all-items` — now a no-op (walking every page is the default)
- `--page-size N` → use `--limit N`
- `--max-items N` → use `--item-limit N`

Combining a deprecated alias with its replacement (e.g.
`--page-size 50 --limit 100`) is rejected with a usage error.

### Added

- `--multibyte-filenames` global flag: emits RFC 5987
  `filename*=UTF-8''…` on multipart uploads so Asana correctly decodes
  non-ASCII attachment filenames (Japanese, Cyrillic, Greek, etc.).
  Off by default to match the underlying SDK behavior
  ([Asana Forum context](https://forum.asana.com/t/attachment-names-uploaded-with-asana-api-are-garbled-on-asanaweb/286200)).
- `--retry-strategy VALUE` global flag (replaces `--retries N`):
  overrides any field of `urllib3.util.retry.Retry`. Accepts shorthand
  `total=5,backoff_factor=1.5`, a JSON object, or `@path/to/file.json`.
  See [`docs/cli-sdk-mapping.md`](docs/cli-sdk-mapping.md).
- Eleven new global flags for previously-unreachable `Configuration`
  properties (full 1:1 SDK parity):
  - mTLS: `--cert-file PATH`, `--key-file PATH`
  - TLS: `--assert-hostname / --no-assert-hostname` (tri-state)
  - Networking: `--connection-pool-maxsize N`,
    `--safe-chars-for-path-param S`
  - Logging: `--logger-format FMT`, `--logger-file PATH`
  - No-op (parity only, inert in python-asana 5.2.4):
    `--username`, `--password`, `--api-key`, `--api-key-prefix`
- New pagination flags (1:1 with the SDK):
  - `--limit N` — per-page size (1-100)
  - `--page-limit N` — same as `--limit` via Configuration (parity flag)
  - `--item-limit N` — total cap on items returned
  - `--full-payload` / `--no-return-page-iterator` — get one
    `{data, next_page}` dict from one HTTP call instead of walking pages
- `--version` now shows the installed `click` version alongside
  `python-asana`
  (`asana-api, version 3.0.0 (python-asana 5.2.4, click 8.3.3)`).

### Changed

- `--help` overhauled for clarity: global options grouped by category;
  command groups carry meaningful one-line descriptions; long SDK
  descriptions no longer truncated; pagination has a consolidated
  "Pagination:" epilog explaining the two modes (iterator vs single
  payload); root help ends with a usage-examples block; subcommand
  help shows global options in compact form (no longer repeats the
  full ~70 lines). See `asana-api --help` for the new shape.
- `--task GID` (and every `*_gid` positional) renders with a `GID`
  metavar and inline example (`Task GID, e.g. 1234567890.`), making
  it obvious that Asana wants the numeric ID rather than a name.
- `--body JSON` on POST/PUT commands always shows the input-format
  hint (`Accepts inline JSON, @path/to/file, or - (stdin). Wrap
  payload in {"data": {...}}.`).
- `--no-verify-ssl` is now part of a toggle
  `--verify-ssl / --no-verify-ssl`. The old `--no-verify-ssl` form
  still works unchanged.
- The `--help` text of every CLI-only flag
  (`--multibyte-filenames`, `--output`, `--query`, `--csv-bom`) ends
  with an `[asana-api extension]` marker so users can distinguish CLI
  additions from SDK-derived options at a glance.

### Compatibility

- Lowered the `asana` SDK constraint from `>=5.2,<6` to `>=5.0.2,<6`.
  The CLI surface is built from whatever `*Api` classes the installed
  SDK exposes, so users on 5.0.x / 5.1.x get a working CLI with
  fewer command groups (5.2 added 9 new ones: AccessRequests,
  Budgets, Exports, ProjectPortfolioSettings, Rates, Reactions,
  Roles, TimeTrackingCategories, TimesheetApprovalStatuses).
  `--retry-strategy` — which relies on `Configuration.retry_strategy`
  introduced in python-asana 5.1 — is hidden from `--help` (and
  rejected as `No such option`) on 5.0.x; on 5.1+ it works as before.
  5.0.0 is excluded because its `api_client.call_api` had a
  `'list' object has no attribute 'items'` bug on no-opts endpoints
  (`delete-*` etc.) that was fixed in 5.0.2.

### Fixed

- On Windows, `--body -` (read JSON body from stdin) now decodes
  input as UTF-8 instead of the locale code page (e.g. cp932 on
  Japanese Windows).
- `--output csv` and `--output table` no longer crash when `--query`
  yields a mixed list whose first element is a dict and later
  elements are not (e.g. `--query '[.data[0], .data | length]'`).

## [2.1.1] - 2026-05-19

### Added

- `--help` for path-positional options whose `_gid` suffix has been stripped (e.g. `task_gid` → `--task`) now shows the original SDK kwarg name as `(SDK kwarg: task_gid)` in the help text, so users can map the CLI flag back to the python-asana API without guessing.

### Fixed

- `--max-items` now uses the SDK's native `item_limit` kwarg instead of the CLI's own page walker, eliminating a class of subtle divergence from SDK pagination semantics. The library helper `AsanaSession.fetch_capped` is removed; library callers should pass `item_limit=N` to the SDK method directly.

## [2.1.0] - 2026-05-16

### Added

- `--csv-bom` flag on commands with CSV output. CSV output is UTF-8 without a BOM by default; passing this flag prepends a UTF-8 BOM so Excel on Windows can decode non-ASCII characters correctly. Off by default so Unix pipelines stay clean.
- `HttpClientPrintRedactor` exported from `asana_api_cli.session`: a context manager that masks Bearer/Basic Authorization values in `http.client`'s wire-level debug output. Used internally when `--debug` is enabled, and usable standalone (`with HttpClientPrintRedactor(): ...`) for library callers who want the same redaction without the rest of the CLI.
- `AsanaSession` is now usable as a context manager (`with AsanaSession(token=...) as session: ...`) so the debug redactor is uninstalled cleanly on exit. An explicit `close()` method is also available. Existing code without `with` continues to work (the redactor stays installed for the lifetime of the process, which is fine for one-shot CLI use).

### Removed

- `--paginate` (deprecated since v1.5.0; use `--all-items` instead).

### Changed

- Renamed `AsanaSession`'s `paginate` keyword argument to `use_page_iterator` to avoid confusion with the (now removed) `--paginate` CLI flag. Library users calling `AsanaSession(token=..., paginate=True)` must switch to `AsanaSession(token=..., use_page_iterator=True)`.
- `--page-size` now validates the value at the CLI layer (1-100 per Asana's API spec) instead of forwarding out-of-range values to the server.
- `--max-items` now rejects negative values at the CLI layer. `--max-items 0` remains valid and returns `[]` without making any API call.
- Raised the lower bound on `jq` from `>=1.5` to `>=1.6` so `pipx install asana-api-cli` works on Windows (the `jq` PyPI package started shipping Windows wheels with 1.6.0).

### Fixed

- `--debug` printed the `Authorization: Bearer …` header (i.e. the access token) verbatim because the SDK enables `http.client`'s wire-level tracing. Bearer/Basic Authorization values are now partially redacted in debug output: only the last six characters of the token survive (e.g. `Authorization: Bearer ...abc123`) so a user juggling multiple accounts (work vs personal) can still tell which token is in use, while tokens shorter than 16 characters are fully redacted as `<REDACTED>`.
- `fetch_capped` (used by `--max-items`) could loop indefinitely on an empty page with a non-empty `next_page.offset`; it now breaks on zero-progress pages.
- `--max-items N` with N > 100 returned a 400 from the API instead of auto-paginating, because the CLI forwarded N as the per-page `limit` (Asana caps `limit` at 100). The per-page size is now held at 100 (or the explicit `--page-size`) regardless of `--max-items`, and pages are walked until N items have been collected. Regression since v1.5.0.
- JSON output containing non-ASCII characters (e.g. Japanese task names) could fail with `UnicodeEncodeError` on Windows where stdout defaults to the locale code page (cp932). The CLI now reconfigures stdout/stderr to UTF-8 at startup, matching the JSON spec's UTF-8 requirement (RFC 8259).
- CSV output produced doubled line endings (`\r\r\n`) on Windows because the `csv` module's default `\r\n` line terminator combined with text-mode stdout's `\n` → `\r\n` translation. CSV now emits `\n` and lets the stream handle the platform-specific translation.
- `--workspace` help text said `(falls back to ASANA_DEFAULT_WORKSPACE)` on every endpoint, including those where the env var is not used as a fallback (workspace marked optional in the SDK, e.g. `projects get-projects`, `tasks get-tasks`). The help now differentiates required-workspace endpoints (env-var fallback applies) from optional-workspace endpoints (env var not used).
- `--all-items --debug` leaked the raw `Authorization` header on every page beyond the first. The SDK's lazy `PageIterator` was iterated by the formatter after the session — and the `http.client` redactor it owns — had already exited the `with` block, so all but the first page's request hit `http.client.print` with the debug redactor uninstalled. The CLI now collapses the iterator inside the session scope so every page request happens while the redactor is still installed.
- `--body @<file>` with a non-UTF-8 file (e.g. a binary blob passed in by mistake) surfaced a raw `UnicodeDecodeError` traceback instead of a clean error message. The CLI now exits with "Body file is not valid UTF-8: ..." in that case.
- `--retries N` accepted negative integers and silently disabled retries (`urllib3.Retry(total=-1)` behaves as "already exhausted"). The option is now validated at the CLI layer as `>= 0`; pass `--retries 0` to explicitly disable retries.
- `--output csv` raised an unhandled `ValueError` when a later row contained a key not present in the first row, which happens routinely with Asana responses where optional fields (e.g. `due_on`) appear on some items but not others. CSV output now collects the union of keys across all rows; rows missing a field render with an empty cell.
- `--query EXPR` returned only the first value that jq yielded, so expressions like `.data[]` silently dropped all but the first match, and a no-match expression (e.g. `.data[] | select(...)` filtering everything out) raised an unhandled `StopIteration` traceback. `--query EXPR` is now equivalent to piping the output through `jq 'EXPR'`: each yielded value reaches the chosen output format (separate JSON document for `--output json`, line for `--output text`, row for `--output table` / `--output csv`); zero matches produce no output.

## [2.0.0] - 2026-05-08

### Changed

- **BREAKING**: The CLI command tree is now built at runtime from the
  installed `python-asana`. The CLI surface tracks whichever `asana`
  version is installed; new SDK endpoints surface without releasing a
  new asana-api-cli.
- Replaced the auto-generated CLI modules and `tools/codegen.py` with
  a single hand-written module (CLI behavior unchanged).
- Loosened runtime dependency constraints: `click>=8.0`, `jq>=1.5`,
  `tabulate>=0.9`, `asana>=5.2,<6`. The `<6` on `asana` is kept because
  SDK 6.x is expected to change introspection assumptions.
- Bumped dev and transitive dependencies.

## [1.5.0] - 2026-04-26

### Added

- `--all-items` option on paginatable subcommands to fetch every item (no cap). This is the canonical name; `--paginate` becomes a deprecated alias.
- `--page-size N` option on paginatable subcommands to tune the per-page request size.
- `--max-items N` option on paginatable subcommands to stop after fetching N items in total. The last request is automatically capped to the remaining count to avoid overfetching.
- Global options (`--debug`, `--access-token`, `--host`, `--proxy`, `--no-verify-ssl`, `--ca-cert`, `--retries`, `--timeout`, `--temp-dir`) now work at any level of the command tree, so `asana-api tasks get-tasks --debug` is equivalent to `asana-api --debug tasks get-tasks`. Shell completion offers them on every subcommand. When the same option is given at multiple levels, the more specific (later) one wins.

### Deprecated

- `--paginate` is now a deprecated alias for `--all-items`. Specifying it still works but prints a warning to stderr; it will be removed in a future release.

### Changed

- **BREAKING**: Removed the per-subcommand `--limit` option on paginatable subcommands. Use `--page-size` for per-page tuning and `--max-items` for total caps.
- **BREAKING**: Removed the global `--page-limit` option (redundant with `--page-size` in a single-shot CLI).
- `--offset` is preserved on paginatable subcommands so callers can walk `next_page.offset` themselves for manual pagination.
- `--max-items` cannot be combined with `--all-items` (or its deprecated alias `--paginate`); doing so raises an error.

## [1.4.0] - 2026-04-15

### Added

- GitHub Actions workflow to publish releases to PyPI automatically
  via Trusted Publishers (OIDC) when a `v*` tag is pushed.

### Changed

- `--help` for any subcommand or subgroup now also lists the global
  options (`--access-token`, `--host`, `--debug`, etc.) under a
  "Global Options" section, so they no longer have to be looked up
  from the top-level help.
- Faster `asana-api --help` and startup: subcommand modules are now
  loaded on demand instead of all at once.
- Rewrote `SECURITY.md` to lead with the private reporting channels
  and to be honest about the project's best-effort response.
- Reformatted the codebase with `ruff format`.
- **BREAKING**: Replaced the global `--token-env` option with
  `--access-token`, which now accepts the Asana personal access token
  directly (matching `asana.Configuration.access_token`).
  `ASANA_ACCESS_TOKEN` remains the default fallback. Users who relied
  on `--token-env MY_VAR` should switch to `--access-token "$MY_VAR"`.

## [1.3.0] - 2026-04-14

### Changed

- Relicensed from Apache-2.0 to MIT.

## [1.2.1] - 2026-04-13

### Changed

- Lowered minimum Python requirement to 3.10.

## [1.2.0] - 2026-04-13

### Removed

- **BREAKING**: Removed the `--default-workspace` option. Set the
  `ASANA_DEFAULT_WORKSPACE` environment variable instead.

### Fixed

- Workspace is no longer auto-filled from the environment for endpoints
  where the workspace argument is optional.

## [1.1.0] - 2026-04-12

### Changed

- Unified workspace/GID options across commands.
- Improved error handling and messages.

## [1.0.0] - 2026-04-12

- Initial release.

[Unreleased]: https://github.com/izumo-m/asana-api-cli/compare/v3.1.3...HEAD
[3.1.3]: https://github.com/izumo-m/asana-api-cli/compare/v3.1.2...v3.1.3
[3.1.2]: https://github.com/izumo-m/asana-api-cli/compare/v3.1.1...v3.1.2
[3.1.1]: https://github.com/izumo-m/asana-api-cli/compare/v3.1.0...v3.1.1
[3.1.0]: https://github.com/izumo-m/asana-api-cli/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/izumo-m/asana-api-cli/compare/v2.1.1...v3.0.0
[2.1.1]: https://github.com/izumo-m/asana-api-cli/compare/v2.1.0...v2.1.1
[2.1.0]: https://github.com/izumo-m/asana-api-cli/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/izumo-m/asana-api-cli/compare/v1.5.0...v2.0.0
[1.5.0]: https://github.com/izumo-m/asana-api-cli/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/izumo-m/asana-api-cli/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/izumo-m/asana-api-cli/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/izumo-m/asana-api-cli/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/izumo-m/asana-api-cli/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/izumo-m/asana-api-cli/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/izumo-m/asana-api-cli/releases/tag/v1.0.0
