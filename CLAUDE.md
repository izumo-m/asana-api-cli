# Claude instructions

`asana-api-cli` is a Python CLI that wraps the [`python-asana`](https://github.com/Asana/python-asana) SDK. The command tree is built **at runtime** by introspecting the installed `asana` package — there is no codegen step.

## Always apply

- **English** for committed files.
- Before committing Python changes, run `uv run basedpyright` (no path arguments — it must scan the whole project) and resolve every error, then run `uv run ruff format .`.
- Before committing user-facing changes, update `CHANGELOG.md`'s `[Unreleased]` section.
- Use a [Conventional Commits](https://www.conventionalcommits.org/) prefix (`feat:` / `fix:` / `docs:` / `chore:` / `refactor:` / `test:` etc.) in every commit subject, matching the existing `git log`.
- If you state a rule or caveat in an answer, every subsequent command/example in the same answer must follow it. (See user-memory `feedback-apply-own-rules`.)

## Read before working in these areas

- Project constitution & terminology → [`docs/principles.md`](docs/principles.md)
- Editing `src/asana_api_cli/*.py` or any CLI surface change → [`docs/architecture.md`](docs/architecture.md)
- Release work (`chore: release X.Y.Z`) → [`docs/release.md`](docs/release.md)
- Bumping the `asana` SDK version → [`docs/architecture.md`](docs/architecture.md) (CLI surface snapshot test)

## Off-limits without explicit user instruction

- Embedding concrete API counts (e.g. "46 groups, 240 commands") in `README.md` or PyPI description. Numbers rot when the SDK adds endpoints.
