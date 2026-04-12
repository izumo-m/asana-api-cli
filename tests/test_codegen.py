"""Tests for tools/codegen.py.

Verifies that code generated from the official asana SDK introspection meets
the expected structure, naming conventions, and click command composition.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add tools/ to path so we can import the codegen module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from codegen import (  # noqa: E402
    ApiGroup,
    _api_class_to_group,
    _method_to_command,
    _parse_params,
    _parse_summary,
    _snake,
    generate_cli_init,
    generate_group_module,
    introspect_sdk,
)


# ---------------------------------------------------------------------------
# Name conversion
# ---------------------------------------------------------------------------


class TestNaming:
    def test_snake_simple(self) -> None:
        assert _snake("Tasks") == "tasks"
        assert _snake("CustomFields") == "custom_fields"

    def test_snake_with_acronym(self) -> None:
        # 'AuditLogAPI' -> 'audit_log_api'
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
def groups() -> list[ApiGroup]:
    return introspect_sdk()


@pytest.fixture(scope="module")
def tasks_group(groups: list[ApiGroup]) -> ApiGroup:
    return next(g for g in groups if g.class_name == "TasksApi")


@pytest.fixture(scope="module")
def tasks_code(tasks_group: ApiGroup) -> str:
    return generate_group_module(tasks_group)


class TestIntrospect:
    def test_has_tasks(self, groups: list[ApiGroup]) -> None:
        names = {g.class_name for g in groups}
        assert "TasksApi" in names
        assert "WorkspacesApi" in names
        assert "ProjectsApi" in names

    def test_skips_with_http_info(self, tasks_group: ApiGroup) -> None:
        names = {op.method_name for op in tasks_group.operations}
        assert "get_tasks" in names
        assert not any(n.endswith("_with_http_info") for n in names)

    def test_get_tasks_shape(self, tasks_group: ApiGroup) -> None:
        op = next(op for op in tasks_group.operations if op.method_name == "get_tasks")
        assert op.positional == []  # signature: (opts)
        assert op.has_opts is True
        assert op.has_body is False
        assert op.paginatable is True
        opts_names = {p.name for p in op.opts_params}
        assert "limit" in opts_names
        assert "opt_fields" in opts_names

    def test_get_task_positional(self, tasks_group: ApiGroup) -> None:
        op = next(op for op in tasks_group.operations if op.method_name == "get_task")
        assert op.positional == ["task_gid"]
        assert op.has_body is False

    def test_create_task_has_body(self, tasks_group: ApiGroup) -> None:
        op = next(op for op in tasks_group.operations if op.method_name == "create_task")
        assert op.has_body is True
        assert "body" in op.positional

    def test_delete_task_no_opts(self, tasks_group: ApiGroup) -> None:
        op = next(op for op in tasks_group.operations if op.method_name == "delete_task")
        assert op.positional == ["task_gid"]
        assert op.has_opts is False


# ---------------------------------------------------------------------------
# CLI generation
# ---------------------------------------------------------------------------


class TestGeneratedCli:
    def test_header(self, tasks_code: str) -> None:
        assert "# This file is auto-generated by tools/codegen.py" in tasks_code
        assert "from asana import TasksApi" in tasks_code
        assert "from asana_api_cli.formatter import formatted" in tasks_code
        assert "from asana_api_cli.session import AsanaSession" in tasks_code

    def test_group_decorator(self, tasks_code: str) -> None:
        assert '@click.group("tasks")' in tasks_code
        assert "def tasks_group() -> None:" in tasks_code

    def test_commands_present(self, tasks_code: str) -> None:
        assert '@tasks_group.command("get-tasks")' in tasks_code
        assert '@tasks_group.command("get-task")' in tasks_code
        assert '@tasks_group.command("create-task")' in tasks_code
        assert '@tasks_group.command("delete-task")' in tasks_code

    def test_option_for_path_positional(self, tasks_code: str) -> None:
        # *_gid positionals are converted to --{name} required options
        assert '@click.option("--task", required=True' in tasks_code

    def test_body_option_required(self, tasks_code: str) -> None:
        assert '@click.option("--body", required=True' in tasks_code
        assert "parsed_body = resolve_body(body)" in tasks_code

    def test_paginate_flag_for_list_ops(self, tasks_code: str) -> None:
        assert '"--paginate"' in tasks_code
        assert "AsanaSession.from_env(paginate=paginate)" in tasks_code

    def test_no_paginate_on_single_get(self, tasks_code: str) -> None:
        # get_task(task_gid, opts) has no limit in opts, so --paginate should be absent
        # Extract the function block to verify
        marker = 'tasks_group.command("get-task")\n'
        start = tasks_code.index(marker)
        next_cmd = tasks_code.index('tasks_group.command("', start + len(marker))
        block = tasks_code[start:next_cmd]
        assert '"--paginate"' not in block

    def test_calls_api_method(self, tasks_code: str) -> None:
        assert "api = TasksApi(session.client)" in tasks_code
        assert "return api.get_tasks(opts)" in tasks_code
        assert "return api.get_task(task, opts)" in tasks_code


# ---------------------------------------------------------------------------
# cli/__init__.py generation
# ---------------------------------------------------------------------------


class TestCliInit:
    def test_imports(self, groups: list[ApiGroup]) -> None:
        code = generate_cli_init(groups)
        assert "from asana_api_cli.cli.tasks import tasks_group" in code
        assert "from asana_api_cli.cli.workspaces import workspaces_group" in code

    def test_main_group(self, groups: list[ApiGroup]) -> None:
        code = generate_cli_init(groups)
        assert "@click.group()" in code
        assert "def main(" in code
        assert "debug: bool," in code
        assert '"--debug"' in code
        assert "runtime.debug = debug" in code
        # Global options
        for flag in (
            '"--host"',
            '"--proxy"',
            '"--no-verify-ssl"',
            '"--ca-cert"',
            '"--page-limit"',
            '"--retries"',
            '"--timeout"',
            '"--token-env"',
            '"--temp-dir"',
        ):
            assert flag in code, f"missing option {flag}"
        assert "runtime.host = host" in code
        assert "runtime.verify_ssl = not no_verify_ssl" in code
        assert "runtime.ssl_ca_cert = ca_cert" in code

    def test_add_command(self, groups: list[ApiGroup]) -> None:
        code = generate_cli_init(groups)
        assert "main.add_command(tasks_group)" in code
        assert "main.add_command(workspaces_group)" in code


# ---------------------------------------------------------------------------
# Generated code syntax verification
# ---------------------------------------------------------------------------


class TestGeneratedCodeCompiles:
    def test_all_groups_compile(self, groups: list[ApiGroup]) -> None:
        """Verify that generated code for all tags is syntactically valid Python."""
        for g in groups:
            code = generate_group_module(g)
            compile(code, f"<{g.group_name}>", "exec")

    def test_init_compiles(self, groups: list[ApiGroup]) -> None:
        code = generate_cli_init(groups)
        compile(code, "<cli_init>", "exec")
