"""Tests for click_ext: global option propagation through the command tree.

Builds a small click tree mirroring the real CLI shape (root group →
GroupWithGlobalOptions sub-group → CommandWithGlobalOptions leaf) and verifies
that ``--debug`` / ``--host`` / ``--access-token`` etc. work at any level.
"""

from __future__ import annotations

import re

import click
import pytest
from click.testing import CliRunner

from asana_api_cli.click_ext import (
    GLOBAL_OPTION_GROUPS,
    GLOBAL_OPTION_NAMES,
    CommandWithGlobalOptions,
    GroupWithGlobalOptions,
    LazyGroup,
    _COMPACT_SECTION_LABELS,
)
from asana_api_cli.session import runtime


def _count_option_appearances(output: str, flag: str) -> int:
    """Count distinct appearances of ``flag`` as a standalone token in ``output``.

    Used by tests asserting an option is not duplicated in help. Word
    boundaries (``\\b``) ensure ``--foo`` does not match inside ``--foo-bar``,
    and the leading lookbehind for whitespace/start-of-string keeps us from
    matching, e.g., ``--api-key-prefix`` inside the substring ``api-key``
    when checking ``--api-key``.
    """
    pattern = r"(?:^|(?<=\s))" + re.escape(flag) + r"\b"
    return len(re.findall(pattern, output, re.MULTILINE))


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    """Reset the shared runtime singleton between tests."""
    runtime.debug = False
    runtime.host = None
    runtime.proxy = None
    runtime.verify_ssl = None
    runtime.ssl_ca_cert = None
    runtime.cert_file = None
    runtime.key_file = None
    runtime.assert_hostname = None
    runtime.retry_strategy_overrides = None
    runtime.request_timeout = None
    runtime.connection_pool_maxsize = None
    runtime.access_token = None
    runtime.username = None
    runtime.password = None
    runtime.api_key = None
    runtime.api_key_prefix = None
    runtime.temp_folder_path = None
    runtime.safe_chars_for_path_param = None
    runtime.logger_format = None
    runtime.logger_file = None
    runtime.multibyte_filenames = False


def _build_cli() -> click.Group:
    """Build a 3-level CLI mirroring the real ``asana-api`` shape."""

    @click.group(cls=LazyGroup)
    @click.option("--host", default=None)
    @click.option("--debug", is_flag=True, default=False)
    @click.option("--access-token", "access_token", default=None)
    def root(host: str | None, debug: bool, access_token: str | None) -> None:
        runtime.host = host
        runtime.debug = debug
        if access_token:
            runtime.access_token = access_token

    @root.group("tasks", cls=GroupWithGlobalOptions)
    def tasks() -> None:
        pass

    @tasks.command("act")
    def act() -> None:
        click.echo(f"debug={runtime.debug};host={runtime.host};token={runtime.access_token}")

    return root


class TestGlobalOptionsAtAnyLevel:
    def test_debug_on_leaf_command(self) -> None:
        result = CliRunner().invoke(_build_cli(), ["tasks", "act", "--debug"])
        assert result.exit_code == 0, result.output
        assert "debug=True" in result.output

    def test_debug_on_subgroup(self) -> None:
        result = CliRunner().invoke(_build_cli(), ["tasks", "--debug", "act"])
        assert result.exit_code == 0, result.output
        assert "debug=True" in result.output

    def test_debug_on_root_still_works(self) -> None:
        result = CliRunner().invoke(_build_cli(), ["--debug", "tasks", "act"])
        assert result.exit_code == 0, result.output
        assert "debug=True" in result.output

    def test_host_on_leaf_command(self) -> None:
        result = CliRunner().invoke(_build_cli(), ["tasks", "act", "--host", "https://example.com"])
        assert result.exit_code == 0, result.output
        assert "host=https://example.com" in result.output

    def test_access_token_on_leaf_command(self) -> None:
        result = CliRunner().invoke(_build_cli(), ["tasks", "act", "--access-token", "secret-1"])
        assert result.exit_code == 0, result.output
        assert "token=secret-1" in result.output

    def test_leaf_overrides_root(self) -> None:
        result = CliRunner().invoke(
            _build_cli(),
            [
                "--host",
                "https://root.example",
                "tasks",
                "act",
                "--host",
                "https://leaf.example",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "host=https://leaf.example" in result.output

    def test_default_at_leaf_does_not_clobber_root(self) -> None:
        result = CliRunner().invoke(
            _build_cli(), ["--host", "https://root.example", "tasks", "act"]
        )
        assert result.exit_code == 0, result.output
        assert "host=https://root.example" in result.output


class TestHelpRendering:
    def test_global_options_appear_in_subcommand_help(self) -> None:
        result = CliRunner().invoke(_build_cli(), ["tasks", "act", "--help"])
        assert result.exit_code == 0, result.output
        assert "Global Options" in result.output
        assert "--debug" in result.output
        assert "--access-token" in result.output

    def test_globals_appear_exactly_once_in_subcommand_help(self) -> None:
        result = CliRunner().invoke(_build_cli(), ["tasks", "act", "--help"])
        assert result.exit_code == 0, result.output
        for flag in ("--debug", "--access-token", "--username"):
            count = _count_option_appearances(result.output, flag)
            assert count == 1, f"{flag} appears {count} times in:\n{result.output}"

    def test_root_help_does_not_have_global_options_section(self) -> None:
        result = CliRunner().invoke(_build_cli(), ["--help"])
        assert result.exit_code == 0, result.output
        assert "Global Options" not in result.output
        assert "--debug" in result.output

    def test_root_help_shows_group_headings_as_top_level_sections(self) -> None:
        result = CliRunner().invoke(_build_cli(), ["--help"])
        assert result.exit_code == 0, result.output
        # _build_cli() declares --access-token (Authentication), --host
        # (Connection), --debug (Logging / Debug). Only those 3 groups show
        # up; empty groups are skipped.
        for heading in ("Authentication:", "Connection:", "Logging / Debug:"):
            assert heading in result.output, (
                f"Missing top-level section heading {heading!r} in:\n{result.output}"
            )

    def test_subcommand_help_uses_compact_global_options_table(self) -> None:
        # All 21 globals are auto-injected on subcommands via
        # _make_global_option_params(). The compact form renders them as a
        # one-row-per-category table under a "Global Options:" umbrella with
        # a pointer back to `asana-api --help` for descriptions.
        result = CliRunner().invoke(_build_cli(), ["tasks", "act", "--help"])
        assert result.exit_code == 0, result.output
        assert "Global Options:" in result.output
        assert "See `asana-api --help` for descriptions." in result.output
        # Each non-empty group's label appears as the first column of one
        # row in the table (indented under "Global Options:").
        for heading, _ in GLOBAL_OPTION_GROUPS:
            short = _COMPACT_SECTION_LABELS.get(heading, heading)
            assert re.search(rf"^ +{re.escape(short)}\s", result.output, re.MULTILINE), (
                f"Missing compact label {short!r} in:\n{result.output}"
            )
        # The full per-option help text from root (e.g. for --retry-strategy)
        # must NOT be repeated on the subcommand — the compact form is
        # supposed to skip exactly that.
        assert "Override urllib3 Retry fields" not in result.output

    def test_no_op_section_marks_python_asana_version_on_root(self) -> None:
        # The long section heading appears on root help (full detail). On
        # subcommands the compact form abbreviates it via
        # _COMPACT_SECTION_LABELS, so checking the long form there is wrong.
        # We exercise the long form by invoking the *real* CLI's root --help,
        # which declares all 21 globals (the test fixture only declares 3).
        from asana_api_cli.cli import main

        result = CliRunner().invoke(main, ["--help"])
        assert result.exit_code == 0, result.output
        assert "No-op (SDK parity placeholders — inert in python-asana 5.2.4):" in result.output


class TestShellCompletion:
    def test_subcommand_completion_includes_globals(self) -> None:
        completions = _bash_complete(_build_cli(), ["tasks", "act", "--"])
        assert "--debug" in completions
        assert "--access-token" in completions
        assert "--host" in completions


class TestGlobalOptionNamesInventory:
    def test_covers_runtime_fields(self) -> None:
        for name in (
            "debug",
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
            "multibyte_filenames",
        ):
            assert name in GLOBAL_OPTION_NAMES

    def test_every_global_belongs_to_exactly_one_group(self) -> None:
        """Catch the failure mode where a new global is added without a group.

        Such an option would be silently dropped from ``--help`` because the
        grouping renderer only walks names listed in ``GLOBAL_OPTION_GROUPS``.
        """
        seen: dict[str, str] = {}
        for section_name, members in GLOBAL_OPTION_GROUPS:
            for name in members:
                assert name not in seen, (
                    f"{name!r} is in both {seen[name]!r} and {section_name!r} groups"
                )
                seen[name] = section_name
        assert frozenset(seen) == GLOBAL_OPTION_NAMES, (
            f"GLOBAL_OPTION_NAMES vs GLOBAL_OPTION_GROUPS mismatch:\n"
            f"  in NAMES but not in any group: {sorted(GLOBAL_OPTION_NAMES - frozenset(seen))}\n"
            f"  in some group but not in NAMES: {sorted(frozenset(seen) - GLOBAL_OPTION_NAMES)}"
        )


class TestCommandClassChain:
    def test_subgroup_command_class_is_command_with_globals(self) -> None:
        cli = _build_cli()
        tasks = cli.commands["tasks"]
        assert isinstance(tasks, click.Group)
        act = tasks.commands["act"]
        assert isinstance(act, CommandWithGlobalOptions)

    def test_group_class_attribute_chain(self) -> None:
        assert GroupWithGlobalOptions.command_class is CommandWithGlobalOptions


class TestHelpTextSync:
    """The same global options are declared in two places — once on the root
    ``main`` via ``@click.option`` decorators (cli.py) and once via
    ``_make_global_option_params()`` so subcommands can also accept them
    (click_ext.py). The two declarations must stay byte-identical so users
    see the same wording regardless of which level they read ``--help`` at.
    """

    def test_cli_and_click_ext_help_strings_match(self) -> None:
        from asana_api_cli.cli import main
        from asana_api_cli.click_ext import _make_global_option_params

        cli_help = {
            p.name: p.help
            for p in main.params
            if isinstance(p, click.Option) and p.name in GLOBAL_OPTION_NAMES
        }
        ext_help = {
            p.name: p.help
            for p in _make_global_option_params()
            if isinstance(p, click.Option) and p.name in GLOBAL_OPTION_NAMES
        }
        assert cli_help.keys() == ext_help.keys(), (
            f"cli.py vs click_ext.py option name sets differ:\n"
            f"  cli only: {sorted(cli_help.keys() - ext_help.keys())}\n"
            f"  ext only: {sorted(ext_help.keys() - cli_help.keys())}"
        )
        mismatched = {
            name: (cli_help[name], ext_help[name])
            for name in cli_help
            if cli_help[name] != ext_help[name]
        }
        assert not mismatched, "\n".join(
            f"{name}: cli={cli!r} ext={ext!r}" for name, (cli, ext) in mismatched.items()
        )


def _bash_complete(cmd: click.Command, args: list[str]) -> list[str]:
    """Drive click's bash completion machinery and return the suggested values."""
    from click.shell_completion import ShellComplete

    incomplete = args[-1]
    leading = args[:-1]
    comp = ShellComplete(cmd, {}, "asana-api", "_ASANA_API_COMPLETE")
    items = comp.get_completions(leading, incomplete)
    return [item.value for item in items]
