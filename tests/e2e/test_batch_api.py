"""End-to-end tests for the Asana batch API.

Four scenarios cover the parts of ``/batch`` semantics that are unique
to batching:

* **E-1 / E-2**: ``actions`` outside the 1–10 range -> parent 400. The
  test surfaces the parent failure via ``--output-errors json`` (envelope
  on stdout, exit 3) so the response shape is assertable.
* **N-1**: 9 valid + 1 invalid GET -> parent 200 with one per-action
  4xx + ``body.errors[]``. Exercises the "parent always 200 even when
  some sub-actions fail" contract.
* **N-2**: Full CRUD lifecycle driven through batch (10 create / 10
  update / 10 get / 10 delete) inside a dedicated project, with the
  single-call project create / delete bookending the batch operations.

All assertions are made against ``--full-payload`` output (the raw
``{"data": [...]}`` dict from a single HTTP call). The SDK's default
``PageIterator`` mode would flatten ``data`` to a list of the same
per-action objects, but ``/batch`` is not actually paginated; the
``--full-payload`` shape matches the documented response and reads
better in assertions.

Live record::

    ASANA_PYTEST_WORKSPACE=<gid> \\
        uv run pytest --live --record tests/e2e/test_batch_api.py

Replay::

    uv run pytest tests/e2e/test_batch_api.py
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from _cli_runner import make_runner

from asana_api_cli.cli import main
from e2e._maskers import mask_users_in_batch_subresponses


def _run(*args: str) -> "tuple[int, str, str]":
    result = make_runner().invoke(main, list(args))
    return result.exit_code, result.stdout, result.stderr


def _batch_body(actions: list[dict[str, Any]]) -> str:
    return json.dumps({"data": {"actions": actions}})


def _me_action() -> dict[str, Any]:
    """A trivial token-only GET used as filler in the limit / partial tests.

    The L2 ``resource_type``-aware mask in ``conftest.py`` cannot reach
    these sub-responses because ``options.fields`` deliberately omits
    ``resource_type`` here — the goal of this test is to exercise the
    L3 ``cassette_mask`` hook against a realistic batch sub-response
    shape (one a caller might actually send).
    """
    return {
        "relative_path": "/users/me",
        "method": "get",
        "options": {"fields": ["gid", "name"]},
    }


@pytest.mark.vcr
def test_batch_zero_actions_returns_400() -> None:
    """Empty ``actions`` -> parent 400; envelope on stdout, exit 3."""
    code, out, _ = _run(
        "batch-api",
        "create-batch-request",
        "--body",
        _batch_body([]),
        "--output-errors",
        "json",
    )
    assert code == 3, out
    envelope = json.loads(out)
    assert envelope["exception"] == "asana.rest.ApiException"
    assert envelope["status"] == 400


@pytest.mark.vcr
def test_batch_over_limit_returns_400() -> None:
    """11 actions exceed the documented cap of 10 -> parent 400."""
    code, out, _ = _run(
        "batch-api",
        "create-batch-request",
        "--body",
        _batch_body([_me_action() for _ in range(11)]),
        "--output-errors",
        "json",
    )
    assert code == 3, out
    envelope = json.loads(out)
    assert envelope["exception"] == "asana.rest.ApiException"
    assert envelope["status"] == 400


@pytest.mark.vcr
@pytest.mark.cassette_mask.with_args(mask_users_in_batch_subresponses)
def test_batch_partial_failure() -> None:
    """9 valid + 1 invalid sub-action -> parent 200 with one per-action 4xx.

    The invalid action GETs a well-formed but non-existent task gid so
    Asana returns a 4xx with an ``errors[]`` body, while the parent
    response stays 200 ("parent always 200 if at least the request
    itself parses" contract).
    """
    # 16-digit numeric: passes the cheap regex shape check at Asana's
    # edge so the request lands inside ``/batch`` and is dispatched as
    # a sub-action. The cassette shows Asana then rejects the value
    # with ``404 task: Not a recognized ID: <gid>`` — a 4xx body
    # carrying ``errors[]`` is what the assertion below cares about,
    # not the precise sub-status, hence the soft ``>= 400`` check.
    invalid_gid = "9999999999999999"
    actions = [_me_action() for _ in range(9)] + [
        {
            "relative_path": f"/tasks/{invalid_gid}",
            "method": "get",
            "options": {"fields": ["gid", "name"]},
        },
    ]

    code, out, _ = _run(
        "batch-api",
        "create-batch-request",
        "--body",
        _batch_body(actions),
        "--full-payload",
    )
    assert code == 0, out
    payload = json.loads(out)
    assert len(payload["data"]) == 10

    for i in range(9):
        result = payload["data"][i]
        assert result["status_code"] == 200, (i, result)
        assert "data" in result["body"], (i, result)

    bad = payload["data"][9]
    assert bad["status_code"] >= 400, bad
    assert "errors" in bad["body"], bad


@pytest.mark.vcr
def test_batch_crud_lifecycle(
    workspace_gid: str,
    created_projects: list[str],
    created_tasks: list[str],
) -> None:
    """Project create -> batch (10x create / update / get / delete) -> project delete.

    The single-call project create / delete bookend the four batch
    operations. Each batch is asserted on ``--full-payload`` shape: the
    ``data`` array has 10 entries in the same order as the request, and
    every per-action ``status_code`` is 2xx.
    """
    project_name = "pytest-e2e-test_batch_crud_lifecycle"

    # CREATE PROJECT (single, not batched).
    code, out, _ = _run(
        "projects",
        "create-project-for-workspace",
        "--workspace",
        workspace_gid,
        "--body",
        json.dumps({"data": {"name": project_name}}),
    )
    assert code == 0, out
    project_gid = json.loads(out)["gid"]
    created_projects.append(project_gid)

    # BATCH CREATE 10 tasks.
    create_actions = [
        {
            "relative_path": "/tasks",
            "method": "post",
            "data": {"name": f"batch-task-{i:02d}", "projects": [project_gid]},
        }
        for i in range(10)
    ]
    code, out, _ = _run(
        "batch-api",
        "create-batch-request",
        "--body",
        _batch_body(create_actions),
        "--full-payload",
    )
    assert code == 0, out
    payload = json.loads(out)
    assert len(payload["data"]) == 10

    # Pass 1 — best-effort gid extraction for the cleanup safety net.
    # Sub-actions in a batch run independently, so the response can
    # legitimately mix 2xx and 4xx. Appending here guarantees the
    # ``created_tasks`` teardown still sees every server-side creation
    # even if Pass 2 raises mid-loop.
    task_gids: list[str] = []
    for result in payload["data"]:
        body_data = (result.get("body") or {}).get("data")
        if isinstance(body_data, dict) and "gid" in body_data:
            gid = body_data["gid"]
            task_gids.append(gid)
            created_tasks.append(gid)
    # Pass 2 — validate each result with (i, result) context so a
    # regression in Asana's response shape (e.g. missing ``gid``) is
    # diagnosed at the offending index rather than as an opaque
    # length mismatch.
    for i, result in enumerate(payload["data"]):
        assert 200 <= result["status_code"] < 300, (i, result)
        body_data = (result.get("body") or {}).get("data")
        assert isinstance(body_data, dict) and body_data.get("gid"), (i, result)

    # BATCH UPDATE — set a unique notes value per task.
    update_actions = [
        {
            "relative_path": f"/tasks/{gid}",
            "method": "put",
            "data": {"notes": f"batch-updated-{i:02d}"},
        }
        for i, gid in enumerate(task_gids)
    ]
    code, out, _ = _run(
        "batch-api",
        "create-batch-request",
        "--body",
        _batch_body(update_actions),
        "--full-payload",
    )
    assert code == 0, out
    payload = json.loads(out)
    for i, result in enumerate(payload["data"]):
        assert result["status_code"] == 200, (i, result)
        assert result["body"]["data"]["gid"] == task_gids[i]

    # BATCH GET — confirm the updated notes round-trip.
    get_actions = [
        {
            "relative_path": f"/tasks/{gid}",
            "method": "get",
            "options": {"fields": ["gid", "name", "notes"]},
        }
        for gid in task_gids
    ]
    code, out, _ = _run(
        "batch-api",
        "create-batch-request",
        "--body",
        _batch_body(get_actions),
        "--full-payload",
    )
    assert code == 0, out
    payload = json.loads(out)
    for i, result in enumerate(payload["data"]):
        assert result["status_code"] == 200, (i, result)
        body_data = result["body"]["data"]
        assert body_data["gid"] == task_gids[i]
        assert body_data["notes"] == f"batch-updated-{i:02d}"

    # BATCH DELETE.
    delete_actions = [{"relative_path": f"/tasks/{gid}", "method": "delete"} for gid in task_gids]
    code, out, _ = _run(
        "batch-api",
        "create-batch-request",
        "--body",
        _batch_body(delete_actions),
        "--full-payload",
    )
    assert code == 0, out
    payload = json.loads(out)
    for i, result in enumerate(payload["data"]):
        assert 200 <= result["status_code"] < 300, (i, result)
    for gid in task_gids:
        created_tasks.remove(gid)

    # DELETE PROJECT (single, not batched).
    code, _, _ = _run("projects", "delete-project", "--project", project_gid)
    assert code == 0
    created_projects.remove(project_gid)
