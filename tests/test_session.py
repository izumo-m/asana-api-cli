"""Tests for asana_api_cli.session — resolve_body helper."""
from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest

from asana_api_cli.session import resolve_body


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
