"""Tests for click_ext: global option propagation through the command tree.

Builds a small click tree mirroring the real CLI shape (root group →
GroupWithGlobalOptions sub-group → CommandWithGlobalOptions leaf) and verifies
that ``--debug`` / ``--host`` / ``--access-token`` etc. work at any level.
"""

from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from asana_api_cli.click_ext import (
    GLOBAL_OPTION_NAMES,
    CommandWithGlobalOptions,
    GroupWithGlobalOptions,
    LazyGroup,
)
from asana_api_cli.session import runtime


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

    def test_globals_not_duplicated_in_options_section(self) -> None:
        result = CliRunner().invoke(_build_cli(), ["tasks", "act", "--help"])
        assert result.exit_code == 0, result.output
        # Each global flag should appear exactly once (under "Global Options").
        assert result.output.count("--debug") == 1
        assert result.output.count("--access-token") == 1

    def test_root_help_does_not_have_global_options_section(self) -> None:
        result = CliRunner().invoke(_build_cli(), ["--help"])
        assert result.exit_code == 0, result.output
        assert "Global Options" not in result.output
        assert "--debug" in result.output


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


class TestCommandClassChain:
    def test_subgroup_command_class_is_command_with_globals(self) -> None:
        cli = _build_cli()
        tasks = cli.commands["tasks"]
        assert isinstance(tasks, click.Group)
        act = tasks.commands["act"]
        assert isinstance(act, CommandWithGlobalOptions)

    def test_group_class_attribute_chain(self) -> None:
        assert GroupWithGlobalOptions.command_class is CommandWithGlobalOptions


def _bash_complete(cmd: click.Command, args: list[str]) -> list[str]:
    """Drive click's bash completion machinery and return the suggested values."""
    from click.shell_completion import ShellComplete

    incomplete = args[-1]
    leading = args[:-1]
    comp = ShellComplete(cmd, {}, "asana-api", "_ASANA_API_COMPLETE")
    items = comp.get_completions(leading, incomplete)
    return [item.value for item in items]
