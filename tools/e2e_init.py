"""Provision the workspace fixtures used by ``tests/e2e/``.

Creates two projects under ``ASANA_PYTEST_WORKSPACE``:

- ``pagination-test`` with 1500 tasks named ``ptest-0001`` .. ``ptest-1500``
- ``pagination-test-small`` with 50 tasks named ``psmall-0001`` .. ``psmall-0050``

Idempotent: existing projects are reused, tasks already present (by name)
are skipped, and tasks NOT matching the expected name pattern within each
project are deleted so the project ends up with exactly the expected set.

Each project is treated as test-dedicated; do not run this against a
workspace that has other meaningful data in projects of these names.

Usage::

    export ASANA_ACCESS_TOKEN=...
    export ASANA_PYTEST_WORKSPACE=...
    uv run python tools/e2e_init.py
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar, cast

import asana
from asana.rest import ApiException

# Minimum interval between mutating API calls (measured from request start
# to request start) to stay under Asana's per-minute API limit
# (free tier ~150 req/min). 0.5s -> max ~120 req/min. If a single call
# already took longer than the interval, no extra sleep happens. Lower if
# your account allows.
MIN_INTERVAL_BETWEEN_WRITES = 0.5

# Retry transient API failures (HTTP 5xx, 429). Asana itself notes that
# 500 responses "usually" go away on retry. Exponential backoff caps the
# total wait at ~7s before giving up.
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 1.0  # seconds; wait = BASE * 2**attempt

_last_write_at: float = 0.0


def _throttle_write() -> None:
    """Sleep until ``MIN_INTERVAL_BETWEEN_WRITES`` has elapsed since the
    last throttled call, then update the timestamp."""
    global _last_write_at
    elapsed = time.monotonic() - _last_write_at
    remaining = MIN_INTERVAL_BETWEEN_WRITES - elapsed
    if remaining > 0:
        time.sleep(remaining)
    _last_write_at = time.monotonic()


_T = TypeVar("_T")


def _retry(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Call ``fn`` with exponential backoff on transient Asana failures
    (HTTP 5xx, 429). Non-transient failures propagate immediately."""
    for attempt in range(MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except ApiException as e:
            status = getattr(e, "status", 0) or 0
            transient = status >= 500 or status == 429
            if not transient or attempt == MAX_RETRIES - 1:
                raise
            wait = RETRY_BACKOFF_BASE * (2**attempt)
            print(f"  (transient {status}; retry {attempt + 1}/{MAX_RETRIES} after {wait:.1f}s)")
            time.sleep(wait)
    raise RuntimeError("unreachable")


@dataclass(frozen=True)
class ProjectSpec:
    name: str
    task_count: int
    task_name_fmt: str
    task_name_re: re.Pattern[str]


PROJECT_SPECS: list[ProjectSpec] = [
    ProjectSpec(
        name="pagination-test",
        task_count=1500,
        task_name_fmt="ptest-{:04d}",
        task_name_re=re.compile(r"^ptest-\d{4}$"),
    ),
    ProjectSpec(
        name="pagination-test-small",
        task_count=50,
        task_name_fmt="psmall-{:04d}",
        task_name_re=re.compile(r"^psmall-\d{4}$"),
    ),
]


def _client() -> asana.ApiClient:
    token = os.environ.get("ASANA_ACCESS_TOKEN")
    if not token:
        sys.exit("ASANA_ACCESS_TOKEN is not set")
    config = asana.Configuration()
    config.access_token = token
    return asana.ApiClient(config)


def _find_project(projects_api: asana.ProjectsApi, workspace_gid: str, name: str) -> str | None:
    projects = cast(
        "list[dict[str, Any]]",
        _retry(projects_api.get_projects_for_workspace, workspace_gid, {"opt_fields": "name"}),
    )
    for p in projects:
        if p.get("name") == name:
            return cast("str", p["gid"])
    return None


def _create_project(projects_api: asana.ProjectsApi, workspace_gid: str, name: str) -> str:
    result = cast(
        "dict[str, Any]",
        _retry(
            projects_api.create_project_for_workspace,
            {"data": {"name": name}},
            workspace_gid,
            {},
        ),
    )
    return cast("str", result["gid"])


def _all_tasks(tasks_api: asana.TasksApi, project_gid: str) -> list[dict[str, Any]]:
    return list(
        cast(
            "list[dict[str, Any]]",
            _retry(
                tasks_api.get_tasks,
                {"project": project_gid, "opt_fields": "name", "limit": 100},
            ),
        )
    )


def _create_task(tasks_api: asana.TasksApi, name: str, project_gid: str) -> None:
    _retry(tasks_api.create_task, {"data": {"name": name, "projects": [project_gid]}}, {})


def _delete_task(tasks_api: asana.TasksApi, task_gid: str) -> None:
    _retry(tasks_api.delete_task, task_gid)


def _provision(
    projects_api: asana.ProjectsApi,
    tasks_api: asana.TasksApi,
    workspace_gid: str,
    spec: ProjectSpec,
) -> None:
    print(f"\n--- provisioning project: {spec.name} ---")
    project_gid = _find_project(projects_api, workspace_gid, spec.name)
    if project_gid:
        print(f"reusing project ({project_gid})")
    else:
        project_gid = _create_project(projects_api, workspace_gid, spec.name)
        print(f"created project ({project_gid})")

    tasks = _all_tasks(tasks_api, project_gid)
    print(f"found {len(tasks)} task(s) in project")

    strays = [t for t in tasks if not spec.task_name_re.match(t.get("name", "") or "")]
    if strays:
        print(f"deleting {len(strays)} stray task(s) not matching {spec.task_name_fmt} ...")
        for i, t in enumerate(strays, 1):
            _throttle_write()
            _delete_task(tasks_api, cast("str", t["gid"]))
            if i % 50 == 0:
                print(f"  {i}/{len(strays)} deleted")
        tasks = _all_tasks(tasks_api, project_gid)

    existing_names = {cast("str", t["name"]) for t in tasks if t.get("name")}

    missing = [
        spec.task_name_fmt.format(i)
        for i in range(1, spec.task_count + 1)
        if spec.task_name_fmt.format(i) not in existing_names
    ]
    if not missing:
        print(f"all {spec.task_count} tasks already present; nothing to create")
        return

    print(f"creating {len(missing)} missing task(s) ...")
    for i, name in enumerate(missing, 1):
        _throttle_write()
        _create_task(tasks_api, name, project_gid)
        if i % 100 == 0:
            print(f"  {i}/{len(missing)} created")
    print(f"done. created {len(missing)} task(s).")


def main() -> int:
    workspace_gid = os.environ.get("ASANA_PYTEST_WORKSPACE")
    if not workspace_gid:
        print("ASANA_PYTEST_WORKSPACE is not set", file=sys.stderr)
        return 1

    client = _client()
    projects_api = asana.ProjectsApi(client)
    tasks_api = asana.TasksApi(client)

    for spec in PROJECT_SPECS:
        _provision(projects_api, tasks_api, workspace_gid, spec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
