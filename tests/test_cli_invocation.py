"""End-to-end tests for the CLI -> SDK invocation path.

Each test builds a real command via ``_make_command`` against a real
``*Api`` class, replaces the underlying SDK method with a ``MagicMock``,
drives the command through ``CliRunner``, and asserts on what reached the
SDK (positional args, opts dict, kwargs, call count).

This catches bugs in the argument-plumbing layer that structural tests
miss -- for example forgetting to forward ``--max-items`` as the SDK's
``item_limit`` kwarg.
"""

from __future__ import annotations

import copy
import http.client
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
    (the ``opts`` dict) are deep-copied before being recorded so each
    call's snapshot survives any later in-place mutation.
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
# Pagination: --max-items forwards to the SDK's ``item_limit`` kwarg
# ---------------------------------------------------------------------------


class TestMaxItemsKwarg:
    """``--max-items N`` is forwarded as ``item_limit=N`` to the SDK method.

    The SDK's ``PageIterator`` then caps each per-request ``limit`` to
    ``min(page_limit, item_limit - count)`` and stops at exactly N items,
    so the CLI does not need to walk pages itself.
    """

    def test_max_items_passes_item_limit_kwarg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(
            monkeypatch,
            "TasksApi",
            "get_tasks",
            return_value=iter([{"gid": str(i)} for i in range(250)]),
        )
        result = CliRunner().invoke(cmd, ["--max-items", "250"])
        assert result.exit_code == 0, result.output
        assert mock.call_count == 1
        assert mock.call_args_list[0].kwargs == {"item_limit": 250}
        # CLI does not push ``limit`` into opts; that's the SDK's job.
        assert "limit" not in mock.call_args_list[0].args[0]
        assert len(json.loads(result.output)) == 250

    def test_max_items_zero_passes_item_limit_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--max-items 0`` still constructs a session and calls the SDK; the
        SDK's PageIterator short-circuits and makes no HTTP request."""
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(
            monkeypatch,
            "TasksApi",
            "get_tasks",
            return_value=iter([]),
        )
        result = CliRunner().invoke(cmd, ["--max-items", "0"])
        assert result.exit_code == 0, result.output
        assert mock.call_count == 1
        assert mock.call_args_list[0].kwargs == {"item_limit": 0}
        assert json.loads(result.output) == []

    def test_max_items_5_passes_item_limit_5(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(
            monkeypatch,
            "TasksApi",
            "get_tasks",
            return_value=iter([{"gid": str(i)} for i in range(5)]),
        )
        result = CliRunner().invoke(cmd, ["--max-items", "5"])
        assert result.exit_code == 0, result.output
        assert mock.call_args_list[0].kwargs == {"item_limit": 5}


# ---------------------------------------------------------------------------
# Pagination: --page-size feeds Configuration.page_limit
# ---------------------------------------------------------------------------


class TestPageSizeKwarg:
    """``--page-size N`` sets ``Configuration.page_limit = N`` for the SDK.

    The SDK reads ``page_limit`` to populate ``query_params['limit']`` when
    the caller did not. We assert by snapshotting the configuration at the
    moment the SDK method is invoked.
    """

    def _capture_page_limit_on_call(
        self, monkeypatch: pytest.MonkeyPatch, return_value: Any
    ) -> list[int]:
        captured: list[int] = []

        def patched_get_tasks(self_api: Any, opts: Any, **kwargs: Any) -> Any:
            captured.append(self_api.api_client.configuration.page_limit)
            return return_value

        monkeypatch.setattr(asana.TasksApi, "get_tasks", patched_get_tasks)
        return captured

    def test_page_size_sets_page_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = _build_command("TasksApi", "get_tasks")
        captured = self._capture_page_limit_on_call(monkeypatch, return_value=iter([{"gid": "1"}]))
        result = CliRunner().invoke(cmd, ["--page-size", "50", "--max-items", "10"])
        assert result.exit_code == 0, result.output
        assert captured == [50]

    def test_no_page_size_keeps_sdk_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default ``page_limit`` is 100 (the SDK default) when ``--page-size``
        is not passed."""
        cmd = _build_command("TasksApi", "get_tasks")
        captured = self._capture_page_limit_on_call(monkeypatch, return_value=_page([]))
        result = CliRunner().invoke(cmd, [])
        assert result.exit_code == 0, result.output
        assert captured == [100]


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
# Debug redactor lifecycle across pagination
# ---------------------------------------------------------------------------


class TestGlobalOptionValidation:
    """Type/range validation on global options that affect runtime state."""

    def test_retries_rejects_negative(self) -> None:
        """``--retries`` must be a non-negative integer; negative values
        previously silently disabled retries via urllib3.Retry."""
        from asana_api_cli.cli import main

        result = CliRunner().invoke(main, ["--retries", "-1"])
        assert result.exit_code != 0
        assert "Invalid value" in result.output or "is not in the range" in result.output

    def test_retries_zero_accepted(self) -> None:
        """``--retries 0`` is valid (disables retries explicitly)."""
        from asana_api_cli.cli import main

        # Click parses 0 fine; we don't run a subcommand so we expect a
        # missing-command error rather than a validation error.
        result = CliRunner().invoke(main, ["--retries", "0"])
        # Either succeeds (showing help) or fails with "Missing command",
        # but not with an IntRange validation error.
        assert "Invalid value" not in result.output


class TestDebugRedactorLifecycle:
    """The http.client debug redactor must stay installed for the duration
    of every paginated request, including the lazy per-page HTTP calls that
    `--all-items` triggers when the formatter iterates the SDK's
    PageIterator."""

    def test_all_items_with_debug_keeps_redactor_installed_during_iteration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression for v2.1.0: ``--all-items`` must consume the SDK
        PageIterator inside the ``AsanaSession`` ``with`` block. If the
        iterator is consumed after the session exits, the debug redactor
        is already gone and pages past the first leak the raw
        Authorization header to stderr.
        """
        redactor_states: list[bool] = []

        def _generator() -> Iterator[dict[str, Any]]:
            for i in range(3):
                current = http.client.__dict__.get("print")
                redactor_states.append(getattr(current, "_asana_cli_redactor", False))
                yield {"gid": str(i)}

        cmd = _build_command("TasksApi", "get_tasks")
        _patch(monkeypatch, "TasksApi", "get_tasks", return_value=_generator())
        monkeypatch.setattr(runtime, "debug", True)

        saved_print = http.client.__dict__.get("print")
        saved_debuglevel = http.client.HTTPConnection.debuglevel
        try:
            result = CliRunner().invoke(cmd, ["--all-items"])
            assert result.exit_code == 0, result.output
            assert len(redactor_states) == 3
            assert all(redactor_states), (
                "redactor must be installed during every PageIterator yield; "
                f"observed states={redactor_states}"
            )
        finally:
            if saved_print is None:
                http.client.__dict__.pop("print", None)
            else:
                http.client.print = saved_print  # pyright: ignore[reportAttributeAccessIssue]
            http.client.HTTPConnection.debuglevel = saved_debuglevel


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
