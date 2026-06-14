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
  token is ``os.environ[...]`` unless a non-empty ``--access-token`` was given
  (then its masked form is embedded — see the credential masking note below).
* **Credential masking** (constitution #2): values that carry a credential —
  the ``--access-token`` value, an ``Authorization`` / ``Proxy-Authorization``
  header given via ``--set-default-header`` / ``--header-params``, and the
  password in a ``--proxy`` URL — are masked everywhere they would appear in
  the emitted text (the config lines, the call's ``header_params``, and the
  ``# Equivalent to:`` comment). A value too short to be a real token is
  kept verbatim so the documented dummy-token workflow round-trips.
* **Call** (C-9 body / C-10 iterator): ``asana.<Api>(api_client).<method>(...)``
  with the body inlined as a Python literal, the ``opts`` dict, and the
  per-call kwargs. Array endpoints are wrapped in ``list(...)`` — predicted
  statically, never by calling the SDK.
* **Output** (C-3 / C-17 / C-14): without ``--query`` the value is rendered
  directly; with ``--query`` it is jq-filtered and each yield rendered (the jq
  dependency is added only then). ``json`` calls ``json.dumps`` directly;
  ``text`` / ``table`` / ``csv`` inline the CLI's pure converters via
  ``inspect.getsource`` (single source of truth) and drive them as
  ``_format_output`` does.
* **Errors** (C-16): under ``--exception-output`` the call is wrapped in
  try/except that echoes the exception to stderr and renders the same envelope
  the CLI does, then exits 3. Under the default ``none`` there is no try block
  and exceptions propagate (Python traceback, exit 1).
* **Debug / upload** (C-7 / C-11): ``--debug`` inlines ``redactor.py`` and wraps
  the call in ``with HttpClientAuthRedactor()`` (so the wire trace keeps the
  ``Authorization`` header masked — constitution #2); ``--multibyte-filenames``
  inlines ``multibyte_filename.py`` and wraps the call in
  ``with MultibyteFilenameSupport()``.
* **Header** (C-5): a provenance line plus the original command.

:func:`render_version` handles ``--generate-python --version`` (C-15) — a
command-free path that inlines ``version.py`` and prints the version.
"""

from __future__ import annotations

import ast
import inspect
import json
import math
import pprint
import re
import shlex
import sys
from collections.abc import Callable
from types import ModuleType
from typing import TYPE_CHECKING

import click

from asana_api_cli import formatter, multibyte_filename, redactor, version
from asana_api_cli.session import _CONFIG_KNOBS, ACCESS_TOKEN_ENV, runtime

if TYPE_CHECKING:
    # Type-only: importing ``cli`` at runtime would cycle (cli -> formatter ->
    # codegen). ``render_python`` reads the plan's attributes structurally, so it
    # never needs the class object — only the annotation, which
    # ``from __future__ import annotations`` keeps as a string.
    from asana_api_cli.cli import CallPlan

# Per ``--output`` / ``--exception-output`` format: the pure formatter functions
# it needs inlined, the stdlib modules their bodies use, and whether ``tabulate``
# is required. ``json`` inlines nothing — it calls ``json.dumps`` directly (see
# ``_render_single`` / ``_render_yields``) — but still needs ``import json``. The
# function set mirrors the branches of ``formatter._format_output``; the
# equivalence tests exec the emitted script, so any drift (a converter growing a
# new import) surfaces as a runtime error there.
_FORMAT_FUNCS: dict[str, tuple[frozenset[str], frozenset[str], bool]] = {
    "json": (frozenset(), frozenset({"json"}), False),
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
        self.typing_any: bool = False
        self.tabulate: bool = False
        self.jq: bool = False
        self.api_exception: bool = False

    def block(self) -> list[str]:
        lines = ["from __future__ import annotations"]
        # The stdlib group: ``import x`` lines plus ``from typing import Any``
        # (needed by the inlined converters' annotations), sorted by module name
        # the way isort/ruff would order them.
        stdlib_lines = [f"import {name}" for name in self.stdlib]
        if self.typing_any:
            stdlib_lines.append("from typing import Any")
        if stdlib_lines:
            lines.append("")
            lines += sorted(stdlib_lines, key=lambda line: line.split()[1])
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


def _inline_module(module: ModuleType) -> list[str]:
    """The source of *module* as lines, ready to embed in the generated script.

    The module docstring and its ``from __future__ import annotations`` are
    dropped (the docstring documents the standalone file, not the inlined copy;
    a future-import is only valid at the top of the generated script, which
    carries its own); surrounding blank lines are trimmed. The module's other
    imports stay inline — these modules (``redactor`` / ``multibyte_filename`` /
    ``version``) need nothing beyond what a python-asana install already provides,
    so they copy in as-is.
    """
    source = inspect.getsource(module)
    drop: set[int] = set()
    body = ast.parse(source).body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        doc = body[0]
        drop = set(range(doc.lineno, (doc.end_lineno or doc.lineno) + 1))
    lines = [
        line
        for number, line in enumerate(source.splitlines(), start=1)
        if number not in drop and line.strip() != "from __future__ import annotations"
    ]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _render_support(plan: CallPlan) -> list[str]:
    """Inline the standalone helper modules the call's ``with`` blocks need."""
    blocks: list[list[str]] = []
    if runtime.debug:
        blocks.append(
            ["# --- inlined from asana_api_cli/redactor.py ---", *_inline_module(redactor)]
        )
    if plan.multibyte:
        blocks.append(
            [
                "# --- inlined from asana_api_cli/multibyte_filename.py ---",
                *_inline_module(multibyte_filename),
            ]
        )
    out: list[str] = []
    for index, block in enumerate(blocks):
        if index:
            out.append("")
        out += block
    return out


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
    # Every converter annotates with ``Any`` (e.g. ``def format_json(value: Any)``),
    # so the inlined block needs ``from typing import Any``.
    needs.typing_any = True
    ordered = [name for name in _CONVERTER_ORDER if name in wanted]
    sources = [inspect.getsource(getattr(formatter, name)).rstrip() for name in ordered]
    return "\n\n".join(sources).split("\n")


# ---------- credential masking ----------------------------------------------
#
# Constitution #2: a credential must not survive into the emitted text. The
# policy constants are shared with the ``--debug`` trace mask in ``redactor.py``
# so the two presentations stay recognizably the same (``...<last 6>``); the
# short-value branch differs on purpose — the trace conservatively redacts a
# short token, while here a value too short to be a real Asana token is a
# dummy by construction and is kept verbatim so the documented dummy-token
# workflow round-trips.


def _mask_secret(value: str) -> str:
    """Mask a credential for embedding in generated output; a value too
    short to be a real token is kept verbatim."""
    if len(value) < redactor._MASK_MIN_LEN:
        return value
    return f"...{value[-redactor._MASK_SUFFIX_LEN :]}"


# Header names whose value is a credential by definition.
_AUTH_HEADER_NAMES = frozenset({"authorization", "proxy-authorization"})

_AUTH_SCHEME_RE = re.compile(r"^(Bearer|Basic)\s+(.+)$", re.IGNORECASE | re.DOTALL)


def _mask_authorization(value: str) -> str:
    """Mask an ``Authorization`` / ``Proxy-Authorization`` header value,
    keeping a ``Bearer`` / ``Basic`` scheme prefix visible like the
    ``--debug`` trace mask. A ``Basic`` credential is fully masked — the
    value is base64 of ``user:password``, so even a tail reveal would
    expose password characters."""
    m = _AUTH_SCHEME_RE.match(value)
    if m is None:
        return _mask_secret(value)
    if m.group(1).lower() == "basic":
        return f"{m.group(1)} {redactor._BASIC_MASK}"
    return f"{m.group(1)} {_mask_secret(m.group(2))}"


_PROXY_USERINFO_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://[^/@:]*):([^/@]*)@")


def _mask_proxy(value: str) -> str:
    """Mask the password component of a proxy URL's userinfo."""
    return _PROXY_USERINFO_RE.sub(r"\1:***@", value)


def _mask_header_params(value: dict[str, object]) -> dict[str, object]:
    """A copy of a ``header_params`` dict with credential-header values masked."""
    return {
        name: _mask_authorization(v)
        if name.lower() in _AUTH_HEADER_NAMES and isinstance(v, str)
        else v
        for name, v in value.items()
    }


def _mask_header_arg(value: str) -> str:
    """Mask the ``Authorization`` value inside a header flag's raw argument.

    The plain single-pair ``Name=Value`` form is masked precisely; any other
    shape that mentions ``authorization`` (a JSON object, a multi-pair list)
    is replaced wholesale — over-masking is safe for a provenance comment.
    An ``@path`` value carries no secret in argv and passes through.
    """
    if value.startswith("@") or "authorization" not in value.lower():
        return value
    name, sep, rest = value.partition("=")
    if sep and name.strip().lower() in _AUTH_HEADER_NAMES:
        return f"{name}={_mask_authorization(rest)}"
    return "<masked>"


# ``--flag VALUE`` flags whose value can carry a credential, with the mask to
# apply in the ``# Equivalent to:`` argv transcription. The ``--flag=VALUE``
# spelling is handled too (see ``_mask_argv``).
_SENSITIVE_FLAG_MASKS: dict[str, Callable[[str], str]] = {
    "--access-token": _mask_secret,
    "--proxy": _mask_proxy,
    "--set-default-header": _mask_header_arg,
    "--header-params": _mask_header_arg,
}


def _mask_argv(args: list[str]) -> list[str]:
    """*args* with every credential-bearing flag value masked
    (``_SENSITIVE_FLAG_MASKS``), in both the ``--flag VALUE`` and
    ``--flag=VALUE`` spellings."""
    masked: list[str] = []
    pending: Callable[[str], str] | None = None
    for arg in args:
        if pending is not None:
            masked.append(pending(arg))
            pending = None
            continue
        flag, sep, inline = arg.partition("=")
        mask = _SENSITIVE_FLAG_MASKS.get(flag)
        if mask is None:
            masked.append(arg)
        elif sep:
            masked.append(f"{flag}={mask(inline)}")
        else:
            masked.append(arg)
            pending = mask
    return masked


def _render_config(needs: _Imports) -> list[str]:
    """Emit the ``Configuration`` / ``ApiClient`` setup (C-8 / C-4).

    Mirrors ``AsanaSession``: the same ``_CONFIG_KNOBS`` table under the same
    apply condition (so only user-set options appear and there is no drift), then
    the ``ApiClient``-instance settings. The token follows ``from_env``'s
    resolution — a truthy ``--access-token`` embeds its masked form, otherwise
    the script reads ``$ASANA_ACCESS_TOKEN`` — so an explicit empty
    ``--access-token`` falls back to the env var, like the live CLI. A script
    carrying a masked token fails with 401 rather than silently using a
    different credential.
    """
    lines = ["configuration = asana.Configuration()"]
    if runtime.access_token:
        lines.append(f"configuration.access_token = {_mask_secret(runtime.access_token)!r}")
    else:
        needs.stdlib.add("os")
        lines.append(f"configuration.access_token = os.environ[{ACCESS_TOKEN_ENV!r}]")
    for attr, apply_when_truthy in _CONFIG_KNOBS:
        value = getattr(runtime, attr)
        applies = value if apply_when_truthy else value is not None
        if applies:
            if attr == "proxy":
                value = _mask_proxy(value)
            lines.append(f"configuration.{attr} = {value!r}")
    if runtime.debug:
        # Not a ``_CONFIG_KNOBS`` entry: ``AsanaSession`` sets this in ``open()``
        # alongside the redactor. The property setter flips the ``http.client``
        # debuglevel; the inlined ``with HttpClientAuthRedactor()`` masks the token.
        lines.append("configuration.debug = True")
    if runtime.retry_strategy_overrides is not None:
        kwargs = ", ".join(f"{k}={v!r}" for k, v in runtime.retry_strategy_overrides.items())
        lines.append(f"configuration.retry_strategy = configuration.retry_strategy.new({kwargs})")
    lines.append("api_client = asana.ApiClient(configuration)")
    if runtime.default_headers:
        for name, value in runtime.default_headers.items():
            if name.lower() in _AUTH_HEADER_NAMES:
                value = _mask_authorization(value)
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


def _has_non_finite_float(value: object) -> bool:
    """Whether *value* contains a NaN / Infinity float anywhere (which ``pprint``
    renders as the non-name tokens ``nan`` / ``inf``)."""
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_has_non_finite_float(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_non_finite_float(v) for v in value)
    return False


def _render_body(raw_body: str, needs: _Imports) -> list[str]:
    """Emit code that binds ``body`` from the unresolved ``--body`` string (C-9).

    Each input form is reproduced rather than flattened: ``-`` and ``@file`` read
    stdin / the file in the *generated* script (never here), so the script stays
    re-runnable against a different payload; a JSON literal is validated now
    (exit 2 on bad JSON, matching ``resolve_body``) and inlined as a Python
    literal. Mirrors ``cli.resolve_body``'s three branches — including UTF-8
    decoding: ``@file`` opens with ``encoding="utf-8"``, and the stdin branch
    reconfigures ``sys.stdin`` to UTF-8 first, matching the CLI's startup
    reconfigure (``cli.py:main``) so a piped UTF-8 body is not misdecoded with the
    locale code page on cp932 Windows (constitution #5).
    """
    if raw_body == "-":
        needs.stdlib |= {"sys", "json"}
        return [
            'if hasattr(sys.stdin, "reconfigure"):',
            '    sys.stdin.reconfigure(encoding="utf-8")',
            "body = json.load(sys.stdin)",
        ]
    if raw_body.startswith("@"):
        needs.stdlib.add("json")
        path = raw_body[1:]
        return [f'with open({path!r}, encoding="utf-8") as f:', "    body = json.load(f)"]
    try:
        value = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise click.BadParameter(f"invalid JSON: {exc}", param_hint="--body") from exc
    if _has_non_finite_float(value):
        # json.loads accepts NaN / Infinity / -Infinity (as does resolve_body), but
        # pprint would render them as the bare tokens ``nan`` / ``inf`` — not valid
        # Python names, so the inlined literal would NameError at run time. Reproduce
        # the CLI's parse instead so the body round-trips identically.
        needs.stdlib.add("json")
        return [f"body = json.loads({raw_body!r})"]
    return [f"body = {pprint.pformat(value, sort_dicts=False)}"]


def _render_call_setup(plan: CallPlan, needs: _Imports) -> list[str]:
    """The API instance and the body / opts literals — everything before the call."""
    lines = [f"api_instance = asana.{plan.api_cls.__name__}(api_client)"]
    if plan.has_body:
        assert plan.raw_body is not None  # has_body ⟺ a required --body was given
        lines += _render_body(plan.raw_body, needs)
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
    keyword: list[str] = []
    for name, value in plan.method_kwargs.items():
        if name == "header_params" and isinstance(value, dict):
            value = _mask_header_params(value)
        keyword.append(f"{name}={value!r}")
    call = f"api_instance.{plan.method_name}({', '.join(positional + keyword)})"
    return f"list({call})" if _returns_iterator(plan) else call


def _render_json_dumps(value_expr: str) -> str:
    """``json.dumps`` matching ``formatter.format_json`` (indent 2, non-ASCII
    kept). Inlined directly — the wrapper would add a function + ``Any`` import
    for a one-liner; the json equivalence test guards against drift."""
    return f"print(json.dumps({value_expr}, indent=2, ensure_ascii=False))"


def _render_rows_tail(output_format: str, csv_bom: bool, needs: _Imports) -> list[str]:
    """The shared ``elif rows:`` body for table / csv — stringify cells, then
    print. Emitted at one level of indentation (inside ``elif rows:``)."""
    lines = ["    rows = [{k: scalar_text(v) for k, v in row.items()} for row in rows]"]
    if output_format == "table":
        lines.append("    print(format_table(rows))")
    else:
        needs.stdlib.add("sys")
        lines += [
            f"    text = format_csv(rows, with_bom={csv_bom!r})",
            "    if text:",
            "        sys.stdout.flush()",
            '        sys.stdout.buffer.write(text.encode(sys.stdout.encoding or "utf-8"))',
            "        sys.stdout.buffer.flush()",
        ]
    return lines


def _render_single(var: str, output_format: str, csv_bom: bool, needs: _Imports) -> list[str]:
    """Render *var* as one value — the no-``--query`` path."""
    if output_format == "json":
        return [_render_json_dumps(var)]
    if output_format == "text":
        return [
            f"if isinstance({var}, list):",
            f"    for item in {var}:",
            "        print(format_text(item))",
            "else:",
            f"    print(format_text({var}))",
        ]
    # table / csv
    return [
        f"rows = to_rows({var})",
        "if rows is None:",
        f"    print(scalar_text({var}))",
        "elif rows:",
        *_render_rows_tail(output_format, csv_bom, needs),
    ]


def _render_yields(output_format: str, csv_bom: bool, needs: _Imports) -> list[str]:
    """Render every jq yield in ``results`` — the ``--query`` path."""
    if output_format == "json":
        return ["for value in results:", f"    {_render_json_dumps('value')}"]
    if output_format == "text":
        return [
            "for value in results:",
            "    if isinstance(value, list):",
            "        for item in value:",
            "            print(format_text(item))",
            "    else:",
            "        print(format_text(value))",
        ]
    # table / csv: collect rows across yields, fall back to scalars
    return [
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
        *_render_rows_tail(output_format, csv_bom, needs),
    ]


def _render_render(
    var: str, output_format: str, jq_query: str | None, csv_bom: bool, needs: _Imports
) -> list[str]:
    """Emit code that renders *var* to stdout via *output_format*.

    Without ``--query`` the value is rendered directly (:func:`_render_single`).
    With ``--query`` it is jq-filtered into ``results`` and each yield rendered
    (:func:`_render_yields`); ``none`` still runs the jq pass so a bad expression
    exits 2. Mirrors ``formatter._format_output``; used for both the success
    value and the error envelope.
    """
    # ``if jq_query`` (truthy), not ``is not None`` — matches
    # ``formatter._format_output``: an empty ``--query ''`` is treated as no
    # filter, not as the (invalid) jq program ``""``.
    if not jq_query:
        if output_format == "none":
            return []
        return _render_single(var, output_format, csv_bom, needs)
    needs.jq = True
    needs.stdlib.add("sys")
    lines = [
        "try:",
        f"    results = jq.all({jq_query!r}, {var})",
        "except ValueError as exc:",
        '    sys.stderr.write(f"Invalid jq expression: {exc}\\n")',
        "    sys.exit(2)",
    ]
    if output_format == "none":
        return lines  # jq ran for validation; nothing is printed
    return lines + _render_yields(output_format, csv_bom, needs)


def _render_call_block(
    plan: CallPlan, exception_output: str, exception_query: str | None, needs: _Imports
) -> list[str]:
    """The call statement, wrapped (innermost first) in the upload / debug
    ``with`` blocks (C-11 / C-7) and then, when an ``--exception-output`` format
    is set, in try/except + envelope (C-16). Under ``none`` the bare call
    propagates exceptions (Python traceback, exit 1)."""
    core = [f"result = {_call_expression(plan)}"]
    if plan.multibyte:
        core = ["with MultibyteFilenameSupport():", *_indent(core)]
    if runtime.debug:
        core = ["with HttpClientAuthRedactor():", *_indent(core)]
    if exception_output == "none":
        return core
    needs.stdlib |= {"sys", "traceback"}
    needs.api_exception = True
    envelope = _render_render("envelope", exception_output, exception_query, False, needs)
    return [
        "try:",
        *_indent(core),
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


def _header(equivalent_args: list[str]) -> list[str]:
    """The provenance / traceability header (C-5).

    ``version_string()`` records the SDK + click versions the surface was
    introspected against; the ``Equivalent to`` line is the original command
    (read from ``sys.argv``, minus ``--generate-python``). ``shlex.join`` quotes
    each argument but leaves embedded newlines literal, so a multiline argument
    (e.g. a multiline ``--query`` program) is wrapped across continuation lines
    each re-prefixed with ``#`` — otherwise the newline would end the comment and
    drop the rest into the script as source. Credential-bearing flag values are
    masked (``_mask_argv``), matching the masking the transcribed body applies
    (constitution #2).
    """
    # ``splitlines`` (not ``split("\n")``) so a bare ``\r`` — which Python's
    # tokenizer also treats as a line end, and which shlex.join leaves literal —
    # cannot end the comment either. ``joined`` is never empty (always "asana-api").
    first, *rest = shlex.join(["asana-api", *_mask_argv(equivalent_args)]).splitlines()
    return [
        f"# Generated by asana-api {version.version_string()}",
        f"# Equivalent to: {first}",
        *(f"#   {line}" for line in rest),
    ]


def render_version() -> str:
    """Render ``--generate-python --version`` (C-15): a self-contained script
    that prints ``version_string()``, with ``version.py`` inlined verbatim so the
    computation stays single-sourced."""
    return (
        "\n".join(
            [
                *_header(["--version"]),
                "",
                "from __future__ import annotations",
                "",
                *_inline_module(version),
                "",
                "print(version_string())",
            ]
        )
        + "\n"
    )


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

    Reproduces the header, the global configuration, the SDK call, the
    ``--output`` (and ``--query``) rendering, the ``--exception-output`` error
    envelope, and the ``--debug`` / ``--multibyte-filenames`` ``with`` blocks.
    """
    needs = _Imports()
    converters = _render_converters({output_format, exception_output}, needs)
    support = _render_support(plan)
    config = _render_config(needs)
    setup = _render_call_setup(plan, needs)
    call_block = _render_call_block(plan, exception_output, exception_query, needs)
    success = _render_render("result", output_format, jq_query, csv_bom, needs)
    # Reconfigure the streams to UTF-8 whenever the script writes to one. Besides
    # the rendered output, ``--query`` emits a ``sys.stderr.write`` of the jq error
    # on a bad expression even under ``--output none`` — so a truthy ``jq_query``
    # also needs the reconfigure (``exception_query`` does not: its envelope, and
    # thus its jq error, is only rendered when ``exception_output`` is not none).
    # ``--debug`` writes too: it sets ``configuration.debug = True``, which flips
    # ``http.client``'s wire-level tracing — printed to stdout via ``print`` — so a
    # non-ASCII trace would raise ``UnicodeEncodeError`` on cp932 Windows without
    # the reconfigure, just as the live CLI reconfigures unconditionally (#5).
    writes_a_stream = (
        output_format != "none" or exception_output != "none" or bool(jq_query) or runtime.debug
    )
    reconfigure = _render_reconfigure(needs) if writes_a_stream else []

    equivalent = [arg for arg in sys.argv[1:] if arg != "--generate-python"]
    sections: list[list[str]] = [_header(equivalent), needs.block()]
    if converters:
        sections.append(converters)
    if support:
        sections.append(support)
    if reconfigure:
        sections.append(reconfigure)
    sections += [config, setup, call_block]
    if success:
        sections.append(success)
    return "\n\n".join("\n".join(section) for section in sections) + "\n"
