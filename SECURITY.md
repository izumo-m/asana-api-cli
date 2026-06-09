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

## Generated scripts mask your credentials

`asana-api --generate-python` prints a standalone Python script instead of
running the call. Credential-bearing values are masked in the emitted text —
in the configuration lines and in the `# Equivalent to:` header comment alike:

- a non-empty `--access-token` is embedded in masked form (`...` plus the
  last 6 characters, the same presentation as the `--debug` trace); the
  script then fails with 401 instead of silently using a different
  credential — edit in a token, or omit `--access-token` when generating so
  the script reads `os.environ["ASANA_ACCESS_TOKEN"]` instead;
- an `Authorization` / `Proxy-Authorization` header given via
  `--set-default-header` or `--header-params` is masked too — the scheme
  prefix stays visible; a `Bearer` value keeps its last 6 characters, while
  a `Basic` credential is fully masked (`Basic <REDACTED>`): it is base64 of
  `user:password`, where even a tail reveal would expose password
  characters;
- the password in a `--proxy` URL (`http://user:pass@host`) is replaced
  with `***`.

A value too short to be a real Asana token (fewer than 16 characters) is kept
verbatim, so generating with a dummy token still produces a script with the
dummy in place.

Everything else is transcribed verbatim. If a secret rides in a `--body`
payload or in a custom header under any name other than `Authorization` /
`Proxy-Authorization` (for example an `X-Api-Key`), the script carries it in
clear text: keep such a script out of source control and out of issues, logs,
and screenshots, and rotate any credential it may have exposed (Asana tokens
at <https://app.asana.com/0/my-apps>).

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
body) to stderr. The CLI masks only the request `Authorization` header
(it carries the personal access token) and the `Proxy-Authorization`
header in the stdout wire trace; everything else — on either stream — is
shown verbatim, so scrub both streams before sharing.

This masking lives in the CLI, not the SDK. Direct use of the SDK's
HTTP debug logging from Python leaves the `Authorization` header in
clear text.

### Custom request headers other than (Proxy-)Authorization are not masked

`--header-params VALUE` (per call) and the session-wide `--user-agent` /
`--set-default-header NAME=VALUE` (repeatable) let you inject arbitrary HTTP
request headers. The `--debug` redactor masks the `Authorization` and
`Proxy-Authorization` headers whatever their source or scheme; a header under
any **other** name is logged verbatim. If such a header carries a secret — an
API key, signing token, or other credential — treat the `--debug` log as
containing that value in clear text and scrub it before sharing.
