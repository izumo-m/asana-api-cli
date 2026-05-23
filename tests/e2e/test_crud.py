"""End-to-end CRUD tests for project and task resources.

Each test creates a resource, exercises get / update, then explicitly
deletes it. The ``created_projects`` / ``created_tasks`` fixtures provide
a safety-net teardown so a failed test does not leak resources in the
live workspace.

Live record::

    ASANA_PYTEST_ENABLE_E2E=1 ASANA_PYTEST_WORKSPACE=<gid> \\
        uv run pytest --record-mode=all tests/e2e/test_crud.py

Replay::

    ASANA_PYTEST_ENABLE_E2E=1 ASANA_PYTEST_WORKSPACE=<gid> \\
        uv run pytest tests/e2e/test_crud.py
"""

from __future__ import annotations

import json

import pytest
from _cli_runner import make_runner

from asana_api_cli.cli import main


def _run(*args: str) -> "tuple[int, str, str]":
    result = make_runner().invoke(main, list(args))
    return result.exit_code, result.stdout, result.stderr


@pytest.mark.vcr
def test_project_crud(workspace_gid: str, created_projects: list[str]) -> None:
    """create-project -> get -> update -> delete a project in the workspace."""
    name = "pytest-e2e-test_project_crud"

    # CREATE
    code, out, _ = _run(
        "projects",
        "create-project-for-workspace",
        "--workspace",
        workspace_gid,
        "--body",
        json.dumps({"data": {"name": name}}),
    )
    assert code == 0, out
    project = json.loads(out)
    project_gid = project["gid"]
    created_projects.append(project_gid)
    assert project["name"] == name
    assert project["resource_type"] == "project"

    # GET
    code, out, _ = _run("projects", "get-project", "--project", project_gid)
    assert code == 0, out
    fetched = json.loads(out)
    assert fetched["gid"] == project_gid
    assert fetched["name"] == name

    # UPDATE
    new_name = name + "-updated"
    code, out, _ = _run(
        "projects",
        "update-project",
        "--project",
        project_gid,
        "--body",
        json.dumps({"data": {"name": new_name}}),
    )
    assert code == 0, out
    updated = json.loads(out)
    assert updated["gid"] == project_gid
    assert updated["name"] == new_name

    # DELETE
    code, _, _ = _run("projects", "delete-project", "--project", project_gid)
    assert code == 0
    created_projects.remove(project_gid)  # explicit delete succeeded


@pytest.mark.vcr
def test_task_crud(pagination_project_gid: str, created_tasks: list[str]) -> None:
    """create-task -> get -> update -> delete a task in the pagination-test project."""
    name = "pytest-e2e-test_task_crud"

    # CREATE
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
    assert task["name"] == name
    assert task["resource_type"] == "task"

    # GET
    code, out, _ = _run("tasks", "get-task", "--task", task_gid)
    assert code == 0, out
    fetched = json.loads(out)
    assert fetched["gid"] == task_gid
    assert fetched["name"] == name

    # UPDATE
    new_notes = "updated by e2e test"
    code, out, _ = _run(
        "tasks",
        "update-task",
        "--task",
        task_gid,
        "--body",
        json.dumps({"data": {"notes": new_notes}}),
    )
    assert code == 0, out
    updated = json.loads(out)
    assert updated["gid"] == task_gid
    assert updated.get("notes") == new_notes

    # DELETE
    code, _, _ = _run("tasks", "delete-task", "--task", task_gid)
    assert code == 0
    created_tasks.remove(task_gid)
