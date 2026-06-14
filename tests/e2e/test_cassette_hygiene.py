"""Cassette hygiene: every committed cassette must be fully masked.

These run in plain replay mode (no network, no token). The central
invariant is that no *bare* identifier survives into a committed
cassette: every gid is either a named ``${VAR}`` placeholder or a
``${GID:<synthetic>}`` marker, every ``Authorization`` is redacted, and
no real email / presigned URL is present.

Because a real (unmasked) gid is just a bare number, wrapping the
synthetic in ``${GID:...}`` is what makes "all gids are masked" provable:
``test_no_bare_gids`` and ``test_every_16_digit_run_is_wrapped`` both go
red the moment a future ``--record`` lets a raw gid through, instead of
the leak hiding as a real-looking 16-digit number.

``test_leak_scan_clean`` runs the shared ``_leakscan`` detectors — the
same checks the record-time gate in ``conftest`` enforces — which also
look one encoding layer deep (base64 / percent-encoding), so an
identifier hidden inside e.g. a JWT pagination cursor cannot pass.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from e2e import _leakscan
from e2e import conftest as cf

_CASSETTE_DIR = Path(__file__).parent / "cassettes"
_CASSETTES = sorted(_CASSETTE_DIR.rglob("*.yaml"))
_IDS = [str(p.relative_to(_CASSETTE_DIR)) for p in _CASSETTES]

_REDACTED_AUTH = "Bearer ***REDACTED***"
_SYNTH_RE = re.compile(r"^[1-9][0-9]{15}$")  # shape of a synthetic gid
_GID_MARK_RE = re.compile(r"\$\{GID:([0-9]+)\}")
_BARE_16_RE = re.compile(r"\b[1-9][0-9]{15}\b")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_SIGNED_URL_RE = re.compile(r"asanausercontent\.com[^\s'\"\\]*\?")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_cassettes_present() -> None:
    """Guard against a glob that silently matches nothing."""
    assert _CASSETTES, "no cassettes found — parametrized hygiene checks would vacuously pass"


@pytest.mark.parametrize("path", _CASSETTES, ids=_IDS)
def test_no_bare_gids(path: Path) -> None:
    """``_collect_gids`` (the masker's own gid finder) finds nothing left.

    Equivalent to: every gid the recorder would have masked is now a
    ``${GID:...}`` marker or a named ``${VAR}`` — no raw digit gid remains.
    """
    leftovers = cf._collect_gids(_load(path))
    assert leftovers == [], f"bare numeric gids in {path.name}: {leftovers[:5]}"


@pytest.mark.parametrize("path", _CASSETTES, ids=_IDS)
def test_every_16_digit_run_is_wrapped(path: Path) -> None:
    """Collector-independent backstop: every gid-shaped run is inside a marker.

    Catches a gid that leaked in a position ``_collect_gids`` does not
    scan. If a future re-record introduces a legitimately non-gid 16-digit
    number, wrap-or-allowlist it here.
    """
    raw = path.read_text(encoding="utf-8")
    unwrapped = [
        m.group(0) for m in _BARE_16_RE.finditer(raw) if raw[m.start() - 6 : m.start()] != "${GID:"
    ]
    assert not unwrapped, f"un-wrapped 16-digit run(s) in {path.name}: {unwrapped[:5]}"


@pytest.mark.parametrize("path", _CASSETTES, ids=_IDS)
def test_gid_markers_are_synthetic_shaped(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    bad = [g for g in _GID_MARK_RE.findall(raw) if not _SYNTH_RE.match(g)]
    assert not bad, f"non-synthetic-shaped ${{GID:...}} markers in {path.name}: {bad[:5]}"


@pytest.mark.parametrize("path", _CASSETTES, ids=_IDS)
def test_authorization_always_redacted(path: Path) -> None:
    doc = _load(path)
    for inter in doc.get("interactions", []) or []:
        for side in ("request", "response"):
            headers = (inter.get(side) or {}).get("headers") or {}
            if not isinstance(headers, dict):
                continue
            for name, vals in headers.items():
                if name.lower() != "authorization":
                    continue
                for v in vals if isinstance(vals, list) else [vals]:
                    assert v == _REDACTED_AUTH, f"un-redacted Authorization in {path.name}: {v!r}"


@pytest.mark.parametrize("path", _CASSETTES, ids=_IDS)
def test_no_plaintext_pii(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    emails = [e for e in _EMAIL_RE.findall(raw) if not e.lower().endswith(".invalid")]
    assert not emails, f"non-.invalid email in {path.name}: {emails[:3]}"
    assert not _SIGNED_URL_RE.search(raw), f"presigned asanausercontent URL (query) in {path.name}"


@pytest.mark.parametrize("path", _CASSETTES, ids=_IDS)
def test_leak_scan_clean(path: Path) -> None:
    """The shared leak detectors find nothing in a committed cassette.

    Same checks as the record-time gate, applied to what is actually in
    the repo — catches a cassette that predates the gate or was edited
    by hand.
    """
    doc = _load(path)
    findings = _leakscan.scan_cassette(doc) + _leakscan.scan_headers(doc)
    assert findings == [], f"leak scan findings in {path.name}:\n  " + "\n  ".join(findings[:10])


def test_gid_wrap_unwrap_round_trip() -> None:
    """Serializer wraps to ${GID:...}; deserializer restores the *same*
    synthetic in both the request URI and the response body, so vcrpy's
    request-matching invariant survives the round trip."""
    import json

    gid = "9999888877776666"
    cassette = {
        "interactions": [
            {
                "request": {
                    "uri": f"https://app.asana.com/api/1.0/tasks/{gid}",
                    "headers": {},
                    "method": "GET",
                    "body": None,
                },
                "response": {
                    "body": {"string": json.dumps({"data": {"gid": gid}})},
                    "headers": {},
                    "status": {"code": 200, "message": "OK"},
                },
            }
        ],
        "version": 1,
    }
    text = cf._templated_yaml_serialize(cassette)
    assert "${GID:" in text, "serializer did not wrap the gid"
    assert gid not in text, "original gid survived (should be hashed away)"

    back = cf._templated_yaml_deserialize(text)
    uri_gid = back["interactions"][0]["request"]["uri"].rsplit("/", 1)[-1]
    body_gid = json.loads(back["interactions"][0]["response"]["body"]["string"])["data"]["gid"]
    assert uri_gid == body_gid, "URI and body gids diverged — replay would not match"
    assert "${GID:" not in uri_gid, "marker not stripped at deserialize"
    assert _SYNTH_RE.match(uri_gid), f"restored gid is not synthetic-shaped: {uri_gid!r}"
