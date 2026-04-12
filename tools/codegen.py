#!/usr/bin/env python3
"""Generate CLI modules by introspecting the Asana SDK.

Walks the `*Api` classes of the official `asana` SDK and emits click command
groups from each method's signature and docstring. No OAS file is consulted.

Usage:
    python tools/codegen.py [--outdir src/asana_api_cli]
"""
from __future__ import annotations

import argparse
import inspect
import re
from dataclasses import dataclass, field
from pathlib import Path

import asana


# ---------------------------------------------------------------------------
# Name conversion
# ---------------------------------------------------------------------------

def _snake(name: str) -> str:
    """PascalCase / 'AuditLogAPI' → snake_case ('audit_log_api')"""
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower()


def _api_class_to_group(cls_name: str) -> str:
    """TasksApi → 'tasks', AuditLogAPIApi → 'audit_log_api'"""
    assert cls_name.endswith("Api")
    return _snake(cls_name[:-3])


def _method_to_command(method_name: str) -> str:
    """get_tasks → 'get-tasks'"""
    return method_name.replace("_", "-")


# ---------------------------------------------------------------------------
# Docstring parsing
# ---------------------------------------------------------------------------

# e.g. ":param list[str] opt_fields: This endpoint..."
_PARAM_RE = re.compile(r"^\s*:param\s+(\S+)\s+(\w+)\s*:\s*(.*)$")


@dataclass
class DocParam:
    name: str
    py_type: str
    description: str
    required: bool


def _parse_summary(doc: str) -> str:
    """Extract the first line (summary) of a docstring."""
    for line in doc.split("\n"):
        line = line.strip()
        if not line:
            continue
        # Strip trailing noqa directive
        return re.sub(r"\s*#\s*" + "noqa" + r".*$", "", line).strip()
    return ""


def _parse_params(doc: str) -> dict[str, DocParam]:
    """Extract :param <type> <name>: <desc> entries from a docstring."""
    params: dict[str, DocParam] = {}
    current: DocParam | None = None

    for raw in doc.split("\n"):
        m = _PARAM_RE.match(raw)
        if m:
            if current is not None:
                params[current.name] = current
            py_type = m.group(1)
            name = m.group(2)
            desc = m.group(3).strip()
            current = DocParam(
                name=name,
                py_type=py_type,
                description=desc,
                required=False,  # determined below
            )
            continue

        # Continuation line (indented description) — append to the current param
        if current is not None and raw.strip() and not raw.strip().startswith(":"):
            current.description = (current.description + " " + raw.strip()).strip()

    if current is not None:
        params[current.name] = current

    # Determine required: look for "(required)" in description
    for p in params.values():
        if "(required)" in p.description:
            p.required = True
            p.description = p.description.replace("(required)", "").strip()

    # Ignore SDK-internal flags
    params.pop("async_req", None)
    return params


# ---------------------------------------------------------------------------
# SDK -> intermediate representation
# ---------------------------------------------------------------------------

# Docstring Python type -> click type mapping
_CLICK_TYPE_MAP: dict[str, str] = {
    "int": "int",
    "float": "float",
    "bool": "bool",
}


def _click_type(py_type: str) -> str | None:
    """Return the click `type=...` value. None for strings (click default)."""
    # list[str], list[int], etc. are accepted as strings
    if py_type.startswith("list"):
        return None
    return _CLICK_TYPE_MAP.get(py_type)


def _py_annotation(py_type: str, optional: bool) -> str:
    """Return a Python type annotation for the CLI function signature."""
    base = {
        "int": "int",
        "float": "float",
        "bool": "bool",
    }.get(py_type, "str")
    return f"{base} | None" if optional else base


@dataclass
class Operation:
    method_name: str  # get_tasks
    command_name: str  # get-tasks
    summary: str
    positional: list[str]  # body, task_gid, ... (from signature)
    params: dict[str, DocParam]  # from docstring
    has_opts: bool

    @property
    def has_body(self) -> bool:
        return "body" in self.positional

    @property
    def path_positionals(self) -> list[str]:
        return [p for p in self.positional if p != "body"]

    @property
    def opts_params(self) -> list[DocParam]:
        """Query params that go into the opts dict (from docstring, excluding positionals)."""
        return [
            p for p in self.params.values()
            if p.name not in self.positional and p.name != "body"
        ]

    @property
    def paginatable(self) -> bool:
        return any(p.name == "limit" for p in self.opts_params)


@dataclass
class ApiGroup:
    class_name: str  # TasksApi
    group_name: str  # tasks (snake_case)
    operations: list[Operation] = field(default_factory=list)


def _enumerate_api_classes() -> list[type]:
    return sorted(
        (
            cls for name, cls in vars(asana).items()
            if inspect.isclass(cls) and name.endswith("Api")
        ),
        key=lambda c: c.__name__,
    )


def _extract_operation(method_name: str, fn: object) -> Operation | None:
    if method_name.startswith("_") or method_name.endswith("_with_http_info"):
        return None
    if not callable(fn):
        return None
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None

    params_iter = list(sig.parameters.values())
    if not params_iter or params_iter[0].name != "self":
        return None

    positional: list[str] = []
    has_opts = False
    for p in params_iter[1:]:
        if p.kind == inspect.Parameter.VAR_KEYWORD:
            continue
        if p.name == "opts":
            has_opts = True
            continue
        positional.append(p.name)

    doc = fn.__doc__ or ""
    summary = _parse_summary(doc)
    params = _parse_params(doc)

    return Operation(
        method_name=method_name,
        command_name=_method_to_command(method_name),
        summary=summary,
        positional=positional,
        params=params,
        has_opts=has_opts,
    )


def introspect_sdk() -> list[ApiGroup]:
    groups: list[ApiGroup] = []
    for cls in _enumerate_api_classes():
        group = ApiGroup(
            class_name=cls.__name__,
            group_name=_api_class_to_group(cls.__name__),
        )
        for method_name in sorted(vars(cls)):
            fn = vars(cls)[method_name]
            op = _extract_operation(method_name, fn)
            if op is not None:
                group.operations.append(op)
        if group.operations:
            groups.append(group)
    return groups


# ---------------------------------------------------------------------------
# Code generation
# ---------------------------------------------------------------------------

_HEADER = (
    "# This file is auto-generated by tools/codegen.py — do not edit manually.\n"
    "from __future__ import annotations\n\n"
    "from typing import Any\n\n"
    "import click\n"
    "from asana import {class_name}\n\n"
    "from asana_api_cli.formatter import formatted\n"
    "from asana_api_cli.session import AsanaSession, resolve_body, resolve_workspace\n\n\n"
    '@click.group("{group_name}")\n'
    "def {group_var}() -> None:\n"
    '    """{group_doc} commands."""\n'
)

# Workspace-related parameter names (both positional and option forms)
_WORKSPACE_PARAMS = {"workspace_gid", "workspace"}


def _option_name(param_name: str) -> str:
    """Convert a SDK parameter name to a CLI option name.

    Strips the ``_gid`` suffix so that e.g. ``task_gid`` becomes ``task``
    and ``workspace_gid`` becomes ``workspace``.
    """
    if param_name.endswith("_gid"):
        return param_name[:-4]
    return param_name


def _is_workspace_param(name: str) -> bool:
    """Return True if the parameter represents a workspace identifier."""
    return name in _WORKSPACE_PARAMS


def _escape_help(text: str) -> str:
    """Escape a string for click help text: strip HTML tags, normalize whitespace, truncate."""
    t = re.sub(r"<[^>]+>", "", text)  # strip <b>, <code>, etc.
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > 200:
        t = t[:197] + "..."
    return t.replace("\\", "\\\\").replace('"', '\\"')


def _build_command(op: Operation, class_name: str, group_var: str) -> str:
    path_positionals = op.path_positionals
    has_body = op.has_body
    opts_params = sorted(op.opts_params, key=lambda p: (not p.required, p.name))
    paginatable = op.paginatable

    # Detect workspace in opts_params
    ws_opt = next((p for p in opts_params if _is_workspace_param(p.name)), None)
    # Detect workspace in path positionals
    ws_positional = next((n for n in path_positionals if _is_workspace_param(n)), None)
    has_workspace = ws_opt is not None or ws_positional is not None
    # --no-workspace is only for optional workspace opts (not path params, not required opts)
    needs_no_workspace = ws_opt is not None and not ws_opt.required

    # Non-workspace positionals and opts
    non_ws_positionals = [n for n in path_positionals if not _is_workspace_param(n)]
    non_ws_opts = [p for p in opts_params if not _is_workspace_param(p.name)]

    lines: list[str] = []
    lines.append(f'@{group_var}.command("{op.command_name}")')

    # path positionals → --option (with _gid stripped)
    for name in non_ws_positionals:
        opt_name = _option_name(name)
        flag = "--" + opt_name.replace("_", "-")
        dp = op.params.get(name)
        help_text = _escape_help(dp.description) if dp else ""
        help_part = f', help="{help_text}"' if help_text else ""
        lines.append(f'@click.option("{flag}", required=True{help_part})')

    # --workspace (unified from both positional workspace_gid and option workspace)
    if has_workspace:
        lines.append(
            '@click.option("--workspace", default=None, '
            'help="Workspace GID (falls back to ASANA_DEFAULT_WORKSPACE)")'
        )

    # body → required --body option
    if has_body:
        default_body_desc = "Request body (JSON string, @file, or - for stdin)"
        body_param = op.params.get(
            "body", DocParam("body", "dict", default_body_desc, True)
        )
        body_help = _escape_help(body_param.description or default_body_desc)
        lines.append(f'@click.option("--body", required=True, help="{body_help}")')

    # remaining opts params → options (excluding workspace)
    for p in non_ws_opts:
        flag = "--" + p.name.replace("_", "-")
        click_type = _click_type(p.py_type)
        help_text = _escape_help(p.description)
        parts = [f'"{flag}"']
        if click_type:
            parts.append(f"type={click_type}")
        if p.required:
            parts.append("required=True")
        else:
            parts.append("default=None")
        parts.append(f'help="{help_text}"')
        lines.append(f"@click.option({', '.join(parts)})")

    # --no-workspace (only for optional workspace opts)
    if needs_no_workspace:
        lines.append(
            '@click.option("--no-workspace", is_flag=True, default=False, '
            'help="Do not send workspace parameter even if a default is configured")'
        )

    # --paginate
    if paginatable:
        lines.append(
            '@click.option("--paginate", is_flag=True, default=False, '
            'help="Fetch all pages")'
        )

    lines.append("@formatted")

    # function signature
    fn_args: list[str] = []
    for name in non_ws_positionals:
        opt_name = _option_name(name)
        fn_args.append(f"{opt_name}: str")
    if has_workspace:
        fn_args.append("workspace: str | None")
    if has_body:
        fn_args.append("body: str")
    for p in non_ws_opts:
        annotation = _py_annotation(p.py_type, optional=not p.required)
        fn_args.append(f"{p.name}: {annotation}")
    if needs_no_workspace:
        fn_args.append("no_workspace: bool")
    if paginatable:
        fn_args.append("paginate: bool")

    lines.append(f"def {op.method_name}({', '.join(fn_args)}) -> Any:")
    summary = op.summary or f"Call {class_name}.{op.method_name}"
    lines.append(f'    """{_escape_help(summary)}"""')

    # body parse
    if has_body:
        lines.append("    parsed_body = resolve_body(body)")

    # resolve workspace
    if has_workspace:
        ws_required = ws_positional is not None or (ws_opt is not None and ws_opt.required)
        no_ws_arg = "no_workspace=no_workspace, " if needs_no_workspace else ""
        lines.append(
            f"    resolved_workspace = resolve_workspace("
            f"workspace, {no_ws_arg}required={ws_required})"
        )

    # session
    if paginatable:
        lines.append("    session = AsanaSession.from_env(paginate=paginate)")
    else:
        lines.append("    session = AsanaSession.from_env()")
    lines.append(f"    api = {class_name}(session.client)")

    # opts dict
    if opts_params:
        lines.append("    opts: dict[str, Any] = {}")
        for p in non_ws_opts:
            if p.required:
                lines.append(f'    opts["{p.name}"] = {p.name}')
            else:
                lines.append(f"    if {p.name} is not None:")
                lines.append(f'        opts["{p.name}"] = {p.name}')
        # workspace opt → use resolved value
        if ws_opt is not None:
            lines.append("    if resolved_workspace is not None:")
            lines.append(f'        opts["{ws_opt.name}"] = resolved_workspace')
    else:
        lines.append("    opts: dict[str, Any] = {}")

    # call
    call_args: list[str] = []
    if has_body:
        call_args.append("parsed_body")
    for name in path_positionals:
        if _is_workspace_param(name):
            call_args.append("resolved_workspace")
        else:
            call_args.append(_option_name(name))
    if op.has_opts:
        call_args.append("opts")
    lines.append(f"    return api.{op.method_name}({', '.join(call_args)})")

    return "\n".join(lines) + "\n"


def generate_group_module(group: ApiGroup) -> str:
    group_var = f"{group.group_name}_group"
    header = _HEADER.format(
        class_name=group.class_name,
        group_name=group.group_name.replace("_", "-"),
        group_var=group_var,
        group_doc=group.class_name[:-3],
    )
    commands = "\n\n".join(
        _build_command(op, group.class_name, group_var) for op in group.operations
    )
    return header + "\n\n" + commands


def generate_cli_init(groups: list[ApiGroup]) -> str:
    lines = [
        "# This file is auto-generated by tools/codegen.py — do not edit manually.",
        "from __future__ import annotations",
        "",
        "import click",
        "",
        "from asana_api_cli.version import version_string",
        "",
    ]
    lines.append("from asana_api_cli.session import runtime")
    for g in groups:
        var = f"{g.group_name}_group"
        lines.append(f"from asana_api_cli.cli.{g.group_name} import {var}")
    lines.append("")
    lines.append("")
    lines.append("@click.group()")
    lines.append("@click.version_option(version_string(), prog_name=\"asana-api\")")
    lines.append(
        '@click.option("--host", default=None, '
        'help="Override API base URL (default: https://app.asana.com/api/1.0)")'
    )
    lines.append(
        '@click.option("--proxy", default=None, '
        'help="HTTP/HTTPS proxy URL")'
    )
    lines.append(
        '@click.option("--no-verify-ssl", is_flag=True, default=False, '
        'help="Disable TLS certificate verification (insecure)")'
    )
    lines.append(
        '@click.option("--ca-cert", "ca_cert", default=None, '
        'type=click.Path(exists=True, dir_okay=False), '
        'help="Path to a PEM bundle of trusted CA certificates")'
    )
    lines.append(
        '@click.option("--page-limit", "page_limit", type=int, default=None, '
        'help="Default per-page size for paginated endpoints")'
    )
    lines.append(
        '@click.option("--retries", type=int, default=None, '
        'help="Number of retries on 429/5xx responses (default: 5)")'
    )
    lines.append(
        '@click.option("--timeout", type=float, default=None, '
        'help="Per-request timeout in seconds")'
    )
    lines.append(
        '@click.option("--token-env", "token_env", default=None, '
        'help="Environment variable name holding the Asana access token '
        '(default: ASANA_ACCESS_TOKEN)")'
    )
    lines.append(
        '@click.option("--temp-dir", "temp_dir", default=None, '
        'type=click.Path(file_okay=False), '
        'help="Directory for temporary downloads")'
    )
    lines.append(
        '@click.option("--debug", is_flag=True, default=False, '
        'help="Print HTTP request/response to stderr for troubleshooting")'
    )
    lines.append("def main(")
    lines.append("    host: str | None,")
    lines.append("    proxy: str | None,")
    lines.append("    no_verify_ssl: bool,")
    lines.append("    ca_cert: str | None,")
    lines.append("    page_limit: int | None,")
    lines.append("    retries: int | None,")
    lines.append("    timeout: float | None,")
    lines.append("    token_env: str | None,")
    lines.append("    temp_dir: str | None,")
    lines.append("    debug: bool,")
    lines.append(") -> None:")
    lines.append('    """Asana API CLI (SDK-backed wrapper)."""')
    lines.append("    runtime.host = host")
    lines.append("    runtime.proxy = proxy")
    lines.append("    runtime.verify_ssl = not no_verify_ssl")
    lines.append("    runtime.ssl_ca_cert = ca_cert")
    lines.append("    runtime.page_limit = page_limit")
    lines.append("    runtime.retries = retries")
    lines.append("    runtime.timeout = timeout")
    lines.append("    if token_env:")
    lines.append("        runtime.token_env = token_env")
    lines.append("    runtime.temp_dir = temp_dir")
    lines.append("    runtime.debug = debug")
    lines.append("")
    lines.append("")
    for g in groups:
        lines.append(f"main.add_command({g.group_name}_group)")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate CLI modules from asana SDK introspection"
    )
    parser.add_argument("--outdir", default="src/asana_api_cli", help="Output directory")
    args = parser.parse_args()

    outdir = Path(args.outdir)
    cli_dir = outdir / "cli"
    cli_dir.mkdir(parents=True, exist_ok=True)

    groups = introspect_sdk()

    for g in groups:
        module = generate_group_module(g)
        out = cli_dir / f"{g.group_name}.py"
        out.write_text(module)
        print(f"  generated: {out} ({len(g.operations)} commands)")

    init = generate_cli_init(groups)
    init_path = cli_dir / "__init__.py"
    init_path.write_text(init)
    print(f"  generated: {init_path}")

    total = sum(len(g.operations) for g in groups)
    print(f"\nDone: {len(groups)} groups, {total} commands.")


if __name__ == "__main__":
    main()
