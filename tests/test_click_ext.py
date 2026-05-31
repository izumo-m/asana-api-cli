"""Tests for click_ext: global option propagation through the command tree.

Builds a small click tree mirroring the real CLI shape (root group →
GroupWithGlobalOptions sub-group → CommandWithGlobalOptions leaf) and verifies
that ``--debug`` / ``--host`` / ``--access-token`` etc. work at any level.
"""

from __future__ import annotations

import re
from typing import Any

import click
from _cli_runner import full_output, make_runner

from asana_api_cli.click_ext import (
    _COMPACT_SECTION_LABELS,
    GLOBAL_OPTION_GROUPS,
    GLOBAL_OPTION_NAMES,
    CommandWithGlobalOptions,
    GroupWithGlobalOptions,
    LazyGroup,
)
from asana_api_cli.session import runtime


def _count_option_appearances(output: str, flag: str) -> int:
    """Count distinct appearances of ``flag`` as a standalone token in ``output``.

    Used by tests asserting an option is not duplicated in help. The leading
    whitespace/start-of-string lookbehind plus the trailing word boundary
    (``\\b``) match ``flag`` only as a standalone token, not as a substring of
    a longer flag name or value.
    """
    pattern = r"(?:^|(?<=\s))" + re.escape(flag) + r"\b"
    return len(re.findall(pattern, output, re.MULTILINE))


# Runtime isolation between tests is provided by the autouse
# ``_reset_runtime`` fixture in ``tests/conftest.py``. It snapshots all
# ``_Runtime`` fields via ``dataclasses.fields`` so new fields are picked
# up automatically — no per-file maintenance needed here.


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
        result = make_runner().invoke(_build_cli(), ["tasks", "act", "--debug"])
        assert result.exit_code == 0, full_output(result)
        assert "debug=True" in full_output(result)

    def test_debug_on_subgroup(self) -> None:
        result = make_runner().invoke(_build_cli(), ["tasks", "--debug", "act"])
        assert result.exit_code == 0, full_output(result)
        assert "debug=True" in full_output(result)

    def test_debug_on_root_still_works(self) -> None:
        result = make_runner().invoke(_build_cli(), ["--debug", "tasks", "act"])
        assert result.exit_code == 0, full_output(result)
        assert "debug=True" in full_output(result)

    def test_host_on_leaf_command(self) -> None:
        result = make_runner().invoke(
            _build_cli(), ["tasks", "act", "--host", "https://example.com"]
        )
        assert result.exit_code == 0, full_output(result)
        assert "host=https://example.com" in full_output(result)

    def test_access_token_on_leaf_command(self) -> None:
        result = make_runner().invoke(_build_cli(), ["tasks", "act", "--access-token", "secret-1"])
        assert result.exit_code == 0, full_output(result)
        assert "token=secret-1" in full_output(result)

    def test_leaf_overrides_root(self) -> None:
        result = make_runner().invoke(
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
        assert result.exit_code == 0, full_output(result)
        assert "host=https://leaf.example" in full_output(result)

    def test_default_at_leaf_does_not_clobber_root(self) -> None:
        result = make_runner().invoke(
            _build_cli(), ["--host", "https://root.example", "tasks", "act"]
        )
        assert result.exit_code == 0, full_output(result)
        assert "host=https://root.example" in full_output(result)


class TestHelpRendering:
    def test_global_options_appear_in_subcommand_help(self) -> None:
        result = make_runner().invoke(_build_cli(), ["tasks", "act", "--help"])
        assert result.exit_code == 0, full_output(result)
        assert "Global Options" in full_output(result)
        assert "--debug" in full_output(result)
        assert "--access-token" in full_output(result)

    def test_globals_appear_exactly_once_in_subcommand_help(self) -> None:
        result = make_runner().invoke(_build_cli(), ["tasks", "act", "--help"])
        assert result.exit_code == 0, full_output(result)
        for flag in ("--debug", "--access-token", "--host"):
            count = _count_option_appearances(full_output(result), flag)
            assert count == 1, f"{flag} appears {count} times in:\n{full_output(result)}"

    def test_root_help_does_not_have_global_options_section(self) -> None:
        result = make_runner().invoke(_build_cli(), ["--help"])
        assert result.exit_code == 0, full_output(result)
        assert "Global Options" not in full_output(result)
        assert "--debug" in full_output(result)

    def test_root_help_shows_group_headings_as_top_level_sections(self) -> None:
        result = make_runner().invoke(_build_cli(), ["--help"])
        assert result.exit_code == 0, full_output(result)
        # _build_cli() declares --access-token (Authentication), --host
        # (Connection), --debug (Logging / Debug). Only those 3 groups show
        # up; empty groups are skipped.
        for heading in ("Authentication:", "Connection:", "Logging / Debug:"):
            assert heading in full_output(result), (
                f"Missing top-level section heading {heading!r} in:\n{full_output(result)}"
            )

    def test_subcommand_help_uses_compact_global_options_table(self) -> None:
        # Every global is auto-injected on subcommands via
        # _make_global_option_params(). The compact form renders them as a
        # one-row-per-category table under a "Global Options:" umbrella with
        # a pointer back to `asana-api --help` for descriptions.
        result = make_runner().invoke(_build_cli(), ["tasks", "act", "--help"])
        assert result.exit_code == 0, full_output(result)
        assert "Global Options:" in full_output(result)
        assert "See `asana-api --help` for descriptions." in full_output(result)
        # Each non-empty group's label appears as the first column of one
        # row in the table (indented under "Global Options:").
        for heading, _ in GLOBAL_OPTION_GROUPS:
            short = _COMPACT_SECTION_LABELS.get(heading, heading)
            assert re.search(rf"^ +{re.escape(short)}\s", full_output(result), re.MULTILINE), (
                f"Missing compact label {short!r} in:\n{full_output(result)}"
            )
        # The full per-option help text from root (e.g. for --retry-strategy)
        # must NOT be repeated on the subcommand — the compact form is
        # supposed to skip exactly that.
        assert "Override urllib3 Retry fields" not in full_output(result)


class TestShellCompletion:
    def test_subcommand_completion_includes_globals(self) -> None:
        completions = _bash_complete(_build_cli(), ["tasks", "act", "--"])
        assert "--debug" in completions
        assert "--access-token" in completions
        assert "--host" in completions


class TestGlobalOptionNamesInventory:
    def test_covers_runtime_fields(self) -> None:
        from asana_api_cli.click_ext import _SDK_HAS_RETRY_STRATEGY

        expected_names = [
            "debug",
            "host",
            "proxy",
            "verify_ssl",
            "ssl_ca_cert",
            "cert_file",
            "key_file",
            "assert_hostname",
            "connection_pool_maxsize",
            "access_token",
            "temp_folder_path",
            "safe_chars_for_path_param",
            "logger_format",
            "logger_file",
            # ApiClient-instance settings (not Configuration knobs): applied to
            # the ApiClient after construction. Session-wide, unlike the
            # per-call --header-params opt.
            "user_agent",
            "default_headers",
            # Configuration-backed iterator knobs stay global. The per-call
            # kwargs (item_limit / full_payload / header_params /
            # _request_timeout) are per-command options now, not globals.
            # multibyte_filenames is also no longer global — it is a per-command
            # option on upload commands (see test_cli.py).
            "return_page_iterator",
            "page_limit",
            # The error output controls (exception_output / exception_query) are NOT
            # global: they are per-command formatter options on every leaf
            # command, symmetric with --output / --query. See test_formatter.py
            # and docs/cli-sdk-mapping.md "Output formatter options".
        ]
        if _SDK_HAS_RETRY_STRATEGY:
            expected_names.append("retry_strategy_overrides")
        for name in expected_names:
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

    def test_cli_and_click_ext_full_signature_match(self) -> None:
        """Help text alone is not enough: the two declaration sites must also
        agree on flag spelling, default, flag-ness, and **type** — a
        ``click.IntRange(min=1)`` or ``click.Path(exists=True, ...)`` that
        drifts on only one side would make the same flag validate/coerce
        differently at the root vs. on a subcommand, silently, while the
        help-text check above still passes. This is what the "byte-identical"
        claims in cli.py / click_ext.py / docs/architecture.md actually
        promise, so enforce the whole signature.
        """
        from asana_api_cli.cli import main
        from asana_api_cli.click_ext import _make_global_option_params

        def _type_fingerprint(t: click.ParamType) -> tuple[Any, ...]:
            parts: list[Any] = [type(t).__name__]
            for attr in ("min", "max", "clamp", "exists", "dir_okay", "file_okay"):
                if hasattr(t, attr):
                    parts.append((attr, getattr(t, attr)))
            if isinstance(t, click.Choice):
                parts.append(("choices", tuple(t.choices)))
            return tuple(parts)

        def _sig(p: click.Option) -> tuple[Any, ...]:
            return (
                p.help,
                tuple(p.opts),
                tuple(p.secondary_opts),
                p.default,
                p.is_flag,
                _type_fingerprint(p.type),
            )

        cli_sig = {
            p.name: _sig(p)
            for p in main.params
            if isinstance(p, click.Option) and p.name in GLOBAL_OPTION_NAMES
        }
        ext_sig = {
            p.name: _sig(p)
            for p in _make_global_option_params()
            if isinstance(p, click.Option) and p.name in GLOBAL_OPTION_NAMES
        }
        assert cli_sig.keys() == ext_sig.keys()
        mismatched = {
            name: (cli_sig[name], ext_sig[name])
            for name in cli_sig
            if cli_sig[name] != ext_sig[name]
        }
        assert not mismatched, "\n".join(
            f"{name}:\n  cli={cli!r}\n  ext={ext!r}" for name, (cli, ext) in mismatched.items()
        )


def _bash_complete(cmd: click.Command, args: list[str]) -> list[str]:
    """Drive click's bash completion machinery and return the suggested values."""
    from click.shell_completion import ShellComplete

    incomplete = args[-1]
    leading = args[:-1]
    comp = ShellComplete(cmd, {}, "asana-api", "_ASANA_API_COMPLETE")
    items = comp.get_completions(leading, incomplete)
    return [item.value for item in items]
