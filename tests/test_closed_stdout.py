"""The CLI's behavior when stdout's reader goes away (``| head``, quitting ``| less``).

Each test runs the real CLI in a subprocess whose stdout is a pipe with its
read end already closed, against a local fake API server. stdout is unbuffered
(``PYTHONUNBUFFERED``), so the very first write fails — under ``--debug`` that
is the first request's ``send:`` trace line, which used to leave the request
unsent while urllib3 waited forever for its response (buffered, the line that
hits the full buffer is a matter of chance). Every such run must
end promptly with click's quiet exit ``1`` — no traceback, and no
"Exception ignored ... BrokenPipeError" from a flush at interpreter shutdown
(which would also turn the exit code into ``120``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.parse
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

_PAGES = 3


class _Api(BaseHTTPRequestHandler):
    """``/workspaces`` paginates one item per page; any other path is a 404."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any, **kwargs: Any) -> None:
        return

    def do_GET(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        if not url.path.endswith("/workspaces"):
            self._send(404, {"errors": [{"message": "Not Found"}]})
            return
        offset = int(urllib.parse.parse_qs(url.query).get("offset", ["0"])[0])
        next_page = None
        if offset + 1 < _PAGES:
            next_page = {"offset": str(offset + 1), "path": url.path, "uri": "x"}
        self._send(200, {"data": [{"gid": str(offset), "name": "w"}], "next_page": next_page})

    def _send(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def host() -> Iterator[str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Api)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/api/1.0"
    finally:
        srv.shutdown()
        srv.server_close()


def _run_with_closed_stdout(
    argv: list[str], *, unbuffered: bool = True
) -> subprocess.CompletedProcess[str]:
    read_end, write_end = os.pipe()
    os.close(read_end)  # the reader is gone before the first write
    try:
        return subprocess.run(
            [sys.executable, "-c", "from asana_api_cli.cli import main; main()", *argv],
            stdout=write_end,
            stderr=subprocess.PIPE,
            text=True,
            env={
                **{k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"},
                "ASANA_ACCESS_TOKEN": "dummy-token-1234567890",
                **({"PYTHONUNBUFFERED": "1"} if unbuffered else {}),
            },
            timeout=60,  # the --debug case used to hang forever
        )
    finally:
        os.close(write_end)


@pytest.mark.parametrize(
    "args",
    [
        ["workspaces", "get-workspaces"],
        ["workspaces", "get-workspaces", "--output", "csv"],
        ["workspaces", "get-workspaces", "--output", "none"],
        ["--debug", "workspaces", "get-workspaces"],
        ["--debug", "workspaces", "get-workspaces", "--output", "none"],
        ["--debug", "tasks", "get-task", "--task", "1"],
        ["--debug", "tasks", "get-task", "--task", "1", "--exception-output", "json"],
    ],
    ids=lambda args: " ".join(args),
)
def test_closed_stdout_exits_quietly(host: str, args: list[str]) -> None:
    result = _run_with_closed_stdout(["--host", host, *args])
    # ``--output none`` has nothing left to write: the (unbuffered) trace lines
    # were dropped as they failed, so it is a plain success.
    expected = 0 if args[-1] == "none" else 1
    assert result.returncode == expected, result.stderr
    assert "Traceback" not in result.stderr
    assert "Exception ignored" not in result.stderr


def test_buffered_trace_under_output_none(host: str) -> None:
    # Buffered, the wire-trace lines wait in stdout's buffer for a flush that
    # fails. That flush now happens before exiting, where click turns it into a
    # quiet exit 1 — not at interpreter shutdown (a stderr message, exit 120).
    args = ["--host", host, "--debug", "workspaces", "get-workspaces", "--output", "none"]
    result = _run_with_closed_stdout(args, unbuffered=False)
    assert result.returncode == 1, result.stderr
    assert "Exception ignored" not in result.stderr
