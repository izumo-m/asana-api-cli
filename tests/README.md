# Tests

Run the full suite:

```bash
uv run pytest
```

Run a single test:

```bash
uv run pytest tests/test_formatter.py::test_name
```

## Lower-bound versions

The default `uv run pytest` resolves every dependency to its newest
compatible version. To instead verify that the suite passes at the
*lowest* versions the project declares — the `>=` floors in
`pyproject.toml` (`dependencies` and the `dev` group) — run:

```bash
UV_PROJECT_ENVIRONMENT=$(mktemp -d) uv run --resolution lowest-direct --isolated pytest
```

- `--resolution lowest-direct` pins the direct dependencies to their
  declared floors while letting transitive dependencies resolve to their
  newest compatible versions.
- `--isolated` resolves from `pyproject.toml` alone, so `uv.lock` is not
  rewritten.
- `UV_PROJECT_ENVIRONMENT=$(mktemp -d)` builds the floor environment in a
  throwaway directory, leaving the project's `.venv` untouched.

Run this whenever a floor changes (and as part of bumping the `asana`
SDK) to confirm the declared minimum still works.

## End-to-end tests

End-to-end tests under [`tests/e2e/`](e2e/) run the CLI against the real
Asana API and are skipped from network access by default — the default
`pytest` invocation replays from committed VCR cassettes, so no Asana
account or network is needed.

Live and record modes require `ASANA_ACCESS_TOKEN` +
`ASANA_PYTEST_WORKSPACE`. See [`tests/e2e/README.md`](e2e/README.md) for
the live / replay workflow, environment variables, and the one-time
workspace provisioning step.
