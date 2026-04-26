# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/izumo-m/asana-api-cli/compare/v1.5.0...HEAD
[1.5.0]: https://github.com/izumo-m/asana-api-cli/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/izumo-m/asana-api-cli/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/izumo-m/asana-api-cli/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/izumo-m/asana-api-cli/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/izumo-m/asana-api-cli/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/izumo-m/asana-api-cli/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/izumo-m/asana-api-cli/releases/tag/v1.0.0
