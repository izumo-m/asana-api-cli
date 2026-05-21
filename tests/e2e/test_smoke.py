"""Smoke tests that exercise the vcrpy record/replay loop end-to-end.

Run live (records / refreshes cassettes)::

    ASANA_PYTEST_ENABLE_E2E=1 ASANA_PYTEST_WORKSPACE=<gid> \\
        uv run pytest --record-mode=all tests/e2e/test_smoke.py

Run from cassette (no network)::

    ASANA_PYTEST_ENABLE_E2E=1 uv run pytest tests/e2e/test_smoke.py
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from asana_api_cli.cli import main


@pytest.mark.vcr
def test_workspaces_get_workspace(workspace_gid: str) -> None:
    """Get a single workspace by GID — exercises ${WORKSPACE_GID} templating
    in the request path AND in the response body.
    """
    result = CliRunner().invoke(main, ["workspaces", "get-workspace", "--workspace", workspace_gid])
    assert result.exit_code == 0, result.output
    ws = json.loads(result.output)
    assert ws["resource_type"] == "workspace"
    # Returned gid round-trips to whatever we asked for.
    assert ws["gid"] == workspace_gid
    assert isinstance(ws["name"], str) and ws["name"]


@pytest.mark.vcr
def test_workspaces_get_workspaces(workspace_gid: str) -> None:
    """List all workspaces. Account-shape tolerant: the test workspace
    must appear with the expected shape, but the total count and other
    workspaces' gids are not asserted (they vary per Asana account, and
    workspaces cannot be created or deleted via the API).

    Replay portability: ``${WORKSPACE_GID}`` substitution puts the
    current env's gid into the response on replay, so the "contains the
    test workspace" assertion holds regardless of which account recorded
    the cassette.
    """
    result = CliRunner().invoke(main, ["workspaces", "get-workspaces"])
    assert result.exit_code == 0, result.output
    workspaces = json.loads(result.output)
    assert isinstance(workspaces, list)
    for ws in workspaces:
        assert ws.get("resource_type") == "workspace"
        assert isinstance(ws.get("gid"), str) and ws["gid"]
        assert isinstance(ws.get("name"), str) and ws["name"]
    match = next((ws for ws in workspaces if ws["gid"] == workspace_gid), None)
    assert match is not None, f"test workspace {workspace_gid} not in {workspaces}"
    # Name is real at record time, ``${WORKSPACE_NAME}``-substituted on
    # replay; both are non-empty strings, so we only assert the shape.
    assert isinstance(match["name"], str) and match["name"]
