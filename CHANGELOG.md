# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

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

[Unreleased]: https://github.com/izumo-m/asana-api-cli/compare/v1.3.0...HEAD
[1.3.0]: https://github.com/izumo-m/asana-api-cli/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/izumo-m/asana-api-cli/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/izumo-m/asana-api-cli/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/izumo-m/asana-api-cli/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/izumo-m/asana-api-cli/releases/tag/v1.0.0
