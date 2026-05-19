"""Asana SDK client construction utilities.

A thin wrapper around the official `asana` SDK ApiClient that handles
initialization from environment variables, toggling pagination mode, and
applying the global configuration passed in from the CLI.
"""

from __future__ import annotations

import builtins
import functools
import http.client
import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asana
import click
from urllib3.util.retry import Retry

ACCESS_TOKEN_ENV = "ASANA_ACCESS_TOKEN"
DEFAULT_WORKSPACE_ENV = "ASANA_DEFAULT_WORKSPACE"

# Match ``Authorization: <scheme> <token>`` inside an HTTP request-headers
# chunk. We never apply this regex to user-controlled data (request body or
# response body), so the value side just needs to terminate cleanly at the
# next header boundary: any run of non-whitespace, non-backslash characters
# stops at real CR/LF (raw form) and at the leading ``\\`` of literal
# ``\\r\\n`` (``repr`` form). Character set is intentionally not pinned to
# the shape of today's Asana access tokens — Asana's developer docs
# explicitly state that token formats are opaque and may change.
_AUTH_HEADER_RE = re.compile(
    r"(Authorization:\s*(?:Bearer|Basic))\s+([^\s\\]+)",
    re.IGNORECASE,
)

# HTTP request methods that may appear at the start of the headers chunk in
# ``send: b'<METHOD> ... HTTP/1.1\\r\\n...'``. Used to distinguish that chunk
# from the body chunk(s) that ``http.client`` prints from a separate
# ``send()`` call.
_HTTP_METHODS: tuple[str, ...] = (
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "HEAD",
    "OPTIONS",
    "TRACE",
    "CONNECT",
)

# Mask reveals the last ``_MASK_SUFFIX_LEN`` characters of the token, but
# only when the token is at least ``_MASK_MIN_LEN`` long. The threshold
# caps the leak ratio at 6/16 = 37.5% for unexpectedly short tokens.
_MASK_MIN_LEN = 16
_MASK_SUFFIX_LEN = 6


def _looks_like_request_headers(repr_bytes: str) -> bool:
    """Return True if *repr_bytes* is the ``repr()`` of an HTTP request-headers
    chunk, i.e. starts with ``b'<METHOD> `` (or ``b"<METHOD> ``).

    ``http.client.HTTPConnection.send()`` is called once per chunk it
    flushes. For typical requests that means one call for the headers
    block and zero or more calls for the body chunks. Only the headers
    chunk starts with a method line — body chunks start with whatever the
    body is (JSON ``{...}``, multipart boundary, raw text, etc.) and may
    legitimately contain the substring ``Authorization: Bearer ...`` as
    part of user content (e.g. an Asana task description). Limiting
    redaction to the headers chunk guarantees the body is never touched.
    """
    if not (repr_bytes.startswith("b'") or repr_bytes.startswith('b"')):
        return False
    after_quote = repr_bytes[2:]
    return any(after_quote.startswith(m + " ") for m in _HTTP_METHODS)


def _default_mask_token(token: str) -> str:
    """Return a masked form of *token* suitable for debug output.

    Reveals the last few characters so the user can tell two tokens
    apart (e.g. work vs personal account) without exposing the bulk of
    the secret. For unexpectedly short tokens, falls back to a complete
    redaction.

    Format-agnostic: Asana's developer documentation declares that token
    formats are opaque and may change without notice, so this function
    must not depend on the current PAT/OAuth shape.
    """
    if len(token) < _MASK_MIN_LEN:
        return "<REDACTED>"
    return f"...{token[-_MASK_SUFFIX_LEN:]}"


class HttpClientPrintRedactor:
    """Context manager that redacts ``Authorization`` headers in
    ``http.client``'s wire-level debug output.

    When ``http.client.HTTPConnection.debuglevel`` is 1 (which the asana
    SDK sets via ``Configuration.debug = True``), ``http.client`` issues
    a bare ``print("send:", repr(data))`` on every chunk it flushes to
    the socket. The first such chunk is the request headers, which
    carries the Authorization header verbatim.

    Python resolves the bare ``print`` inside ``http.client`` via the
    module's globals before falling back to builtins, so binding a
    callable to ``http.client.print`` intercepts every debug print the
    module makes — without touching ``sys.stdout``.

    Redaction is applied **only** to ``("send:", "b'<METHOD> ...'")``
    chunks. Body chunks (which may legitimately contain user-supplied
    text matching ``Authorization: ...``) pass through verbatim.

    Layered patches: if some other library has already patched
    ``http.client.print``, ``install`` chains through it instead of
    clobbering it. ``uninstall`` is conservative — it restores the
    previous ``http.client.print`` only if our wrapper is still the
    top-of-stack value. If something else has wrapped us in the
    meantime, ``uninstall`` is a no-op so that outer chain stays intact.

    Usable as a context manager (recommended) or via explicit
    ``install()`` / ``uninstall()``::

        with HttpClientPrintRedactor():
            ...  # asana SDK calls or any HTTP traffic with debuglevel=1

        r = HttpClientPrintRedactor()
        r.install()
        try:
            ...
        finally:
            r.uninstall()
    """

    def __init__(self, mask_fn: Callable[[str], str] | None = None) -> None:
        self._mask_fn: Callable[[str], str] = mask_fn or _default_mask_token
        self._wrapper: Callable[..., None] | None = None

    def install(self) -> None:
        """Patch ``http.client.print`` to redact Authorization values.

        Idempotent across instances of this class: if the current
        ``http.client.print`` is already a wrapper produced by this
        class, ``install`` is a no-op (the existing chain stays in
        place and this instance does not take ownership). Otherwise
        wraps whatever is there — preserving any unrelated patch from
        another library.
        """
        current = http.client.__dict__.get("print")
        if getattr(current, "_asana_cli_redactor", False):
            return
        inner: Callable[..., Any] = current if callable(current) else builtins.print
        mask_fn = self._mask_fn

        def _redact_match(m: re.Match[str]) -> str:
            return f"{m.group(1)} {mask_fn(m.group(2))}"

        def _redact_print(*args: Any, **kwargs: Any) -> None:
            if len(args) >= 2 and args[0] == "send:" and _looks_like_request_headers(str(args[1])):
                args = (args[0],) + tuple(
                    _AUTH_HEADER_RE.sub(_redact_match, str(a)) for a in args[1:]
                )
            inner(*args, **kwargs)

        _redact_print._asana_cli_redactor = True  # type: ignore[attr-defined]
        _redact_print._asana_cli_inner = inner  # type: ignore[attr-defined]
        http.client.print = _redact_print  # pyright: ignore[reportAttributeAccessIssue]
        self._wrapper = _redact_print

    def uninstall(self) -> None:
        """Restore the previous ``http.client.print``.

        Only acts if our wrapper is still the top-of-stack value. If
        another patch was installed on top of us, leaves the chain
        alone — uninstalling in that case would either drop the outer
        patch (data loss for that library) or leave its
        ``_asana_cli_inner`` reference dangling.

        Safe to call multiple times: subsequent calls are no-ops.
        """
        wrapper = self._wrapper
        if wrapper is None:
            return
        current = http.client.__dict__.get("print")
        if current is wrapper:
            inner = getattr(wrapper, "_asana_cli_inner", builtins.print)
            if inner is builtins.print:
                http.client.__dict__.pop("print", None)
            else:
                http.client.print = inner  # pyright: ignore[reportAttributeAccessIssue]
        self._wrapper = None

    def __enter__(self) -> HttpClientPrintRedactor:
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
        raw = sys.stdin.read()
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

        self._redactor: HttpClientPrintRedactor | None = None
        if runtime.debug:
            # The SDK debug setter enables http.client.HTTPConnection.debuglevel
            # and bumps the urllib3/asana loggers to DEBUG. The only path that
            # leaks the Authorization header is http.client's wire-level
            # ``print()`` calls — the SDK's own loggers do not log headers.
            # Install the redactor AFTER the SDK setup so we wrap whatever
            # http.client.print is at that point.
            config.debug = True
            self._redactor = HttpClientPrintRedactor()
            self._redactor.install()

        try:
            self._config = config
            self._client = asana.ApiClient(config)
            # Configuration has no --timeout knob, so wrap call_api to inject it.
            if runtime.timeout is not None:
                self._install_timeout(runtime.timeout)
        except Exception:
            # If construction fails after the redactor was installed, the
            # caller never gets a session to call close() on, so undo the
            # global http.client.print patch here rather than leaving it
            # leaked for the rest of the process.
            if self._redactor is not None:
                self._redactor.uninstall()
                self._redactor = None
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
        """Uninstall the debug redactor (if any).

        Safe to call multiple times. Prefer using the session as a
        context manager (``with AsanaSession(...) as session: ...``)
        which calls ``close`` automatically.
        """
        if self._redactor is not None:
            self._redactor.uninstall()
            self._redactor = None

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
