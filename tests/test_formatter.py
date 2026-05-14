"""Tests for asana_api_cli.formatter."""

from __future__ import annotations

import json
from typing import Any

import click
import pytest
from asana.rest import ApiException
from click.testing import CliRunner

from asana_api_cli.formatter import (
    _format_output,
    _handle_api_exception,
    _to_rows,
    formatted,
)
from asana_api_cli.session import runtime


# ---------------------------------------------------------------------------
# _to_rows
# ---------------------------------------------------------------------------


class TestToRows:
    def test_list_of_dicts(self) -> None:
        data = [{"a": 1}, {"a": 2}]
        assert _to_rows(data) == data

    def test_list_of_scalars(self) -> None:
        assert _to_rows([1, "two", 3]) == [{"value": 1}, {"value": "two"}, {"value": 3}]

    def test_empty_list(self) -> None:
        assert _to_rows([]) == []

    def test_single_dict(self) -> None:
        assert _to_rows({"x": 1}) == [{"x": 1}]

    def test_scalar_returns_none(self) -> None:
        assert _to_rows("hello") is None
        assert _to_rows(42) is None
        assert _to_rows(None) is None


# ---------------------------------------------------------------------------
# _format_output
# ---------------------------------------------------------------------------


class TestFormatOutputJson:
    def test_dict(self, capsys: pytest.CaptureFixture[str]) -> None:
        _format_output({"name": "Task"}, output_format="json", jq_query=None)
        out = capsys.readouterr().out
        assert json.loads(out) == {"name": "Task"}

    def test_list(self, capsys: pytest.CaptureFixture[str]) -> None:
        _format_output([1, 2], output_format="json", jq_query=None)
        assert json.loads(capsys.readouterr().out) == [1, 2]

    def test_none(self, capsys: pytest.CaptureFixture[str]) -> None:
        _format_output(None, output_format="json", jq_query=None)
        assert json.loads(capsys.readouterr().out) is None

    def test_unicode(self, capsys: pytest.CaptureFixture[str]) -> None:
        _format_output({"name": "日本語"}, output_format="json", jq_query=None)
        out = capsys.readouterr().out
        assert "日本語" in out  # ensure_ascii=False


class TestFormatOutputTable:
    def test_dict_as_table(self, capsys: pytest.CaptureFixture[str]) -> None:
        _format_output({"gid": "1", "name": "T"}, output_format="table", jq_query=None)
        out = capsys.readouterr().out
        assert "gid" in out
        assert "name" in out
        assert "T" in out

    def test_list_of_dicts(self, capsys: pytest.CaptureFixture[str]) -> None:
        data = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
        _format_output(data, output_format="table", jq_query=None)
        out = capsys.readouterr().out
        assert "a" in out and "b" in out

    def test_scalar_falls_through(self, capsys: pytest.CaptureFixture[str]) -> None:
        _format_output("plain text", output_format="table", jq_query=None)
        assert capsys.readouterr().out.strip() == "plain text"


class TestFormatOutputCsv:
    def test_dict_as_csv(self, capsys: pytest.CaptureFixture[str]) -> None:
        _format_output({"gid": "1", "name": "T"}, output_format="csv", jq_query=None)
        out = capsys.readouterr().out
        lines = out.strip().splitlines()
        assert lines[0] == "gid,name"
        assert lines[1] == "1,T"

    def test_list_of_dicts(self, capsys: pytest.CaptureFixture[str]) -> None:
        data = [{"x": "a", "y": "b"}, {"x": "c", "y": "d"}]
        _format_output(data, output_format="csv", jq_query=None)
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 3  # header + 2 rows

    def test_empty_list_produces_no_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        _format_output([], output_format="csv", jq_query=None)
        assert capsys.readouterr().out == ""

    def test_no_carriage_returns_in_csv_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        # csv module's default lineterminator is "\r\n", which would interact
        # with Windows text-mode stdout to produce "\r\r\n". We force "\n"
        # so the platform layer handles any translation.
        _format_output(
            [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}],
            output_format="csv",
            jq_query=None,
        )
        out = capsys.readouterr().out
        assert "\r" not in out

    def test_bom_off_by_default(self, capsys: pytest.CaptureFixture[str]) -> None:
        _format_output([{"a": "1"}], output_format="csv", jq_query=None)
        out = capsys.readouterr().out
        assert not out.startswith("\ufeff")

    def test_bom_prepended_when_requested(self, capsys: pytest.CaptureFixture[str]) -> None:
        _format_output([{"a": "1"}], output_format="csv", jq_query=None, csv_bom=True)
        out = capsys.readouterr().out
        assert out.startswith("\ufeff")
        # BOM must come exactly once, before the header.
        assert out.count("\ufeff") == 1

    def test_bom_ignored_for_non_csv(self, capsys: pytest.CaptureFixture[str]) -> None:
        # The flag is wired to every command via the decorator, so passing it
        # alongside --output json must not corrupt the JSON output.
        _format_output({"a": 1}, output_format="json", jq_query=None, csv_bom=True)
        out = capsys.readouterr().out
        assert not out.startswith("\ufeff")


class TestFormatOutputText:
    def test_string_scalar(self, capsys: pytest.CaptureFixture[str]) -> None:
        _format_output("hello", output_format="text", jq_query=None)
        assert capsys.readouterr().out.strip() == "hello"

    def test_int_scalar(self, capsys: pytest.CaptureFixture[str]) -> None:
        _format_output(42, output_format="text", jq_query=None)
        assert capsys.readouterr().out.strip() == "42"

    def test_bool_scalar(self, capsys: pytest.CaptureFixture[str]) -> None:
        _format_output(True, output_format="text", jq_query=None)
        assert capsys.readouterr().out.strip() == "True"

    def test_none(self, capsys: pytest.CaptureFixture[str]) -> None:
        _format_output(None, output_format="text", jq_query=None)
        assert capsys.readouterr().out.strip() == "None"

    def test_dict_tab_separated(self, capsys: pytest.CaptureFixture[str]) -> None:
        _format_output({"gid": "1", "name": "Task"}, output_format="text", jq_query=None)
        assert capsys.readouterr().out.strip() == "1\tTask"

    def test_list_of_dicts(self, capsys: pytest.CaptureFixture[str]) -> None:
        data = [{"gid": "1", "name": "A"}, {"gid": "2", "name": "B"}]
        _format_output(data, output_format="text", jq_query=None)
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines[0] == "1\tA"
        assert lines[1] == "2\tB"

    def test_list_of_scalars(self, capsys: pytest.CaptureFixture[str]) -> None:
        _format_output(["a", "b", "c"], output_format="text", jq_query=None)
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines == ["a", "b", "c"]

    def test_empty_list(self, capsys: pytest.CaptureFixture[str]) -> None:
        _format_output([], output_format="text", jq_query=None)
        assert capsys.readouterr().out == ""

    def test_with_jq_query(self, capsys: pytest.CaptureFixture[str]) -> None:
        _format_output({"data": {"gid": "123"}}, output_format="text", jq_query=".data.gid")
        assert capsys.readouterr().out.strip() == "123"


class TestFormatOutputJq:
    def test_jq_filter(self, capsys: pytest.CaptureFixture[str]) -> None:
        _format_output(
            {"data": {"name": "Hello"}},
            output_format="json",
            jq_query=".data.name",
        )
        assert json.loads(capsys.readouterr().out) == "Hello"

    def test_jq_array_filter(self, capsys: pytest.CaptureFixture[str]) -> None:
        _format_output(
            [{"gid": "1"}, {"gid": "2"}],
            output_format="json",
            jq_query=".[0].gid",
        )
        assert json.loads(capsys.readouterr().out) == "1"

    def test_invalid_jq_exits(self) -> None:
        with pytest.raises(SystemExit):
            _format_output({"a": 1}, output_format="json", jq_query=".[invalid")


# ---------------------------------------------------------------------------
# _handle_api_exception
# ---------------------------------------------------------------------------


class TestHandleApiException:
    def _make_exception(
        self,
        status: int = 400,
        body: str | bytes | None = None,
        reason: str = "Bad Request",
    ) -> ApiException:
        exc = ApiException(status=status, reason=reason)
        exc.body = body  # type: ignore[assignment]
        return exc

    def test_extracts_error_messages(self, capsys: pytest.CaptureFixture[str]) -> None:
        body = json.dumps({"errors": [{"message": "project not found"}]})
        with pytest.raises(SystemExit):
            _handle_api_exception(self._make_exception(status=404, body=body))
        err = capsys.readouterr().err
        assert "project not found" in err
        assert "404" in err

    def test_multiple_errors(self, capsys: pytest.CaptureFixture[str]) -> None:
        body = json.dumps({"errors": [{"message": "err1"}, {"message": "err2"}]})
        with pytest.raises(SystemExit):
            _handle_api_exception(self._make_exception(body=body))
        err = capsys.readouterr().err
        assert "err1" in err
        assert "err2" in err

    def test_bytes_body(self, capsys: pytest.CaptureFixture[str]) -> None:
        body = json.dumps({"errors": [{"message": "bytes err"}]}).encode()
        with pytest.raises(SystemExit):
            _handle_api_exception(self._make_exception(body=body))
        assert "bytes err" in capsys.readouterr().err

    def test_fallback_to_reason(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            _handle_api_exception(self._make_exception(reason="Forbidden", body=None))
        assert "Forbidden" in capsys.readouterr().err

    def test_unparseable_body_falls_back(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            _handle_api_exception(self._make_exception(reason="Oops", body="not json"))
        assert "Oops" in capsys.readouterr().err


class TestHandleNonJsonResponse:
    """Tests for non-JSON error responses (HTML, XML, plain text, etc.)."""

    def _make_exception(
        self,
        status: int = 502,
        body: str | bytes | None = None,
        reason: str = "Bad Gateway",
    ) -> ApiException:
        exc = ApiException(status=status, reason=reason)
        exc.body = body  # type: ignore[assignment]
        return exc

    def test_html_body_shows_hint(self, capsys: pytest.CaptureFixture[str]) -> None:
        html = "<html><body><h1>502 Bad Gateway</h1></body></html>"
        with pytest.raises(SystemExit):
            _handle_api_exception(self._make_exception(body=html))
        err = capsys.readouterr().err
        assert "non-JSON response" in err
        assert "--debug" in err
        # Raw body should NOT appear without debug mode
        assert html not in err

    def test_xml_body_shows_hint(self, capsys: pytest.CaptureFixture[str]) -> None:
        xml = '<?xml version="1.0"?><Error><Message>fail</Message></Error>'
        with pytest.raises(SystemExit):
            _handle_api_exception(self._make_exception(status=500, body=xml))
        err = capsys.readouterr().err
        assert "non-JSON response" in err

    def test_plain_text_body_shows_hint(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            _handle_api_exception(self._make_exception(body="upstream connect error"))
        err = capsys.readouterr().err
        assert "non-JSON response" in err

    def test_debug_dumps_raw_body(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "debug", True)
        html = "<html><body><h1>502 Bad Gateway</h1></body></html>"
        with pytest.raises(SystemExit):
            _handle_api_exception(self._make_exception(body=html))
        err = capsys.readouterr().err
        assert "--- raw response body ---" in err
        assert html in err
        assert "--- end of response body ---" in err
        monkeypatch.setattr(runtime, "debug", False)

    def test_debug_off_hides_raw_body(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "debug", False)
        html = "<html><body>error</body></html>"
        with pytest.raises(SystemExit):
            _handle_api_exception(self._make_exception(body=html))
        err = capsys.readouterr().err
        assert "--- raw response body ---" not in err
        assert "<html>" not in err

    def test_json_body_does_not_trigger_hint(self, capsys: pytest.CaptureFixture[str]) -> None:
        body = json.dumps({"errors": [{"message": "not found"}]})
        with pytest.raises(SystemExit):
            _handle_api_exception(self._make_exception(status=404, body=body))
        err = capsys.readouterr().err
        assert "non-JSON response" not in err

    def test_empty_body_does_not_trigger_hint(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            _handle_api_exception(self._make_exception(body=""))
        err = capsys.readouterr().err
        assert "non-JSON response" not in err

    def test_none_body_does_not_trigger_hint(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            _handle_api_exception(self._make_exception(body=None))
        err = capsys.readouterr().err
        assert "non-JSON response" not in err

    def test_bytes_html_body(self, capsys: pytest.CaptureFixture[str]) -> None:
        html_bytes = b"<html><body>nginx error</body></html>"
        with pytest.raises(SystemExit):
            _handle_api_exception(self._make_exception(body=html_bytes))
        err = capsys.readouterr().err
        assert "non-JSON response" in err


# ---------------------------------------------------------------------------
# @formatted decorator (integration)
# ---------------------------------------------------------------------------


class TestFormattedDecorator:
    def _make_cli(self, return_value: Any) -> click.Command:
        @click.command()
        @formatted
        def cmd() -> Any:
            """Test command."""
            return return_value

        return cmd

    def test_json_output(self) -> None:
        runner = CliRunner()
        result = runner.invoke(self._make_cli({"gid": "1"}))
        assert result.exit_code == 0
        assert json.loads(result.output) == {"gid": "1"}

    def test_table_output(self) -> None:
        runner = CliRunner()
        result = runner.invoke(self._make_cli({"gid": "1", "name": "T"}), ["--output", "table"])
        assert result.exit_code == 0
        assert "gid" in result.output
        assert "T" in result.output

    def test_csv_output(self) -> None:
        runner = CliRunner()
        result = runner.invoke(self._make_cli([{"a": "x"}]), ["--output", "csv"])
        assert result.exit_code == 0
        assert "a\n" in result.output
        assert "x\n" in result.output

    def test_csv_bom_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(self._make_cli([{"a": "x"}]), ["--output", "csv", "--csv-bom"])
        assert result.exit_code == 0
        assert result.output.startswith("\ufeff")

    def test_query_option(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            self._make_cli({"data": [1, 2, 3]}),
            ["--query", ".data | length"],
        )
        assert result.exit_code == 0
        assert json.loads(result.output) == 3

    def test_generator_collapsed_to_list(self) -> None:
        def gen():  # type: ignore[no-untyped-def]
            yield {"gid": "1"}
            yield {"gid": "2"}

        runner = CliRunner()
        result = runner.invoke(self._make_cli(gen()))
        assert result.exit_code == 0
        assert json.loads(result.output) == [{"gid": "1"}, {"gid": "2"}]

    def test_api_exception_handled(self) -> None:
        @click.command()
        @formatted
        def cmd() -> Any:
            """Raise API error."""
            raise ApiException(status=403, reason="Forbidden")

        runner = CliRunner()
        result = runner.invoke(cmd)
        assert result.exit_code != 0
        assert "Forbidden" in result.output  # CliRunner merges stderr
