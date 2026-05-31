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
* The boilerplate per-call kwargs ``--item-limit`` / ``--full-payload`` /
  ``--header-params`` / ``--request-timeout`` (the SDK's ``all_params``) are
  common per-command options present on every command — they are method
  inputs, labeled ``(kwargs: ...)``. The ``Configuration`` knobs
  ``--return-page-iterator/--no-return-page-iterator`` and ``--page-limit``
  are global flags. Each option's ``--help`` carries an SDK-destination
  label; see ``_sdk_dest``.
* ``--all-items``, ``--page-size``, and ``--max-items`` are retained as
  per-command deprecation aliases (gated by ``paginatable``) that warn
  and forward to their replacements.

Because the CLI surface tracks whatever ``asana`` package version is
installed in the active environment, ``pip install -U asana`` is enough to
pick up newly added SDK methods without releasing a new asana-api-cli.
"""

from __future__ import annotations

import collections.abc
import functools
import inspect
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import asana
import click

from asana_api_cli.click_ext import (
    _SDK_HAS_RETRY_STRATEGY,
    CommandWithGlobalOptions,
    GroupWithGlobalOptions,
    LazyGroup,
    _make_global_option_params,
)
from asana_api_cli.formatter import formatted, formatter_flag_names
from asana_api_cli.session import (
    AsanaSession,
    runtime,
)
from asana_api_cli.structured_arg import RETRY_FIELD_SCHEMA, click_callback
from asana_api_cli.version import version_string

# ---------------------------------------------------------------------------
# Input resolution
#
# Turn a raw CLI option value into the argument the SDK call receives, exiting
# with code 2 (user-input error) on bad input. These are pure invocation-layer
# helpers — no SDK client / session involved — called only from the command
# callback below.
# ---------------------------------------------------------------------------

DEFAULT_WORKSPACE_ENV = "ASANA_DEFAULT_WORKSPACE"

JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None


def resolve_body(value: str) -> JsonValue:
    """Parse a body argument as JSON.

    Supports three input forms:
    - ``@path`` — read JSON from a file
    - ``-``     — read JSON from stdin
    - otherwise — parse the string itself as JSON
    """
    if value == "-":
        try:
            raw = sys.stdin.read()
        except UnicodeDecodeError as exc:
            # stdin is reconfigured to UTF-8 at startup (see ``main``), so
            # non-UTF-8 input from a pipe surfaces here instead of being
            # silently misdecoded with the locale code page.
            click.echo(f"Body from stdin is not valid UTF-8: {exc}", err=True)
            sys.exit(2)
    elif value.startswith("@"):
        path = Path(value[1:])
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            click.echo(f"Body file not found: {path}", err=True)
            sys.exit(2)
        except UnicodeDecodeError as exc:
            click.echo(
                f"Body file {path} is not valid UTF-8: {exc}",
                err=True,
            )
            sys.exit(2)
        except OSError as exc:
            click.echo(f"Cannot read body file {path}: {exc}", err=True)
            sys.exit(2)
    else:
        raw = value

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        click.echo(f"Invalid JSON in body: {exc}", err=True)
        sys.exit(2)


def resolve_workspace(
    explicit: str | None,
    *,
    required: bool = False,
) -> str | None:
    """Resolve workspace GID with fallback chain.

    Priority: explicit ``--workspace`` value > ``ASANA_DEFAULT_WORKSPACE``
    env var (only when *required* is True).

    When workspace is optional (``required=False``), the env-var fallback is
    **not** used. This prevents the default workspace from being sent
    alongside other scope parameters (e.g. ``--project`` on ``get-tasks``)
    that the Asana API accepts in place of workspace.

    If *required* is True and no value is found, exits with an error.
    """
    if explicit is not None:
        return explicit
    if required:
        ws = os.environ.get(DEFAULT_WORKSPACE_ENV)
        if ws:
            return ws
        click.echo(
            f"Workspace is required. Specify --workspace or set {DEFAULT_WORKSPACE_ENV}.",
            err=True,
        )
        sys.exit(2)
    return None


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
# sync.
#
# Length constraint: each entry must fit in ≤ 45 chars. click's
# ``make_default_short_help`` (max_length=45) renders the Commands table
# on ``asana-api --help``; longer strings get truncated mid-sentence
# with "…", routinely dropping the key noun (``organization-wide…``,
# ``tasks and…``). The same wording also appears verbatim as the
# group-level ``--help`` header, so the limit keeps both renderings
# aligned. When tempted to write something richer than 45 chars allows,
# prefer:
#   - a stronger, shorter verb (``Trigger and download org-wide
#     exports`` over ``Request and retrieve organization-wide exports``)
#   - the Asana resource word over an explanatory paraphrase
#     (``(deprecated)`` parenthetical over "legacy … prefer X")
#   - omitting redundant context the section already implies
#     (``Read who has access to portfolios``, not "List the user
#     records who have access to a given portfolio")
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
    "Exports": "Initiate graph or resource exports",
    "GoalRelationships": "Manage links between goals",
    "Goals": "Manage organizational goals and metrics",
    "Jobs": "Check status of async background jobs",
    "Memberships": "Manage memberships across object types",
    "OrganizationExports": "Trigger and download org-wide exports",
    "PortfolioMemberships": "Read who has access to portfolios",
    "Portfolios": "Manage portfolios (project collections)",
    "ProjectBriefs": "Manage project briefs",
    "ProjectMemberships": "Read who has access to projects",
    "ProjectPortfolioSettings": "Read/update project-portfolio settings",
    "ProjectStatuses": "Post project statuses (deprecated)",
    "ProjectTemplates": "Instantiate or remove project templates",
    "Projects": "Manage projects, members, and followers",
    "Rates": "Manage per-user billing rates on projects",
    "Reactions": "Read emoji reactions on stories",
    "Roles": "Manage user roles within a workspace",
    "Rules": "Trigger Asana rule via incoming webhook",
    "Sections": "Manage project sections (board/list)",
    "StatusUpdates": "Post status updates on any object",
    "Stories": "Manage stories (comments + activity)",
    "Tags": "Manage tags applied to tasks",
    "TaskTemplates": "Instantiate or remove task templates",
    "Tasks": "Manage tasks, subtasks, and dependencies",
    "TeamMemberships": "Read who belongs to teams",
    "Teams": "Manage teams within organizations",
    "TimePeriods": "Read time periods (for goals, reporting)",
    "TimeTrackingCategories": "Manage time-tracking categories",
    "TimeTrackingEntries": "Manage time-tracking entries on tasks",
    "TimesheetApprovalStatuses": "Manage weekly timesheet approval statuses",
    "Typeahead": "Type-ahead lookup of workspace resources",
    "UserTaskLists": "Read a user's My Tasks list",
    "Users": "Read/update users (`me` = authenticated)",
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


# SDK-destination labels. Every CLI option's --help ends with a uniform
# ``(<kind>: <name>)`` suffix naming where its value lands in the python-asana
# call, so a reader can map the flag back to the SDK. Square brackets are
# reserved for click's own ``[required]`` / ``[default]`` metadata, so every
# asana-api label uses parentheses. Five kinds cover the SDK input structure:
#
#   (Configuration: <name>)  set on asana.Configuration         (global flags)
#   (args: <name>)           positional method argument          (body / path GID / workspace_gid)
#   (opts: <name>)           entry in the method ``opts`` dict   (docstring :param)
#   (kwargs: <name>)         boilerplate **kwargs every method accepts (all_params)
#   (asana-api: extension)   no SDK counterpart                  (CLI-only)
#
# Configuration globals carry the literal by hand in both ``main`` and
# ``_make_global_option_params`` (kept byte-identical between cli.py and
# click_ext.py by ``test_click_ext.TestHelpTextSync``); the CLI-only formatter
# flags (``--output`` / ``--query`` / ``--csv-bom`` and the error-path twins
# ``--exception-output`` / ``--exception-query``) live only in ``formatted``. This
# helper builds every label
# ``_make_command`` derives at runtime: ``args`` / ``opts`` for path / body /
# docstring params, ``kwargs`` for the common per-call kwargs, and the
# extension marker on the deprecated aliases.
def _sdk_dest(category: str, name: str = "") -> str:
    if category == "args":
        return f"(args: {name})"
    if category == "opts":
        return f"(opts: {name})"
    if category == "kwargs":
        return f"(kwargs: {name})"
    if category == "extension":
        return "(asana-api: extension)"
    raise ValueError(f"unknown SDK-destination category: {category!r}")


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

        Used as the gate for the deprecated alias flags
        (``--all-items`` / ``--page-size`` / ``--max-items``) which only
        make sense on endpoints that page. The pagination/iterator control
        flags themselves are global; this predicate stays only until the
        deprecated aliases are removed.
        """
        return any(p.name == "limit" for p in self.opts_params)

    @property
    def does_upload(self) -> bool:
        """True iff the SDK method performs a multipart file upload.

        Detected by the presence of a ``file`` opt — a cheap proxy for "the
        method populates ``local_var_files`` / sends ``multipart/form-data``".
        The only such method in python-asana is
        ``attachments create-attachment-for-object``. The proxy is held exact
        by ``tests/test_sdk_boilerplate.py`` (a source scan of the whole SDK),
        so a future SDK that adds or renames an upload endpoint trips that
        guard rather than silently misclassifying the command.

        Gates the per-command ``--multibyte-filenames`` extension flag, which
        only affects multipart uploads whose filename is non-ASCII.
        """
        return any(p.name == "file" for p in self.opts_params)

    @property
    def workspace_positional(self) -> str | None:
        """The path positional that is a workspace GID, if any (``workspace_gid``)."""
        return next((n for n in self.path_positionals if _is_workspace_param(n)), None)

    @property
    def workspace_opt(self) -> _DocParam | None:
        """The ``opts`` param that is a workspace filter, if any (``workspace``)."""
        return next((p for p in self.opts_params if _is_workspace_param(p.name)), None)

    @property
    def has_workspace(self) -> bool:
        return self.workspace_positional is not None or self.workspace_opt is not None

    @property
    def workspace_required(self) -> bool:
        """Workspace is required exactly when it is a path positional.

        An ``opts`` workspace is always optional — no python-asana method marks
        a query param ``(required)``. Drives the ``ASANA_DEFAULT_WORKSPACE``
        env-var fallback: auto-fill only when required.
        """
        wo = self.workspace_opt
        return self.workspace_positional is not None or (wo is not None and wo.required)


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


def _make_per_call_kwarg_options() -> list[click.Option]:
    """Fresh ``click.Option`` instances for the user-facing SDK ``all_params``
    kwargs, common to every command.

    These are method inputs (the boilerplate ``**kwargs`` every SDK method
    accepts — see ``tests/test_sdk_boilerplate.py``), so they render as
    per-command options labeled ``(kwargs: ...)`` rather than global flags.
    ``--page-limit`` / ``--return-page-iterator`` stay global because they are
    ``Configuration`` properties, not per-call kwargs. Fresh instances are
    returned each call because click stores per-command state on Option objects
    (same reason as ``click_ext._make_global_option_params``).
    """
    return [
        click.Option(
            ["--item-limit", "item_limit"],
            type=int,
            default=None,
            help=(
                "Stop after this many items total in iterator mode. Silently "
                "ignored in --full-payload / --no-return-page-iterator modes. "
                f"{_sdk_dest('kwargs', 'item_limit')}"
            ),
        ),
        click.Option(
            ["--full-payload", "full_payload"],
            is_flag=True,
            default=False,
            help=(
                "Return a single raw payload dict from one HTTP call. "
                "Equivalent to --no-return-page-iterator. For events get-events "
                "this yields {data, sync, has_more} so sync tokens stay "
                f"reachable from shell scripts. {_sdk_dest('kwargs', 'full_payload')}"
            ),
        ),
        click.Option(
            ["--header-params", "header_params"],
            default=None,
            callback=click_callback(),
            help=(
                "Custom HTTP request headers merged into the request. VALUE: "
                "'k1=v1,k2=v2,...', JSON object, or @path. Use cases include "
                "Asana-Enable/-Disable deprecation opt-in. Not redacted in "
                f"--debug output — see SECURITY.md. {_sdk_dest('kwargs', 'header_params')}"
            ),
        ),
        click.Option(
            ["--request-timeout", "request_timeout"],
            type=float,
            default=None,
            help=f"Per-request timeout in seconds. {_sdk_dest('kwargs', '_request_timeout')}",
        ),
    ]


@functools.cache
def _static_reserved_flags() -> frozenset[str]:
    """Built-in CLI flag strings present on (essentially) every command.

    An SDK arg/opt whose derived flag lands in this set is exposed with a
    ``sdk-`` prefix (see :func:`_decls`) so the built-in keeps its bare name.
    Derived from the actual option builders (not hand-kept) so it tracks
    renames / additions automatically. Per-command conditional flags
    (deprecated aliases, ``--multibyte-filenames``) are added in
    :func:`_reserved_flags`.

    ``--help`` is added by click at parse time (never in ``cmd.params``) and
    ``--version`` is root-only, so neither is discoverable by scanning a leaf's
    params — they are listed explicitly so a future SDK ``help`` / ``version``
    param is still pushed to ``--sdk-help`` / ``--sdk-version``.
    """
    flags: set[str] = {"--help", "--version"}
    flags |= formatter_flag_names()
    for params in (_make_per_call_kwarg_options(), _make_global_option_params()):
        for p in params:
            flags.update(p.opts)
            flags.update(getattr(p, "secondary_opts", []))
    return frozenset(flags)


def _reserved_flags(op: _Operation) -> frozenset[str]:
    """Built-in flags this command occupies (static set + per-command extras)."""
    flags = set(_static_reserved_flags())
    if op.paginatable:
        flags |= {"--all-items", "--page-size", "--max-items"}
    if op.does_upload:
        flags.add("--multibyte-filenames")
    return frozenset(flags)


def _decls(flag: str, dest: str, reserved: frozenset[str]) -> list[str]:
    """Declaration list for an SDK-derived option, ``sdk-`` prefixed on collision.

    If ``flag`` collides with a built-in CLI flag (in ``reserved``), the SDK
    param yields: it is exposed as ``--sdk-<name>`` with an *explicit* ``dest``
    equal to the SDK param name, so the call path (which pops by param name) is
    unchanged and the ``(opts/arg: <name>)`` help label still shows the real
    name. Otherwise the bare ``[flag]`` is used (dest auto-derives to ``dest``).
    """
    if flag in reserved:
        return [f"--sdk-{flag.removeprefix('--')}", dest]
    return [flag]


def _path_arg_option(name: str, op: _Operation, reserved: frozenset[str]) -> click.Option:
    """Render a required path positional as ``--<name>`` (``_gid`` stripped).

    For ``*_gid`` params the SDK description is uninformative ("Globally unique
    identifier for the X", or "The task to operate on."), so we synthesize a
    help line that says it's a GID, gives an example, and uses ``metavar=GID``
    so the signature reads ``--task GID`` not ``--task TEXT``. Non-``_gid`` path
    args (e.g. ``parent`` / ``target``) keep their docstring description.
    """
    opt_name = _option_name(name)
    flag = f"--{opt_name.replace('_', '-')}"
    kw: dict[str, Any] = {"required": True}
    if name.endswith("_gid"):
        thing = opt_name.replace("_", " ")
        kw["metavar"] = "GID"
        kw["help"] = f"{thing.capitalize()} GID, e.g. 1234567890. {_sdk_dest('args', name)}"
    else:
        dp = op.params.get(name)
        desc = _escape_help(dp.description) if dp else ""
        kw["help"] = f"{desc} {_sdk_dest('args', name)}".strip()
    return click.Option(_decls(flag, opt_name, reserved), **kw)


def _body_option(op: _Operation, reserved: frozenset[str]) -> click.Option:
    """Render the required ``--body`` option with the input-format hint.

    The SDK docstring usually has only a terse "The X to create." line; users
    also need the input *format* (inline JSON / @path / stdin) and Asana's
    ``{"data": {...}}`` envelope, so the hint is always appended.
    """
    body_format = (
        'Accepts inline JSON, @path/to/file, or - (stdin). Wrap payload in {"data": {...}}.'
    )
    bp = op.params.get("body")
    sdk_desc = _escape_help(bp.description) if bp and bp.description else ""
    help_text = f"{sdk_desc} {body_format} {_sdk_dest('args', 'body')}".strip()
    return click.Option(
        _decls("--body", "body", reserved), required=True, metavar="JSON", help=help_text
    )


def _workspace_option(op: _Operation, reserved: frozenset[str]) -> click.Option:
    """Render the unified ``--workspace`` option.

    ``--workspace`` is polymorphic: a positional ``workspace_gid`` on some
    endpoints, an ``opts['workspace']`` on others. It is labelled for whichever
    shape this method declares so the SDK destination stays accurate. The
    env-var fallback (``ASANA_DEFAULT_WORKSPACE``) applies only when the param
    is required — i.e. a path positional; optional-workspace endpoints (e.g.
    ``get-tasks``) are deliberately not auto-filled.
    """
    wp = op.workspace_positional
    if wp is not None:
        label = _sdk_dest("args", wp)
    else:
        wo = op.workspace_opt
        assert wo is not None  # caller invokes this only when a workspace param exists
        label = _sdk_dest("opts", wo.name)
    help_text = (
        "Workspace GID (falls back to ASANA_DEFAULT_WORKSPACE)"
        if op.workspace_required
        else "Workspace GID (optional; not auto-filled from ASANA_DEFAULT_WORKSPACE)"
    )
    return click.Option(
        _decls("--workspace", "workspace", reserved), default=None, help=f"{help_text} {label}"
    )


def _plain_opt_option(
    p: _DocParam, api_cls: type, op: _Operation, reserved: frozenset[str]
) -> click.Option:
    """Render a docstring ``opts`` param as ``--<name>`` (non-workspace).

    Two special cases: an empty SDK ``:param`` description falls back to
    ``_OPT_HELP_OVERRIDES`` (a few endpoints, notably the upload ``--file``);
    and ``--limit`` gets the "per-request — use --item-limit to cap the total"
    pointer appended on the option line itself (not only the epilog) so a
    casual Options-table scan still catches the pitfall.
    """
    flag = f"--{p.name.replace('_', '-')}"
    help_text = _escape_help(p.description)
    if not help_text:
        help_text = _OPT_HELP_OVERRIDES.get((api_cls.__name__[:-3], op.method_name, p.name), "")
    if p.name == "limit":
        help_text = f"{help_text} Use --item-limit to cap the total."
    help_text = f"{help_text} {_sdk_dest('opts', p.name)}".strip()
    kw: dict[str, Any] = {"help": help_text}
    click_type = _click_type(p.py_type)
    if click_type is not None:
        kw["type"] = click_type
    if p.required:
        kw["required"] = True
    else:
        kw["default"] = None
    return click.Option(_decls(flag, p.name, reserved), **kw)


def _make_command(api_cls: type, op: _Operation) -> click.Command:
    """Build a :class:`CommandWithGlobalOptions` for a single SDK method.

    Option display order (mirrors the SDK call contract, then SDK docs):

    1. **Path / body positionals**, in the function-signature order
       (``op.positional``) — e.g. ``add_dependencies_for_task(self, body,
       task_gid)`` renders ``--body`` then ``--task``.
    2. **Opts**, in the SDK docstring's ``:param`` order. python-asana never
       marks an opt ``(required)`` — required inputs are always positionals —
       so there is no required/optional split to order by.
    3. **Boilerplate per-call kwargs** (the SDK ``all_params``), plus the
       upload / deprecation extensions where they apply.
    """
    # If the SDK method has no ``opts`` parameter, docstring-derived named
    # arguments cannot be forwarded — they would be silently dropped at call
    # time. python-asana 5.x does not produce this combination today.
    assert op.has_opts or not op.opts_params, (
        f"{api_cls.__name__}.{op.method_name}: docstring declares params but "
        f"the method has no `opts` argument; CLI options would be dropped"
    )
    paginatable = op.paginatable
    does_upload = op.does_upload
    # Built-in flags this command occupies. An SDK arg/opt whose flag collides
    # with one of these is exposed as ``--sdk-<name>`` (see ``_decls``) so the
    # built-in keeps its bare name; the SDK param stays reachable + labelled.
    reserved = _reserved_flags(op)

    options: list[click.Option] = []

    # Tier 1 — path / body positionals in function-signature order. Each
    # positional renders by kind: body, the unified workspace, or a plain path
    # arg. ``--workspace`` is rendered here only when workspace is positional
    # (required); the optional workspace opt renders in the opts tier below.
    for name in op.positional:
        if name == "body":
            options.append(_body_option(op, reserved))
        elif _is_workspace_param(name):
            options.append(_workspace_option(op, reserved))
        else:
            options.append(_path_arg_option(name, op, reserved))

    # Tier 2 — opts in the SDK docstring's ``:param`` order (``op.opts_params``
    # preserves it). python-asana never marks an opt ``(required)``, so there is
    # no required/optional split to sort by. ``introspect_to_manifest`` keeps a
    # name-sorted canonical order instead — see the note there.
    for p in op.opts_params:
        if _is_workspace_param(p.name):
            options.append(_workspace_option(op, reserved))
        else:
            options.append(_plain_opt_option(p, api_cls, op, reserved))

    # Tier 3 — boilerplate per-call kwargs (the SDK ``all_params``), common to
    # every command. ``--page-limit`` / ``--return-page-iterator`` stay global
    # (they are ``Configuration`` properties). See ``_make_per_call_kwarg_options``.
    options.extend(_make_per_call_kwarg_options())

    # ``--multibyte-filenames`` is an asana-api extension that toggles the
    # multipart filename patch (``MultibyteFilenameSupport`` in session.py). It
    # only affects multipart uploads, so it is exposed solely on upload commands
    # (``does_upload``) rather than as a global flag. Off by default to preserve
    # strict SDK parity (the SDK emits ``filename=`` only); see sdk-deviations.md.
    if does_upload:
        options.append(
            click.Option(
                ["--multibyte-filenames", "multibyte_filenames"],
                is_flag=True,
                default=False,
                help=(
                    "Emit RFC 5987 filename*=UTF-8'' on this multipart upload. "
                    "Required when the --file name contains non-ASCII characters; "
                    "off by default to match the underlying SDK behavior. "
                    f"{_sdk_dest('extension')}"
                ),
            )
        )

    # Deprecated aliases remain per-command (gated by ``paginatable``) until
    # they are removed. Each emits a stderr warning at runtime and forwards to
    # its v3 replacement. The option ``name`` (``all_items`` / ``page_size`` /
    # ``max_items``) is what ``_DEPRECATED_OPTION_NAMES`` matches on.
    if paginatable:
        options.append(
            click.Option(
                ["--all-items", "all_items"],
                is_flag=True,
                default=False,
                help=f"No-op; walking every page is now the default. {_sdk_dest('extension')}",
            )
        )
        options.append(
            click.Option(
                ["--page-size", "page_size"],
                type=int,
                default=None,
                help=f"Alias for --limit. {_sdk_dest('extension')}",
            )
        )
        options.append(
            click.Option(
                ["--max-items", "max_items"],
                type=int,
                default=None,
                help=f"Alias for --item-limit. {_sdk_dest('extension')}",
            )
        )

    def inner_callback(**kwargs: Any) -> Any:
        # Common per-call kwargs (rendered as per-command options above): pop
        # them into locals so they do not fall through into the opts dict, then
        # forward to the SDK call below.
        item_limit = kwargs.pop("item_limit", None)
        full_payload = kwargs.pop("full_payload", False)
        header_params = kwargs.pop("header_params", None)
        request_timeout = kwargs.pop("request_timeout", None)

        # Per-command extension on upload commands only: pop the toggle (so it
        # does not leak into the opts dict) and set the runtime flag the session
        # reads when deciding whether to install MultibyteFilenameSupport. Other
        # commands never expose it, so their runtime value stays the default.
        if does_upload:
            runtime.multibyte_filenames = kwargs.pop("multibyte_filenames", False)

        # Deprecated aliases: pop from kwargs (per-command) and warn. Effective
        # values fold into local vars without mutating ``runtime``, so the
        # dispatch state stays scoped to this invocation.
        all_items = kwargs.pop("all_items", False) if paginatable else False
        page_size = kwargs.pop("page_size", None) if paginatable else None
        max_items = kwargs.pop("max_items", None) if paginatable else None

        effective_item_limit = item_limit

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
            # Canonical --limit wins when both are given.
            if kwargs.get("limit") is None:
                kwargs["limit"] = page_size
        if max_items is not None:
            click.echo(
                "warning: --max-items is deprecated; use --item-limit "
                "instead (will be removed in a future release)",
                err=True,
            )
            # Canonical --item-limit wins when both are given.
            if effective_item_limit is None:
                effective_item_limit = max_items

        if op.has_body:
            body_value = kwargs.pop("body")  # click marks --body as required
            parsed_body = resolve_body(body_value)
        else:
            parsed_body = None

        if op.has_workspace:
            workspace_value = kwargs.pop("workspace", None)
            resolved_workspace = resolve_workspace(workspace_value, required=op.workspace_required)
        else:
            resolved_workspace = None

        with AsanaSession.from_env() as session:
            api = api_cls(session.client)

            # Non-workspace opts pop straight into the opts dict; the workspace
            # opt is resolved separately (env-var fallback) and added after.
            opts: dict[str, Any] = {}
            for p in op.opts_params:
                if _is_workspace_param(p.name):
                    continue
                value = kwargs.pop(p.name, None)
                if p.required or value is not None:
                    opts[p.name] = value
            workspace_opt = op.workspace_opt
            if workspace_opt is not None and resolved_workspace is not None:
                opts[workspace_opt.name] = resolved_workspace

            # Build positional call args in function-signature order. Body is
            # always the first positional in python-asana (e.g.
            # ``add_followers_for_task(body, task_gid, opts)``).
            call_args: list[Any] = []
            if op.has_body:
                call_args.append(parsed_body)
            for name in op.path_positionals:
                if _is_workspace_param(name):
                    call_args.append(resolved_workspace)
                else:
                    call_args.append(kwargs.pop(_option_name(name)))

            method = getattr(api, op.method_name)
            # Forward the common per-call kwargs uniformly. The SDK accepts
            # ``item_limit`` / ``full_payload`` / ``header_params`` /
            # ``_request_timeout`` on every method (its ``all_params``), so we
            # pass them without per-method gating; a method that does not act on
            # a given kwarg simply ignores it. ``_request_timeout`` propagates
            # to every page request through the SDK ``PageIterator``.
            method_kwargs: dict[str, Any] = {}
            if effective_item_limit is not None:
                method_kwargs["item_limit"] = effective_item_limit
            if full_payload:
                method_kwargs["full_payload"] = True
            if header_params is not None:
                method_kwargs["header_params"] = header_params
            if request_timeout is not None:
                method_kwargs["_request_timeout"] = request_timeout
            # ``method_kwargs`` (the common per-call kwargs) is forwarded in
            # both branches: methods without an ``opts`` parameter still accept
            # the boilerplate ``**kwargs`` (their ``all_params``), so dropping
            # them here would silently no-op --request-timeout / --header-params
            # / --item-limit / --full-payload on every no-opts endpoint.
            result = (
                method(*call_args, opts, **method_kwargs)
                if op.has_opts
                else method(*call_args, **method_kwargs)
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

    # ``formatted`` adds the output-formatting options (--output / --query /
    # --csv-bom and their error-path twins --exception-output / --exception-query)
    # via click.option decorators; pull those Option instances out of the
    # wrapped callback (in natural order) and append them to our options list.
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
            "require JSON. (Configuration: retry_strategy)"
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
    help="Override API base URL (default: https://app.asana.com/api/1.0). (Configuration: host)",
)
@click.option("--proxy", default=None, help="HTTP/HTTPS proxy URL. (Configuration: proxy)")
@click.option(
    "--verify-ssl/--no-verify-ssl",
    "verify_ssl",
    default=None,
    help=(
        "Verify TLS certificates (default: True). Pass --no-verify-ssl "
        "to disable (insecure). (Configuration: verify_ssl)"
    ),
)
@click.option(
    "--ssl-ca-cert",
    "ssl_ca_cert",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a PEM bundle of trusted CA certs. (Configuration: ssl_ca_cert)",
)
@click.option(
    "--cert-file",
    "cert_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Client TLS certificate for mTLS. (Configuration: cert_file)",
)
@click.option(
    "--key-file",
    "key_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Client TLS private key for mTLS. (Configuration: key_file)",
)
@click.option(
    "--assert-hostname/--no-assert-hostname",
    "assert_hostname",
    default=None,
    help=(
        "Verify the server certificate's hostname matches the request URL "
        "host. Tri-state: unspecified → urllib3 default. "
        "(Configuration: assert_hostname)"
    ),
)
@_retry_strategy_option
@click.option(
    "--connection-pool-maxsize",
    "connection_pool_maxsize",
    type=click.IntRange(min=1),
    default=None,
    help=(
        "Max urllib3 connections cached per host (default: cpu_count "
        "* 5). (Configuration: connection_pool_maxsize)"
    ),
)
@click.option(
    "--access-token",
    "access_token",
    default=None,
    help=(
        "Asana personal access token (default: $ASANA_ACCESS_TOKEN). (Configuration: access_token)"
    ),
)
@click.option(
    "--temp-folder-path",
    "temp_folder_path",
    default=None,
    type=click.Path(file_okay=False),
    help="Directory for temporary downloads. (Configuration: temp_folder_path)",
)
@click.option(
    "--safe-chars-for-path-param",
    "safe_chars_for_path_param",
    default=None,
    help=(
        "Extra chars treated as safe when percent-encoding path "
        "parameters. (Configuration: safe_chars_for_path_param)"
    ),
)
@click.option(
    "--logger-format",
    "logger_format",
    default=None,
    help="Python logging format string. (Configuration: logger_format)",
)
@click.option(
    "--logger-file",
    "logger_file",
    default=None,
    type=click.Path(dir_okay=False),
    help="Path SDK loggers write to. (Configuration: logger_file)",
)
@click.option(
    "--debug",
    is_flag=True,
    default=False,
    help="Print HTTP request/response to stderr for troubleshooting. (Configuration: debug)",
)
@click.option(
    "--return-page-iterator/--no-return-page-iterator",
    "return_page_iterator",
    default=None,
    help=(
        "Toggle the SDK page iterator (default: enabled). With "
        "--no-return-page-iterator, paginatable endpoints return a "
        "single {data, next_page} dict from one HTTP call instead of "
        "auto-walking every page. (Configuration: return_page_iterator)"
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
        "are set. (Configuration: page_limit)"
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
    connection_pool_maxsize: int | None = None,
    access_token: str | None = None,
    temp_folder_path: str | None = None,
    safe_chars_for_path_param: str | None = None,
    logger_format: str | None = None,
    logger_file: str | None = None,
    debug: bool = False,
    return_page_iterator: bool | None = None,
    page_limit: int | None = None,
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
    runtime.connection_pool_maxsize = connection_pool_maxsize
    if access_token:
        runtime.access_token = access_token
    runtime.temp_folder_path = temp_folder_path
    runtime.safe_chars_for_path_param = safe_chars_for_path_param
    runtime.logger_format = logger_format
    runtime.logger_file = logger_file
    runtime.debug = debug
    if return_page_iterator is not None:
        runtime.return_page_iterator = return_page_iterator
    runtime.page_limit = page_limit


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
            # Canonical (name-sorted) order for a stable drift snapshot. This
            # intentionally differs from the help display order (docstring
            # order, see ``_make_command``): the manifest's job is to detect
            # added / removed / renamed / retyped params, so a canonical order
            # keeps the fixture from churning when an SDK docstring merely
            # reshuffles its ``:param`` lines.
            opts_params = sorted(op.opts_params, key=lambda p: p.name)
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
