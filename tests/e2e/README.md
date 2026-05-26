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
| `test_events.py` | `events get-events` sync-token cycle: 412 bootstrap (`--output-errors json` → envelope on stdout, exit 3) → trigger → poll (`--full-payload`). Exercises the v3.1 `--output-errors` + `--full-payload` combination needed to surface the fresh sync token. |
| `test_webhooks.py` | `webhooks` group lifecycle: create (workspace subscribe with `project added`/`deleted` filters) → list → get → trigger events (create + delete a project) → assert events arrived at the receiver → delete → list again. **Live only, opt-in** — Asana's `X-Hook-Secret` handshake POST flows Asana → receiver, outside vcrpy's CLI → Asana hook, so cassettes cannot replay it. The fixture spawns a Cloudflare Quick Tunnel (`cloudflared tunnel --url`) and an in-process receiver. |

## Environment variables

| Name | When required | Purpose |
|---|---|---|
| `ASANA_ACCESS_TOKEN` | live mode | Personal access token. Replay auto-injects a dummy token if absent — vcrpy never hits the network. |
| `ASANA_PYTEST_WORKSPACE` | live mode | GID of the workspace under test. Treat as **test-dedicated** — CRUD tests create and delete projects / tasks in it. Cassettes store the gid as `${WORKSPACE_GID}` and substitute it back at load time; replay falls back to the literal `WORKSPACE_GID` sentinel if the env is unset. |
| `ASANA_PYTEST_WEBHOOK_TUNNEL` | `test_webhooks.py` | `<provider>:<port>`. Currently only `cloudflare-quick:<port>` is wired (e.g. `cloudflare-quick:8765`). Anything else (unset, unknown provider, missing port) skips the module. Requires the `cloudflared` binary on `$PATH`. |

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

### Webhook tests

`test_webhooks.py` is **live only and opt-in**, independent of the
`--live` / `--record` flags. Asana's `X-Hook-Secret` handshake is
delivered to the target inline during `POST /webhooks`, outside vcrpy's
CLI → Asana hook, so it cannot be replayed. The test always hits the
real API and runs an in-process receiver fronted by a Cloudflare Quick
Tunnel.

```bash
ASANA_PYTEST_WORKSPACE=<gid> \
  ASANA_PYTEST_WEBHOOK_TUNNEL=cloudflare-quick:8765 \
  uv run pytest tests/e2e/test_webhooks.py
```

Prerequisites: `cloudflared` on `$PATH`. The fixture invokes cloudflared
with `--config /dev/null` so an existing named-tunnel
`~/.cloudflared/config.yml` (if any) does not hijack the Quick Tunnel
ingress and respond `http_status:404` to every edge request.

To re-record a subset of cassettes (e.g. after changing the CLI
surface), delete the affected files first — `--record` writes new
interactions but does not prune stale ones:

```bash
rm tests/e2e/cassettes/<dir>/<test>.yaml
uv run pytest --live --record tests/e2e/<file>::<test>
```

### After re-recording: confirm nothing leaked

Re-recording hits the real Asana API and writes the response straight
into the cassette file. The masking layer covers the common cases,
but a new field or shape can slip through silently.

**After every `--record` invocation, run `git diff tests/e2e/cassettes/`
and confirm none of the following from your account survived into the
cassette:**

- real email addresses
- `asanausercontent.com` presigned-URL signatures (`?e=...&t=...`)
- real gids (any 16-digit number that exists as a real workspace,
  project, task, user, etc. in your Asana account)

Do not commit until the diff is clean.

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

### Auto-hashed gids

Identifiers that don't have a semantic name (user gid, team gid, transient
task / project / section gids, attachment asset ids) are replaced with a
**deterministic synthetic gid** derived from `sha256(real_gid)` truncated
into the `[10^15, 10^16)` decimal range. The shape matches a current Asana
gid (`[1-9][0-9]{15}`) so the cassette stays drop-in compatible — there is
no `${...}` wrapping at replay time.

Discovery sources at record time (see `_collect_gids` in `conftest.py`):

- `"gid": "<digits>"` in any JSON body — covers every resource gid the
  API returns, threshold-free.
- `/<parent>/<id>` URL path segments where `<parent>` is harvested from
  the installed asana SDK's path templates (`asana/api/*.py` constants
  like `/tasks/{task_gid}`). Catches gids that appear only in URLs.
- `asanausercontent.com/.../assets/.../<asset_id>/...` — the asset CDN
  host is not in the SDK so it gets its own pattern.

Same real gid always maps to the same synthetic, so identifiers stay
traceable across cassettes by grep. The check `len(set(synthetics)) ==
len(synthetics)` guards against hash collisions; at our scale (<2k gids
per cassette) it is essentially impossible to hit, but a real hit forces
a re-record against a fresh resource set.

## PII masking (three layers)

Three layers run at cassette record time, in this order. Each layer
covers PII shapes the next one cannot see, so adding a test rarely needs
more than picking the right layer for the response shape at hand.

### Layer 1 — Universal value- and format-based pass

Applied to every cassette automatically (see `_templated_yaml_serialize`
in `conftest.py`):

- `Authorization` request header → `Bearer ***REDACTED***` (vcrpy
  `filter_headers`).
- `${VAR}` placeholders — see [Account-neutral templating](#account-neutral-templating)
  above.
- Auto-hashed gids — see [Auto-hashed gids](#auto-hashed-gids) above.
- Asana events sync tokens (`<32-hex>:<int>`) in request URLs and
  response bodies are hashed (sha256 of the prefix) at serialize time.
  Sync tokens are not credentials but they are account-coupled opaque
  strings; hashing keeps the cassette portable while preserving
  vcrpy's request-matching invariant (same real token always hashes to
  the same synthetic, so a test can extract the token from a response
  and send it back in the next request unmodified).

### Layer 2 — `resource_type`-aware response hook

Applied by `_before_record_response` to every response body that parses
as JSON. Hits are dispatched by each object's `resource_type` field:

- `user.email` / `user.name` / `user.photo` → bound values / `null`
- `workspace.name` / `workspace.email_domains` → bound value /
  `["example.invalid"]`
- `team.name` → bound value
- `attachment.download_url` / `attachment.view_url` → query string
  stripped (Asana issues presigned `?e=<expiry>&t=<HMAC>` URLs against
  `asanausercontent.com`; the token grants read access to the asset
  until expiry and must not be committed).

Real `user.name` / `user.email` values that leak into free-text fields
(e.g. `story.text` "X さんが …") are harvested before the structured
masking runs and substituted to the bound `USER_NAME` / `USER_EMAIL`
values in the serialized response body.

**Gap**: L2 requires the response object to carry `resource_type`. Some
APIs (notably `/batch` sub-responses) only return fields the caller
explicitly asked for via `options.fields`, so when `resource_type` is
absent L2 silently does nothing. Layer 3 covers that case.

### Layer 3 — Per-test masker hook

Tests opt in via `@pytest.mark.cassette_mask.with_args(fn, ...)`. Each
`fn` is a callable `(cassette_dict) -> None` that mutates the parsed
cassette in place; the `pytest_runtest_setup` hook in `conftest.py`
reads the marker and populates the maskers list, the serializer
invokes them before Layer 1 runs (so maskers write the bound value,
e.g. `"E2E User"`, and Layer 1's templating then rewrites it to
`${USER_NAME}` — keeping the bound value as the single source of
truth), and the `pytest_runtest_teardown` hook (`trylast=True`)
drains the list AFTER every fixture teardown — including
pytest-recording's `vcr` — has finished, so the cassette save sees
the populated list.

The `.with_args` form is required: `@pytest.mark.X(callable)` is
interpreted by `MarkDecorator` as "apply mark X (no args) to this
*callable* as a test function," so without `.with_args` the masker is
silently swallowed.

Helpers live in `tests/e2e/_maskers.py`. The first one,
`mask_users_in_batch_subresponses`, walks every `/batch` interaction
and rewrites `data[i].body.data.name` for any sub-action whose
`relative_path` starts with `/users/`. Add a new helper here when a
test exposes a PII shape that the existing layers cannot reach (e.g.
an API whose response embeds names without a `resource_type` tag or
inside an API-specific nested structure).

### What to compare on in test assertions

Compare on structure or against bound values, never on real account
data. Real names / emails / gids that leak into a cassette are
verification failures regardless of which layer should have caught
them.

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
  and gid set). Assertions tolerate this by checking the test workspace's
  presence rather than the full list, but cassette diffs across
  re-recordings can be noisy.
- **Workspace lifecycle is read-only via the API.** Asana does not expose
  create / delete on workspaces, so the test environment relies on at
  least one workspace already existing (the user provides its gid via
  `ASANA_PYTEST_WORKSPACE`). Tests do not attempt to create or remove
  workspaces.
- **Events sync-token expiry is not exercised.** `events get-events`
  rotates a fresh token after ~24h; reproducing the 412-expire path
  deterministically would require a >24h-old fixture token. Verification
  is manual.

[pytest-recording]: https://github.com/kiwicom/pytest-recording
