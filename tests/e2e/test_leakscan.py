"""Unit tests for ``_leakscan`` and the record-time gate in ``conftest``.

No network. The detector cases pin both directions: each leak class is
found (a regression here silently re-opens the leak), and each known-safe
value class — synthetics, placeholders, binary CDN blobs — is NOT flagged
(a regression here breaks recording with false positives).
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from e2e import _leakscan as ls
from e2e import conftest as cf

# A realistic Asana pagination cursor: HS256 JWT whose payload carries
# real-looking gids — the leak class that motivated the encoded-content scan.
_PAYLOAD = {"border_rank": '["V",1214927779918259,"9YVQ1MB93ML",1214928250704159]'}
_JWT = ".".join(
    base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode()
    if isinstance(part, dict)
    else part
    for part in ({"typ": "JWT", "alg": "HS256"}, _PAYLOAD, "c2lnbmF0dXJl")
)


class TestScanString:
    def test_jwt_is_flagged_with_hidden_gids(self) -> None:
        findings = ls.scan_string(f"https://app.asana.com/api/1.0/tasks?offset={_JWT}&limit=2")
        assert any("JWT-shaped" in f for f in findings)
        # The payload gids are surfaced through the base64 layer too.
        assert any("1214927779918259" in f for f in findings)

    def test_base64_hidden_gid_without_jwt_shape_is_flagged(self) -> None:
        blob = base64.b64encode(b'{"parent": "1214927779918259"}').decode()
        findings = ls.scan_string(f"cursor={blob}")
        assert any("hidden in base64" in f for f in findings)

    def test_bare_gid_and_email_and_presigned_url(self) -> None:
        findings = ls.scan_string(
            '{"gid": "1214927779918259", "email": "real.person@example.com", '
            '"url": "https://x.asanausercontent.com/a?sig=1"}'
        )
        assert any("bare 16-digit" in f for f in findings)
        assert any("real.person@example.com" in f for f in findings)
        assert any("presigned" in f for f in findings)

    def test_pat_shapes_and_bearer_are_flagged(self) -> None:
        # Built by concatenation, never a single literal: the detector must
        # flag a real Asana-PAT shape, but a contiguous PAT literal in source
        # would trip GitHub push protection. Keep it split.
        token = "2/1200000000000001/1200000000000002:" + "ab01" * 8
        assert any("credential-shaped" in f for f in ls.scan_string(token))
        assert any("Bearer" in f for f in ls.scan_string(f"Authorization: Bearer {token}"))

    def test_percent_encoded_gid_is_flagged(self) -> None:
        encoded = "".join(f"%{ord(c):02X}" for c in "1214927779918259")
        findings = ls.scan_string(f"https://h.test/?parent={encoded}")
        assert any("percent-encoding" in f for f in findings)

    def test_known_safe_values_are_clean(self) -> None:
        safe = [
            # The masking pipeline's own outputs:
            "tasks/${GID:1234567890123456}",
            "${WORKSPACE_GID}",
            cf._synthetic_jwt(_JWT),
            "12a89bd56980d443d23d9457dba72fc6:3",  # synthetic sync token
            "Bearer ***REDACTED***",
            "e2e-user@example.invalid",
            # Binary base64 (CDN trace ids): decodes to non-printable → skipped.
            base64.b64encode(bytes(range(32))).decode(),
            "https://app.asana.com/api/1.0/tasks?limit=100&opt_fields=name",
        ]
        for s in safe:
            assert ls.scan_string(s) == [], f"false positive on {s!r}: {ls.scan_string(s)}"


class TestScanHeaders:
    @staticmethod
    def _cassette(req_headers: dict[str, Any], resp_headers: dict[str, Any]) -> dict[str, Any]:
        return {
            "interactions": [
                {
                    "request": {"headers": req_headers},
                    "response": {"headers": resp_headers},
                }
            ]
        }

    def test_standard_recording_headers_are_clean(self) -> None:
        cassette = self._cassette(
            {
                "Accept": ["application/json"],
                "Content-Type": ["application/json"],
                "User-Agent": ["x"],
                "X-Asana-Client-Lib": ["y"],
                "authorization": ["Bearer ***REDACTED***"],
            },
            {"Content-Type": ["application/json"], "Server-Timing": ["x"]},
        )
        assert ls.scan_headers(cassette) == []

    def test_cookie_headers_are_forbidden_on_both_sides(self) -> None:
        cassette = self._cassette({"Cookie": ["sid=1"]}, {"Set-Cookie": ["sid=1"]})
        findings = ls.scan_headers(cassette)
        assert any("'Cookie'" in f and "request" in f for f in findings)
        assert any("'Set-Cookie'" in f and "response" in f for f in findings)

    def test_unreviewed_request_header_is_flagged(self) -> None:
        cassette = self._cassette({"X-Api-Key": ["k"]}, {})
        assert any("unreviewed request header" in f for f in ls.scan_headers(cassette))


class TestFindSecrets:
    def test_secret_value_is_found_anywhere(self) -> None:
        cassette = {"interactions": [{"response": {"body": {"string": "x SEEKRIT-VALUE y"}}}]}
        assert ls.find_secrets(cassette, ["SEEKRIT-VALUE"])
        assert ls.find_secrets(cassette, ["absent-value"]) == []

    def test_short_and_empty_secrets_are_ignored(self) -> None:
        # "x" appears in the body, but sub-6-char values would flag constantly.
        cassette = {"interactions": [{"response": {"body": {"string": "x"}}}]}
        assert ls.find_secrets(cassette, ["", "x"]) == []


class TestRecordGate:
    """``_templated_yaml_serialize`` refuses to write a leaking cassette."""

    @staticmethod
    def _cassette(uri: str, body: str) -> dict[str, Any]:
        return {
            "interactions": [
                {
                    "request": {"uri": uri, "headers": {}, "method": "GET", "body": None},
                    "response": {
                        "body": {"string": body},
                        "headers": {},
                        "status": {"code": 200, "message": "OK"},
                    },
                }
            ],
            "version": 1,
        }

    def test_jwt_cursor_is_masked_not_rejected(self) -> None:
        # The pipeline masks what it knows how to mask; the gate only fires
        # on what survives. A JWT cursor in URI + body round-trips to the
        # same synthetic on both sides (vcrpy matching invariant).
        cassette = self._cassette(
            f"https://app.asana.com/api/1.0/tasks?offset={_JWT}",
            json.dumps({"next_page": {"offset": _JWT}}),
        )
        text = cf._templated_yaml_serialize(cassette)
        assert "eyJ" not in text
        assert text.count(cf._synthetic_jwt(_JWT)) == 2

    def test_unmaskable_leak_aborts_the_write(self) -> None:
        cassette = self._cassette(
            "https://app.asana.com/api/1.0/users/me",
            '{"data": {"note": "mail real.person@example.com"}}',
        )
        with pytest.raises(RuntimeError, match=r"(?s)leak scan.*email"):
            cf._templated_yaml_serialize(cassette)

    def test_live_secret_in_body_aborts_the_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ASANA_ACCESS_TOKEN", "seekrit-live-token-0001")
        cassette = self._cassette(
            "https://app.asana.com/api/1.0/users/me",
            '{"echo": "seekrit-live-token-0001"}',
        )
        with pytest.raises(RuntimeError, match="live secret"):
            cf._templated_yaml_serialize(cassette)
