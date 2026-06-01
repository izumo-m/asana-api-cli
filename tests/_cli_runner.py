"""Click version compat helpers for the test suite.

Click 8.2 removed the ``mix_stderr`` knob on ``CliRunner`` and always
separates stdout / stderr. On click 8.0 / 8.1 the default ``CliRunner()``
folds stderr into stdout and ``result.stderr`` raises ``ValueError``.
``result.output`` also drifted:

* click <8.2 + ``mix_stderr=True`` (default): ``output`` is ``stdout``
  (with stderr mixed in)
* click <8.2 + ``mix_stderr=False``: ``output`` is ``stdout`` only
* click 8.2+: ``output`` is ``stdout`` and ``stderr`` interleaved in write order (always)

This module hides both differences so tests can be written once and pass on
the full declared support range (``click>=8.0``).
"""

from __future__ import annotations

import inspect
from typing import Any

from click.testing import CliRunner

_HAS_MIX_STDERR_KWARG = "mix_stderr" in inspect.signature(CliRunner.__init__).parameters


def make_runner() -> CliRunner:
    """A ``CliRunner`` with separated stdout / stderr on every click version.

    On click 8.2+ this is just ``CliRunner()`` (streams are always separate).
    On click 8.0 / 8.1 we opt into ``mix_stderr=False`` so ``result.stderr``
    works and ``result.stdout`` does not include the error stream.
    """
    if _HAS_MIX_STDERR_KWARG:
        # mix_stderr was removed in click 8.2; pyright sees the 8.3 stubs
        # and flags the kwarg here, but this branch is dead on 8.2+.
        return CliRunner(mix_stderr=False)  # type: ignore[call-arg]
    return CliRunner()


def full_output(result: Any) -> str:
    """``stdout`` then ``stderr`` of a ``CliRunner`` ``Result``, on every click version.

    Always concatenates stdout before stderr (unlike click 8.2+'s
    ``Result.output``, which interleaves the two streams in write order) — a
    fixed order is fine for existence checks like
    ``assert "x" in full_output(result)``. Tests that want to assert against
    everything the CLI emitted — including click's own ``Error: ...`` lines that
    go to stderr — should use this instead of ``result.output``.
    """
    return result.stdout + result.stderr
