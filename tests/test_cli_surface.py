"""Snapshot test for the runtime-introspected CLI surface.

The CLI is built dynamically from whatever ``asana`` SDK version is installed,
so a bump of the ``asana`` dependency can silently change every command.
This test pins the surface to ``tests/fixtures/cli_surface.json``: any
addition, removal, rename, or signature change shows up as a fixture diff
that must be reviewed (and recorded in CHANGELOG) before merging.

To regenerate after an intentional SDK bump::

    uv run python -c "import json; from asana_api_cli.cli import \
        introspect_to_manifest; \
        json.dump(introspect_to_manifest(), \
        open('tests/fixtures/cli_surface.json', 'w'), \
        indent=2, sort_keys=True); \
        open('tests/fixtures/cli_surface.json', 'a').write(chr(10))"
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asana_api_cli.cli import introspect_to_manifest


FIXTURE = Path(__file__).parent / "fixtures" / "cli_surface.json"


@pytest.fixture(scope="module")
def expected() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.fixture(scope="module")
def actual() -> dict:
    return introspect_to_manifest()


def _commands_index(manifest: dict) -> dict[str, dict]:
    """Flatten manifest to {f'{group}/{command}': params_dict}."""
    index: dict[str, dict] = {}
    for g in manifest["groups"]:
        for c in g["commands"]:
            index[f"{g['group']}/{c['command']}"] = c
    return index


class TestSurfaceSnapshot:
    def test_group_set_matches(self, actual: dict, expected: dict) -> None:
        actual_groups = {g["group"] for g in actual["groups"]}
        expected_groups = {g["group"] for g in expected["groups"]}
        added = actual_groups - expected_groups
        removed = expected_groups - actual_groups
        assert not added and not removed, (
            f"Group set drift: added={sorted(added)} removed={sorted(removed)}. "
            "If intentional after an SDK bump, regenerate the fixture."
        )

    def test_command_set_matches(self, actual: dict, expected: dict) -> None:
        actual_cmds = set(_commands_index(actual))
        expected_cmds = set(_commands_index(expected))
        added = actual_cmds - expected_cmds
        removed = expected_cmds - actual_cmds
        assert not added and not removed, (
            f"Command set drift: added={sorted(added)[:10]} "
            f"removed={sorted(removed)[:10]}. "
            "If intentional after an SDK bump, regenerate the fixture."
        )

    def test_full_manifest_matches(self, actual: dict, expected: dict) -> None:
        # Deep-compare so option signatures (params, types, required) are
        # also covered.
        assert actual == expected, (
            "CLI surface differs from the recorded fixture. "
            "Review the diff; if the SDK bump is intentional, regenerate "
            "tests/fixtures/cli_surface.json and note the change in "
            "CHANGELOG.md."
        )
