"""Tests for asana_api_cli.session — resolve_body and resolve_workspace."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest

from asana_api_cli.session import (
    DEFAULT_WORKSPACE_ENV,
    resolve_body,
    resolve_workspace,
    runtime,
)


class TestResolveBodyInline:
    def test_plain_json_object(self) -> None:
        result = resolve_body('{"data": {"name": "Hello"}}')
        assert result == {"data": {"name": "Hello"}}

    def test_plain_json_array(self) -> None:
        result = resolve_body("[1, 2, 3]")
        assert result == [1, 2, 3]

    def test_invalid_json_exits(self) -> None:
        with pytest.raises(SystemExit):
            resolve_body("{bad json")


class TestResolveBodyFile:
    def test_reads_file(self, tmp_path: Path) -> None:
        body_file = tmp_path / "body.json"
        body_file.write_text('{"data": {"name": "from file"}}')
        result = resolve_body(f"@{body_file}")
        assert result == {"data": {"name": "from file"}}

    def test_missing_file_exits(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            resolve_body(f"@{tmp_path / 'nonexistent.json'}")

    def test_invalid_json_in_file_exits(self, tmp_path: Path) -> None:
        body_file = tmp_path / "bad.json"
        body_file.write_text("not json")
        with pytest.raises(SystemExit):
            resolve_body(f"@{body_file}")


class TestResolveBodyStdin:
    def test_reads_stdin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin", StringIO('{"data": {"name": "stdin"}}'))
        result = resolve_body("-")
        assert result == {"data": {"name": "stdin"}}

    def test_invalid_stdin_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin", StringIO("not json"))
        with pytest.raises(SystemExit):
            resolve_body("-")


# ---------------------------------------------------------------------------
# resolve_workspace
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_workspace_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure each test starts with no workspace defaults set."""
    monkeypatch.delenv(DEFAULT_WORKSPACE_ENV, raising=False)


class TestResolveWorkspaceExplicit:
    """Explicit --workspace value always wins."""

    def test_returns_explicit_value(self) -> None:
        assert resolve_workspace("111") == "111"

    def test_explicit_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(DEFAULT_WORKSPACE_ENV, "env_ws")
        assert resolve_workspace("explicit_ws") == "explicit_ws"


class TestResolveWorkspaceEnvFallback:
    """ASANA_DEFAULT_WORKSPACE env var is used only when required=True."""

    def test_falls_back_to_env_when_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(DEFAULT_WORKSPACE_ENV, "env_ws")
        assert resolve_workspace(None, required=True) == "env_ws"

    def test_no_fallback_when_optional(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(DEFAULT_WORKSPACE_ENV, "env_ws")
        assert resolve_workspace(None) is None


class TestResolveWorkspaceNoValue:
    """No workspace available anywhere."""

    def test_returns_none_when_optional(self) -> None:
        assert resolve_workspace(None) is None

    def test_exits_when_required(self) -> None:
        with pytest.raises(SystemExit):
            resolve_workspace(None, required=True)
