"""Click extensions: lazy subcommand loading and global-option propagation.

Three concerns live here:

1. ``LazyGroup`` — a ``click.Group`` that resolves subcommands on demand by
   import path, so ``--help`` and tab completion do not need to import every
   ``*Api`` module up front.

2. ``GroupWithGlobalOptions`` / ``CommandWithGlobalOptions`` — accept the root
   group's options (``--debug``, ``--access-token``, ...) at any level of the
   command tree, so ``asana-api tasks get-tasks --debug`` works the same as
   ``asana-api --debug tasks get-tasks``. Values whose source is the command
   line are written to the shared ``runtime`` singleton; defaults are ignored
   so they cannot clobber values already set at a higher level.

3. Both subclasses surface those options grouped by semantic category in
   ``--help`` so they remain discoverable from any subcommand. On the root
   they render as top-level sections (``Authentication:``, ``Connection:``,
   ``TLS:`` ...); on subcommands the same sections nest under a single
   ``Global Options:`` umbrella to keep them visually separated from the
   subcommand's own options. See ``GLOBAL_OPTION_GROUPS``.
"""

from __future__ import annotations

import importlib
import textwrap
from typing import Any

import asana
import click
from click.core import ParameterSource

from asana_api_cli.session import runtime
from asana_api_cli.structured_arg import RETRY_FIELD_SCHEMA, click_callback

# ``Configuration.retry_strategy`` was introduced in python-asana 5.1.
# When wrapping an older SDK the property simply does not exist, so
# exposing ``--retry-strategy`` would crash at apply time. Detect once at
# import and gate every retry-strategy touchpoint (option declaration on
# the root, mirror in ``_make_global_option_params``, the "Retry" group
# under ``GLOBAL_OPTION_GROUPS``). The runtime applier in ``session.py``
# already checks ``runtime.retry_strategy_overrides is not None`` so its
# code path is naturally dead when the flag is hidden. We instantiate
# ``Configuration()`` because ``retry_strategy`` is an instance attribute
# set in ``__init__`` — checking ``hasattr`` on the class itself returns
# False even on 5.2.4.
_SDK_HAS_RETRY_STRATEGY: bool = hasattr(asana.Configuration(), "retry_strategy")


GLOBAL_OPTION_GROUPS: list[tuple[str, list[str]]] = [
    ("Authentication", ["access_token"]),
    (
        "Connection",
        ["host", "proxy", "connection_pool_maxsize"],
    ),
    (
        "TLS",
        ["verify_ssl", "ssl_ca_cert", "cert_file", "key_file", "assert_hostname"],
    ),
    *([("Retry", ["retry_strategy_overrides"])] if _SDK_HAS_RETRY_STRATEGY else []),
    (
        "Pagination / iteration",
        [
            "return_page_iterator",
            "page_limit",
        ],
    ),
    (
        "Logging / Debug",
        ["debug", "logger_format", "logger_file"],
    ),
    ("Advanced", ["temp_folder_path", "safe_chars_for_path_param"]),
]

GLOBAL_OPTION_NAMES: frozenset[str] = frozenset(
    name for _, members in GLOBAL_OPTION_GROUPS for name in members
)

# Shorter labels used in the compact (non-root) Global Options table.
# The first column's width is driven by the longest label, so trimming the
# few longest ones widens the right-hand option column on narrow terminals.
_COMPACT_SECTION_LABELS: dict[str, str] = {
    "Pagination / iteration": "Pagination",
}

# Endpoint-local option ``name``s that should render under their own
# "Deprecated" section rather than mixed into the main Options block.
# These options keep working (they emit a stderr warning at runtime and
# resolve to the v3 replacement) but should be visually separated so a
# new reader of ``--help`` doesn't pick them up by mistake.
_DEPRECATED_OPTION_NAMES: frozenset[str] = frozenset({"all_items", "page_size", "max_items"})


def _make_global_option_params() -> list[click.Option]:
    """Build fresh ``click.Option`` instances mirroring the root group's globals.

    Kept in sync with the ``@click.option`` decorators on ``main`` in
    ``cli.py``. Fresh instances are returned each call because click stores
    per-command state on Option objects.
    """
    return [
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
        *(
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
            ]
            if _SDK_HAS_RETRY_STRATEGY
            else []
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
        click.Option(
            ["--access-token", "access_token"],
            default=None,
            help=(
                "Asana personal access token (default: $ASANA_ACCESS_TOKEN). "
                "(Configuration: access_token)"
            ),
        ),
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
        click.Option(
            ["--debug"],
            is_flag=True,
            default=False,
            help=(
                "Print HTTP request/response to stderr for troubleshooting. (Configuration: debug)"
            ),
        ),
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
    ]


def _apply_global_to_runtime(name: str, value: Any) -> None:
    """Write a single global option value to the shared ``runtime`` singleton.

    Mirrors the body of ``main`` in ``cli.py``; if the set of global options
    changes there, update both places.
    """
    if name == "host":
        runtime.host = value
    elif name == "proxy":
        runtime.proxy = value
    elif name == "verify_ssl":
        # Click guarantees the toggle yields True / False once the user
        # picked a side; the None default is filtered out by the
        # COMMANDLINE-source check in ``_consume_global_options``.
        runtime.verify_ssl = value
    elif name == "ssl_ca_cert":
        runtime.ssl_ca_cert = value
    elif name == "cert_file":
        runtime.cert_file = value
    elif name == "key_file":
        runtime.key_file = value
    elif name == "assert_hostname":
        runtime.assert_hostname = value
    elif name == "retry_strategy_overrides":
        runtime.retry_strategy_overrides = value
    elif name == "connection_pool_maxsize":
        runtime.connection_pool_maxsize = value
    elif name == "access_token":
        if value:
            runtime.access_token = value
    elif name == "temp_folder_path":
        runtime.temp_folder_path = value
    elif name == "safe_chars_for_path_param":
        runtime.safe_chars_for_path_param = value
    elif name == "logger_format":
        runtime.logger_format = value
    elif name == "logger_file":
        runtime.logger_file = value
    elif name == "debug":
        runtime.debug = value
    elif name == "return_page_iterator":
        # No ``is not None`` guard needed here (cf. the symmetric guard in
        # ``cli.py:main`` for the root-level decorator): this function is
        # only called from ``_consume_global_options`` after the
        # ``ParameterSource.COMMANDLINE`` check, which excludes the
        # tri-state's ``None`` default value.
        runtime.return_page_iterator = value
    elif name == "page_limit":
        runtime.page_limit = value


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
    """A ``click.Command`` that also accepts the root group's global options."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for opt in _make_global_option_params():
            self.params.append(opt)

    def invoke(self, ctx: click.Context) -> Any:
        _consume_global_options(ctx)
        return super().invoke(ctx)


class GroupWithGlobalOptions(_GlobalOptionsMixin, click.Group):
    """A ``click.Group`` that also accepts the root group's global options.

    Children created via ``@group.command(...)`` default to
    ``CommandWithGlobalOptions`` so they inherit the same behavior.
    """

    command_class = CommandWithGlobalOptions

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for opt in _make_global_option_params():
            self.params.append(opt)

    def invoke(self, ctx: click.Context) -> Any:
        _consume_global_options(ctx)
        return super().invoke(ctx)


class LazyGroup(_GlobalOptionsMixin, click.Group):
    """A ``click.Group`` that loads subcommand modules only when invoked.

    ``lazy_subcommands`` maps a subcommand name to a ``(import_path, short_help)``
    tuple. ``import_path`` is the standard ``"package.module:attr"`` form. The
    ``short_help`` is shown in the parent's command listing without importing
    the target module, which keeps top-level ``--help`` cheap.
    """

    def __init__(
        self,
        *args: Any,
        lazy_subcommands: dict[str, tuple[str, str]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.lazy_subcommands: dict[str, tuple[str, str]] = lazy_subcommands or {}

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted({*self.commands, *self.lazy_subcommands})

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        if cmd_name in self.lazy_subcommands:
            import_path, _ = self.lazy_subcommands[cmd_name]
            modname, _, attr = import_path.partition(":")
            if not attr:
                raise ValueError(
                    f"lazy_subcommands import path must be 'module:attr', got {import_path!r}"
                )
            module = importlib.import_module(modname)
            return getattr(module, attr)
        return super().get_command(ctx, cmd_name)

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        rows: list[tuple[str, str]] = []
        for name in self.list_commands(ctx):
            if name in self.lazy_subcommands:
                _, short_help = self.lazy_subcommands[name]
            else:
                cmd = self.commands.get(name)
                if cmd is None:
                    continue
                short_help = cmd.get_short_help_str()
            rows.append((name, short_help))

        if rows:
            with formatter.section("Commands"):
                formatter.write_dl(rows)
