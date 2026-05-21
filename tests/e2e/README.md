# End-to-end tests

These tests run the CLI against the real Asana API and record HTTP traffic to
cassettes via [vcrpy](https://vcrpy.readthedocs.io/) + [pytest-recording].
They are **skipped by default**. Set `ASANA_PYTEST_ENABLE_E2E=1` to enable
collection — required for both record and replay modes.

## What this covers

| File | Scope |
|---|---|
| `test_smoke.py` | Single-workspace `get-workspace`; list `get-workspaces` (account-shape tolerant). |
| `test_pagination.py` | Every paginatable-command flag exposed by `tasks get-tasks` (`--limit`, `--offset`, `--page-limit`, `--item-limit`, `--no-return-page-iterator`, `--full-payload`) and the v2 deprecation aliases. |
| `test_crud.py` | `project` and `task` create → get → update → delete. |
| `test_attachments.py` | Attachment upload / get / delete across ASCII / Japanese text / binary content and Japanese filenames (the latter via the `--multibyte-filenames` flag). |

## Environment variables

| Name | When required | Purpose |
|---|---|---|
| `ASANA_PYTEST_ENABLE_E2E` | always | Non-empty value enables collection of e2e tests. |
| `ASANA_ACCESS_TOKEN` | live mode | Personal access token. Replay auto-injects a dummy token if absent — vcrpy never hits the network. |
| `ASANA_PYTEST_WORKSPACE` | live mode, and replay tests that reference `workspace_gid` | GID of the workspace under test. Cassettes store this as `${WORKSPACE_GID}` and substitute it back at load time. Treat the workspace as **test-dedicated** — CRUD tests create and delete projects / tasks in it. |

## One-time provisioning (live mode only)

Some tests rely on standing fixtures in the test workspace (e.g. a project
with 1500 tasks for pagination). `tools/e2e_init.py` is idempotent — safe
to re-run; it only creates / deletes what is needed to reach the target
state, with 5xx/429 retry and a 0.5 s minimum interval between writes to
stay under Asana's per-minute rate limit.

```bash
export ASANA_ACCESS_TOKEN=...
export ASANA_PYTEST_WORKSPACE=<test-dedicated workspace gid>
uv run python tools/e2e_init.py
```

Provisioned per workspace:

- Project `pagination-test` with 1500 tasks (`ptest-0001` .. `ptest-1500`).
- Project `pagination-test-small` with 50 tasks (`psmall-0001` .. `psmall-0050`); used to verify `--full-payload` / `--no-return-page-iterator` behavior *below* Asana's per-response cap (~1000 items).

The first full run takes roughly 12 minutes (1500 task creations × 0.5 s).

## Running

`pytest-recording` reuses cassettes unless `--record-mode` is given.

| Goal | Command |
|---|---|
| Replay (offline, no real token needed) | `ASANA_PYTEST_ENABLE_E2E=1 uv run pytest tests/e2e/` |
| Record from scratch | `rm -rf tests/e2e/cassettes && ASANA_PYTEST_ENABLE_E2E=1 uv run pytest --record-mode=all tests/e2e/` |
| Record only missing interactions | `ASANA_PYTEST_ENABLE_E2E=1 uv run pytest --record-mode=once tests/e2e/` |
| Re-record a single test (always delete the file first) | `rm tests/e2e/cassettes/<dir>/<test>.yaml && ASANA_PYTEST_ENABLE_E2E=1 ASANA_PYTEST_WORKSPACE=<gid> uv run pytest --record-mode=all tests/e2e/<file>::<test>` |

> **Important**: `--record-mode=all` **appends** to any existing cassette;
> delete the cassette file first for a clean recording.

After re-recording, review `git diff tests/e2e/cassettes/` and commit.

## Account-neutral templating

Cassettes are designed to be portable across Asana accounts. Values that
vary per account are templated as `${VAR}` placeholders at record time and
substituted back at replay time (see `tests/e2e/conftest.py`):

| Placeholder | Bound to | Source |
|---|---|---|
| `${WORKSPACE_GID}` | the env var `ASANA_PYTEST_WORKSPACE` | per-account / per-environment |
| `${WORKSPACE_NAME}` | `"E2E Workspace"` | fixed literal |
| `${USER_EMAIL}` | `"e2e-user@example.invalid"` | fixed literal |
| `${USER_NAME}` | `"E2E User"` | fixed literal |
| `${TEAM_NAME}` | `"E2E Team"` | fixed literal |
| `${PAGINATION_PROJECT_GID}` | gid discovered at fixture setup (`pagination_project_gid`) | per-account, run-time discovery |
| `${PAGINATION_SMALL_PROJECT_GID}` | gid discovered at fixture setup (`pagination_small_project_gid`) | per-account, run-time discovery |

A different developer can replay the same cassettes by setting their own
`ASANA_PYTEST_WORKSPACE`; the cassette's request URL and response body
adapt to their value at load time.

## PII masking

Applied at record time by `_before_record_response` in `conftest.py`. Hits
are dispatched by `resource_type`:

- `Authorization` request header → `Bearer ***REDACTED***`
- `user.email` / `user.name` / `user.photo` → bound values / `null`
- `workspace.name` / `workspace.email_domains` → bound value / `["example.invalid"]`
- `team.name` → bound value

Test assertions should compare on structure or against the bound values,
not on real account data.

## Attachment-specific notes

Asana's attachment endpoint requires the RFC 5987
`filename*=UTF-8''<percent-encoded>` parameter of `Content-Disposition` to
correctly decode non-ASCII filenames. The upstream `python-asana` SDK
(via urllib3) does not emit it, so non-ASCII filenames get garbled by
default. The CLI ships an opt-in workaround: pass
`--multibyte-filenames` to the `asana-api` invocation, which installs a
session-scoped patch on `urllib3.fields.RequestField.make_multipart`
that adds `filename*=` when the filename has non-ASCII bytes.

The `japanese_filename_*` parametrized cases in `test_attachments.py`
exercise this path; the ASCII / Japanese-content cases use the unmodified
SDK path to confirm parity behavior.

## Known limitations

- **List-endpoint cassettes are account-shape-dependent.** `get-workspaces`
  records whichever workspaces the recording account has access to;
  re-recording on a different account produces a different list (count
  and other-workspace gids). Assertions tolerate this by checking the
  test workspace's presence rather than the full list, but cassette
  diffs across re-recordings can be noisy.
- **Other-resource gids are stored literally.** Only `${WORKSPACE_GID}`,
  `${PAGINATION_PROJECT_GID}` and `${PAGINATION_SMALL_PROJECT_GID}` are
  templated. Other workspace / project / task gids that incidentally
  appear in responses (e.g. the second workspace returned by
  `get-workspaces`) stay as the recording account's literals. They are
  not PII but make cross-account re-recording diffs noisier.
- **Workspace lifecycle is read-only via the API.** Asana does not expose
  create / delete on workspaces, so the test environment relies on at
  least one workspace already existing (the user provides its gid via
  `ASANA_PYTEST_WORKSPACE`). Tests do not attempt to create or remove
  workspaces.

[pytest-recording]: https://github.com/kiwicom/pytest-recording
