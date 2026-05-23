# End-to-end tests

These tests run the CLI against the real Asana API and record HTTP traffic to
cassettes via [vcrpy](https://vcrpy.readthedocs.io/) + [pytest-recording].
The default `pytest` invocation **replays from the committed cassettes**, so
no Asana account or network access is needed to run the full suite. Opt-in
flags switch to live API access; see [Running](#running) below.

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
| `ASANA_ACCESS_TOKEN` | live mode | Personal access token. Replay auto-injects a dummy token if absent — vcrpy never hits the network. |
| `ASANA_PYTEST_WORKSPACE` | live mode | GID of the workspace under test. Treat as **test-dedicated** — CRUD tests create and delete projects / tasks in it. Cassettes store the gid as `${WORKSPACE_GID}` and substitute it back at load time; replay falls back to the literal `WORKSPACE_GID` sentinel if the env is unset. |

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

Two project-specific flags select the mode:

| Goal | Command |
|---|---|
| Replay from committed cassettes (default) | `uv run pytest` |
| Live API access, do not touch cassettes | `uv run pytest --live` |
| Live + overwrite cassettes (regenerate) | `uv run pytest --live --record` |

`--record` without `--live` is rejected as a usage error.

Live modes require `ASANA_ACCESS_TOKEN` + `ASANA_PYTEST_WORKSPACE` to be
set; tests that need a workspace gid skip automatically when it is
missing.

To re-record a subset of cassettes (e.g. after changing the CLI
surface), delete the affected files first — `--record` writes new
interactions but does not prune stale ones:

```bash
rm tests/e2e/cassettes/<dir>/<test>.yaml
uv run pytest --live --record tests/e2e/<file>::<test>
```

After re-recording, review `git diff tests/e2e/cassettes/` and commit.

### Escape hatch: pytest-recording native flags

The underlying [pytest-recording] flags still work for advanced
workflows:

- `--record-mode=once` (record only new interactions)
- `--record-mode=new_episodes` (append new interactions to existing
  cassettes)
- `--disable-recording` (vcrpy off entirely — equivalent to `--live`)

Live-mode detection in fixtures considers all three of `--live`,
`--record-mode != none`, and `--disable-recording`, so the same
workspace / token requirements apply regardless of which flag you use.

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
- `attachment.download_url` / `attachment.view_url` → query string stripped
  (Asana issues presigned `?e=<expiry>&t=<HMAC>` URLs against
  `asanausercontent.com`; the token grants read access to the asset
  until expiry and must not be committed)

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
