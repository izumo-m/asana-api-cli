# Development

For contributor setup (cloning, `uv sync`, running tests, code style, PR
rules), see [`CONTRIBUTING.md`](../CONTRIBUTING.md). This document covers
the internal architecture.

The `asana_api_cli` package is internal to the CLI. Importing it directly
from Python is possible but unsupported — names and behavior may change
in any release without notice.

## Install from source

```bash
pipx install .
```

## Project layout

The CLI command tree is built **at runtime** by introspecting the installed
`python-asana` SDK. There is no codegen step. When the SDK is upgraded, the
CLI surface follows automatically; the snapshot test guards against silent
breaking changes.

```
src/asana_api_cli/
├── __init__.py
├── session.py            # SDK client wrapper + helpers used by the CLI
├── redactor.py           # http.client debug-output Authorization redactor (stdlib-only, copyable)
├── formatter.py          # CLI output formatting (@formatted decorator)
├── click_ext.py          # LazyGroup + global-options propagation mixins
├── version.py            # version_string()
└── cli.py                # Runtime introspection + click command tree

tests/
├── test_cli*.py                # CLI shape, invocation, and surface snapshot
├── test_*.py                   # Per-module unit tests (formatter, session, ...)
├── e2e/                        # Real-API tests (opt-in); see tests/e2e/README.md
└── fixtures/
    └── cli_surface.json        # Canonical CLI surface for the bundled SDK

tools/
└── e2e_init.py                 # One-time fixture provisioner for tests/e2e/
```

- **`session.py`** — builds `asana.Configuration` + `ApiClient`, forwards
  the `return_page_iterator` and `page_limit` kwargs to the matching SDK
  `Configuration` properties, installs `HttpClientAuthRedactor` (from
  `redactor.py`) when `--debug` is set, optionally augments multipart
  uploads with the RFC 5987 `filename*=UTF-8''` parameter via
  `MultibyteFilenameSupport` when `--multibyte-filenames` is set, and
  exposes `resolve_body` / `resolve_workspace` to `cli.py`.
- **`redactor.py`** — stdlib-only module exposing `HttpClientAuthRedactor`,
  a context manager that masks `Authorization` headers in
  `http.client`'s wire-level debug output. Has no third-party
  dependencies so the file is copyable as-is into other projects.
- **`formatter.py`** — supports `json` / `table` / `csv` / `text` output and
  `--query` (jq).
- **`click_ext.py`** — `LazyGroup` for cheap top-level help, plus the
  `GroupWithGlobalOptions` / `CommandWithGlobalOptions` pair that lets
  `--debug`, `--access-token`, etc. work at any level of the tree.
- **`cli.py`** — introspects every `*Api` class on the installed `asana`
  package and builds click commands per method. Method-level introspection
  is deferred per group so top-level `--help` is cheap.

## Bumping the SDK

See [`architecture.md`](architecture.md#cli-surface-snapshot-test) for the
procedure and how the CLI surface snapshot test guards the change.

## Trying shell completion locally

`asana-api` is built with Click, which generates dynamic completion scripts.
To experiment with completion without touching your real shell config, spawn
an isolated sub-shell via `uv run` and install completion only inside it:

```bash
uv run $SHELL
```

`uv run $SHELL` puts `.venv/bin` on `PATH` so `asana-api` is callable
directly. Inside the sub-shell, evaluate the appropriate completion source
for your shell:

```bash
# bash
eval "$(_ASANA_API_COMPLETE=bash_source asana-api)"

# zsh
eval "$(_ASANA_API_COMPLETE=zsh_source asana-api)"

# fish
_ASANA_API_COMPLETE=fish_source asana-api | source
```

Then try interactive completion:

```text
asana-api tasks get-tasks --<TAB><TAB>      # all options, including --debug etc.
asana-api tasks get-tasks --de<TAB>         # completes to --debug
asana-api tasks --<TAB><TAB>                # global options also work on subgroups
asana-api tasks get-tasks --ca-cert <TAB>   # path completion for FILE-typed options
```

Exit with `exit` (or Ctrl-D) to drop completion and return to your normal
shell. Nothing is persisted.

For a quick non-interactive smoke test that doesn't need a sub-shell, drive
the bash completion protocol directly:

```bash
COMP_WORDS="asana-api tasks get-tasks --" COMP_CWORD=3 \
  _ASANA_API_COMPLETE=bash_complete uv run asana-api
```

This prints the candidate list as `type,value` lines.
