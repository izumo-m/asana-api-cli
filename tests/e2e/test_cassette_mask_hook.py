"""Unit tests for the L3 cassette-mask hook.

These tests do not touch the network. They exercise the wiring that
``@pytest.mark.cassette_mask.with_args(fn, ...)`` adds: the
``pytest_runtest_setup`` hook in ``conftest.py`` registers the listed
callables into ``_active_maskers``, ``_templated_yaml_serialize``
invokes them before the L1 universal templating pass, and
``pytest_runtest_teardown`` drains the list after every fixture
teardown for the test has finished.

* U-1..U-4 cover the hook wiring itself.
* The ``mask_users_in_batch_subresponses`` cases (N-2 / N-3 from the
  spec, a positive baseline, and a non-batch-interaction negative
  case) verify that the first L3 helper masks only what it should.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from e2e import conftest as cf
from e2e._maskers import mask_users_in_batch_subresponses


# Spies used by U-2 / U-3: append a marker to ``cassette["__spy__"]`` so
# the test body can read back the invocation order.
def _spy_a(cassette: Any) -> None:
    cassette.setdefault("__spy__", []).append("a")


def _spy_b(cassette: Any) -> None:
    cassette.setdefault("__spy__", []).append("b")


# ---------- U-1..U-4: marker / fixture wiring -------------------------------


def test_u1_unmarked_test_has_no_active_maskers() -> None:
    """Tests without the marker leave ``_active_maskers`` empty."""
    assert cf._active_maskers == []


@pytest.mark.cassette_mask.with_args(_spy_a)
def test_u2_single_marker_is_invoked_by_serializer() -> None:
    """A single ``cassette_mask`` arg is registered AND runs against the cassette."""
    assert cf._active_maskers == [_spy_a]
    cassette: dict[str, Any] = {"interactions": [], "version": 1}
    cf._templated_yaml_serialize(cassette)
    assert cassette.get("__spy__") == ["a"]


@pytest.mark.cassette_mask.with_args(_spy_a, _spy_b)
def test_u3_multiple_maskers_run_in_declared_order() -> None:
    """Multiple varargs in a single marker run left-to-right."""
    assert cf._active_maskers == [_spy_a, _spy_b]
    cassette: dict[str, Any] = {"interactions": [], "version": 1}
    cf._templated_yaml_serialize(cassette)
    assert cassette.get("__spy__") == ["a", "b"]


def test_u4_teardown_hook_drains_active_maskers() -> None:
    """``pytest_runtest_teardown`` unconditionally clears ``_active_maskers``.

    This is the cleanup contract that survives a test crash: pytest
    always invokes ``pytest_runtest_teardown`` after the test phase,
    even when the test body raises, so a stray masker can never leak
    into the next test.
    """

    class _FakeItem:
        pass

    cf._active_maskers.extend([_spy_a, _spy_b])
    assert cf._active_maskers == [_spy_a, _spy_b]
    cf.pytest_runtest_teardown(_FakeItem(), None)  # type: ignore[arg-type]
    assert cf._active_maskers == []


# ---------- mask_users_in_batch_subresponses --------------------------------


def _batch_interaction(
    actions: list[dict[str, Any]],
    sub_results: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "request": {
            "uri": "https://app.asana.com/api/1.0/batch",
            "method": "POST",
            "body": json.dumps({"data": {"actions": actions}}),
        },
        "response": {"body": {"string": json.dumps({"data": sub_results})}},
    }


def _user_sub(name: str = "izumoma") -> dict[str, Any]:
    """A /users/me sub-response shaped as it is when ``resource_type`` is NOT requested."""
    return {"status_code": 200, "headers": [], "body": {"data": {"gid": "1", "name": name}}}


def test_mask_users_replaces_name_with_user_name_binding() -> None:
    """Positive baseline: a leaked real name is rewritten to the bound ``USER_NAME``."""
    cassette = {
        "interactions": [
            _batch_interaction(
                actions=[{"relative_path": "/users/me", "method": "get"}],
                sub_results=[_user_sub("izumoma")],
            ),
        ],
    }
    mask_users_in_batch_subresponses(cassette)
    body = json.loads(cassette["interactions"][0]["response"]["body"]["string"])
    assert body["data"][0]["body"]["data"]["name"] == "E2E User"


def test_n2_mask_users_skips_non_user_sub_actions() -> None:
    """Sub-actions outside ``/users/*`` are untouched — names there are not PII by class."""
    cassette = {
        "interactions": [
            _batch_interaction(
                actions=[
                    {"relative_path": "/tasks/123", "method": "get"},
                    {"relative_path": "/users/me", "method": "get"},
                ],
                sub_results=[
                    {
                        "status_code": 200,
                        "headers": [],
                        "body": {"data": {"gid": "1", "name": "Task name stays"}},
                    },
                    _user_sub("izumoma"),
                ],
            ),
        ],
    }
    mask_users_in_batch_subresponses(cassette)
    body = json.loads(cassette["interactions"][0]["response"]["body"]["string"])
    assert body["data"][0]["body"]["data"]["name"] == "Task name stays"
    assert body["data"][1]["body"]["data"]["name"] == "E2E User"


def test_n3_mask_users_skips_non_json_request_body() -> None:
    """Multipart upload bodies (or any non-JSON shape) are skipped without raising.

    Asserts both ``no exception`` AND ``response untouched`` so a future
    refactor that papers over the failure by mutating the response cannot
    pass quietly.
    """
    original_response = '{"data":[]}'
    cassette = {
        "interactions": [
            {
                "request": {
                    "uri": "https://app.asana.com/api/1.0/batch",
                    "method": "POST",
                    "body": (
                        "--boundary\r\nContent-Disposition: form-data; "
                        'name="file"\r\n\r\n<binary>\r\n--boundary--'
                    ),
                },
                "response": {"body": {"string": original_response}},
            },
        ],
    }
    mask_users_in_batch_subresponses(cassette)
    assert cassette["interactions"][0]["response"]["body"]["string"] == original_response


def test_mask_users_ignores_non_batch_interactions() -> None:
    """Direct (non-batch) ``/users/me`` calls are not rewritten by this helper.

    L2 (``_before_record_response`` in ``conftest.py``) is responsible
    for that case; doubling up here would be confusing for readers.
    """
    cassette = {
        "interactions": [
            {
                "request": {
                    "uri": "https://app.asana.com/api/1.0/users/me",
                    "method": "GET",
                    "body": None,
                },
                "response": {
                    "body": {
                        "string": json.dumps({"data": {"gid": "1", "name": "izumoma"}}),
                    },
                },
            },
        ],
    }
    mask_users_in_batch_subresponses(cassette)
    body = json.loads(cassette["interactions"][0]["response"]["body"]["string"])
    assert body["data"]["name"] == "izumoma"
