"""Tests for asana_api_cli.formatter."""

from __future__ import annotations

import json
from typing import Any

import click
import pytest
from _cli_runner import full_output, make_runner
from asana.rest import ApiException

from asana_api_cli.formatter import (
    _echo_exception_only,
    _format_output,
    _handle_exception,
    _scalar_text,
    _to_rows,
    formatted,
)

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

    def test_mixed_list_returns_none(self) -> None:
        # A mixed dict/scalar list has no clean column layout; the caller
        # falls through to plain printing instead of crashing csv.DictWriter.
        assert _to_rows([{"a": 1}, "scalar"]) is None
        assert _to_rows([1, {"a": 1}]) is None


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


class TestFormatOutputJqEquivalentToPipe:
    """``--query EXPR`` is equivalent to ``... | jq 'EXPR'``: every value
    yielded by jq reaches the output, not just the first. Each output
    format renders the value stream naturally."""

    def test_json_multi_yield_emits_separate_documents(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``.data[]`` yields N values; ``--output json`` emits each as
        its own JSON document, matching external ``jq``'s stream output."""
        _format_output(
            {"data": [{"gid": "1"}, {"gid": "2"}, {"gid": "3"}]},
            output_format="json",
            jq_query=".data[]",
        )
        out = capsys.readouterr().out
        assert '"gid": "1"' in out
        assert '"gid": "2"' in out
        assert '"gid": "3"' in out

    def test_json_single_yield_emits_single_document(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``.data`` yields the array as one value; the document is just
        the array (not wrapped in another container)."""
        _format_output(
            {"data": [{"gid": "1"}, {"gid": "2"}]},
            output_format="json",
            jq_query=".data",
        )
        assert json.loads(capsys.readouterr().out) == [{"gid": "1"}, {"gid": "2"}]

    def test_json_zero_yield_emits_nothing(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A query that matches nothing produces no output and does not
        raise ``StopIteration``."""
        _format_output(
            {"data": [{"name": "a"}, {"name": "b"}]},
            output_format="json",
            jq_query='.data[] | select(.name == "nonexistent")',
        )
        assert capsys.readouterr().out == ""

    def test_table_multi_yield_collects_rows(self, capsys: pytest.CaptureFixture[str]) -> None:
        _format_output(
            {"data": [{"gid": "1", "name": "a"}, {"gid": "2", "name": "b"}]},
            output_format="table",
            jq_query=".data[]",
        )
        out = capsys.readouterr().out
        # Both rows must appear.
        assert "1" in out and "a" in out
        assert "2" in out and "b" in out

    def test_csv_multi_yield_collects_rows(self, capsys: pytest.CaptureFixture[str]) -> None:
        _format_output(
            {"data": [{"gid": "1"}, {"gid": "2"}, {"gid": "3"}]},
            output_format="csv",
            jq_query=".data[]",
        )
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines == ["gid", "1", "2", "3"]

    def test_text_multi_yield_lines(self, capsys: pytest.CaptureFixture[str]) -> None:
        _format_output(
            {"data": [{"name": "a"}, {"name": "b"}, {"name": "c"}]},
            output_format="text",
            jq_query=".data[] | .name",
        )
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines == ["a", "b", "c"]

    def test_table_scalar_yields_fall_through(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When all yields are scalars (no rowable structure), ``--output
        table`` falls through to plain printing per yield rather than
        crashing."""
        _format_output(
            {"data": [{"name": "a"}, {"name": "b"}]},
            output_format="table",
            jq_query=".data[] | .name",
        )
        out = capsys.readouterr().out
        assert "a" in out
        assert "b" in out


class TestCsvFieldnamesUnion:
    """``--output csv`` collects the union of keys across all rows so that
    rows with different optional fields all render correctly."""

    def test_extra_keys_on_later_rows_are_kept(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Row 0 has only ``gid``; row 1 also has ``due_on``. The header
        must include both columns and row 0 must show an empty
        ``due_on`` cell."""
        data = [{"gid": "1"}, {"gid": "2", "due_on": "2026-01-01"}]
        _format_output(data, output_format="csv", jq_query=None)
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines[0] == "gid,due_on"
        assert lines[1] == "1,"
        assert lines[2] == "2,2026-01-01"

    def test_keys_appear_in_first_seen_order(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Column order should follow first-appearance order across rows."""
        data = [
            {"a": "1", "b": "2"},
            {"a": "3", "c": "4"},  # c first seen here
            {"a": "5", "b": "6", "c": "7"},
        ]
        _format_output(data, output_format="csv", jq_query=None)
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines[0] == "a,b,c"


# ---------------------------------------------------------------------------
# _handle_exception (envelope schema + format dispatch)
# ---------------------------------------------------------------------------


class TestHandleApiException:
    """Cover the JSON envelope path with ``--exception-output json``.

    The default of ``--exception-output`` is ``none`` (stderr echo + exit
    1, no envelope); these tests opt into the envelope by passing
    ``exception_output="json"`` explicitly to ``_handle_exception`` — these are
    now the leaf command's per-call option values, not global runtime state.

    Schema: ``{exception, status, reason, body, headers}`` where ``body``
    is the UTF-8 decoded response *string* (or null). See
    ``docs/sdk-deviations.md``.
    """

    def _make_exception(
        self,
        status: int | None = 412,
        reason: str | None = "Precondition Failed",
        body: str | bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> ApiException:
        exc = ApiException(status=status, reason=reason)
        exc.body = body  # type: ignore[assignment]
        exc.headers = headers  # type: ignore[assignment]
        return exc

    def _envelope(self, capsys: pytest.CaptureFixture[str]) -> Any:
        return json.loads(capsys.readouterr().out)

    def test_string_body_preserved(self, capsys: pytest.CaptureFixture[str]) -> None:
        body = json.dumps({"sync": "abc", "errors": [{"message": "Sync token invalid"}]})
        exc = self._make_exception(body=body, headers={"X-Asana-Request-Id": "r1"})
        with pytest.raises(SystemExit) as exc_info:
            _handle_exception(exc, exception_output="json", exception_query=None)
        assert exc_info.value.code == 3
        env = self._envelope(capsys)
        assert env["exception"] == "asana.rest.ApiException"
        assert env["status"] == 412
        assert env["reason"] == "Precondition Failed"
        assert env["body"] == body
        assert env["headers"] == {"X-Asana-Request-Id": "r1"}

    def test_non_json_body_preserved_verbatim(self, capsys: pytest.CaptureFixture[str]) -> None:
        html = "<html><body>502</body></html>"
        exc = self._make_exception(status=502, reason="Bad Gateway", body=html)
        with pytest.raises(SystemExit):
            _handle_exception(exc, exception_output="json", exception_query=None)
        env = self._envelope(capsys)
        assert env["status"] == 502
        assert env["body"] == html

    def test_bytes_body_decoded(self, capsys: pytest.CaptureFixture[str]) -> None:
        body_bytes = json.dumps({"errors": []}).encode("utf-8")
        exc = self._make_exception(body=body_bytes)
        with pytest.raises(SystemExit):
            _handle_exception(exc, exception_output="json", exception_query=None)
        env = self._envelope(capsys)
        assert env["body"] == body_bytes.decode("utf-8")

    def test_undecodable_bytes_use_replace(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Production rarely sees this, but the policy is errors="replace"
        # so the envelope still emits a string rather than failing.
        exc = self._make_exception(body=b"\x80\x81\x82")
        with pytest.raises(SystemExit):
            _handle_exception(exc, exception_output="json", exception_query=None)
        env = self._envelope(capsys)
        assert isinstance(env["body"], str)

    def test_none_body_emits_null(self, capsys: pytest.CaptureFixture[str]) -> None:
        # SDK's SSLError-wrapping path raises ApiException(status=0,
        # reason=..., body=None, headers=None).
        exc = self._make_exception(status=0, reason="SSLError\n...", body=None, headers=None)
        with pytest.raises(SystemExit) as exc_info:
            _handle_exception(exc, exception_output="json", exception_query=None)
        assert exc_info.value.code == 3
        env = self._envelope(capsys)
        assert env["status"] == 0
        assert env["body"] is None
        assert env["headers"] is None

    def test_exception_field_is_fqdn_for_subclass(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A subclass surfaces its own ``module.qualname`` — SDK users
        catch the exact class via the printed import path."""

        class CustomApiException(ApiException):
            pass

        exc = CustomApiException(status=500, reason="Boom")
        with pytest.raises(SystemExit):
            _handle_exception(exc, exception_output="json", exception_query=None)
        env = self._envelope(capsys)
        # Module is this test file; qualname includes the nested class
        # path. Asserting the suffix is robust to test-runner module
        # naming differences.
        assert env["exception"].endswith(".CustomApiException")
        assert "." in env["exception"]  # FQDN, not bare name


class TestHandleApiExceptionFormats:
    """``--exception-output`` reuses ``_format_output``; cover the four formats."""

    def _make_exception(self) -> ApiException:
        exc = ApiException(status=412, reason="Precondition Failed")
        exc.body = '{"sync":"abc","errors":[{"message":"Sync token invalid"}]}'  # type: ignore[assignment]
        exc.headers = {"X-Asana-Request-Id": "r1"}  # type: ignore[assignment]
        return exc

    def test_text_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            _handle_exception(self._make_exception(), exception_output="text", exception_query=None)
        # _print_text for a dict joins values by tab.
        out = capsys.readouterr().out.rstrip("\n")
        cells = out.split("\t")
        assert cells[0] == "asana.rest.ApiException"
        assert cells[1] == "412"
        assert cells[2] == "Precondition Failed"
        # headers (dict) is rendered via _scalar_text → JSON, not Python repr
        assert cells[4].startswith("{") and '"X-Asana-Request-Id"' in cells[4]

    def test_csv_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            _handle_exception(self._make_exception(), exception_output="csv", exception_query=None)
        out = capsys.readouterr().out.strip().splitlines()
        assert out[0] == "exception,status,reason,body,headers"
        # data row begins with FQDN exception + status etc.; headers cell is JSON
        assert out[1].startswith("asana.rest.ApiException,412,Precondition Failed,")
        assert '"X-Asana-Request-Id"' in out[1]

    def test_table_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            _handle_exception(
                self._make_exception(), exception_output="table", exception_query=None
            )
        out = capsys.readouterr().out
        # tabulate's "simple" format puts column headers on line 0.
        assert "exception" in out and "status" in out and "headers" in out
        assert "ApiException" in out

    def test_json_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            _handle_exception(self._make_exception(), exception_output="json", exception_query=None)
        env = json.loads(capsys.readouterr().out)
        assert env["exception"] == "asana.rest.ApiException"
        assert env["status"] == 412


class TestHandleApiExceptionQuery:
    """``--exception-query`` applies jq to the envelope; output format follows
    ``--exception-output``. Both arrive as explicit ``_handle_exception`` kwargs
    (the leaf command's per-call option values)."""

    def _make_exception(self) -> ApiException:
        exc = ApiException(status=412, reason="Precondition Failed")
        exc.body = '{"sync":"new-token","errors":[]}'  # type: ignore[assignment]
        return exc

    def test_query_filter_default_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc_info:
            _handle_exception(
                self._make_exception(), exception_output="json", exception_query=".status"
            )
        assert exc_info.value.code == 3
        assert capsys.readouterr().out.strip() == "412"

    def test_query_with_fromjson_text_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The events-polling idiom: fromjson on body to navigate parsed
        fields, then output raw text for shell consumption."""
        with pytest.raises(SystemExit):
            _handle_exception(
                self._make_exception(),
                exception_output="text",
                exception_query=".body | fromjson | .sync",
            )
        # text format outputs the bare scalar (no JSON quotes)
        assert capsys.readouterr().out.strip() == "new-token"

    def test_query_invalid_jq_exits_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc_info:
            _handle_exception(
                self._make_exception(), exception_output="json", exception_query="bad(("
            )
        # exit 2 = user-input error (jq syntax). docs/usage.md.
        assert exc_info.value.code == 2
        # The error message ("Invalid jq expression: ...") is the one
        # consistently-stderr path: a user-input error that *isn't* the
        # rendered envelope.
        assert "Invalid jq expression" in capsys.readouterr().err


class TestScalarText:
    """``_scalar_text`` renders nested containers as JSON, scalars as str()."""

    def test_scalars_unchanged(self) -> None:
        assert _scalar_text("hello") == "hello"
        assert _scalar_text(42) == "42"
        assert _scalar_text(True) == "True"
        assert _scalar_text(None) == "None"

    def test_dict_as_json(self) -> None:
        assert _scalar_text({"a": 1, "b": "x"}) == '{"a": 1, "b": "x"}'

    def test_list_as_json(self) -> None:
        assert _scalar_text([1, "two", None]) == '[1, "two", null]'

    def test_nested_dict_uses_double_quotes_not_python_repr(self) -> None:
        # str(dict) gives `{'a': 'b'}` (Python repr); _scalar_text gives
        # JSON `{"a": "b"}` so the cell is shell-tool friendly.
        out = _scalar_text({"a": "b"})
        assert "'" not in out
        assert '"a"' in out and '"b"' in out

    def test_unicode_preserved(self) -> None:
        assert _scalar_text({"name": "日本語"}) == '{"name": "日本語"}'

    def test_unserializable_falls_back_to_str(self) -> None:
        class NotJson:
            def __repr__(self) -> str:
                return "<NotJson>"

        assert _scalar_text(NotJson()) == "<NotJson>"


class TestHandleNonApiException:
    """Non-ApiException exceptions (urllib3 connection errors, generic
    Python errors) collapse to ``{exception, reason}`` — no HTTP context
    fields, since none apply. ``exception_output="json"`` is passed explicitly
    (the leaf command's per-call option value)."""

    def _envelope(self, capsys: pytest.CaptureFixture[str]) -> Any:
        return json.loads(capsys.readouterr().out)

    def test_urllib3_max_retry_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        from urllib3.exceptions import MaxRetryError

        exc = MaxRetryError(
            pool=None,  # type: ignore[arg-type]
            url="https://x.invalid/y",
            reason=Exception("connection refused"),
        )
        with pytest.raises(SystemExit) as exc_info:
            _handle_exception(exc, exception_output="json", exception_query=None)
        assert exc_info.value.code == 3
        env = self._envelope(capsys)
        assert env["exception"] == "urllib3.exceptions.MaxRetryError"
        assert "reason" in env and env["reason"]
        # status/body/headers are absent (not null) because the exception
        # carries no HTTP response context.
        assert "status" not in env
        assert "body" not in env
        assert "headers" not in env

    def test_builtin_exception(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc_info:
            _handle_exception(RuntimeError("oops"), exception_output="json", exception_query=None)
        assert exc_info.value.code == 3
        env = self._envelope(capsys)
        assert env["exception"] == "builtins.RuntimeError"
        assert env["reason"] == "oops"
        assert set(env.keys()) == {"exception", "reason"}


class TestEchoExceptionOnly:
    """``_echo_exception_only`` writes the exception to **stderr**
    without traceback frames.

    Format: ``traceback.format_exception_only(type(e), e)`` — the
    qualified class name followed by the exception's ``__str__``,
    matching what Python's default top-level handler prints, but
    without the traceback frames. The output is multi-line whenever
    the exception's ``__str__`` is (notably ``ApiException``, which
    embeds status / reason / headers / body across separate lines).

    Called by the ``formatted`` decorator before either branch
    (``--exception-output=none`` exit-1 or the envelope formats exit-3),
    so the stderr format is identical regardless of ``exception_output``.
    """

    def test_api_exception_includes_full_http_context(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exc = ApiException(status=412, reason="Precondition Failed")
        exc.body = '{"sync":"new-token"}'  # type: ignore[assignment]
        exc.headers = {"X-Asana-Request-Id": "r1"}  # type: ignore[assignment]
        _echo_exception_only(exc)
        captured = capsys.readouterr()
        # ApiException.__str__ embeds status / reason / headers / body.
        assert "ApiException" in captured.err
        assert "(412)" in captured.err
        assert "Precondition Failed" in captured.err
        assert '{"sync":"new-token"}' in captured.err
        # Function writes only to stderr.
        assert captured.out == ""

    def test_generic_exception_no_traceback(self, capsys: pytest.CaptureFixture[str]) -> None:
        _echo_exception_only(RuntimeError("boom"))
        err = capsys.readouterr().err
        # ``format_exception_only`` for a builtin yields "RuntimeError:
        # boom\n" (no module prefix on the short form). The exact line
        # is stable across Python versions.
        assert "RuntimeError" in err
        assert "boom" in err
        # No traceback frames.
        assert "Traceback" not in err
        assert ".py" not in err  # no source-line references


class TestFormattedDecoratorStderrEcho:
    """Integration: the ``formatted`` decorator runs ``_echo_exception_only``
    before either exit path, so ``--exception-query`` cannot mask the raw
    exception by stripping it from the stdout envelope."""

    def test_exception_query_cannot_hide_raw_exception(self) -> None:
        @click.command()
        @formatted
        def cmd() -> Any:
            exc = ApiException(status=500, reason="Internal Server Error")
            exc.body = "<html>oops</html>"  # type: ignore[assignment]
            raise exc

        result = make_runner().invoke(
            cmd, ["--exception-output", "json", "--exception-query", ".status"]
        )
        assert result.exit_code == 3
        # Stdout: only ``.status`` after the jq filter.
        assert result.stdout.strip() == "500"
        # Stderr: full Python-style exception including body / reason —
        # the raw exception survived the jq filter applied to stdout.
        assert "500" in result.stderr
        assert "Internal Server Error" in result.stderr
        assert "<html>oops</html>" in result.stderr


class TestFormattedDecoratorReraisesClickExceptions:
    """ClickException / Abort / Exit raised from inside the callback must
    bubble up to Click's own handler — *not* be wrapped in an envelope."""

    def _run(self, raise_exc: Exception) -> Any:
        @click.command()
        @formatted
        def cmd() -> Any:
            raise raise_exc

        return make_runner().invoke(cmd)

    def test_click_usage_error_propagates(self) -> None:
        result = self._run(click.UsageError("bad usage"))
        # Click's UsageError exits 2 with its own formatting; the envelope
        # is *not* emitted (no JSON, no exit 3).
        assert result.exit_code == 2
        assert "bad usage" in full_output(result)

    def test_click_abort_propagates(self) -> None:
        result = self._run(click.Abort())
        # Abort is Click's aborted-command sentinel; CliRunner translates
        # it to exit 1 with the literal "Aborted!" output. Asserting both
        # the exit code AND the absence of an envelope guards against
        # the regression "decorator swallows Abort and returns normally"
        # which a plain ``!= 3`` would not catch.
        assert result.exit_code == 1
        out = full_output(result)
        assert "Aborted" in out
        assert "exception" not in out  # envelope must NOT be emitted

    def test_click_exit_propagates(self) -> None:
        result = self._run(click.exceptions.Exit(7))
        assert result.exit_code == 7


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
        runner = make_runner()
        result = runner.invoke(self._make_cli({"gid": "1"}))
        assert result.exit_code == 0
        assert json.loads(full_output(result)) == {"gid": "1"}

    def test_table_output(self) -> None:
        runner = make_runner()
        result = runner.invoke(self._make_cli({"gid": "1", "name": "T"}), ["--output", "table"])
        assert result.exit_code == 0
        assert "gid" in full_output(result)
        assert "T" in full_output(result)

    def test_csv_output(self) -> None:
        runner = make_runner()
        result = runner.invoke(self._make_cli([{"a": "x"}]), ["--output", "csv"])
        assert result.exit_code == 0
        assert "a\n" in full_output(result)
        assert "x\n" in full_output(result)

    def test_csv_bom_flag(self) -> None:
        runner = make_runner()
        result = runner.invoke(self._make_cli([{"a": "x"}]), ["--output", "csv", "--csv-bom"])
        assert result.exit_code == 0
        assert full_output(result).startswith("\ufeff")

    def test_query_option(self) -> None:
        runner = make_runner()
        result = runner.invoke(
            self._make_cli({"data": [1, 2, 3]}),
            ["--query", ".data | length"],
        )
        assert result.exit_code == 0
        assert json.loads(full_output(result)) == 3

    # ``test_generator_collapsed_to_list`` (pre-v3.1) verified that the
    # ``@formatted`` decorator would consume a raw generator return value
    # into a list before rendering. That fallback was removed in v3.1:
    # iterator consumption now happens upstream in ``cli.py:_make_command``
    # (Layer B inside the session context, gated by
    # ``isinstance(result, collections.abc.Iterator)``). Letting iterators
    # leak past the session context would re-introduce the
    # ``Authorization``-in-``--debug`` leak risk the upstream gate exists
    # to prevent. Test removed.

    def test_api_exception_envelope(self) -> None:
        """With --exception-output json, ApiException is rendered as an
        envelope on stdout (exit 3) and *also* echoed to stderr via
        ``_echo_exception_only`` — the same stderr format used by the
        ``none`` branch. Regression guard against the echo being lost
        from the envelope path."""

        @click.command()
        @formatted
        def cmd() -> Any:
            """Raise API error."""
            raise ApiException(status=403, reason="Forbidden")

        runner = make_runner()
        result = runner.invoke(cmd, ["--exception-output", "json"])
        assert result.exit_code == 3
        env = json.loads(result.stdout)
        assert env["exception"] == "asana.rest.ApiException"
        assert env["reason"] == "Forbidden"
        # Stderr echo also runs on the envelope branch — same format
        # as the ``none`` branch.
        assert "asana.rest.ApiException" in result.stderr
        assert "Forbidden" in result.stderr
        assert "Traceback (most recent call last)" not in result.stderr


class TestNoneDefault:
    """``--exception-output none`` (the default) catches the exception,
    writes ``traceback.format_exception_only`` (qualified class name +
    ``__str__``, *no* traceback frames) to stderr, and exits 1. For
    ``ApiException`` the stderr output is multi-line (status, reason,
    headers, body), so the response payload (e.g. the 412 sync-token
    body in events polling) stays visible without traceback noise."""

    def test_api_exception_renders_without_traceback(self) -> None:
        @click.command()
        @formatted
        def cmd() -> Any:
            raise ApiException(status=403, reason="Forbidden")

        runner = make_runner()
        result = runner.invoke(cmd)
        assert result.exit_code == 1
        # Nothing rendered to stdout — no envelope.
        assert result.stdout == ""
        # Stderr carries the qualified exception name + its __str__,
        # not a traceback.
        assert "asana.rest.ApiException" in result.stderr
        assert "(403)" in result.stderr
        assert "Forbidden" in result.stderr
        assert "Traceback (most recent call last)" not in result.stderr

    def test_generic_exception_renders_without_traceback(self) -> None:
        @click.command()
        @formatted
        def cmd() -> Any:
            raise RuntimeError("boom")

        runner = make_runner()
        result = runner.invoke(cmd)
        assert result.exit_code == 1
        assert result.stdout == ""
        assert "RuntimeError: boom" in result.stderr
        assert "Traceback (most recent call last)" not in result.stderr


class TestFormatOutputExitCodes:
    """``--query`` invalid jq must exit 2 (user-input), not 1 (the SDK
    call exception path).

    Anchors the exit-code policy: ``_format_output`` never produces
    exit 1 by itself — exit 1 is reserved for the ``--exception-output=none``
    SDK exception path in :func:`formatted` (see ``docs/usage.md``).
    """

    def test_invalid_jq_exits_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc_info:
            _format_output({"data": [1]}, output_format="json", jq_query="bad((")
        assert exc_info.value.code == 2
        assert "Invalid jq expression" in capsys.readouterr().err


class TestFormatOutputNone:
    """``--output none`` suppresses the success payload but still runs the
    ``--query`` pass, so value-level validation is independent of the chosen
    format. Symmetric with ``--exception-output none``."""

    def test_no_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        _format_output({"gid": "1", "name": "T"}, output_format="none", jq_query=None)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_no_output_with_list(self, capsys: pytest.CaptureFixture[str]) -> None:
        _format_output([{"a": 1}, {"a": 2}], output_format="none", jq_query=None)
        assert capsys.readouterr().out == ""

    def test_invalid_jq_still_exits_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Even with output silenced, an invalid jq expression must surface
        as exit 2 — otherwise scripts that flip ``--output`` to ``none`` would
        silently lose jq-bug detection."""
        with pytest.raises(SystemExit) as exc_info:
            _format_output({"data": [1]}, output_format="none", jq_query="bad((")
        assert exc_info.value.code == 2
        assert "Invalid jq expression" in capsys.readouterr().err

    def test_jq_runtime_error_still_exits_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        """jq runtime errors (e.g. type-mismatch against the actual input)
        also surface — not just syntax errors. ``.foo`` on a non-object
        raises a jq runtime error which jqlib re-raises as ValueError."""
        with pytest.raises(SystemExit) as exc_info:
            _format_output(42, output_format="none", jq_query=".foo")
        assert exc_info.value.code == 2
        assert "Invalid jq expression" in capsys.readouterr().err

    def test_csv_bom_ignored(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``--csv-bom`` is silently ignored when output is suppressed,
        same as for ``--output json``."""
        _format_output({"a": 1}, output_format="none", jq_query=None, csv_bom=True)
        assert capsys.readouterr().out == ""
