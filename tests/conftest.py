"""Project-rootdir pytest hooks and the autouse fixtures shared across the whole test tree.

The autouse ``_reset_runtime`` fixture isolates the module-level ``runtime``
singleton between tests, and the session-wide ``_lazy_sdk_thread_pool`` keeps
the SDK's per-client thread pools from hanging the run. ``pytest_addoption``
registers the ``--live`` / ``--record`` flags so they show up in
``pytest --help`` regardless of which path the user collects, and
``pytest_configure`` validates their combinations and translates them into
pytest-recording's native options. The e2e test fixtures, vcr_config, masking
and templating remain in ``tests/e2e/conftest.py`` where their scope is
self-evident.

See ``tests/e2e/README.md`` for the full workflow.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from multiprocessing.pool import ThreadPool
from typing import Any

import asana.api_client
import pytest

from asana_api_cli.session import _Runtime, runtime


class _LazyThreadPool:
    """Stand-in for the ``ThreadPool`` each SDK ``ApiClient`` starts in its
    ``__init__``: the real pool starts only when something uses it."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._args = args
        self._kwargs = kwargs
        self._pool: ThreadPool | None = None

    def __getattr__(self, name: str) -> Any:
        # Reached for the rest of the pool API — ``apply_async`` under the SDK's
        # ``async_req``, which the CLI never passes.
        if self._pool is None:
            self._pool = ThreadPool(*self._args, **self._kwargs)
        return getattr(self._pool, name)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()

    def join(self) -> None:
        if self._pool is not None:
            self._pool.join()


@pytest.fixture(autouse=True, scope="session")
def _lazy_sdk_thread_pool() -> Iterator[None]:
    """Keep each SDK ``ApiClient`` from starting a thread pool nothing uses.

    ``ApiClient.__init__`` starts a ``multiprocessing.pool.ThreadPool`` (one
    worker per CPU plus three handler threads) and ``ApiClient.__del__`` closes
    and joins it. The suite builds hundreds of short-lived clients, most of them
    freed by the cyclic GC, which runs on whichever thread happens to allocate —
    pool threads included. When it runs on the worker-handler thread of the very
    pool it frees, the pool's finalizer waits for the task handler, which waits
    for a sentinel that only the now-blocked worker handler sends: the run hangs
    for good (seen on Windows, rarely). The same GC on another thread of that
    pool surfaces as a ``PytestUnraisableExceptionWarning`` with ``RuntimeError:
    cannot join current thread``. CPython does not support cleaning up a pool
    from ``__del__`` (bpo-39360). The CLI itself is not exposed: it builds one
    client per process and keeps it until the command ends.

    ``test_sdk_boilerplate.py::test_api_client_starts_no_pool_threads`` fails if
    an SDK bump makes this stand-in stop applying.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(asana.api_client, "ThreadPool", _LazyThreadPool)
        yield


@pytest.fixture(autouse=True)
def _reset_runtime() -> Iterator[None]:
    """Reset the module-level ``runtime`` singleton between tests.

    The Configuration-backed global flags (e.g. ``--page-limit`` /
    ``--return-page-iterator``) are written into ``runtime`` by
    ``_consume_global_options`` whenever a test invokes a CLI command with
    those flags. Without this fixture the value persists into the next test,
    producing order-dependent failures. (The per-call kwargs ``--item-limit``
    / ``--full-payload`` / ``--header-params`` / ``--request-timeout`` are
    per-command options forwarded directly to the SDK call, not ``runtime``
    state, so they cannot leak this way.)

    Snapshots all ``_Runtime`` fields up-front and restores them after
    each test so any field — including ones added in the future — gets
    rolled back automatically.
    """
    saved = {f.name: getattr(runtime, f.name) for f in dataclasses.fields(_Runtime)}
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(runtime, name, value)


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("asana-api-cli e2e")
    group.addoption(
        "--live",
        action="store_true",
        default=False,
        help=(
            "Run e2e tests against the real Asana API. Without it, tests "
            "replay from committed cassettes (default)."
        ),
    )
    group.addoption(
        "--record",
        action="store_true",
        default=False,
        help=(
            "Overwrite cassettes from the live API responses. Requires "
            "--live; --record on its own is a usage error."
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    record = config.getoption("--record", default=False)
    live = config.getoption("--live", default=False)
    if record and not live:
        raise pytest.UsageError("--record requires --live")

    # Reject combining our flags with pytest-recording's native ones —
    # they would set the same underlying options to potentially
    # different values and the result would be undefined.
    record_mode_native = config.getoption("--record-mode", default="none") or "none"
    disable_recording_native = config.getoption("--disable-recording", default=False)
    native_set = record_mode_native != "none" or disable_recording_native
    if (live or record) and native_set:
        raise pytest.UsageError(
            "--live / --record cannot be combined with --record-mode / "
            "--disable-recording. Use one set or the other."
        )

    # Translate our flags into pytest-recording's native options so the
    # vcrpy fixture wires itself accordingly.
    if record:
        config.option.record_mode = "all"
    elif live:
        config.option.disable_recording = True
