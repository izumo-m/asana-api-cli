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
        help="Output format (default: json)",
    )
    @click.option(
        "--query",
        "jq_query",
        default=None,
        help="jq expression to filter output",
    )
    @click.option(
        "--csv-bom",
        "csv_bom",
        is_flag=True,
        default=False,
        help="Prepend a UTF-8 BOM to CSV output so Excel on Windows opens it without mojibake",
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
    if jq_query:
        try:
            data = jqlib.first(jq_query, data)
        except ValueError as e:
            click.echo(f"Invalid jq expression: {e}", err=True)
            sys.exit(1)

    if output_format == "text":
        _print_text(data)
        return

    if output_format == "json":
        click.echo(json.dumps(data, indent=2, ensure_ascii=False))
        return

    rows = _to_rows(data)
    if rows is None:
        click.echo(data)
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
        if isinstance(data[0], dict):
            return data
        return [{"value": v} for v in data]
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
    # lineterminator="\n" avoids Windows text-mode stdout translating the
    # csv module's default "\r\n" into "\r\r\n".
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    click.echo(buf.getvalue(), nl=False)
