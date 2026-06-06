"""Tests for the ``--generate-python`` code-generation mode (Phase 1 plumbing).

Phase 1 wires the mode without yet emitting faithful code: the
``--generate-python`` global flag sets ``runtime.generate_python``;
``cli.py:inner_callback`` then returns the session-free ``CallPlan`` instead of
executing it, and ``formatter.py:formatted`` renders it via
``codegen.render_python`` (a stub for now) instead of formatting an SDK result.

These tests pin that wiring — no SDK call, no token, formatter bypassed, the
flag honored at any level of the tree, and the build-time input validation that
still runs in generate mode — against the stub renderer. The faithful generated
code is covered in later phases.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import asana
import click
import pytest
from _cli_runner import full_output, make_runner

from asana_api_cli.cli import (
    _enumerate_api_classes,
    _make_command,
    _operations_for,
    main,
)

# Runtime isolation between tests is provided by the autouse ``_reset_runtime``
# fixture in ``tests/conftest.py`` (it snapshots/restores every ``_Runtime``
# field, ``generate_python`` included). Env vars are controlled per test.


def _build_command(api_cls_name: str, method_name: str) -> click.Command:
    """Build the real CLI command bound to ``asana.<api_cls_name>.<method_name>``."""
    api_cls = next(c for c in _enumerate_api_classes() if c.__name__ == api_cls_name)
    op = next(o for o in _operations_for(api_cls) if o.method_name == method_name)
    return _make_command(api_cls, op)


def _spy(monkeypatch: pytest.MonkeyPatch, api_cls_name: str, method_name: str) -> MagicMock:
    """Replace an SDK method with a recorder so a stray call is observable."""
    spy = MagicMock(return_value=None)

    def _stub(self: object, *args: object, **kwargs: object) -> object:
        return spy(*args, **kwargs)

    monkeypatch.setattr(getattr(asana, api_cls_name), method_name, _stub)
    return spy


class TestGenerateModeBypassesExecution:
    def test_skips_sdk_and_needs_no_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No token in the environment: execute mode would exit 2 in
        # ``AsanaSession.from_env``. Generate mode must still succeed, proving it
        # never opens a session.
        monkeypatch.delenv("ASANA_ACCESS_TOKEN", raising=False)
        cmd = _build_command("TasksApi", "get_tasks")
        spy = _spy(monkeypatch, "TasksApi", "get_tasks")

        result = make_runner().invoke(cmd, ["--generate-python"])

        assert result.exit_code == 0, full_output(result)
        assert spy.call_count == 0, "the SDK method must not be invoked in generate mode"
        # The rendered code goes to stdout (so ``> script.py`` captures only it).
        assert "TasksApi.get_tasks" in result.stdout
        assert result.stdout.lstrip().startswith("#")

    def test_execute_mode_still_calls_sdk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The contrast arm: without the flag the same command opens a session and
        # calls the SDK, formatting the result. Guards that the generate branch
        # did not divert the normal path.
        monkeypatch.setenv("ASANA_ACCESS_TOKEN", "test-token")
        cmd = _build_command("TasksApi", "get_tasks")
        spy = _spy(monkeypatch, "TasksApi", "get_tasks")

        result = make_runner().invoke(cmd, [])

        assert result.exit_code == 0, full_output(result)
        # The load-bearing claim: the same command, without the flag, reaches the
        # SDK exactly once (vs. zero times in generate mode above).
        assert spy.call_count == 1

    def test_bypasses_formatter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ``--output``/``--query``/``--csv-bom`` are transcribed into the emitted
        # code, never applied to an SDK result, so the formatter is bypassed
        # entirely. The output is the rendered code regardless of ``--output``.
        monkeypatch.delenv("ASANA_ACCESS_TOKEN", raising=False)
        cmd = _build_command("TasksApi", "get_tasks")
        spy = _spy(monkeypatch, "TasksApi", "get_tasks")

        result = make_runner().invoke(cmd, ["--generate-python", "--output", "csv"])

        assert result.exit_code == 0, full_output(result)
        assert spy.call_count == 0, "the SDK must not be invoked even with --output set"
        # Rendered code, not CSV of an SDK result.
        assert result.stdout.lstrip().startswith("#")
        assert "TasksApi.get_tasks" in result.stdout


class TestGenerateModeIsGlobal:
    @pytest.mark.parametrize(
        "argv",
        [
            ["--generate-python", "tasks", "get-tasks"],
            ["tasks", "get-tasks", "--generate-python"],
        ],
    )
    def test_flag_honored_before_or_after_command(
        self, monkeypatch: pytest.MonkeyPatch, argv: list[str]
    ) -> None:
        # ``--generate-python`` is a global option appended at every level, so it
        # works at the root and on the leaf — like ``--debug``. Driven through the
        # real ``main`` tree with no token: a tokenless exit 0 proves no session
        # was opened (execute mode would exit 2 in ``AsanaSession.from_env``), so
        # the flag was honored at this position. (No SDK spy here: ``main``
        # introspects the real method lazily at invoke time, and patching it
        # first would rebuild the command from the stub's signature.)
        monkeypatch.delenv("ASANA_ACCESS_TOKEN", raising=False)

        result = make_runner().invoke(main, argv)

        assert result.exit_code == 0, full_output(result)
        assert "TasksApi.get_tasks" in result.stdout


class TestGenerateModeStillValidatesInput:
    """Build-time input validation in ``build_call_plan`` runs in both modes."""

    def test_required_workspace_omitted_exits_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ``search-tasks-for-workspace`` has a required (positional) workspace
        # enforced by ``resolve_workspace`` in ``build_call_plan``, not by click.
        # Omitting it with no ``ASANA_DEFAULT_WORKSPACE`` is exit 2 even in
        # generate mode.
        monkeypatch.delenv("ASANA_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("ASANA_DEFAULT_WORKSPACE", raising=False)
        cmd = _build_command("TasksApi", "search_tasks_for_workspace")

        result = make_runner().invoke(cmd, ["--generate-python"])

        assert result.exit_code == 2, full_output(result)

    def test_malformed_body_literal_exits_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A malformed ``--body`` JSON literal fails resolution (exit 2) before any
        # rendering. (``@file`` / stdin bodies are a later phase and not read
        # here.)
        monkeypatch.delenv("ASANA_ACCESS_TOKEN", raising=False)
        cmd = _build_command("TasksApi", "create_task")

        result = make_runner().invoke(cmd, ["--generate-python", "--body", "{not valid json"])

        assert result.exit_code == 2, full_output(result)
