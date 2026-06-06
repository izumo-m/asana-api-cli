"""Pin the SDK's uniform inputs so a python-asana bump can't slip a new one past us.

The CLI derives two SDK-uniform input families from introspection:

* the boilerplate ``**kwargs`` every generated method accepts — its
  ``all_params`` list. The user-facing four (``item_limit`` / ``full_payload``
  / ``header_params`` / ``_request_timeout``) become common per-command
  options; ``async_req`` / ``_return_http_data_only`` / ``_preload_content``
  stay SDK-internal;
* the settable ``asana.Configuration`` properties, which become global flags
  (minus object-/callable-typed members and the inert auth fields — see
  ``docs/sdk-deviations.md``).

Both families are deliberately **outside** ``tests/fixtures/cli_surface.json``
(see ``test_cli_surface.py``: synthetic / global options are intentionally not
in the manifest). So a bump that adds a new boilerplate kwarg or Configuration
property would otherwise go unnoticed. These two tests pin both sets so such a
bump fails loudly and forces a conscious classification — a global flag
(Configuration) or a common per-command ``(kwargs: ...)`` option, with the
matching SDK-destination label (see ``docs/cli-sdk-mapping.md``).

A third guard pins which methods perform a multipart file upload. The CLI
exposes the ``--multibyte-filenames`` extension flag only on upload commands,
detected at runtime by a cheap proxy (an op declaring a ``file`` opt — see
``_Operation.does_upload``). ``test_upload_detection_matches_multipart_population``
proves that proxy equals the true signal (a source scan for assignment into
``local_var_files``), so a future SDK that adds or renames an upload endpoint
fails here rather than silently dropping the flag from a command that needs it.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Iterator
from typing import Any

import asana

from asana_api_cli.cli import _enumerate_api_classes, _operations_for

# Identical across all ``*_with_http_info`` methods in python-asana 5.2.4.
EXPECTED_ALL_PARAMS: frozenset[str] = frozenset(
    {
        "async_req",
        "header_params",
        "_return_http_data_only",
        "_preload_content",
        "_request_timeout",
        "full_payload",
        "item_limit",
    }
)

# Settable attributes the CLI knows about on a default ``asana.Configuration()``.
# Not every one is a CLI flag: object-/callable-typed members (``logger*``,
# ``refresh_api_key_hook``) cannot be flags, and the inert auth fields
# (``username`` / ``password`` / ``api_key`` / ``api_key_prefix``) are
# deliberately NOT exposed — Asana auth is Bearer-token only, so they are
# swagger-codegen dead weight (see docs/sdk-deviations.md). They remain in this
# set because the SDK's Configuration still declares them; this guard tracks the
# SDK surface, not the CLI's. ``logger_format`` / ``logger_file`` reach the
# ``logger*`` members via @property setters.
#
# This is the union across the supported asana range (>=5.0.2), pinned at the
# latest. ``test_no_unknown_configuration_settable_attrs`` checks only one
# direction — that the installed SDK exposes nothing OUTSIDE this set, since a
# *new* attribute is the actionable drift (it likely needs a flag). Older SDKs
# initialize fewer attributes (``access_token`` arrived in 5.0.6,
# ``retry_strategy`` in 5.1.0); those absences are tolerated so the guard passes
# on the floor as well as the pinned version.
EXPECTED_CONFIGURATION_ATTRS: frozenset[str] = frozenset(
    {
        "access_token",
        "api_key",
        "api_key_prefix",
        "assert_hostname",
        "cert_file",
        "connection_pool_maxsize",
        "host",
        "key_file",
        "logger",
        "logger_file_handler",
        "logger_formatter",
        "logger_stream_handler",
        "page_limit",
        "password",
        "proxy",
        "refresh_api_key_hook",
        "retry_strategy",
        "return_page_iterator",
        "safe_chars_for_path_param",
        "ssl_ca_cert",
        "temp_folder_path",
        "username",
        "verify_ssl",
    }
)

# The one python-asana method that performs a multipart file upload — i.e. it
# assigns into ``local_var_files`` (every other method has only the empty
# ``local_var_files = {}`` boilerplate). The CLI exposes ``--multibyte-filenames``
# only on upload commands, detected at runtime by a cheap proxy (an op declaring
# a ``file`` opt; see ``_Operation.does_upload``). This anchor plus the
# proxy-equality check below pin that proxy to the true multipart signal.
EXPECTED_MULTIPART_UPLOAD_METHODS: frozenset[tuple[str, str]] = frozenset(
    {("AttachmentsApi", "create_attachment_for_object")}
)

# The python-asana methods whose response is an array — they return a lazy
# ``PageIterator`` / ``EventIterator`` (built in the ``*_with_http_info`` source)
# under the default flags, which the CLI materializes with ``list(...)``. The
# runtime detects them via a cheap proxy (the ``:return:`` type ends in
# ``Array``; see ``_Operation.returns_iterator``). This anchor plus the
# proxy-equality check below pin that proxy to the true iterator-construction
# signal so an SDK bump that adds, removes, or reshapes an array endpoint fails
# loudly. (73 methods in python-asana 5.2.4.)
EXPECTED_ITERATOR_RETURNING_METHODS: frozenset[tuple[str, str]] = frozenset(
    {
        ("AccessRequestsApi", "get_access_requests"),
        ("AllocationsApi", "get_allocations"),
        ("AttachmentsApi", "get_attachments_for_object"),
        ("AuditLogAPIApi", "get_audit_log_events"),
        ("BatchAPIApi", "create_batch_request"),
        ("BudgetsApi", "get_budgets"),
        ("CustomFieldSettingsApi", "get_custom_field_settings_for_goal"),
        ("CustomFieldSettingsApi", "get_custom_field_settings_for_portfolio"),
        ("CustomFieldSettingsApi", "get_custom_field_settings_for_project"),
        ("CustomFieldSettingsApi", "get_custom_field_settings_for_team"),
        ("CustomFieldsApi", "get_custom_fields_for_workspace"),
        ("CustomTypesApi", "get_custom_types"),
        ("EventsApi", "get_events"),
        ("GoalRelationshipsApi", "get_goal_relationships"),
        ("GoalsApi", "get_goals"),
        ("GoalsApi", "get_parent_goals_for_goal"),
        ("MembershipsApi", "get_memberships"),
        ("PortfolioMembershipsApi", "get_portfolio_memberships"),
        ("PortfolioMembershipsApi", "get_portfolio_memberships_for_portfolio"),
        ("PortfoliosApi", "get_items_for_portfolio"),
        ("PortfoliosApi", "get_portfolios"),
        ("ProjectMembershipsApi", "get_project_memberships_for_project"),
        ("ProjectPortfolioSettingsApi", "get_project_portfolio_settings_for_portfolio"),
        ("ProjectPortfolioSettingsApi", "get_project_portfolio_settings_for_project"),
        ("ProjectStatusesApi", "get_project_statuses_for_project"),
        ("ProjectTemplatesApi", "get_project_templates"),
        ("ProjectTemplatesApi", "get_project_templates_for_team"),
        ("ProjectsApi", "get_projects"),
        ("ProjectsApi", "get_projects_for_task"),
        ("ProjectsApi", "get_projects_for_team"),
        ("ProjectsApi", "get_projects_for_workspace"),
        ("ProjectsApi", "search_projects_for_workspace"),
        ("RatesApi", "get_rates"),
        ("ReactionsApi", "get_reactions_on_object"),
        ("RolesApi", "get_roles"),
        ("SectionsApi", "get_sections_for_project"),
        ("StatusUpdatesApi", "get_statuses_for_object"),
        ("StoriesApi", "get_stories_for_goal"),
        ("StoriesApi", "get_stories_for_task"),
        ("TagsApi", "get_tags"),
        ("TagsApi", "get_tags_for_task"),
        ("TagsApi", "get_tags_for_workspace"),
        ("TaskTemplatesApi", "get_task_templates"),
        ("TasksApi", "get_dependencies_for_task"),
        ("TasksApi", "get_dependents_for_task"),
        ("TasksApi", "get_subtasks_for_task"),
        ("TasksApi", "get_tasks"),
        ("TasksApi", "get_tasks_for_project"),
        ("TasksApi", "get_tasks_for_section"),
        ("TasksApi", "get_tasks_for_tag"),
        ("TasksApi", "get_tasks_for_user_task_list"),
        ("TasksApi", "search_tasks_for_workspace"),
        ("TeamMembershipsApi", "get_team_memberships"),
        ("TeamMembershipsApi", "get_team_memberships_for_team"),
        ("TeamMembershipsApi", "get_team_memberships_for_user"),
        ("TeamsApi", "get_teams_for_user"),
        ("TeamsApi", "get_teams_for_workspace"),
        ("TimePeriodsApi", "get_time_periods"),
        ("TimeTrackingCategoriesApi", "get_time_tracking_categories"),
        ("TimeTrackingCategoriesApi", "get_time_tracking_entries_for_time_tracking_category"),
        ("TimeTrackingEntriesApi", "get_time_tracking_entries"),
        ("TimeTrackingEntriesApi", "get_time_tracking_entries_for_task"),
        ("TimesheetApprovalStatusesApi", "get_timesheet_approval_statuses"),
        ("TypeaheadApi", "typeahead_for_workspace"),
        ("UsersApi", "get_favorites_for_user"),
        ("UsersApi", "get_users"),
        ("UsersApi", "get_users_for_team"),
        ("UsersApi", "get_users_for_workspace"),
        ("WebhooksApi", "get_webhooks"),
        ("WorkspaceMembershipsApi", "get_workspace_memberships_for_user"),
        ("WorkspaceMembershipsApi", "get_workspace_memberships_for_workspace"),
        ("WorkspacesApi", "get_workspace_events"),
        ("WorkspacesApi", "get_workspaces"),
    }
)


def _all_params_for(func: Any) -> frozenset[str]:
    """Collect the ``all_params.append('x')`` string literals from a method body.

    ``all_params`` is a local variable built at call time, so it can only be
    read from the source: parse the AST and gather every
    ``all_params.append(<str literal>)`` argument.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    names: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "append" or not isinstance(node.func.value, ast.Name):
            continue
        if node.func.value.id != "all_params" or not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            names.append(arg.value)
    return frozenset(names)


def _with_http_info_methods() -> Iterator[tuple[str, str, Any]]:
    for cls_name, cls in sorted(vars(asana).items(), key=lambda kv: kv[0]):
        if not (inspect.isclass(cls) and cls_name.endswith("Api")):
            continue
        for method_name in sorted(vars(cls)):
            if method_name.endswith("_with_http_info"):
                yield cls_name, method_name, vars(cls)[method_name]


def _methods_populating_local_var_files() -> set[tuple[str, str]]:
    """Methods whose ``*_with_http_info`` source writes into ``local_var_files``.

    A subscript write — ``local_var_files['file'] = ...`` (assignment) or an
    augmented assignment — is the true "this method sends multipart/form-data
    with a file" signal, as opposed to the universal ``local_var_files = {}``
    initializer (a plain ``Name`` target, not a subscript). Returns
    ``(ApiClassName, public_method_name)`` pairs.
    """
    suffix = "_with_http_info"
    out: set[tuple[str, str]] = set()
    for cls_name, method_name, func in _with_http_info_methods():
        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            else:
                continue
            for target in targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "local_var_files"
                ):
                    out.add((cls_name, method_name[: -len(suffix)]))
    return out


def _upload_ops_via_does_upload() -> set[tuple[str, str]]:
    """Methods the runtime classifies as uploads via ``_Operation.does_upload``."""
    out: set[tuple[str, str]] = set()
    for cls in _enumerate_api_classes():
        for op in _operations_for(cls):
            if op.does_upload:
                out.add((cls.__name__, op.method_name))
    return out


# The constructors the SDK calls in the ``return_page_iterator`` branch of an
# array-response endpoint. The method returns their ``.items()`` generator, so
# the runtime materialization gate sees an iterator.
_ITERATOR_CTORS = {"PageIterator", "EventIterator"}


def _methods_constructing_iterator() -> set[tuple[str, str]]:
    """Methods whose ``*_with_http_info`` source constructs a lazy iterator.

    Constructing a ``PageIterator`` / ``EventIterator`` (the array-response
    branch) is the ground-truth source signal that the endpoint returns a lazy
    iterator — the method returns the constructor's ``.items()`` generator, which
    the runtime ``isinstance(result, Iterator)`` gate materializes. Detected by an
    AST scan for a call to either constructor (a bare ``Name``, as the SDK imports
    them). Returns ``(ApiClassName, public_method_name)`` pairs.
    """
    suffix = "_with_http_info"
    out: set[tuple[str, str]] = set()
    for cls_name, method_name, func in _with_http_info_methods():
        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in _ITERATOR_CTORS
            ):
                out.add((cls_name, method_name[: -len(suffix)]))
    return out


def _iterator_ops_via_returns_iterator() -> set[tuple[str, str]]:
    """Methods the runtime classifies as iterator-returning via
    ``_Operation.returns_iterator``."""
    out: set[tuple[str, str]] = set()
    for cls in _enumerate_api_classes():
        for op in _operations_for(cls):
            if op.returns_iterator:
                out.add((cls.__name__, op.method_name))
    return out


def test_all_methods_share_expected_all_params() -> None:
    offenders: dict[str, list[str]] = {}
    count = 0
    for cls_name, method_name, func in _with_http_info_methods():
        count += 1
        got = _all_params_for(func)
        if got != EXPECTED_ALL_PARAMS:
            offenders[f"{cls_name}.{method_name}"] = sorted(got ^ EXPECTED_ALL_PARAMS)

    assert count > 0, "no *_with_http_info methods found — SDK introspection broke"
    assert not offenders, (
        "all_params drifted from EXPECTED_ALL_PARAMS (symmetric diff per method):\n"
        + "\n".join(f"  {name}: {diff}" for name, diff in offenders.items())
        + "\nA new boilerplate kwarg likely needs a common per-command option "
        "in _make_command with a (kwargs: <name>) label — not a global flag "
        "(or removal handling). See docs/cli-sdk-mapping.md."
    )


def test_no_unknown_configuration_settable_attrs() -> None:
    got = frozenset(k for k in vars(asana.Configuration()) if not k.startswith("_"))
    unknown = got - EXPECTED_CONFIGURATION_ATTRS
    assert not unknown, (
        "asana.Configuration exposes settable attributes the CLI does not know "
        f"about: {sorted(unknown)}\n"
        "A newly settable property likely needs a global flag + "
        "(Configuration: <name>) label in cli.py / click_ext.py, then adding it "
        "to EXPECTED_CONFIGURATION_ATTRS. See docs/cli-sdk-mapping.md."
    )


def test_upload_detection_matches_multipart_population() -> None:
    multipart = _methods_populating_local_var_files()
    assert multipart, "no method populates local_var_files — SDK introspection broke"
    assert multipart == EXPECTED_MULTIPART_UPLOAD_METHODS, (
        "the set of multipart-upload methods drifted:\n"
        f"  added:   {sorted(multipart - EXPECTED_MULTIPART_UPLOAD_METHODS)}\n"
        f"  removed: {sorted(EXPECTED_MULTIPART_UPLOAD_METHODS - multipart)}\n"
        "A new/renamed upload endpoint must get the per-command "
        "--multibyte-filenames flag (cli.py:_make_command, gated by "
        "_Operation.does_upload). See docs/sdk-deviations.md."
    )
    via_proxy = _upload_ops_via_does_upload()
    assert via_proxy == multipart, (
        "_Operation.does_upload (the 'has a file opt' proxy) no longer matches "
        "the methods that actually populate local_var_files:\n"
        f"  proxy-only:     {sorted(via_proxy - multipart)}\n"
        f"  multipart-only: {sorted(multipart - via_proxy)}\n"
        "Update the does_upload predicate in cli.py so --multibyte-filenames "
        "lands on exactly the upload commands."
    )


def test_iterator_detection_matches_source_construction() -> None:
    constructed = _methods_constructing_iterator()
    assert constructed, (
        "no method constructs a PageIterator/EventIterator — SDK introspection broke"
    )
    assert constructed == EXPECTED_ITERATOR_RETURNING_METHODS, (
        "the set of iterator-returning methods drifted:\n"
        f"  added:   {sorted(constructed - EXPECTED_ITERATOR_RETURNING_METHODS)}\n"
        f"  removed: {sorted(EXPECTED_ITERATOR_RETURNING_METHODS - constructed)}\n"
        "These are exactly the calls execute_call_plan materializes with "
        "list(...); review the change and regenerate the set. See "
        "_Operation.returns_iterator."
    )
    via_proxy = _iterator_ops_via_returns_iterator()
    assert via_proxy == constructed, (
        "_Operation.returns_iterator (the ':return: *Array' proxy) no longer "
        "matches the methods that actually construct an iterator:\n"
        f"  proxy-only:  {sorted(via_proxy - constructed)}\n"
        f"  source-only: {sorted(constructed - via_proxy)}\n"
        "Update the returns_iterator predicate in cli.py."
    )


def test_pagination_items_are_generators() -> None:
    # The runtime gate (cli.py:execute_call_plan) materializes a result with
    # ``isinstance(result, collections.abc.Iterator)`` while the session — and the
    # --debug redactor — is still open (principle #2: pages 2..N must be fetched
    # in-session). Array endpoints return ``PageIterator(...).items()`` /
    # ``EventIterator(...).items()``, and ``.items()`` is a generator function, so
    # the result is a generator — hence always an Iterator — which is what makes
    # the gate fire. Pin that: a future SDK whose ``.items()`` stopped being a
    # generator (e.g. returned a list) would silently change materialization, and
    # under --debug could leak Authorization on later pages.
    from asana.pagination import EventIterator, PageIterator

    assert inspect.isgeneratorfunction(PageIterator.items)
    assert inspect.isgeneratorfunction(EventIterator.items)
