"""End-to-end attachment upload tests.

Five cases combining file content (ASCII text / Japanese text / binary)
with filename variants (ASCII / Japanese). Asana derives the stored
attachment name from the multipart ``Content-Disposition`` filename.

Non-ASCII filenames require the ``--multibyte-filenames`` global flag,
which makes the CLI emit the RFC 5987 ``filename*=UTF-8''<percent-encoded>``
parameter. The upstream SDK does not emit it (default behavior), so
without the flag Asana stores the literal Latin-1 of the UTF-8 bytes
(mojibake). The flag is off by default to preserve strict SDK parity;
the Japanese-filename test cases below pass it explicitly.

Live record::

    ASANA_PYTEST_ENABLE_E2E=1 ASANA_PYTEST_WORKSPACE=<gid> \\
        uv run pytest --record-mode=all tests/e2e/test_attachments.py

Replay::

    ASANA_PYTEST_ENABLE_E2E=1 ASANA_PYTEST_WORKSPACE=<gid> \\
        uv run pytest tests/e2e/test_attachments.py
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from asana_api_cli.cli import main

from _cli_runner import make_runner

_DATA_DIR = Path(__file__).parent / "data"


def _run(*args: str) -> "tuple[int, str, str]":
    result = make_runner().invoke(main, list(args))
    return result.exit_code, result.stdout, result.stderr


ATTACHMENT_CASES = [
    pytest.param("ascii.txt", "ascii.txt", id="ascii"),
    pytest.param("japanese-text.txt", "japanese-text.txt", id="japanese_text"),
    pytest.param("small.png", "small.png", id="binary"),
    pytest.param("japanese-text.txt", "日本語テキスト.txt", id="japanese_filename_text"),
    pytest.param("small.png", "日本語画像.png", id="japanese_filename_binary"),
]


@pytest.mark.vcr
@pytest.mark.parametrize("source_name,upload_name", ATTACHMENT_CASES)
def test_attachment_round_trip(
    attachment_parent_task: str,
    created_attachments: list[str],
    tmp_path: Path,
    source_name: str,
    upload_name: str,
) -> None:
    """Upload, fetch, then delete an attachment under a temporary task."""
    upload_path = tmp_path / upload_name
    shutil.copy(_DATA_DIR / source_name, upload_path)

    # UPLOAD — opt in to RFC 5987 filename* encoding when the basename is
    # non-ASCII. ASCII basenames work with the default (SDK parity) path.
    needs_multibyte = any(ord(c) > 127 for c in upload_name)
    upload_args = ["attachments", "create-attachment-for-object"]
    if needs_multibyte:
        upload_args = ["--multibyte-filenames", *upload_args]
    code, out, _ = _run(
        *upload_args,
        "--parent",
        attachment_parent_task,
        "--file",
        str(upload_path),
    )
    assert code == 0, out
    attachment = json.loads(out)
    attachment_gid = attachment["gid"]
    created_attachments.append(attachment_gid)
    assert attachment["resource_type"] == "attachment"
    assert attachment["name"] == upload_name

    # GET — confirm the attachment is retrievable by gid and round-trips
    # the upload name verbatim (covers UTF-8 / RFC 5987 handling).
    code, out, _ = _run("attachments", "get-attachment", "--attachment", attachment_gid)
    assert code == 0, out
    fetched = json.loads(out)
    assert fetched["gid"] == attachment_gid
    assert fetched["name"] == upload_name

    # DELETE
    code, _, _ = _run("attachments", "delete-attachment", "--attachment", attachment_gid)
    assert code == 0
    created_attachments.remove(attachment_gid)
