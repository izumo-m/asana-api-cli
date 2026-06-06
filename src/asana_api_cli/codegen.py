"""Render a CLI invocation as standalone python-asana code.

The ``--generate-python`` mode (``runtime.generate_python``) collects the SDK
call into a session-free :class:`~asana_api_cli.cli.CallPlan` and hands it here
to be rendered as a self-contained script instead of being executed.
``formatter.py:formatted`` calls :func:`render_python` in place of
``_format_output`` whenever the mode is active.

What the emitted script reproduces (this milestone):

* **Config / client** (C-8 / C-4): an ``asana.Configuration`` built from the
  same global flags the CLI applies — only options the user passed are written,
  using the shared ``_CONFIG_KNOBS`` table so the two cannot drift. The access
  token is ``os.environ[...]`` unless ``--access-token`` was given (then it is
  transcribed literally; the user is expected to pass a dummy).
* **Call** (C-9 body / C-10 iterator): ``asana.<Api>(api_client).<method>(...)``
  with the body inlined as a Python literal, the ``opts`` dict, and the
  per-call kwargs. Endpoints that return a lazy iterator are wrapped in
  ``list(...)`` — predicted statically, never by calling the SDK.
* **Output** (C-3 / C-17): the CLI's pure formatter functions are inlined via
  ``inspect.getsource`` (single source of truth) and driven exactly as
  ``_format_output`` drives them.

Not yet reproduced — :func:`render_python` refuses these with a clear error
rather than emit wrong (or, for ``--debug``, token-leaking) code:
``--query`` (C-14), ``--exception-output`` (C-16), ``--debug`` (C-7). Upload's
``--multibyte-filenames`` (C-11) and ``--generate-python --version`` (C-15) are
also later work.
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

# Per ``--output`` format: the pure formatter functions to inline (in
# definition order so callees precede callers), the stdlib modules their bodies
# need, and whether ``tabulate`` is required. The function set mirrors the
# branches of ``formatter._format_output``; the equivalence tests exec the
# emitted script, so any drift (a converter growing a new import) surfaces as a
# runtime error there.
_OUTPUT_DEPS: dict[str, tuple[tuple[str, ...], frozenset[str], bool]] = {
    "json": (("format_json",), frozenset({"json"}), False),
    "text": (("scalar_text", "format_text"), frozenset({"json"}), False),
    "table": (("scalar_text", "to_rows", "format_table"), frozenset({"json"}), True),
    "csv": (("scalar_text", "to_rows", "format_csv"), frozenset({"json", "csv", "io"}), False),
    "none": ((), frozenset(), False),
}


class _Imports:
    """Accumulates the import lines the emitted script needs.

    ``asana`` is always imported; stdlib modules and ``tabulate`` are added on
    demand by the section renderers. ``block`` emits them isort-grouped (future,
    stdlib, third-party).
    """

    def __init__(self) -> None:
        self.stdlib: set[str] = set()
        self.tabulate: bool = False

    def block(self) -> list[str]:
        lines = ["from __future__ import annotations"]
        if self.stdlib:
            lines.append("")
            lines += [f"import {name}" for name in sorted(self.stdlib)]
        lines.append("")
        lines.append("import asana")
        if self.tabulate:
            lines.append("from tabulate import tabulate")
        return lines


def _reject_unsupported(*, jq_query: str | None, exception_output: str) -> None:
    """Refuse the flags this milestone cannot yet render faithfully.

    Generating code that silently dropped one of these would be wrong — and for
    ``--debug`` it would emit a script that logs the token unmasked, violating
    constitution #2. Refusing (exit 2) until the matching layer lands is the
    safe behavior.
    """
    unsupported: list[str] = []
    if runtime.debug:
        unsupported.append("--debug")
    if jq_query is not None:
        unsupported.append("--query")
    if exception_output != "none":
        unsupported.append("--exception-output")
    if unsupported:
        raise click.UsageError(
            f"--generate-python does not yet support {', '.join(unsupported)} "
            "(coming in a later release)."
        )


def _render_converters(output_format: str, needs: _Imports) -> list[str]:
    """Inline the pure formatter functions for *output_format* via getsource."""
    names, stdlib, needs_tabulate = _OUTPUT_DEPS[output_format]
    if not names:
        return []
    needs.stdlib |= stdlib
    needs.tabulate = needs.tabulate or needs_tabulate
    sources = [inspect.getsource(getattr(formatter, name)).rstrip() for name in names]
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


def _render_call(plan: CallPlan) -> list[str]:
    """Emit the API instantiation and the method call (C-9 body / C-10 iterator).

    Argument order matches ``execute_call_plan``: ``method(body, *path_args,
    opts, **method_kwargs)``. The body and opts are inlined as Python literals;
    path positionals (gids) and the per-call kwargs are inlined in place.
    """
    lines = [f"api_instance = asana.{plan.api_cls.__name__}(api_client)"]
    positional: list[str] = []
    if plan.has_body:
        lines.append(f"body = {pprint.pformat(plan.body, sort_dicts=False)}")
        positional.append("body")
    positional += [repr(arg) for arg in plan.path_call_args]
    if plan.has_opts:
        lines.append(f"opts = {pprint.pformat(plan.opts, sort_dicts=False)}")
        positional.append("opts")
    keyword = [f"{name}={value!r}" for name, value in plan.method_kwargs.items()]
    call = f"api_instance.{plan.method_name}({', '.join(positional + keyword)})"
    lines.append(f"result = list({call})" if _returns_iterator(plan) else f"result = {call}")
    return lines


def _render_output(output_format: str, csv_bom: bool, needs: _Imports) -> list[str]:
    """Drive the inlined converters over ``result`` (C-3 / C-17).

    Reproduces ``_format_output`` for the no-query case (a single value): json /
    text print straight; table / csv reuse ``to_rows`` + ``scalar_text`` and fall
    back to ``scalar_text`` for non-rowable data. csv writes bytes through the
    binary layer so the RFC 4180 CRLFs are not doubled on Windows.
    """
    if output_format == "none":
        return []
    needs.stdlib.add("sys")
    # Reproduce the CLI's startup UTF-8 reconfigure so non-ASCII prints on
    # Windows too (constitution #5). Harmless for csv, which writes bytes.
    lines = [
        'if hasattr(sys.stdout, "reconfigure"):',
        '    sys.stdout.reconfigure(encoding="utf-8")',
    ]
    if output_format == "json":
        lines.append("print(format_json(result))")
    elif output_format == "text":
        lines += [
            "if isinstance(result, list):",
            "    for item in result:",
            "        print(format_text(item))",
            "else:",
            "    print(format_text(result))",
        ]
    else:  # table / csv share the row-collection and non-rowable fallback
        lines += [
            "rows = to_rows(result)",
            "if rows is None:",
            "    print(scalar_text(result))",
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

    Reproduces the global configuration, the SDK call, and the ``--output``
    rendering. ``--query`` / ``--exception-output`` / ``--debug`` are refused for
    now (see :func:`_reject_unsupported`); ``exception_query`` rides along with
    ``--exception-output`` and so is unused until that layer lands.
    """
    _reject_unsupported(jq_query=jq_query, exception_output=exception_output)
    needs = _Imports()
    converters = _render_converters(output_format, needs)
    config = _render_config(needs)
    call = _render_call(plan)
    output = _render_output(output_format, csv_bom, needs)

    sections = [needs.block(), *([converters] if converters else []), config, call]
    if output:
        sections.append(output)
    return "\n\n".join("\n".join(section) for section in sections) + "\n"
