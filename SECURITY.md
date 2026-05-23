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

## Debug output

`asana-api --debug` enables HTTP request/response logging to stderr to help
diagnose issues. The CLI installs an `HttpClientAuthRedactor` that masks
the `Authorization` header before printing, so debug logs are safe to copy
into bug reports.

Note that this masking is provided by the CLI, not by the SDK. If you
enable the SDK's HTTP debug logging directly from Python (without going
through `asana-api`), the `Authorization` header — which contains your
personal access token — appears in the output in clear text.
