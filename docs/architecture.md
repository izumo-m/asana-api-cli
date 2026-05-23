# Architecture

`asana-api-cli` is a **runtime-introspection wrapper** around `python-asana`. The CLI command tree is built at import time by walking every `*Api` class on the installed `asana` package and generating Click commands per method. There is no codegen step — the static `tools/codegen.py` script was removed in v2.0.0.

## File layout

The source tree is deliberately small. Seven hand-written modules under `src/asana_api_cli/`:

| File | Role |
|---|---|
| `cli.py` | Runtime introspection + Click command tree |
| `session.py` | SDK client + body/workspace resolution helpers; installs the debug redactor and the multibyte-filename patch |
| `formatter.py` | Output formatting (`json` / `table` / `csv` / `text`) + the `@formatted` decorator |
| `click_ext.py` | `LazyGroup` for cheap top-level `--help`; mixins propagating global options to subgroups |
| `redactor.py` | `HttpClientAuthRedactor` — masks `Authorization` headers in `http.client` debug output (stdlib-only, copyable) |
| `structured_arg.py` | Hybrid value parser for structured CLI options (`--retry-strategy`, `--api-key`, `--api-key-prefix`): accepts `k=v,k=v` shorthand, JSON object, or `@path` |
| `version.py` | `version_string()` used by `--version` |

If you find yourself wanting to add an eighth module, reconsider — the current shape keeps cognitive load low. Add to one of these seven unless the new concern truly doesn't fit any of them.

## How commands are constructed

At import time, `cli.py` walks every `*Api` class on `asana` and produces:

1. One Click subgroup per `*Api` class (e.g. `TasksApi` → `tasks`, `AuditLogAPIApi` → `audit-log-api`).
2. One Click command per method on that class (e.g. `get_tasks` → `get-tasks`).
3. Click options per docstring `:param:` line, with name conversion (`snake_case` → `kebab-case`, `_gid` suffix stripped, so `task_gid` → `--task`).

Method-level introspection is **deferred per group**: top-level `asana-api --help` only enumerates the group names, so its cost stays flat as the SDK grows.

**The entry point for changing command shape is `_make_command()` in `cli.py`.** Any change to how an SDK method becomes a CLI command — adding a pagination flag, hiding an SDK param, deprecation aliases, renaming `--task` etc. — is an edit to `_make_command()`.

## CLI surface snapshot test

`tests/test_cli_surface.py` deep-compares `introspect_to_manifest()` output against `tests/fixtures/cli_surface.json`. The fixture captures every group, command, and docstring-derived option signature. Any unintended change (typically from an `asana` SDK bump that adds/removes/renames a method) fails this test loudly.

**What the manifest tracks**: only docstring-derived parameters (`op.opts_params`). Synthetic options invented inside `_make_command` (pagination control flags like `--page-limit` / `--item-limit` / `--no-return-page-iterator` / `--full-payload`, deprecation aliases like `--all-items`) do **not** appear in the manifest. Adding such a flag does not require regenerating the fixture.

## When bumping the `asana` dependency

1. Edit `dependencies` in `pyproject.toml` to raise the lower bound.
2. `uv sync` to install the new SDK.
3. `uv run pytest` — failures in `test_cli_surface.py` print the diff.
4. Review the diff; describe user-visible changes in `CHANGELOG.md`.
5. Regenerate the fixture (exact command in `tests/test_cli_surface.py`'s module docstring).
6. Verify the no-op disclosure on `--username` / `--password` / `--api-key` /
   `--api-key-prefix` still holds for the new SDK. These four flags are
   exposed for `Configuration` parity but are inert in python-asana 5.2.4
   because every `*Api` method passes
   `auth_settings = ['personalAccessToken']` only. Re-check with:

   ```bash
   grep -rh "auth_settings = \[" .venv/lib/python*/site-packages/asana/api/ | sort -u
   ```

   If the output is anything other than the single
   `auth_settings = ['personalAccessToken']` line, the disclosure in
   the `--help` text (and `docs/cli-sdk-mapping.md` /
   `docs/sdk-deviations.md`) needs to be updated to reflect what the
   new SDK actually wires up. Bump the python-asana version pin in the
   `--help` strings too.
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
8. Commit `pyproject.toml`, `uv.lock`, `tests/fixtures/cli_surface.json`, and `CHANGELOG.md` together.

## Output formats

The four formats (`json` / `table` / `csv` / `text`) and the `--csv-bom` flag are intentionally retained:

- `json` is canonical and lossless.
- `csv` has Excel-on-Windows users; `--csv-bom` was added in v2.1.0 for that workflow.
- `table` and `text` cover casual eyeballing and shell-script consumption respectively.

## Pagination

Paginatable commands (those whose SDK method has a `:param limit:`) expose every SDK pagination input as a 1:1 CLI flag:

| CLI flag | SDK input |
|---|---|
| `--limit N` | `opts["limit"]` |
| `--offset T` | `opts["offset"]` |
| `--page-limit N` | `Configuration.page_limit` |
| `--item-limit N` | kwarg `item_limit=N` |
| `--return-page-iterator` / `--no-return-page-iterator` | `Configuration.return_page_iterator` |
| `--full-payload` | kwarg `full_payload=True` |

Default behavior is the SDK iterator path — the CLI walks every page and prints a flat list of items. `--all-items`, `--page-size`, `--max-items` are kept as v2 deprecation aliases that emit a stderr warning and forward to the equivalent flag above.
