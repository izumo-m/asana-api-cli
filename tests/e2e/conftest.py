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
cassette never contains the real email / name / photo. Identifiers
without a named binding are auto-hashed to synthetic gids wrapped in
``${GID:<synthetic>}`` markers (unwrapped at load time) so that no bare
numeric gid survives into a committed cassette. After all masking, a
record-time gate runs the shared ``_leakscan`` detectors and refuses to
write a cassette that still carries an identifier (even behind base64 /
percent-encoding), a credential shape, or a live environment value.

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
from e2e import _leakscan

WORKSPACE_ENV = "ASANA_PYTEST_WORKSPACE"

_E2E_ROOT = Path(__file__).parent.resolve()
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
# Self-describing marker wrapping every auto-hashed synthetic gid, e.g.
# ``${GID:1234567890123456}``. It carries the synthetic value inline (no
# side table), and the deserializer unwraps it back to the bare synthetic so
# vcrpy request-matching is unaffected. Its purpose is auditability: a
# committed cassette must contain *no* bare numeric gid, only ``${GID:...}``
# markers and named ``${VAR}`` placeholders, which makes "every gid is masked"
# a checkable invariant (see ``tests/e2e/test_cassette_hygiene.py``).
_GID_MARK_RE = re.compile(r"\$\{GID:(\d+)\}")


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


def _harvest_user_identifiers(obj: Any, names: set[str], emails: set[str]) -> None:
    """Collect every ``user.name`` / ``user.email`` value in *obj*.

    Used by :func:`_before_record_response` to also strip these values
    from free-text fields (e.g. ``story.text`` of the form
    ``"<user> さんが ..."``) that the ``resource_type``-aware
    :func:`_mask_object` cannot reach.
    """
    if isinstance(obj, dict):
        if obj.get("resource_type") == "user":
            for key, sink in (("name", names), ("email", emails)):
                v = obj.get(key)
                if isinstance(v, str) and v:
                    sink.add(v)
        for v in obj.values():
            _harvest_user_identifiers(v, names, emails)
    elif isinstance(obj, list):
        for item in obj:
            _harvest_user_identifiers(item, names, emails)


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
    # Harvest real identifiers before they are masked away in-place, so we
    # can also strip them from unstructured strings further down.
    real_names: set[str] = set()
    real_emails: set[str] = set()
    _harvest_user_identifiers(data, real_names, real_emails)
    _mask_object(data, _bindings())
    # Free-text substitution catches values embedded in fields such as
    # ``story.text`` ("X さんが …"). Done on the parsed structure (rather
    # than the serialized JSON) so names/emails containing JSON-escapable
    # characters like ``"`` / ``\`` still match. Longest-first; emails
    # before names because the user's display name is sometimes the email
    # itself.
    b = _bindings()
    email_repl = b.get("USER_EMAIL", "")
    name_repl = b.get("USER_NAME", "")
    substitutions: list[tuple[str, str]] = []
    if email_repl:
        substitutions.extend((v, email_repl) for v in sorted(real_emails, key=len, reverse=True))
    if name_repl:
        substitutions.extend(
            (v, name_repl) for v in sorted(real_names - real_emails, key=len, reverse=True)
        )
    if substitutions:

        def _apply(s: str) -> str:
            for old, new in substitutions:
                s = s.replace(old, new)
            return s

        data = _walk(data, _apply)
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    response["body"]["string"] = text.encode("utf-8")
    return response


# ---------- GID auto-hashing ------------------------------------------------


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
                # behind; only digit-form ids participate in auto-hashing.
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
    """16-digit decimal derived from ``sha256(real_gid)``.

    Format ``[1-9][0-9]{15}`` matches a current Asana gid's shape, so once
    the deserializer unwraps the surrounding ``${GID:...}`` marker (see
    ``_GID_MARK_RE``) the replayed request carries a real-looking gid. Same
    real gid → same synthetic, so identifiers stay traceable by grep across
    cassettes (grep either the synthetic or the ``${GID:`` marker).
    """
    digest = hashlib.sha256(real_gid.encode("ascii")).digest()
    n = int.from_bytes(digest[:8], "big")
    return str(10**15 + (n % (9 * 10**15)))


# Asana events API sync token: ``<32-hex>:<integer>``. Appears in request
# URLs (``:`` URL-encoded as ``%3A``) and response bodies (literal ``:``).
_SYNC_TOKEN_RE = re.compile(r"\b([a-f0-9]{32})(:|%3A)(\d+)\b")


def _replace_sync_token(match: re.Match[str]) -> str:
    prefix, sep, suffix = match.group(1), match.group(2), match.group(3)
    digest = hashlib.sha256(prefix.encode("ascii")).hexdigest()[:32]
    return f"{digest}{sep}{suffix}"


def _mask_sync_tokens(obj: Any) -> Any:
    """Replace each Asana events sync token with a deterministic synthetic.

    Sync tokens are not credentials (they expire in ~24h and are useless
    without the ``Authorization`` header, which is already masked) but
    they are account-coupled opaque strings that need not survive into
    a committed cassette. The events API echoes them in request URLs and
    response bodies; hashing both consistently at record time preserves
    vcrpy's request-matching invariant at replay (the test extracts the
    synthetic sync from the response body and sends it back, matching
    the synthetic in the recorded URL).

    The 32-hex prefix is sha256'd back to 32 hex chars; the ``:N`` /
    ``%3AN`` suffix is preserved so the "same prefix family" relation
    (Asana increments ``N`` between polls on one subscription) stays
    visible in the cassette.
    """
    return _walk(obj, lambda s: _SYNC_TOKEN_RE.sub(_replace_sync_token, s))


# Hex digit -> letter, so a synthetic carries no digit run at all and the
# "every 16-digit run is wrapped" hygiene rule can never fire on one.
_HEX_TO_ALPHA = str.maketrans("0123456789", "ghijklmnop")


def _synthetic_jwt(token: str) -> str:
    """Digit-free deterministic stand-in for an opaque JWT-shaped cursor.

    Same token → same synthetic, so a cursor echoed between a response
    body and the next request URL keeps vcrpy's request-matching
    invariant at replay; distinct cursors stay distinct, so paged
    requests remain distinguishable.
    """
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()[:40]
    return "masked-jwt-" + digest.translate(_HEX_TO_ALPHA)


def _mask_jwt_tokens(obj: Any) -> Any:
    """Replace each JWT-shaped string with a deterministic synthetic.

    Asana's pagination ``offset`` cursors are HS256 JWTs whose base64url
    payload carries *real* task / project gids (``border_rank``) — the
    plain-text gid masking cannot see through the encoding, so the whole
    token is replaced. Like sync tokens, cursors are account-coupled
    opaque strings (short-lived, useless without ``Authorization``) that
    need not survive into a committed cassette.
    """
    return _walk(obj, lambda s: _leakscan._JWT_RE.sub(lambda m: _synthetic_jwt(m.group(0)), s))


def _auto_hash_gids(cassette_dict: Any) -> Any:
    """Replace each discovered identifier with a marked synthetic gid.

    Runs AFTER the explicit binding pass so ``${WORKSPACE_GID}`` /
    ``${PAGINATION_PROJECT_GID}`` / ... take priority. The rest — user gid,
    team gid, transient task / project gids, asset ids, etc. — become
    ``${GID:<16-digit synthetic>}`` markers: the synthetic reveals nothing
    about the recording account, and the ``${GID:...}`` wrapper makes the
    masking self-evident (no bare numeric gid survives into the cassette, so
    an unmasked real gid stands out instead of hiding as a look-alike).
    The deserializer strips the wrapper back to the bare synthetic at replay.
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
    return _walk(
        cassette_dict,
        lambda s: pattern.sub(lambda m: "${GID:" + mapping[m.group(0)] + "}", s),
    )


# ---------- Per-test cassette mask hook (L3) --------------------------------

# Maskers attached for the currently-running test. Populated by
# ``_register_cassette_maskers`` (called from ``pytest_runtest_setup``)
# from a ``@pytest.mark.cassette_mask.with_args(fn, ...)`` marker and
# drained by ``pytest_runtest_teardown`` (``trylast=True``).
#
# The ``.with_args`` form is required: ``MarkDecorator``'s call sugar
# treats a single callable positional argument as the decorated test
# function and stores a bare (arg-less) mark on it instead, so
# ``@pytest.mark.cassette_mask(fn)`` silently does nothing.
#
# Three masking layers cover recorded responses (L2 at record time; L1/L3 at
# serialize time):
#
#   L1 — universal value/format pass: ``${VAR}`` templating,
#        ``_auto_hash_gids``, ``_mask_sync_tokens``, ``_mask_jwt_tokens``.
#   L2 — schema-aware response hook: ``_before_record_response`` /
#        ``_mask_object`` dispatch on ``resource_type``.
#   L3 — per-test/API hook (this list): when L2's ``resource_type``-keyed
#        masking cannot reach the PII because the response lacks
#        ``resource_type`` (e.g. a ``/batch`` sub-response whose action did
#        not request that field) the test attaches an API-specific masker
#        that mutates the parsed cassette dict in place before L1 runs.
_active_maskers: list[Callable[[Any], None]] = []


def _register_cassette_maskers(item: pytest.Item) -> None:
    """Populate ``_active_maskers`` from the test's ``cassette_mask`` marker.

    Called from :func:`pytest_runtest_setup` so registration happens
    *before* pytest-recording's ``vcr`` fixture sets up. That ordering
    is critical: ``vcr`` saves the cassette during *its* fixture
    teardown by invoking :func:`_templated_yaml_serialize`, which reads
    ``_active_maskers``. If this list were drained too early — e.g. in
    an autouse fixture's ``finally``, which under pytest's LIFO
    semantics runs *before* the ``vcr`` teardown — the serializer
    would observe an empty list and skip the L3 pass.

    Drained from :func:`pytest_runtest_teardown` (``trylast=True``),
    which runs *after* every fixture teardown for the test (so the
    ``vcr`` save has already consumed the list by then).
    """
    _active_maskers.clear()
    marker = item.get_closest_marker("cassette_mask")
    if marker is not None:
        _active_maskers.extend(marker.args)


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers so pytest does not warn about them."""
    config.addinivalue_line(
        "markers",
        "cassette_mask(*fns): attach per-test cassette-mask hooks (L3 PII "
        "layer). Apply with `.with_args(fn, ...)` so the callable args are "
        "not mistaken for the decorated test function.",
    )


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


def _live_secret_values() -> list[str]:
    """The recording environment's real values that must never be written:
    the access token, the real workspace gid, and every fixture-discovered
    gid bound during the test. (The fixed bindings are excluded — they are
    the public *replacement* literals, expected to appear.)"""
    return [
        os.environ.get("ASANA_ACCESS_TOKEN", ""),
        os.environ.get(WORKSPACE_ENV, ""),
        *_dynamic_bindings.values(),
    ]


def _templated_yaml_serialize(cassette_dict):  # type: ignore[no-untyped-def]
    # L3 first so per-test maskers can read the raw recorded values before
    # L1 templating rewrites them (e.g. before USER_NAME becomes ${USER_NAME}).
    for masker in _active_maskers:
        masker(cassette_dict)
    bindings = _bindings()
    templated = _walk(cassette_dict, lambda s: _template_string(s, bindings))
    # JWTs first: replacing whole tokens before the gid pass keeps a
    # ``${GID:...}`` marker from ever being spliced into a token's base64
    # (which would break both the token match and replay determinism).
    templated = _mask_jwt_tokens(templated)
    templated = _auto_hash_gids(templated)
    templated = _mask_sync_tokens(templated)
    # Record-time gate: a cassette that fails the leak scan is never written.
    # Failing here turns a masking gap into a loud record-time error instead
    # of a quietly committed leak that only the hygiene tests might catch.
    findings = _leakscan.scan_cassette(templated)
    findings += _leakscan.scan_headers(templated)
    findings += _leakscan.find_secrets(templated, _live_secret_values())
    if findings:
        details = "\n  ".join(findings[:20])
        raise RuntimeError(
            f"refusing to write cassette — leak scan found {len(findings)} issue(s):\n  {details}"
        )
    return _orig_yaml_serialize(templated)


def _templated_yaml_deserialize(cassette_string):  # type: ignore[no-untyped-def]
    cassette_dict = _orig_yaml_deserialize(cassette_string)
    bindings = _bindings()

    def _restore(s: str) -> str:
        # ${VAR} → bound value (or the bare VAR fallback); ${GID:N} → bare
        # synthetic N. Independent syntaxes, so order is immaterial: the
        # ${VAR} pattern requires an upper-case name and never matches
        # ${GID:<digits>}.
        s = _substitute_string(s, bindings)
        return _GID_MARK_RE.sub(lambda m: m.group(1), s)

    return _walk(cassette_dict, _restore)


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
    projects = json.loads(result.stdout)
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
    the small fixture (50 tasks) used to test the ``--full-payload`` success
    path against Asana's unpaginated-response cap (1000 items).
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
def _ensure_token(request: pytest.FixtureRequest) -> Generator[None, None, None]:
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
    """Per-test pre-fixture setup: cassette wipe (for --record) + L3 masker registration.

    Both pieces must run *before* pytest-recording's ``vcr`` fixture
    sets up, so they live in this hook rather than an autouse fixture:

    * **Cassette wipe.** ``vcrpy`` 's ``record_mode="all"`` appends to
      the existing cassette rather than overwriting it. Auto-hashed
      synthetic gids from a prior recording would then be re-collected
      by ``_auto_hash_gids`` as if they were real, double-hashing them
      on the next save. Wiping here gives VCR an empty cassette to
      start from.
    * **Masker registration.** See :func:`_register_cassette_maskers`
      for the timing rationale.
    """
    _register_cassette_maskers(item)
    if not item.config.getoption("--record", default=False):
        return
    if item.get_closest_marker("vcr") is None:
        return
    module_path = Path(str(item.fspath))
    cassette_path = module_path.parent / "cassettes" / module_path.stem / f"{item.name}.yaml"
    cassette_path.unlink(missing_ok=True)


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> None:  # noqa: ARG001
    """Drain ``_active_maskers`` after pytest has torn down every fixture.

    ``trylast=True`` is essential. Without it our hook implementation
    runs *before* the core ``_pytest.runner`` impl that actually tears
    down fixtures (including pytest-recording's ``vcr``), so the clear
    would race the cassette save and leave ``_templated_yaml_serialize``
    looking at an empty list. ``trylast=True`` pushes us behind the
    core impl, so by the time we run the L3 maskers have already been
    consumed.
    """
    _active_maskers.clear()
