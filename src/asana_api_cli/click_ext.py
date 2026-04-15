"""Click extensions: lazy subcommand loading and global-option help injection.

Two concerns live here:

1. ``LazyGroup`` — a ``click.Group`` that resolves subcommands on demand by
   import path, so ``--help`` and tab completion do not need to import every
   ``*Api`` module up front.

2. ``GroupWithGlobalOptions`` / ``CommandWithGlobalOptions`` — surface the
   root group's options under a "Global Options" section in every
   subcommand/subgroup ``--help`` output, so users can discover ``--access-token``
   and friends from any level of the CLI tree.
"""

from __future__ import annotations

import importlib
from typing import Any

import click


class _GlobalOptionsMixin:
    """Append the root command's options to ``--help`` as "Global Options"."""

    def format_options(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        super().format_options(ctx, formatter)  # type: ignore[misc]

        root_ctx = ctx
        while root_ctx.parent is not None:
            root_ctx = root_ctx.parent
        if root_ctx is ctx:
            return

        records: list[tuple[str, str]] = []
        for param in root_ctx.command.params:
            if not isinstance(param, click.Option):
                continue
            record = param.get_help_record(root_ctx)
            if record is not None:
                records.append(record)

        if records:
            with formatter.section("Global Options"):
                formatter.write_dl(records)


class CommandWithGlobalOptions(_GlobalOptionsMixin, click.Command):
    """A ``click.Command`` that lists global options in its ``--help``."""


class GroupWithGlobalOptions(_GlobalOptionsMixin, click.Group):
    """A ``click.Group`` that lists global options in its ``--help``.

    Children created via ``@group.command(...)`` default to
    ``CommandWithGlobalOptions`` so they inherit the same behavior.
    """

    command_class = CommandWithGlobalOptions


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
