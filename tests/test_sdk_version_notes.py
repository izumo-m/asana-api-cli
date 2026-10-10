"""Guard the version-pinned SDK notes in ``docs/``.

Some statements describe ``python-asana`` behavior as of the newest SDK checked
and carry the tag ``X.Y.Z — the latest version checked``. Once an SDK bump moves
``_SNAPSHOT_ASANA_VERSION``, these tests fail until every tagged note has been
re-checked against the new SDK and names its version — see
``docs/development.md`` §Bumping the asana SDK, step 7. Unlike the snapshot
tests they do not depend on the installed SDK, so they never skip.
"""

from __future__ import annotations

import re
from pathlib import Path

from test_cli_surface import _SNAPSHOT_ASANA_VERSION as SURFACE_VERSION
from test_generate_python_snapshots import _SNAPSHOT_ASANA_VERSION as GENERATE_VERSION

ROOT = Path(__file__).resolve().parent.parent

# The tag, with the version it pins when present (the text may wrap).
_TAG = re.compile(r"(?:(\d+\.\d+\.\d+)\s+—\s+)?the\s+latest\s+version\s+checked")

# development.md describes the tag (step 7) rather than using it.
_NOT_NOTES = {Path("docs/development.md")}


def _tagged_notes() -> list[tuple[str, str | None]]:
    """``(file:line, version)`` for every tag under ``docs/``; ``None`` if it names none."""
    notes: list[tuple[str, str | None]] = []
    for path in sorted((ROOT / "docs").rglob("*.md")):
        rel = path.relative_to(ROOT)
        if rel in _NOT_NOTES:
            continue
        text = path.read_text(encoding="utf-8")
        for m in _TAG.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            notes.append((f"{rel.as_posix()}:{line}", m.group(1)))
    return notes


def test_snapshot_versions_agree() -> None:
    assert SURFACE_VERSION == GENERATE_VERSION, (
        "_SNAPSHOT_ASANA_VERSION differs between test_cli_surface.py and "
        "test_generate_python_snapshots.py; bump both "
        "(docs/development.md §Bumping the asana SDK, step 2)"
    )


def test_tagged_notes_name_the_snapshot_version() -> None:
    notes = _tagged_notes()
    assert notes, "no 'X.Y.Z — the latest version checked' tag found under docs/"
    stale = [f"{where} ({ver or 'no version'})" for where, ver in notes if ver != SURFACE_VERSION]
    assert not stale, (
        f"version-pinned SDK notes not yet re-checked for python-asana {SURFACE_VERSION}: "
        f"{stale}. Re-verify each one (docs/development.md §Bumping the asana SDK, "
        "step 7), then make it name the new version."
    )


def test_src_has_no_version_pinned_notes() -> None:
    """Such notes live in ``docs/`` only: ``--generate-python`` inlines some
    ``src/`` docstrings into users' scripts."""
    hits = [
        path.relative_to(ROOT).as_posix()
        for path in sorted((ROOT / "src").rglob("*.py"))
        if re.search(r"latest\s+version\s+checked", path.read_text(encoding="utf-8"))
    ]
    assert not hits, f"move these SDK version notes to docs/ and link them instead: {hits}"
