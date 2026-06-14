"""Leak detectors for e2e cassettes.

Two callers share these checks so they cannot drift:

* the record-time gate in ``conftest._templated_yaml_serialize`` — a
  cassette that fails the scan is never written to disk;
* the committed-cassette hygiene tests in ``test_cassette_hygiene.py``.

The detectors walk every *parsed* string of a cassette dict (never the
raw YAML text: folded scalars split long values across lines, hiding
them from line-based matching) and also look one encoding layer deep —
base64 blobs are decoded and re-scanned, percent-encoded URI parts are
unquoted — because that is exactly where a leak survives the plain-text
masking passes (e.g. real gids inside a base64url JWT pagination
cursor).

Stdlib-only, and intentionally independent of ``conftest`` (which
imports this module).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import re
import urllib.parse
from collections.abc import Iterable, Iterator
from typing import Any

# ``${GID:<synthetic>}`` markers are the *masked* form of a gid — strip them
# before scanning so their digits never read as a bare gid.
_GID_MARK_RE = re.compile(r"\$\{GID:\d+\}")

# A gid-shaped digit run in a top-level (already plain-text) string. Kept
# identical to the committed-cassette invariant in test_cassette_hygiene.py.
_BARE_GID_RE = re.compile(r"\b[1-9][0-9]{15}\b")
# Inside *decoded* content nothing id-like should exist at all once masking
# has run, so the net is wider there.
_DECODED_GID_RE = re.compile(r"\b[1-9][0-9]{11,17}\b")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Asana personal-access-token shapes (old ``0/<hex>`` and current
# ``2/<user>/<token-gid>:<hex>``) and adjacent credential formats.
_PAT_RES = (
    re.compile(r"\b[01]/[0-9a-f]{32,}\b"),
    re.compile(r"\b2/\d{6,}(?:/\d{6,})?:[0-9a-f]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

# A Bearer value other than vcrpy's redaction placeholder.
_BEARER_RE = re.compile(r"Bearer\s+(?!\*\*\*REDACTED\*\*\*)[A-Za-z0-9._/+:\-]{16,}")

# A three-part base64url JWT (``eyJ`` is base64 for ``{"``). Real Asana
# pagination cursors have this shape; the masking pass replaces them with a
# digit-free synthetic that never starts with ``eyJ``, so any survivor is a
# leak. No trailing ``\b``: a signature may end in ``-``, which defeats it.
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")

# Candidate base64/base64url runs worth decoding. Hyphen/underscore are in
# the urlsafe alphabet, so word-ish text can qualify — the printable-decode
# requirement below discards those.
_B64_CANDIDATE_RE = re.compile(r"[A-Za-z0-9+/_\-]{24,}={0,2}")

# Presigned Asana CDN URL (query string carries the access grant).
_SIGNED_URL_RE = re.compile(r"asanausercontent\.com[^\s'\"\\]*\?")


def iter_strings(obj: Any) -> Iterator[str]:
    """Every string value in *obj*, depth-first; bytes are decoded as UTF-8."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, bytes):
        with contextlib.suppress(UnicodeDecodeError):
            yield obj.decode("utf-8")
    elif isinstance(obj, dict):
        for key, value in obj.items():
            yield from iter_strings(key)
            yield from iter_strings(value)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from iter_strings(item)


def _decode_base64(candidate: str) -> str | None:
    """*candidate* decoded as printable UTF-8 text, or None.

    Tries both alphabets with padding fixed up. Random binary (CDN trace
    ids, hash digests) fails the UTF-8 / printable requirement, which is
    what keeps this from flagging every opaque-but-harmless blob.
    """
    padded = candidate + "=" * (-len(candidate) % 4)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            raw = decoder(padded)
        except (binascii.Error, ValueError):
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if len(text) >= 8 and sum(c.isprintable() for c in text) / len(text) >= 0.9:
            return text
    return None


def _scan_decoded(text: str, origin: str, findings: list[str]) -> None:
    """Apply the decoded-content detectors to *text* (one layer down)."""
    for m in _DECODED_GID_RE.finditer(text):
        findings.append(f"id-like digit run {m.group(0)!r} hidden in {origin}")
    for m in _EMAIL_RE.finditer(text):
        if not m.group(0).lower().endswith(".invalid"):
            findings.append(f"email {m.group(0)!r} hidden in {origin}")
    for pat in _PAT_RES:
        if pat.search(text):
            findings.append(f"credential-shaped value hidden in {origin}")
    if _JWT_RE.search(text):
        findings.append(f"nested JWT hidden in {origin}")


def scan_string(s: str) -> list[str]:
    """All leak findings for one plain string value."""
    findings: list[str] = []
    s = _GID_MARK_RE.sub("", s)

    for m in _BARE_GID_RE.finditer(s):
        findings.append(f"bare 16-digit gid run {m.group(0)!r}")
    for m in _EMAIL_RE.finditer(s):
        if not m.group(0).lower().endswith(".invalid"):
            findings.append(f"email {m.group(0)!r}")
    for pat in _PAT_RES:
        for m in pat.finditer(s):
            findings.append(f"credential-shaped value {m.group(0)[:24]!r}…")
    for m in _BEARER_RE.finditer(s):
        findings.append(f"unredacted Bearer value {m.group(0)[:30]!r}…")
    for m in _JWT_RE.finditer(s):
        findings.append(f"JWT-shaped token {m.group(0)[:32]!r}…")
    if _SIGNED_URL_RE.search(s):
        findings.append("presigned asanausercontent URL (query present)")

    # One encoding layer down: base64 blobs and percent-encoded parts.
    for m in _B64_CANDIDATE_RE.finditer(s):
        decoded = _decode_base64(m.group(0))
        if decoded is not None:
            _scan_decoded(decoded, f"base64 {m.group(0)[:24]!r}…", findings)
    if "%" in s:
        unquoted = urllib.parse.unquote(s)
        if unquoted != s:
            for m in _BARE_GID_RE.finditer(unquoted):
                findings.append(f"gid run {m.group(0)!r} behind percent-encoding")

    return findings


def scan_cassette(cassette: Any) -> list[str]:
    """All leak findings across every string in a parsed cassette dict."""
    findings: list[str] = []
    for s in iter_strings(cassette):
        findings.extend(scan_string(s))
    return findings


# The complete set of request headers the recording stack sends. A new name
# here means the SDK / vcrpy started sending something unreviewed — extend
# the list consciously after checking the value is not credential-bearing.
_ALLOWED_REQUEST_HEADERS = frozenset(
    {"accept", "content-type", "user-agent", "x-asana-client-lib", "authorization"}
)
# Never acceptable on either side.
_FORBIDDEN_HEADERS = frozenset(
    {"cookie", "set-cookie", "proxy-authorization", "x-amz-security-token"}
)


def scan_headers(cassette: Any) -> list[str]:
    """Structural findings: forbidden headers and unreviewed request headers."""
    findings: list[str] = []
    interactions = cassette.get("interactions") if isinstance(cassette, dict) else None
    for i, inter in enumerate(interactions or []):
        if not isinstance(inter, dict):
            continue
        for side, allow in (("request", _ALLOWED_REQUEST_HEADERS), ("response", None)):
            headers = (inter.get(side) or {}).get("headers") or {}
            if not isinstance(headers, dict):
                continue
            for name in headers:
                lower = str(name).lower()
                if lower in _FORBIDDEN_HEADERS:
                    findings.append(f"forbidden {side} header {name!r} (interaction {i})")
                elif allow is not None and lower not in allow:
                    findings.append(f"unreviewed request header {name!r} (interaction {i})")
    return findings


def find_secrets(cassette: Any, secrets: Iterable[str]) -> list[str]:
    """Findings for any known live secret value appearing verbatim.

    *secrets* are the recording environment's real values (access token,
    real workspace gid, fixture-discovered gids) — every one of them must
    have been masked or templated away before serialization.
    """
    wanted = [secret for secret in secrets if secret and len(secret) >= 6]
    findings: list[str] = []
    for s in iter_strings(cassette):
        for secret in wanted:
            if secret in s:
                findings.append(f"live secret value (len {len(secret)}) present: {secret[:6]!r}…")
    return findings
