"""Render a CLI invocation as standalone python-asana code.

The ``--generate-python`` mode (``runtime.generate_python``) collects the SDK
call into a session-free :class:`~asana_api_cli.cli.CallPlan` and hands it here
to be rendered as a self-contained script instead of being executed.
``formatter.py:formatted`` calls :func:`render_python` in place of
``_format_output`` whenever the mode is active.

What the emitted script reproduces:

* **Config / client** (C-8 / C-4): an ``asana.Configuration`` built from the
  same global flags the CLI applies — only options the user passed are written,
  using the shared ``_CONFIG_KNOBS`` table so the two cannot drift. The access
  token is ``os.environ[...]`` unless ``--access-token`` was given (then it is
  transcribed literally; the user is expected to pass a dummy).
* **Call** (C-9 body / C-10 iterator): ``asana.<Api>(api_client).<method>(...)``
  with the body inlined as a Python literal, the ``opts`` dict, and the
  per-call kwargs. Array endpoints are wrapped in ``list(...)`` — predicted
  statically, never by calling the SDK.
* **Output** (C-3 / C-17 / C-14): the CLI's pure formatter functions are inlined
  via ``inspect.getsource`` (single source of truth) and driven exactly as
  ``_format_output`` drives them, including the ``--query`` jq filter (the jq
  dependency is added only when ``--query`` is used).
* **Errors** (C-16): under ``--exception-output`` the call is wrapped in
  try/except that echoes the exception to stderr and renders the same envelope
  the CLI does, then exits 3. Under the default ``none`` there is no try block
  and exceptions propagate (Python traceback, exit 1).

Not yet reproduced — :func:`render_python` refuses it rather than emit
token-leaking code: ``--debug`` (C-7). Upload's ``--multibyte-filenames`` (C-11)
and ``--generate-python --version`` (C-15) are also later work.
"""

from __future__ import annotations

import inspect
import pprint
from typing import TYPE_CHECKING

import click

from asana_api_cli import formatter
from asana_api_cli.session import _CONFIG_KNOBS, ACCESS_TOKEN_ENV, runtime

if TYPE_CHECKING:
    # Type-only: importing ``cli`` at runtime would cycle (cli -> formatter ->
    # codegen). ``render_python`` reads the plan's attributes structurally, so it
    # never needs the class object — only the annotation, which
    # ``from __future__ import annotations`` keeps as a string.
    from asana_api_cli.cli import CallPlan

# Per ``--output`` / ``--exception-output`` format: the pure formatter functions
# it needs, the stdlib modules their bodies use, and whether ``tabulate`` is
# required. The function set mirrors the branches of ``formatter._format_output``;
# the equivalence tests exec the emitted script, so any drift (a converter
# growing a new import) surfaces as a runtime error there.
_FORMAT_FUNCS: dict[str, tuple[frozenset[str], frozenset[str], bool]] = {
    "json": (frozenset({"format_json"}), frozenset({"json"}), False),
    "text": (frozenset({"scalar_text", "format_text"}), frozenset({"json"}), False),
    "table": (frozenset({"scalar_text", "to_rows", "format_table"}), frozenset({"json"}), True),
    "csv": (
        frozenset({"scalar_text", "to_rows", "format_csv"}),
        frozenset({"json", "csv", "io"}),
        False,
    ),
    "none": (frozenset(), frozenset(), False),
}

# Inline order: callees before callers, so the emitted block is import-clean.
_CONVERTER_ORDER: tuple[str, ...] = (
    "scalar_text",
    "to_rows",
    "format_json",
    "format_text",
    "format_table",
    "format_csv",
)


class _Imports:
    """Accumulates the import lines the emitted script needs.

    ``asana`` is always imported; everything else is added on demand by the
    section renderers. ``block`` emits them isort-grouped (future, stdlib,
    third-party).
    """

    def __init__(self) -> None:
        self.stdlib: set[str] = set()
        self.tabulate: bool = False
        self.jq: bool = False
        self.api_exception: bool = False

    def block(self) -> list[str]:
        lines = ["from __future__ import annotations"]
        if self.stdlib:
            lines.append("")
            lines += [f"import {name}" for name in sorted(self.stdlib)]
        lines.append("")
        lines.append("import asana")
        if self.api_exception:
            lines.append("from asana.rest import ApiException")
        if self.jq:
            lines.append("import jq  # requires: pip install jq")
        if self.tabulate:
            lines.append("from tabulate import tabulate")
        return lines


def _indent(lines: list[str], by: int = 4) -> list[str]:
    pad = " " * by
    return [pad + line if line else line for line in lines]


def _reject_unsupported() -> None:
    """Refuse the flags this milestone cannot yet render faithfully.

    Generating ``--debug`` code without the ``HttpClientAuthRedactor`` would emit
    a script that logs the token unmasked, violating constitution #2. Refusing
    (exit 2) until that layer lands is the safe behavior.
    """
    if runtime.debug:
        raise click.UsageError(
            "--generate-python does not yet support --debug (coming in a later release)."
        )


def _render_converters(formats: set[str], needs: _Imports) -> list[str]:
    """Inline (via getsource) the pure formatter functions for *formats*.

    The union across the success and error output formats, emitted once in
    dependency order.
    """
    wanted: set[str] = set()
    for fmt in formats:
        names, stdlib, needs_tabulate = _FORMAT_FUNCS[fmt]
        wanted |= names
        needs.stdlib |= stdlib
        needs.tabulate = needs.tabulate or needs_tabulate
    if not wanted:
        return []
    ordered = [name for name in _CONVERTER_ORDER if name in wanted]
    sources = [inspect.getsource(getattr(formatter, name)).rstrip() for name in ordered]
    return "\n\n".join(sources).split("\n")


def _render_config(needs: _Imports) -> list[str]:
    """Emit the ``Configuration`` / ``ApiClient`` setup (C-8 / C-4).

    Mirrors ``AsanaSession.__init__``: the same ``_CONFIG_KNOBS`` table under the
    same apply condition (so only user-set options appear and there is no drift),
    the token, then the ``ApiClient``-instance settings.
    """
    lines = ["configuration = asana.Configuration()"]
    if runtime.access_token is not None:
        lines.append(f"configuration.access_token = {runtime.access_token!r}")
    else:
        needs.stdlib.add("os")
        lines.append(f"configuration.access_token = os.environ[{ACCESS_TOKEN_ENV!r}]")
    for attr, apply_when_truthy in _CONFIG_KNOBS:
        value = getattr(runtime, attr)
        applies = value if apply_when_truthy else value is not None
        if applies:
            lines.append(f"configuration.{attr} = {value!r}")
    if runtime.retry_strategy_overrides is not None:
        kwargs = ", ".join(f"{k}={v!r}" for k, v in runtime.retry_strategy_overrides.items())
        lines.append(f"configuration.retry_strategy = configuration.retry_strategy.new({kwargs})")
    lines.append("api_client = asana.ApiClient(configuration)")
    if runtime.default_headers:
        for name, value in runtime.default_headers.items():
            lines.append(f"api_client.set_default_header({name!r}, {value!r})")
    if runtime.user_agent is not None:
        lines.append(f"api_client.user_agent = {runtime.user_agent!r}")
    return lines


def _returns_iterator(plan: CallPlan) -> bool:
    """Predict ``list(...)`` materialization, matching ``execute_call_plan``'s
    live ``isinstance`` gate: an ``*Array`` endpoint yields an iterator only when
    the page iterator is on (default) and ``--full-payload`` was not requested."""
    if not plan.returns_iterator:
        return False
    if plan.method_kwargs.get("full_payload"):
        return False
    return runtime.return_page_iterator is not False


def _render_call_setup(plan: CallPlan) -> list[str]:
    """The API instance and the body / opts literals — everything before the call."""
    lines = [f"api_instance = asana.{plan.api_cls.__name__}(api_client)"]
    if plan.has_body:
        lines.append(f"body = {pprint.pformat(plan.body, sort_dicts=False)}")
    if plan.has_opts:
        lines.append(f"opts = {pprint.pformat(plan.opts, sort_dicts=False)}")
    return lines


def _call_expression(plan: CallPlan) -> str:
    """``api_instance.method(body, *path_args, opts, **kwargs)`` — order matches
    ``execute_call_plan``; array endpoints are wrapped in ``list(...)``."""
    positional: list[str] = []
    if plan.has_body:
        positional.append("body")
    positional += [repr(arg) for arg in plan.path_call_args]
    if plan.has_opts:
        positional.append("opts")
    keyword = [f"{name}={value!r}" for name, value in plan.method_kwargs.items()]
    call = f"api_instance.{plan.method_name}({', '.join(positional + keyword)})"
    return f"list({call})" if _returns_iterator(plan) else call


def _render_render(
    var: str, output_format: str, jq_query: str | None, csv_bom: bool, needs: _Imports
) -> list[str]:
    """Emit code that renders *var* to stdout via *output_format*.

    Mirrors ``formatter._format_output``: optionally jq-filter *var* into
    ``results`` (each yield rendered), else treat it as a single yield; ``none``
    suppresses output but still runs the jq pass so a bad expression exits 2.
    Used for both the success value and the error envelope.
    """
    lines: list[str] = []
    if jq_query is not None:
        needs.jq = True
        needs.stdlib.add("sys")
        lines += [
            "try:",
            f"    results = jq.all({jq_query!r}, {var})",
            "except ValueError as exc:",
            '    sys.stderr.write(f"Invalid jq expression: {exc}\\n")',
            "    sys.exit(2)",
        ]
    elif output_format == "none":
        return []
    else:
        lines.append(f"results = [{var}]")
    if output_format == "none":
        return lines  # jq ran for validation; nothing is printed
    needs.stdlib.add("sys")
    if output_format == "json":
        lines += ["for value in results:", "    print(format_json(value))"]
    elif output_format == "text":
        lines += [
            "for value in results:",
            "    if isinstance(value, list):",
            "        for item in value:",
            "            print(format_text(item))",
            "    else:",
            "        print(format_text(value))",
        ]
    else:  # table / csv: collect rows across yields, fall back to scalars
        lines += [
            "rows = []",
            "non_rowable = []",
            "for value in results:",
            "    converted = to_rows(value)",
            "    if converted is None:",
            "        non_rowable.append(value)",
            "    else:",
            "        rows.extend(converted)",
            "if not rows and non_rowable:",
            "    for value in non_rowable:",
            "        print(scalar_text(value))",
            "elif rows:",
            "    rows = [{k: scalar_text(v) for k, v in row.items()} for row in rows]",
        ]
        if output_format == "table":
            lines.append("    print(format_table(rows))")
        else:
            lines += [
                f"    text = format_csv(rows, with_bom={csv_bom!r})",
                "    if text:",
                "        sys.stdout.flush()",
                '        sys.stdout.buffer.write(text.encode(sys.stdout.encoding or "utf-8"))',
                "        sys.stdout.buffer.flush()",
            ]
    return lines


def _render_call_block(
    plan: CallPlan, exception_output: str, exception_query: str | None, needs: _Imports
) -> list[str]:
    """The call statement, wrapped in try/except + envelope when an
    ``--exception-output`` format is set (C-16). Under ``none`` the bare call
    propagates exceptions (Python traceback, exit 1)."""
    call = f"result = {_call_expression(plan)}"
    if exception_output == "none":
        return [call]
    needs.stdlib |= {"sys", "traceback"}
    needs.api_exception = True
    envelope = _render_render("envelope", exception_output, exception_query, False, needs)
    return [
        "try:",
        f"    {call}",
        "except Exception as exc:",
        '    sys.stderr.write("".join(traceback.format_exception_only(type(exc), exc)))',
        '    qualified = f"{type(exc).__module__}.{type(exc).__qualname__}"',
        "    if isinstance(exc, ApiException):",
        "        raw_body = exc.body",
        "        if isinstance(raw_body, (bytes, bytearray)):",
        '            body_text = bytes(raw_body).decode("utf-8", errors="replace")',
        "        elif isinstance(raw_body, str):",
        "            body_text = raw_body",
        "        else:",
        "            body_text = None",
        "        envelope = {",
        '            "exception": qualified,',
        '            "status": exc.status,',
        '            "reason": exc.reason,',
        '            "body": body_text,',
        '            "headers": dict(exc.headers) if exc.headers is not None else None,',
        "        }",
        "    else:",
        '        envelope = {"exception": qualified, "reason": str(exc)}',
        *_indent(envelope),
        "    sys.exit(3)",
    ]


def _render_reconfigure(needs: _Imports) -> list[str]:
    """Reproduce the CLI's startup UTF-8 reconfigure so non-ASCII prints on
    Windows too (constitution #5). Harmless for csv, which writes bytes."""
    needs.stdlib.add("sys")
    return [
        "for _stream in (sys.stdout, sys.stderr):",
        '    if hasattr(_stream, "reconfigure"):',
        '        _stream.reconfigure(encoding="utf-8")',
    ]


def render_python(
    plan: CallPlan,
    *,
    output_format: str,
    jq_query: str | None,
    csv_bom: bool,
    exception_output: str,
    exception_query: str | None,
) -> str:
    """Render *plan* as a standalone python-asana script.

    Reproduces the global configuration, the SDK call, the ``--output`` (and
    ``--query``) rendering, and the ``--exception-output`` error envelope.
    ``--debug`` is refused for now (see :func:`_reject_unsupported`).
    """
    _reject_unsupported()
    needs = _Imports()
    converters = _render_converters({output_format, exception_output}, needs)
    config = _render_config(needs)
    setup = _render_call_setup(plan)
    call_block = _render_call_block(plan, exception_output, exception_query, needs)
    success = _render_render("result", output_format, jq_query, csv_bom, needs)
    prints = output_format != "none" or exception_output != "none"
    reconfigure = _render_reconfigure(needs) if prints else []

    sections: list[list[str]] = [needs.block()]
    if converters:
        sections.append(converters)
    if reconfigure:
        sections.append(reconfigure)
    sections += [config, setup, call_block]
    if success:
        sections.append(success)
    return "\n\n".join("\n".join(section) for section in sections) + "\n"
