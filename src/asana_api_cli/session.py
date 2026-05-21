"""Asana SDK client construction utilities.

A thin wrapper around the official `asana` SDK ApiClient that handles
initialization from environment variables, toggling pagination mode, and
applying the global configuration passed in from the CLI.
"""

from __future__ import annotations

import functools
import json
import os
import sys
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asana
import click
from urllib3.fields import RequestField
from urllib3.util.retry import Retry

from asana_api_cli.redactor import HttpClientAuthRedactor

ACCESS_TOKEN_ENV = "ASANA_ACCESS_TOKEN"
DEFAULT_WORKSPACE_ENV = "ASANA_DEFAULT_WORKSPACE"


class MultibyteFilenameSupport:
    """Context manager that augments ``urllib3.fields.RequestField`` to
    emit the RFC 5987 ``filename*=UTF-8''<percent-encoded>`` parameter of
    ``Content-Disposition`` whenever a multipart field has a non-ASCII
    filename.

    Asana's attachment endpoint requires the ``filename*=`` form to
    decode non-ASCII filenames correctly; without it the server treats
    the ``filename="..."`` value as a literal and stores it as mojibake
    or percent-encoded text. The official ``python-asana`` SDK (via
    urllib3's default multipart formatter) does not emit ``filename*=`` —
    this is a long-standing upstream gap (Asana forum discussion since
    2022-12, unresolved as of 2026-05).

    Off by default to preserve strict SDK parity. The CLI enables it
    when ``--multibyte-filenames`` is set; library callers can use it
    standalone::

        with MultibyteFilenameSupport():
            # urllib3-based uploads in this block emit filename*=
            ...
    """

    def __init__(self) -> None:
        self._original: Callable[..., None] | None = None

    def install(self) -> None:
        if self._original is not None:
            return
        self._original = RequestField.make_multipart
        original = self._original

        def _patched(
            field: RequestField,
            content_disposition: str | None = None,
            content_type: str | None = None,
            content_location: str | None = None,
        ) -> None:
            original(field, content_disposition, content_type, content_location)
            filename = field._filename  # pyright: ignore[reportPrivateUsage]
            if filename and any(ord(c) > 127 for c in filename):
                encoded = urllib.parse.quote(filename, safe="")
                existing = field.headers.get("Content-Disposition") or ""
                field.headers["Content-Disposition"] = existing + f"; filename*=utf-8''{encoded}"

        RequestField.make_multipart = _patched  # pyright: ignore[reportAttributeAccessIssue]

    def uninstall(self) -> None:
        if self._original is None:
            return
        RequestField.make_multipart = self._original  # pyright: ignore[reportAttributeAccessIssue]
        self._original = None

    def __enter__(self) -> MultibyteFilenameSupport:
        self.install()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.uninstall()


JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None


def resolve_body(value: str) -> JsonValue:
    """Parse a body argument as JSON.

    Supports three input forms:
    - ``@path`` — read JSON from a file
    - ``-``     — read JSON from stdin
    - otherwise — parse the string itself as JSON
    """
    if value == "-":
        try:
            raw = sys.stdin.read()
        except UnicodeDecodeError as exc:
            # stdin is reconfigured to UTF-8 at startup (see cli.py main),
            # so non-UTF-8 input from a pipe surfaces here instead of being
            # silently misdecoded with the locale code page.
            click.echo(f"Body from stdin is not valid UTF-8: {exc}", err=True)
            sys.exit(1)
    elif value.startswith("@"):
        path = Path(value[1:])
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            click.echo(f"Body file not found: {path}", err=True)
            sys.exit(1)
        except UnicodeDecodeError as exc:
            click.echo(
                f"Body file {path} is not valid UTF-8: {exc}",
                err=True,
            )
            sys.exit(1)
        except OSError as exc:
            click.echo(f"Cannot read body file {path}: {exc}", err=True)
            sys.exit(1)
    else:
        raw = value

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        click.echo(f"Invalid JSON in body: {exc}", err=True)
        sys.exit(1)


@dataclass
class _Runtime:
    """Configuration shared globally during a CLI invocation.

    Updated by the ``main`` callback and the global-option mixins in ``click_ext``.
    """

    debug: bool = False
    host: str | None = None
    proxy: str | None = None
    verify_ssl: bool = True
    ssl_ca_cert: str | None = None
    retries: int | None = None
    timeout: float | None = None
    access_token: str | None = None
    temp_dir: str | None = None
    multibyte_filenames: bool = False


runtime = _Runtime()


class AsanaSession:
    """Session that holds an ApiClient from the official asana SDK.

    Usable as a context manager so the optional debug redactor
    (installed when ``runtime.debug`` is True) is uninstalled cleanly
    on exit. Direct instantiation without ``with`` also works — the
    redactor then stays installed for the lifetime of the process,
    which is fine for one-shot CLI use but leaks global state for
    longer-lived library use.
    """

    def __init__(
        self, token: str, *, return_page_iterator: bool = True, page_limit: int | None = None
    ) -> None:
        config = asana.Configuration()
        config.access_token = token
        # *return_page_iterator* and *page_limit* mirror the SDK's
        # ``asana.Configuration`` properties of the same names: with
        # ``return_page_iterator=True`` (the SDK default) paginatable
        # methods return an iterator that walks every page; with False
        # they return a single ``{data, next_page}`` dict per HTTP call.
        # ``page_limit`` (SDK default 100) is the per-page size used on the
        # iterator path when ``opts["limit"]`` is not set.
        config.return_page_iterator = return_page_iterator
        if page_limit is not None:
            config.page_limit = page_limit

        # Apply runtime values to Configuration
        if runtime.host:
            config.host = runtime.host
        if runtime.proxy:
            config.proxy = runtime.proxy  # pyright: ignore[reportAttributeAccessIssue]
        if not runtime.verify_ssl:
            config.verify_ssl = False
        if runtime.ssl_ca_cert:
            config.ssl_ca_cert = runtime.ssl_ca_cert  # pyright: ignore[reportAttributeAccessIssue]
        if runtime.temp_dir:
            config.temp_folder_path = runtime.temp_dir  # pyright: ignore[reportAttributeAccessIssue]
        if runtime.retries is not None:
            # Build a Retry with the user-specified total and python-asana's
            # default backoff/status_forcelist.
            config.retry_strategy = Retry(
                total=runtime.retries,
                backoff_factor=2,
                status_forcelist=[429, 500, 502, 503, 504],
            )

        self._redactor: HttpClientAuthRedactor | None = None
        if runtime.debug:
            # The SDK debug setter enables http.client.HTTPConnection.debuglevel
            # and bumps the urllib3/asana loggers to DEBUG. The only path that
            # leaks the Authorization header is http.client's wire-level
            # ``print()`` calls — the SDK's own loggers do not log headers.
            # Install the redactor AFTER the SDK setup so we wrap whatever
            # http.client.print is at that point.
            config.debug = True
            self._redactor = HttpClientAuthRedactor()
            self._redactor.install()

        self._multibyte_filenames: MultibyteFilenameSupport | None = None
        if runtime.multibyte_filenames:
            self._multibyte_filenames = MultibyteFilenameSupport()
            self._multibyte_filenames.install()

        try:
            self._config = config
            self._client = asana.ApiClient(config)
            # Configuration has no --timeout knob, so wrap call_api to inject it.
            if runtime.timeout is not None:
                self._install_timeout(runtime.timeout)
        except Exception:
            # If construction fails after the patches were installed, the
            # caller never gets a session to call close() on, so undo the
            # global patches here rather than leaving them leaked for the
            # rest of the process.
            if self._redactor is not None:
                self._redactor.uninstall()
                self._redactor = None
            if self._multibyte_filenames is not None:
                self._multibyte_filenames.uninstall()
                self._multibyte_filenames = None
            raise

    def _install_timeout(self, timeout: float) -> None:
        """Wrap ApiClient.call_api to inject a default _request_timeout."""
        original = self._client.call_api

        @functools.wraps(original)
        def call_api_with_timeout(*args: Any, **kwargs: Any) -> Any:
            kwargs.setdefault("_request_timeout", timeout)
            return original(*args, **kwargs)

        self._client.call_api = call_api_with_timeout  # type: ignore[method-assign]

    @property
    def client(self) -> asana.ApiClient:
        return self._client

    def close(self) -> None:
        """Uninstall any global patches installed for this session
        (debug redactor, multibyte-filename multipart patch).

        Safe to call multiple times. Prefer using the session as a
        context manager (``with AsanaSession(...) as session: ...``)
        which calls ``close`` automatically.
        """
        if self._redactor is not None:
            self._redactor.uninstall()
            self._redactor = None
        if self._multibyte_filenames is not None:
            self._multibyte_filenames.uninstall()
            self._multibyte_filenames = None

    def __enter__(self) -> AsanaSession:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @classmethod
    def from_env(
        cls, *, return_page_iterator: bool = True, page_limit: int | None = None
    ) -> AsanaSession:
        """Build a session from runtime.access_token, falling back to $ASANA_ACCESS_TOKEN."""
        token = runtime.access_token or os.environ.get(ACCESS_TOKEN_ENV, "")
        if not token:
            click.echo(
                f"Access token is not set. Pass --access-token or set {ACCESS_TOKEN_ENV}.",
                err=True,
            )
            sys.exit(1)
        return cls(token=token, return_page_iterator=return_page_iterator, page_limit=page_limit)


def resolve_workspace(
    explicit: str | None,
    *,
    required: bool = False,
) -> str | None:
    """Resolve workspace GID with fallback chain.

    Priority: explicit ``--workspace`` value > ``ASANA_DEFAULT_WORKSPACE``
    env var (only when *required* is True).

    When workspace is optional (``required=False``), the env-var fallback is
    **not** used. This prevents the default workspace from being sent
    alongside other scope parameters (e.g. ``--project`` on ``get-tasks``)
    that the Asana API accepts in place of workspace.

    If *required* is True and no value is found, exits with an error.
    """
    if explicit is not None:
        return explicit
    if required:
        ws = os.environ.get(DEFAULT_WORKSPACE_ENV)
        if ws:
            return ws
        click.echo(
            f"Workspace is required. Specify --workspace or set {DEFAULT_WORKSPACE_ENV}.",
            err=True,
        )
        sys.exit(1)
    return None
