# Usage

Full reference for the `asana-api` command line. For installation, environment
variables, and shell completion, see the [README](../README.md). For how each
flag maps to the `python-asana` SDK, see
[`cli-sdk-mapping.md`](cli-sdk-mapping.md); for why CLI-only flags exist, see
[`sdk-deviations.md`](sdk-deviations.md).

Every invocation is `asana-api <group> <command> [OPTIONS]`. See
[Options](#options) for the full option reference.

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

## Options

`--help` / `--version` aside (see below), every option falls into one of three
scopes:

- **Global** — client-wide settings; valid at any point in the command path;
  the later occurrence wins when repeated.
- **Per-command** — bound to a single SDK call; must appear **after** the
  command path, not before it.
- **Command parameters** — the SDK method's own inputs (`--project`, `--task`,
  `--body`, …), derived per command from the SDK and effectively unbounded.
  They are **not** listed here; see each command's `--help`.

Every option's `--help` line ends with a `(<kind>: <name>)` label naming where
its value lands in the SDK (`(Configuration: host)`, `(opts: opt_fields)`,
`(asana-api: extension)`, …); the full scheme is in
[`cli-sdk-mapping.md`](cli-sdk-mapping.md).

### Help and version

| Option | Effect |
|---|---|
| `--help` | Print the current command's help — root, group, or leaf. No token needed |
| `--version` | Print `asana-api, version X (python-asana Y, click Z)` and exit. No token needed |

### Global options

Client-wide; valid at any point in the command path.

| Option | Effect |
|---|---|
| `--access-token TOKEN` | Bearer token. Default `$ASANA_ACCESS_TOKEN` — see [Authentication](#authentication) |
| `--host URL` | API base URL (default `https://app.asana.com/api/1.0`) |
| `--proxy URL` | HTTP/HTTPS proxy |
| `--connection-pool-maxsize N` | Max urllib3 connections cached per host (default cpu×5) |
| `--verify-ssl` / `--no-verify-ssl` | Verify TLS certificates (default on; `--no-verify-ssl` is insecure) |
| `--ssl-ca-cert FILE` | PEM bundle of trusted CA certs |
| `--cert-file FILE` / `--key-file FILE` | Client certificate / private key for mTLS |
| `--assert-hostname` / `--no-assert-hostname` | Verify the cert hostname; unspecified → urllib3 default |
| `--user-agent VALUE` | Override the `User-Agent` header on every request — see [Debugging and headers](#debugging-and-headers) |
| `--set-default-header NAME=VALUE` | Session-wide header, repeatable; **not** redacted — see [Debugging and headers](#debugging-and-headers) |
| `--retry-strategy VALUE` | Override urllib3 `Retry` fields (structured value) — see [Structured values](#structured-values) |
| `--debug` | SDK HTTP debug to stdout/stderr, `Authorization` masked — see [Debugging and headers](#debugging-and-headers) |
| `--logger-format FMT` / `--logger-file PATH` | SDK logging format string / output file |
| `--temp-folder-path DIR` | Directory for temporary downloads |
| `--safe-chars-for-path-param CHARS` | Characters left unescaped in path parameters |
| `--return-page-iterator` / `--no-return-page-iterator` | Toggle the SDK page iterator (default on) — see [Pagination](#pagination) |
| `--page-limit N` | Per-page size via `Configuration` (default 100) — see [Pagination](#pagination) |

### Per-command options

Present on every command and bound to the single SDK call; place them **after**
the command path.

| Option | Effect |
|---|---|
| `--item-limit N` | Stop after N items have been collected — see [Pagination](#pagination) |
| `--full-payload` | Return the raw single-page response dict instead of auto-paginating — see [Pagination](#pagination) |
| `--header-params VALUE` | Extra HTTP headers for this call (structured value; **not** redacted) — see [Structured values](#structured-values) |
| `--request-timeout SECONDS` | Per-request timeout; propagated to every page request |
| `--output {json\|table\|csv\|text\|none}` | Success render format (default `json`) — see [Output formats](#output-formats) |
| `--query EXPR` | `jq` filter over the success payload — see [Output formats](#output-formats) |
| `--csv-bom` | Prepend a UTF-8 BOM to CSV output (Excel on Windows) — see [Output formats](#output-formats) |
| `--exception-output {none\|json\|text\|csv\|table}` | Error-envelope format (default `none`) — see [Error handling](#error-handling) |
| `--exception-query EXPR` | `jq` filter over the error envelope — see [Error handling](#error-handling) |

Paginatable commands additionally expose `--limit` (per-page size) and
`--offset` (cursor) — see [Pagination](#pagination).

### CLI-only flags

Extensions with no SDK counterpart (`(asana-api: extension)`).

| Flag | Effect |
|---|---|
| `--generate-python` | Print equivalent python-asana code instead of running the call; valid at any point in the command path. No token, no network — see [Generating Python code](#generating-python-code) |
| `--multibyte-filenames` | Upload commands only: preserve non-ASCII attachment names — see [File uploads](#file-uploads) |
| `--all-items` *(deprecated)* | Paginatable commands only; no-op (walking every page is the default) — see [Deprecated aliases](#deprecated-aliases) |
| `--page-size N` *(deprecated)* | Paginatable commands only; use `--limit N` — see [Deprecated aliases](#deprecated-aliases) |
| `--max-items N` *(deprecated)* | Paginatable commands only; use `--item-limit N` — see [Deprecated aliases](#deprecated-aliases) |

## Output formats

These options control how a successful response is printed.

| Option | Effect |
|---|---|
| `--output {json\|table\|csv\|text\|none}` | Render format. Default `json` (canonical, lossless). `none` suppresses the success payload — useful for side-effect-only calls (delete/update) where only the exit code matters |
| `--query EXPR` | Filter the response through `jq`; each result is rendered per `--output`. Mirrors `aws --query` |
| `--csv-bom` | Prepend a UTF-8 BOM to CSV output (for Excel on Windows). Off by default so Unix pipelines stay clean |

Non-JSON formats render a list of dicts as one row per item. The default
auto-paginating output is already a flat list, so it is directly rowable; under
`--full-payload` (a single raw response dict) pair the format with
`--query '.data'` to unwrap the `{"data": [...]}` envelope first:

```bash
asana-api tasks get-tasks --project <PROJECT_GID> --output table
asana-api tasks get-tasks --project <PROJECT_GID> --output csv
asana-api tasks get-tasks --project <PROJECT_GID> --full-payload --query '.data' --output table
asana-api tasks get-tasks --project <PROJECT_GID> --output csv --csv-bom > tasks.csv

# Side-effect-only call: only the exit code matters
asana-api tasks delete-task --task <TASK_GID> --output none
```

`--query` runs and validates even under `--output none`, so a broken jq
expression still surfaces (exit `2`) regardless of the chosen format.

## Generating Python code

`--generate-python` prints a standalone `python-asana` script equivalent to the
command instead of running it. It is global (valid anywhere in the command
path), makes no network call, and needs no token — so it is a quick way to turn
a working CLI invocation into copy-pasteable SDK code.

```bash
asana-api --generate-python tasks get-tasks --workspace <WS> --opt-fields name
asana-api tasks get-task --task <TASK_GID> --generate-python > fetch_task.py
```

The emitted script is self-contained — it never imports `asana_api_cli`, only
`asana` and the standard library (plus `jq` when you use `--query` or
`--exception-query`, `tabulate` when you use the `table` format for `--output`
or `--exception-output`, and `urllib3` — already a `python-asana` dependency —
when you use `--multibyte-filenames`). It reproduces:

- **Configuration** — every global option you passed (`--host`, `--retry-strategy`,
  `--user-agent`, …), applied to `asana.Configuration` / `ApiClient` exactly as
  the CLI would.
- **The call** — `api_instance.<method>(...)` with the same positional args,
  `opts`, and per-call kwargs. A `--body` JSON literal is inlined as a Python
  literal; a `--body @file` / `--body -` is emitted as code that reads the file
  or stdin **when the generated script runs** (not at generation time), so the
  script stays re-runnable against a different payload. Endpoints that
  auto-paginate are wrapped in `list(...)`.
- **Output** — the `--output` format, `--query` (adds an `import jq`), and
  `--csv-bom` are written into the script, so running it prints what the command
  would have printed.
- **Errors** — `--exception-output` / `--exception-query` reproduce the error
  envelope and exit `3`; under the default `none` the script lets exceptions
  propagate.
- **`--debug`** — emits the request/response trace with the `Authorization`
  header masked, the same as the CLI.
- **`--multibyte-filenames`** — reproduces the RFC 5987 upload patch.

The access token is read from `os.environ["ASANA_ACCESS_TOKEN"]` unless you pass a
non-empty `--access-token`, whose value is transcribed into the script
**verbatim** — pass a dummy when generating, and treat the script like any other
file that may carry a secret (see [SECURITY.md](../SECURITY.md)). Input validation still runs
during generation: a malformed `--body` literal or a missing required
`--workspace` exits `2`, just as when executing.

`asana-api --generate-python --version` emits a script that prints the version
string (rather than printing it directly).

## Error handling

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

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Unhandled error — the catch-all. Usually an SDK call exception under the default `--exception-output=none` (echoed to stderr, no traceback); but also Python's default for any other uncaught failure the CLI does not classify, e.g. an incompatible `asana` SDK that fails to import (which prints a full traceback) |
| `2` | User-input invalid (missing access token, bad option value, missing required workspace, jq syntax / runtime error, malformed `--body` / structured-arg value) |
| `3` | SDK call exception rendered as an envelope on stdout (requires `--exception-output {json\|text\|csv\|table}`) |

- Only `2` and `3` are narrowly defined: `2` for invalid user input (Click's
  convention, reused for jq and `--body` parse failures), `3` for an SDK error
  you explicitly captured as an envelope. When a command has both bad input and
  a failing API call, the input error (`2`) wins — it is detected first.
- `1` is the catch-all for everything else that failed; do not read a specific
  cause into it. To ask "did it fail?", test for non-zero; to branch on the
  kind, match `2` / `3` and treat any other non-zero (including `1`) as an
  unclassified error.

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
v3 flag. Like the pagination flags they replace, they exist only on paginatable
commands. Scheduled for removal in a future release.

| Deprecated | Replacement |
|---|---|
| `--all-items` | (no-op; walking every page is now the default) |
| `--page-size N` | `--limit N` |
| `--max-items N` | `--item-limit N` |

If both an alias and its replacement are given (e.g. `--page-size 50 --limit
100`), the replacement wins and the alias is ignored.

## Structured values

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

## Debugging and headers

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
