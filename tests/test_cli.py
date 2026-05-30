"""Tests for asana_api_cli.cli — runtime introspection helpers and command tree.

Verifies that the CLI built at import time from the live ``asana`` SDK has
the expected naming, docstring parsing behavior, command shape, and special
handling for body / workspace / pagination.
"""

from __future__ import annotations

import re
from collections import Counter

import click
import pytest

from asana_api_cli.cli import (
    _GROUP_DESCRIPTIONS,
    _api_class_to_group,
    _decls,
    _enumerate_api_classes,
    _escape_help,
    _extract_operation,
    _group_short_help,
    _humanize_class_name,
    _make_command,
    _method_to_command,
    _Operation,
    _operations_for,
    _parse_params,
    _parse_summary,
    _snake,
    _static_reserved_flags,
    main,
)

# ---------------------------------------------------------------------------
# Name conversion
# ---------------------------------------------------------------------------


class TestNaming:
    def test_snake_simple(self) -> None:
        assert _snake("Tasks") == "tasks"
        assert _snake("CustomFields") == "custom_fields"

    def test_snake_with_acronym(self) -> None:
        assert _snake("AuditLogAPI") == "audit_log_api"
        assert _snake("BatchAPI") == "batch_api"

    def test_api_class_to_group(self) -> None:
        assert _api_class_to_group("TasksApi") == "tasks"
        assert _api_class_to_group("AuditLogAPIApi") == "audit_log_api"
        assert _api_class_to_group("BatchAPIApi") == "batch_api"
        assert _api_class_to_group("CustomFieldsApi") == "custom_fields"

    def test_method_to_command(self) -> None:
        assert _method_to_command("get_tasks") == "get-tasks"
        assert _method_to_command("create_task") == "create-task"


class TestHumanizeClassName:
    @pytest.mark.parametrize(
        "class_name, expected",
        [
            ("Tasks", "Tasks"),
            ("Typeahead", "Typeahead"),
            ("AccessRequests", "Access requests"),
            ("OrganizationExports", "Organization exports"),
            ("TimeTrackingCategories", "Time tracking categories"),
            ("BatchAPI", "Batch API"),
            ("AuditLogAPI", "Audit log API"),
            ("HTTPSConnection", "HTTPS connection"),  # acronym ↔ word split
        ],
    )
    def test_humanize_class_name(self, class_name: str, expected: str) -> None:
        assert _humanize_class_name(class_name) == expected


class TestGroupShortHelp:
    def test_curated_entry_wins(self) -> None:
        # "Tasks" is in the curated dict.
        assert _group_short_help("Tasks") == _GROUP_DESCRIPTIONS["Tasks"]

    def test_fallback_to_humanized_when_uncurated(self) -> None:
        # Simulate a brand-new SDK group not yet in the dict.
        assert _group_short_help("FooBarBaz") == "Foo bar baz"

    def test_curated_dict_has_no_blank_or_template_entries(self) -> None:
        # Guard against the old `<Name> commands` template sneaking back in.
        for name, desc in _GROUP_DESCRIPTIONS.items():
            assert desc.strip(), f"{name} has a blank curated description"
            assert not desc.endswith(" commands"), (
                f"{name}: {desc!r} looks like the auto-generated template; "
                f"replace with a real description"
            )

    def test_curated_entries_fit_short_help_limit(self) -> None:
        # click.utils.make_default_short_help(max_length=45) renders text
        # of <=45 chars verbatim; anything longer gets truncated mid-word
        # with "…" appended. Beyond losing characters, the truncation
        # routinely drops the key noun (e.g. "organization-wide…" with
        # "exports" lost). Enforce the limit so editors notice immediately
        # if they make an entry too long.
        for name, desc in _GROUP_DESCRIPTIONS.items():
            assert len(desc) <= 45, (
                f"{name}: {len(desc)}-char description {desc!r} exceeds the "
                f"45-char short-help limit; will be truncated with '...' in "
                f"`asana-api --help`. Rewrite shorter or update the limit."
            )

    def test_every_currently_loaded_sdk_group_renders_non_empty_help(self) -> None:
        # Doesn't fail when a new SDK group is missing from the dict (that
        # path goes through the humanize fallback by design) — just checks
        # the fallback also produces something readable.
        for cls in _enumerate_api_classes():
            short_name = cls.__name__[:-3]  # strip "Api"
            help_text = _group_short_help(short_name)
            assert help_text and help_text != short_name, (
                f"{short_name} produces empty or unchanged help: {help_text!r}"
            )

    def test_group_descriptions_match_docs(self) -> None:
        """``docs/api-groups.md`` is the authoritative source for these
        descriptions; the in-code dict must mirror the table's CLI-group
        column AND its Short-description column. Catches drift when one
        file is edited without the other.
        """
        import pathlib

        from asana_api_cli.cli import _api_class_to_group

        doc = pathlib.Path(__file__).resolve().parent.parent / "docs" / "api-groups.md"
        text = doc.read_text(encoding="utf-8")
        # Match table rows: first column is `<group>` (backtick-quoted),
        # third column is the short description (between the 3rd and 4th
        # ``|`` characters). The middle column (Asana reference link)
        # contains a pipe-free link, so a non-greedy ``[^|]+`` works for
        # both columns.
        doc_rows: dict[str, str] = dict(
            re.findall(
                r"^\|\s*`([a-z][a-z0-9-]*)`\s*\|[^|]+\|\s*(.+?)\s*\|",
                text,
                re.MULTILINE,
            )
        )
        assert doc_rows, f"No CLI groups parsed from {doc} — table format change?"

        dict_rows = {
            _api_class_to_group(class_name + "Api").replace("_", "-"): desc
            for class_name, desc in _GROUP_DESCRIPTIONS.items()
        }

        # Key set must match.
        assert dict_rows.keys() == doc_rows.keys(), (
            f"_GROUP_DESCRIPTIONS vs docs/api-groups.md key mismatch:\n"
            f"  in dict but not in doc: {sorted(dict_rows.keys() - doc_rows.keys())}\n"
            f"  in doc but not in dict: {sorted(doc_rows.keys() - dict_rows.keys())}"
        )

        # Description column must match byte-for-byte for every row that
        # appears in both — a wording fix in one file that doesn't reach
        # the other is the drift this test is meant to surface.
        mismatched = {
            slug: (dict_rows[slug], doc_rows[slug])
            for slug in dict_rows
            if dict_rows[slug] != doc_rows[slug]
        }
        assert not mismatched, "\n".join(
            f"{slug}:\n  dict: {dv!r}\n  doc:  {ddv!r}" for slug, (dv, ddv) in mismatched.items()
        )


# ---------------------------------------------------------------------------
# Docstring parsing
# ---------------------------------------------------------------------------


class TestEscapeHelp:
    def test_strips_html_tags(self) -> None:
        assert _escape_help("<b>bold</b> and <i>italic</i>") == "bold and italic"

    def test_collapses_whitespace(self) -> None:
        assert _escape_help("a  b\n\nc\t\td") == "a b c d"

    def test_preserves_long_text_unchanged(self) -> None:
        # Per issue #6: no upper-bound truncation. A description from the
        # SDK that runs ~470 chars must come through intact so click can
        # wrap it onto as many lines as needed.
        long_desc = (
            "A sync token received from the last request, or none on first sync. "
            "Events will be returned from the point in time that the sync token "
            "was generated. " * 3
        )
        assert _escape_help(long_desc) == long_desc.strip()
        assert "..." not in _escape_help(long_desc)


class TestDocstringParse:
    def test_summary_strips_noqa(self) -> None:
        doc = "Get multiple tasks  # noqa: E501\n\n        <b>scope</b>..."
        assert _parse_summary(doc) == "Get multiple tasks"

    def test_param_detection(self) -> None:
        doc = """Get tasks

        :param async_req bool
        :param int limit: Results per page.
        :param str task_gid: The task to operate on. (required)
        :param list[str] opt_fields: Fields list.
        """
        params = _parse_params(doc)
        assert "async_req" not in params  # SDK-internal flag excluded
        assert params["limit"].py_type == "int"
        assert params["limit"].required is False
        assert params["task_gid"].py_type == "str"
        assert params["task_gid"].required is True
        assert "(required)" not in params["task_gid"].description
        assert params["opt_fields"].py_type == "list[str]"


# ---------------------------------------------------------------------------
# SDK introspection
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def api_classes() -> list[type]:
    return _enumerate_api_classes()


@pytest.fixture(scope="module")
def tasks_cls(api_classes: list[type]) -> type:
    return next(c for c in api_classes if c.__name__ == "TasksApi")


@pytest.fixture(scope="module")
def tasks_ops(tasks_cls: type) -> list[_Operation]:
    return _operations_for(tasks_cls)


class TestIntrospect:
    def test_has_core_apis(self, api_classes: list[type]) -> None:
        names = {c.__name__ for c in api_classes}
        assert "TasksApi" in names
        assert "WorkspacesApi" in names
        assert "ProjectsApi" in names

    def test_skips_with_http_info(self, tasks_ops: list[_Operation]) -> None:
        names = {op.method_name for op in tasks_ops}
        assert "get_tasks" in names
        assert not any(n.endswith("_with_http_info") for n in names)

    def test_get_tasks_shape(self, tasks_ops: list[_Operation]) -> None:
        op = next(o for o in tasks_ops if o.method_name == "get_tasks")
        assert op.positional == []
        assert op.has_opts is True
        assert op.has_body is False
        assert op.paginatable is True
        opts_names = {p.name for p in op.opts_params}
        assert "limit" in opts_names
        assert "opt_fields" in opts_names

    def test_get_task_positional(self, tasks_ops: list[_Operation]) -> None:
        op = next(o for o in tasks_ops if o.method_name == "get_task")
        assert op.positional == ["task_gid"]
        assert op.has_body is False

    def test_create_task_has_body(self, tasks_ops: list[_Operation]) -> None:
        op = next(o for o in tasks_ops if o.method_name == "create_task")
        assert op.has_body is True
        assert "body" in op.positional

    def test_delete_task_no_opts(self, tasks_ops: list[_Operation]) -> None:
        op = next(o for o in tasks_ops if o.method_name == "delete_task")
        assert op.positional == ["task_gid"]
        assert op.has_opts is False

    def test_extract_operation_skips_private(self) -> None:
        # Private methods and the with_http_info variants must be skipped.
        for method_name in ("_internal", "get_tasks_with_http_info"):
            assert _extract_operation(method_name, lambda self: None) is None


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def get_tasks_cmd(tasks_cls: type, tasks_ops: list[_Operation]) -> click.Command:
    op = next(o for o in tasks_ops if o.method_name == "get_tasks")
    return _make_command(tasks_cls, op)


@pytest.fixture(scope="module")
def get_task_cmd(tasks_cls: type, tasks_ops: list[_Operation]) -> click.Command:
    op = next(o for o in tasks_ops if o.method_name == "get_task")
    return _make_command(tasks_cls, op)


@pytest.fixture(scope="module")
def upload_attachment_cmd() -> click.Command:
    # The one SDK endpoint whose method params have no :param: docstrings
    # — used to surface as bare ``--file TEXT`` etc. in --help. Fix lives
    # in ``_OPT_HELP_OVERRIDES`` (cli.py).
    cls = next(c for c in _enumerate_api_classes() if c.__name__ == "AttachmentsApi")
    op = next(o for o in _operations_for(cls) if o.method_name == "create_attachment_for_object")
    return _make_command(cls, op)


@pytest.fixture(scope="module")
def create_task_cmd(tasks_cls: type, tasks_ops: list[_Operation]) -> click.Command:
    op = next(o for o in tasks_ops if o.method_name == "create_task")
    return _make_command(tasks_cls, op)


def _option_flags(cmd: click.Command) -> set[str]:
    """Collect every command-line declaration accepted by the command.

    For ``--x/--no-x`` toggles, both the positive (``--x`` in ``p.opts``)
    and the negative (``--no-x`` in ``p.secondary_opts``) are reachable
    on the command line, so both go into the returned set.
    """
    flags: set[str] = set()
    for p in cmd.params:
        for decl in p.opts:
            flags.add(decl)
        for decl in getattr(p, "secondary_opts", []):
            flags.add(decl)
    return flags


class TestFlagCollisions:
    """An SDK arg/opt whose flag collides with a built-in CLI flag is exposed as
    ``--sdk-<name>`` so the built-in keeps its bare name (``cli._decls`` /
    ``_reserved_flags``). Real case: ``typeahead-for-workspace``'s ``query`` opt
    vs the formatter's ``--query`` (jq filter)."""

    def test_no_command_has_duplicate_flags(self) -> None:
        """Whole-SDK sweep: collision resolution must leave no command with a
        flag declared twice. Guards future SDK bumps that introduce a new
        collision (sibling of ``test_sdk_boilerplate`` / ``test_cli_surface``)."""
        offenders: dict[str, list[str]] = {}
        for cls in _enumerate_api_classes():
            for op in _operations_for(cls):
                cmd = _make_command(cls, op)
                flags: list[str] = [
                    decl
                    for p in cmd.params
                    if isinstance(p, click.Option)
                    for decl in (*p.opts, *p.secondary_opts)
                ]
                dups = [f for f, c in Counter(flags).items() if c > 1]
                if dups:
                    offenders[f"{cls.__name__}.{op.command_name}"] = dups
        assert not offenders, f"commands with duplicate flags: {offenders}"

    def test_typeahead_query_yields_to_sdk_prefix(self) -> None:
        import asana

        op = _extract_operation(
            "typeahead_for_workspace", asana.TypeaheadApi.typeahead_for_workspace
        )
        assert op is not None
        cmd = _make_command(asana.TypeaheadApi, op)
        opts = [p for p in cmd.params if isinstance(p, click.Option)]

        # The jq filter keeps the bare --query.
        jq = next(p for p in opts if p.name == "jq_query")
        assert "--query" in jq.opts

        # The SDK search opt is exposed as --sdk-query, but its dest (call path
        # / help label) stays the real SDK name ``query``.
        sdk_query = next(p for p in opts if p.name == "query")
        assert sdk_query.opts == ["--sdk-query"]
        assert "--query" not in sdk_query.opts
        assert "(opts: query)" in (sdk_query.help or "")


class TestReservedFlags:
    def test_decls_passes_through_when_no_collision(self) -> None:
        assert _decls("--task", "task", frozenset()) == ["--task"]

    def test_decls_prefixes_and_preserves_dest_on_collision(self) -> None:
        assert _decls("--query", "query", frozenset({"--query"})) == ["--sdk-query", "query"]

    def test_static_reserved_covers_each_builtin_family(self) -> None:
        reserved = _static_reserved_flags()
        # formatter / kwarg / global (+ a --no- toggle's negative form).
        for flag in ("--query", "--output", "--item-limit", "--host", "--no-verify-ssl"):
            assert flag in reserved, f"{flag} missing from reserved set"

    def test_help_and_version_reserved_so_sdk_opts_yield(self) -> None:
        # --help is click-added at parse time and --version is root-only, so
        # neither shows up in a leaf's params. They are reserved explicitly so a
        # future SDK opt named help/version is pushed to --sdk-help/--sdk-version
        # instead of shadowing click's built-ins.
        reserved = _static_reserved_flags()
        assert "--help" in reserved
        assert "--version" in reserved
        assert _decls("--help", "help", reserved) == ["--sdk-help", "help"]
        assert _decls("--version", "version", reserved) == ["--sdk-version", "version"]


class TestBuiltCommands:
    def test_get_task_has_task_option(self, get_task_cmd: click.Command) -> None:
        # Path positional ``task_gid`` becomes ``--task`` (gid suffix stripped).
        assert "--task" in _option_flags(get_task_cmd)

    def test_renamed_positional_help_shows_sdk_arg(self, get_task_cmd: click.Command) -> None:
        # When ``task_gid`` is exposed as ``--task``, the original SDK
        # positional-arg name must appear in the help — labeled
        # ``(SDK arg: task_gid)`` — so users can map the CLI flag back to the
        # python-asana API. ``task_gid`` is a positional method argument, not
        # an ``opts`` entry or a kwarg, hence the ``SDK arg`` category.
        task_param = next(
            p for p in get_task_cmd.params if isinstance(p, click.Option) and "--task" in p.opts
        )
        assert "(SDK arg: task_gid)" in (task_param.help or "")

    def test_opts_param_help_shows_opts_label(self, get_tasks_cmd: click.Command) -> None:
        # Docstring ``opts`` entries are labeled ``(opts: <name>)`` so users
        # know the value lands in the SDK method's ``opts`` dict.
        opt_fields = next(
            p
            for p in get_tasks_cmd.params
            if isinstance(p, click.Option) and "--opt-fields" in p.opts
        )
        assert "(opts: opt_fields)" in (opt_fields.help or "")

    def test_gid_positional_uses_gid_metavar_and_example(self, get_task_cmd: click.Command) -> None:
        # Per issue #15: the SDK descriptions for ``*_gid`` params are
        # uninformative ("The task to operate on." or "Globally unique
        # identifier for the X"). The CLI must (1) render the option as
        # ``--task GID`` rather than ``--task TEXT`` and (2) make the
        # numeric-id requirement obvious to a first-time user via an example.
        task_param = next(
            p for p in get_task_cmd.params if isinstance(p, click.Option) and "--task" in p.opts
        )
        assert task_param.metavar == "GID"
        help_text = task_param.help or ""
        assert "GID" in help_text
        assert "1234567890" in help_text  # example, communicates "numeric"

    def test_body_help_includes_input_format_and_envelope(
        self, create_task_cmd: click.Command
    ) -> None:
        # Per issue #16: the SDK description for the ``body`` param
        # ("The task to create.") tells users nothing about how to actually
        # pass the payload. The CLI must always show that --body accepts
        # inline JSON, @path, or stdin, AND mention Asana's data envelope.
        body_param = next(
            p for p in create_task_cmd.params if isinstance(p, click.Option) and "--body" in p.opts
        )
        assert body_param.metavar == "JSON"
        help_text = body_param.help or ""
        for needle in ("inline JSON", "@path", "stdin", '"data"', "(SDK arg: body)"):
            assert needle in help_text, f"--body help missing {needle!r}; got: {help_text!r}"

    def test_get_tasks_pagination_options(self, get_tasks_cmd: click.Command) -> None:
        flags = _option_flags(get_tasks_cmd)
        # Per-command (docstring opts): --limit / --offset come from the
        # SDK method's docstring entries.
        for expected in ("--limit", "--offset"):
            assert expected in flags, f"missing {expected}"
        # Per-call kwargs (--item-limit / --full-payload) are per-command
        # options on every command; --page-limit / --no-return-page-iterator
        # are Configuration-backed globals injected by CommandWithGlobalOptions.
        # All four appear here.
        for expected in (
            "--page-limit",
            "--item-limit",
            "--no-return-page-iterator",
            "--full-payload",
        ):
            assert expected in flags, f"missing {expected}"
        # v2 → v3 deprecation aliases (kept until a future release).
        for expected in ("--all-items", "--page-size", "--max-items"):
            assert expected in flags, f"missing {expected}"
        # ``--paginate`` (deprecated alias for --all-items) was removed in 2.1.0.
        assert "--paginate" not in flags

    def test_get_tasks_workspace_option(self, get_tasks_cmd: click.Command) -> None:
        # ``workspace`` opt is exposed as ``--workspace``.
        assert "--workspace" in _option_flags(get_tasks_cmd)

    def test_create_task_body_required(self, create_task_cmd: click.Command) -> None:
        body_param = next(p for p in create_task_cmd.params if "--body" in p.opts)
        assert body_param.required is True

    def test_get_task_no_pagination(self, get_task_cmd: click.Command) -> None:
        flags = _option_flags(get_task_cmd)
        # Docstring-derived opts (--limit / --offset) and v2 deprecation
        # aliases are absent on non-paginatable commands (get-task's
        # docstring declares neither limit/offset, and aliases are gated
        # by paginatable in _make_command).
        for unexpected in (
            "--limit",
            "--offset",
            "--all-items",
            "--page-size",
            "--max-items",
        ):
            assert unexpected not in flags, f"{unexpected} should not be on non-paginatable cmd"
        # Per-call kwargs (--item-limit / --full-payload, per-command) and the
        # Configuration globals (--page-limit / --no-return-page-iterator) are
        # present on every command incl. non-paginatable ones — the SDK accepts
        # the kwargs uniformly (all_params) and the Configuration knobs apply
        # client-wide.
        for expected in (
            "--page-limit",
            "--item-limit",
            "--no-return-page-iterator",
            "--full-payload",
        ):
            assert expected in flags, f"{expected} should be present on every cmd"

    def test_per_call_kwargs_are_per_command_not_global(self, get_tasks_cmd: click.Command) -> None:
        # The boilerplate per-call kwargs (all_params) are method inputs, so
        # they render as per-command options — present on a command, absent
        # from the root. (Configuration-backed --page-limit /
        # --return-page-iterator stay global; asserted above.)
        cmd_flags = _option_flags(get_tasks_cmd)
        root_flags = _option_flags(main)
        for flag in ("--item-limit", "--full-payload", "--header-params", "--request-timeout"):
            assert flag in cmd_flags, f"{flag} should be a per-command option"
            assert flag not in root_flags, f"{flag} should not be a root/global option"

    def test_output_query_options_present(self, get_tasks_cmd: click.Command) -> None:
        flags = _option_flags(get_tasks_cmd)
        assert "--output" in flags
        assert "--query" in flags

    def test_option_display_order_follows_sdk_signature(
        self,
        tasks_cls: type,
        tasks_ops: list[_Operation],
        get_tasks_cmd: click.Command,
    ) -> None:
        # The "order CLI options by SDK signature" refactor pins a three-tier
        # display order in _make_command (see its docstring): path/body
        # positionals in function-signature order → docstring opts in :param
        # order → the boilerplate per-call kwargs. Every other test here checks
        # only set membership (_option_flags returns a set) or the Deprecated
        # section split, so nothing guarded the actual order; a future re-sort
        # (the manifest path already name-sorts opts) would pass them all.
        #
        # Tier 1 + positionals-before-kwargs: add_dependencies_for_task(self,
        # body, task_gid) → --body then --task, both before the per-call kwargs.
        op = next(o for o in tasks_ops if o.method_name == "add_dependencies_for_task")
        flags = [
            p.opts[0] for p in _make_command(tasks_cls, op).params if isinstance(p, click.Option)
        ]
        assert flags.index("--body") < flags.index("--task") < flags.index("--item-limit")

        # Tier 2 before Tier 3: a docstring opt (--limit) precedes the per-call
        # kwargs (--item-limit) on get-tasks.
        gt = [p.opts[0] for p in get_tasks_cmd.params if isinstance(p, click.Option)]
        assert gt.index("--limit") < gt.index("--item-limit")

    def test_no_pagination_epilog_per_command(
        self, get_tasks_cmd: click.Command, get_task_cmd: click.Command
    ) -> None:
        # In v3.1 the per-command Pagination epilog was removed (the flags
        # became global and self-document via their own help text). Neither
        # paginatable nor non-paginatable commands attach an epilog now.
        assert not (get_tasks_cmd.epilog or "")
        assert not (get_task_cmd.epilog or "")

    def test_long_sdk_descriptions_are_not_truncated(self, get_tasks_cmd: click.Command) -> None:
        # Per issue #6: the previous _escape_help() cut every desc at 200
        # chars with "...", dropping critical caveats. Pin two real-world
        # examples that USED to be truncated mid-sentence:
        #   --assignee's "you must also specify the workspace" caveat
        #   --offset's "If an offset is not passed in, ..." continuation
        assignee = next(
            p
            for p in get_tasks_cmd.params
            if isinstance(p, click.Option) and "--assignee" in p.opts
        )
        assert "workspace" in (assignee.help or ""), (
            "expected the --assignee help to mention the workspace requirement; "
            f"got: {assignee.help!r}"
        )
        # The SDK desc ends with a period; if our truncator is back we'll
        # see "..." somewhere in the help.
        assert "..." not in (assignee.help or ""), (
            f"--assignee help still ends in truncation: {assignee.help!r}"
        )

    def test_attachment_upload_params_have_help(self, upload_attachment_cmd: click.Command) -> None:
        # Per issue #10: ``attachments create-attachment-for-object`` was
        # the one SDK endpoint whose method has no ``:param:`` docstrings,
        # so its CLI options rendered bare (``--file TEXT`` with no help).
        # The _OPT_HELP_OVERRIDES table fills the gap. Pin every param so
        # a future SDK that REMOVES the docstring for an existing kwarg
        # (or adds a new bare one to this endpoint) still has us covered
        # via the override or fails this test if missed.
        bare_target_names = {
            "connect_to_app",
            "file",
            "name",
            "parent",
            "resource_subtype",
            "url",
        }
        seen: set[str] = set()
        for param in upload_attachment_cmd.params:
            if not isinstance(param, click.Option):
                continue
            if param.name not in bare_target_names:
                continue
            seen.add(param.name)
            help_text = (param.help or "").strip()
            assert help_text, (
                f"--{param.name.replace('_', '-')} on attachments "
                f"create-attachment-for-object has empty help; add an entry "
                f"to _OPT_HELP_OVERRIDES in cli.py."
            )
        missing = bare_target_names - seen
        assert not missing, (
            f"expected attachment-upload params {sorted(missing)} on the "
            f"command but did not find them — SDK signature changed?"
        )

    def test_deprecated_aliases_in_separate_section(self, get_tasks_cmd: click.Command) -> None:
        # Per issue #11: --all-items / --page-size / --max-items used to sit
        # in the same Options block as current v3 flags, with a verbose
        # "[Deprecated v3.0] ... Removed in a future release." prefix on
        # each line. A newcomer skimming the table could easily pick one
        # up by mistake. Move them under their own "Deprecated" section
        # and drop the redundant per-line markers (the section heading
        # carries the status).
        ctx = click.Context(get_tasks_cmd, info_name="get-tasks")
        out = get_tasks_cmd.get_help(ctx)

        # Section heading must appear.
        assert "Deprecated (v3.0; will be removed):" in out, (
            f"missing Deprecated section heading in:\n{out}"
        )
        # The 3 aliases must appear under that section, but not in the
        # Options: block.
        options_section, _, rest = out.partition("Deprecated (v3.0; will be removed):")
        assert "--all-items" not in options_section
        assert "--page-size" not in options_section
        assert "--max-items" not in options_section
        for flag in ("--all-items", "--page-size", "--max-items"):
            assert flag in rest, f"{flag} missing from Deprecated section"

        # Each entry should now be terse — the section heading conveys
        # the "[Deprecated v3.0] ... Removed in a future release."
        # boilerplate, so it should not be repeated on every line.
        assert "[Deprecated v3.0]" not in out
        assert "Removed in a future release" not in out

    # ``test_pagination_flag_help_is_self_contained`` (pre-v3.1) constrained
    # each per-command pagination flag's help text to avoid naming other
    # pagination flags — the consolidated per-command epilog (now removed
    # in v3.1) carried the interaction story instead. With the flags
    # promoted to global and the epilog gone, the help text IS the only
    # place to describe equivalences, so cross-references are now both
    # allowed and expected (e.g. ``--full-payload`` notes equivalence with
    # ``--no-return-page-iterator``). Test removed.


# ---------------------------------------------------------------------------
# Root group integration
# ---------------------------------------------------------------------------


class TestRootGroup:
    def test_main_is_click_group(self) -> None:
        assert isinstance(main, click.Group)

    def test_main_lists_known_groups(self) -> None:
        ctx = click.Context(main)
        names = set(main.list_commands(ctx))
        for expected in ("tasks", "projects", "workspaces", "users"):
            assert expected in names

    def test_root_help_has_examples_section(self) -> None:
        # Per issue #12: the root --help used to dump the Commands table
        # and then end abruptly. A newcomer had no obvious "next step" —
        # which command to run, what arguments look like, where the auth
        # comes from. Add an Examples epilog covering read / single-fetch
        # / create / debug, plus a pointer to per-group --help and the
        # auth env-var hint.
        ctx = click.Context(main, info_name="asana-api")
        out = main.get_help(ctx)
        assert "Examples:" in out
        # Spot-check one each of: read, create, debug, group-pointer, auth.
        assert "tasks get-tasks" in out
        assert "tasks create-task" in out
        assert "--debug" in out  # shown as an example
        assert "asana-api tasks --help" in out
        assert "$ASANA_ACCESS_TOKEN" in out

    def test_main_has_global_options(self) -> None:
        from asana_api_cli.click_ext import _SDK_HAS_RETRY_STRATEGY

        flags = _option_flags(main)
        expected_flags = [
            "--host",
            "--proxy",
            "--verify-ssl",
            "--no-verify-ssl",
            "--ssl-ca-cert",
            "--cert-file",
            "--key-file",
            "--assert-hostname",
            "--no-assert-hostname",
            "--connection-pool-maxsize",
            "--access-token",
            "--temp-folder-path",
            "--safe-chars-for-path-param",
            "--logger-format",
            "--logger-file",
            "--debug",
        ]
        if _SDK_HAS_RETRY_STRATEGY:
            expected_flags.append("--retry-strategy")
        for expected in expected_flags:
            assert expected in flags, f"missing {expected}"

    def test_main_does_not_have_removed_options(self) -> None:
        """Old v2 names dropped in v3 must not resurface accidentally."""
        flags = _option_flags(main)
        for absent in ("--retries", "--timeout", "--ca-cert", "--temp-dir"):
            assert absent not in flags, f"{absent} should be removed"

    def test_inert_auth_options_removed(self) -> None:
        # --username / --password / --api-key / --api-key-prefix were dropped:
        # they are inert swagger-codegen Configuration fields with no Asana auth
        # counterpart (basic auth is unsupported; API keys are deprecated and
        # being shut off). Only token auth (--access-token) remains.
        flags = _option_flags(main)
        for absent in ("--username", "--password", "--api-key", "--api-key-prefix"):
            assert absent not in flags, f"{absent} should be removed (inert auth)"
        assert "--access-token" in flags

    def test_multibyte_filenames_scoped_to_upload_command(
        self,
        upload_attachment_cmd: click.Command,
        get_tasks_cmd: click.Command,
    ) -> None:
        # --multibyte-filenames moved from a global flag to a per-command
        # option present only on upload commands (those declaring a ``file``
        # opt — i.e. the one multipart endpoint). It must be absent from the
        # root globals and from non-upload commands, and present on the upload
        # command labeled as an asana-api extension.
        assert "--multibyte-filenames" not in _option_flags(main)
        assert "--multibyte-filenames" not in _option_flags(get_tasks_cmd)
        assert "--multibyte-filenames" in _option_flags(upload_attachment_cmd)
        param = next(
            p
            for p in upload_attachment_cmd.params
            if isinstance(p, click.Option) and "--multibyte-filenames" in p.opts
        )
        assert (param.help or "").rstrip().endswith("(asana-api extension)")

    def test_subgroup_help_resolves(self) -> None:
        # Resolving a subgroup must trigger lazy method introspection.
        ctx = click.Context(main)
        tasks_group = main.get_command(ctx, "tasks")
        assert isinstance(tasks_group, click.Group)
        sub_ctx = click.Context(tasks_group, parent=ctx)
        cmd_names = set(tasks_group.list_commands(sub_ctx))
        assert "get-tasks" in cmd_names
        assert "create-task" in cmd_names
