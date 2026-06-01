# Usage

Full reference for the `asana-api` command line. For installation, environment
variables, and shell completion, see the [README](../README.md). For how each
flag maps to the `python-asana` SDK, see
[`cli-sdk-mapping.md`](cli-sdk-mapping.md); for why CLI-only flags exist, see
[`sdk-deviations.md`](sdk-deviations.md).

Every invocation is `asana-api <group> <command> [OPTIONS]`. Run `--help` at any
level: `asana-api --help`, `asana-api tasks --help`,
`asana-api tasks get-tasks --help`.

## Authentication

Asana accepts Bearer-token authentication only (personal access token, Service
Account token, or OAuth). Provide it with `--access-token` or the
`ASANA_ACCESS_TOKEN` environment variable:

```bash
export ASANA_ACCESS_TOKEN="2/12345..."
asana-api workspaces get-workspaces

# ...or per call:
asana-api --access-token "2/12345..." workspaces get-workspaces
```

No token is needed for `--help` or argument-validation errors.

## Option scopes

`asana-api` has three kinds of options:

- **Global options** — client-wide settings (auth, network, TLS, retry,
  logging). They work at any level of the command tree, and the later one wins
  when repeated. `asana-api --debug tasks get-tasks ...` and
  `asana-api tasks get-tasks ... --debug` are equivalent.
- **Per-command options** — bound to the single SDK call (output formatting,
  error handling, per-call pagination options). They must appear **after** the
  command path, not before it.
- **Command parameters** — the SDK method's own inputs (`--project`, `--task`,
  `--body`, ...), derived per command from the SDK. See each command's `--help`.

Each option's `--help` ends with a `(<kind>: <name>)` label naming where its
value lands in the SDK (e.g. `(Configuration: host)`, `(opts: opt_fields)`,
`(asana-api: extension)`). The
full label scheme is in [`cli-sdk-mapping.md`](cli-sdk-mapping.md).

## Global options

Client-wide settings; valid at any point in the command path.

| Group | Options |
|---|---|
| Auth | `--access-token` (default `$ASANA_ACCESS_TOKEN`) |
| Endpoint / network | `--host`, `--proxy`, `--connection-pool-maxsize` |
| TLS / mTLS | `--verify-ssl` / `--no-verify-ssl`, `--ssl-ca-cert`, `--cert-file`, `--key-file`, `--assert-hostname` / `--no-assert-hostname` |
| HTTP headers | `--user-agent`, `--set-default-header NAME=VALUE` (repeatable) |
| Retry | `--retry-strategy` |
| Logging / debug | `--debug`, `--logger-format`, `--logger-file` |
| Advanced | `--temp-folder-path`, `--safe-chars-for-path-param` |
| Pagination (client-wide) | `--return-page-iterator` / `--no-return-page-iterator`, `--page-limit` — see [Pagination](#pagination) |

`--debug` turns on the SDK's HTTP debug output, with the `Authorization` header
masked. Mirroring the SDK, the wire trace (request/response headers) goes to
stdout and the connection/response log to stderr:

```bash
asana-api --debug tasks get-tasks --project <PROJECT_GID>
```

`--user-agent` overrides the `User-Agent` header on every request.
`--set-default-header NAME=VALUE` (repeatable) adds a header sent on **every**
request for the session — unlike the per-command [`--header-params`](#per-command-options),
which applies to a single call:

```bash
asana-api --user-agent "my-integration/1.0" \
          --set-default-header "X-Trace-Id=abc123" \
          --set-default-header "Accept-Language=ja" \
          tasks get-task --task <TASK_GID>
```

`--user-agent VALUE` is shorthand for `--set-default-header "User-Agent=VALUE"` —
both write the same header. If you set the `User-Agent` through both, the
dedicated `--user-agent` wins.

For a header set on both sides, the session-wide `--set-default-header` wins over a
per-call `--header-params` of the same name (the SDK merges defaults on top).
Like `--header-params`, these custom headers are **not** redacted in `--debug`
output — see [SECURITY.md](../SECURITY.md).

### Structured values

`--retry-strategy` and `--header-params` share one value format. The value's
first character selects how it is parsed:

- `{...}` — a JSON object.
- `@<path>` — read the file at `<path>` and parse it as a JSON object.
- otherwise — shorthand `key=value[,key=value...]`.

Shorthand accepts scalar values only (`int` / `float` / `bool` / `str`); bool
accepts `true` / `false` (case-insensitive), and `1` / `0` are rejected. Fields
whose type is a list (for `--retry-strategy`: `allowed_methods`,
`status_forcelist`, `remove_headers_on_redirect`) must use the JSON or `@file`
form, since commas would collide with the shorthand separator. Unspecified
fields keep the SDK defaults.

```bash
asana-api --retry-strategy 'total=5,backoff_factor=1.5' tasks get-tasks --project <PROJECT_GID>
asana-api --retry-strategy '{"total":3,"status_forcelist":[429,500]}' tasks get-tasks --project <PROJECT_GID>
```

Only `--body` reads JSON from stdin (`-`). The structured options do **not**
accept `-` (multiple stdin readers in one invocation would be order-dependent);
pipe via process substitution instead: `--retry-strategy @<(echo '{"total":3}')`.

## Per-command options

Present on every command and forwarded to the SDK call:

| Option | Effect |
|---|---|
| `--item-limit N` | Stop after N items have been collected — see [Pagination](#pagination) |
| `--full-payload` | Return the raw single-page response dict instead of auto-paginating — typically `{data, next_page}`, but `{data, sync, has_more}` for `events get-events` |
| `--header-params VALUE` | Extra HTTP headers for this call (structured value; **not** redacted in `--debug` — see [SECURITY.md](../SECURITY.md)) |
| `--request-timeout SECONDS` | Per-request timeout; propagated to every page request |

## Output formatting

These options control how a successful response is printed.

| Option | Effect |
|---|---|
| `--output {json\|table\|csv\|text\|none}` | Render format. Default `json` (canonical, lossless). `none` suppresses the success payload — useful for side-effect-only calls (delete/update) where only the exit code matters |
| `--query EXPR` | Filter the response through `jq`; each result is rendered per `--output`. Mirrors `aws --query` |
| `--csv-bom` | Prepend a UTF-8 BOM to CSV output (for Excel on Windows). Off by default so Unix pipelines stay clean |

Pair a non-JSON format with `--query '.data'` to unwrap the `{"data": [...]}`
envelope into one row per item:

```bash
asana-api tasks get-tasks --project <PROJECT_GID> --query '.data' --output table
asana-api tasks get-tasks --project <PROJECT_GID> --query '.data' --output csv
asana-api tasks get-tasks --project <PROJECT_GID> --output csv --csv-bom > tasks.csv

# Side-effect-only call: only the exit code matters
asana-api tasks delete-task --task <TASK_GID> --output none
```

`--query` runs and validates even under `--output none`, so a broken jq
expression still surfaces (exit `2`) regardless of the chosen format.

## Pagination

Paginatable commands expose every pagination input of the SDK as a flag. By
default the CLI walks every page and prints a flat JSON list of items.

| Flag | SDK input | Effect |
|---|---|---|
| (none) | — | Default: walk every page, output a flat list of items |
| `--limit N` | `opts["limit"]` | Per-page size sent to the server (Asana requires 1-100) |
| `--offset TOKEN` | `opts["offset"]` | Pagination cursor (the `next_page.offset` from a previous response) |
| `--page-limit N` | `Configuration.page_limit` | Per-page size via Configuration (default 100). Ignored under `--no-return-page-iterator` / `--full-payload` |
| `--item-limit N` | kwarg `item_limit` | Stop after N items have been collected. Ignored under `--no-return-page-iterator` / `--full-payload` |
| `--return-page-iterator` / `--no-return-page-iterator` | `Configuration.return_page_iterator` | Toggle the SDK page iterator (default on). `--no-return-page-iterator` runs one HTTP request and outputs the raw response dict (typically `{data, next_page}`; `{data, sync, has_more}` for `events get-events`) |
| `--full-payload` | kwarg `full_payload=True` | Same effect as `--no-return-page-iterator`, in per-call kwarg form |

```bash
# Walk every page (default)
asana-api tasks get-tasks --project <PROJECT_GID>

# Cap to the first 250 items
asana-api tasks get-tasks --project <PROJECT_GID> --item-limit 250

# One HTTP call: a single page + the next_page cursor
asana-api tasks get-tasks --project <PROJECT_GID> --limit 100 --full-payload

# Resume from a cursor
asana-api tasks get-tasks --project <PROJECT_GID> --offset <TOKEN>
```

### Deprecated aliases

Retained as deprecation aliases; each emits a stderr warning and forwards to its
v3 flag. Scheduled for removal in a future release.

| Deprecated | Replacement |
|---|---|
| `--all-items` | (no-op; walking every page is now the default) |
| `--page-size N` | `--limit N` |
| `--max-items N` | `--item-limit N` |

If both an alias and its replacement are given (e.g. `--page-size 50 --limit
100`), the replacement wins and the alias is ignored.

## Workspace resolution

A subset of endpoints take a positional `workspace_gid` (e.g.
`projects get-projects-for-workspace`, `tags create-tag-for-workspace`). For
those commands the workspace is resolved in order:

1. `--workspace <WORKSPACE_GID>` on the command
2. `ASANA_DEFAULT_WORKSPACE` environment variable

For commands where workspace is an optional filter (e.g. `tasks get-tasks`,
`goals get-goals`), the env-var fallback is **not** used — pass `--workspace`
explicitly if needed. This avoids ambiguity with alternative scope parameters
like `--project` that the Asana API accepts in place of a workspace.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Unhandled error — the catch-all. Usually an SDK call exception under the default `--exception-output=none` (echoed to stderr, no traceback); but also Python's default for any other uncaught failure the CLI does not classify, e.g. an incompatible `asana` SDK that fails to import (which prints a full traceback) |
| `2` | User-input invalid (bad option value, missing required workspace, jq syntax / runtime error, malformed `--body` / structured-arg value) |
| `3` | SDK call exception rendered as an envelope on stdout (requires `--exception-output {json\|text\|csv\|table}`) |

- Only `2` and `3` are narrowly defined: `2` for invalid user input (Click's
  convention, reused for jq and `--body` parse failures), `3` for an SDK error
  you explicitly captured as an envelope. When a command has both bad input and
  a failing API call, the input error (`2`) wins — it is detected first.
- `1` is the catch-all for everything else that failed; do not read a specific
  cause into it. To ask "did it fail?", test for non-zero; to branch on the
  kind, match `2` / `3` and treat any other non-zero (including `1`) as an
  unclassified error.

## Error output

On an SDK call failure the CLI always echoes the exception to **stderr** in the
format Python uses for an uncaught exception (the qualified class name and the
message, with no traceback frames). For an `ApiException` that output already
includes the status, reason, headers, and body, so the response payload (e.g.
the 412 sync-token body when polling events) is readable without requesting an
envelope.

`--exception-output {none|json|text|csv|table}` (default `none`) controls the
structured envelope:

- `none` — no envelope; exit `1`. Stderr is the only error channel.
- `json` / `text` / `csv` / `table` — additionally render an envelope on
  **stdout** and exit `3`, using the same renderer as `--output`.

The envelope shape:

- `ApiException` → 5 fields `{exception, status, reason, body, headers}`, where
  `body` is the UTF-8 decoded response *string* (or `null`).
- Any other exception from the SDK call path (e.g.
  `urllib3.exceptions.MaxRetryError`) → 2 fields `{exception, reason}`.

`exception` is the qualified `module.qualname` (e.g. `asana.rest.ApiException`),
so a script can map an exit `3` back to the exact SDK exception class to `import`
and `except` it. Click's own errors (`ClickException`, `Abort`, `Exit`) are not
wrapped.

`--exception-query EXPR` applies a `jq` filter to the envelope and renders each
result per `--exception-output`. Pairing it with the default `none` emits a
stderr warning (there is no envelope to filter) but does not block the call.

```bash
# Default none: stderr gets the exception, exit 1
asana-api tasks get-task --task 0 || echo "exit=$?"

# Opt into a stdout envelope: exit 3, structured error on stdout
out=$(asana-api tasks get-task --task 0 --exception-output json)
case $? in
  0) echo "$out" | jq '.' ;;          # success: $out is the payload
  3) echo "$out" | jq '.status' ;;    # API error: $out is the envelope
  *) echo "input error" >&2 ;;        # exit 2: bad input
esac
```

## File uploads

In `python-asana` 5.2.4 — the latest version checked, and most likely later
ones too — uploading a file whose name contains non-ASCII characters (accented
letters, Japanese, emoji, …) stores a garbled (mojibake) filename on Asana.
This is a long-standing bug in the SDK — see the
[Asana forum thread](https://forum.asana.com/t/attachment-names-uploaded-with-asana-api-are-garbled-on-asanaweb/286200)
(open since 2022, still unresolved).

`--multibyte-filenames` works around it: pass it to a file-upload command (e.g.
`attachments create-attachment-for-object`) and `asana-api-cli` applies its own
patch so the original filename round-trips intact. It is off by default to match
stock SDK behavior; turn it on whenever an attachment's name has any character
outside ASCII (plain-ASCII names, including symbols, are unaffected).
