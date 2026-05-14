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


GLOBAL_OPTION_NAMES: frozenset[str] = frozenset(
    {
        "host",
        "proxy",
        "no_verify_ssl",
        "ca_cert",
        "retries",
        "timeout",
        "access_token",
        "temp_dir",
        "debug",
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
            ["--no-verify-ssl"],
            is_flag=True,
            default=False,
            help="Disable TLS certificate verification (insecure)",
        ),
        click.Option(
            ["--ca-cert", "ca_cert"],
            default=None,
            type=click.Path(exists=True, dir_okay=False),
            help="Path to a PEM bundle of trusted CA certificates",
        ),
        click.Option(
            ["--retries"],
            type=int,
            default=None,
            help="Number of retries on 429/5xx responses (default: 5)",
        ),
        click.Option(
            ["--timeout"],
            type=float,
            default=None,
            help="Per-request timeout in seconds",
        ),
        click.Option(
            ["--access-token", "access_token"],
            default=None,
            help="Asana personal access token (default: $ASANA_ACCESS_TOKEN)",
        ),
        click.Option(
            ["--temp-dir", "temp_dir"],
            default=None,
            type=click.Path(file_okay=False),
            help="Directory for temporary downloads",
        ),
        click.Option(
            ["--debug"],
            is_flag=True,
            default=False,
            help="Print HTTP request/response to stderr for troubleshooting",
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
    elif name == "no_verify_ssl":
        runtime.verify_ssl = not value
    elif name == "ca_cert":
        runtime.ssl_ca_cert = value
    elif name == "retries":
        runtime.retries = value
    elif name == "timeout":
        runtime.timeout = value
    elif name == "access_token":
        if value:
            runtime.access_token = value
    elif name == "temp_dir":
        runtime.temp_dir = value
    elif name == "debug":
        runtime.debug = value


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
