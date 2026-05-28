from __future__ import annotations

import csv
import functools
import io
import json
import sys
import traceback
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
        type=click.Choice(["json", "table", "csv", "text", "none"], case_sensitive=False),
        default="json",
        help=(
            "Output format (default: json). 'none' suppresses the success "
            "payload entirely — useful when only the exit code matters "
            "(e.g. side-effect-only operations like delete-task). "
            "Symmetric counterpart of --output-errors 'none' "
            "[asana-api extension]"
        ),
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
            # Iterator consumption is done inside the session context in
            # ``cli.py:_make_command`` (Layer B post-judge via
            # ``isinstance(result, collections.abc.Iterator)``). Iterating
            # here — outside that context — would leak ``Authorization``
            # into ``--debug`` log on multi-page iterators, so the upstream
            # gate is load-bearing.
        except (
            click.exceptions.ClickException,
            click.exceptions.Abort,
            click.exceptions.Exit,
        ):
            # Click's own control flow (BadParameter, ctx.exit, Ctrl-C in
            # prompts) is not an "API call exception" — let Click handle it.
            raise
        except Exception as e:
            # Always echo the exception (no traceback frames) to stderr.
            # For ApiException this includes status / reason / headers /
            # body — the useful payload (e.g. the 412 sync-token body
            # in events polling) stays visible without traceback noise.
            _echo_exception_only(e)
            if runtime.output_errors == "none":
                sys.exit(1)
            # Otherwise also render a ``{exception, ...}`` envelope on
            # stdout and exit 3.
            _handle_exception(e)
        _format_output(data, output_format=output_format, jq_query=jq_query, csv_bom=csv_bom)

    return wrapper


def _qualified_exception_name(e: BaseException) -> str:
    """Return ``module.qualname`` so SDK users can import the same symbol.

    Example: ``urllib3.exceptions.MaxRetryError`` — readers can
    ``from urllib3.exceptions import MaxRetryError`` to handle the same
    error in their own SDK code. Built-ins surface as
    ``builtins.<name>``; the ``builtins.`` prefix is technically
    correct and is left as-is rather than special-cased.
    """
    cls = type(e)
    return f"{cls.__module__}.{cls.__qualname__}"


def _echo_exception_only(e: BaseException) -> None:
    """Write ``traceback.format_exception_only`` output to stderr.

    Format: qualified class name + the exception's ``__str__``, no
    traceback frames. ``ApiException.__str__`` is multi-line, so the
    full stderr block looks like::

        asana.rest.ApiException: (412)
        Reason: Precondition Failed
        HTTP response headers: {...}
        HTTP response body: b'{...}'

    — i.e. the full HTTP response is visible without re-deriving it
    from the envelope.

    Always written from :func:`formatted` (both
    ``--output-errors=none`` and the envelope formats), so the raw
    exception stays visible even when ``--query-errors`` would
    otherwise strip it from stdout.
    """
    click.echo(
        "".join(traceback.format_exception_only(type(e), e)),
        err=True,
        nl=False,
    )


def _handle_exception(e: Exception) -> NoReturn:
    """Render an exception envelope on stdout and exit 3.

    Only called when ``runtime.output_errors`` is one of
    ``json|text|csv|table`` (an envelope format was explicitly
    requested). The stderr echo of the exception is done upstream in
    :func:`formatted` (via :func:`_echo_exception_only`) before this
    function runs, so both the ``none`` and envelope branches share
    the same stderr format.

    ApiException carries full HTTP context: 5-field envelope
    ``{exception, status, reason, body, headers}`` where ``body`` is
    the UTF-8 decoded response *string* (or null). Other exceptions
    (urllib3 connection errors, etc.) collapse to the 2-field
    ``{exception, reason}`` since status/body/headers have no HTTP
    meaning. The ``exception`` field is always the qualified
    ``module.qualname`` form. See ``docs/sdk-deviations.md`` for the
    full schema.

    The envelope lands on **stdout** (not stderr) so that
    ``exit_code == 3`` paired with stdout-only consumption gives a
    clean machine-readable error channel — independent of whatever
    noise urllib3 or other libraries write to stderr. Exit code is
    ``3`` for the rendered envelope; a malformed ``--query-errors``
    expression short-circuits with exit ``2`` from inside
    :func:`_format_output` (user-input error, per
    ``docs/exit-codes.md``).
    """
    envelope: dict[str, Any]
    if isinstance(e, ApiException):
        raw_body = e.body
        body_text: str | None
        if isinstance(raw_body, (bytes, bytearray)):
            body_text = bytes(raw_body).decode("utf-8", errors="replace")
        elif isinstance(raw_body, str):
            body_text = raw_body
        else:
            body_text = None
        envelope = {
            "exception": _qualified_exception_name(e),
            "status": e.status,
            "reason": e.reason,
            "body": body_text,
            "headers": dict(e.headers) if e.headers is not None else None,
        }
    else:
        envelope = {
            "exception": _qualified_exception_name(e),
            "reason": str(e),
        }

    _format_output(
        envelope,
        output_format=runtime.output_errors,
        jq_query=runtime.query_errors,
    )
    sys.exit(3)


def _format_output(
    data: Any,
    *,
    output_format: str,
    jq_query: str | None,
    csv_bom: bool = False,
) -> None:
    """Render *data* on stdout.

    The same renderer powers both the success path (``--output``) and
    the error envelope path (``--output-errors``); both write to
    stdout, so scripts can consume them uniformly. ``exit_code``
    (``0`` vs ``3``) is the discriminator.
    """
    # ``--query EXPR`` is treated as the equivalent of piping through
    # ``jq 'EXPR'``: jq may yield 0, 1, or many values and each output
    # format renders them naturally. When no query is given, the data
    # is treated as a single yield.
    #
    # The jq pass runs *before* the ``output_format == "none"`` short-circuit
    # so the user-supplied expression is still validated (syntax + runtime
    # errors surface as exit 2). Skipping it when output is silenced would
    # make ``--query`` checking depend on the chosen format, masking
    # script-side jq bugs the moment ``--output none`` is added.
    if jq_query:
        try:
            results = jqlib.all(jq_query, data)
        except ValueError as e:
            click.echo(f"Invalid jq expression: {e}", err=True)
            sys.exit(2)
    else:
        results = [data]

    if output_format == "none":
        # ``--output none`` suppresses the success payload entirely. The jq
        # pass above has already executed (and exited 2 on any error), so
        # value-level validation is the same as for the rendered formats.
        return

    if output_format == "json":
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
            click.echo(_scalar_text(v))
        return

    # Stringify nested values (dict / list) as JSON so cells use JSON
    # syntax (`{"a":"b"}`) rather than Python repr (`{'a': 'b'}`).
    rows = [{k: _scalar_text(v) for k, v in row.items()} for row in rows]

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


def _scalar_text(value: Any) -> str:
    """Single-cell text representation.

    Scalars (None / str / int / float / bool) are stringified naturally;
    nested containers (dict / list) are JSON-encoded so that text / csv /
    table cells use JSON syntax (``{"a":"b"}``) rather than Python repr
    (``{'a': 'b'}``).
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _print_text(data: Any) -> None:
    """Print data in plain text format (like ``aws --output text``)."""
    if isinstance(data, dict):
        click.echo("\t".join(_scalar_text(v) for v in data.values()))
        return
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                click.echo("\t".join(_scalar_text(v) for v in item.values()))
            else:
                click.echo(_scalar_text(item))
        return
    click.echo(_scalar_text(data))


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
