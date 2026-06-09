"""Redact ``Authorization`` headers from ``http.client``'s debug output.

When ``http.client.HTTPConnection.debuglevel`` is set to ``1`` — directly
or by an HTTP library that flips it on as part of its own debug mode —
the stdlib module emits a bare ``print("send:", repr(data))`` for every
chunk it flushes to the socket. The first such chunk is the request
headers block, which carries the ``Authorization`` header verbatim and
would leak the bearer / basic credential into stdout.

``HttpClientAuthRedactor`` patches ``http.client.print`` (the module-
level name that Python resolves before falling back to ``builtins.print``)
with a wrapper that masks the token portion of any ``Authorization``
header it sees in a request-headers chunk. Body chunks pass through
verbatim so user content that happens to contain an ``Authorization:``
substring (e.g. a debug log pasted into a task description) is never
rewritten.

This module has no third-party dependencies — it is intended to be
copied, as-is, into any project that needs the same redaction.
"""

from __future__ import annotations

import builtins
import http.client
import re
from collections.abc import Callable
from typing import Any

# ---------- module-level constants ------------------------------------------


# Match an ``Authorization`` / ``Proxy-Authorization`` header value inside an
# HTTP request-headers chunk (the latter appears in proxied requests and in
# the ``CONNECT`` tunnel-establishment chunk, which ``_HTTP_METHODS`` also
# covers). We never apply this regex to user-controlled data (request body or
# response body), so the value side just needs to terminate cleanly at the
# next header boundary: any run of non-backslash, non-CR/LF characters
# stops at real CR/LF (raw form) and at the leading ``\\`` of literal
# ``\\r\\n`` (``repr`` form). A ``Bearer`` / ``Basic`` scheme prefix is
# captured separately so it stays visible in the masked output; any other
# value — a custom scheme or a bare token — is masked whole. The value
# class is intentionally not tied to a specific token shape — many APIs
# declare that token formats are opaque and may change.
_AUTH_HEADER_RE = re.compile(
    r"((?:Proxy-)?Authorization:\s*)((?:Bearer|Basic)\s+)?([^\s\\][^\\\r\n]*)",
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

# Default mask reveals the last ``_MASK_SUFFIX_LEN`` characters of the
# token, but only when the token is at least ``_MASK_MIN_LEN`` long. The
# threshold caps the leak ratio at 6/16 = 37.5% for unexpectedly short
# tokens. ``Basic`` credentials never get the partial reveal (see
# ``_redact_match`` in ``install``): the value is base64 of
# ``user:password``, so even a tail reveal would expose password
# characters — unlike an opaque random token.
_MASK_MIN_LEN = 16
_MASK_SUFFIX_LEN = 6
_BASIC_MASK = "<REDACTED>"

# Marker / inner-function attributes set on the wrapper installed at
# ``http.client.print``. Used for idempotent install across multiple
# instances and for restoring the previous value on uninstall.
_MARKER_ATTR = "_http_client_auth_redactor"
_INNER_ATTR = "_http_client_auth_redactor_inner"


# ---------- helpers ---------------------------------------------------------


def _looks_like_request_headers(repr_bytes: str) -> bool:
    """Return True if *repr_bytes* is the ``repr()`` of an HTTP request-headers
    chunk, i.e. starts with ``b'<METHOD> `` (or ``b"<METHOD> ``).

    ``http.client.HTTPConnection.send()`` is called once per chunk it
    flushes. For typical requests that means one call for the headers
    block and zero or more calls for the body chunks. Only the headers
    chunk starts with a method line — body chunks start with whatever the
    body is (JSON ``{...}``, multipart boundary, raw text, etc.) and may
    legitimately contain the substring ``Authorization: Bearer ...`` as
    part of user content. Limiting redaction to the headers chunk
    guarantees the body is never touched.
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
    redaction so the leak ratio stays bounded.

    Format-agnostic: many APIs declare that token formats are opaque and
    may change without notice, so this function does not depend on any
    specific PAT / OAuth shape.
    """
    if len(token) < _MASK_MIN_LEN:
        return "<REDACTED>"
    return f"...{token[-_MASK_SUFFIX_LEN:]}"


# ---------- public class ----------------------------------------------------


class HttpClientAuthRedactor:
    """Context manager that redacts ``Authorization`` headers in
    ``http.client``'s wire-level debug output.

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

        with HttpClientAuthRedactor():
            ...  # any HTTP traffic with http.client.HTTPConnection.debuglevel = 1

        r = HttpClientAuthRedactor()
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
        if getattr(current, _MARKER_ATTR, False):
            return
        inner: Callable[..., Any] = current if callable(current) else builtins.print
        mask_fn = self._mask_fn

        def _redact_match(m: re.Match[str]) -> str:
            scheme = m.group(2) or ""
            # A Basic credential is fully redacted, bypassing ``mask_fn``:
            # the partial reveal is only safe for opaque random tokens.
            if scheme.rstrip().lower() == "basic":
                return f"{m.group(1)}{scheme}{_BASIC_MASK}"
            return f"{m.group(1)}{scheme}{mask_fn(m.group(3))}"

        def _redact_print(*args: Any, **kwargs: Any) -> None:
            if len(args) >= 2 and args[0] == "send:" and _looks_like_request_headers(str(args[1])):
                args = (args[0],) + tuple(
                    _AUTH_HEADER_RE.sub(_redact_match, str(a)) for a in args[1:]
                )
            inner(*args, **kwargs)

        setattr(_redact_print, _MARKER_ATTR, True)
        setattr(_redact_print, _INNER_ATTR, inner)
        http.client.print = _redact_print  # pyright: ignore[reportAttributeAccessIssue]
        self._wrapper = _redact_print

    def uninstall(self) -> None:
        """Restore the previous ``http.client.print``.

        Only acts if our wrapper is still the top-of-stack value. If
        another patch was installed on top of us, leaves the chain
        alone — uninstalling in that case would either drop the outer
        patch (data loss for that library) or leave its inner reference
        dangling.

        Safe to call multiple times: subsequent calls are no-ops.
        """
        wrapper = self._wrapper
        if wrapper is None:
            return
        current = http.client.__dict__.get("print")
        if current is wrapper:
            inner = getattr(wrapper, _INNER_ATTR, builtins.print)
            if inner is builtins.print:
                http.client.__dict__.pop("print", None)
            else:
                http.client.print = inner  # pyright: ignore[reportAttributeAccessIssue]
        self._wrapper = None

    def __enter__(self) -> HttpClientAuthRedactor:
        self.install()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.uninstall()
