"""Tests for asana_api_cli.session — resolve_body, resolve_workspace, and debug redaction."""

from __future__ import annotations

import http.client
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from typing import Any

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
# AsanaSession side-effect lifecycle (open / close via __enter__ / __exit__)
#
# The ``HttpClientAuthRedactor`` itself (helpers, masking, lifecycle,
# end-to-end with a live HTTP server) is covered in ``test_redactor.py``;
# the tests here only check that ``AsanaSession`` installs the global
# ``http.client`` patches on ``__enter__`` (never at construction) and
# reverses them on ``__exit__``, including the partial-failure path.
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


class TestAsanaSessionSideEffectLifecycle:
    def test_debug_session_installs_on_enter_and_restores_on_exit(
        self, _clean_http_client_print: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``--debug`` session installs its global side effects on
        ``__enter__`` and reverses them on ``__exit__`` — never merely on
        construction. Inside the block, ``http.client`` wire tracing is on
        (debuglevel 1) AND the ``Authorization`` redactor is installed; on exit
        both are reversed together, so the process is never left
        tracing-on-without-mask (constitution #2).
        """
        monkeypatch.setattr(runtime, "debug", True)
        http.client.HTTPConnection.debuglevel = 0

        session = AsanaSession(token="x" * 20)
        # Construction alone touches no process globals.
        assert http.client.HTTPConnection.debuglevel == 0
        assert "print" not in http.client.__dict__

        with session:
            # Inside the block: tracing on AND the masking patch installed.
            assert http.client.HTTPConnection.debuglevel == 1
            assert "print" in http.client.__dict__
        # After exit: both reversed together — never tracing-on-without-mask.
        assert http.client.HTTPConnection.debuglevel == 0
        assert "print" not in http.client.__dict__

    def test_non_debug_session_leaves_debuglevel_untouched(
        self, _clean_http_client_print: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A session that does not enable debug must not touch the global
        debuglevel at all (the install is scoped to sessions that set it)."""
        monkeypatch.setattr(runtime, "debug", False)
        http.client.HTTPConnection.debuglevel = 0

        with AsanaSession(token="x" * 20):
            assert http.client.HTTPConnection.debuglevel == 0
        assert http.client.HTTPConnection.debuglevel == 0

    def test_construction_failure_leaves_globals_untouched(
        self, _clean_http_client_print: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Side effects install on ``__enter__``, not at construction, so an
        ``ApiClient`` construction failure cannot leak a global patch — none
        has been installed yet. Guards against side-effect install creeping
        back into ``__init__`` ahead of the ApiClient build.
        """

        class _BoomClient:
            def __init__(self, _config: Any) -> None:
                raise RuntimeError("simulated SDK init failure")

        monkeypatch.setattr("asana_api_cli.session.asana.ApiClient", _BoomClient)
        monkeypatch.setattr(runtime, "debug", True)
        http.client.HTTPConnection.debuglevel = 0

        pre_print = http.client.__dict__.get("print")
        with pytest.raises(RuntimeError, match="simulated SDK init failure"):
            AsanaSession(token="x" * 20)
        # Nothing was installed, so http.client is exactly as before.
        assert http.client.__dict__.get("print") is pre_print
        assert http.client.HTTPConnection.debuglevel == 0

    def test_open_failure_in_later_patch_uninstalls_earlier_patch(
        self, _clean_http_client_print: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If a *later* ``install()`` raises after the redactor is already
        installed, ``open()`` must uninstall the redactor (and restore the
        debuglevel) before re-raising. ``__enter__`` delegates to ``open()``
        and cannot fall back on ``__exit__`` for this, since Python skips
        ``__exit__`` when ``__enter__`` raises. Guards a global
        ``http.client.print`` leak when, e.g., a urllib3 change breaks
        ``MultibyteFilenameSupport``.
        """

        def _boom(_self: MultibyteFilenameSupport) -> None:
            raise RuntimeError("simulated multibyte patch failure")

        monkeypatch.setattr(MultibyteFilenameSupport, "install", _boom)
        monkeypatch.setattr(runtime, "debug", True)
        monkeypatch.setattr(runtime, "multibyte_filenames", True)
        http.client.HTTPConnection.debuglevel = 0

        pre_print = http.client.__dict__.get("print")
        with (
            pytest.raises(RuntimeError, match="simulated multibyte patch failure"),
            AsanaSession(token="x" * 20),
        ):
            pass
        # The redactor installed before the failing patch was rolled back,
        # and the debuglevel flip with it.
        assert http.client.__dict__.get("print") is pre_print
        assert http.client.HTTPConnection.debuglevel == 0


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

    def test_asana_session_installs_patch_on_enter_and_reverses_on_exit(
        self, _clean_request_field: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With ``--multibyte-filenames``, ``AsanaSession`` installs the
        ``RequestField.make_multipart`` patch on ``__enter__`` and removes it
        on ``__exit__`` — never merely on construction.
        """
        monkeypatch.setattr(runtime, "multibyte_filenames", True)
        original = RequestField.make_multipart

        session = AsanaSession(token="x" * 20)
        # Construction alone does not patch make_multipart.
        assert RequestField.make_multipart is original
        with session:
            assert RequestField.make_multipart is not original
        assert RequestField.make_multipart is original


# ---------------------------------------------------------------------------
# AsanaSession pagination kwargs
# ---------------------------------------------------------------------------


class TestAsanaSessionPaginationKwargs:
    """``AsanaSession`` reads ``return_page_iterator`` and ``page_limit``
    from the shared ``runtime`` singleton (set by the ``--return-page-iterator``
    / ``--page-limit`` global flags). When unset, the SDK's own defaults
    (``True`` and ``100``) are preserved."""

    def test_runtime_values_propagate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ASANA_ACCESS_TOKEN", "x" * 20)
        monkeypatch.setattr(runtime, "return_page_iterator", False)
        monkeypatch.setattr(runtime, "page_limit", 50)
        with AsanaSession.from_env() as session:
            assert session.client.configuration.return_page_iterator is False
            assert session.client.configuration.page_limit == 50

    def test_constructor_defaults_match_sdk_defaults(self) -> None:
        with AsanaSession(token="x" * 20) as session:
            assert session.client.configuration.return_page_iterator is True
