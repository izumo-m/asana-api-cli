# Security Policy

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

## Reporting a vulnerability

If you find a security issue in `asana-api-cli` itself, please report it
privately via GitHub's
[private vulnerability reporting](https://github.com/izumo-m/asana-api-cli/security/advisories/new)
or by emailing <asana@masanao.site>. Please avoid filing public issues for
security problems.
