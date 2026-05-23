# Tests

Run the full suite:

```bash
uv run pytest
```

Run a single test:

```bash
uv run pytest tests/test_formatter.py::test_name
```

## End-to-end tests

End-to-end tests under [`tests/e2e/`](e2e/) run the CLI against the real
Asana API and are skipped from network access by default — the default
`pytest` invocation replays from committed VCR cassettes, so no Asana
account or network is needed.

Live and record modes require `ASANA_ACCESS_TOKEN` +
`ASANA_PYTEST_WORKSPACE`. See [`tests/e2e/README.md`](e2e/README.md) for
the live / replay workflow, environment variables, and the one-time
workspace provisioning step.
