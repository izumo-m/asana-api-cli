"""Tests for asana_api_cli.multibyte_filename — the ``MultibyteFilenameSupport``
context manager that augments ``urllib3.fields.RequestField.make_multipart`` so
multipart fields whose filename contains non-ASCII characters also carry the
RFC 5987 ``filename*=UTF-8''<percent-encoded>`` parameter.

The CLI wiring (the per-command ``--multibyte-filenames`` flag) is covered in
``test_cli_invocation.py``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from urllib3.fields import RequestField

from asana_api_cli.multibyte_filename import MultibyteFilenameSupport


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
    parameter."""

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
