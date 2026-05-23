"""Tests for asana_api_cli.session — resolve_body, resolve_workspace, and debug redaction."""

from __future__ import annotations

import http.client
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
from pathlib import Path
from typing import Any

import asana
import pytest

from urllib3.fields import RequestField

from asana_api_cli.session import (
    DEFAULT_WORKSPACE_ENV,
    AsanaSession,
    MultibyteFilenameSupport,
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

    def test_non_utf8_file_exits_cleanly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A binary file (or any non-UTF-8 byte sequence) must produce a
        clean error message, not a raw ``UnicodeDecodeError`` traceback."""
        body_file = tmp_path / "binary.bin"
        body_file.write_bytes(b"\x80\x81\x82")  # invalid UTF-8 start bytes
        with pytest.raises(SystemExit):
            resolve_body(f"@{body_file}")
        err = capsys.readouterr().err
        assert "not valid UTF-8" in err


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
# AsanaSession redactor cleanup
#
# The ``HttpClientAuthRedactor`` itself (helpers, masking, lifecycle,
# end-to-end with a live HTTP server) is covered in ``test_redactor.py``;
# the test here only checks that ``AsanaSession.__init__`` reverses the
# global ``http.client.print`` patch on failure paths.
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


class TestAsanaSessionRedactorCleanup:
    def test_init_failure_uninstalls_redactor(
        self, _clean_http_client_print: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If ``ApiClient`` construction raises after the redactor has
        been installed, ``AsanaSession.__init__`` must uninstall the
        global ``http.client.print`` patch before re-raising. The
        caller never receives a session object, so they cannot call
        ``close()`` themselves — cleanup must happen here or the patch
        leaks for the lifetime of the process.
        """

        class _BoomClient:
            def __init__(self, _config: Any) -> None:
                raise RuntimeError("simulated SDK init failure")

        monkeypatch.setattr("asana_api_cli.session.asana.ApiClient", _BoomClient)
        monkeypatch.setattr(runtime, "debug", True)

        pre_print = http.client.__dict__.get("print")
        with pytest.raises(RuntimeError, match="simulated SDK init failure"):
            AsanaSession(token="x" * 20)
        # http.client.print is exactly what it was before the failed init.
        assert http.client.__dict__.get("print") is pre_print


# ---------------------------------------------------------------------------
# MultibyteFilenameSupport
# ---------------------------------------------------------------------------


@pytest.fixture
def _clean_request_field() -> Iterator[None]:
    """Snapshot and restore ``urllib3.fields.RequestField.make_multipart``."""
    saved = RequestField.make_multipart
    try:
        yield
    finally:
        RequestField.make_multipart = saved  # pyright: ignore[reportAttributeAccessIssue]


def _content_disposition(filename: str | None) -> str:
    """Build a fresh ``RequestField`` and return its rendered
    Content-Disposition after ``make_multipart``."""
    field = RequestField("file", b"data", filename=filename)
    field.make_multipart()
    result = field.headers["Content-Disposition"]
    assert result is not None  # set unconditionally by make_multipart
    return result


class TestMultibyteFilenameSupport:
    """``MultibyteFilenameSupport`` augments ``RequestField.make_multipart``
    so that multipart fields whose filename contains non-ASCII characters
    also carry the RFC 5987 ``filename*=UTF-8''<percent-encoded>``
    parameter. Off by default to preserve strict SDK parity; opt-in via
    ``--multibyte-filenames`` / ``runtime.multibyte_filenames``."""

    def test_context_manager_installs_and_uninstalls(self, _clean_request_field: None) -> None:
        original = RequestField.make_multipart
        with MultibyteFilenameSupport():
            assert RequestField.make_multipart is not original
        assert RequestField.make_multipart is original

    def test_explicit_install_uninstall(self, _clean_request_field: None) -> None:
        original = RequestField.make_multipart
        patcher = MultibyteFilenameSupport()
        patcher.install()
        assert RequestField.make_multipart is not original
        patcher.uninstall()
        assert RequestField.make_multipart is original

    def test_uninstall_safe_to_call_twice(self, _clean_request_field: None) -> None:
        patcher = MultibyteFilenameSupport()
        patcher.install()
        patcher.uninstall()
        patcher.uninstall()  # no-op, must not raise

    def test_install_idempotent_on_same_instance(self, _clean_request_field: None) -> None:
        """A second ``install()`` on the same instance must not capture
        the already-patched function as the new ``_original`` — that
        would make ``uninstall()`` restore the patch instead of the
        true upstream function."""
        original = RequestField.make_multipart
        patcher = MultibyteFilenameSupport()
        patcher.install()
        first_patched = RequestField.make_multipart
        patcher.install()
        # Still the same patched function — no double-wrapping.
        assert RequestField.make_multipart is first_patched
        patcher.uninstall()
        # And ``_original`` survived intact, so uninstall restored upstream.
        assert RequestField.make_multipart is original

    def test_default_behavior_unchanged_without_patch(self) -> None:
        """Sanity check on the upstream baseline: a non-ASCII filename
        only produces ``filename="<utf-8 chars>"`` (no ``filename*=``)
        when the patch is NOT installed."""
        disposition = _content_disposition("日本語.txt")
        assert "filename*=" not in disposition

    def test_ascii_filename_is_noop(self, _clean_request_field: None) -> None:
        """ASCII filenames must pass through unchanged — no extra
        ``filename*=`` parameter."""
        with MultibyteFilenameSupport():
            disposition = _content_disposition("ascii.txt")
        assert 'filename="ascii.txt"' in disposition
        assert "filename*=" not in disposition

    def test_non_ascii_filename_adds_filename_star(self, _clean_request_field: None) -> None:
        """Non-ASCII filenames get an additional RFC 5987
        ``filename*=utf-8''<percent-encoded>`` parameter."""
        with MultibyteFilenameSupport():
            disposition = _content_disposition("日本語.txt")
        # Original ``filename=`` is kept (servers that don't speak RFC
        # 5987 fall back to it).
        assert 'filename="日本語.txt"' in disposition
        # Plus the encoded ``filename*=`` variant for servers that do.
        assert "filename*=utf-8''%E6%97%A5%E6%9C%AC%E8%AA%9E.txt" in disposition

    def test_no_filename_field_unaffected(self, _clean_request_field: None) -> None:
        """Multipart fields without a filename (e.g. plain form data
        like ``parent=<gid>``) must pass through unchanged."""
        with MultibyteFilenameSupport():
            field = RequestField("parent", b"12345")
            field.make_multipart()
        disposition = field.headers["Content-Disposition"]
        assert disposition is not None
        assert 'name="parent"' in disposition
        assert "filename*=" not in disposition

    def test_asana_session_init_failure_uninstalls_multibyte_patch(
        self, _clean_request_field: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If ``ApiClient`` construction raises after the multibyte
        patch has been installed, ``AsanaSession.__init__`` must
        uninstall the global ``RequestField.make_multipart`` patch
        before re-raising — the caller never receives a session object,
        so they cannot call ``close()`` themselves.
        """

        class _BoomClient:
            def __init__(self, _config: Any) -> None:
                raise RuntimeError("simulated SDK init failure")

        monkeypatch.setattr("asana_api_cli.session.asana.ApiClient", _BoomClient)
        monkeypatch.setattr(runtime, "multibyte_filenames", True)

        original = RequestField.make_multipart
        with pytest.raises(RuntimeError, match="simulated SDK init failure"):
            AsanaSession(token="x" * 20)
        assert RequestField.make_multipart is original


# ---------------------------------------------------------------------------
# AsanaSession pagination kwargs
# ---------------------------------------------------------------------------


class TestAsanaSessionPaginationKwargs:
    """``AsanaSession`` exposes ``return_page_iterator`` and ``page_limit``
    that forward 1:1 to the underlying ``asana.Configuration`` properties of
    the same name. The defaults (``True`` and ``None``) match the SDK's own
    defaults so the SDK behavior is preserved when callers omit them."""

    def test_from_env_explicit_kwargs_propagate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ASANA_ACCESS_TOKEN", "x" * 20)
        with AsanaSession.from_env(return_page_iterator=False, page_limit=50) as session:
            assert session.client.configuration.return_page_iterator is False
            assert session.client.configuration.page_limit == 50

    def test_constructor_defaults_match_sdk_defaults(self) -> None:
        with AsanaSession(token="x" * 20) as session:
            assert session.client.configuration.return_page_iterator is True


# ---------------------------------------------------------------------------
# No-op auth properties: --username / --password / --api-key / --api-key-prefix
#
# These four CLI flags exist for 1:1 parity with ``asana.Configuration`` but
# are inert in the python-asana 5.2.4 request path (every ``*Api`` method
# uses ``auth_settings = ['personalAccessToken']`` only). The disclosure in
# their ``--help`` text and in ``docs/cli-sdk-mapping.md`` calls this out,
# but the load-bearing security claim is broader: setting them must also
# not leak through ``--debug``'s wire-level output. If a future SDK update
# starts including the values in any header / log / repr, this test fails
# loudly so the disclosure (and the redactor, if applicable) can catch up.
# ---------------------------------------------------------------------------


class TestNoopAuthPropertiesDoNotLeak:
    def test_debug_output_contains_none_of_the_canary_values(
        self,
        _clean_http_client_print: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Local mock server: 200 OK for any path, returns a minimal JSON body.
        class _H(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = b'{"data": {"gid": "X", "name": "stub"}}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a: Any, **_k: Any) -> None:
                return

        srv = HTTPServer(("127.0.0.1", 0), _H)
        port = srv.server_address[1]
        threading.Thread(target=srv.handle_request, daemon=True).start()

        # Distinctive canaries with no overlap to the access token, the
        # server response body, or any header value the SDK emits.
        canaries: dict[str, str] = {
            "username": "LEAK_CANARY_USERNAME_8675309",
            "password": "LEAK_CANARY_PASSWORD_8675309",
            "api_key": "LEAK_CANARY_APIKEY_8675309",
            "api_key_prefix": "LEAK_CANARY_PREFIX_8675309",
        }

        saved = {
            "debug": runtime.debug,
            "username": runtime.username,
            "password": runtime.password,
            "api_key": runtime.api_key,
            "api_key_prefix": runtime.api_key_prefix,
            "host": runtime.host,
        }
        runtime.debug = True
        runtime.username = canaries["username"]
        runtime.password = canaries["password"]
        runtime.api_key = {"k1": canaries["api_key"]}
        runtime.api_key_prefix = {"k1": canaries["api_key_prefix"]}
        runtime.host = f"http://127.0.0.1:{port}"
        try:
            with AsanaSession(token="x" * 20) as session:
                asana.TasksApi(session.client).get_task("X", opts={})
        finally:
            for name, value in saved.items():
                setattr(runtime, name, value)

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        for name, value in canaries.items():
            assert value not in combined, (
                f"--{name.replace('_', '-')} canary ({value!r}) leaked "
                f"in --debug output; the no-op disclosure is no longer "
                f"accurate for this python-asana version."
            )
