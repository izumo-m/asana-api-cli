# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
