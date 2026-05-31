"""Tests for asana_api_cli.structured_arg — the hybrid parser shared by
``--retry-strategy`` and ``--header-params``.

Covers the three input forms (JSON / @file / shorthand), the schema-driven
validation and type coercion, and the error path for list-typed fields in
shorthand.
"""

from __future__ import annotations

from pathlib import Path

import click
import pytest

from asana_api_cli.structured_arg import (
    RETRY_FIELD_SCHEMA,
    default_header_callback,
    parse_structured_arg,
)


class TestJsonForm:
    def test_simple_object(self) -> None:
        assert parse_structured_arg('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}

    def test_nested_object_accepted(self) -> None:
        assert parse_structured_arg('{"a": {"b": 2}}') == {"a": {"b": 2}}

    def test_invalid_json_rejected(self) -> None:
        with pytest.raises(click.BadParameter, match="Invalid JSON"):
            parse_structured_arg("{bad json")

    def test_only_brace_triggers_json_form(self) -> None:
        # Inputs starting with '[' fall through to shorthand on purpose;
        # JSON arrays are not a valid Configuration value for any of the
        # three structured options.
        with pytest.raises(click.BadParameter, match="Missing '='"):
            parse_structured_arg("[1, 2, 3]")


class TestFileForm:
    def test_reads_json_from_file(self, tmp_path: Path) -> None:
        f = tmp_path / "data.json"
        f.write_text('{"k": "v"}', encoding="utf-8")
        assert parse_structured_arg(f"@{f}") == {"k": "v"}

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(click.BadParameter, match="File not found"):
            parse_structured_arg(f"@{tmp_path / 'nope.json'}")

    def test_invalid_json_in_file(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.json"
        f.write_text("not json", encoding="utf-8")
        with pytest.raises(click.BadParameter, match="Invalid JSON"):
            parse_structured_arg(f"@{f}")

    def test_non_utf8_file(self, tmp_path: Path) -> None:
        f = tmp_path / "binary.bin"
        f.write_bytes(b"\x80\x81\x82")
        with pytest.raises(click.BadParameter, match="not valid UTF-8"):
            parse_structured_arg(f"@{f}")

    def test_file_containing_json_array_rejected(self, tmp_path: Path) -> None:
        # The file body must parse to a JSON object; arrays / scalars are
        # not valid Configuration values for any of the three options.
        f = tmp_path / "arr.json"
        f.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(click.BadParameter, match="Expected a JSON object"):
            parse_structured_arg(f"@{f}")


class TestShorthandUnsupervised:
    """Without a schema, all values stay as strings (the dict[str, str] case)."""

    def test_pairs_keep_string_values(self) -> None:
        assert parse_structured_arg("a=1,b=hello") == {"a": "1", "b": "hello"}

    def test_strips_whitespace_around_key_and_value(self) -> None:
        assert parse_structured_arg(" a = 1 , b = 2 ") == {"a": "1", "b": "2"}

    def test_duplicate_key_last_wins(self) -> None:
        assert parse_structured_arg("a=1,a=2") == {"a": "2"}

    def test_value_with_equals_kept_in_value(self) -> None:
        assert parse_structured_arg("token=ab=cd") == {"token": "ab=cd"}

    def test_empty_value_rejected(self) -> None:
        with pytest.raises(click.BadParameter, match="Empty value"):
            parse_structured_arg("")

    def test_missing_equals_rejected(self) -> None:
        with pytest.raises(click.BadParameter, match="Missing '='"):
            parse_structured_arg("noeq")

    def test_empty_key_rejected(self) -> None:
        with pytest.raises(click.BadParameter, match="Empty key"):
            parse_structured_arg("=value")


class TestSchemaValidation:
    """When a schema is given, unknown keys are rejected and values coerced."""

    def test_int_coercion(self) -> None:
        assert parse_structured_arg("total=10", schema={"total": int}) == {"total": 10}

    def test_int_invalid_value_rejected(self) -> None:
        with pytest.raises(click.BadParameter, match="expected int"):
            parse_structured_arg("total=abc", schema={"total": int})

    def test_float_coercion(self) -> None:
        result = parse_structured_arg("f=1.5", schema={"f": float})
        assert result == {"f": 1.5}

    def test_bool_true(self) -> None:
        assert parse_structured_arg("x=true", schema={"x": bool}) == {"x": True}

    def test_bool_false(self) -> None:
        assert parse_structured_arg("x=false", schema={"x": bool}) == {"x": False}

    def test_bool_case_insensitive(self) -> None:
        assert parse_structured_arg("x=TRUE", schema={"x": bool}) == {"x": True}
        assert parse_structured_arg("x=False", schema={"x": bool}) == {"x": False}

    def test_bool_rejects_numeric(self) -> None:
        # '1' / '0' would be ambiguous against int fields, so they are rejected.
        with pytest.raises(click.BadParameter, match="'true' or 'false'"):
            parse_structured_arg("x=1", schema={"x": bool})

    def test_unknown_key_in_shorthand_rejected(self) -> None:
        with pytest.raises(click.BadParameter, match="Unknown field"):
            parse_structured_arg("z=1", schema={"a": int})

    def test_unknown_key_in_json_rejected(self) -> None:
        with pytest.raises(click.BadParameter, match="Unknown field"):
            parse_structured_arg('{"z": 1}', schema={"a": int})

    def test_list_field_in_shorthand_rejected(self) -> None:
        # Commas inside a list value would collide with the pair separator;
        # the JSON form is the only way to express list-typed fields.
        with pytest.raises(click.BadParameter, match="list type"):
            parse_structured_arg("items=a", schema={"items": list})

    def test_list_field_in_json_accepted(self) -> None:
        result = parse_structured_arg('{"items": ["a", "b"]}', schema={"items": list})
        assert result == {"items": ["a", "b"]}

    def test_tuple_type_tries_bool_then_int(self) -> None:
        schema: dict[str, type | tuple[type, ...]] = {"redirect": (bool, int)}
        assert parse_structured_arg("redirect=true", schema=schema) == {"redirect": True}
        assert parse_structured_arg("redirect=false", schema=schema) == {"redirect": False}
        assert parse_structured_arg("redirect=5", schema=schema) == {"redirect": 5}

    def test_tuple_type_numeric_str_returns_int_not_bool(self) -> None:
        """``redirect=1`` must return int 1 (not bool True): urllib3's
        ``Retry(redirect=1)`` means "allow one redirect" whereas
        ``Retry(redirect=True)`` means "use the default redirect count" —
        the bool-vs-int distinction is semantically load-bearing.
        """
        schema: dict[str, type | tuple[type, ...]] = {"redirect": (bool, int)}
        result = parse_structured_arg("redirect=1", schema=schema)
        assert result == {"redirect": 1}
        assert isinstance(result["redirect"], int)
        assert not isinstance(result["redirect"], bool)
        # And the negative case for completeness.
        result = parse_structured_arg("redirect=0", schema=schema)
        assert result == {"redirect": 0}
        assert isinstance(result["redirect"], int)
        assert not isinstance(result["redirect"], bool)


class TestRetryFieldSchema:
    """The exported ``RETRY_FIELD_SCHEMA`` is used by ``--retry-strategy``.

    These tests pin the field list down so that adding/removing/typing one
    of them is a deliberate change visible in the test diff.
    """

    def test_covers_user_settable_retry_init_args(self) -> None:
        # ``history`` is excluded on purpose: it is internal state, not a
        # configuration knob.
        expected = {
            "total",
            "connect",
            "read",
            "status",
            "other",
            "redirect",
            "allowed_methods",
            "status_forcelist",
            "backoff_factor",
            "backoff_max",
            "backoff_jitter",
            "raise_on_redirect",
            "raise_on_status",
            "respect_retry_after_header",
            "remove_headers_on_redirect",
            "retry_after_max",
        }
        assert set(RETRY_FIELD_SCHEMA) == expected

    def test_list_typed_fields_pinned(self) -> None:
        for name in ("allowed_methods", "status_forcelist", "remove_headers_on_redirect"):
            assert RETRY_FIELD_SCHEMA[name] is list

    def test_redirect_accepts_bool_or_int(self) -> None:
        result = parse_structured_arg("redirect=false", schema=RETRY_FIELD_SCHEMA)
        assert result == {"redirect": False}
        result = parse_structured_arg("redirect=3", schema=RETRY_FIELD_SCHEMA)
        assert result == {"redirect": 3}

    def test_total_and_backoff_in_one_shorthand(self) -> None:
        result = parse_structured_arg(
            "total=20,backoff_factor=1.5,raise_on_status=false",
            schema=RETRY_FIELD_SCHEMA,
        )
        assert result == {"total": 20, "backoff_factor": 1.5, "raise_on_status": False}

    def test_list_field_in_shorthand_points_to_json(self) -> None:
        with pytest.raises(click.BadParameter, match="JSON form"):
            parse_structured_arg("status_forcelist=429", schema=RETRY_FIELD_SCHEMA)


def _default_headers(*tokens: str) -> dict[str, str] | None:
    """Run ``default_header_callback`` with a real (throwaway) Click context,
    mirroring how ``multiple=True`` hands it a tuple of raw tokens."""
    param = click.Option(["--set-default-header"], multiple=True)
    ctx = click.Context(click.Command("test"))
    return default_header_callback(ctx, param, tokens)


class TestDefaultHeaderCallback:
    """``--set-default-header NAME=VALUE`` (repeatable) parser."""

    def test_not_given_returns_none(self) -> None:
        # Matches the unset sentinel the other globals use.
        assert _default_headers() is None

    def test_single_pair(self) -> None:
        assert _default_headers("X-Foo=bar") == {"X-Foo": "bar"}

    def test_multiple_pairs_accumulate(self) -> None:
        assert _default_headers("A=1", "B=2") == {"A": "1", "B": "2"}

    def test_value_may_contain_equals(self) -> None:
        # Only the first '=' is the separator (header values like base64 or
        # query-ish strings can contain '='), which is exactly why this is a
        # repeatable option and not the comma-split shorthand parser.
        assert _default_headers("A=b=c") == {"A": "b=c"}

    def test_name_is_trimmed_value_is_verbatim(self) -> None:
        assert _default_headers(" X-Foo = bar ") == {"X-Foo": " bar "}

    def test_missing_equals_rejected(self) -> None:
        with pytest.raises(click.BadParameter, match="NAME=VALUE"):
            _default_headers("noequals")

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(click.BadParameter, match="NAME=VALUE"):
            _default_headers("=value")
