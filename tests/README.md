# Tests

Run the full suite:

```bash
uv run pytest
```

Run a single test:

```bash
uv run pytest tests/test_formatter.py::test_name
```

## Multiple Python versions

`uv run pytest` uses the project's `.venv` (a single Python). To run the suite
on 3.10, 3.12, and 3.14 — or other versions you name — use
`tools/test_python_versions.sh`; see
[`tools/README.md` §test_python_versions.sh](../tools/README.md#test_python_versionssh).

```bash
bash tools/test_python_versions.sh
```

## Lower-bound versions

The default `uv run pytest` uses the dependency versions pinned in
`uv.lock`. To instead verify that the suite passes at the *lowest* versions
the project declares — the `>=` floors in `pyproject.toml` (`dependencies` and
the `dev` group) — run:

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

A floor that works on one Python can fail on another, so check the floors on
several Python versions at once with:

```bash
bash tools/test_python_versions.sh --lowest 3.10 3.11 3.12
```

Run this whenever a floor changes (and as part of bumping the `asana`
SDK) to confirm the declared minimum still works.

The floors are deliberately old: `pip install asana-api-cli` must work on an
existing system from the python-asana 5.0.2 era (December 2023) without
upgrading the packages it already has — e.g. `jq` 1.6.0, the release current
then. Do not raise a floor only because that release has no wheel for a newer
Python: pip there simply picks a newer release. This is why `--lowest` fails on
Python 3.13 and later, where `jq` 1.6.0 has no wheel and does not build.

## End-to-end tests

End-to-end tests under [`tests/e2e/`](e2e/) run the CLI against the real
Asana API and are skipped from network access by default — the default
`pytest` invocation replays from committed VCR cassettes, so no Asana
account or network is needed.

Live and record modes require `ASANA_ACCESS_TOKEN` +
`ASANA_PYTEST_WORKSPACE`. See [`tests/e2e/README.md`](e2e/README.md) for
the live / replay workflow, environment variables, and the one-time
workspace provisioning step.
