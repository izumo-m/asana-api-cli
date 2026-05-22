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

3. Both subclasses also surface those options under a "Global Options" section
   in ``--help`` so they remain discoverable from any subcommand.
"""

from __future__ import annotations

import importlib
from typing import Any

import click
from click.core import ParameterSource

from asana_api_cli.session import runtime
from asana_api_cli.structured_arg import RETRY_FIELD_SCHEMA, click_callback


GLOBAL_OPTION_NAMES: frozenset[str] = frozenset(
    {
        "host",
        "proxy",
        "verify_ssl",
        "ssl_ca_cert",
        "cert_file",
        "key_file",
        "assert_hostname",
        "retry_strategy_overrides",
        "request_timeout",
        "connection_pool_maxsize",
        "access_token",
        "username",
        "password",
        "api_key",
        "api_key_prefix",
        "temp_folder_path",
        "safe_chars_for_path_param",
        "logger_format",
        "logger_file",
        "debug",
        "multibyte_filenames",
    }
)


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
            help="Override API base URL (default: https://app.asana.com/api/1.0)",
        ),
        click.Option(["--proxy"], default=None, help="HTTP/HTTPS proxy URL"),
        click.Option(
            ["--verify-ssl/--no-verify-ssl", "verify_ssl"],
            default=None,
            help=(
                "Set Configuration.verify_ssl (SDK default: True). Pass "
                "--no-verify-ssl to disable TLS certificate verification "
                "(insecure)."
            ),
        ),
        click.Option(
            ["--ssl-ca-cert", "ssl_ca_cert"],
            default=None,
            type=click.Path(exists=True, dir_okay=False),
            help="Set Configuration.ssl_ca_cert (path to a PEM bundle of trusted CA certs).",
        ),
        click.Option(
            ["--cert-file", "cert_file"],
            default=None,
            type=click.Path(exists=True, dir_okay=False),
            help="Set Configuration.cert_file (client TLS certificate for mTLS).",
        ),
        click.Option(
            ["--key-file", "key_file"],
            default=None,
            type=click.Path(exists=True, dir_okay=False),
            help="Set Configuration.key_file (client TLS private key for mTLS).",
        ),
        click.Option(
            ["--assert-hostname/--no-assert-hostname", "assert_hostname"],
            default=None,
            help=(
                "Set Configuration.assert_hostname (SDK default: None → "
                "urllib3 default). Tri-state toggle."
            ),
        ),
        click.Option(
            ["--retry-strategy", "retry_strategy_overrides"],
            default=None,
            callback=click_callback(schema=RETRY_FIELD_SCHEMA),
            help=(
                "Override Configuration.retry_strategy fields. VALUE: "
                "'k1=v1,k2=v2,...', JSON object, or @path. See urllib3 "
                "Retry docs. List-typed fields (allowed_methods, "
                "status_forcelist, remove_headers_on_redirect) require JSON."
            ),
        ),
        click.Option(
            ["--request-timeout", "request_timeout"],
            type=float,
            default=None,
            help="Per-request timeout in seconds (SDK kwarg: _request_timeout).",
        ),
        click.Option(
            ["--connection-pool-maxsize", "connection_pool_maxsize"],
            type=click.IntRange(min=1),
            default=None,
            help=(
                "Set Configuration.connection_pool_maxsize (SDK default: "
                "cpu_count * 5). Max urllib3 connections cached per host."
            ),
        ),
        click.Option(
            ["--access-token", "access_token"],
            default=None,
            help="Asana personal access token (default: $ASANA_ACCESS_TOKEN)",
        ),
        click.Option(
            ["--username", "username"],
            default=None,
            help=(
                "Set Configuration.username (HTTP Basic auth user). No-op "
                "as of python-asana 5.2.4: SDK does not read it in the "
                "request path; Asana only accepts Bearer-token auth (see "
                "--access-token)."
            ),
        ),
        click.Option(
            ["--password", "password"],
            default=None,
            help=(
                "Set Configuration.password (HTTP Basic auth password). "
                "No-op as of python-asana 5.2.4: same reason as --username."
            ),
        ),
        click.Option(
            ["--api-key", "api_key"],
            default=None,
            callback=click_callback(),
            help=(
                "Set Configuration.api_key (dict). VALUE: "
                "'k1=v1,k2=v2,...', JSON object, or @path. No-op as of "
                "python-asana 5.2.4: SDK only uses personalAccessToken auth."
            ),
        ),
        click.Option(
            ["--api-key-prefix", "api_key_prefix"],
            default=None,
            callback=click_callback(),
            help=(
                "Set Configuration.api_key_prefix (dict). Same input "
                "format as --api-key. No-op as of python-asana 5.2.4."
            ),
        ),
        click.Option(
            ["--temp-folder-path", "temp_folder_path"],
            default=None,
            type=click.Path(file_okay=False),
            help="Set Configuration.temp_folder_path (directory for temporary downloads).",
        ),
        click.Option(
            ["--safe-chars-for-path-param", "safe_chars_for_path_param"],
            default=None,
            help=(
                "Set Configuration.safe_chars_for_path_param (extra chars "
                "treated as safe when percent-encoding path parameters)."
            ),
        ),
        click.Option(
            ["--logger-format", "logger_format"],
            default=None,
            help="Set Configuration.logger_format (Python logging format string).",
        ),
        click.Option(
            ["--logger-file", "logger_file"],
            default=None,
            type=click.Path(dir_okay=False),
            help="Set Configuration.logger_file (path SDK loggers write to when set).",
        ),
        click.Option(
            ["--debug"],
            is_flag=True,
            default=False,
            help="Print HTTP request/response to stderr for troubleshooting",
        ),
        click.Option(
            ["--multibyte-filenames", "multibyte_filenames"],
            is_flag=True,
            default=False,
            help=(
                "Emit RFC 5987 filename*=UTF-8'' on multipart uploads. Required for "
                "attachment uploads whose filename contains non-ASCII characters; "
                "off by default to match the underlying SDK behavior. "
                "[asana-api extension]"
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
    elif name == "request_timeout":
        runtime.request_timeout = value
    elif name == "connection_pool_maxsize":
        runtime.connection_pool_maxsize = value
    elif name == "access_token":
        if value:
            runtime.access_token = value
    elif name == "username":
        runtime.username = value
    elif name == "password":
        runtime.password = value
    elif name == "api_key":
        runtime.api_key = value
    elif name == "api_key_prefix":
        runtime.api_key_prefix = value
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
    elif name == "multibyte_filenames":
        runtime.multibyte_filenames = value


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


class _GlobalOptionsMixin:
    """Render injected global options under a separate "Global Options" section."""

    def format_options(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        is_root = ctx.parent is None

        # Regular options. On non-root commands, suppress globals here so they
        # are not duplicated with the dedicated "Global Options" section below.
        opts: list[tuple[str, str]] = []
        for param in self.get_params(ctx):  # type: ignore[attr-defined]
            if not is_root and param.name in GLOBAL_OPTION_NAMES:
                continue
            record = param.get_help_record(ctx)
            if record is not None:
                opts.append(record)
        if opts:
            with formatter.section("Options"):
                formatter.write_dl(opts)

        # Mimic ``click.MultiCommand.format_options`` by listing subcommands
        # for groups; this branch is dead for plain commands.
        if isinstance(self, click.Group):
            self.format_commands(ctx, formatter)

        if is_root:
            return

        records: list[tuple[str, str]] = []
        for param in self.params:  # type: ignore[attr-defined]
            if isinstance(param, click.Option) and param.name in GLOBAL_OPTION_NAMES:
                record = param.get_help_record(ctx)
                if record is not None:
                    records.append(record)
        if records:
            with formatter.section("Global Options"):
                formatter.write_dl(records)


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
