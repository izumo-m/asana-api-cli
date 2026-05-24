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
* Docstring opts (``limit``, ``offset``, ``sync``, ``assignee``, ...) are
  generated per-command from the SDK docstring — methods that declare
  them get the corresponding ``--`` flag, others do not. This is the
  natural per-method category.
* SDK-uniform inputs are exposed globally (v3.1): the boilerplate kwargs
  ``--full-payload``, ``--item-limit``, ``--header-params``, and the
  ``Configuration`` knobs ``--return-page-iterator/--no-return-page-iterator``
  and ``--page-limit`` appear on every command, since the SDK accepts them
  uniformly across all methods.
* v2.x flags ``--all-items``, ``--page-size``, and ``--max-items`` are
  retained as per-command deprecation aliases (gated by ``paginatable``)
  that warn and forward to their v3 replacements.

Because the CLI surface tracks whatever ``asana`` package version is
installed in the active environment, ``pip install -U asana`` is enough to
pick up newly added SDK methods without releasing a new asana-api-cli.
"""

from __future__ import annotations

import collections.abc
import inspect
import re
import sys
from typing import Any

import asana
import click

from asana_api_cli.click_ext import (
    _SDK_HAS_RETRY_STRATEGY,
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
from asana_api_cli.structured_arg import RETRY_FIELD_SCHEMA, click_callback
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


def _humanize_class_name(name: str) -> str:
    """PascalCase API class name → natural English, used as group-help fallback.

    ``AccessRequests`` → ``"Access requests"``,
    ``AuditLogAPI`` → ``"Audit log API"``,
    ``Typeahead`` → ``"Typeahead"``. Acronyms (all-caps runs of length 2+)
    are preserved; other non-leading words are lowercased.
    """
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
    parts = spaced.split(" ")
    out = [parts[0]]
    for p in parts[1:]:
        if len(p) > 1 and p.isupper():
            out.append(p)
        else:
            out.append(p.lower())
    return " ".join(out)


# Curated short help for each ``*Api`` class. Looked up by the class name
# with the trailing ``Api`` stripped. Wording is sourced from
# ``docs/api-groups.md``, which is the authoritative human-reviewable
# table; ``test_group_descriptions_match_docs`` asserts the two stay in
# sync. Each entry is intentionally kept ≤ 45 chars so click's
# ``make_default_short_help`` (max_length=45) renders it verbatim — no
# "…" truncation that would otherwise drop the key noun.
#
# Entries are intentionally kept across SDK versions: if a future SDK
# removes a group, its description is harmless dead data (and re-engages
# on a downgrade). When the SDK adds a group not in this dict,
# ``_group_short_help`` falls back to ``_humanize_class_name`` so the
# CLI keeps working — adding a curated entry is a soft improvement, not
# a release blocker.
_GROUP_DESCRIPTIONS: dict[str, str] = {
    "AccessRequests": "Manage private-object access requests",
    "Allocations": "Manage user allocations across projects",
    "Attachments": "Upload, list, and remove file attachments",
    "AuditLogAPI": "Read domain audit log events",
    "BatchAPI": "Execute multiple API requests in parallel",
    "Budgets": "Manage project and portfolio budgets",
    "CustomFieldSettings": "List custom fields attached to objects",
    "CustomFields": "Manage workspace custom fields",
    "CustomTypes": "Read workspace custom object types",
    "Events": "Poll resource change events (sync token)",
    "Exports": "Initiate bulk exports of project resources",
    "GoalRelationships": "Manage links between goals",
    "Goals": "Manage organizational goals and metrics",
    "Jobs": "Check status of async background jobs",
    "Memberships": "Manage memberships across object types",
    "OrganizationExports": "Trigger and download org-wide exports",
    "PortfolioMemberships": "Read who has access to portfolios",
    "Portfolios": "Manage portfolios (project collections)",
    "ProjectBriefs": "Manage project brief documents",
    "ProjectMemberships": "Read who has access to projects",
    "ProjectPortfolioSettings": "Settings for projects within portfolios",
    "ProjectStatuses": "Per-project status updates (deprecated)",
    "ProjectTemplates": "Manage and instantiate project templates",
    "Projects": "Manage projects (CRUD + members, etc.)",
    "Rates": "Manage per-user billing rates on projects",
    "Reactions": "Read emoji reactions on stories",
    "Roles": "Manage RBAC roles within a workspace",
    "Rules": "Trigger Asana rule via incoming webhook",
    "Sections": "Manage project sections (board/list)",
    "StatusUpdates": "Manage status updates on any object",
    "Stories": "Manage stories (comments + activity)",
    "Tags": "Manage tags applied to tasks",
    "TaskTemplates": "Manage and instantiate task templates",
    "Tasks": "Manage tasks (CRUD + lifecycle ops)",
    "TeamMemberships": "Read who belongs to teams",
    "Teams": "Manage teams within organizations",
    "TimePeriods": "Read time periods (for goals, reporting)",
    "TimeTrackingCategories": "Manage time-tracking categories",
    "TimeTrackingEntries": "Manage time-tracking entries on tasks",
    "TimesheetApprovalStatuses": "Manage weekly timesheet approval states",
    "Typeahead": "Auto-complete search for workspace objects",
    "UserTaskLists": "Read a user's My Tasks list",
    "Users": "Manage user records (`me` = authenticated)",
    "Webhooks": "Manage webhook subscriptions (real-time)",
    "WorkspaceMemberships": "Read workspace members (admin/guest flags)",
    "Workspaces": "Update workspace and manage its users",
}


def _group_short_help(class_name: str) -> str:
    """Return the short help for a command group, curated or auto-generated."""
    return _GROUP_DESCRIPTIONS.get(class_name, _humanize_class_name(class_name))


# Hand-written help text for endpoint-local options whose SDK ``:param:``
# docstring is empty. The triple ``(class_name, method_name, param_name)``
# is the key because bare param names (``file``, ``parent``, ``name``)
# would collide across endpoints. Sourced from Asana's developer
# reference. Lookup is conditional — only used when the SDK provides no
# description — so an SDK that later fills in a description silently wins
# over the override (which is fine, the SDK text is authoritative).
_OPT_HELP_OVERRIDES: dict[tuple[str, str, str], str] = {
    ("Attachments", "create_attachment_for_object", "connect_to_app"): (
        "Connect this attachment to the current app (for app component widgets)."
    ),
    ("Attachments", "create_attachment_for_object", "file"): (
        "Local file path to upload (required when resource-subtype=asana)."
    ),
    ("Attachments", "create_attachment_for_object", "name"): (
        "Display name for the attachment (required when resource-subtype=external)."
    ),
    ("Attachments", "create_attachment_for_object", "parent"): (
        "GID of the parent task, project, or project_brief."
    ),
    ("Attachments", "create_attachment_for_object", "resource_subtype"): (
        "Attachment type: 'asana' (file upload) or 'external' (URL link); default 'asana'."
    ),
    ("Attachments", "create_attachment_for_object", "url"): (
        "URL of the external resource (required when resource-subtype=external)."
    ),
}


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
    """Strip HTML tags and collapse whitespace; preserve full length.

    Length is left to click's ``wrap_text`` (which wraps to multiple lines
    rather than truncating). A previous version cut the text at 200 chars
    and appended ``"..."``, which routinely dropped critical caveats from
    the end of SDK descriptions (e.g. ``--assignee``'s "*you must also
    specify the `workspace`*" was hidden behind ``...``).
    """
    t = re.sub(r"<[^>]+>", "", text)
    t = re.sub(r"\s+", " ", t).strip()
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
        """True iff the SDK method declares a ``limit`` query parameter.

        Used as the gate for the deprecated v2.x alias flags
        (``--all-items`` / ``--page-size`` / ``--max-items``) which only
        make sense on endpoints that page. The pagination/iterator control
        flags themselves are global as of v3.1; this predicate stays only
        until the v2 aliases are removed in a future version.
        """
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

    # Path positionals → required --options (with ``_gid`` stripped). For
    # ``*_gid`` params the SDK description is uninformative ("Globally unique
    # identifier for the X" or worse, "The task to operate on."), so we
    # synthesize a help line that says it's a GID, gives an example, and
    # uses ``metavar=GID`` so the option signature itself reads naturally
    # (``--task GID`` instead of ``--task TEXT``).
    for name in non_ws_positionals:
        opt_name = _option_name(name)
        flag = f"--{opt_name.replace('_', '-')}"
        opt_kwargs: dict[str, Any] = {"required": True}
        if name.endswith("_gid"):
            thing = opt_name.replace("_", " ")
            opt_kwargs["metavar"] = "GID"
            opt_kwargs["help"] = f"{thing.capitalize()} GID, e.g. 1234567890. (SDK kwarg: {name})"
        else:
            dp = op.params.get(name)
            opt_kwargs["help"] = _escape_help(dp.description) if dp else ""
        options.append(click.Option([flag], **opt_kwargs))

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

    # body → required --body option. The SDK docstring usually has a short
    # "The X to create." line which is not enough — users also need to know
    # the input *format* (inline JSON / @path / stdin) and Asana's
    # ``{"data": {...}}`` envelope. Always append the format hint so help
    # is self-contained even when the SDK description is generic.
    if has_body:
        body_format = (
            'Accepts inline JSON, @path/to/file, or - (stdin). Wrap payload in {"data": {...}}.'
        )
        bp = op.params.get("body")
        sdk_desc = _escape_help(bp.description) if bp and bp.description else ""
        body_help = f"{sdk_desc} {body_format}".strip()
        options.append(click.Option(["--body"], required=True, metavar="JSON", help=body_help))

    # Remaining opts params (excluding workspace). The SDK-derived help is
    # used as-is, with two exceptions:
    #   - When the SDK provides no ``:param:`` docstring (a handful of
    #     endpoints, notably ``attachments create-attachment-for-object``),
    #     fall back to ``_OPT_HELP_OVERRIDES`` so the user isn't staring
    #     at a bare ``--file TEXT``.
    #   - ``--limit`` reads as if it caps the total but is per-HTTP-request;
    #     append the pointer on the option line itself (not only in the
    #     epilog) so a casual Options-table scan still catches the pitfall.
    for p in non_ws_opts:
        flag = f"--{p.name.replace('_', '-')}"
        help_text = _escape_help(p.description)
        if not help_text:
            help_text = _OPT_HELP_OVERRIDES.get(
                (api_cls.__name__[:-3], op.method_name, p.name),
                "",
            )
        if p.name == "limit":
            help_text = f"{help_text} Use --item-limit to cap the total."
        kw: dict[str, Any] = {"help": help_text}
        click_type = _click_type(p.py_type)
        if click_type is not None:
            kw["type"] = click_type
        if p.required:
            kw["required"] = True
        else:
            kw["default"] = None
        options.append(click.Option([flag], **kw))

    # Pagination/iterator control flags (--full-payload, --item-limit,
    # --return-page-iterator/--no-return-page-iterator, --page-limit) are
    # global as of v3.1 — defined once on the root and inherited by every
    # command via ``CommandWithGlobalOptions``. Per-command injection of
    # those flags is no longer needed.
    #
    # Deprecated v2.x aliases remain per-command (gated by ``paginatable``)
    # until they are removed. Each emits a stderr warning at runtime and
    # forwards to the corresponding v3 flag. The option ``name``
    # (``all_items``, ``page_size``, ``max_items``) is what
    # ``_DEPRECATED_OPTION_NAMES`` matches on.
    if paginatable:
        options.append(
            click.Option(
                ["--all-items", "all_items"],
                is_flag=True,
                default=False,
                help="No-op; walking every page is now the default.",
            )
        )
        options.append(
            click.Option(
                ["--page-size", "page_size"],
                type=int,
                default=None,
                help="Alias for --limit.",
            )
        )
        options.append(
            click.Option(
                ["--max-items", "max_items"],
                type=int,
                default=None,
                help="Alias for --item-limit.",
            )
        )

    def inner_callback(**kwargs: Any) -> Any:
        # v2.x deprecated aliases: pop from kwargs (per-command) and warn.
        # Effective values fold into local vars without mutating ``runtime``,
        # so the dispatch state stays scoped to this invocation.
        all_items = kwargs.pop("all_items", False) if paginatable else False
        page_size = kwargs.pop("page_size", None) if paginatable else None
        max_items = kwargs.pop("max_items", None) if paginatable else None

        effective_item_limit = runtime.item_limit

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
            if effective_item_limit is not None:
                raise click.UsageError(
                    "--max-items is the deprecated alias of --item-limit; specify only one"
                )
            effective_item_limit = max_items

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

        with AsanaSession.from_env() as session:
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
            # Forward global iterator / header kwargs uniformly. The SDK
            # accepts ``full_payload`` / ``item_limit`` / ``header_params``
            # on every method (boilerplate kwargs in every ``all_params``
            # list), so we pass them without per-method gating. Methods
            # that do not act on them simply ignore the kwarg.
            method_kwargs: dict[str, Any] = {}
            if effective_item_limit is not None:
                method_kwargs["item_limit"] = effective_item_limit
            if runtime.full_payload:
                method_kwargs["full_payload"] = True
            if runtime.header_params is not None:
                method_kwargs["header_params"] = runtime.header_params
            result = (
                method(*call_args, opts, **method_kwargs) if op.has_opts else method(*call_args)
            )
            # Lazy iterator consumption inside the session context.
            #
            # Two independent layers:
            #   - Layer A (session lifecycle, above): every SDK call runs
            #     inside ``with AsanaSession.from_env() as session:``, which
            #     keeps the ``HttpClientAuthRedactor`` installed.
            #   - Layer B (this block): when the SDK returns a lazy iterator
            #     (PageIterator / EventIterator), iterating it issues one
            #     HTTP request per page. We must consume the iterator *before*
            #     leaving Layer A's ``with`` block — otherwise pages 2..N
            #     are fetched after the redactor is uninstalled and leak
            #     ``Authorization`` into ``--debug`` log.
            #
            # post-judge by return value type: any ``Iterator`` is consumed
            # here regardless of which endpoint produced it.
            if isinstance(result, collections.abc.Iterator):
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


# Shown at the bottom of ``asana-api --help``. Five short, copy-pasteable
# command lines (auth env-var assumed) cover the four most common entry
# points — read, single-fetch, create, debug — plus a pointer to per-group
# ``--help`` for everything else. The trailing reminder names the auth env
# var so the examples make sense without a preceding ``--access-token``.
# Leading ``\b`` keeps click's wrap_text from reflowing the aligned form.
_ROOT_EPILOG = (
    "\b\n"
    "Examples:\n"
    "  asana-api tasks get-tasks --workspace WS --assignee me\n"
    "  asana-api tasks get-task --task 1234567890 --opt-fields name,assignee.name\n"
    "  asana-api tasks create-task --body @new-task.json   # workspace goes inside body\n"
    "  asana-api --debug tasks get-tasks --workspace WS   # show HTTP requests\n"
    "  asana-api <group> --help   # e.g. asana-api tasks --help\n"
    "\n"
    "  Set $ASANA_ACCESS_TOKEN once, or pass --access-token TOKEN."
)


def _retry_strategy_option(f: Any) -> Any:
    """Apply the ``--retry-strategy`` decorator only when the installed
    python-asana exposes ``Configuration.retry_strategy`` (added in 5.1).

    On older SDKs the flag would crash at apply time, so we hide it
    entirely — both from ``--help`` and from the parser, so users on
    5.0.x get a clean ``no such option`` rather than a traceback.
    """
    if not _SDK_HAS_RETRY_STRATEGY:
        return f
    return click.option(
        "--retry-strategy",
        "retry_strategy_overrides",
        default=None,
        callback=click_callback(schema=RETRY_FIELD_SCHEMA),
        help=(
            "Override urllib3 Retry fields. VALUE: 'k1=v1,k2=v2,...', JSON "
            "object, or @path. See urllib3 Retry docs. List-typed fields "
            "(allowed_methods, status_forcelist, remove_headers_on_redirect) "
            "require JSON. (Configuration.retry_strategy)"
        ),
    )(f)


# Root uses LazyGroup so that the manually declared @click.option globals are
# not duplicated by GroupWithGlobalOptions' auto-append behavior. Subgroups
# (_ApiGroup) and leaf commands (CommandWithGlobalOptions) still auto-append
# so global options work at any level of the tree.
@click.group(name="asana-api", cls=LazyGroup, epilog=_ROOT_EPILOG)
@click.version_option(version_string(), prog_name="asana-api")
@click.option(
    "--host",
    default=None,
    help="Override API base URL (default: https://app.asana.com/api/1.0)",
)
@click.option("--proxy", default=None, help="HTTP/HTTPS proxy URL")
@click.option(
    "--verify-ssl/--no-verify-ssl",
    "verify_ssl",
    default=None,
    help=(
        "Verify TLS certificates (default: True). Pass --no-verify-ssl "
        "to disable (insecure). (Configuration.verify_ssl)"
    ),
)
@click.option(
    "--ssl-ca-cert",
    "ssl_ca_cert",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a PEM bundle of trusted CA certs. (Configuration.ssl_ca_cert)",
)
@click.option(
    "--cert-file",
    "cert_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Client TLS certificate for mTLS. (Configuration.cert_file)",
)
@click.option(
    "--key-file",
    "key_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Client TLS private key for mTLS. (Configuration.key_file)",
)
@click.option(
    "--assert-hostname/--no-assert-hostname",
    "assert_hostname",
    default=None,
    help=(
        "Verify the server certificate's hostname matches the request URL "
        "host. Tri-state: unspecified → urllib3 default. "
        "(Configuration.assert_hostname)"
    ),
)
@_retry_strategy_option
@click.option(
    "--request-timeout",
    "request_timeout",
    type=float,
    default=None,
    help="Per-request timeout in seconds. (SDK kwarg: _request_timeout)",
)
@click.option(
    "--connection-pool-maxsize",
    "connection_pool_maxsize",
    type=click.IntRange(min=1),
    default=None,
    help=(
        "Max urllib3 connections cached per host (default: cpu_count "
        "* 5). (Configuration.connection_pool_maxsize)"
    ),
)
@click.option(
    "--access-token",
    "access_token",
    default=None,
    help="Asana personal access token (default: $ASANA_ACCESS_TOKEN)",
)
@click.option(
    "--username",
    "username",
    default=None,
    help="Use --access-token. (Configuration.username)",
)
@click.option(
    "--password",
    "password",
    default=None,
    help="Use --access-token. (Configuration.password)",
)
@click.option(
    "--api-key",
    "api_key",
    default=None,
    callback=click_callback(),
    help="Use --access-token. (Configuration.api_key)",
)
@click.option(
    "--api-key-prefix",
    "api_key_prefix",
    default=None,
    callback=click_callback(),
    help="Use --access-token. (Configuration.api_key_prefix)",
)
@click.option(
    "--temp-folder-path",
    "temp_folder_path",
    default=None,
    type=click.Path(file_okay=False),
    help="Directory for temporary downloads. (Configuration.temp_folder_path)",
)
@click.option(
    "--safe-chars-for-path-param",
    "safe_chars_for_path_param",
    default=None,
    help=(
        "Extra chars treated as safe when percent-encoding path "
        "parameters. (Configuration.safe_chars_for_path_param)"
    ),
)
@click.option(
    "--logger-format",
    "logger_format",
    default=None,
    help="Python logging format string. (Configuration.logger_format)",
)
@click.option(
    "--logger-file",
    "logger_file",
    default=None,
    type=click.Path(dir_okay=False),
    help="Path SDK loggers write to. (Configuration.logger_file)",
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
@click.option(
    "--return-page-iterator/--no-return-page-iterator",
    "return_page_iterator",
    default=None,
    help=(
        "Toggle the SDK page iterator (default: enabled). With "
        "--no-return-page-iterator, paginatable endpoints return a "
        "single {data, next_page} dict from one HTTP call instead of "
        "auto-walking every page. (Configuration.return_page_iterator)"
    ),
)
@click.option(
    "--page-limit",
    "page_limit",
    type=int,
    default=None,
    help=(
        "Per-page size when the iterator falls back to Configuration "
        "(default: 100). Equivalent to --limit on paginatable endpoints; "
        '--limit (per-call opts["limit"]) takes precedence when both '
        "are set. (Configuration.page_limit)"
    ),
)
@click.option(
    "--item-limit",
    "item_limit",
    type=int,
    default=None,
    help=(
        "Stop after this many items total in iterator mode (kwarg item_limit). "
        "Silently ignored in --full-payload / --no-return-page-iterator modes."
    ),
)
@click.option(
    "--full-payload",
    "full_payload",
    is_flag=True,
    default=False,
    help=(
        "Return a single raw payload dict from one HTTP call "
        "(kwarg full_payload=True). Equivalent to --no-return-page-iterator. "
        "For events get-events this yields {data, sync, has_more} so sync "
        "tokens stay reachable from shell scripts."
    ),
)
@click.option(
    "--header-params",
    "header_params",
    default=None,
    callback=click_callback(),
    help=(
        "Custom HTTP request headers merged into the request "
        "(kwarg header_params). VALUE: 'k1=v1,k2=v2,...', JSON object, "
        "or @path. Use cases include Asana-Enable/-Disable deprecation "
        "opt-in. Not redacted in --debug output — see SECURITY.md."
    ),
)
def main(
    host: str | None,
    proxy: str | None,
    verify_ssl: bool | None,
    ssl_ca_cert: str | None,
    cert_file: str | None,
    key_file: str | None,
    assert_hostname: bool | None,
    # ``retry_strategy_overrides`` and everything after it have ``= None``
    # defaults so the ``--retry-strategy`` decorator can be skipped on
    # python-asana <5.1 without click then trying to call this function
    # without a value for that name.
    retry_strategy_overrides: dict[str, Any] | None = None,
    request_timeout: float | None = None,
    connection_pool_maxsize: int | None = None,
    access_token: str | None = None,
    username: str | None = None,
    password: str | None = None,
    api_key: dict[str, str] | None = None,
    api_key_prefix: dict[str, str] | None = None,
    temp_folder_path: str | None = None,
    safe_chars_for_path_param: str | None = None,
    logger_format: str | None = None,
    logger_file: str | None = None,
    debug: bool = False,
    multibyte_filenames: bool = False,
    return_page_iterator: bool | None = None,
    page_limit: int | None = None,
    item_limit: int | None = None,
    full_payload: bool = False,
    header_params: dict[str, str] | None = None,
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
    # Both verify_ssl and assert_hostname are tri-state toggles (positive /
    # negative / unset). Guard so the unset case does not clobber a value
    # set by an earlier code path; symmetry with how the leaf-level
    # propagation in ``click_ext._consume_global_options`` skips the
    # default ``None``.
    if verify_ssl is not None:
        runtime.verify_ssl = verify_ssl
    runtime.ssl_ca_cert = ssl_ca_cert
    runtime.cert_file = cert_file
    runtime.key_file = key_file
    if assert_hostname is not None:
        runtime.assert_hostname = assert_hostname
    runtime.retry_strategy_overrides = retry_strategy_overrides
    runtime.request_timeout = request_timeout
    runtime.connection_pool_maxsize = connection_pool_maxsize
    if access_token:
        runtime.access_token = access_token
    runtime.username = username
    runtime.password = password
    runtime.api_key = api_key
    runtime.api_key_prefix = api_key_prefix
    runtime.temp_folder_path = temp_folder_path
    runtime.safe_chars_for_path_param = safe_chars_for_path_param
    runtime.logger_format = logger_format
    runtime.logger_file = logger_file
    runtime.debug = debug
    runtime.multibyte_filenames = multibyte_filenames
    if return_page_iterator is not None:
        runtime.return_page_iterator = return_page_iterator
    runtime.page_limit = page_limit
    runtime.item_limit = item_limit
    runtime.full_payload = full_payload
    runtime.header_params = header_params


def _register_groups(root: click.Group) -> None:
    for cls in _enumerate_api_classes():
        root.add_command(
            _ApiGroup(
                api_cls=cls,
                name=_api_class_to_group(cls.__name__).replace("_", "-"),
                help=_group_short_help(cls.__name__[:-3]),
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
