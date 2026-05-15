"""Command-line interface for the Asana API.

The CLI is built at import time by introspecting every ``*Api`` class exposed
by the official ``asana`` SDK. Each ``*Api`` class becomes a click subgroup;
its methods become commands underneath. Method-level introspection is
deferred per group so top-level ``--help`` does not pay the cost of walking
every method.

Translation rules:

* ``TasksApi`` → group ``tasks``; ``AuditLogAPIApi`` → ``audit-log-api``.
* ``get_tasks`` → command ``get-tasks``.
* Method parameters and ``:param`` lines from the docstring become click
  options. Path GIDs are exposed with the ``_gid`` suffix stripped
  (``task_gid`` → ``--task``).
* Positional ``workspace_gid`` and the ``workspace`` opt are unified into a
  single ``--workspace`` option that falls back to
  ``$ASANA_DEFAULT_WORKSPACE`` only when the parameter is required.
* Methods that accept a ``body`` positional get a required ``--body`` option
  routed through ``resolve_body`` (supports ``@file`` / ``-`` / JSON string).
* Paginatable methods (those with a ``limit`` doc-param) hide the raw
  ``--limit`` option in favor of ``--page-size``, and gain ``--all-items``,
  ``--max-items``, plus the deprecated ``--paginate`` alias.

Because the CLI surface tracks whatever ``asana`` package version is
installed in the active environment, ``pip install -U asana`` is enough to
pick up newly added SDK methods without releasing a new asana-api-cli.
"""

from __future__ import annotations

import inspect
import re
import sys
from typing import Any

import asana
import click

from asana_api_cli.click_ext import (
    CommandWithGlobalOptions,
    GroupWithGlobalOptions,
    LazyGroup,
)
from asana_api_cli.formatter import formatted
from asana_api_cli.session import (
    AsanaSession,
    resolve_body,
    resolve_workspace,
    runtime,
)
from asana_api_cli.version import version_string


# ---------------------------------------------------------------------------
# Name conversion
# ---------------------------------------------------------------------------

_PARAM_RE = re.compile(r"^\s*:param\s+(\S+)\s+(\w+)\s*:\s*(.*)$")
_WORKSPACE_PARAMS: frozenset[str] = frozenset({"workspace_gid", "workspace"})


def _snake(name: str) -> str:
    """PascalCase / 'AuditLogAPI' → snake_case ('audit_log_api')."""
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower()


def _api_class_to_group(cls_name: str) -> str:
    """TasksApi → 'tasks', AuditLogAPIApi → 'audit_log_api'."""
    assert cls_name.endswith("Api")
    return _snake(cls_name[:-3])


def _method_to_command(method_name: str) -> str:
    """get_tasks → 'get-tasks'."""
    return method_name.replace("_", "-")


def _option_name(param_name: str) -> str:
    """Strip ``_gid`` suffix; ``task_gid`` → ``task``."""
    if param_name.endswith("_gid"):
        return param_name[:-4]
    return param_name


def _is_workspace_param(name: str) -> bool:
    return name in _WORKSPACE_PARAMS


def _escape_help(text: str) -> str:
    """Strip HTML tags, normalize whitespace, truncate to 200 chars."""
    t = re.sub(r"<[^>]+>", "", text)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > 200:
        t = t[:197] + "..."
    return t


# ---------------------------------------------------------------------------
# Docstring parsing
# ---------------------------------------------------------------------------


class _DocParam:
    __slots__ = ("description", "name", "py_type", "required")

    def __init__(self, name: str, py_type: str, description: str, required: bool) -> None:
        self.name = name
        self.py_type = py_type
        self.description = description
        self.required = required


def _parse_summary(doc: str) -> str:
    """Return the first non-empty line of *doc* with any trailing noqa stripped."""
    for line in doc.split("\n"):
        line = line.strip()
        if not line:
            continue
        return re.sub(r"\s*#\s*noqa.*$", "", line).strip()
    return ""


def _parse_params(doc: str) -> dict[str, _DocParam]:
    """Extract ``:param TYPE NAME: DESC`` entries from *doc*."""
    params: dict[str, _DocParam] = {}
    current: _DocParam | None = None

    for raw in doc.split("\n"):
        stripped = raw.strip()
        m = _PARAM_RE.match(raw)
        if m:
            if current is not None:
                params[current.name] = current
            current = _DocParam(
                name=m.group(2),
                py_type=m.group(1),
                description=m.group(3).strip(),
                required=False,
            )
            continue
        # Any other ``:directive:`` line ends the current param so continuation
        # text after ``:return:`` etc. is not appended to its description.
        if stripped.startswith(":"):
            if current is not None:
                params[current.name] = current
                current = None
            continue
        if current is not None and stripped:
            current.description = (current.description + " " + stripped).strip()

    if current is not None:
        params[current.name] = current

    for p in params.values():
        if "(required)" in p.description:
            p.required = True
            p.description = p.description.replace("(required)", "").strip()

    params.pop("async_req", None)
    return params


# ---------------------------------------------------------------------------
# Operation introspection
# ---------------------------------------------------------------------------


_CLICK_TYPE_MAP: dict[str, type] = {
    "int": int,
    "float": float,
    "bool": bool,
}


def _click_type(py_type: str) -> type | None:
    """Map a docstring Python type to a click ``type=...`` value (None for str)."""
    if py_type.startswith("list"):
        return None
    return _CLICK_TYPE_MAP.get(py_type)


class _Operation:
    __slots__ = ("command_name", "has_opts", "method_name", "params", "positional", "summary")

    def __init__(
        self,
        method_name: str,
        command_name: str,
        summary: str,
        positional: list[str],
        params: dict[str, _DocParam],
        has_opts: bool,
    ) -> None:
        self.method_name = method_name
        self.command_name = command_name
        self.summary = summary
        self.positional = positional
        self.params = params
        self.has_opts = has_opts

    @property
    def has_body(self) -> bool:
        return "body" in self.positional

    @property
    def path_positionals(self) -> list[str]:
        return [p for p in self.positional if p != "body"]

    @property
    def opts_params(self) -> list[_DocParam]:
        return [
            p for p in self.params.values() if p.name not in self.positional and p.name != "body"
        ]

    @property
    def paginatable(self) -> bool:
        return any(p.name == "limit" for p in self.opts_params)


def _extract_operation(method_name: str, fn: object) -> _Operation | None:
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
    return _Operation(
        method_name=method_name,
        command_name=_method_to_command(method_name),
        summary=_parse_summary(doc),
        positional=positional,
        params=_parse_params(doc),
        has_opts=has_opts,
    )


def _enumerate_api_classes() -> list[type]:
    return sorted(
        (cls for name, cls in vars(asana).items() if inspect.isclass(cls) and name.endswith("Api")),
        key=lambda c: c.__name__,
    )


def _operations_for(api_cls: type) -> list[_Operation]:
    ops: list[_Operation] = []
    for method_name in sorted(vars(api_cls)):
        op = _extract_operation(method_name, vars(api_cls)[method_name])
        if op is not None:
            ops.append(op)
    return ops


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


def _resolve_effective_page_size(page_size: int | None, max_items: int | None) -> int | None:
    """Return the per-page size to request when ``--max-items`` caps the total.

    Only shrink below the natural per-page size (100, Asana's max) when
    ``--max-items`` is *smaller* than what we would otherwise request — that
    way a single trailing page is not wasted. When ``--max-items`` is larger,
    keep the user's explicit ``--page-size`` (or fall back to the SDK default
    of 100); shrinking to ``--max-items`` would push the per-request ``limit``
    above the Asana API cap of 100 and produce a 400 response.
    """
    if max_items is None:
        return page_size
    if max_items < (page_size or 100):
        return max_items
    return page_size


def _make_command(api_cls: type, op: _Operation) -> click.Command:
    """Build a :class:`CommandWithGlobalOptions` for a single SDK method."""
    # If the SDK method has no ``opts`` parameter, docstring-derived named
    # arguments cannot be forwarded — they would be silently dropped at call
    # time. python-asana 5.x does not produce this combination today.
    assert op.has_opts or not op.opts_params, (
        f"{api_cls.__name__}.{op.method_name}: docstring declares params but "
        f"the method has no `opts` argument; CLI options would be dropped"
    )
    path_positionals = op.path_positionals
    has_body = op.has_body
    opts_params = sorted(op.opts_params, key=lambda p: (not p.required, p.name))
    paginatable = op.paginatable

    ws_opt = next((p for p in opts_params if _is_workspace_param(p.name)), None)
    ws_positional = next((n for n in path_positionals if _is_workspace_param(n)), None)
    has_workspace = ws_opt is not None or ws_positional is not None
    ws_required = ws_positional is not None or (ws_opt is not None and ws_opt.required)

    non_ws_positionals = [n for n in path_positionals if not _is_workspace_param(n)]
    non_ws_opts = [p for p in opts_params if not _is_workspace_param(p.name)]

    # Hide the raw SDK ``limit`` for paginatable ops; expose --page-size instead.
    if paginatable:
        non_ws_opts = [p for p in non_ws_opts if p.name != "limit"]

    options: list[click.Option] = []

    # Path positionals → required --options (with ``_gid`` stripped).
    for name in non_ws_positionals:
        opt_name = _option_name(name)
        flag = f"--{opt_name.replace('_', '-')}"
        dp = op.params.get(name)
        help_text = _escape_help(dp.description) if dp else ""
        options.append(click.Option([flag], required=True, help=help_text))

    # Unified --workspace option.
    if has_workspace:
        options.append(
            click.Option(
                ["--workspace"],
                default=None,
                help="Workspace GID (falls back to ASANA_DEFAULT_WORKSPACE)",
            )
        )

    # body → required --body option.
    if has_body:
        body_help = "Request body (JSON string, @file, or - for stdin)"
        bp = op.params.get("body")
        if bp and bp.description:
            body_help = _escape_help(bp.description)
        options.append(click.Option(["--body"], required=True, help=body_help))

    # Remaining opts params (excluding workspace and limit-when-paginatable).
    for p in non_ws_opts:
        flag = f"--{p.name.replace('_', '-')}"
        kw: dict[str, Any] = {"help": _escape_help(p.description)}
        click_type = _click_type(p.py_type)
        if click_type is not None:
            kw["type"] = click_type
        if p.required:
            kw["required"] = True
        else:
            kw["default"] = None
        options.append(click.Option([flag], **kw))

    # Pagination options.
    if paginatable:
        options.append(
            click.Option(
                ["--all-items", "all_items"],
                is_flag=True,
                default=False,
                help="Fetch all items (no cap)",
            )
        )
        options.append(
            click.Option(
                ["--paginate"],
                is_flag=True,
                default=False,
                help="(Deprecated) Alias for --all-items",
            )
        )
        options.append(
            click.Option(
                ["--page-size", "page_size"],
                type=click.IntRange(min=1, max=100),
                default=None,
                help="Items per page (Asana API requires 1-100, default 100)",
            )
        )
        options.append(
            click.Option(
                ["--max-items", "max_items"],
                type=click.IntRange(min=0),
                default=None,
                help="Stop after fetching this many items in total",
            )
        )

    def inner_callback(**kwargs: Any) -> Any:
        # Pop pagination flags first so kwargs only carries SDK params afterwards.
        if paginatable:
            all_items = kwargs.pop("all_items")
            paginate = kwargs.pop("paginate")
            page_size = kwargs.pop("page_size")
            max_items = kwargs.pop("max_items")
        else:
            all_items = paginate = False
            page_size = max_items = None

        if has_body:
            body_value = kwargs.pop("body")  # click marks --body as required
            parsed_body = resolve_body(body_value)
        else:
            parsed_body = None

        if has_workspace:
            workspace_value = kwargs.pop("workspace", None)
            resolved_workspace = resolve_workspace(workspace_value, required=ws_required)
        else:
            resolved_workspace = None

        if paginate:
            click.echo(
                "Warning: --paginate is deprecated; use --all-items instead.",
                err=True,
            )
        fetch_all = all_items or paginate
        if fetch_all and max_items is not None:
            raise click.UsageError(
                "--max-items cannot be combined with --all-items "
                "(or its deprecated alias --paginate)"
            )

        # --max-items 0 makes no API call; skip session creation so we don't
        # briefly set Configuration.page_limit = 0 (derived from max_items)
        # on a session we immediately discard.
        if max_items == 0:
            return []

        effective_page_size = _resolve_effective_page_size(page_size, max_items)

        if paginatable:
            session = AsanaSession.from_env(
                use_page_iterator=fetch_all, page_size=effective_page_size
            )
        else:
            session = AsanaSession.from_env()
        api = api_cls(session.client)

        opts: dict[str, Any] = {}
        for p in non_ws_opts:
            value = kwargs.pop(p.name, None)
            if p.required or value is not None:
                opts[p.name] = value
        if ws_opt is not None and resolved_workspace is not None:
            opts[ws_opt.name] = resolved_workspace

        # Build positional call args. Body comes first, mirroring the
        # python-asana convention (e.g.
        # ``add_followers_for_task(body, task_gid, opts)``).
        call_args: list[Any] = []
        if has_body:
            call_args.append(parsed_body)
        for name in path_positionals:
            if _is_workspace_param(name):
                call_args.append(resolved_workspace)
            else:
                call_args.append(kwargs.pop(_option_name(name)))

        method = getattr(api, op.method_name)
        if paginatable and max_items is not None:
            return session.fetch_capped(method, *call_args, opts=opts, max_items=max_items)
        if op.has_opts:
            return method(*call_args, opts)
        return method(*call_args)

    callback = formatted(inner_callback)

    # ``formatted`` adds --output and --query via click.option decorators; pull
    # those Option instances out of the wrapped callback (in natural order)
    # and append them to our options list.
    fmt_params = list(reversed(getattr(callback, "__click_params__", [])))
    options.extend(fmt_params)

    summary = _escape_help(op.summary or f"Call {api_cls.__name__}.{op.method_name}")
    return CommandWithGlobalOptions(
        name=op.command_name,
        params=options,
        callback=callback,
        help=summary,
    )


# ---------------------------------------------------------------------------
# Lazy API group
# ---------------------------------------------------------------------------


class _ApiGroup(GroupWithGlobalOptions):
    """A click group bound to a single ``*Api`` class.

    Method introspection is deferred until the first ``list_commands`` or
    ``get_command`` call so that top-level ``--help`` does not walk every
    method.
    """

    def __init__(self, api_cls: type, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._api_cls = api_cls
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        for op in _operations_for(self._api_cls):
            self.add_command(_make_command(self._api_cls, op))

    def list_commands(self, ctx: click.Context) -> list[str]:
        self._ensure_loaded()
        return super().list_commands(ctx)

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        self._ensure_loaded()
        return super().get_command(ctx, cmd_name)


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------


# Root uses LazyGroup so that the manually declared @click.option globals are
# not duplicated by GroupWithGlobalOptions' auto-append behavior. Subgroups
# (_ApiGroup) and leaf commands (CommandWithGlobalOptions) still auto-append
# so global options work at any level of the tree.
@click.group(name="asana-api", cls=LazyGroup)
@click.version_option(version_string(), prog_name="asana-api")
@click.option(
    "--host",
    default=None,
    help="Override API base URL (default: https://app.asana.com/api/1.0)",
)
@click.option("--proxy", default=None, help="HTTP/HTTPS proxy URL")
@click.option(
    "--no-verify-ssl",
    is_flag=True,
    default=False,
    help="Disable TLS certificate verification (insecure)",
)
@click.option(
    "--ca-cert",
    "ca_cert",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a PEM bundle of trusted CA certificates",
)
@click.option(
    "--retries",
    type=int,
    default=None,
    help="Number of retries on 429/5xx responses (default: 5)",
)
@click.option("--timeout", type=float, default=None, help="Per-request timeout in seconds")
@click.option(
    "--access-token",
    "access_token",
    default=None,
    help="Asana personal access token (default: $ASANA_ACCESS_TOKEN)",
)
@click.option(
    "--temp-dir",
    "temp_dir",
    default=None,
    type=click.Path(file_okay=False),
    help="Directory for temporary downloads",
)
@click.option(
    "--debug",
    is_flag=True,
    default=False,
    help="Print HTTP request/response to stderr for troubleshooting",
)
def main(
    host: str | None,
    proxy: str | None,
    no_verify_ssl: bool,
    ca_cert: str | None,
    retries: int | None,
    timeout: float | None,
    access_token: str | None,
    temp_dir: str | None,
    debug: bool,
) -> None:
    """Asana API CLI — runtime-introspected wrapper around the python-asana SDK."""
    # JSON output is required to be UTF-8 by RFC 8259, but on Windows the
    # default stdout encoding is the locale code page (cp932 on Japanese
    # Windows), which raises UnicodeEncodeError when writing non-ASCII data.
    # Reconfigure to UTF-8 so the same output works on every platform.
    # The hasattr guard keeps CliRunner's in-memory streams (used by tests)
    # from blowing up, since StringIO has no reconfigure().
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]

    runtime.host = host
    runtime.proxy = proxy
    runtime.verify_ssl = not no_verify_ssl
    runtime.ssl_ca_cert = ca_cert
    runtime.retries = retries
    runtime.timeout = timeout
    if access_token:
        runtime.access_token = access_token
    runtime.temp_dir = temp_dir
    runtime.debug = debug


def _register_groups(root: click.Group) -> None:
    for cls in _enumerate_api_classes():
        root.add_command(
            _ApiGroup(
                api_cls=cls,
                name=_api_class_to_group(cls.__name__).replace("_", "-"),
                help=f"{cls.__name__[:-3]} commands",
            )
        )


_register_groups(main)


# ---------------------------------------------------------------------------
# Snapshot helpers (used by tests/CI to detect SDK surface drift)
# ---------------------------------------------------------------------------


def introspect_to_manifest() -> dict[str, Any]:
    """Return a JSON-serializable summary of the entire CLI surface.

    Used by ``tests/test_cli_surface.py`` to detect surface drift when the
    bundled ``asana`` SDK version changes.
    """
    groups: list[dict[str, Any]] = []
    for cls in _enumerate_api_classes():
        ops = _operations_for(cls)
        if not ops:
            continue
        commands: list[dict[str, Any]] = []
        for op in ops:
            opts_params = sorted(op.opts_params, key=lambda p: (not p.required, p.name))
            commands.append(
                {
                    "command": op.command_name,
                    "method": op.method_name,
                    "positional": list(op.positional),
                    "has_opts": op.has_opts,
                    "paginatable": op.paginatable,
                    "params": [
                        {
                            "name": p.name,
                            "py_type": p.py_type,
                            "required": p.required,
                        }
                        for p in opts_params
                    ],
                }
            )
        groups.append(
            {
                "class_name": cls.__name__,
                "group": _api_class_to_group(cls.__name__).replace("_", "-"),
                "commands": commands,
            }
        )
    return {"groups": groups}
