"""Project-rootdir pytest hooks shared across the whole test tree.

Only ``pytest_addoption`` / ``pytest_configure`` live here so the
``--live`` / ``--record`` flags show up in ``pytest --help`` regardless
of which path the user collects. Test fixtures, vcr_config, masking and
templating remain in ``tests/e2e/conftest.py`` where their scope is
self-evident.

See ``tests/e2e/README.md`` for the full workflow.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator

import pytest

from asana_api_cli.session import _Runtime, runtime


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
