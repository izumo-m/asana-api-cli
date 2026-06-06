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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asana
import click

from asana_api_cli.click_ext import (
    CommandWithGlobalOptions,
    GroupWithGlobalOptions,
    LazyGroup,
)
from asana_api_cli.formatter import formatted, formatter_flag_names, make_formatter_options
from asana_api_cli.multibyte_filename import MultibyteFilenameSupport
from asana_api_cli.session import (
    AsanaSession,
    runtime,
)
from asana_api_cli.structured_arg import (
    click_callback,
)
from asana_api_cli.version import version_string

# ---------------------------------------------------------------------------
# Input resolution
#
# Turn a raw CLI option value into the argument the SDK call receives, raising
# ``click.BadParameter`` (exit code 2, user-input error) on bad input. These are
# pure invocation-layer helpers — no SDK client / session involved — called only
# from the command callback below.
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
            raise click.BadParameter(
                f"stdin is not valid UTF-8: {exc}", param_hint="--body"
            ) from exc
    elif value.startswith("@"):
        path = Path(value[1:])
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise click.BadParameter(f"file not found: {path}", param_hint="--body") from exc
        except UnicodeDecodeError as exc:
            raise click.BadParameter(
                f"file {path} is not valid UTF-8: {exc}", param_hint="--body"
            ) from exc
        except OSError as exc:
            raise click.BadParameter(
                f"cannot read file {path}: {exc}", param_hint="--body"
            ) from exc
    else:
        raw = value

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.BadParameter(f"invalid JSON: {exc}", param_hint="--body") from exc


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

    If *required* is True and no value is found, raises ``click.BadParameter``.
    """
    if explicit is not None:
        return explicit
    if required:
        ws = os.environ.get(DEFAULT_WORKSPACE_ENV)
        if ws:
            return ws
        raise click.BadParameter(
            f"required (or set {DEFAULT_WORKSPACE_ENV})", param_hint="--workspace"
        )
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
# docstring is empty. The key is ``(group_class, method_name, param_name)``
# where ``group_class`` is the ``*Api`` class name with the ``Api`` suffix
# stripped (e.g. ``"Attachments"``, not ``"AttachmentsApi"`` — the lookup uses
# ``api_cls.__name__[:-3]``); bare param names (``file``, ``parent``, ``name``)
# would otherwise collide across endpoints. Sourced from Asana's developer
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
# asana-api label uses parentheses. Six kinds cover the SDK input structure:
#
#   (Configuration: <name>)  set on asana.Configuration         (global flags)
#   (ApiClient: <name>)      set on the ApiClient instance       (user_agent, set_default_header)
#   (args: <name>)           positional method argument          (body / path GID / workspace_gid)
#   (opts: <name>)           entry in the method ``opts`` dict   (docstring :param)
#   (kwargs: <name>)         boilerplate **kwargs every method accepts (all_params)
#   (asana-api: extension)   no SDK counterpart                  (CLI-only)
#
# Configuration globals and the two ApiClient-instance globals (--user-agent /
# --set-default-header) carry the literal in their single declaration in
# ``click_ext.py:_global_option_sections`` (the one source every command's
# globals are built from, so the label is identical at the root and at any
# subcommand); the CLI-only formatter flags (``--output`` / ``--query`` /
# ``--csv-bom`` and the error-path twins ``--exception-output`` /
# ``--exception-query``) live in ``formatter.py:make_formatter_options``. This helper builds every
# label ``_make_command`` derives at runtime: ``args`` / ``opts`` for path /
# body / docstring params, ``kwargs`` for the common per-call kwargs, and the
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


def _parse_return_type(doc: str) -> str | None:
    """Return the SDK ``:return:`` type token (e.g. ``TaskResponseArray``).

    python-asana's codegen emits ``:return: <Type>`` on its own line. Returns
    ``None`` when absent. Feeds :attr:`_Operation.returns_iterator`: a ``*Array``
    type marks an array-response endpoint that yields a lazy iterator.
    """
    m = re.search(r"^\s*:return:\s*(\S+)", doc, re.MULTILINE)
    return m.group(1) if m else None


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
    # colon after the name, so the regex never matches). Kept as a guard in
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
    __slots__ = (
        "command_name",
        "has_opts",
        "method_name",
        "params",
        "positional",
        "return_type",
        "summary",
    )

    def __init__(
        self,
        method_name: str,
        command_name: str,
        summary: str,
        positional: list[str],
        params: dict[str, _DocParam],
        has_opts: bool,
        return_type: str | None,
    ) -> None:
        self.method_name = method_name
        self.command_name = command_name
        self.summary = summary
        self.positional = positional
        self.params = params
        self.has_opts = has_opts
        self.return_type = return_type

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
    def returns_iterator(self) -> bool:
        """True iff the SDK method's response is an array.

        Under the default flags (``return_page_iterator`` on, ``full_payload``
        off) such a method returns a lazy ``PageIterator`` / ``EventIterator``,
        which the CLI materializes with ``list(...)`` in
        :func:`execute_call_plan`.

        Detected from the docstring ``:return:`` type ending in ``Array`` (e.g.
        ``TaskResponseArray``) — python-asana's codegen emits that exactly for
        array-response endpoints. ``tests/test_sdk_boilerplate.py`` holds this in
        lockstep with the ground-truth source signal (the ``PageIterator(`` /
        ``EventIterator(`` construction in the ``*_with_http_info`` sibling), so
        a future SDK that breaks the correspondence trips that guard.

        Distinct from ``paginatable`` (which keys off a ``limit`` query param):
        ``events`` returns an iterator yet declares no ``limit``.
        """
        return self.return_type is not None and self.return_type.endswith("Array")

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
        """Workspace is required when it is a path positional.

        An ``opts`` workspace is always optional — no python-asana method marks
        a query param ``(required)`` — so the return's second branch
        (``wo.required``) is a defensive guard that does not fire on today's SDK.
        Drives the ``ASANA_DEFAULT_WORKSPACE`` env-var fallback: auto-fill only
        when required.
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
        return_type=_parse_return_type(doc),
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
    """All asana-api own flags (no SDK counterpart). An SDK arg/opt whose
    derived flag collides with one is exposed as ``--sdk-<name>``
    (:func:`_decls`) so the built-in keeps its bare name. ``--help`` /
    ``--version`` are listed explicitly because neither appears in a leaf's
    params.
    """
    flags: set[str] = {"--help", "--version"}
    flags |= formatter_flag_names()
    # pagination aliases + upload toggle; keep in sync with _make_command
    flags |= {"--all-items", "--page-size", "--max-items", "--multibyte-filenames"}
    return frozenset(flags)


def _decls(flag: str, dest: str, reserved: frozenset[str]) -> list[str]:
    """Declaration list for an SDK-derived option, ``sdk-`` prefixed on collision.

    If ``flag`` collides with a built-in CLI flag (in ``reserved``), the SDK
    param yields: it is exposed as ``--sdk-<name>`` with an *explicit* ``dest``
    equal to the SDK param name, so the call path (which pops by param name) is
    unchanged and the ``(opts/args: <name>)`` help label still shows the real
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


def _apply_deprecated_aliases(kwargs: dict[str, Any], item_limit: int | None) -> int | None:
    """Resolve the deprecated pagination aliases and return the effective item
    limit. Pops ``all_items`` / ``page_size`` / ``max_items`` from ``kwargs``
    (absent on non-paginatable commands), warns on stderr, and folds
    ``page_size`` into ``kwargs["limit"]`` / ``max_items`` into the item limit,
    with the canonical ``--limit`` / ``--item-limit`` winning when both are given.
    """
    all_items = kwargs.pop("all_items", False)
    page_size = kwargs.pop("page_size", None)
    max_items = kwargs.pop("max_items", None)

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
        if item_limit is None:
            item_limit = max_items
    return item_limit


def _multibyte_filenames_callback(ctx: click.Context, param: click.Parameter, value: bool) -> None:
    """``--multibyte-filenames`` callback: install the RFC 5987 multipart patch
    for this command and uninstall it at context teardown. ``with_resource``
    enters the context manager now (install) and exits it when the command
    finishes (uninstall), so the patch is scoped to this one invocation.
    """
    if value:
        ctx.with_resource(MultibyteFilenameSupport())


@dataclass
class CallPlan:
    """A fully collected SDK method invocation, independent of any session.

    :func:`build_call_plan` produces it from the parsed CLI ``kwargs`` without
    opening a session or needing a token; :func:`execute_call_plan` consumes it
    to perform the call. The split is at the session boundary: everything that
    needs no client — argument collection plus the session-free ``--body`` and
    workspace resolution — happens in :func:`build_call_plan`, while the API
    instantiation, the call itself, and lazy-iterator materialization (all of
    which need the session's client) happen in :func:`execute_call_plan`. This
    keeps the session — and the ``--debug`` redactor — scoped to the actual HTTP
    work.

    ``body`` is the already-resolved body value (``@file`` / ``-`` / JSON literal
    parsed by :func:`resolve_body`); ``has_body`` distinguishes "no body
    positional" from a body that legitimately parsed to ``None`` (``--body
    null``). ``api_cls`` is the class, not an instance — instantiation needs the
    session's client and happens in :func:`execute_call_plan`.
    """

    api_cls: type
    method_name: str
    has_opts: bool
    has_body: bool
    body: JsonValue
    path_call_args: list[Any]
    opts: dict[str, Any]
    method_kwargs: dict[str, Any]
    # Static prediction of whether the SDK returns a lazy iterator for this
    # endpoint (``*Array`` response → ``PageIterator`` / ``EventIterator``). The
    # generate path needs it to decide ``result = list(...)`` vs ``result = ...``
    # without calling the SDK; ``execute_call_plan`` instead post-judges the live
    # result via ``isinstance``. Gated further at render time by
    # ``return_page_iterator`` / ``full_payload`` (see ``codegen``).
    returns_iterator: bool


def build_call_plan(op: _Operation, api_cls: type, kwargs: dict[str, Any]) -> CallPlan:
    """Collect everything needed to call ``op`` from the parsed ``kwargs``.

    Session-free: needs no client and no token. It does read local input
    (``@file`` / stdin for ``--body``, the ``ASANA_DEFAULT_WORKSPACE`` env var),
    but nothing that requires the SDK client — that is what makes it separable
    from :func:`execute_call_plan`. Consumes ``kwargs`` by popping each value,
    mirroring the original single-pass collection.
    """
    # Common per-call kwargs (rendered as per-command options): pop into locals
    # so they do not fall through into the opts dict.
    item_limit = kwargs.pop("item_limit", None)
    full_payload = kwargs.pop("full_payload", False)
    header_params = kwargs.pop("header_params", None)
    request_timeout = kwargs.pop("request_timeout", None)

    # Deprecated aliases (--all-items / --page-size / --max-items): warn and fold
    # into their canonical replacements.
    effective_item_limit = _apply_deprecated_aliases(kwargs, item_limit)

    # Resolve the body (``@file`` / stdin / JSON literal) before workspace, the
    # same order as the original closure. Both reads are session-free, so they
    # belong on the collection side of the split. ``--body`` is click-required,
    # so the pop always yields a value when ``has_body``.
    body = resolve_body(kwargs.pop("body")) if op.has_body else None

    # Workspace resolution is session-free (an explicit value or the
    # ``ASANA_DEFAULT_WORKSPACE`` env-var fallback — no client, no token), so it
    # belongs here. ``resolve_workspace(required=True)`` can still raise when a
    # required workspace is omitted and the env var is unset; it ran here, after
    # body resolution, in the original closure too, so the order is preserved.
    if op.has_workspace:
        resolved_workspace = resolve_workspace(
            kwargs.pop("workspace", None), required=op.workspace_required
        )
    else:
        resolved_workspace = None

    # Non-workspace opts pop straight into the opts dict; the workspace opt is
    # resolved separately (env-var fallback) and added after.
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

    # Path positionals in function-signature order. Body is prepended in
    # :func:`execute_call_plan` (it is always the first positional in python-asana).
    path_call_args: list[Any] = []
    for name in op.path_positionals:
        if _is_workspace_param(name):
            path_call_args.append(resolved_workspace)
        else:
            path_call_args.append(kwargs.pop(_option_name(name)))

    # Forward the common per-call kwargs uniformly. The SDK accepts
    # ``item_limit`` / ``full_payload`` / ``header_params`` / ``_request_timeout``
    # on every method (its ``all_params``), so a method that does not act on a
    # given kwarg simply ignores it. ``_request_timeout`` propagates to every
    # page request through the SDK ``PageIterator``.
    method_kwargs: dict[str, Any] = {}
    if effective_item_limit is not None:
        method_kwargs["item_limit"] = effective_item_limit
    if full_payload:
        method_kwargs["full_payload"] = True
    if header_params is not None:
        method_kwargs["header_params"] = header_params
    if request_timeout is not None:
        method_kwargs["_request_timeout"] = request_timeout

    return CallPlan(
        api_cls=api_cls,
        method_name=op.method_name,
        has_opts=op.has_opts,
        has_body=op.has_body,
        body=body,
        path_call_args=path_call_args,
        opts=opts,
        method_kwargs=method_kwargs,
        returns_iterator=op.returns_iterator,
    )


def execute_call_plan(plan: CallPlan) -> Any:
    """Execute a collected :class:`CallPlan` and return the SDK result.

    Opens a session, builds the API instance, invokes the method, and
    materializes a lazy iterator while the session — and the ``--debug``
    redactor — are still active. Everything session-free (argument collection,
    body / workspace resolution) already happened in :func:`build_call_plan`.
    """
    # Body is always the first positional in python-asana. ``has_body`` (not
    # ``body is not None``) is the gate, so a body that parsed to ``None``
    # (``--body null``) is still passed through.
    if plan.has_body:
        call_args: list[Any] = [plan.body, *plan.path_call_args]
    else:
        call_args = list(plan.path_call_args)

    with AsanaSession.from_env() as session:
        api = plan.api_cls(session.client)
        method = getattr(api, plan.method_name)
        # ``method_kwargs`` (the common per-call kwargs) is forwarded in both
        # branches: methods without an ``opts`` parameter still accept the
        # boilerplate ``**kwargs`` (their ``all_params``), so dropping them here
        # would silently no-op --request-timeout / --header-params / --item-limit
        # / --full-payload on every no-opts endpoint.
        result = (
            method(*call_args, plan.opts, **plan.method_kwargs)
            if plan.has_opts
            else method(*call_args, **plan.method_kwargs)
        )
        # Lazy iterator consumption inside the session context.
        #
        # Two independent layers:
        #   - Layer A (session lifecycle): every SDK call runs inside
        #     ``with AsanaSession.from_env() as session:``, which keeps the
        #     ``HttpClientAuthRedactor`` installed when ``--debug`` is active.
        #   - Layer B (this block): when the SDK returns a lazy iterator
        #     (PageIterator / EventIterator), iterating it issues one HTTP
        #     request per page. We must consume the iterator *before* leaving
        #     Layer A's ``with`` block — otherwise pages 2..N are fetched after
        #     the redactor is uninstalled and leak ``Authorization`` into the
        #     ``--debug`` log.
        #
        # post-judge by return value type: any ``Iterator`` is consumed here
        # regardless of which endpoint produced it.
        if isinstance(result, collections.abc.Iterator):
            result = list(result)
        return result


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
    4. **Output-formatting options** (``--output`` / ``--query`` / ``--csv-bom``
       / ``--exception-output`` / ``--exception-query``) from
       ``make_formatter_options``, appended last.
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
    # asana-api's own flags (reserved uniformly across commands). An SDK arg/opt
    # whose flag collides with one is exposed as ``--sdk-<name>`` (see
    # ``_decls``) so the own flag keeps its bare name; the SDK param stays
    # reachable + labelled.
    reserved = _static_reserved_flags()

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
    # multipart filename patch (``MultibyteFilenameSupport`` in
    # ``multibyte_filename.py``). It only affects multipart uploads, so it is
    # exposed solely on upload commands (``does_upload``) rather than as a global
    # flag. Off by default to preserve strict SDK parity (the SDK emits
    # ``filename=`` only); see sdk-deviations.md.
    # Its callback installs the patch and scopes it to this command via
    # ``ctx.with_resource``; ``expose_value=False`` keeps it out of
    # ``inner_callback``'s ``**kwargs``.
    if does_upload:
        options.append(
            click.Option(
                ["--multibyte-filenames", "multibyte_filenames"],
                is_flag=True,
                default=False,
                callback=_multibyte_filenames_callback,
                expose_value=False,
                help=(
                    "Emit RFC 5987 filename*=UTF-8'' on this multipart upload. "
                    "Required when the --file name contains non-ASCII characters; "
                    "off by default to match the underlying SDK behavior. "
                    "Not yet reproduced by --generate-python (coming in a later "
                    f"release). {_sdk_dest('extension')}"
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
        # Collect the invocation (session-free), then either render or execute it.
        # Splitting at the session boundary (``build_call_plan`` /
        # ``execute_call_plan``) keeps the session — and the ``--debug`` redactor —
        # scoped to the HTTP work. In ``--generate-python`` mode the collected
        # plan is self-describing, so return it for ``formatted`` to render to
        # code; no session is opened and no token is needed.
        plan = build_call_plan(op, api_cls, kwargs)
        if runtime.generate_python:
            return plan
        return execute_call_plan(plan)

    callback = formatted(inner_callback)

    # The output-formatting options (--output / --query / --csv-bom and their
    # error-path twins --exception-output / --exception-query) are declared by
    # ``make_formatter_options``; ``callback`` (wrapped by ``formatted``) consumes
    # their parsed values as kwargs.
    options.extend(make_formatter_options())

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


def _version_callback(ctx: click.Context, param: click.Parameter, value: bool) -> None:
    """``--version`` callback: print the version and exit.

    A lazy stand-in for ``click.version_option``: ``version_string()`` (three
    ``importlib.metadata`` lookups) is evaluated only when ``--version`` is
    actually passed, instead of at import time as
    ``click.version_option(version_string(), ...)`` would force. The message
    matches click's default ``%(prog)s, version %(version)s`` format; ``is_eager``
    keeps the flag order-independent.
    """
    if not value or ctx.resilient_parsing:
        return
    click.echo(f"asana-api, version {version_string()}")
    ctx.exit()


# Root group. ``LazyGroup`` is a ``GroupWithGlobalOptions``, so the root
# appends and consumes the global Configuration / ApiClient flags from the
# single ``_global_option_sections`` source in ``click_ext.py`` — exactly the
# way every subgroup (``_ApiGroup``) and leaf command (``CommandWithGlobalOptions``)
# does. There is no separate root-level declaration; the flags work at any level
# of the tree, and ``--retry-strategy`` is gated on the SDK version in that one
# source.
@click.group(name="asana-api", cls=LazyGroup, epilog=_ROOT_EPILOG)
@click.option(
    "--version",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=_version_callback,
    help="Show the version and exit.",
)
def main() -> None:
    """Asana API CLI — runtime-introspected wrapper around the python-asana SDK."""
    # JSON I/O is required to be UTF-8 by RFC 8259, but on Windows the default
    # stream encodings are the locale code page (e.g. cp932 on Japanese
    # Windows): stdout/stderr raise UnicodeEncodeError when writing non-ASCII
    # data, and stdin silently misdecodes a UTF-8 ``--body -`` payload.
    # Reconfigure all three to UTF-8 so the same input/output works on every
    # platform. The hasattr guard keeps CliRunner's in-memory streams (used
    # by tests) from blowing up, since StringIO has no reconfigure().
    #
    # The global flags are parsed into the root context and written to
    # ``runtime`` by ``GroupWithGlobalOptions.invoke`` → ``_consume_global_options``
    # (inherited by ``LazyGroup``) before this callback runs, so there is nothing
    # to apply here.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]


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
