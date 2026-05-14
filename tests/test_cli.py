"""Tests for asana_api_cli.cli — runtime introspection helpers and command tree.

Verifies that the CLI built at import time from the live ``asana`` SDK has
the expected naming, docstring parsing behavior, command shape, and special
handling for body / workspace / pagination.
"""

from __future__ import annotations

import click
import pytest

from asana_api_cli.cli import (
    _Operation,
    _api_class_to_group,
    _enumerate_api_classes,
    _extract_operation,
    _make_command,
    _method_to_command,
    _operations_for,
    _parse_params,
    _parse_summary,
    _snake,
    main,
)


# ---------------------------------------------------------------------------
# Name conversion
# ---------------------------------------------------------------------------


class TestNaming:
    def test_snake_simple(self) -> None:
        assert _snake("Tasks") == "tasks"
        assert _snake("CustomFields") == "custom_fields"

    def test_snake_with_acronym(self) -> None:
        assert _snake("AuditLogAPI") == "audit_log_api"
        assert _snake("BatchAPI") == "batch_api"

    def test_api_class_to_group(self) -> None:
        assert _api_class_to_group("TasksApi") == "tasks"
        assert _api_class_to_group("AuditLogAPIApi") == "audit_log_api"
        assert _api_class_to_group("BatchAPIApi") == "batch_api"
        assert _api_class_to_group("CustomFieldsApi") == "custom_fields"

    def test_method_to_command(self) -> None:
        assert _method_to_command("get_tasks") == "get-tasks"
        assert _method_to_command("create_task") == "create-task"


# ---------------------------------------------------------------------------
# Docstring parsing
# ---------------------------------------------------------------------------


class TestDocstringParse:
    def test_summary_strips_noqa(self) -> None:
        doc = "Get multiple tasks  # noqa: E501\n\n        <b>scope</b>..."
        assert _parse_summary(doc) == "Get multiple tasks"

    def test_param_detection(self) -> None:
        doc = """Get tasks

        :param async_req bool
        :param int limit: Results per page.
        :param str task_gid: The task to operate on. (required)
        :param list[str] opt_fields: Fields list.
        """
        params = _parse_params(doc)
        assert "async_req" not in params  # SDK-internal flag excluded
        assert params["limit"].py_type == "int"
        assert params["limit"].required is False
        assert params["task_gid"].py_type == "str"
        assert params["task_gid"].required is True
        assert "(required)" not in params["task_gid"].description
        assert params["opt_fields"].py_type == "list[str]"


# ---------------------------------------------------------------------------
# SDK introspection
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def api_classes() -> list[type]:
    return _enumerate_api_classes()


@pytest.fixture(scope="module")
def tasks_cls(api_classes: list[type]) -> type:
    return next(c for c in api_classes if c.__name__ == "TasksApi")


@pytest.fixture(scope="module")
def tasks_ops(tasks_cls: type) -> list[_Operation]:
    return _operations_for(tasks_cls)


class TestIntrospect:
    def test_has_core_apis(self, api_classes: list[type]) -> None:
        names = {c.__name__ for c in api_classes}
        assert "TasksApi" in names
        assert "WorkspacesApi" in names
        assert "ProjectsApi" in names

    def test_skips_with_http_info(self, tasks_ops: list[_Operation]) -> None:
        names = {op.method_name for op in tasks_ops}
        assert "get_tasks" in names
        assert not any(n.endswith("_with_http_info") for n in names)

    def test_get_tasks_shape(self, tasks_ops: list[_Operation]) -> None:
        op = next(o for o in tasks_ops if o.method_name == "get_tasks")
        assert op.positional == []
        assert op.has_opts is True
        assert op.has_body is False
        assert op.paginatable is True
        opts_names = {p.name for p in op.opts_params}
        assert "limit" in opts_names
        assert "opt_fields" in opts_names

    def test_get_task_positional(self, tasks_ops: list[_Operation]) -> None:
        op = next(o for o in tasks_ops if o.method_name == "get_task")
        assert op.positional == ["task_gid"]
        assert op.has_body is False

    def test_create_task_has_body(self, tasks_ops: list[_Operation]) -> None:
        op = next(o for o in tasks_ops if o.method_name == "create_task")
        assert op.has_body is True
        assert "body" in op.positional

    def test_delete_task_no_opts(self, tasks_ops: list[_Operation]) -> None:
        op = next(o for o in tasks_ops if o.method_name == "delete_task")
        assert op.positional == ["task_gid"]
        assert op.has_opts is False

    def test_extract_operation_skips_private(self) -> None:
        # Private methods and the with_http_info variants must be skipped.
        for method_name in ("_internal", "get_tasks_with_http_info"):
            assert _extract_operation(method_name, lambda self: None) is None


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def get_tasks_cmd(tasks_cls: type, tasks_ops: list[_Operation]) -> click.Command:
    op = next(o for o in tasks_ops if o.method_name == "get_tasks")
    return _make_command(tasks_cls, op)


@pytest.fixture(scope="module")
def get_task_cmd(tasks_cls: type, tasks_ops: list[_Operation]) -> click.Command:
    op = next(o for o in tasks_ops if o.method_name == "get_task")
    return _make_command(tasks_cls, op)


@pytest.fixture(scope="module")
def create_task_cmd(tasks_cls: type, tasks_ops: list[_Operation]) -> click.Command:
    op = next(o for o in tasks_ops if o.method_name == "create_task")
    return _make_command(tasks_cls, op)


def _option_flags(cmd: click.Command) -> set[str]:
    flags: set[str] = set()
    for p in cmd.params:
        for decl in p.opts:
            flags.add(decl)
    return flags


class TestBuiltCommands:
    def test_get_task_has_task_option(self, get_task_cmd: click.Command) -> None:
        # Path positional ``task_gid`` becomes ``--task`` (gid suffix stripped).
        assert "--task" in _option_flags(get_task_cmd)

    def test_get_tasks_pagination_options(self, get_tasks_cmd: click.Command) -> None:
        flags = _option_flags(get_tasks_cmd)
        assert "--all-items" in flags
        assert "--paginate" in flags
        assert "--page-size" in flags
        assert "--max-items" in flags

    def test_get_tasks_hides_raw_limit(self, get_tasks_cmd: click.Command) -> None:
        # ``--limit`` is replaced by ``--page-size``.
        assert "--limit" not in _option_flags(get_tasks_cmd)
        # ``--offset`` stays as a passthrough for manual pagination.
        assert "--offset" in _option_flags(get_tasks_cmd)

    def test_get_tasks_workspace_option(self, get_tasks_cmd: click.Command) -> None:
        # ``workspace`` opt is exposed as ``--workspace``.
        assert "--workspace" in _option_flags(get_tasks_cmd)

    def test_create_task_body_required(self, create_task_cmd: click.Command) -> None:
        body_param = next(p for p in create_task_cmd.params if "--body" in p.opts)
        assert body_param.required is True

    def test_get_task_no_pagination(self, get_task_cmd: click.Command) -> None:
        flags = _option_flags(get_task_cmd)
        assert "--all-items" not in flags
        assert "--page-size" not in flags
        assert "--max-items" not in flags

    def test_output_query_options_present(self, get_tasks_cmd: click.Command) -> None:
        flags = _option_flags(get_tasks_cmd)
        assert "--output" in flags
        assert "--query" in flags


# ---------------------------------------------------------------------------
# Root group integration
# ---------------------------------------------------------------------------


class TestRootGroup:
    def test_main_is_click_group(self) -> None:
        assert isinstance(main, click.Group)

    def test_main_lists_known_groups(self) -> None:
        ctx = click.Context(main)
        names = set(main.list_commands(ctx))
        for expected in ("tasks", "projects", "workspaces", "users"):
            assert expected in names

    def test_main_has_global_options(self) -> None:
        flags = _option_flags(main)
        for expected in (
            "--host",
            "--proxy",
            "--no-verify-ssl",
            "--ca-cert",
            "--retries",
            "--timeout",
            "--access-token",
            "--temp-dir",
            "--debug",
        ):
            assert expected in flags

    def test_subgroup_help_resolves(self) -> None:
        # Resolving a subgroup must trigger lazy method introspection.
        ctx = click.Context(main)
        tasks_group = main.get_command(ctx, "tasks")
        assert isinstance(tasks_group, click.Group)
        sub_ctx = click.Context(tasks_group, parent=ctx)
        cmd_names = set(tasks_group.list_commands(sub_ctx))
        assert "get-tasks" in cmd_names
        assert "create-task" in cmd_names
