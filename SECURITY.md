# Security Policy

Thank you for taking the time to report a security issue in
`asana-api-cli`. This document explains how to reach the maintainer
privately.

## Reporting a vulnerability

Please report suspected vulnerabilities **privately** through one of the
following channels:

- GitHub [private vulnerability reporting](https://github.com/izumo-m/asana-api-cli/security/advisories/new) (preferred)
- Email <asana@masanao.site>

Please **do not** file public GitHub issues, pull requests, or discussion
posts for security problems until a fix has been released.

When reporting, please include as much of the following as you can:

- A description of the issue and its impact
- The affected version or commit hash
- Steps to reproduce, a proof-of-concept, or a minimal test case
- Any suggested mitigation, if you have one

## A note on response

`asana-api-cli` is a small personal project. I cannot guarantee a
response time, nor that every report will result in a fix or a new
release, but I will read every report and do my best to handle it
responsibly.

## Handling Asana API tokens

`asana-api-cli` reads your Asana personal access token from the
`--access-token` option, falling back to the `ASANA_ACCESS_TOKEN`
environment variable. Treat this token as a secret:

- Do not commit it to source control. Keep it out of `.env` files that are
  tracked by git and dotfiles you sync publicly.
- Do not paste it into issues, bug reports, logs, or screenshots. A
  personal access token grants the same access as your Asana account.
- Rotate it immediately at <https://app.asana.com/0/my-apps> if you
  suspect it has been exposed.
- When sharing command output, scrub any GIDs or data you do not want to
  disclose; `asana-api-cli` prints raw API responses by default.

## Generated scripts can carry your token

`asana-api --generate-python` prints a standalone Python script instead of
running the call. When you pass a literal `--access-token`, its value is written
into the emitted script verbatim (`configuration.access_token = "..."`). When
`--access-token` is omitted, the script instead reads
`os.environ["ASANA_ACCESS_TOKEN"]` and embeds no secret.

- Generate with a dummy token (or omit `--access-token`) when you intend to
  share, commit, or paste the script.
- Treat any script generated with a real `--access-token` — or with a
  credential-bearing `--proxy` / `--set-default-header` / `--header-params`,
  which are likewise transcribed verbatim — as a secret-bearing file: keep it
  out of source control and out of issues, logs, and screenshots.
- Rotate the token at <https://app.asana.com/0/my-apps> if such a script may
  have been exposed.

## Secrets on the command line

A value passed as a command-line argument is visible to other users through
the process list (for example `ps`) while the command runs, and is written to
your shell history afterwards. This applies to ordinary use, not to any single
feature. The values to watch are:

- the `--access-token` value — prefer the `ASANA_ACCESS_TOKEN` environment
  variable, which keeps the token off the command line entirely;
- a `--proxy` URL that embeds credentials (`http://user:pass@host`);
- a credential carried by `--header-params VALUE` or the session-wide
  `--set-default-header NAME=VALUE` (for example an `Authorization` header).
  `--header-params` accepts a `@file` form (`--header-params @headers.json`)
  that reads the value from a file rather than the command line.

Treat any secret you have typed on the command line as exposed: avoid passing
real secrets inline where an environment variable or `@file` is available,
clear or scrub your shell history, and rotate a token you suspect has leaked.

## Debug output

`asana-api --debug` turns on the SDK's HTTP debug output. Mirroring the
SDK, the `http.client` wire trace (request and response headers) goes to
stdout and the SDK / urllib3 debug log (connection, status line, response
body) to stderr. The CLI masks only the request `Authorization` header in
the stdout wire trace (it carries the personal access token); everything
else — on either stream — is shown verbatim, so scrub both streams before
sharing.

This masking lives in the CLI, not the SDK. Direct use of the SDK's
HTTP debug logging from Python leaves the `Authorization` header in
clear text.

### Custom request headers are not masked

`--header-params VALUE` (per call) and the session-wide `--user-agent` /
`--set-default-header NAME=VALUE` (repeatable) let you inject arbitrary HTTP
request headers. The `--debug` redactor masks **only** the `Authorization`
header; any value passed via these flags is logged verbatim. If a custom
header carries a secret — an API key, signing token, or other credential —
treat the `--debug` log as containing that value in clear text and scrub it
before sharing.
