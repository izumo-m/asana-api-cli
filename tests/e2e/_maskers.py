"""Per-test cassette maskers (L3 of the e2e masking pipeline).

Each function here mutates a parsed cassette dict in place. Tests opt in
via ``@pytest.mark.cassette_mask.with_args(mask_users_in_batch_subresponses,
...)``; ``conftest.py`` then runs the listed maskers before the L1
universal templating pass (``${VAR}`` / auto-hashed gids / sync tokens).
``.with_args`` is required — ``@pytest.mark.cassette_mask(fn)`` treats
``fn`` as the decorated test function and silently drops the marker.

Use this layer when the L2 ``resource_type``-aware pass in
``_before_record_response`` cannot reach the PII — typically when the
recorded response object lacks ``resource_type`` (e.g. ``/batch``
sub-responses whose action did not request the field) or when the value
is buried inside an API-specific shape that the schema-keyed L2
dispatcher does not know how to walk.

Helpers should write the **bound value** (``_bindings()["USER_NAME"]``,
etc.) rather than the ``${VAR}`` placeholder. The L1 templating pass
that runs afterward rewrites bound values into ``${VAR}`` so the bound
value is the single source of truth.
"""

from __future__ import annotations

import json
from typing import Any

# ``_bindings`` lives in ``tests/e2e/conftest.py``, which pytest imports as the
# package-qualified module ``e2e.conftest`` (the bare top-level ``conftest`` is
# the project-root ``tests/conftest.py``, a different file).
from e2e.conftest import _bindings


def _decode(value: Any) -> str | None:
    """Return *value* as ``str`` whether it came in as ``bytes`` or ``str``."""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(value, str):
        return value
    return None


def mask_users_in_batch_subresponses(cassette_dict: Any) -> None:
    """Replace ``user.name`` in ``/batch`` sub-responses for ``/users/*`` actions.

    The batch endpoint mirrors each sub-action's response under
    ``data[i].body.data``. When the action's ``options.fields`` omits
    ``resource_type`` (Asana only returns fields you ask for), the L2
    hook in ``conftest.py`` cannot classify the object as a user and
    leaves ``name`` untouched — the recording account's real display
    name then leaks into the cassette.

    This masker keys off the *request* (``actions[i].relative_path``
    starting with ``/users/``) instead of the response shape, so it
    works regardless of which fields were requested. Iteration is
    zipped index-wise: a sub-response at position ``i`` belongs to the
    action at position ``i``, per Asana's documented contract.
    """
    name_bind = _bindings().get("USER_NAME")
    if not name_bind:
        return

    for interaction in cassette_dict.get("interactions", []):
        if not isinstance(interaction, dict):
            continue
        request = interaction.get("request") or {}
        if "/batch" not in str(request.get("uri", "")):
            continue
        req_text = _decode(request.get("body"))
        response = interaction.get("response") or {}
        body_obj = response.get("body") or {}
        resp_raw = body_obj.get("string")
        resp_text = _decode(resp_raw)
        if req_text is None or resp_text is None:
            continue
        try:
            req = json.loads(req_text)
            resp = json.loads(resp_text)
        except json.JSONDecodeError:
            continue

        req_data = req.get("data") if isinstance(req, dict) else None
        actions = req_data.get("actions") if isinstance(req_data, dict) else None
        sub_results = resp.get("data") if isinstance(resp, dict) else None
        if not isinstance(actions, list) or not isinstance(sub_results, list):
            continue

        modified = False
        # ``strict=False``: Asana guarantees ``len(sub_results) == len(actions)``,
        # but masking should degrade gracefully when a malformed cassette
        # is fed in — truncating to the shorter list is what we want.
        for action, sub in zip(actions, sub_results, strict=False):
            if not (isinstance(action, dict) and isinstance(sub, dict)):
                continue
            if not str(action.get("relative_path", "")).startswith("/users/"):
                continue
            sub_data = (sub.get("body") or {}).get("data")
            if isinstance(sub_data, dict) and "name" in sub_data:
                sub_data["name"] = name_bind
                modified = True

        if not modified:
            continue
        rewritten = json.dumps(resp, ensure_ascii=False, separators=(",", ":"))
        body_obj["string"] = rewritten.encode("utf-8") if isinstance(resp_raw, bytes) else rewritten
