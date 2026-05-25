"""End-to-end test for the ``webhooks`` group.

This test exercises the full create → list → trigger events → delete
lifecycle against the **real** Asana API. It is **live only** — vcrpy
records only the CLI→Asana direction, so the inline ``X-Hook-Secret``
handshake (Asana→target during ``POST /webhooks``) cannot be replayed
from a cassette. Running the same shape under replay would deadlock.

How the public tunnel is set up
-------------------------------

The webhook ``target`` URL must be reachable from the public internet
so Asana can deliver the handshake POST. This test uses **Cloudflare
Quick Tunnel** (``cloudflared tunnel --url http://127.0.0.1:<port>``)
and parses the ephemeral ``https://*.trycloudflare.com`` URL out of
cloudflared's startup output. The tunnel dies when cloudflared exits,
so cleanup is just ``proc.terminate()``.

Prerequisites:

- ``cloudflared`` binary on ``$PATH``. Debian/Ubuntu/WSL2 install::

      sudo mkdir -p --mode=0755 /usr/share/keyrings
      curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \\
        | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
      echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] \\
        https://pkg.cloudflare.com/cloudflared jammy main' \\
        | sudo tee /etc/apt/sources.list.d/cloudflared.list
      sudo apt update && sudo apt install -y cloudflared

  No Cloudflare account, no domain, no DNS record needed — Quick Tunnel
  publishes through Cloudflare's shared ``trycloudflare.com`` domain.

Environment variables:

    ASANA_ACCESS_TOKEN           personal access token (required)
    ASANA_PYTEST_WORKSPACE       workspace gid the test runs against
                                 (required)
    ASANA_PYTEST_WEBHOOK_TUNNEL  ``<provider>:<port>``. Currently only
                                 ``cloudflare-quick:<port>`` is wired
                                 up (e.g. ``cloudflare-quick:8765``).
                                 ``<port>`` is the local bind port for
                                 the in-process receiver. Anything else
                                 (unset, unknown provider, missing
                                 port) skips the module. Other provider
                                 names (e.g. ``cloudflare-named``,
                                 ``ngrok``) are reserved for future
                                 implementations.

Run::

    ASANA_PYTEST_WORKSPACE=<gid> \\
        ASANA_PYTEST_WEBHOOK_TUNNEL=cloudflare-quick:8765 \\
        uv run pytest tests/e2e/test_webhooks.py

Quick Tunnel publishes through Cloudflare's shared
``*.trycloudflare.com`` domain. Some networks block that hostname
pattern (the same reason ngrok gets blocked) or have DNS resolvers
that take a while to learn the freshly-minted hostname. If that
happens, switch to a named tunnel — see
``_local/notes/webhook-api/cloudflare-tunnel-setup.md``.

Asana webhook spec used by the assertions
-----------------------------------------

The webhook subscribes to the **workspace** (a "higher-level" resource
that requires ``filters``) with two filters: ``project/added`` and
``project/deleted``. Creating a project triggers an ``added`` event
whose ``resource`` is the new project; deleting it triggers a
``deleted`` event. Assertion is intentionally loose ("at least one
matching event arrived") so we don't break on Asana's batching or
on adjacent membership / story events that come alongside.

Full spec notes: ``_local/notes/webhook-api/asana-webhook-spec.md``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Generator
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from _cli_runner import make_runner

from asana_api_cli.cli import main

TUNNEL_ENV = "ASANA_PYTEST_WEBHOOK_TUNNEL"
TUNNEL_PROVIDER = "cloudflare-quick"
TUNNEL_VALUE_RE = re.compile(rf"^{re.escape(TUNNEL_PROVIDER)}:(\d+)$")
WORKSPACE_ENV = "ASANA_PYTEST_WORKSPACE"

QUICK_TUNNEL_URL_RE = re.compile(rb"https://[a-z0-9-]+\.trycloudflare\.com")
TUNNEL_STARTUP_TIMEOUT_SEC = 60
HEALTH_TIMEOUT_SEC = 60
HEALTH_PROBE_INTERVAL_SEC = 2
EVENT_WAIT_SEC = 15


def _parse_tunnel() -> int | None:
    """Return the receiver port from TUNNEL_ENV, or None to skip."""
    raw = os.environ.get(TUNNEL_ENV)
    if raw is None:
        return None
    m = TUNNEL_VALUE_RE.fullmatch(raw)
    if m is None:
        return None
    return int(m.group(1))


# ---------- receiver --------------------------------------------------------


class _Receiver(ThreadingHTTPServer):
    """In-process Asana webhook target.

    Handles the handshake (echo back ``X-Hook-Secret``) and accumulates
    every delivered event so the test can assert against them. Both
    fields are guarded by ``lock`` because event delivery POSTs land on
    worker threads.
    """

    handshake_secret: str | None
    received_events: list[dict[str, Any]]
    lock: threading.Lock

    def __init__(self, addr: tuple[str, int]) -> None:
        super().__init__(addr, _ReceiverHandler)
        self.handshake_secret = None
        self.received_events = []
        self.lock = threading.Lock()


class _ReceiverHandler(BaseHTTPRequestHandler):
    server: _Receiver  # type: ignore[assignment]

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — base class API
        return  # silence default per-request line

    def do_GET(self) -> None:  # noqa: N802 — base class API
        if self.path == "/health":
            self._send(HTTPStatus.OK, body=b"ok\n")
        else:
            self._send(HTTPStatus.NOT_FOUND, body=b"not found\n")

    def do_POST(self) -> None:  # noqa: N802 — base class API
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""

        secret = self.headers.get("X-Hook-Secret")
        if secret is not None:
            with self.server.lock:
                self.server.handshake_secret = secret
            self._send(HTTPStatus.OK, headers={"X-Hook-Secret": secret})
            return

        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(HTTPStatus.OK)
            return
        events = payload.get("events") if isinstance(payload, dict) else None
        if isinstance(events, list):
            with self.server.lock:
                self.server.received_events.extend(events)
        self._send(HTTPStatus.OK)

    def _send(
        self,
        status: int,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


# ---------- fixtures --------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _webhook_module_check() -> None:
    """Skip the whole module unless the operator opted in."""
    if _parse_tunnel() is None:
        pytest.skip(f"set {TUNNEL_ENV}={TUNNEL_PROVIDER}:<port> to enable webhook tests")
    if not os.environ.get(WORKSPACE_ENV):
        pytest.skip(f"{WORKSPACE_ENV} required for webhook lifecycle test")
    if not os.environ.get("ASANA_ACCESS_TOKEN"):
        pytest.skip("ASANA_ACCESS_TOKEN required for webhook lifecycle test")
    if not shutil.which("cloudflared"):
        pytest.skip("cloudflared binary not found in $PATH")


@pytest.fixture(scope="module")
def webhook_receiver() -> Generator[_Receiver, None, None]:
    port = _parse_tunnel()
    assert port is not None  # guaranteed by _webhook_module_check
    server = _Receiver(("127.0.0.1", port))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def webhook_target(webhook_receiver: _Receiver) -> Generator[str, None, None]:
    """Spawn cloudflared and yield the published ``*.trycloudflare.com`` URL.

    The tunnel dies when the cloudflared subprocess exits, so teardown
    is just ``proc.terminate()``.
    """
    port = webhook_receiver.server_address[1]
    # ``--config /dev/null`` keeps a user-level ``~/.cloudflared/config.yml``
    # (e.g. a named-tunnel ingress with a ``http_status:404`` catch-all) out of
    # the picture — otherwise its rules would be applied to the freshly minted
    # Quick Tunnel hostname and edge would 404 every request.
    proc = subprocess.Popen(  # noqa: S603 — argv is hardcoded
        ["cloudflared", "--config", os.devnull, "tunnel", "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    try:
        url = _read_tunnel_url(proc, deadline=time.monotonic() + TUNNEL_STARTUP_TIMEOUT_SEC)
        _wait_health(url, deadline=time.monotonic() + HEALTH_TIMEOUT_SEC)
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _read_tunnel_url(proc: subprocess.Popen[bytes], deadline: float) -> str:
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError("cloudflared exited before publishing a URL")
            continue
        m = QUICK_TUNNEL_URL_RE.search(line)
        if m:
            return m.group(0).decode("ascii")
    raise RuntimeError(f"cloudflared did not publish a URL within {TUNNEL_STARTUP_TIMEOUT_SEC}s")


def _wait_health(base_url: str, deadline: float) -> None:
    last_detail: str = "no probe attempted yet"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=5) as resp:  # noqa: S310
                if resp.status == 200:
                    return
                last_detail = f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            # Capture the body so we can tell edge-side errors (Cloudflare
            # HTML pages) from origin-side errors (our receiver's 404 text).
            try:
                body = exc.read(200).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 — diagnostic best-effort
                body = "<unreadable>"
            last_detail = f"HTTP {exc.code}: {body!r}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_detail = repr(exc)
        time.sleep(HEALTH_PROBE_INTERVAL_SEC)
    raise RuntimeError(f"tunnel {base_url} not reachable: {last_detail}")


@pytest.fixture
def created_webhooks() -> Generator[list[str], None, None]:
    """Best-effort teardown for webhooks created during a test."""
    gids: list[str] = []
    yield gids
    for gid in gids:
        make_runner().invoke(main, ["webhooks", "delete-webhook", "--webhook", gid])


# ---------- test ------------------------------------------------------------


def _run(*args: str) -> tuple[int, str, str]:
    result = make_runner().invoke(main, list(args))
    return result.exit_code, result.stdout, result.stderr


def test_webhook_lifecycle(
    workspace_gid: str,
    webhook_target: str,
    webhook_receiver: _Receiver,
    created_projects: list[str],
    created_webhooks: list[str],
) -> None:
    """create → list → get → trigger events → delete → list (gone)."""
    # Reset receiver state in case a prior test in the module left some.
    with webhook_receiver.lock:
        webhook_receiver.handshake_secret = None
        webhook_receiver.received_events.clear()

    # 1. CREATE WEBHOOK (workspace subscribe; filters are required for
    #    higher-level resources like workspace/team/portfolio).
    code, out, err = _run(
        "webhooks",
        "create-webhook",
        "--body",
        json.dumps(
            {
                "data": {
                    "resource": workspace_gid,
                    "target": webhook_target,
                    "filters": [
                        {"resource_type": "project", "action": "added"},
                        {"resource_type": "project", "action": "deleted"},
                    ],
                },
            },
        ),
    )
    assert code == 0, err
    webhook = json.loads(out)
    webhook_gid = webhook["gid"]
    created_webhooks.append(webhook_gid)
    # The handshake must have completed during the POST above; if it
    # had failed, create-webhook would have returned 4xx (not 0).
    assert webhook_receiver.handshake_secret, "handshake secret not received"

    # 2. LIST → contains our gid
    code, out, err = _run("webhooks", "get-webhooks", "--workspace", workspace_gid)
    assert code == 0, err
    assert any(w["gid"] == webhook_gid for w in json.loads(out))

    # 3. GET individual webhook
    code, out, err = _run("webhooks", "get-webhook", "--webhook", webhook_gid)
    assert code == 0, err
    assert json.loads(out)["gid"] == webhook_gid

    # 4. CREATE PROJECT → "added" event delivery
    code, out, err = _run(
        "projects",
        "create-project",
        "--body",
        json.dumps(
            {"data": {"name": "pytest-webhook-project", "workspace": workspace_gid}},
        ),
    )
    assert code == 0, err
    project_gid = json.loads(out)["gid"]
    created_projects.append(project_gid)

    # 5. DELETE PROJECT → "deleted" event delivery
    code, _, err = _run("projects", "delete-project", "--project", project_gid)
    assert code == 0, err
    created_projects.remove(project_gid)

    # 6. WAIT for the events to be batched and delivered. Asana's
    #    observed indexing + delivery latency is roughly 5-10s; pad
    #    a bit for slow days.
    time.sleep(EVENT_WAIT_SEC)

    # 7. ASSERT the expected events arrived. We do not assert on the
    #    *count* — the workspace subscribe may also surface adjacent
    #    membership / section events, and Asana is free to coalesce
    #    or split batches.
    with webhook_receiver.lock:
        events = list(webhook_receiver.received_events)
    added = [
        e
        for e in events
        if e.get("action") == "added"
        and e.get("resource", {}).get("gid") == project_gid
        and e.get("resource", {}).get("resource_type") == "project"
    ]
    deleted = [
        e
        for e in events
        if e.get("action") == "deleted" and e.get("resource", {}).get("gid") == project_gid
    ]
    assert added, f"no 'added' event for project {project_gid}; got: {events}"
    assert deleted, f"no 'deleted' event for project {project_gid}; got: {events}"

    # 8. DELETE WEBHOOK
    code, _, err = _run("webhooks", "delete-webhook", "--webhook", webhook_gid)
    assert code == 0, err
    created_webhooks.remove(webhook_gid)

    # 9. LIST → no longer contains
    code, out, err = _run("webhooks", "get-webhooks", "--workspace", workspace_gid)
    assert code == 0, err
    assert all(w["gid"] != webhook_gid for w in json.loads(out))
