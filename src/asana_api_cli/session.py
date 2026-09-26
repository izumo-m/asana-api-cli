"""Asana SDK client construction utilities.

A thin wrapper around the official `asana` SDK ApiClient that handles
initialization from environment variables, toggling pagination mode, and
applying the global configuration passed in from the CLI.
"""

from __future__ import annotations

import http.client
import logging
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import asana
import click

from asana_api_cli.redactor import _BASIC_MASK, HttpClientAuthRedactor, _default_mask_token

ACCESS_TOKEN_ENV = "ASANA_ACCESS_TOKEN"


@dataclass
class _Runtime:
    """Configuration shared globally during a CLI invocation.

    Updated by the global-option mixins in ``click_ext`` (``_consume_global_options``).
    Each non-flag scalar defaults to ``None`` so ``AsanaSession`` can tell
    "user did not pass the flag" from "user explicitly chose this value" and
    leave the SDK default in place for the former.
    """

    debug: bool = False
    # CLI-only code-generation mode (``--generate-python``): when set, ``cli.py``
    # returns the collected ``CallPlan`` and ``formatter.py`` renders it as
    # standalone Python instead of executing the call. Consumed via the same
    # ``_consume_global_options`` path as ``--debug``.
    generate_python: bool = False
    host: str | None = None
    proxy: str | None = None
    verify_ssl: bool | None = None
    ssl_ca_cert: str | None = None
    access_token: str | None = None
    temp_folder_path: str | None = None
    logger_format: str | None = None
    logger_file: str | None = None
    cert_file: str | None = None
    key_file: str | None = None
    assert_hostname: bool | None = None
    connection_pool_maxsize: int | None = None
    safe_chars_for_path_param: str | None = None
    retry_strategy_overrides: dict[str, Any] | None = None
    # ApiClient-instance settings (not Configuration knobs): applied to the
    # ``ApiClient`` after construction in ``AsanaSession.__init__`` via
    # ``user_agent`` / ``set_default_header``. Session-wide — they ride every
    # request, unlike the per-call ``--header-params`` opt.
    user_agent: str | None = None
    default_headers: dict[str, str] | None = None
    # Configuration-backed iterator knobs. The per-call kwargs
    # (full_payload / item_limit / header_params / _request_timeout) are NOT
    # here: they are per-command options forwarded by ``cli.py:_make_command``.
    # The CLI-only output-formatting options (--output / --query / --csv-bom and
    # their error-path twins --exception-output / --exception-query) are likewise
    # not here: they flow as kwargs through ``formatter.py:formatted``.
    return_page_iterator: bool | None = None
    page_limit: int | None = None


runtime = _Runtime()


# Configuration knobs applied from ``runtime`` onto ``asana.Configuration`` in
# ``AsanaSession.__init__``. Each entry is ``(attr, apply_when_truthy)``: the
# Configuration attribute name matches the ``_Runtime`` field 1:1, and the
# condition follows each knob's SDK semantics.
# ``apply_when_truthy`` is True for only the four host/path strings where an
# empty value is meaningless (``host`` / ``proxy`` / ``ssl_ca_cert`` /
# ``temp_folder_path``, skipped when empty); it is False for every other field —
# including the path-like ``cert_file`` / ``key_file`` / ``logger_file`` — which
# apply on "is not None" (so an explicit ``False`` / ``0`` still applies).
# ``access_token`` (set from the constructor argument), the ``ApiClient``-instance
# settings (``user_agent`` / ``default_headers``), and ``retry_strategy`` (which
# transforms the value via ``Retry.new()``) are applied separately, not here.
_CONFIG_KNOBS: tuple[tuple[str, bool], ...] = (
    ("return_page_iterator", False),
    ("page_limit", False),
    ("host", True),
    ("proxy", True),
    ("verify_ssl", False),
    ("ssl_ca_cert", True),
    ("temp_folder_path", True),
    ("logger_format", False),
    ("logger_file", False),
    ("cert_file", False),
    ("key_file", False),
    ("assert_hostname", False),
    ("connection_pool_maxsize", False),
    ("safe_chars_for_path_param", False),
)


class AsanaSession:
    """Session holding an ApiClient from the official asana SDK.

    Use it as a context manager. The global side effects — the ``--debug``
    HTTP-log redactor, the ``http.client`` debuglevel flip, and the asana/urllib3
    logger levels the SDK's debug setter raises — are installed on ``__enter__``
    and reversed on
    ``__exit__``, so they are in effect only for the duration of the ``with``
    block. Constructing a session without entering it builds a plain ApiClient
    and mutates no process globals.

    Internal class: reached only through the CLI's
    ``with AsanaSession.from_env() as session:`` and carrying no stability
    guarantee (see ``docs/principles.md``).
    """

    def __init__(self, token: str) -> None:
        config = asana.Configuration()
        config.access_token = token

        # Apply runtime values to Configuration from the ``_CONFIG_KNOBS`` table
        # (module level). Each knob's Configuration attribute matches its
        # ``_Runtime`` field name, and ``apply_when_truthy`` encodes each knob's
        # condition (truthy for path/host-like
        # strings, "is not None" otherwise — so an explicit ``verify_ssl=False``
        # still applies, while an empty ``--ssl-ca-cert`` is skipped). Unspecified
        # values leave the SDK default in place (e.g. return_page_iterator / page_limit
        # at True / 100). ``setattr`` keeps the few attributes python-asana does
        # not type-annotate ignore-free.
        for attr, apply_when_truthy in _CONFIG_KNOBS:
            value = getattr(runtime, attr)
            applies = value if apply_when_truthy else value is not None
            if applies:
                setattr(config, attr, value)
        if runtime.retry_strategy_overrides is not None:
            # Start from the SDK's default Retry instance so unspecified
            # fields keep their python-asana defaults (e.g. total=5,
            # backoff_factor=2, status_forcelist=[429,500,502,503,504]).
            # An empty dict (e.g. `--retry-strategy '{}'`) yields a copy
            # with no field overridden — semantically a no-op, but still
            # honored as "user did pass the flag".
            base_retry = config.retry_strategy
            config.retry_strategy = base_retry.new(**runtime.retry_strategy_overrides)

        # Global side effects (the debug redactor, the ``http.client`` debuglevel
        # flip, and the asana/urllib3 logger levels) are installed by ``open()``
        # (on ``__enter__``) and reversed by ``close()`` (on
        # ``__exit__``), so merely constructing a session never mutates process
        # globals. ApiClient construction stays here because it touches no
        # globals — a failure just propagates with nothing to unwind.
        # Pre-initialize the redactor handle to ``None`` so ``open()``'s cleanup
        # and ``close()`` can tell whether it actually got installed.
        self._config = config
        self._redactor: HttpClientAuthRedactor | None = None
        # Prior ``http.client.HTTPConnection.debuglevel`` and the prior levels
        # of the asana/urllib3 loggers, captured by ``open()`` only when this
        # session turns debug on (the SDK ``debug`` setter raises both).
        # ``None`` means "this session did not touch the globals", so cleanup
        # leaves them alone.
        self._prev_debuglevel: int | None = None
        self._prev_logger_levels: dict[logging.Logger, int] | None = None
        self._client = asana.ApiClient(config)

        # ApiClient-instance settings (not Configuration knobs) applied after
        # construction. ``default_headers`` first, then ``user_agent`` last, so
        # the dedicated ``--user-agent`` wins over a ``--set-default-header`` that
        # also targets ``User-Agent`` (both write ``default_headers['User-Agent']``).
        if runtime.default_headers:
            for name, value in runtime.default_headers.items():
                self._client.set_default_header(name, value)
        if runtime.user_agent is not None:
            self._client.user_agent = runtime.user_agent

    @property
    def client(self) -> asana.ApiClient:
        return self._client

    def open(self) -> None:
        """Install this session's global side effects under ``--debug``: the
        ``http.client`` debuglevel flip, the asana/urllib3 logger levels the SDK
        debug setter raises, and the ``Authorization`` redactor. The reverse of
        :meth:`close`; ``__enter__`` calls it so the globals live only for the
        ``with`` block.

        If a later ``install()`` raises after an earlier one already
        succeeded, every side effect installed so far is reversed (via
        ``close``) before the error propagates. ``__enter__`` cannot defer this
        to ``__exit__``: Python skips ``__exit__`` when ``__enter__`` raises.
        """
        try:
            if runtime.debug:
                # ``config.debug = True`` has two process-global side effects:
                # it flips ``http.client.HTTPConnection.debuglevel`` to 1 (the
                # wire-level ``print()`` tracing — the only path that can leak
                # the Authorization header) and it raises the asana/urllib3
                # loggers to DEBUG. Capture BOTH so ``close()`` restores them;
                # otherwise debuglevel stays 1 after the redactor is uninstalled
                # (a later non-debug session would print the raw header) and the
                # loggers stay at DEBUG for the rest of the process. Install the
                # redactor AFTER the SDK debug setup so it wraps whatever
                # ``http.client.print`` is by then; ``close()`` turns tracing
                # back off before removing the mask so the two are never out of
                # step (constitution #2).
                self._prev_debuglevel = http.client.HTTPConnection.debuglevel
                self._prev_logger_levels = {
                    lg: lg.level
                    for lg in self._config.logger.values()  # pyright: ignore[reportAttributeAccessIssue]
                }
                self._config.debug = True
                self._redactor = HttpClientAuthRedactor()
                self._redactor.install()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Reverse every global side effect :meth:`open` installed: the
        ``http.client`` debuglevel flip and the asana/urllib3 logger levels the
        SDK debug setter raised, and the ``Authorization`` redactor.

        Safe to call multiple times, and a no-op if :meth:`open` never ran.
        Prefer using the session as a context manager
        (``with AsanaSession(...) as session: ...``) which pairs ``open`` on
        entry with ``close`` on exit automatically.
        """
        # Turn wire-level tracing OFF first — restore the debuglevel (and the
        # asana/urllib3 logger levels the SDK debug setter raised) — and only
        # THEN remove the Authorization mask, so there is never a window where
        # tracing is on without the mask (constitution #2).
        if self._prev_debuglevel is not None:
            http.client.HTTPConnection.debuglevel = self._prev_debuglevel
            self._prev_debuglevel = None
        if self._prev_logger_levels is not None:
            for logger, level in self._prev_logger_levels.items():
                logger.setLevel(level)
            self._prev_logger_levels = None
        if self._redactor is not None:
            self._redactor.uninstall()
            self._redactor = None

    def __enter__(self) -> AsanaSession:
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @classmethod
    def from_env(cls) -> AsanaSession:
        """Build a session from runtime.access_token, falling back to $ASANA_ACCESS_TOKEN."""
        token = _resolved_token()
        if not token:
            click.echo(
                f"Access token is not set. Pass --access-token or set {ACCESS_TOKEN_ENV}.",
                err=True,
            )
            sys.exit(2)
        return cls(token=token)


def _resolved_token() -> str:
    """The access token a session uses: ``--access-token``, else ``$ASANA_ACCESS_TOKEN``."""
    return runtime.access_token or os.environ.get(ACCESS_TOKEN_ENV, "")


# ---------- credential masking for error output -----------------------------
#
# Constitution #2. An SDK / urllib3 / http.client exception can quote a
# credential verbatim: a token with a stray CR/LF (e.g. read from a CRLF file)
# makes http.client raise ``Invalid header value b'Bearer <token>\r'``, and an
# unparsable ``--proxy`` / ``--host`` URL is echoed whole by urllib3's
# ``LocationParseError``. ``mask_credentials`` masks every credential this
# invocation holds before such text is shown, with the ``--debug`` trace's
# presentation (``...<last 6>``; ``Basic`` fully masked; a URL password as
# ``***``).

_AUTH_HEADER_NAMES = frozenset({"authorization", "proxy-authorization"})

# ``scheme://user:password@`` anywhere in free text — a fallback for a URL the
# known-value pass below cannot match (e.g. one urllib3 re-renders). Bounded by
# whitespace and quotes because the URL is embedded in a message.
_URL_PASSWORD_RE = re.compile(r"""([A-Za-z][A-Za-z0-9+.-]*://[^\s'"/@:]*):[^\s'"/@]*@""")

# A shorter value is not worth masking — it cannot be a real credential of the
# kind masked here, and replacing it would mangle unrelated text.
_MIN_MASKED_LEN = 8


def _mask_auth_value(value: str) -> str:
    """Mask an ``Authorization`` / ``Proxy-Authorization`` value like the
    ``--debug`` trace: the ``Bearer`` / ``Basic`` scheme stays visible and a
    ``Basic`` credential (base64 of ``user:password``) is fully masked."""
    scheme, sep, credential = value.partition(" ")
    if sep and scheme.lower() == "basic":
        return f"{scheme} {_BASIC_MASK}"
    if sep and scheme.lower() == "bearer":
        return f"{scheme} {_default_mask_token(credential.strip())}"
    return _default_mask_token(value)


def _credential_masks(header_params: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    """``(secret, masked)`` pairs for every credential this invocation holds:
    the access token, ``(Proxy-)Authorization`` header values (session-wide and
    per call), and the password in the ``--proxy`` / ``--host`` URL. Values are
    matched without surrounding whitespace — a stray CR/LF is exactly what makes
    http.client quote them, and it shows up escaped in the message."""
    masks: list[tuple[str, str]] = []
    token = _resolved_token().strip()
    if len(token) >= _MIN_MASKED_LEN:
        masks.append((token, _default_mask_token(token)))
    for headers in (runtime.default_headers, header_params):
        for name, value in (headers or {}).items():
            if name.lower() in _AUTH_HEADER_NAMES and isinstance(value, str):
                value = value.strip()
                if len(value) >= _MIN_MASKED_LEN:
                    masks.append((value, _mask_auth_value(value)))
    for url in (runtime.proxy, runtime.host):
        try:
            parts = urlsplit(url or "")
            user, password = parts.username, parts.password
        except ValueError:  # e.g. an invalid port; the regex fallback still applies
            continue
        if password:
            masks.append((f"{user}:{password}@", f"{user}:***@"))
    # Longest first, so a value that contains another is masked whole.
    return sorted(masks, key=lambda m: len(m[0]), reverse=True)


def mask_credentials(text: str, header_params: Mapping[str, Any] | None = None) -> str:
    """*text* with every credential this invocation holds masked (see above).
    *header_params* are the per-call ``--header-params``, when known."""
    for secret, masked in _credential_masks(header_params):
        text = text.replace(secret, masked)
    return _URL_PASSWORD_RE.sub(r"\1:***@", text)
