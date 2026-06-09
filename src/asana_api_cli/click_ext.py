"""Click extensions: global-option propagation across the command tree.

Two concerns live here:

1. ``GroupWithGlobalOptions`` / ``CommandWithGlobalOptions`` — accept the
   global options (``--debug``, ``--access-token``, ...) — declared once in
   ``_global_option_sections`` and applied identically to the root group and
   every subcommand — at any level of the command tree, so
   ``asana-api tasks get-tasks --debug`` works the same as
   ``asana-api --debug tasks get-tasks``. Values whose source is the command
   line are written to the shared ``runtime`` singleton; defaults are ignored
   so they cannot clobber values already set at a higher level.

2. Both subclasses surface those options grouped by semantic category in
   ``--help`` so they remain discoverable from any subcommand. On the root
   they render as top-level sections (``Authentication:``, ``Connection:``,
   ``TLS:`` ...); on subcommands the same sections nest under a single
   ``Global Options:`` umbrella to keep them visually separated from the
   subcommand's own options. See ``GLOBAL_OPTION_GROUPS``.
"""

from __future__ import annotations

import textwrap
from typing import Any

import asana
import click
from click.core import ParameterSource

from asana_api_cli.session import runtime
from asana_api_cli.structured_arg import (
    RETRY_FIELD_SCHEMA,
    click_callback,
    default_header_callback,
)

# ``Configuration.retry_strategy`` was introduced in python-asana 5.1.
# When wrapping an older SDK the property simply does not exist, so
# exposing ``--retry-strategy`` would crash at apply time. Detect once at
# import and gate the single retry-strategy touchpoint: the "Retry" section
# in ``_global_option_sections`` (from which the flag, the help grouping, and
# ``GLOBAL_OPTION_NAMES`` all derive). The runtime applier in ``session.py``
# already checks ``runtime.retry_strategy_overrides is not None`` so its
# code path is naturally dead when the flag is hidden. We instantiate
# ``Configuration()`` because ``retry_strategy`` is an instance attribute
# set in ``__init__`` — checking ``hasattr`` on the class itself returns
# False even on 5.2.4.
_SDK_HAS_RETRY_STRATEGY: bool = hasattr(asana.Configuration(), "retry_strategy")


def _global_option_sections() -> list[tuple[str, list[click.Option]]]:
    """The single source of truth for the global Configuration / ApiClient knobs.

    Each global option is declared here exactly once, grouped by the ``--help``
    section it renders under. The root command and every subcommand draw their
    global options from this one builder (via :func:`_make_global_option_params`),
    so there is no second declaration site to keep in sync — every command
    exposes byte-identical global-option definitions (flag spelling, help,
    default, flag-ness, type) by construction. (How they *render* differs by
    level — see :class:`_GlobalOptionsMixin`.) ``--help`` section order and the
    option order within a section both derive from this list (see
    :data:`GLOBAL_OPTION_GROUPS`).

    Fresh ``click.Option`` instances are returned on every call: click stores
    per-command state on Option objects, so the same instance must not be shared
    across commands.

    ``--retry-strategy`` is present only when the installed python-asana exposes
    ``Configuration.retry_strategy`` (added in 5.1); on older SDKs the whole
    section is omitted (see :data:`_SDK_HAS_RETRY_STRATEGY`).
    """
    return [
        (
            "Authentication",
            [
                click.Option(
                    ["--access-token", "access_token"],
                    default=None,
                    help=(
                        "Asana personal access token (default: $ASANA_ACCESS_TOKEN). "
                        "(Configuration: access_token)"
                    ),
                ),
            ],
        ),
        (
            "Connection",
            [
                click.Option(
                    ["--host"],
                    default=None,
                    help=(
                        "Override API base URL (default: https://app.asana.com/api/1.0). "
                        "(Configuration: host)"
                    ),
                ),
                click.Option(
                    ["--proxy"],
                    default=None,
                    help="HTTP/HTTPS proxy URL. (Configuration: proxy)",
                ),
                click.Option(
                    ["--connection-pool-maxsize", "connection_pool_maxsize"],
                    type=click.IntRange(min=1),
                    default=None,
                    help=(
                        "Max urllib3 connections cached per host (default: "
                        "cpu_count * 5). (Configuration: connection_pool_maxsize)"
                    ),
                ),
            ],
        ),
        (
            "TLS",
            [
                click.Option(
                    ["--verify-ssl/--no-verify-ssl", "verify_ssl"],
                    default=None,
                    help=(
                        "Verify TLS certificates (default: True). Pass "
                        "--no-verify-ssl to disable (insecure). "
                        "(Configuration: verify_ssl)"
                    ),
                ),
                click.Option(
                    ["--ssl-ca-cert", "ssl_ca_cert"],
                    default=None,
                    type=click.Path(exists=True, dir_okay=False),
                    help="Path to a PEM bundle of trusted CA certs. (Configuration: ssl_ca_cert)",
                ),
                click.Option(
                    ["--cert-file", "cert_file"],
                    default=None,
                    type=click.Path(exists=True, dir_okay=False),
                    help="Client TLS certificate for mTLS. (Configuration: cert_file)",
                ),
                click.Option(
                    ["--key-file", "key_file"],
                    default=None,
                    type=click.Path(exists=True, dir_okay=False),
                    help="Client TLS private key for mTLS. (Configuration: key_file)",
                ),
                click.Option(
                    ["--assert-hostname/--no-assert-hostname", "assert_hostname"],
                    default=None,
                    help=(
                        "Verify the server certificate's hostname matches the "
                        "request URL host. Tri-state: unspecified → urllib3 "
                        "default. (Configuration: assert_hostname)"
                    ),
                ),
            ],
        ),
        (
            "HTTP headers",
            [
                click.Option(
                    ["--user-agent", "user_agent"],
                    default=None,
                    help=(
                        "Override the User-Agent header the SDK sends on every request. "
                        "(ApiClient: user_agent)"
                    ),
                ),
                click.Option(
                    ["--set-default-header", "default_headers"],
                    multiple=True,
                    callback=default_header_callback,
                    help=(
                        "Add an HTTP header sent on every request, given as NAME=VALUE; "
                        "repeatable. Unlike per-call --header-params it applies to all "
                        "calls. Only Authorization / Proxy-Authorization values are "
                        "redacted in --debug output — see SECURITY.md. "
                        "(ApiClient: set_default_header)"
                    ),
                ),
            ],
        ),
        *(
            [
                (
                    "Retry",
                    [
                        click.Option(
                            ["--retry-strategy", "retry_strategy_overrides"],
                            default=None,
                            callback=click_callback(schema=RETRY_FIELD_SCHEMA),
                            help=(
                                "Override urllib3 Retry fields. VALUE: "
                                "'k1=v1,k2=v2,...', JSON object, or @path. See "
                                "urllib3 Retry docs. List-typed fields "
                                "(allowed_methods, status_forcelist, "
                                "remove_headers_on_redirect) require JSON. "
                                "(Configuration: retry_strategy)"
                            ),
                        ),
                    ],
                ),
            ]
            if _SDK_HAS_RETRY_STRATEGY
            else []
        ),
        (
            "Pagination / iteration",
            [
                click.Option(
                    ["--return-page-iterator/--no-return-page-iterator", "return_page_iterator"],
                    default=None,
                    help=(
                        "Toggle the SDK page iterator (default: enabled). With "
                        "--no-return-page-iterator, paginatable endpoints return a "
                        "single {data, next_page} dict from one HTTP call instead of "
                        "auto-walking every page. (Configuration: return_page_iterator)"
                    ),
                ),
                click.Option(
                    ["--page-limit", "page_limit"],
                    type=int,
                    default=None,
                    help=(
                        "Per-page size when the iterator falls back to Configuration "
                        "(default: 100). Equivalent to --limit on paginatable endpoints; "
                        '--limit (per-call opts["limit"]) takes precedence when both '
                        "are set. (Configuration: page_limit)"
                    ),
                ),
            ],
        ),
        (
            "Logging / Debug",
            [
                click.Option(
                    ["--debug"],
                    is_flag=True,
                    default=False,
                    help=(
                        "Print the SDK's HTTP request/response debug output for "
                        "troubleshooting, with the Authorization header masked. As the "
                        "SDK emits it, the wire trace (headers) goes to stdout and the "
                        "connection/response log to stderr. (Configuration: debug)"
                    ),
                ),
                click.Option(
                    ["--logger-format", "logger_format"],
                    default=None,
                    help="Python logging format string. (Configuration: logger_format)",
                ),
                click.Option(
                    ["--logger-file", "logger_file"],
                    default=None,
                    type=click.Path(dir_okay=False),
                    help="Path SDK loggers write to. (Configuration: logger_file)",
                ),
            ],
        ),
        (
            "Advanced",
            [
                click.Option(
                    ["--temp-folder-path", "temp_folder_path"],
                    default=None,
                    type=click.Path(file_okay=False),
                    help="Directory for temporary downloads. (Configuration: temp_folder_path)",
                ),
                click.Option(
                    ["--safe-chars-for-path-param", "safe_chars_for_path_param"],
                    default=None,
                    help=(
                        "Extra chars treated as safe when percent-encoding path "
                        "parameters. (Configuration: safe_chars_for_path_param)"
                    ),
                ),
            ],
        ),
        (
            "Code generation",
            [
                click.Option(
                    ["--generate-python", "generate_python"],
                    is_flag=True,
                    default=False,
                    help=(
                        "Print standalone python-asana code equivalent to this "
                        "command instead of running it. Makes no network call and "
                        "needs no access token. (asana-api: extension)"
                    ),
                ),
            ],
        ),
    ]


def _make_global_option_params() -> list[click.Option]:
    """Flatten the single global-option source into a flat option list.

    Every command (the root group, each subgroup, and each leaf) appends a fresh
    copy of this list to its own params so the globals are accepted at any level.
    See :func:`_global_option_sections`.
    """
    return [opt for _, opts in _global_option_sections() for opt in opts]


# Derived from the single source above. ``GLOBAL_OPTION_GROUPS`` maps each
# ``--help`` section to the option ``name``s (dests) it contains, in render
# order; ``GLOBAL_OPTION_NAMES`` is the flat set used to recognize a global
# option anywhere in the tree.
def _opt_dest(opt: click.Option) -> str:
    """The dest of a global option. Every global option has one (click derives
    it from the flags), so the ``None`` case is unreachable — narrow for typing."""
    assert opt.name is not None
    return opt.name


GLOBAL_OPTION_GROUPS: list[tuple[str, list[str]]] = [
    (section, [_opt_dest(opt) for opt in opts]) for section, opts in _global_option_sections()
]

GLOBAL_OPTION_NAMES: frozenset[str] = frozenset(
    name for _, members in GLOBAL_OPTION_GROUPS for name in members
)

# Shorter labels used in the compact (non-root) Global Options table.
# The first column's width is driven by the longest label, so trimming the
# longest one widens the right-hand option column on narrow terminals.
_COMPACT_SECTION_LABELS: dict[str, str] = {
    "Pagination / iteration": "Pagination",
}

# Endpoint-local option ``name``s that should render under their own
# "Deprecated" section rather than mixed into the main Options block.
# These options keep working (they emit a stderr warning at runtime and
# resolve to the v3 replacement) but should be visually separated so a
# new reader of ``--help`` doesn't pick them up by mistake.
_DEPRECATED_OPTION_NAMES: frozenset[str] = frozenset({"all_items", "page_size", "max_items"})


def _apply_global_to_runtime(name: str, value: Any) -> None:
    """Write a single global option value to the shared ``runtime`` singleton.

    Every global option's dest matches a ``_Runtime`` field name 1:1 — the
    option declarations and the dataclass fields are authored together, and the
    inventory tests in ``test_click_ext.py`` (``TestGlobalOptionNamesInventory``)
    pin the ``GLOBAL_OPTION_NAMES`` set against ``_Runtime``'s fields — so a
    direct ``setattr`` suffices. Every option, ``access_token`` included,
    follows the same last-wins rule: the command-line value from the deepest
    level reached overwrites whatever an earlier level wrote. An explicit empty
    ``--access-token`` therefore clears a value set earlier, and
    ``AsanaSession.from_env`` then falls back to ``$ASANA_ACCESS_TOKEN``.

    The caller, :func:`_consume_global_options`, only ever passes names drawn
    from ``GLOBAL_OPTION_NAMES`` and only when the parameter source is
    ``ParameterSource.COMMANDLINE``. This is the single application path for
    every level of the tree (root group included). That guarantee is why this
    function needs no name validation and no ``None``-default guarding: the
    tri-state toggles' (``verify_ssl`` / ``assert_hostname`` /
    ``return_page_iterator``) ``None`` default never reaches here.
    """
    setattr(runtime, name, value)


def _consume_global_options(ctx: click.Context) -> None:
    """Pop injected global options from ``ctx.params`` and apply user values.

    Called before the wrapped callback runs so the original function signature
    does not need to grow these parameters. Only values whose source is the
    command line are written back to ``runtime``; defaults are dropped so they
    cannot clobber values already set at a higher level (e.g. on the root
    command).
    """
    for name in GLOBAL_OPTION_NAMES:
        if name not in ctx.params:
            continue
        value = ctx.params.pop(name)
        if ctx.get_parameter_source(name) is ParameterSource.COMMANDLINE:
            _apply_global_to_runtime(name, value)


def _write_compact_dl(
    formatter: click.HelpFormatter, rows: list[tuple[str, str]], *, col_spacing: int = 2
) -> None:
    """Two-column rendering for the compact "Global Options" table.

    Used instead of ``formatter.write_dl`` because the latter wraps the
    right column with ``break_on_hyphens=True``, which splits multi-hyphen
    option names like ``--connection-pool-maxsize`` mid-word. We render
    by hand with ``textwrap`` configured to wrap only at whitespace
    (option boundaries), so a long row breaks between options rather than
    inside one.
    """
    if not rows:
        return
    label_width = max(len(label) for label, _ in rows)
    first_col = label_width + col_spacing
    indent = formatter.current_indent
    text_width = max(formatter.width - first_col - indent, 30)
    pad = " " * indent
    cont_pad = " " * (indent + first_col)
    for label, options in rows:
        wrapped = textwrap.wrap(
            options,
            width=text_width,
            break_long_words=False,
            break_on_hyphens=False,
        ) or [""]
        formatter.write(f"{pad}{label}{' ' * (first_col - len(label))}{wrapped[0]}\n")
        for line in wrapped[1:]:
            formatter.write(f"{cont_pad}{line}\n")


class _GlobalOptionsMixin:
    """Render global options grouped by category in ``--help``.

    On the root command, each group renders as a top-level section
    (``Authentication:``, ``Connection:`` ...) with full per-option help
    text.

    On subcommands, the same groups collapse to a single compact table
    under ``Global Options:`` — one row per category listing the option
    names only — with a pointer back to ``asana-api --help`` for the
    descriptions. Subcommands inherit the global flags' behavior but no
    longer drown their own ``Options:`` block in ~70 lines of repeated
    global-option help text.
    """

    def format_options(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        is_root = ctx.parent is None

        # Split params into (local, deprecated-local, global). Locals
        # render under "Options:"; deprecated locals render under their
        # own "Deprecated" section so they don't get picked up by a
        # newcomer skimming the main Options block; globals render via
        # the grouping defined in GLOBAL_OPTION_GROUPS.
        local_records: list[tuple[str, str]] = []
        deprecated_records: list[tuple[str, str]] = []
        globals_by_name: dict[str, click.Parameter] = {}
        for param in self.get_params(ctx):  # type: ignore[attr-defined]
            if param.name in GLOBAL_OPTION_NAMES:
                globals_by_name[param.name] = param
                continue
            record = param.get_help_record(ctx)
            if record is None:
                continue
            if param.name in _DEPRECATED_OPTION_NAMES:
                deprecated_records.append(record)
            else:
                local_records.append(record)

        if local_records:
            with formatter.section("Options"):
                formatter.write_dl(local_records)

        if deprecated_records:
            with formatter.section("Deprecated (v3.0; will be removed)"):
                formatter.write_dl(deprecated_records)

        grouped_params: list[tuple[str, list[click.Option]]] = []
        for section_name, member_names in GLOBAL_OPTION_GROUPS:
            section_params: list[click.Option] = []
            for name in member_names:
                opt = globals_by_name.get(name)
                if isinstance(opt, click.Option):
                    section_params.append(opt)
            if section_params:
                grouped_params.append((section_name, section_params))

        if is_root:
            for section_name, section_params in grouped_params:
                records = [
                    record
                    for record in (p.get_help_record(ctx) for p in section_params)
                    if record is not None
                ]
                if records:
                    with formatter.section(section_name):
                        formatter.write_dl(records)
        elif grouped_params:
            with formatter.section("Global Options"):
                formatter.write_text("See `asana-api --help` for descriptions.")
                formatter.write_paragraph()
                rows = [
                    (
                        _COMPACT_SECTION_LABELS.get(section_name, section_name),
                        " ".join(p.opts[0] for p in section_params),
                    )
                    for section_name, section_params in grouped_params
                ]
                _write_compact_dl(formatter, rows)

        # Subcommand listing (groups only); placed last so the commands table
        # is the final thing the user sees before the prompt.
        if isinstance(self, click.Group):
            self.format_commands(ctx, formatter)


class CommandWithGlobalOptions(_GlobalOptionsMixin, click.Command):
    """A ``click.Command`` that also accepts the shared global options."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for opt in _make_global_option_params():
            self.params.append(opt)

    def invoke(self, ctx: click.Context) -> Any:
        _consume_global_options(ctx)
        return super().invoke(ctx)


class GroupWithGlobalOptions(_GlobalOptionsMixin, click.Group):
    """A ``click.Group`` that also accepts the shared global options.

    Used for the root group and every subgroup: the global flags come from
    the single ``_global_option_sections`` source at every level, with no
    separate root declaration. Children created via ``@group.command(...)``
    default to ``CommandWithGlobalOptions`` so they inherit the same behavior.
    """

    command_class = CommandWithGlobalOptions

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for opt in _make_global_option_params():
            self.params.append(opt)

    def invoke(self, ctx: click.Context) -> Any:
        _consume_global_options(ctx)
        return super().invoke(ctx)
