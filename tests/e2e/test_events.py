"""End-to-end test for the events API sync-token cycle.

Asana's events endpoint returns HTTP 412 with a fresh sync token in the
response body on the initial poll (no sync given). The CLI surfaces this
via ``--exception-output json`` (envelope on stdout, exit 3) when
``--full-payload`` is also set; without ``--full-payload`` the SDK's
``EventIterator`` absorbs the 412 silently and the fresh token is lost
inside the iterator. The test exercises the bootstrap → trigger → poll
cycle a shell script would use to keep up with task changes.

Live record::

    ASANA_PYTEST_WORKSPACE=<gid> \\
        uv run pytest --live --record tests/e2e/test_events.py

Replay::

    uv run pytest tests/e2e/test_events.py
"""

from __future__ import annotations

import json
import time

import pytest
from _cli_runner import make_runner

from asana_api_cli.cli import main

PROPAGATION_SLEEP_SEC = 2.0


def _run(*args: str) -> "tuple[int, str, str]":
    result = make_runner().invoke(main, list(args))
    return result.exit_code, result.stdout, result.stderr


def _is_live(config: pytest.Config) -> bool:
    """Mirror of ``tests/e2e/conftest.py:_is_live_mode``.

    Inlined to keep the test file self-contained; if a second test needs
    the same check, promote it to a conftest fixture.
    """
    if config.getoption("--live", default=False):
        return True
    if config.getoption("--disable-recording", default=False):
        return True
    mode = config.getoption("--record-mode", default="none") or "none"
    return mode != "none"


@pytest.mark.vcr
def test_events_sync_cycle(
    pagination_project_gid: str,
    created_tasks: list[str],
    request: pytest.FixtureRequest,
) -> None:
    """events get-events sync cycle: bootstrap (412) → trigger → poll.

    Subscribes to a freshly-created task, triggers a name-change, and
    asserts the rename event surfaces in a follow-up poll. ``data`` may
    also include create-time noise events (story/added, task/added for
    project / section), depending on Asana's event-indexing timing, so
    the assertion is "at least one entry matches the rename" rather than
    a count.
    """
    name = "pytest-e2e-events-sync"

    # CREATE — dedicated task so events have a stable, noise-free origin
    code, out, _ = _run(
        "tasks",
        "create-task",
        "--body",
        json.dumps({"data": {"name": name, "projects": [pagination_project_gid]}}),
    )
    assert code == 0, out
    task = json.loads(out)
    task_gid = task["gid"]
    created_tasks.append(task_gid)

    # BOOTSTRAP — initial poll returns 412 with a fresh sync token.
    # --full-payload is load-bearing: without it the SDK's EventIterator
    # absorbs the 412 and the token is lost.
    # --exception-output json is required: the default 'none' would exit 1
    # with the body on stderr (readable but not capturable as JSON),
    # instead of giving us an envelope on stdout to parse.
    code, out, err = _run(
        "events",
        "get-events",
        "--resource",
        task_gid,
        "--full-payload",
        "--exception-output",
        "json",
    )
    assert code == 3, f"bootstrap should exit 3 (412), got {code}: {err}"
    envelope = json.loads(out)
    assert envelope["exception"] == "asana.rest.ApiException"
    assert envelope["status"] == 412
    # body is the UTF-8 decoded response string; parse to extract the sync token.
    body = json.loads(envelope["body"])
    sync = body["sync"]
    assert sync, "bootstrap envelope must contain a sync token"

    # TRIGGER — rename the task
    new_name = name + "-renamed"
    code, out, _ = _run(
        "tasks",
        "update-task",
        "--task",
        task_gid,
        "--body",
        json.dumps({"data": {"name": new_name}}),
    )
    assert code == 0, out

    # PROPAGATION — wait for Asana to index the event. Skipped in replay
    # because the cassette already captures the post-propagation response.
    if _is_live(request.config):
        time.sleep(PROPAGATION_SLEEP_SEC)

    # POLL — fetch the event payload using the bootstrap sync
    code, out, err = _run(
        "events",
        "get-events",
        "--resource",
        task_gid,
        "--sync",
        sync,
        "--full-payload",
    )
    assert code == 0, f"poll should exit 0, got {code}: {err}"
    payload = json.loads(out)
    assert "data" in payload
    assert "sync" in payload

    rename_events = [
        e
        for e in payload["data"]
        if e.get("type") == "task"
        and e.get("action") == "changed"
        and e.get("change", {}).get("field") == "name"
        and e.get("resource", {}).get("gid") == task_gid
    ]
    assert len(rename_events) >= 1, (
        f"expected at least one task name-change event for {task_gid}; "
        f"got {len(payload['data'])} events: {payload['data']}"
    )

    # DELETE
    code, _, _ = _run("tasks", "delete-task", "--task", task_gid)
    assert code == 0
    created_tasks.remove(task_gid)
