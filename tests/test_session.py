"""Tests for asana_api_cli.session — resolve_body, resolve_workspace, and debug redaction."""

from __future__ import annotations

import http.client
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from asana_api_cli.session import (
    DEFAULT_WORKSPACE_ENV,
    HttpClientPrintRedactor,
    _AUTH_HEADER_RE,
    _default_mask_token,
    resolve_body,
    resolve_workspace,
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


# ---------------------------------------------------------------------------
# _default_mask_token
# ---------------------------------------------------------------------------


class TestDefaultMaskToken:
    """``_default_mask_token`` reveals the last few characters when the
    token is long enough that the leak ratio stays bounded; otherwise
    falls back to a complete redaction."""

    def test_long_token_reveals_last_six(self) -> None:
        # 40-char token, comfortably above the threshold.
        assert _default_mask_token("a" * 34 + "tail99") == "...tail99"

    def test_token_at_threshold_partial(self) -> None:
        # len == 16 (the threshold) → partial reveal.
        assert _default_mask_token("0123456789abcdef") == "...abcdef"

    def test_short_token_full_redact(self) -> None:
        # len < 16 → full redact, so leak ratio stays at most 37.5%.
        assert _default_mask_token("abc") == "<REDACTED>"
        assert _default_mask_token("0123456789abcde") == "<REDACTED>"  # 15 chars
        assert _default_mask_token("") == "<REDACTED>"

    def test_distinguishes_two_long_tokens(self) -> None:
        # The whole point of partial reveal: a user juggling two
        # accounts can tell which token a debug line refers to.
        t1 = "2/1200000000000001:" + "A" * 30 + "abc123"
        t2 = "2/1200000099999999:" + "B" * 30 + "xyz789"
        assert _default_mask_token(t1) != _default_mask_token(t2)


# ---------------------------------------------------------------------------
# Authorization header regex
# ---------------------------------------------------------------------------


class TestAuthHeaderRegex:
    """``_AUTH_HEADER_RE`` captures the Authorization scheme prefix and
    the opaque token value across raw and ``repr()`` forms.

    The character class is intentionally not tied to today's Asana PAT
    or OAuth shape — Asana's developer documentation declares that
    token formats may change without notice."""

    def test_captures_bearer_in_raw_text(self) -> None:
        m = _AUTH_HEADER_RE.search("Authorization: Bearer 2/123456/789:abcdef\r\nNext: ok\r\n")
        assert m is not None
        assert m.group(1) == "Authorization: Bearer"
        assert m.group(2) == "2/123456/789:abcdef"

    def test_captures_bearer_in_http_client_repr(self) -> None:
        # http.client's debug print emits ``repr(bytes)``, so the header
        # delimiter appears as the literal ``\r\n`` (backslash sequence).
        # The match must stop at the leading backslash, NOT swallow the
        # next header name.
        raw = (
            r"send: b'GET /api/1.0/tasks HTTP/1.1\r\n"
            r"Authorization: Bearer 2/abcdef:0123\r\n"
            r"Content-Type: application/json\r\n'"
        )
        m = _AUTH_HEADER_RE.search(raw)
        assert m is not None
        assert m.group(2) == "2/abcdef:0123"

    def test_captures_basic_auth(self) -> None:
        m = _AUTH_HEADER_RE.search("Authorization: Basic dXNlcjpwYXNz\r\n")
        assert m is not None
        assert m.group(1) == "Authorization: Basic"
        assert m.group(2) == "dXNlcjpwYXNz"

    def test_captures_token_with_unusual_characters(self) -> None:
        # The character class accepts any non-whitespace, non-backslash run.
        raw = r"Authorization: Bearer ~!@#$%^&*()_+{}|<>?,abc\r\nNext: x"
        m = _AUTH_HEADER_RE.search(raw)
        assert m is not None
        assert m.group(2) == "~!@#$%^&*()_+{}|<>?,abc"

    def test_no_match_for_bearer_word_in_payload(self) -> None:
        # The word "Bearer" alone in a payload must not trigger redaction.
        payload = '{"data": {"name": "Bearer is just a word here"}}'
        assert _AUTH_HEADER_RE.search(payload) is None


# ---------------------------------------------------------------------------
# HttpClientPrintRedactor
# ---------------------------------------------------------------------------


@pytest.fixture
def _clean_http_client_print() -> Iterator[None]:
    """Snapshot and restore ``http.client.print`` and ``debuglevel``."""
    saved_print = http.client.__dict__.get("print")
    saved_debuglevel = http.client.HTTPConnection.debuglevel
    try:
        yield
    finally:
        if saved_print is None:
            http.client.__dict__.pop("print", None)
        else:
            http.client.print = saved_print  # pyright: ignore[reportAttributeAccessIssue]
        http.client.HTTPConnection.debuglevel = saved_debuglevel


class TestHttpClientPrintRedactor:
    """``HttpClientPrintRedactor`` patches ``http.client.print`` without
    touching ``sys.stdout`` / ``sys.stderr``, and uninstalls cleanly on
    context-manager exit."""

    def test_context_manager_installs_and_uninstalls(self, _clean_http_client_print: None) -> None:
        # http.client.print is normally absent from the module dict —
        # it falls through to builtins.
        http.client.__dict__.pop("print", None)
        with HttpClientPrintRedactor():
            installed = http.client.__dict__.get("print")
            assert installed is not None
            assert getattr(installed, "_asana_cli_redactor", False)
        assert "print" not in http.client.__dict__

    def test_explicit_install_uninstall(self, _clean_http_client_print: None) -> None:
        http.client.__dict__.pop("print", None)
        r = HttpClientPrintRedactor()
        r.install()
        installed = http.client.__dict__.get("print")
        assert installed is not None
        assert getattr(installed, "_asana_cli_redactor", False)
        r.uninstall()
        assert "print" not in http.client.__dict__

    def test_uninstall_safe_to_call_twice(self, _clean_http_client_print: None) -> None:
        r = HttpClientPrintRedactor()
        r.install()
        r.uninstall()
        r.uninstall()  # no-op, must not raise

    def test_nested_redactors_share_one_install(self, _clean_http_client_print: None) -> None:
        """The outer redactor owns the install; nested redactors no-op
        both on install (marker detected) and uninstall (not top of
        stack)."""
        http.client.__dict__.pop("print", None)
        with HttpClientPrintRedactor():
            first_wrapper = http.client.__dict__["print"]
            with HttpClientPrintRedactor():
                # Second install was a no-op; the wrapper is unchanged.
                assert http.client.__dict__["print"] is first_wrapper
            # Inner uninstall did not restore — outer still owns.
            assert http.client.__dict__["print"] is first_wrapper
        # Outer uninstall restored.
        assert "print" not in http.client.__dict__

    def test_chains_through_existing_patch(self, _clean_http_client_print: None) -> None:
        """If something else patched ``http.client.print`` first, our
        install must wrap it (not clobber it).

        Preserves layered redaction: if a future SDK installs its own
        masking print, ours runs on the way in and theirs runs as the
        inner.
        """
        seen: list[tuple[Any, ...]] = []

        def _pre_existing(*args: Any, **kwargs: Any) -> None:
            seen.append(args)

        http.client.print = _pre_existing  # pyright: ignore[reportAttributeAccessIssue]
        with HttpClientPrintRedactor():
            http.client.print(  # type: ignore[attr-defined]
                "send:",
                r"b'GET / HTTP/1.1\r\nAuthorization: Bearer 1234567890ABCDEFGH\r\n'",
            )
        assert len(seen) == 1
        joined = " ".join(str(a) for a in seen[0])
        assert "1234567890ABCDEFGH" not in joined
        # Partial reveal: only the last six characters survive.
        assert "...CDEFGH" in joined

    def test_uninstall_noop_when_wrapped_by_another(self, _clean_http_client_print: None) -> None:
        """If another library wraps us after we installed, our
        ``uninstall`` must not disrupt that outer chain — the outer
        wrapper still references us as its inner."""
        r = HttpClientPrintRedactor()
        r.install()
        ours = http.client.__dict__["print"]

        def _outer(*args: Any, **kwargs: Any) -> None:
            ours(*args, **kwargs)

        http.client.print = _outer  # pyright: ignore[reportAttributeAccessIssue]
        r.uninstall()
        # _outer is still in place; mutating the chain would break it.
        assert http.client.__dict__["print"] is _outer

    def test_custom_mask_fn(self, _clean_http_client_print: None) -> None:
        """Callers can supply a custom mask function (e.g. for tests)."""
        calls: list[str] = []

        def _mask(token: str) -> str:
            calls.append(token)
            return "<CUSTOM>"

        with HttpClientPrintRedactor(mask_fn=_mask):
            line = (
                r"b'POST /tasks HTTP/1.1\r\n"
                r"Authorization: Bearer SECRET-VALUE\r\n'"
            )
            # Reach the wrapper directly with a request-headers chunk.
            wrapper = http.client.__dict__["print"]
            wrapper("send:", line)
        assert calls == ["SECRET-VALUE"]

    def test_redacts_real_http_client_send(
        self, _clean_http_client_print: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """End-to-end: with debuglevel=1 and the redactor installed,
        ``http.client``'s ``send:`` line must not contain the raw token.

        Exercises the path the asana SDK actually triggers — the bare
        ``print("send:", repr(data))`` inside ``HTTPConnection.send``.
        """

        class _H(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args: Any, **kwargs: Any) -> None:
                return

        srv = HTTPServer(("127.0.0.1", 0), _H)
        port = srv.server_address[1]
        threading.Thread(target=srv.handle_request, daemon=True).start()

        token = "SECRET-TOKEN-2/123456/789:abcdef0123"
        with HttpClientPrintRedactor():
            http.client.HTTPConnection.debuglevel = 1
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            # Add a trailing header so we can assert the regex did not
            # swallow the next header name at the boundary.
            conn.request(
                "GET",
                "/x",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Test-Trailer": "preserved",
                },
            )
            conn.getresponse().read()
            conn.close()

        captured = capsys.readouterr()
        assert token not in captured.out
        assert token not in captured.err
        # Partial reveal: the last six characters of the token survive.
        assert f"...{token[-6:]}" in captured.out
        assert "X-Test-Trailer: preserved" in captured.out
