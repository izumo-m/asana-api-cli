# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

- **BREAKING:** Removed the `--default-workspace` option. Set the
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

[Unreleased]: https://github.com/izumo-m/asana-api-cli/compare/v2.1.0...HEAD
[2.1.0]: https://github.com/izumo-m/asana-api-cli/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/izumo-m/asana-api-cli/compare/v1.5.0...v2.0.0
[1.5.0]: https://github.com/izumo-m/asana-api-cli/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/izumo-m/asana-api-cli/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/izumo-m/asana-api-cli/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/izumo-m/asana-api-cli/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/izumo-m/asana-api-cli/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/izumo-m/asana-api-cli/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/izumo-m/asana-api-cli/releases/tag/v1.0.0
