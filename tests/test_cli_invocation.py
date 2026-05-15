"""End-to-end tests for the CLI -> SDK invocation path.

Each test builds a real command via ``_make_command`` against a real
``*Api`` class, replaces the underlying SDK method with a ``MagicMock``,
drives the command through ``CliRunner``, and asserts on what reached the
SDK (positional args, opts dict, call count).

This catches bugs in the argument-plumbing layer that structural tests
miss -- for example the ``--max-items > 100`` regression where the CLI
forwarded ``limit=N`` to the SDK even though Asana caps ``limit`` at 100.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import asana
import click
import pytest
from click.testing import CliRunner

from asana_api_cli.cli import _enumerate_api_classes, _make_command, _operations_for
from asana_api_cli.session import runtime


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Provide a token and clear ``runtime`` between tests.

    ``AsanaSession.from_env`` exits if no token is set. We also snapshot and
    restore the process-wide ``runtime`` dataclass so flags set by one test
    cannot leak into the next.
    """
    monkeypatch.setenv("ASANA_ACCESS_TOKEN", "test-token")
    monkeypatch.delenv("ASANA_DEFAULT_WORKSPACE", raising=False)
    saved = {
        name: getattr(runtime, name)
        for name in (
            "debug",
            "host",
            "proxy",
            "verify_ssl",
            "ssl_ca_cert",
            "retries",
            "timeout",
            "access_token",
            "temp_dir",
        )
    }
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(runtime, name, value)


def _build_command(api_cls_name: str, method_name: str) -> click.Command:
    """Build the real CLI command bound to ``asana.<api_cls_name>.<method_name>``."""
    api_cls = next(c for c in _enumerate_api_classes() if c.__name__ == api_cls_name)
    op = next(o for o in _operations_for(api_cls) if o.method_name == method_name)
    return _make_command(api_cls, op)


def _page(items: list[dict[str, Any]], offset: str | None = None) -> dict[str, Any]:
    """Build a single SDK page response (``{"data": ..., "next_page": ...}``)."""
    return {
        "data": items,
        "next_page": {"offset": offset} if offset else None,
    }


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    api_cls_name: str,
    method_name: str,
    *,
    side_effect: list[Any] | None = None,
    return_value: Any = None,
) -> MagicMock:
    """Replace an SDK method with a ``MagicMock`` and return the mock.

    ``MagicMock.call_args_list[i].args`` reflects the arguments the CLI
    passed in (without ``self``, since the stub strips it). Mutable args
    (the ``opts`` dict) are deep-copied before being recorded so that
    ``fetch_capped``'s in-place mutation across iterations does not collapse
    every recorded call into the dict's final state.
    """
    api_cls = getattr(asana, api_cls_name)
    mock = MagicMock(
        side_effect=side_effect,
        return_value=return_value if side_effect is None else None,
    )

    def _stub(self: Any, *args: Any, **kwargs: Any) -> Any:
        snapped = tuple(copy.deepcopy(a) if isinstance(a, dict) else a for a in args)
        return mock(*snapped, **kwargs)

    monkeypatch.setattr(api_cls, method_name, _stub)
    return mock


# ---------------------------------------------------------------------------
# Pagination: --max-items size handling (regression area)
# ---------------------------------------------------------------------------


class TestMaxItemsLimitArg:
    """``--max-items`` must never push ``opts["limit"]`` above the API cap (100)."""

    def test_max_items_250_walks_100_100_50(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression: previously this forwarded ``limit=250`` and got a 400."""
        cmd = _build_command("TasksApi", "get_tasks")
        responses = [
            _page([{"gid": str(i)} for i in range(0, 100)], offset="off1"),
            _page([{"gid": str(i)} for i in range(100, 200)], offset="off2"),
            _page([{"gid": str(i)} for i in range(200, 250)]),
        ]
        mock = _patch(monkeypatch, "TasksApi", "get_tasks", side_effect=responses)

        result = CliRunner().invoke(cmd, ["--max-items", "250"])

        assert result.exit_code == 0, result.output
        assert mock.call_count == 3
        limits = [c.args[0]["limit"] for c in mock.call_args_list]
        assert limits == [100, 100, 50]
        # ``offset`` is threaded from each response into the next request.
        assert "offset" not in mock.call_args_list[0].args[0]
        assert mock.call_args_list[1].args[0]["offset"] == "off1"
        assert mock.call_args_list[2].args[0]["offset"] == "off2"

    def test_max_items_exactly_at_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(
            monkeypatch,
            "TasksApi",
            "get_tasks",
            side_effect=[_page([{"gid": str(i)} for i in range(100)])],
        )
        result = CliRunner().invoke(cmd, ["--max-items", "100"])
        assert result.exit_code == 0, result.output
        assert mock.call_count == 1
        assert mock.call_args_list[0].args[0]["limit"] == 100

    def test_max_items_below_cap_one_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(
            monkeypatch,
            "TasksApi",
            "get_tasks",
            side_effect=[_page([{"gid": str(i)} for i in range(5)])],
        )
        result = CliRunner().invoke(cmd, ["--max-items", "5"])
        assert result.exit_code == 0, result.output
        assert mock.call_count == 1
        assert mock.call_args_list[0].args[0]["limit"] == 5

    def test_max_items_zero_makes_no_api_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--max-items 0`` returns ``[]`` without ever calling the SDK."""
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(monkeypatch, "TasksApi", "get_tasks", return_value=_page([]))
        result = CliRunner().invoke(cmd, ["--max-items", "0"])
        assert result.exit_code == 0, result.output
        assert mock.call_count == 0
        assert json.loads(result.output) == []


# ---------------------------------------------------------------------------
# Pagination: termination conditions
# ---------------------------------------------------------------------------


class TestMaxItemsTermination:
    """``fetch_capped`` must terminate even when the server cuts pagination short."""

    def test_stops_when_offset_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No ``next_page.offset`` -> stop, even if ``--max-items`` was larger."""
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(
            monkeypatch,
            "TasksApi",
            "get_tasks",
            side_effect=[_page([{"gid": "1"}], offset=None)],
        )
        result = CliRunner().invoke(cmd, ["--max-items", "500"])
        assert result.exit_code == 0, result.output
        assert mock.call_count == 1
        assert json.loads(result.output) == [{"gid": "1"}]

    def test_stops_on_empty_page_with_phantom_offset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty page with a non-empty offset must not loop forever (zero-progress guard)."""
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(
            monkeypatch,
            "TasksApi",
            "get_tasks",
            side_effect=[_page([], offset="phantom")],
        )
        result = CliRunner().invoke(cmd, ["--max-items", "500"])
        assert result.exit_code == 0, result.output
        assert mock.call_count == 1


# ---------------------------------------------------------------------------
# Pagination: --page-size interaction with --max-items
# ---------------------------------------------------------------------------


class TestPageSizeWithMaxItems:
    """``--page-size`` controls per-request size; ``--max-items`` caps the total."""

    def test_page_size_smaller_than_max_items_used_as_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _build_command("TasksApi", "get_tasks")
        responses = [
            _page([{"gid": str(i)} for i in range(0, 50)], offset="o1"),
            _page([{"gid": str(i)} for i in range(50, 100)], offset="o2"),
            _page([{"gid": str(i)} for i in range(100, 120)]),
        ]
        mock = _patch(monkeypatch, "TasksApi", "get_tasks", side_effect=responses)
        result = CliRunner().invoke(cmd, ["--page-size", "50", "--max-items", "120"])
        assert result.exit_code == 0, result.output
        assert [c.args[0]["limit"] for c in mock.call_args_list] == [50, 50, 20]

    def test_page_size_larger_than_max_items_shrinks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(
            monkeypatch,
            "TasksApi",
            "get_tasks",
            side_effect=[_page([{"gid": str(i)} for i in range(20)])],
        )
        result = CliRunner().invoke(cmd, ["--page-size", "50", "--max-items", "20"])
        assert result.exit_code == 0, result.output
        assert mock.call_count == 1
        assert mock.call_args_list[0].args[0]["limit"] == 20


# ---------------------------------------------------------------------------
# Pagination: default, --all-items, --offset, and the removed --paginate alias
# ---------------------------------------------------------------------------


class TestPaginationModes:
    """The four pagination modes: default, ``--max-items``, ``--all-items``, ``--offset``."""

    def test_default_single_page_no_limit_in_opts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without any pagination flag we don't push ``limit`` into opts."""
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(
            monkeypatch,
            "TasksApi",
            "get_tasks",
            return_value=_page([{"gid": "1"}, {"gid": "2"}]),
        )
        result = CliRunner().invoke(cmd, [])
        assert result.exit_code == 0, result.output
        assert mock.call_count == 1
        assert "limit" not in mock.call_args_list[0].args[0]

    def test_all_items_does_not_push_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--all-items`` delegates pagination to the SDK; we don't set ``limit``."""
        cmd = _build_command("TasksApi", "get_tasks")
        # Emulate the SDK's PageIterator with a plain iterator -- the
        # formatter wrapper collapses any non-list iterable to a list.
        mock = _patch(
            monkeypatch,
            "TasksApi",
            "get_tasks",
            return_value=iter([{"gid": "1"}, {"gid": "2"}]),
        )
        result = CliRunner().invoke(cmd, ["--all-items"])
        assert result.exit_code == 0, result.output
        assert mock.call_count == 1
        assert "limit" not in mock.call_args_list[0].args[0]
        assert json.loads(result.output) == [{"gid": "1"}, {"gid": "2"}]

    def test_paginate_alias_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--paginate`` was removed in 2.1.0; the option is no longer accepted."""
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(monkeypatch, "TasksApi", "get_tasks", return_value=_page([]))
        result = CliRunner().invoke(cmd, ["--paginate"])
        assert result.exit_code != 0
        assert "No such option: --paginate" in result.output
        assert mock.call_count == 0

    def test_max_items_with_all_items_is_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(monkeypatch, "TasksApi", "get_tasks", return_value=_page([]))
        result = CliRunner().invoke(cmd, ["--max-items", "10", "--all-items"])
        assert result.exit_code != 0
        assert "cannot be combined" in result.output
        assert mock.call_count == 0

    def test_offset_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--offset TOKEN`` reaches ``opts["offset"]`` on a single request."""
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(
            monkeypatch,
            "TasksApi",
            "get_tasks",
            return_value=_page([{"gid": "1"}]),
        )
        result = CliRunner().invoke(cmd, ["--offset", "abc123"])
        assert result.exit_code == 0, result.output
        assert mock.call_args_list[0].args[0]["offset"] == "abc123"


# ---------------------------------------------------------------------------
# Argument forwarding: positionals, body, opts
# ---------------------------------------------------------------------------


class TestArgumentForwarding:
    """Path positionals, ``--body``, and arbitrary opts flow to the SDK as documented."""

    def test_path_positional_is_first_call_arg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--task GID`` -> SDK called as ``get_task(GID, opts)``."""
        cmd = _build_command("TasksApi", "get_task")
        mock = _patch(
            monkeypatch,
            "TasksApi",
            "get_task",
            return_value={"data": {"gid": "TASK"}},
        )
        result = CliRunner().invoke(cmd, ["--task", "TASK_GID"])
        assert result.exit_code == 0, result.output
        assert mock.call_args_list[0].args[0] == "TASK_GID"

    def test_body_is_parsed_and_passed_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = _build_command("TasksApi", "create_task")
        mock = _patch(
            monkeypatch,
            "TasksApi",
            "create_task",
            return_value={"data": {"gid": "NEW"}},
        )
        body_json = '{"data": {"name": "x"}}'
        result = CliRunner().invoke(cmd, ["--body", body_json])
        assert result.exit_code == 0, result.output
        # ``body`` (parsed JSON) is the first positional, opts is the second.
        assert mock.call_args_list[0].args[0] == {"data": {"name": "x"}}

    def test_arbitrary_opt_param_reaches_opts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = _build_command("TasksApi", "get_task")
        mock = _patch(monkeypatch, "TasksApi", "get_task", return_value={"data": {}})
        result = CliRunner().invoke(cmd, ["--task", "T", "--opt-fields", "name,gid"])
        assert result.exit_code == 0, result.output
        opts = mock.call_args_list[0].args[1]
        assert opts["opt_fields"] == "name,gid"

    def test_optional_unset_is_omitted_from_opts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An optional opt the user did not supply must NOT be sent as ``None``."""
        cmd = _build_command("TasksApi", "get_task")
        mock = _patch(monkeypatch, "TasksApi", "get_task", return_value={"data": {}})
        result = CliRunner().invoke(cmd, ["--task", "T"])
        assert result.exit_code == 0, result.output
        opts = mock.call_args_list[0].args[1]
        assert "opt_fields" not in opts

    def test_method_without_opts_called_without_opts_dict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``delete_task`` has no ``opts`` parameter; the CLI must not pass one."""
        cmd = _build_command("TasksApi", "delete_task")
        mock = _patch(monkeypatch, "TasksApi", "delete_task", return_value={"data": {}})
        result = CliRunner().invoke(cmd, ["--task", "T"])
        assert result.exit_code == 0, result.output
        assert mock.call_args_list[0].args == ("T",)


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------


class TestWorkspaceResolution:
    """``--workspace`` resolves differently depending on whether the endpoint requires it."""

    def test_explicit_workspace_reaches_opts_when_optional(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``get-tasks`` exposes ``workspace`` as an optional opts param."""
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(monkeypatch, "TasksApi", "get_tasks", return_value=_page([]))
        result = CliRunner().invoke(cmd, ["--workspace", "WS123"])
        assert result.exit_code == 0, result.output
        assert mock.call_args_list[0].args[0]["workspace"] == "WS123"

    def test_env_var_not_used_when_workspace_is_optional(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``$ASANA_DEFAULT_WORKSPACE`` must not be auto-filled for optional endpoints."""
        monkeypatch.setenv("ASANA_DEFAULT_WORKSPACE", "ENV_WS")
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(monkeypatch, "TasksApi", "get_tasks", return_value=_page([]))
        result = CliRunner().invoke(cmd, [])
        assert result.exit_code == 0, result.output
        assert "workspace" not in mock.call_args_list[0].args[0]

    def test_env_var_fills_required_positional_workspace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``get-projects-for-workspace`` takes ``workspace_gid`` as a path positional."""
        monkeypatch.setenv("ASANA_DEFAULT_WORKSPACE", "ENV_WS")
        cmd = _build_command("ProjectsApi", "get_projects_for_workspace")
        mock = _patch(
            monkeypatch,
            "ProjectsApi",
            "get_projects_for_workspace",
            return_value=_page([]),
        )
        result = CliRunner().invoke(cmd, [])
        assert result.exit_code == 0, result.output
        # workspace_gid is positional, so it shows up as the first call arg.
        assert mock.call_args_list[0].args[0] == "ENV_WS"

    def test_explicit_workspace_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ASANA_DEFAULT_WORKSPACE", "ENV_WS")
        cmd = _build_command("ProjectsApi", "get_projects_for_workspace")
        mock = _patch(
            monkeypatch,
            "ProjectsApi",
            "get_projects_for_workspace",
            return_value=_page([]),
        )
        result = CliRunner().invoke(cmd, ["--workspace", "EXPLICIT_WS"])
        assert result.exit_code == 0, result.output
        assert mock.call_args_list[0].args[0] == "EXPLICIT_WS"

    def test_required_workspace_missing_exits_without_calling_sdk(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _build_command("ProjectsApi", "get_projects_for_workspace")
        mock = _patch(
            monkeypatch,
            "ProjectsApi",
            "get_projects_for_workspace",
            return_value=_page([]),
        )
        result = CliRunner().invoke(cmd, [])
        assert result.exit_code != 0
        assert mock.call_count == 0
