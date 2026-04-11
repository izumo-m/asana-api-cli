"""Asana SDK client construction utilities.

A thin wrapper around the official `asana` SDK ApiClient that handles
initialization from environment variables, toggling pagination mode, and
applying the global configuration passed in from the CLI.
"""
from __future__ import annotations

import functools
import os
import sys
from dataclasses import dataclass
from typing import Any

import asana
from urllib3.util.retry import Retry

DEFAULT_TOKEN_ENV = "ASANA_ACCESS_TOKEN"


@dataclass
class _Runtime:
    """Configuration shared globally during a CLI invocation. Updated by the main group callback."""

    debug: bool = False
    host: str | None = None
    proxy: str | None = None
    verify_ssl: bool = True
    ssl_ca_cert: str | None = None
    page_limit: int | None = None
    retries: int | None = None
    timeout: float | None = None
    token_env: str = DEFAULT_TOKEN_ENV
    temp_dir: str | None = None


runtime = _Runtime()


class AsanaSession:
    """Session that holds an ApiClient from the official asana SDK."""

    def __init__(self, token: str, *, paginate: bool = False) -> None:
        config = asana.Configuration()
        config.access_token = token
        # When --paginate is set, the SDK returns a PageIterator that walks every page.
        config.return_page_iterator = paginate

        # Apply runtime values to Configuration
        if runtime.host:
            config.host = runtime.host
        if runtime.proxy:
            config.proxy = runtime.proxy
        if not runtime.verify_ssl:
            config.verify_ssl = False
        if runtime.ssl_ca_cert:
            config.ssl_ca_cert = runtime.ssl_ca_cert
        if runtime.page_limit is not None:
            config.page_limit = runtime.page_limit
        if runtime.temp_dir:
            config.temp_folder_path = runtime.temp_dir
        if runtime.retries is not None:
            # Replace only `total` while keeping the existing backoff/status_forcelist.
            config.retry_strategy = Retry(
                total=runtime.retries,
                backoff_factor=2,
                status_forcelist=[429, 500, 502, 503, 504],
            )
        if runtime.debug:
            # The SDK debug setter attaches a stderr handler to the urllib3/asana
            # loggers and enables http.client.HTTPConnection.debuglevel.
            config.debug = True

        self._config = config
        self._client = asana.ApiClient(config)

        # Configuration has no --timeout knob, so wrap call_api to inject it.
        if runtime.timeout is not None:
            self._install_timeout(runtime.timeout)

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

    def api(self, api_class: type) -> object:
        """Take a <Tag>Api class and return an instance bound to this session."""
        return api_class(self._client)

    @classmethod
    def from_env(cls, *, paginate: bool = False) -> "AsanaSession":
        """Build a session from environment variables (variable name from runtime.token_env)."""
        var = runtime.token_env or DEFAULT_TOKEN_ENV
        token = os.environ.get(var, "")
        if not token:
            print(f"{var} environment variable is not set", file=sys.stderr)
            sys.exit(1)
        return cls(token=token, paginate=paginate)
