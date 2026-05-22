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
* Paginatable methods (those with a ``limit`` doc-param) expose ``--limit``,
  ``--offset``, ``--page-limit``, ``--item-limit``,
  ``--no-return-page-iterator``, and ``--full-payload``. Each maps 1:1 to a
  python-asana SDK input (opts key, ``Configuration`` property, or method
  kwarg) so the CLI works as a thin probe for SDK behavior. v2.x flags
  ``--all-items``, ``--page-size``, and ``--max-items`` are retained as
  deprecation aliases that warn and forward.

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

    # `_PARAM_RE` already drops the SDK's `:param async_req bool` line (no
    # colon after the type, so the regex never matches). Kept as a guard in
    # case the SDK docstring format changes to the colon form.
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

    options: list[click.Option] = []

    # Path positionals → required --options (with ``_gid`` stripped).
    for name in non_ws_positionals:
        opt_name = _option_name(name)
        flag = f"--{opt_name.replace('_', '-')}"
        dp = op.params.get(name)
        help_text = _escape_help(dp.description) if dp else ""
        if name != opt_name:
            suffix = f"(SDK kwarg: {name})"
            help_text = f"{help_text} {suffix}".strip() if help_text else suffix
        options.append(click.Option([flag], required=True, help=help_text))

    # Unified --workspace option. The env-var fallback only applies when the
    # endpoint requires a workspace; for optional-workspace endpoints (e.g.
    # ``get-tasks``) the CLI deliberately does not auto-fill from the env var,
    # so the help text differs by case.
    if has_workspace:
        ws_help = (
            "Workspace GID (falls back to ASANA_DEFAULT_WORKSPACE)"
            if ws_required
            else "Workspace GID (optional; not auto-filled from ASANA_DEFAULT_WORKSPACE)"
        )
        options.append(
            click.Option(
                ["--workspace"],
                default=None,
                help=ws_help,
            )
        )

    # body → required --body option.
    if has_body:
        body_help = "Request body (JSON string, @file, or - for stdin)"
        bp = op.params.get("body")
        if bp and bp.description:
            body_help = _escape_help(bp.description)
        options.append(click.Option(["--body"], required=True, help=body_help))

    # Remaining opts params (excluding workspace).
    for p in non_ws_opts:
        flag = f"--{p.name.replace('_', '-')}"
        help_text = _escape_help(p.description)
        # ``--limit`` is the per-page size sent to the server; users
        # routinely confuse it with a total cap. Point them at
        # ``--item-limit`` to avoid that pitfall.
        if p.name == "limit":
            help_text = (
                f"{help_text} (This caps each HTTP request, not the total "
                "number of items returned across pages — use --item-limit "
                "for a total cap.)"
            )
        kw: dict[str, Any] = {"help": help_text}
        click_type = _click_type(p.py_type)
        if click_type is not None:
            kw["type"] = click_type
        if p.required:
            kw["required"] = True
        else:
            kw["default"] = None
        options.append(click.Option([flag], **kw))

    # Pagination options. Each flag maps 1:1 to a python-asana SDK input
    # (``Configuration`` property, ``opts`` key, or method kwarg).
    if paginatable:
        options.append(
            click.Option(
                ["--no-return-page-iterator", "no_return_page_iterator"],
                is_flag=True,
                default=False,
                help=(
                    "Set Configuration.return_page_iterator=False. The SDK "
                    "returns a single {data, next_page} dict from one HTTP "
                    "call instead of an iterator. Equivalent to --full-payload."
                ),
            )
        )
        options.append(
            click.Option(
                ["--page-limit", "page_limit"],
                type=int,
                default=None,
                help=(
                    "Set Configuration.page_limit (SDK default: 100). Used "
                    "as the per-page size when --limit is not set and the "
                    "iterator path is taken (i.e. when neither "
                    "--no-return-page-iterator nor --full-payload is given)."
                ),
            )
        )
        options.append(
            click.Option(
                ["--item-limit", "item_limit"],
                type=int,
                default=None,
                help=(
                    "SDK kwarg: item_limit. Stop after this many items have "
                    "been yielded. Honored only on the iterator path; "
                    "silently ignored when --no-return-page-iterator or "
                    "--full-payload is set."
                ),
            )
        )
        options.append(
            click.Option(
                ["--full-payload", "full_payload"],
                is_flag=True,
                default=False,
                help=(
                    "SDK kwarg: full_payload=True. Skip the iterator and "
                    "return a single {data, next_page} dict from one HTTP "
                    "call. Equivalent to --no-return-page-iterator."
                ),
            )
        )
        # Deprecated v2.x aliases. Each emits a stderr warning at runtime
        # and forwards to the corresponding v3 flag (or no-ops when the
        # behavior is now the default). Kept visible in --help with a
        # ``[Deprecated v3.0]`` prefix so v2 users discover the migration
        # path without having to read the changelog.
        options.append(
            click.Option(
                ["--all-items", "all_items"],
                is_flag=True,
                default=False,
                help=(
                    "[Deprecated v3.0] No-op; walking every page is now the "
                    "default. Removed in a future release."
                ),
            )
        )
        options.append(
            click.Option(
                ["--page-size", "page_size"],
                type=int,
                default=None,
                help=("[Deprecated v3.0] Alias for --limit. Removed in a future release."),
            )
        )
        options.append(
            click.Option(
                ["--max-items", "max_items"],
                type=int,
                default=None,
                help=("[Deprecated v3.0] Alias for --item-limit. Removed in a future release."),
            )
        )

    def inner_callback(**kwargs: Any) -> Any:
        # Pop all pagination control flags before later code touches kwargs,
        # so the opts loop and positional extractor see only docstring-derived
        # parameters. The else branch keeps these names bound for
        # non-paginatable commands so the listify check below need not
        # re-guard them.
        if paginatable:
            no_return_page_iterator = kwargs.pop("no_return_page_iterator")
            page_limit = kwargs.pop("page_limit")
            item_limit = kwargs.pop("item_limit")
            full_payload = kwargs.pop("full_payload")
            all_items = kwargs.pop("all_items")
            page_size = kwargs.pop("page_size")
            max_items = kwargs.pop("max_items")
        else:
            no_return_page_iterator = full_payload = False
            page_limit = item_limit = page_size = max_items = None
            all_items = False

        # Deprecated v2.x aliases: warn and resolve to the v3 flag.
        if all_items:
            click.echo(
                "warning: --all-items is deprecated; walking every page is "
                "now the default (will be removed in a future release)",
                err=True,
            )
        if page_size is not None:
            click.echo(
                "warning: --page-size is deprecated; use --limit instead "
                "(will be removed in a future release)",
                err=True,
            )
            if kwargs.get("limit") is not None:
                raise click.UsageError(
                    "--page-size is the deprecated alias of --limit; specify only one"
                )
            kwargs["limit"] = page_size
        if max_items is not None:
            click.echo(
                "warning: --max-items is deprecated; use --item-limit "
                "instead (will be removed in a future release)",
                err=True,
            )
            if item_limit is not None:
                raise click.UsageError(
                    "--max-items is the deprecated alias of --item-limit; specify only one"
                )
            item_limit = max_items

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

        if paginatable:
            session_ctx = AsanaSession.from_env(
                return_page_iterator=not no_return_page_iterator,
                page_limit=page_limit,
            )
        else:
            session_ctx = AsanaSession.from_env()

        with session_ctx as session:
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
            method_kwargs: dict[str, Any] = {}
            if item_limit is not None:
                method_kwargs["item_limit"] = item_limit
            if full_payload:
                method_kwargs["full_payload"] = True
            result = (
                method(*call_args, opts, **method_kwargs) if op.has_opts else method(*call_args)
            )
            # When the SDK returns a PageIterator it lazily issues an HTTP
            # request per page on iteration. The formatter would otherwise
            # iterate it after this ``with`` block exits — that is, after
            # the debug redactor has been uninstalled — leaking Authorization
            # headers on every page past the first. Consume it here so every
            # page request lands while the session (and its redactor) is
            # still live.
            if paginatable and not no_return_page_iterator and not full_payload:
                result = list(result)
            return result

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
    type=click.IntRange(min=0),
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
@click.option(
    "--multibyte-filenames",
    "multibyte_filenames",
    is_flag=True,
    default=False,
    help=(
        "Emit RFC 5987 filename*=UTF-8'' on multipart uploads. Required for "
        "attachment uploads whose filename contains non-ASCII characters; "
        "off by default to match the underlying SDK behavior. "
        "[asana-api extension]"
    ),
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
    multibyte_filenames: bool,
) -> None:
    """Asana API CLI — runtime-introspected wrapper around the python-asana SDK."""
    # JSON I/O is required to be UTF-8 by RFC 8259, but on Windows the default
    # stream encodings are the locale code page (e.g. cp932 on Japanese
    # Windows): stdout/stderr raise UnicodeEncodeError when writing non-ASCII
    # data, and stdin silently misdecodes a UTF-8 ``--body -`` payload.
    # Reconfigure all three to UTF-8 so the same input/output works on every
    # platform. The hasattr guard keeps CliRunner's in-memory streams (used
    # by tests) from blowing up, since StringIO has no reconfigure().
    for stream in (sys.stdin, sys.stdout, sys.stderr):
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
    runtime.multibyte_filenames = multibyte_filenames


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
