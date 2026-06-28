# Development

For contributor setup (cloning, `uv sync`, running tests, code style, PR
rules), see [`CONTRIBUTING.md`](../CONTRIBUTING.md). This document covers
development and maintenance: installing from source, the project layout, the
`asana` SDK version-bump workflow, and trying shell completion.

The `asana_api_cli` package is internal to the CLI. Importing it directly
from Python is possible but unsupported — names and behavior may change
in any release without notice.

## Install from source

```bash
pipx install .
```

## Project layout

For the `src/asana_api_cli/` module roles, see [`architecture.md`](architecture.md).

```
tests/
├── test_cli*.py                # CLI shape, invocation, and surface snapshot
├── test_*.py                   # Per-module unit tests (formatter, session, ...)
├── e2e/                        # Real-API tests (opt-in); see tests/e2e/README.md
└── fixtures/
    ├── cli_surface.json        # Canonical CLI surface for the bundled SDK
    └── generate_python/        # Golden snapshots for --generate-python output

tools/
├── e2e_init.py                 # One-time fixture provisioner for tests/e2e/
├── publish_pypi.sh             # Build (python -m build) + twine upload to PyPI
└── tag_version.sh              # Create the annotated git tag from the pyproject version
```

## Bumping the `asana` SDK

The CLI surface snapshot test (see
[`architecture.md`](architecture.md#surface-snapshot-guardrail)) catches
group/command/option churn introduced by an SDK bump. The `dependencies` lower
bound in `pyproject.toml` is kept wide (`asana>=5.0.2,<6`) — the CLI adapts to
whatever SDK is installed — so a bump tracks the *snapshot*, not the floor.
Procedure:

1. `uv sync --upgrade-package asana` to relock and install the new SDK
   (`uv.lock` moves; `pyproject.toml` stays).
2. Bump `_SNAPSHOT_ASANA_VERSION` to the new version in **both**
   `tests/test_cli_surface.py` and `tests/test_generate_python_snapshots.py` —
   otherwise those snapshot guards silently *skip* on the new version instead
   of checking it.
3. `uv run pytest` — failures in `test_cli_surface.py` /
   `test_generate_python_snapshots.py` print the diff.
4. Review the diff; describe user-visible changes in `CHANGELOG.md`.
5. Regenerate the fixtures (exact command in each test's module docstring):
   - `tests/fixtures/cli_surface.json` — the CLI surface. Besides commands and
     options it pins each command's `paginatable` / `returns_iterator` /
     `does_upload` classification, so a new array-response or upload endpoint
     surfaces here as a fixture diff — there is no separate hand-maintained set.
     (`tests/test_sdk_boilerplate.py` independently proves those two classifiers
     still match the SDK source on the installed version.)
   - `tests/fixtures/generate_python/*.py` via
     `UPDATE_GENERATE_SNAPSHOTS=1 uv run pytest tests/test_generate_python_snapshots.py`.
6. Verify Asana auth is still Bearer-token-only — confirm the new SDK still
   wires up only the token scheme:

   ```bash
   grep -rh "auth_settings = \[" .venv/lib/python*/site-packages/asana/api/ | sort -u
   ```

   If the output names any auth scheme other than `personalAccessToken` (the
   bundled SDK prints the single line
   `auth_settings = ['personalAccessToken']  # noqa: E501`), the SDK has started
   wiring up additional auth schemes — revisit whether those Configuration
   fields should now be exposed, and update `docs/sdk-deviations.md`.
   `tests/test_sdk_boilerplate.py` also pins the settable `Configuration`
   set, so a new auth-related property fails that guard too.
7. *(Optional, soft improvement)* Review the group descriptions when
   the SDK adds or removes resource groups:
   - [`docs/api-groups.md`](api-groups.md) is the authoritative table
     (CLI group → Asana reference link → short description).
   - `_GROUP_DESCRIPTIONS` in `src/asana_api_cli/cli.py` mirrors that
     table; the `test_group_descriptions_match_docs` test asserts the
     two stay in sync.
   - Groups that are new in this SDK render with a fallback English
     name derived from the class name (e.g. `FooBar` → "Foo bar") so
     the CLI keeps working without action. Add curated entries to both
     the doc and the dict for richer wording.
   - Removed groups can stay in both places — they're harmless dead
     data and re-engage on downgrades.
   - Source descriptions from
     [developers.asana.com/llms.txt](https://developers.asana.com/llms.txt)
     (an AI-friendly Markdown index of the reference) and/or the
     individual `/reference/<group>.md` pages.
8. Commit `uv.lock`, the two `_SNAPSHOT_ASANA_VERSION` bumps, the regenerated
   fixtures (`tests/fixtures/cli_surface.json` and any `generate_python/*`
   snapshots), and `CHANGELOG.md` together (plus any group-description edits
   from step 7).

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
asana-api tasks get-tasks --ssl-ca-cert <TAB>   # path completion for FILE-typed options
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
