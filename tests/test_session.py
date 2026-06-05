"""Tests for asana_api_cli.session — the AsanaSession side-effect lifecycle,
MultibyteFilenameSupport, the pagination knobs, the ``_CONFIG_KNOBS``
Configuration table, and the ApiClient-instance settings (``--user-agent`` /
``--set-default-header``).

The input-resolution helpers (``resolve_body`` / ``resolve_workspace``) now
live in ``asana_api_cli.cli``; their tests are in ``test_cli.py``.
"""

from __future__ import annotations

import http.client
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from urllib3.fields import RequestField

from asana_api_cli.session import (
    AsanaSession,
    MultibyteFilenameSupport,
    runtime,
)

# ---------------------------------------------------------------------------
# AsanaSession side-effect lifecycle (open / close via __enter__ / __exit__)
#
# The ``HttpClientAuthRedactor`` itself (helpers, masking, lifecycle,
# end-to-end with a live HTTP server) is covered in ``test_redactor.py``;
# the tests here check that ``AsanaSession`` installs all its global side
# effects (the ``http.client`` debuglevel/print patch and the asana/urllib3
# logger levels raised by the SDK ``debug`` setter) on ``__enter__`` (never at
# construction) and reverses them on ``__exit__``.
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


@pytest.fixture
def _restore_logger_levels() -> Iterator[None]:
    """Snapshot and restore the asana / urllib3 logger levels that the SDK's
    ``debug`` setter raises to DEBUG."""
    loggers = [logging.getLogger("asana"), logging.getLogger("urllib3")]
    saved = [(lg, lg.level) for lg in loggers]
    try:
        yield
    finally:
        for lg, level in saved:
            lg.setLevel(level)


class TestAsanaSessionSideEffectLifecycle:
    def test_debug_session_installs_on_enter_and_restores_on_exit(
        self, _clean_http_client_print: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``--debug`` session installs its global side effects on
        ``__enter__`` and reverses them on ``__exit__`` — never merely on
        construction. Inside the block, ``http.client`` wire tracing is on
        (debuglevel 1) AND the ``Authorization`` redactor is installed; on exit
        both are reversed in order (tracing off first, mask removed last), so the
        process is never left tracing-on-without-mask (constitution #2).
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
        # After exit: both reversed in order (tracing off first, mask last) —
        # never tracing-on-without-mask.
        assert http.client.HTTPConnection.debuglevel == 0
        assert "print" not in http.client.__dict__

    def test_debug_session_restores_logger_levels_on_exit(
        self,
        _clean_http_client_print: None,
        _restore_logger_levels: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The SDK ``debug`` setter raises the asana/urllib3 loggers to DEBUG
        alongside the debuglevel flip. ``close()`` must restore their prior
        levels — not leave them at DEBUG (a process-wide leak), and not force
        WARNING as the SDK's own ``debug = False`` would.
        """
        asana_logger = logging.getLogger("asana")
        urllib3_logger = logging.getLogger("urllib3")
        # Distinct, non-DEBUG sentinels prove exact restoration (not "forced to
        # WARNING" and not "left at DEBUG").
        asana_logger.setLevel(logging.WARNING)
        urllib3_logger.setLevel(logging.ERROR)
        monkeypatch.setattr(runtime, "debug", True)

        with AsanaSession(token="x" * 20):
            # The SDK debug setter raised both to DEBUG inside the block.
            assert asana_logger.level == logging.DEBUG
            assert urllib3_logger.level == logging.DEBUG
        # Restored to the exact pre-session levels.
        assert asana_logger.level == logging.WARNING
        assert urllib3_logger.level == logging.ERROR

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
    ``--multibyte-filenames``."""

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


# ---------------------------------------------------------------------------
# AsanaSession.from_env token resolution
# ---------------------------------------------------------------------------


class TestAsanaSessionFromEnv:
    """``from_env`` resolves the token as ``runtime.access_token`` first, then
    ``$ASANA_ACCESS_TOKEN``, and exits ``2`` if neither is set. The empty-token
    fallback is the other half of the ``--access-token`` last-wins behavior: an
    explicit empty ``--access-token`` clears the runtime value, then this
    fallback applies."""

    def test_empty_runtime_token_falls_back_to_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(runtime, "access_token", "")
        monkeypatch.setenv("ASANA_ACCESS_TOKEN", "env-token-1234567890")
        with AsanaSession.from_env() as session:
            assert session.client.configuration.access_token == "env-token-1234567890"

    def test_no_token_anywhere_exits_2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(runtime, "access_token", "")
        monkeypatch.delenv("ASANA_ACCESS_TOKEN", raising=False)
        with pytest.raises(SystemExit) as exc:
            AsanaSession.from_env()
        assert exc.value.code == 2
        assert "Access token is not set" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# AsanaSession Configuration knobs (the _CONFIG_KNOBS table)
# ---------------------------------------------------------------------------


class TestAsanaSessionConfigKnobs:
    """Every ``runtime`` Configuration knob reaches ``asana.Configuration`` via
    the ``_CONFIG_KNOBS`` table in ``AsanaSession.__init__``. The expected
    (knob -> value) pairs here are written independently of that table, so a
    dropped or mis-conditioned entry is caught. (``retry_strategy`` has its own
    end-to-end coverage in ``test_cli_invocation.py:TestRetryStrategyReachesSession``.)
    """

    def test_runtime_knobs_reach_configuration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Representative non-default values. Includes the "is not None" knobs
        # that must still apply an explicit ``False`` (return_page_iterator /
        # verify_ssl) alongside the path/string knobs.
        expected: dict[str, Any] = {
            "return_page_iterator": False,
            "page_limit": 50,
            "host": "https://example.test/api",
            "proxy": "http://127.0.0.1:8080",
            "verify_ssl": False,
            "ssl_ca_cert": "/tmp/ca.pem",
            "temp_folder_path": "/tmp/dl",
            "logger_format": "%(message)s",
            "logger_file": "/tmp/sdk.log",
            "cert_file": "/tmp/client.crt",
            "key_file": "/tmp/client.key",
            "assert_hostname": True,
            "connection_pool_maxsize": 7,
            "safe_chars_for_path_param": "/:",
        }
        for attr, value in expected.items():
            monkeypatch.setattr(runtime, attr, value)
        config = AsanaSession(token="x" * 20).client.configuration
        for attr, value in expected.items():
            assert getattr(config, attr) == value, f"{attr} did not reach Configuration"

    @pytest.mark.parametrize("attr", ["host", "proxy", "ssl_ca_cert", "temp_folder_path"])
    def test_truthy_guarded_empty_string_is_skipped(
        self, attr: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The truthy-guarded knobs must NOT overwrite the SDK default with an
        # empty string (mirrors the original ``if runtime.X:`` conditions). A
        # flipped ``apply_when_truthy`` would let "" through. Asserting "value is
        # not the empty string" works regardless of the knob's own default
        # (host defaults to a URL; proxy/ssl_ca_cert/temp_folder_path to None).
        monkeypatch.setattr(runtime, attr, "")
        config = AsanaSession(token="x" * 20).client.configuration
        assert getattr(config, attr, None) != "", (
            f"empty {attr!r} must be skipped, leaving the SDK default"
        )


# ---------------------------------------------------------------------------
# ApiClient-instance settings (--user-agent / --set-default-header)
# ---------------------------------------------------------------------------


class TestAsanaSessionApiClientHeaders:
    """``runtime.user_agent`` / ``runtime.default_headers`` are applied to the
    ``ApiClient`` after construction (they are ApiClient-instance settings, not
    ``Configuration`` knobs)."""

    def test_user_agent_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(runtime, "user_agent", "MyApp/1.0")
        session = AsanaSession(token="x" * 20)
        assert session.client.user_agent == "MyApp/1.0"

    def test_default_headers_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(runtime, "default_headers", {"X-Foo": "bar", "X-Baz": "qux"})
        session = AsanaSession(token="x" * 20)
        assert session.client.default_headers["X-Foo"] == "bar"
        assert session.client.default_headers["X-Baz"] == "qux"

    def test_user_agent_wins_over_default_header_user_agent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Both target default_headers['User-Agent']; the dedicated --user-agent
        # is applied last so it wins (session.py orders the two deliberately).
        monkeypatch.setattr(runtime, "default_headers", {"User-Agent": "FromHeader"})
        monkeypatch.setattr(runtime, "user_agent", "FromUserAgent")
        session = AsanaSession(token="x" * 20)
        assert session.client.user_agent == "FromUserAgent"

    def test_unset_leaves_sdk_default_user_agent(self) -> None:
        # No runtime overrides → the SDK's own default User-Agent is untouched.
        session = AsanaSession(token="x" * 20)
        assert session.client.user_agent.startswith("Swagger-Codegen")
