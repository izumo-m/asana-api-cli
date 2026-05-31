"""Asana SDK client construction utilities.

A thin wrapper around the official `asana` SDK ApiClient that handles
initialization from environment variables, toggling pagination mode, and
applying the global configuration passed in from the CLI.
"""

from __future__ import annotations

import http.client
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

from asana_api_cli.redactor import HttpClientAuthRedactor

ACCESS_TOKEN_ENV = "ASANA_ACCESS_TOKEN"
DEFAULT_WORKSPACE_ENV = "ASANA_DEFAULT_WORKSPACE"


class MultibyteFilenameSupport:
    """Make multipart uploads round-trip filenames with non-ASCII characters.

    In ``python-asana`` 5.2.4 (the latest version checked, and likely later
    ones too), uploading a file whose name has characters outside ASCII
    stores a garbled (mojibake) name on Asana: the SDK's multipart encoder
    emits only ``filename="..."`` and omits the RFC 5987 ``filename*=``
    parameter the server needs to decode them. This context manager patches
    ``urllib3.fields.RequestField.make_multipart`` to add
    ``filename*=UTF-8''<percent-encoded>`` for such names.

    Off by default to preserve strict SDK parity. The CLI enables it when
    ``--multibyte-filenames`` is passed to an upload command (e.g.
    ``attachments create-attachment-for-object``); library callers can use
    it standalone::

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
            sys.exit(2)
    elif value.startswith("@"):
        path = Path(value[1:])
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            click.echo(f"Body file not found: {path}", err=True)
            sys.exit(2)
        except UnicodeDecodeError as exc:
            click.echo(
                f"Body file {path} is not valid UTF-8: {exc}",
                err=True,
            )
            sys.exit(2)
        except OSError as exc:
            click.echo(f"Cannot read body file {path}: {exc}", err=True)
            sys.exit(2)
    else:
        raw = value

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        click.echo(f"Invalid JSON in body: {exc}", err=True)
        sys.exit(2)


@dataclass
class _Runtime:
    """Configuration shared globally during a CLI invocation.

    Updated by the ``main`` callback and the global-option mixins in ``click_ext``.
    Each non-flag scalar defaults to ``None`` so ``AsanaSession`` can tell
    "user did not pass the flag" from "user explicitly chose this value" and
    leave the SDK default in place for the former.
    """

    debug: bool = False
    host: str | None = None
    proxy: str | None = None
    verify_ssl: bool | None = None
    ssl_ca_cert: str | None = None
    access_token: str | None = None
    temp_folder_path: str | None = None
    # Set by the upload command's per-command ``--multibyte-filenames`` flag
    # (``cli.py:_make_command``), not a global option. Kept here because the
    # session reads it to decide whether to install ``MultibyteFilenameSupport``;
    # non-upload commands leave it at this default.
    multibyte_filenames: bool = False
    logger_format: str | None = None
    logger_file: str | None = None
    cert_file: str | None = None
    key_file: str | None = None
    assert_hostname: bool | None = None
    connection_pool_maxsize: int | None = None
    safe_chars_for_path_param: str | None = None
    retry_strategy_overrides: dict[str, Any] | None = None
    # Configuration-backed iterator knobs. The per-call kwargs
    # (full_payload / item_limit / header_params / _request_timeout) are NOT
    # here: they are per-command options forwarded by ``cli.py:_make_command``.
    # The CLI-only output-formatting options (--output / --query and their
    # error-path twins --exception-output / --exception-query) are likewise not here:
    # they flow as kwargs through ``formatter.py:formatted``.
    return_page_iterator: bool | None = None
    page_limit: int | None = None


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

    def __init__(self, token: str) -> None:
        config = asana.Configuration()
        config.access_token = token

        # Apply runtime values to Configuration.
        # ``return_page_iterator`` / ``page_limit`` are read from runtime
        # like the other Configuration knobs. Unspecified ⇒ leave the SDK
        # default (True / 100) in place.
        if runtime.return_page_iterator is not None:
            config.return_page_iterator = runtime.return_page_iterator
        if runtime.page_limit is not None:
            config.page_limit = runtime.page_limit
        if runtime.host:
            config.host = runtime.host
        if runtime.proxy:
            config.proxy = runtime.proxy  # pyright: ignore[reportAttributeAccessIssue]
        if runtime.verify_ssl is not None:
            # Honor both sides of the toggle: --no-verify-ssl writes False,
            # --verify-ssl writes True (pinning the SDK default even if the
            # SDK later changes its own default).
            config.verify_ssl = runtime.verify_ssl
        if runtime.ssl_ca_cert:
            config.ssl_ca_cert = runtime.ssl_ca_cert  # pyright: ignore[reportAttributeAccessIssue]
        if runtime.temp_folder_path:
            config.temp_folder_path = runtime.temp_folder_path  # pyright: ignore[reportAttributeAccessIssue]
        if runtime.logger_format is not None:
            config.logger_format = runtime.logger_format  # pyright: ignore[reportAttributeAccessIssue]
        if runtime.logger_file is not None:
            config.logger_file = runtime.logger_file  # pyright: ignore[reportAttributeAccessIssue]
        if runtime.cert_file is not None:
            config.cert_file = runtime.cert_file  # pyright: ignore[reportAttributeAccessIssue]
        if runtime.key_file is not None:
            config.key_file = runtime.key_file  # pyright: ignore[reportAttributeAccessIssue]
        if runtime.assert_hostname is not None:
            config.assert_hostname = runtime.assert_hostname  # pyright: ignore[reportAttributeAccessIssue]
        if runtime.connection_pool_maxsize is not None:
            config.connection_pool_maxsize = runtime.connection_pool_maxsize  # pyright: ignore[reportAttributeAccessIssue]
        if runtime.safe_chars_for_path_param is not None:
            config.safe_chars_for_path_param = runtime.safe_chars_for_path_param  # pyright: ignore[reportAttributeAccessIssue]
        if runtime.retry_strategy_overrides is not None:
            # Start from the SDK's default Retry instance so unspecified
            # fields keep their python-asana defaults (e.g. total=5,
            # backoff_factor=2, status_forcelist=[429,500,502,503,504]).
            # An empty dict (e.g. `--retry-strategy '{}'`) yields a copy
            # with no field overridden — semantically a no-op, but still
            # honored as "user did pass the flag".
            config.retry_strategy = config.retry_strategy.new(  # pyright: ignore[reportAttributeAccessIssue]
                **runtime.retry_strategy_overrides
            )

        # Install the global patches (debug redactor, multibyte multipart
        # patch) and construct the ApiClient inside one try/except. If ANY step
        # raises — including one ``install()`` after another already
        # succeeded — every patch installed so far is uninstalled before
        # re-raising. The caller never receives a session to ``close()``, so
        # cleanup must happen here or a patch leaks for the rest of the process.
        # Both handles are pre-initialized to ``None`` so the ``except`` can
        # safely test which patches actually got installed.
        self._redactor: HttpClientAuthRedactor | None = None
        self._multibyte_filenames: MultibyteFilenameSupport | None = None
        # Prior ``http.client.HTTPConnection.debuglevel``, captured only when this
        # session turns debug on (see below). ``None`` means "this session did not
        # touch the global", so cleanup leaves it alone.
        self._prev_debuglevel: int | None = None
        try:
            if runtime.debug:
                # The SDK debug setter enables http.client.HTTPConnection.debuglevel
                # and bumps the urllib3/asana loggers to DEBUG. The only path that
                # leaks the Authorization header is http.client's wire-level
                # ``print()`` calls — the SDK's own loggers do not log headers.
                # Install the redactor AFTER the SDK setup so we wrap whatever
                # http.client.print is at that point.
                #
                # ``config.debug = True`` flips the process-global
                # ``http.client.HTTPConnection.debuglevel`` to 1. That global
                # outlives this session, so capture its prior value here and
                # restore it in ``close()`` — otherwise debuglevel stays 1 after
                # the redactor is uninstalled, and a later non-debug session in
                # the same process (which installs no redactor) would print the
                # raw ``Authorization`` header. The restore is paired with
                # ``redactor.uninstall()`` so the masking and the wire-level
                # tracing are always reversed together (constitution #2).
                self._prev_debuglevel = http.client.HTTPConnection.debuglevel
                config.debug = True
                self._redactor = HttpClientAuthRedactor()
                self._redactor.install()

            if runtime.multibyte_filenames:
                self._multibyte_filenames = MultibyteFilenameSupport()
                self._multibyte_filenames.install()

            self._config = config
            self._client = asana.ApiClient(config)
        except Exception:
            # Reverse every global side effect installed so far, including the
            # debuglevel flip, via the same path used by ``close()``.
            self.close()
            raise

    @property
    def client(self) -> asana.ApiClient:
        return self._client

    def close(self) -> None:
        """Reverse every global side effect installed for this session: the
        debug redactor, the multibyte-filename multipart patch, and the
        ``http.client`` debuglevel flip the SDK's debug setter performed.

        Safe to call multiple times. Prefer using the session as a
        context manager (``with AsanaSession(...) as session: ...``)
        which calls ``close`` automatically.
        """
        if self._redactor is not None:
            self._redactor.uninstall()
            self._redactor = None
        # Restore the wire-level debuglevel the SDK debug setter flipped to 1.
        # Paired with the redactor uninstall above so tracing is never left on
        # without the Authorization mask.
        if self._prev_debuglevel is not None:
            http.client.HTTPConnection.debuglevel = self._prev_debuglevel
            self._prev_debuglevel = None
        if self._multibyte_filenames is not None:
            self._multibyte_filenames.uninstall()
            self._multibyte_filenames = None

    def __enter__(self) -> AsanaSession:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @classmethod
    def from_env(cls) -> AsanaSession:
        """Build a session from runtime.access_token, falling back to $ASANA_ACCESS_TOKEN."""
        token = runtime.access_token or os.environ.get(ACCESS_TOKEN_ENV, "")
        if not token:
            click.echo(
                f"Access token is not set. Pass --access-token or set {ACCESS_TOKEN_ENV}.",
                err=True,
            )
            sys.exit(2)
        return cls(token=token)


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
        sys.exit(2)
    return None
