"""Guards for README statements that must track ``pyproject.toml``."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_pipx_sdk_upgrade_keeps_supported_upper_bound() -> None:
    # The README's pipx SDK-upgrade command pins the same upper bound as our
    # ``asana`` dependency: a bare ``pip install -U asana`` ignores it and would
    # install an unsupported major version. When the bound moves, update both.
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'"asana>=[^",]+,(<[^"]+)"', pyproject)
    assert m is not None, "asana dependency with an upper bound not found in pyproject.toml"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f'pipx runpip asana-api-cli install -U "asana{m.group(1)}"' in readme
