"""End-to-end pagination tests.

Exercises every pagination flag exposed by ``tasks get-tasks`` against the
projects provisioned by ``tools/e2e_init.py``:

- ``pagination-test`` (1500 tasks named ``ptest-0001`` .. ``ptest-1500``)
  drives the multi-page tests.
- ``pagination-test-small`` (50 tasks named ``psmall-0001`` .. ``psmall-0050``)
  drives ``test_full_payload_under_threshold`` (success path below
  Asana's per-response cap of ~1000 items).

Live record::

    ASANA_PYTEST_ENABLE_E2E=1 ASANA_PYTEST_WORKSPACE=<gid> \\
        uv run pytest --record-mode=all tests/e2e/test_pagination.py

Replay::

    ASANA_PYTEST_ENABLE_E2E=1 ASANA_PYTEST_WORKSPACE=<gid> \\
        uv run pytest tests/e2e/test_pagination.py
"""

from __future__ import annotations

import json

import pytest
from _cli_runner import make_runner

from asana_api_cli.cli import main

# Must match ``PROJECT_SPECS[0].task_count`` in ``tools/e2e_init.py``.
TOTAL_TASKS = 1500


def _run(*args: str) -> "tuple[int, str, str]":
    """Invoke the CLI and return (exit_code, stdout, stderr)."""
    result = make_runner().invoke(main, list(args))
    return result.exit_code, result.stdout, result.stderr


@pytest.mark.vcr
def test_default_walks_all_pages(pagination_project_gid: str) -> None:
    """No pagination flag -> the SDK iterator walks every page and the CLI
    flattens the result into one JSON list of items."""
    code, out, _ = _run("tasks", "get-tasks", "--project", pagination_project_gid)
    assert code == 0, out
    data = json.loads(out)
    assert isinstance(data, list)
    assert len(data) == TOTAL_TASKS


@pytest.mark.vcr
def test_item_limit_caps_results(pagination_project_gid: str) -> None:
    """`--item-limit N` stops the iterator after N items have been collected."""
    code, out, _ = _run(
        "tasks", "get-tasks", "--project", pagination_project_gid, "--item-limit", "250"
    )
    assert code == 0, out
    data = json.loads(out)
    assert isinstance(data, list)
    assert len(data) == 250


@pytest.mark.vcr
def test_limit_full_payload(pagination_project_gid: str) -> None:
    """`--limit 50 --full-payload` -> one HTTP call, dict with 50-item data."""
    code, out, _ = _run(
        "tasks",
        "get-tasks",
        "--project",
        pagination_project_gid,
        "--limit",
        "50",
        "--full-payload",
    )
    assert code == 0, out
    payload = json.loads(out)
    assert isinstance(payload, dict)
    assert "data" in payload and "next_page" in payload
    assert len(payload["data"]) == 50


@pytest.mark.vcr
def test_no_return_page_iterator_with_limit(pagination_project_gid: str) -> None:
    """`--no-return-page-iterator --limit N` -> one HTTP call, dict with N items.

    Mirrors ``test_limit_full_payload`` using the Configuration-property flag
    instead of the per-call kwarg form, confirming both surfaces are wired.
    """
    code, out, _ = _run(
        "tasks",
        "get-tasks",
        "--project",
        pagination_project_gid,
        "--no-return-page-iterator",
        "--limit",
        "50",
    )
    assert code == 0, out
    payload = json.loads(out)
    assert isinstance(payload, dict)
    assert "data" in payload and "next_page" in payload
    assert len(payload["data"]) == 50


@pytest.mark.vcr
def test_full_payload_without_limit_errors(pagination_project_gid: str) -> None:
    """`--full-payload` without `--limit` sends an unbounded single request,
    which Asana refuses with HTTP 400 ("result too large") once the
    project exceeds the per-response cap (~1000 items). Documents the
    v3 surface: `Configuration.page_limit` is iterator-only, so the
    single-call mode needs an explicit `--limit` for large datasets.
    Pairs with ``test_full_payload_under_threshold`` which covers the
    success case.
    """
    # --output-errors json opts into the envelope path so we can inspect
    # the API body programmatically. The default 'raw' would let the
    # exception propagate uncaught (exit 1) and bury the body in a
    # Python traceback.
    code, out, _ = _run(
        "tasks",
        "get-tasks",
        "--project",
        pagination_project_gid,
        "--full-payload",
        "--output-errors",
        "json",
    )
    assert code == 3
    envelope = json.loads(out)
    assert envelope["status"] == 400
    assert "too large" in envelope["body"].lower()


@pytest.mark.vcr
def test_full_payload_under_threshold(pagination_small_project_gid: str) -> None:
    """`--full-payload` without `--limit` on a project below Asana's
    per-response cap returns all items in one HTTP call. Pairs with
    ``test_full_payload_without_limit_errors`` which covers the failure
    case above the cap.
    """
    code, out, _ = _run(
        "tasks",
        "get-tasks",
        "--project",
        pagination_small_project_gid,
        "--full-payload",
    )
    assert code == 0, out
    payload = json.loads(out)
    assert isinstance(payload, dict)
    assert "data" in payload
    assert len(payload["data"]) == 50
    # ``next_page`` is omitted (or null) when the entire result set fits in
    # the single response; both forms mean "no more pages".
    assert payload.get("next_page") in (None, {})


@pytest.mark.vcr
def test_all_items_deprecated_noop(pagination_project_gid: str) -> None:
    """`--all-items` is a no-op; capped with --item-limit to keep cassette small."""
    code, out, err = _run(
        "tasks",
        "get-tasks",
        "--project",
        pagination_project_gid,
        "--all-items",
        "--item-limit",
        "50",
    )
    assert code == 0, out
    assert "--all-items" in err and "deprecated" in err
    data = json.loads(out)
    assert isinstance(data, list)
    assert len(data) == 50


@pytest.mark.vcr
def test_page_size_deprecated_alias(pagination_project_gid: str) -> None:
    """`--page-size N` forwards to `--limit N`."""
    code, out, err = _run(
        "tasks",
        "get-tasks",
        "--project",
        pagination_project_gid,
        "--page-size",
        "50",
        "--item-limit",
        "50",
    )
    assert code == 0, out
    assert "--page-size" in err and "deprecated" in err
    data = json.loads(out)
    assert isinstance(data, list)
    assert len(data) == 50


@pytest.mark.vcr
def test_max_items_deprecated_alias(pagination_project_gid: str) -> None:
    """`--max-items N` forwards to `--item-limit N`."""
    code, out, err = _run(
        "tasks",
        "get-tasks",
        "--project",
        pagination_project_gid,
        "--max-items",
        "100",
    )
    assert code == 0, out
    assert "--max-items" in err and "deprecated" in err
    data = json.loads(out)
    assert isinstance(data, list)
    assert len(data) == 100
