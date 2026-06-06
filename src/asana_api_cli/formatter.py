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


def make_formatter_options() -> list[click.Option]:
    """Fresh ``click.Option`` instances for the output-formatting flags consumed
    by :func:`formatted`: the success path ``--output`` / ``--query`` /
    ``--csv-bom`` and the error path ``--exception-output`` / ``--exception-query``.
    Returned in display order. Fresh instances each call because click stores
    per-command state on Option objects (same reason as
    ``cli._make_per_call_kwarg_options``).
    """
    return [
        click.Option(
            ["--output", "output_format"],
            type=click.Choice(["json", "table", "csv", "text", "none"], case_sensitive=False),
            default="json",
            help=(
                "Output format (default: json). 'none' suppresses the success "
                "payload entirely — useful when only the exit code matters "
                "(e.g. side-effect-only operations like delete-task). "
                "Symmetric counterpart of --exception-output 'none' "
                "(asana-api: extension)"
            ),
        ),
        click.Option(
            ["--query", "jq_query"],
            default=None,
            help="jq expression to filter output (asana-api: extension)",
        ),
        click.Option(
            ["--csv-bom", "csv_bom"],
            is_flag=True,
            default=False,
            help=(
                "Prepend a UTF-8 BOM to CSV output so Excel on Windows renders "
                "non-ASCII characters correctly (asana-api: extension)"
            ),
        ),
        click.Option(
            ["--exception-output", "exception_output"],
            type=click.Choice(["none", "json", "text", "csv", "table"], case_sensitive=False),
            default="none",
            show_default=True,
            help=(
                "How to surface exceptions from the SDK call. The exception is "
                "always echoed to stderr without traceback frames (for "
                "ApiException this includes status/reason/headers/body). 'none' "
                "(default) then exits 1 with no envelope. json/text/csv/table "
                "additionally render an envelope on stdout and exit 3 — "
                "{exception, status, reason, body, headers} for ApiException, "
                "{exception, reason} for other exceptions "
                "(asana-api: extension)"
            ),
        ),
        click.Option(
            ["--exception-query", "exception_query"],
            default=None,
            help=(
                "Apply a jq filter to the error envelope; result is rendered via "
                "--exception-output. Pairing with the default 'none' emits a stderr "
                "warning (the filter would be a no-op) but does not block the call "
                "(asana-api: extension)"
            ),
        ),
    ]


def formatted(f: Any) -> Any:
    """Decorator that renders the result of the wrapped SDK call.

    The output-formatting options it consumes — the success path ``--output`` /
    ``--query`` / ``--csv-bom`` and the error path ``--exception-output`` /
    ``--exception-query`` — are declared by :func:`make_formatter_options` and
    added to each command's params by ``cli.py:_make_command``; this wrapper
    receives their parsed values as keyword arguments. They are per-command
    (leaf) options bound to the single method invocation, not global flags.
    """

    @functools.wraps(f)
    def wrapper(
        *args: Any,
        output_format: str,
        jq_query: str | None,
        csv_bom: bool,
        exception_output: str,
        exception_query: str | None,
        **kwargs: Any,
    ) -> None:
        # ``--exception-query`` paired with the default ``--exception-output none``
        # has no envelope to filter — the expression would silently do nothing.
        # Warn (don't block) so the underlying call result / exception is
        # preserved rather than masked by a usage error. Fires on every
        # invocation regardless of outcome, mirroring the success path.
        if exception_output == "none" and exception_query is not None:
            click.echo(
                "warning: --exception-query is ignored when --exception-output is "
                "'none' (the default) — pass --exception-output {json,text,csv,table} "
                "to enable error filtering.",
                err=True,
            )
        try:
            data = f(*args, **kwargs)
            # Iterator consumption is done inside the session context in
            # ``cli.py:execute_call_plan`` (Layer B post-judge via
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
            if exception_output == "none":
                sys.exit(1)
            # Otherwise also render a ``{exception, ...}`` envelope on
            # stdout and exit 3.
            _handle_exception(e, exception_output=exception_output, exception_query=exception_query)
        _format_output(data, output_format=output_format, jq_query=jq_query, csv_bom=csv_bom)

    return wrapper


def formatter_flag_names() -> frozenset[str]:
    """Flag strings declared by :func:`make_formatter_options` (``--output`` /
    ``--query`` / ``--csv-bom`` / ``--exception-output`` / ``--exception-query``).

    Derived from the option builder (not a hand-kept list) so it cannot drift
    from the actual options — including when those flags are later renamed.
    ``cli.py`` uses it to detect when an SDK arg/opt name collides with one of
    these built-in flags.
    """
    flags: set[str] = set()
    for p in make_formatter_options():
        flags.update(p.opts)
        flags.update(getattr(p, "secondary_opts", []))
    return frozenset(flags)


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
    ``--exception-output=none`` and the envelope formats), so the raw
    exception stays visible even when ``--exception-query`` would
    otherwise strip it from stdout.
    """
    click.echo(
        "".join(traceback.format_exception_only(type(e), e)),
        err=True,
        nl=False,
    )


def _handle_exception(
    e: Exception, *, exception_output: str, exception_query: str | None
) -> NoReturn:
    """Render an exception as an envelope on stdout, then exit 3.

    Only reached for the envelope formats (``json|text|csv|table``); the
    ``none`` path and the stderr echo are handled upstream in
    :func:`formatted`. For the envelope schema and exit-code contract see
    ``docs/usage.md`` ("Error handling"); for the rationale,
    ``docs/sdk-deviations.md``.
    """
    envelope: dict[str, Any]
    if isinstance(e, ApiException):
        # ApiException carries full HTTP context → 5-field envelope.
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
        # No HTTP context → 2-field envelope.
        envelope = {
            "exception": _qualified_exception_name(e),
            "reason": str(e),
        }

    _format_output(
        envelope,
        output_format=exception_output,
        jq_query=exception_query,
    )
    # Envelope written to stdout; exit 3 is the API / connection-error code.
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
    the error envelope path (``--exception-output``); both write to
    stdout, so scripts can consume them uniformly. The caller's exit code
    (``0`` for success, ``3`` for the error-envelope path) is the discriminator.
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
            click.echo(format_json(v))
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
        r = to_rows(v)
        if r is None:
            non_rowable.append(v)
        else:
            rows.extend(r)

    if not rows and non_rowable:
        for v in non_rowable:
            click.echo(scalar_text(v))
        return

    # Stringify nested values (dict / list) as JSON so cells use JSON
    # syntax (`{"a":"b"}`) rather than Python repr (`{'a': 'b'}`).
    rows = [{k: scalar_text(v) for k, v in row.items()} for row in rows]

    if output_format == "table":
        # Skip empty data instead of emitting a spurious blank line:
        # ``tabulate([], ...)`` returns ``""`` and ``click.echo("")`` would
        # still write a newline. Matches ``_print_csv``'s empty-rows guard.
        if rows:
            click.echo(format_table(rows))
    elif output_format == "csv":
        _print_csv(rows, with_bom=csv_bom)


def to_rows(data: Any) -> list[dict[str, Any]] | None:
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


def scalar_text(value: Any) -> str:
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


def format_json(value: Any) -> str:
    """JSON text for one value (indent 2, non-ASCII preserved). Pure (no I/O)."""
    return json.dumps(value, indent=2, ensure_ascii=False)


def format_table(rows: list[dict[str, Any]]) -> str:
    """Table text for *rows*. Pure (no I/O); the caller guards the empty case."""
    return tabulate(rows, headers="keys", tablefmt="simple")


def format_text(value: Any) -> str:
    """Plain-text representation of a single value (like ``aws --output text``).

    A dict renders as its values tab-joined; anything else falls back to
    :func:`scalar_text`. Pure (no I/O); the top-level list iteration lives in
    :func:`_print_text` so an empty list emits nothing.
    """
    if isinstance(value, dict):
        return "\t".join(scalar_text(v) for v in value.values())
    return scalar_text(value)


def _print_text(data: Any) -> None:
    """Print data in plain text format (like ``aws --output text``).

    Thin I/O wrapper over :func:`format_text`: a top-level list prints one
    line per item (an empty list prints nothing); any other value prints a
    single line.
    """
    if isinstance(data, list):
        for item in data:
            click.echo(format_text(item))
        return
    click.echo(format_text(data))


def format_csv(rows: list[dict[str, Any]], *, with_bom: bool = False) -> str:
    """CSV text for *rows* (RFC 4180, CRLF record terminators). Pure (no I/O).

    Empty *rows* \u2192 empty string. The caller writes the result through the
    binary stdout layer (:func:`_print_csv`) so the CRLF terminators are not
    doubled on Windows.
    """
    if not rows:
        return ""
    buf = io.StringIO()
    if with_bom:
        buf.write("\ufeff")
    # Asana responses often have optional fields that appear on some rows
    # and not others (e.g. ``due_on``). Collect the union of keys across
    # all rows so no row's data is silently dropped.
    # ``dict.fromkeys`` preserves insertion order (Python 3.7+), giving a
    # stable column order based on first appearance.
    fieldnames = list(dict.fromkeys(key for row in rows for key in row))
    # RFC 4180: CRLF between records; newlines inside a field stay verbatim.
    writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def _print_csv(rows: list[dict[str, Any]], *, with_bom: bool = False) -> None:
    """Write :func:`format_csv` output to stdout's binary layer.

    The binary layer avoids the text layer's newline translation (which on
    Windows would double each CRLF); falls back to text for streams without
    one (e.g. an in-memory ``StringIO`` in tests).
    """
    text = format_csv(rows, with_bom=with_bom)
    if not text:
        return
    out = sys.stdout
    raw = getattr(out, "buffer", None)
    if raw is None:
        out.write(text)
    else:
        out.flush()
        raw.write(text.encode(out.encoding or "utf-8"))
        raw.flush()
