"""Configuration for end-to-end tests.

The ``--live`` / ``--record`` flags themselves are registered in
``tests/conftest.py`` (so they appear in ``pytest --help`` without
having to collect under ``tests/e2e/`` first). This file provides the
e2e-only pieces: fixtures, vcr_config, resource-type masking and the
``${VAR}`` templating layer for cassette portability.

Modes:

- ``pytest`` (default): replay from committed cassettes, no network, no
  real token needed.
- ``pytest --live``: hit the real Asana API but do not write cassettes
  (use this to verify cassettes still match current API behavior).
- ``pytest --live --record``: hit the real API and overwrite cassettes
  (the cassette-regeneration workflow).

The pytest-recording native flags (``--record-mode`` / ``--disable-recording``)
remain available as an escape hatch when used alone, and are honored by
the same live-mode detection.

Account-dependent values are templated into the cassette as ``${VAR}``
placeholders by an in-place patch of vcrpy's YAML serializer (see
``_templated_yaml_serialize`` / ``_templated_yaml_deserialize`` below)
and substituted back at load time. PII fields are first masked to the
binding value by the ``resource_type``-aware response hook so the
cassette never contains the real email / name / photo.

See ``tests/e2e/README.md`` for the full workflow.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import urllib.parse
from collections.abc import Generator
from pathlib import Path
from typing import Any, Callable

import pytest
from _cli_runner import full_output, make_runner
from vcr.serializers import yamlserializer

from asana_api_cli.cli import main

WORKSPACE_ENV = "ASANA_PYTEST_WORKSPACE"

_E2E_ROOT = Path(__file__).parent.resolve()
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


# ---------- mode detection (options registered in tests/conftest.py) --------


def _is_live_mode(config: pytest.Config) -> bool:
    """True when the test session will hit the real network.

    Considers both this conftest's ``--live`` flag and the
    pytest-recording native options (``--record-mode`` / ``--disable-recording``)
    so an escape-hatch user gets the same fixture behavior.
    """
    if config.getoption("--live", default=False):
        return True
    if config.getoption("--disable-recording", default=False):
        return True
    record_mode = config.getoption("--record-mode", default="none") or "none"
    return record_mode != "none"


# Template variable -> binding value.
# WORKSPACE_GID comes from env (per-account); the others are fixed literals
# so cassettes are portable even without those env vars being set.
_FIXED_BINDINGS: dict[str, str] = {
    "WORKSPACE_NAME": "E2E Workspace",
    "USER_EMAIL": "e2e-user@example.invalid",
    "USER_NAME": "E2E User",
    "TEAM_NAME": "E2E Team",
}

# Bindings populated by fixtures during a test (e.g. discovered resource
# gids). Cleared on teardown by the fixture that set them.
_dynamic_bindings: dict[str, str] = {}


def _bindings() -> dict[str, str]:
    """Current ${VAR} -> value mapping. Read fresh so env changes apply."""
    b = dict(_FIXED_BINDINGS)
    ws = os.environ.get(WORKSPACE_ENV)
    if ws:
        b["WORKSPACE_GID"] = ws
    b.update(_dynamic_bindings)
    return b


# ---------- PII masking (resource_type aware) -------------------------------


def _strip_signed_query(url: str) -> str:
    """Drop the query string from an ``asanausercontent.com`` URL.

    Asana's ``download_url`` / ``view_url`` are presigned: the
    ``?e=<expiry>&v=...&t=<HMAC>`` query grants read access to the asset
    until the expiry. Never commit it to the repo — the response body is
    enough to verify the URL's shape, and tests do not follow the URL.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.netloc.endswith("asanausercontent.com"):
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return url


def _mask_object(obj: Any, bindings: dict[str, str]) -> None:
    if isinstance(obj, dict):
        rtype = obj.get("resource_type")
        if rtype == "user":
            if "email" in obj:
                obj["email"] = bindings.get("USER_EMAIL", obj["email"])
            if "name" in obj:
                obj["name"] = bindings.get("USER_NAME", obj["name"])
            if "photo" in obj:
                obj["photo"] = None
        elif rtype == "workspace":
            if "name" in obj:
                obj["name"] = bindings.get("WORKSPACE_NAME", obj["name"])
            if "email_domains" in obj:
                obj["email_domains"] = ["example.invalid"]
        elif rtype == "team":
            if "name" in obj:
                obj["name"] = bindings.get("TEAM_NAME", obj["name"])
        elif rtype == "attachment":
            for url_field in ("download_url", "view_url"):
                value = obj.get(url_field)
                if isinstance(value, str):
                    obj[url_field] = _strip_signed_query(value)
        for v in obj.values():
            _mask_object(v, bindings)
    elif isinstance(obj, list):
        for item in obj:
            _mask_object(item, bindings)


def _before_record_response(response):  # type: ignore[no-untyped-def]
    body = response.get("body", {}).get("string")
    if not isinstance(body, bytes):
        return response
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return response
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return response
    _mask_object(data, _bindings())
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    response["body"]["string"] = text.encode("utf-8")
    return response


# ---------- GID auto-templating ---------------------------------------------


def _gid_parent_segments() -> frozenset[str]:
    """Path segments preceding any ``{*_gid}`` / ``{custom_id}`` placeholder
    in the installed asana SDK's endpoint definitions.

    Scans ``asana/api/*.py`` for ``'/<path>/{placeholder}'``-style string
    literals (swagger-codegen output). Computed once at import time; re-derived
    implicitly on each SDK bump.
    """
    import asana

    api_dir = Path(asana.__file__).parent / "api"
    template_re = re.compile(r"'(/[^']+)'\s*,\s*'(?:GET|POST|PUT|DELETE|HEAD|PATCH)'")
    parent_re = re.compile(r"/([^/{]+)/\{[^}]+\}")
    parents: set[str] = set()
    for f in api_dir.glob("*.py"):
        text = f.read_text(encoding="utf-8")
        for m in template_re.finditer(text):
            for pm in parent_re.finditer(m.group(1)):
                parents.add(pm.group(1))
    return frozenset(parents)


_GID_PARENTS = _gid_parent_segments()

# Match a path segment immediately after a known parent (captures the gid
# or custom_id position). Built from the SDK so it tracks the API surface.
_GID_PATH_RE = re.compile(
    r"/(?:" + "|".join(re.escape(p) for p in sorted(_GID_PARENTS)) + r')/([^/?"\\\s]+)'
)

# ``"gid": "<value>"`` in any JSON-shaped string. Threshold-free so gids of
# any digit length are captured.
_JSON_GID_RE = re.compile(r'"gid"\s*:\s*"([^"]+)"')

# Asset-id position in an asanausercontent.com URL path. Asana's CDN host
# is not in the SDK so it needs its own pattern. The asset id is a storage
# identifier distinct from the attachment gid.
_ASSET_URL_RE = re.compile(r'asanausercontent\.com/[^/]+/assets/[^/]+/([^/?"\\\s]+)')


def _collect_gids(cassette_dict: Any) -> list[str]:
    """Discover Asana identifiers used anywhere in the cassette.

    Three sources, all unioned: JSON ``"gid"`` fields, URL path segments
    after SDK-known parents, and asanausercontent.com asset-id positions.
    Returns first-occurrence order (deterministic for a given cassette).
    """
    seen: dict[str, None] = {}

    def _collect_str(text: str) -> None:
        for pattern in (_JSON_GID_RE, _GID_PATH_RE, _ASSET_URL_RE):
            for m in pattern.finditer(text):
                value = m.group(1)
                # Skip ``${VAR}`` placeholders the explicit-binding pass left
                # behind; only digit-form ids participate in auto-templating.
                if value.isdigit():
                    seen.setdefault(value, None)

    def _visit(obj: Any) -> None:
        if isinstance(obj, str):
            _collect_str(obj)
        elif isinstance(obj, bytes):
            with contextlib.suppress(UnicodeDecodeError):
                _collect_str(obj.decode("utf-8"))
        elif isinstance(obj, dict):
            for v in obj.values():
                _visit(v)
        elif isinstance(obj, list):
            for item in obj:
                _visit(item)

    _visit(cassette_dict)
    return list(seen.keys())


def _synthetic_gid(real_gid: str) -> str:
    """16-digit decimal placeholder derived from ``sha256(real_gid)``.

    Format ``[1-9][0-9]{15}`` matches a current Asana gid's shape so the
    cassette stays drop-in compatible (no ``${...}`` wrapping). Same real
    gid → same synthetic, so identifiers stay traceable by grep across
    cassettes.
    """
    digest = hashlib.sha256(real_gid.encode("ascii")).digest()
    n = int.from_bytes(digest[:8], "big")
    return str(10**15 + (n % (9 * 10**15)))


def _auto_template_gids(cassette_dict: Any) -> Any:
    """Replace each discovered identifier with a deterministic synthetic gid.

    Runs AFTER the explicit binding pass so ``${WORKSPACE_GID}`` /
    ``${PAGINATION_PROJECT_GID}`` / ... take priority. The rest — user gid,
    team gid, transient task / project gids, asset ids, etc. — become
    16-digit decimals that look like real gids but reveal nothing about
    the recording account.
    """
    gids = _collect_gids(cassette_dict)
    if not gids:
        return cassette_dict

    mapping = {gid: _synthetic_gid(gid) for gid in gids}
    if len(set(mapping.values())) != len(mapping):
        # Two real gids hashed to the same synthetic — replay would become
        # ambiguous. ~1 in 9*10^15 per pair, so essentially impossible at
        # our scale; surface loudly if it ever does happen so the cassette
        # can be re-recorded against a fresh resource set.
        raise RuntimeError(
            f"synthetic gid collision across {len(gids)} gids; "
            f"re-record the cassette or extend the hash domain"
        )

    # Longest-first so a gid that's a prefix of another can't half-match.
    alternation = "|".join(re.escape(g) for g in sorted(gids, key=len, reverse=True))
    pattern = re.compile(r"\b(?:" + alternation + r")\b")
    return _walk(cassette_dict, lambda s: pattern.sub(lambda m: mapping[m.group(0)], s))


# ---------- Templated YAML serializer ---------------------------------------


def _template_string(s: str, bindings: dict[str, str]) -> str:
    """Replace each bound value with its ${VAR} placeholder (longest-first)."""
    for var, value in sorted(bindings.items(), key=lambda kv: -len(kv[1])):
        if value and value in s:
            s = s.replace(value, "${" + var + "}")
    return s


def _substitute_string(s: str, bindings: dict[str, str]) -> str:
    """Replace ${VAR} placeholders with the bound value, or fall back to VAR."""

    def repl(match: re.Match[str]) -> str:
        return bindings.get(match.group(1), match.group(1))

    return _PLACEHOLDER_RE.sub(repl, s)


def _walk(obj: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, bytes):
        try:
            text = obj.decode("utf-8")
        except UnicodeDecodeError:
            return obj
        return fn(text).encode("utf-8")
    if isinstance(obj, dict):
        return {k: _walk(v, fn) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(item, fn) for item in obj]
    return obj


# Patch ``yamlserializer.{serialize,deserialize}`` to template/substitute
# ``${VAR}`` placeholders. Done at module import time; this conftest is only
# loaded when pytest collects tests under ``tests/e2e/``, so non-e2e runs are
# unaffected. (Passing a custom serializer object via ``vcr_config`` is not
# supported by pytest-recording, which expects a string suffix.)
_orig_yaml_serialize = yamlserializer.serialize
_orig_yaml_deserialize = yamlserializer.deserialize


def _templated_yaml_serialize(cassette_dict):  # type: ignore[no-untyped-def]
    bindings = _bindings()
    templated = _walk(cassette_dict, lambda s: _template_string(s, bindings))
    templated = _auto_template_gids(templated)
    return _orig_yaml_serialize(templated)


def _templated_yaml_deserialize(cassette_string):  # type: ignore[no-untyped-def]
    cassette_dict = _orig_yaml_deserialize(cassette_string)
    bindings = _bindings()
    return _walk(cassette_dict, lambda s: _substitute_string(s, bindings))


yamlserializer.serialize = _templated_yaml_serialize
yamlserializer.deserialize = _templated_yaml_deserialize


# ---------- pytest fixtures --------------------------------------------------


@pytest.fixture(scope="module")
def vcr_config() -> dict[str, Any]:
    return {
        "filter_headers": [("authorization", "Bearer ***REDACTED***")],
        "before_record_response": _before_record_response,
        "decode_compressed_response": True,
    }


@pytest.fixture
def workspace_gid(request: pytest.FixtureRequest) -> str:
    """The workspace GID under test.

    In live mode the real env value is required (no env -> skip with a
    helpful message). In replay mode the env is optional: when unset,
    return the ``"WORKSPACE_GID"`` sentinel string that the deserializer
    also falls back to for unbound ``${WORKSPACE_GID}`` placeholders, so
    requests and cassette entries line up on the same literal.
    """
    gid = os.environ.get(WORKSPACE_ENV)
    if gid:
        return gid
    if _is_live_mode(request.config):
        pytest.skip(f"{WORKSPACE_ENV} required for --live mode")
    return "WORKSPACE_GID"


PAGINATION_PROJECT_NAME = "pagination-test"
PAGINATION_SMALL_PROJECT_NAME = "pagination-test-small"


def _discover_project_gid(workspace_gid: str, name: str) -> str:
    """Look up a project's gid by name within the workspace."""
    result = make_runner().invoke(
        main,
        [
            "projects",
            "get-projects-for-workspace",
            "--workspace",
            workspace_gid,
            "--opt-fields",
            "name",
        ],
    )
    if result.exit_code != 0:
        pytest.fail(f"failed to list projects: {full_output(result)}")
    projects = json.loads(full_output(result))
    target = next((p for p in projects if p.get("name") == name), None)
    if target is None:
        pytest.skip(
            f"project {name!r} not found in workspace {workspace_gid}; "
            "run `python tools/e2e_init.py` to provision it",
        )
    return target["gid"]


@pytest.fixture
def pagination_project_gid(workspace_gid: str) -> Generator[str, None, None]:
    """Discover the ``pagination-test`` project gid in the test workspace.

    Registers the discovered gid as ``${PAGINATION_PROJECT_GID}`` so the
    surrounding cassette stores the placeholder. In replay mode the
    discovery call is served from cassette and yields the literal
    ``"PAGINATION_PROJECT_GID"`` string (the deserializer's fallback when
    the placeholder is unbound) — the test workflow stays consistent
    because both the recorded URL and the replayed live URL share that
    same literal string.
    """
    gid = _discover_project_gid(workspace_gid, PAGINATION_PROJECT_NAME)
    _dynamic_bindings["PAGINATION_PROJECT_GID"] = gid
    try:
        yield gid
    finally:
        _dynamic_bindings.pop("PAGINATION_PROJECT_GID", None)


@pytest.fixture
def pagination_small_project_gid(workspace_gid: str) -> Generator[str, None, None]:
    """Discover the ``pagination-test-small`` project gid.

    Same template-binding mechanism as ``pagination_project_gid`` but for
    the small fixture (50 tasks) used to test the ``--full-payload`` /
    ``--no-return-page-iterator`` success path against Asana's
    unpaginated-response cap (1000 items).
    """
    gid = _discover_project_gid(workspace_gid, PAGINATION_SMALL_PROJECT_NAME)
    _dynamic_bindings["PAGINATION_SMALL_PROJECT_GID"] = gid
    try:
        yield gid
    finally:
        _dynamic_bindings.pop("PAGINATION_SMALL_PROJECT_GID", None)


@pytest.fixture
def created_projects() -> Generator[list[str], None, None]:
    """Tracker for project gids created during a test.

    Tests append created gids and ``remove()`` them after a successful
    explicit delete; anything still in the list at teardown is best-effort
    deleted so a failed test does not leak resources. The teardown delete
    runs inside the vcr cassette context, so it is recorded / replayed
    along with the rest of the test.
    """
    gids: list[str] = []
    yield gids
    for gid in gids:
        make_runner().invoke(main, ["projects", "delete-project", "--project", gid])


@pytest.fixture
def created_tasks() -> Generator[list[str], None, None]:
    """Tracker for task gids created during a test. Same semantics as
    ``created_projects``."""
    gids: list[str] = []
    yield gids
    for gid in gids:
        make_runner().invoke(main, ["tasks", "delete-task", "--task", gid])


@pytest.fixture
def created_attachments() -> Generator[list[str], None, None]:
    """Tracker for attachment gids created during a test. Same semantics as
    ``created_projects``."""
    gids: list[str] = []
    yield gids
    for gid in gids:
        make_runner().invoke(main, ["attachments", "delete-attachment", "--attachment", gid])


@pytest.fixture
def attachment_parent_task(pagination_project_gid: str, created_tasks: list[str]) -> str:
    """Create a temporary parent task to attach files to.

    Cleanup is handled by the ``created_tasks`` fixture's teardown.
    """
    name = "pytest-e2e-attachment-parent"
    result = make_runner().invoke(
        main,
        [
            "tasks",
            "create-task",
            "--body",
            json.dumps({"data": {"name": name, "projects": [pagination_project_gid]}}),
        ],
    )
    if result.exit_code != 0:
        pytest.fail(f"failed to create parent task: {result.stderr}")
    task = json.loads(result.stdout)
    created_tasks.append(task["gid"])
    return task["gid"]


@pytest.fixture(autouse=True)
def _ensure_token(request: pytest.FixtureRequest):
    """Inject a dummy ASANA_ACCESS_TOKEN in replay mode if none is set.

    The CLI refuses to start without a token; replay does not actually use it
    since vcrpy intercepts the HTTP layer. Live mode requires a real token —
    we don't paper over a missing one because the SDK call would then 401.
    """
    if not _is_live_mode(request.config) and not os.environ.get("ASANA_ACCESS_TOKEN"):
        os.environ["ASANA_ACCESS_TOKEN"] = "replay-dummy-token"
        try:
            yield
        finally:
            os.environ.pop("ASANA_ACCESS_TOKEN", None)
    else:
        yield


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Delete the cassette file before re-recording.

    vcrpy's ``record_mode="all"`` re-records every interaction but APPENDS
    to the existing cassette rather than overwriting. Auto-templated
    synthetic gids from a prior recording would then be re-collected by
    ``_auto_template_gids`` as if they were real, double-hashing them on
    the next save. Wiping the file in ``pytest_runtest_setup`` — which
    runs before pytest-recording's ``vcr`` fixture loads it — gives VCR
    an empty cassette to start from.
    """
    if not item.config.getoption("--record", default=False):
        return
    if item.get_closest_marker("vcr") is None:
        return
    module_path = Path(str(item.fspath))
    cassette_path = module_path.parent / "cassettes" / module_path.stem / f"{item.name}.yaml"
    cassette_path.unlink(missing_ok=True)
