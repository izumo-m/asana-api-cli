"""Static check that src/ stays Python 3.10 compatible.

Runs vermin against the package and fails if anything in src/ would
require a newer Python than 3.10 (e.g. 3.11+ stdlib imports like
``tomllib`` or syntax such as ``typing.Self``).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
VERMIN_BIN = Path(sys.executable).parent / "vermin"


@pytest.mark.skipif(
    not VERMIN_BIN.exists(),
    reason="vermin is not installed (dev dependency)",
)
def test_src_is_python_3_10_compatible() -> None:
    result = subprocess.run(
        [
            str(VERMIN_BIN),
            "--target=3.10-",
            "--violations",
            "--no-tips",
            "--eval-annotations",
            str(SRC_DIR),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "src/ contains code incompatible with Python 3.10.\n"
        f"--- vermin stdout ---\n{result.stdout}\n"
        f"--- vermin stderr ---\n{result.stderr}"
    )
