from __future__ import annotations

import contextlib
import csv
import functools
import io
import json
import sys
from typing import Any, NoReturn

import click
import jq as jqlib
from asana.rest import ApiException
from tabulate import tabulate

from asana_api_cli.session import runtime


def formatted(f: Any) -> Any:
    """Decorator that adds --output / --query and auto-formats the returned data."""

    @click.option(
        "--output",
        "output_format",
        type=click.Choice(["json", "table", "csv", "text"], case_sensitive=False),
        default="json",
        help="Output format (default: json) [asana-api extension]",
    )
    @click.option(
        "--query",
        "jq_query",
        default=None,
        help="jq expression to filter output [asana-api extension]",
    )
    @click.option(
        "--csv-bom",
        "csv_bom",
        is_flag=True,
        default=False,
        help=(
            "Prepend a UTF-8 BOM to CSV output so Excel on Windows renders "
            "non-ASCII characters correctly [asana-api extension]"
        ),
    )
    @functools.wraps(f)
    def wrapper(
        *args: Any,
        output_format: str,
        jq_query: str | None,
        csv_bom: bool,
        **kwargs: Any,
    ) -> None:
        try:
            data = f(*args, **kwargs)
            # Collapse the asana SDK PageIterator / generator into a list
            if not isinstance(data, (dict, list, str, int, float, bool, type(None))):
                with contextlib.suppress(TypeError):
                    data = list(data)
        except ApiException as e:
            _handle_api_exception(e)
        _format_output(data, output_format=output_format, jq_query=jq_query, csv_bom=csv_bom)

    return wrapper


def _handle_api_exception(e: ApiException) -> NoReturn:
    """Print an Asana API error in human-readable form and exit."""
    status = e.status or "error"
    messages: list[str] = []
    body = e.body
    if isinstance(body, bytes):
        with contextlib.suppress(UnicodeDecodeError):
            body = body.decode("utf-8")
    if isinstance(body, str):
        with contextlib.suppress(json.JSONDecodeError):
            payload = json.loads(body)
            if isinstance(payload, dict):
                for err in payload.get("errors") or []:
                    if isinstance(err, dict) and "message" in err:
                        messages.append(str(err["message"]))
    if not messages:
        messages.append(e.reason or "Unknown API error")
    for msg in messages:
        click.echo(f"Error ({status}): {msg}", err=True)
    # When the body was not JSON, show a hint and,
    # in debug mode, dump the raw body so the user can diagnose the issue.
    if isinstance(body, str) and body and not _is_json(body):
        click.echo(
            "The server returned a non-JSON response. "
            "Re-run with --debug to see the full response body.",
            err=True,
        )
        if runtime.debug:
            click.echo("--- raw response body ---", err=True)
            click.echo(body, err=True)
            click.echo("--- end of response body ---", err=True)
    sys.exit(1)


def _is_json(text: str) -> bool:
    """Return True if *text* looks like JSON."""
    try:
        json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return False
    return True


def _format_output(
    data: Any, *, output_format: str, jq_query: str | None, csv_bom: bool = False
) -> None:
    # ``--query EXPR`` is treated as the equivalent of piping through
    # ``jq 'EXPR'``: jq may yield 0, 1, or many values and each output
    # format renders them naturally. When no query is given, the data
    # is treated as a single yield.
    if jq_query:
        try:
            results = jqlib.all(jq_query, data)
        except ValueError as e:
            click.echo(f"Invalid jq expression: {e}", err=True)
            sys.exit(1)
    else:
        results = [data]

    if output_format == "json":
        # Stream of values: each yield is its own JSON document. Matches
        # external ``jq``'s default output.
        for v in results:
            click.echo(json.dumps(v, indent=2, ensure_ascii=False))
        return

    if output_format == "text":
        for v in results:
            _print_text(v)
        return

    # table / csv: collect rows from every yield. If no yield is rowable
    # (all scalars, e.g. ``.data | length`` or ``.data[] | .name``),
    # fall through to plain printing per scalar instead. Mixed yields
    # (some rowable, some scalar) render only the rowable ones — scalars
    # are dropped silently in that uncommon case.
    rows: list[dict[str, Any]] = []
    non_rowable: list[Any] = []
    for v in results:
        r = _to_rows(v)
        if r is None:
            non_rowable.append(v)
        else:
            rows.extend(r)

    if not rows and non_rowable:
        for v in non_rowable:
            click.echo(v)
        return

    if output_format == "table":
        click.echo(tabulate(rows, headers="keys", tablefmt="simple"))
    elif output_format == "csv":
        _print_csv(rows, with_bom=csv_bom)


def _to_rows(data: Any) -> list[dict[str, Any]] | None:
    """Convert data into a list of dicts for table/csv. Return None if not possible."""
    if isinstance(data, list):
        if not data:
            return []
        # A jq yield can produce a mixed list (e.g. `[.data[0], .data | length]`)
        # which has no clean column layout. Require homogeneous shape.
        if all(isinstance(item, dict) for item in data):
            return data
        if not any(isinstance(item, dict) for item in data):
            return [{"value": v} for v in data]
        return None
    if isinstance(data, dict):
        return [data]
    return None


def _print_text(data: Any) -> None:
    """Print data in plain text format (like ``aws --output text``)."""
    if data is None:
        click.echo("None")
        return
    if isinstance(data, (str, int, float, bool)):
        click.echo(data)
        return
    if isinstance(data, dict):
        click.echo("\t".join(str(v) for v in data.values()))
        return
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                click.echo("\t".join(str(v) for v in item.values()))
            else:
                click.echo(item)
        return
    click.echo(data)


def _print_csv(rows: list[dict[str, Any]], *, with_bom: bool = False) -> None:
    if not rows:
        return
    buf = io.StringIO()
    if with_bom:
        buf.write("\ufeff")
    # Asana responses often have optional fields that appear on some rows
    # and not others (e.g. ``due_on``). Collect the union of keys across
    # all rows so no row's data is silently dropped.
    # ``dict.fromkeys`` preserves insertion order (Python 3.7+), giving a
    # stable column order based on first appearance.
    fieldnames = list(dict.fromkeys(key for row in rows for key in row))
    # lineterminator="\n" avoids Windows text-mode stdout translating the
    # csv module's default "\r\n" into "\r\r\n".
    writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    click.echo(buf.getvalue(), nl=False)
