"""The standard streams are UTF-8 before the CLI prints anything.

The root group's ``--help`` runs while its arguments are parsed, ahead of the
group's callback, and its text has non-ASCII characters. Each test runs the
real CLI in a subprocess with a code-page stdout, as on Japanese Windows when
stdout is redirected (a console is not affected: Python writes to it in UTF-16).
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


def _run(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-c", "from asana_api_cli.cli import main; main()", *argv],
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "cp932"},
        timeout=60,
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["tasks", "--help"],
        ["tasks", "get-task", "--help"],
    ],
)
def test_help_is_utf8_on_a_code_page_stdout(argv: list[str]) -> None:
    result = _run(argv)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert "Usage:" in result.stdout.decode("utf-8")


def test_root_help_keeps_its_non_ascii_text() -> None:
    assert "—" in _run(["--help"]).stdout.decode("utf-8")


def test_help_without_arguments_does_not_crash() -> None:
    # The exit code depends on the click version (0 before 8.2, 2 from then on).
    result = _run([])
    assert b"UnicodeEncodeError" not in result.stderr
    assert "Usage:".encode() in result.stdout + result.stderr
