"""Hybrid parser for structured option values.

Used by `--retry-strategy` and `--header-params` so they share a single
input format. The parser dispatches by the
first character of the value:

* ``{...}`` — parse as a JSON object
* ``@<path>`` — read the file at ``<path>`` and parse it as a JSON object
* anything else — parse as shorthand ``key=value[,key=value...]``

Shorthand only supports scalar values (``int`` / ``float`` / ``bool`` /
``str``). Fields whose declared type is a list (or any other container)
must be provided via the JSON form; passing them in shorthand raises
``click.BadParameter`` with a hint to use the ``{...}`` / ``@file`` form.

When a ``schema`` mapping is supplied, unknown keys are rejected and
shorthand values are coerced to the declared type. Without a schema the
result is whatever JSON parsed (for the JSON / file forms) or
``dict[str, str]`` for shorthand — which is what the dict-typed
``--header-params`` value wants.

Bool values in shorthand accept only ``true`` / ``false`` (case
insensitive). ``1`` / ``0`` are intentionally rejected so int and bool
fields cannot be confused by readers of the command line.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

_BOOL_TRUE = {"true"}
_BOOL_FALSE = {"false"}


# Schema for ``--retry-strategy``. Each value is the Python type the
# shorthand parser will coerce to. ``list`` is a sentinel: list-typed
# fields cannot be expressed in shorthand and must be supplied via the
# JSON form. ``redirect`` is special — urllib3 accepts either an int
# (max number of redirects) or ``False`` (disallow), so the parser tries
# bool first then falls back to int.
RETRY_FIELD_SCHEMA: dict[str, type | tuple[type, ...]] = {
    "total": int,
    "connect": int,
    "read": int,
    "status": int,
    "other": int,
    "redirect": (bool, int),
    "allowed_methods": list,
    "status_forcelist": list,
    "backoff_factor": float,
    "backoff_max": float,
    "backoff_jitter": float,
    "raise_on_redirect": bool,
    "raise_on_status": bool,
    "respect_retry_after_header": bool,
    "remove_headers_on_redirect": list,
    "retry_after_max": int,
}


def parse_structured_arg(
    value: str,
    *,
    schema: dict[str, type | tuple[type, ...]] | None = None,
) -> dict[str, Any]:
    """Parse a CLI value into a dict.

    See the module docstring for the dispatch and schema rules. Raises
    ``click.BadParameter`` on any malformed input or schema mismatch so the
    caller can pass the error straight to Click.
    """
    if value == "":
        raise click.BadParameter("Empty value; pass '{}' for an empty object.")

    first = value[0]
    if first == "{":
        parsed = _parse_json_object(value)
        if schema is not None:
            _validate_keys(parsed, schema)
        return parsed
    if first == "@":
        path = Path(value[1:])
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise click.BadParameter(f"File not found: {path}") from exc
        except UnicodeDecodeError as exc:
            raise click.BadParameter(f"File {path} is not valid UTF-8: {exc}") from exc
        except OSError as exc:
            raise click.BadParameter(f"Cannot read file {path}: {exc}") from exc
        parsed = _parse_json_object(raw)
        if schema is not None:
            _validate_keys(parsed, schema)
        return parsed
    return _parse_shorthand(value, schema)


def _parse_json_object(text: str) -> dict[str, Any]:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise click.BadParameter(f"Invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise click.BadParameter(f"Expected a JSON object, got {type(obj).__name__}.")
    return obj


def _validate_keys(parsed: dict[str, Any], schema: dict[str, type | tuple[type, ...]]) -> None:
    unknown = sorted(k for k in parsed if k not in schema)
    if unknown:
        allowed = ", ".join(sorted(schema))
        raise click.BadParameter(f"Unknown field(s): {', '.join(unknown)}. Allowed: {allowed}.")


def _parse_shorthand(
    value: str,
    schema: dict[str, type | tuple[type, ...]] | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw_pair in value.split(","):
        pair = raw_pair.strip()
        if "=" not in pair:
            raise click.BadParameter(
                f"Missing '=' in shorthand pair: {pair!r}. "
                "Use 'key=value[,key=value...]' or a JSON object."
            )
        key, raw_val = pair.split("=", 1)
        key = key.strip()
        raw_val = raw_val.strip()
        if not key:
            raise click.BadParameter("Empty key in shorthand pair.")
        if schema is None:
            result[key] = raw_val
            continue
        if key not in schema:
            allowed = ", ".join(sorted(schema))
            raise click.BadParameter(f"Unknown field: {key!r}. Allowed: {allowed}.")
        result[key] = _coerce_value(key, raw_val, schema[key])
    return result


def _coerce_value(key: str, raw: str, expected: type | tuple[type, ...]) -> Any:
    if isinstance(expected, tuple):
        last_error: click.BadParameter | None = None
        for typ in expected:
            try:
                return _coerce_value(key, raw, typ)
            except click.BadParameter as exc:
                last_error = exc
        assert last_error is not None
        names = " or ".join(t.__name__ for t in expected)
        raise click.BadParameter(f"Field {key!r}: expected {names}, got {raw!r}.")
    if expected is bool:
        lower = raw.lower()
        if lower in _BOOL_TRUE:
            return True
        if lower in _BOOL_FALSE:
            return False
        raise click.BadParameter(f"Field {key!r}: expected 'true' or 'false', got {raw!r}.")
    if expected is int:
        try:
            return int(raw)
        except ValueError as exc:
            raise click.BadParameter(f"Field {key!r}: expected int, got {raw!r}.") from exc
    if expected is float:
        try:
            return float(raw)
        except ValueError as exc:
            raise click.BadParameter(f"Field {key!r}: expected float, got {raw!r}.") from exc
    if expected is str:
        return raw
    type_name = getattr(expected, "__name__", str(expected))
    raise click.BadParameter(
        f"Field {key!r} has {type_name} type; use the JSON form ('{{...}}' or @file)."
    )


def click_callback(
    *,
    schema: dict[str, type | tuple[type, ...]] | None = None,
) -> Callable[[click.Context, click.Parameter, str | None], dict[str, Any] | None]:
    """Build a Click ``callback`` that parses a structured option value.

    Returns the original ``None`` untouched when the user did not supply
    the flag, so a missing value never trips the parser's empty-string
    guard. Otherwise delegates to :func:`parse_structured_arg` with the
    supplied schema.
    """

    def _cb(
        _ctx: click.Context, _param: click.Parameter, value: str | None
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        return parse_structured_arg(value, schema=schema)

    return _cb
