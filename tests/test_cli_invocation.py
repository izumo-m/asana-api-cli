"""End-to-end tests for the CLI -> SDK invocation path.

Each test builds a real command via ``_make_command`` against a real
``*Api`` class, replaces the underlying SDK method with a ``MagicMock``,
drives the command through ``CliRunner``, and asserts on what reached the
SDK (positional args, opts dict, kwargs, call count).

This catches bugs in the argument-plumbing layer that structural tests
miss -- for example forgetting to forward ``--max-items`` as the SDK's
``item_limit`` kwarg.
"""

from __future__ import annotations

import copy
import http.client
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import asana
import click
import pytest
from _cli_runner import full_output, make_runner

from asana_api_cli.cli import _enumerate_api_classes, _make_command, _operations_for
from asana_api_cli.click_ext import _SDK_HAS_RETRY_STRATEGY
from asana_api_cli.session import runtime

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Provide a token and clear ``runtime`` between tests.

    ``AsanaSession.from_env`` exits if no token is set. We also snapshot and
    restore the process-wide ``runtime`` dataclass so flags set by one test
    cannot leak into the next.
    """
    monkeypatch.setenv("ASANA_ACCESS_TOKEN", "test-token")
    monkeypatch.delenv("ASANA_DEFAULT_WORKSPACE", raising=False)
    saved = {
        name: getattr(runtime, name)
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
        )
    }
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(runtime, name, value)


def _build_command(api_cls_name: str, method_name: str) -> click.Command:
    """Build the real CLI command bound to ``asana.<api_cls_name>.<method_name>``."""
    api_cls = next(c for c in _enumerate_api_classes() if c.__name__ == api_cls_name)
    op = next(o for o in _operations_for(api_cls) if o.method_name == method_name)
    return _make_command(api_cls, op)


def _page(items: list[dict[str, Any]], offset: str | None = None) -> dict[str, Any]:
    """Build a single SDK page response (``{"data": ..., "next_page": ...}``)."""
    return {
        "data": items,
        "next_page": {"offset": offset} if offset else None,
    }


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    api_cls_name: str,
    method_name: str,
    *,
    side_effect: list[Any] | None = None,
    return_value: Any = None,
) -> MagicMock:
    """Replace an SDK method with a ``MagicMock`` and return the mock.

    ``MagicMock.call_args_list[i].args`` reflects the arguments the CLI
    passed in (without ``self``, since the stub strips it). Mutable args
    (the ``opts`` dict) are deep-copied before being recorded so each
    call's snapshot survives any later in-place mutation.
    """
    api_cls = getattr(asana, api_cls_name)
    mock = MagicMock(
        side_effect=side_effect,
        return_value=return_value if side_effect is None else None,
    )

    def _stub(self: Any, *args: Any, **kwargs: Any) -> Any:
        snapped = tuple(copy.deepcopy(a) if isinstance(a, dict) else a for a in args)
        return mock(*snapped, **kwargs)

    monkeypatch.setattr(api_cls, method_name, _stub)
    return mock


# ---------------------------------------------------------------------------
# v3 primary pagination flags (1:1 with SDK inputs)
# ---------------------------------------------------------------------------


def _capture_configuration(
    monkeypatch: pytest.MonkeyPatch, return_value: Any
) -> list[tuple[bool, int]]:
    """Patch ``TasksApi.get_tasks`` to snapshot
    ``(return_page_iterator, page_limit)`` at call time."""
    captured: list[tuple[bool, int]] = []

    def patched(self_api: Any, opts: Any, **kwargs: Any) -> Any:
        cfg = self_api.api_client.configuration
        captured.append((cfg.return_page_iterator, cfg.page_limit))
        return return_value

    monkeypatch.setattr(asana.TasksApi, "get_tasks", patched)
    return captured


class TestItemLimitKwarg:
    """``--item-limit N`` is forwarded as the SDK's ``item_limit`` kwarg.

    The SDK's ``PageIterator`` then caps each per-request ``limit`` to
    ``min(page_limit, item_limit - count)`` and stops at exactly N items.
    """

    def test_item_limit_passes_kwarg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(
            monkeypatch,
            "TasksApi",
            "get_tasks",
            return_value=iter([{"gid": str(i)} for i in range(250)]),
        )
        result = make_runner().invoke(cmd, ["--item-limit", "250"])
        assert result.exit_code == 0, full_output(result)
        assert mock.call_count == 1
        assert mock.call_args_list[0].kwargs == {"item_limit": 250}
        # CLI does not push ``limit`` into opts; that's the SDK's job.
        assert "limit" not in mock.call_args_list[0].args[0]
        assert len(json.loads(full_output(result))) == 250

    def test_item_limit_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--item-limit 0`` still constructs a session and calls the SDK;
        the SDK's PageIterator short-circuits and yields nothing."""
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(monkeypatch, "TasksApi", "get_tasks", return_value=iter([]))
        result = make_runner().invoke(cmd, ["--item-limit", "0"])
        assert result.exit_code == 0, full_output(result)
        assert mock.call_args_list[0].kwargs == {"item_limit": 0}
        assert json.loads(full_output(result)) == []


class TestPageLimitFlag:
    """``--page-limit N`` sets ``Configuration.page_limit = N``.

    Used by the SDK to populate ``query_params['limit']`` when the caller
    did not set ``opts["limit"]``.
    """

    def test_page_limit_sets_configuration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = _build_command("TasksApi", "get_tasks")
        captured = _capture_configuration(monkeypatch, return_value=iter([]))
        result = make_runner().invoke(cmd, ["--page-limit", "50"])
        assert result.exit_code == 0, full_output(result)
        assert captured == [(True, 50)]

    def test_no_page_limit_keeps_sdk_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without ``--page-limit``, ``Configuration.page_limit`` stays at
        the SDK default (100)."""
        cmd = _build_command("TasksApi", "get_tasks")
        captured = _capture_configuration(monkeypatch, return_value=iter([]))
        result = make_runner().invoke(cmd, [])
        assert result.exit_code == 0, full_output(result)
        assert captured == [(True, 100)]


class TestV3PrimaryFlags:
    """v3 flags that map 1:1 to SDK ``Configuration`` properties, ``opts``
    keys, and method kwargs."""

    def test_limit_reaches_opts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--limit N`` lands in ``opts["limit"]`` (sent as ``?limit=N``)."""
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(monkeypatch, "TasksApi", "get_tasks", return_value=_page([{"gid": "1"}]))
        result = make_runner().invoke(cmd, ["--limit", "50", "--no-return-page-iterator"])
        assert result.exit_code == 0, full_output(result)
        assert mock.call_args_list[0].args[0]["limit"] == 50

    def test_offset_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--offset TOKEN`` reaches ``opts["offset"]`` on a single request."""
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(monkeypatch, "TasksApi", "get_tasks", return_value=_page([]))
        result = make_runner().invoke(cmd, ["--offset", "abc123"])
        assert result.exit_code == 0, full_output(result)
        assert mock.call_args_list[0].args[0]["offset"] == "abc123"

    def test_no_return_page_iterator_flips_configuration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--no-return-page-iterator`` sets
        ``Configuration.return_page_iterator = False`` for that call."""
        cmd = _build_command("TasksApi", "get_tasks")
        captured = _capture_configuration(monkeypatch, return_value=_page([]))
        result = make_runner().invoke(cmd, ["--no-return-page-iterator"])
        assert result.exit_code == 0, full_output(result)
        assert captured == [(False, 100)]

    def test_full_payload_passes_kwarg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--full-payload`` adds ``full_payload=True`` to the SDK call."""
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(monkeypatch, "TasksApi", "get_tasks", return_value=_page([]))
        result = make_runner().invoke(cmd, ["--full-payload"])
        assert result.exit_code == 0, full_output(result)
        assert mock.call_args_list[0].kwargs == {"full_payload": True}

    def test_default_keeps_iterator_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without any pagination flag, ``Configuration.return_page_iterator``
        stays at the SDK default (True) and no ``limit`` is pushed into opts."""
        cmd = _build_command("TasksApi", "get_tasks")
        captured = _capture_configuration(monkeypatch, return_value=iter([]))
        result = make_runner().invoke(cmd, [])
        assert result.exit_code == 0, full_output(result)
        assert captured == [(True, 100)]

    def test_paginate_alias_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--paginate`` was removed in 2.1.0; the option is no longer accepted."""
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(monkeypatch, "TasksApi", "get_tasks", return_value=_page([]))
        result = make_runner().invoke(cmd, ["--paginate"])
        assert result.exit_code != 0
        assert "No such option: --paginate" in full_output(result)
        assert mock.call_count == 0


# ---------------------------------------------------------------------------
# v2 → v3 deprecation aliases
# ---------------------------------------------------------------------------


class TestDeprecationAliases:
    """``--all-items``, ``--page-size``, ``--max-items`` are retained as
    deprecation aliases. Each emits a stderr warning and forwards to its v3
    equivalent (or no-ops when the behavior is now the default).
    """

    def test_all_items_warns_and_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(
            monkeypatch,
            "TasksApi",
            "get_tasks",
            return_value=iter([{"gid": "1"}, {"gid": "2"}]),
        )
        result = make_runner().invoke(cmd, ["--all-items"])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "--all-items is deprecated" in result.stderr
        # No-op alias: SDK call should look identical to passing no flag.
        assert mock.call_args_list[0].kwargs == {}
        assert "limit" not in mock.call_args_list[0].args[0]
        assert json.loads(result.stdout) == [{"gid": "1"}, {"gid": "2"}]

    def test_page_size_warns_and_forwards_to_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(monkeypatch, "TasksApi", "get_tasks", return_value=iter([]))
        result = make_runner().invoke(cmd, ["--page-size", "50"])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "--page-size is deprecated" in result.stderr
        # Forwarded to ``opts["limit"]`` (the v3 --limit destination).
        assert mock.call_args_list[0].args[0]["limit"] == 50

    def test_max_items_warns_and_forwards_to_item_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(monkeypatch, "TasksApi", "get_tasks", return_value=iter([]))
        result = make_runner().invoke(cmd, ["--max-items", "100"])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "--max-items is deprecated" in result.stderr
        # Forwarded to the ``item_limit`` kwarg (the v3 --item-limit destination).
        assert mock.call_args_list[0].kwargs == {"item_limit": 100}

    def test_page_size_with_limit_is_usage_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(monkeypatch, "TasksApi", "get_tasks", return_value=_page([]))
        result = make_runner().invoke(cmd, ["--page-size", "50", "--limit", "100"])
        assert result.exit_code != 0
        assert "alias of --limit" in full_output(result)
        assert mock.call_count == 0

    def test_max_items_with_item_limit_is_usage_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(monkeypatch, "TasksApi", "get_tasks", return_value=_page([]))
        result = make_runner().invoke(cmd, ["--max-items", "100", "--item-limit", "200"])
        assert result.exit_code != 0
        assert "alias of --item-limit" in full_output(result)
        assert mock.call_count == 0


# ---------------------------------------------------------------------------
# Debug redactor lifecycle across pagination
# ---------------------------------------------------------------------------


class TestGlobalOptionValidation:
    """Type/range validation on global options that affect runtime state."""

    def test_retry_strategy_total_zero_accepted(self) -> None:
        """``--retry-strategy total=0`` parses fine (disables retries explicitly)."""
        from asana_api_cli.cli import main

        result = make_runner().invoke(main, ["--retry-strategy", "total=0"])
        # Either succeeds (showing help) or fails with "Missing command",
        # but never with a parser error.
        assert "Invalid value" not in full_output(result)

    @pytest.mark.skipif(
        not _SDK_HAS_RETRY_STRATEGY,
        reason="--retry-strategy is hidden on python-asana <5.1",
    )
    def test_retry_strategy_rejects_unknown_field(self) -> None:
        """Unknown retry fields must be rejected before any SDK call."""
        from asana_api_cli.cli import main

        result = make_runner().invoke(main, ["--retry-strategy", "bogus=1"])
        assert result.exit_code != 0
        assert "Unknown field" in full_output(result)

    @pytest.mark.skipif(
        not _SDK_HAS_RETRY_STRATEGY,
        reason="--retry-strategy is hidden on python-asana <5.1",
    )
    def test_retry_strategy_list_field_in_shorthand_rejected(self) -> None:
        """List-typed fields require the JSON form."""
        from asana_api_cli.cli import main

        result = make_runner().invoke(main, ["--retry-strategy", "status_forcelist=429"])
        assert result.exit_code != 0
        assert "list type" in full_output(result)

    def test_connection_pool_maxsize_rejects_zero(self) -> None:
        from asana_api_cli.cli import main

        result = make_runner().invoke(main, ["--connection-pool-maxsize", "0"])
        assert result.exit_code != 0
        assert "Invalid value" in full_output(result) or "is not in the range" in full_output(
            result
        )


# ---------------------------------------------------------------------------
# Tri-state toggle plumbing (--verify-ssl/--no-verify-ssl,
# --assert-hostname/--no-assert-hostname)
# ---------------------------------------------------------------------------


class TestTriStateToggles:
    """Toggles with ``default=None`` must distinguish three states:
    explicit True (user passed the positive form), explicit False (user
    passed the negative form), and unset (user passed neither).

    ``main()`` guards the unset case so the runtime singleton is not
    clobbered with ``None`` over a value an earlier code path may have
    set. ``AsanaSession`` mirrors the guard so it only writes
    ``config.<prop>`` when the runtime carries an explicit value.

    These tests invoke ``main`` with a subcommand path ending in
    ``--help``: Click runs the root group callback (which writes to
    ``runtime``) before descending into the subcommand, and the leaf
    ``--help`` then short-circuits without touching the SDK — exactly
    what we want for an option-plumbing test.
    """

    def test_verify_ssl_explicit_true(self) -> None:
        from asana_api_cli.cli import main

        runtime.verify_ssl = None
        result = make_runner().invoke(main, ["--verify-ssl", "tasks", "get-task", "--help"])
        assert result.exit_code == 0, full_output(result)
        assert runtime.verify_ssl is True

    def test_no_verify_ssl_explicit_false(self) -> None:
        from asana_api_cli.cli import main

        runtime.verify_ssl = None
        result = make_runner().invoke(main, ["--no-verify-ssl", "tasks", "get-task", "--help"])
        assert result.exit_code == 0, full_output(result)
        assert runtime.verify_ssl is False

    def test_verify_ssl_unset_does_not_clobber_existing_runtime_value(self) -> None:
        """Regression for the round-1 review fix: when ``main()`` runs
        without either side of the toggle, it must NOT overwrite a value
        already in ``runtime.verify_ssl`` (set by a higher-priority source).
        Pre-set to False, invoke main with no toggle on the command line;
        the guard must leave it intact."""
        from asana_api_cli.cli import main

        runtime.verify_ssl = False
        result = make_runner().invoke(main, ["tasks", "get-task", "--help"])
        assert result.exit_code == 0, full_output(result)
        assert runtime.verify_ssl is False

    def test_assert_hostname_explicit_true(self) -> None:
        from asana_api_cli.cli import main

        runtime.assert_hostname = None
        result = make_runner().invoke(main, ["--assert-hostname", "tasks", "get-task", "--help"])
        assert result.exit_code == 0, full_output(result)
        assert runtime.assert_hostname is True

    def test_no_assert_hostname_explicit_false(self) -> None:
        from asana_api_cli.cli import main

        runtime.assert_hostname = None
        result = make_runner().invoke(main, ["--no-assert-hostname", "tasks", "get-task", "--help"])
        assert result.exit_code == 0, full_output(result)
        assert runtime.assert_hostname is False

    def test_assert_hostname_unset_does_not_clobber_existing_runtime_value(self) -> None:
        """Same regression shape as ``verify_ssl`` — round-2 review added
        the ``is not None`` guard to ``main()`` for symmetry."""
        from asana_api_cli.cli import main

        runtime.assert_hostname = True
        result = make_runner().invoke(main, ["tasks", "get-task", "--help"])
        assert result.exit_code == 0, full_output(result)
        assert runtime.assert_hostname is True


# ---------------------------------------------------------------------------
# --retry-strategy reaches session.py and exercises Retry.new()
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _SDK_HAS_RETRY_STRATEGY,
    reason="--retry-strategy is hidden on python-asana <5.1",
)
class TestRetryStrategyReachesSession:
    """End-to-end plumbing for ``--retry-strategy``: the parsed overrides
    must reach ``AsanaSession.__init__`` and be applied via
    ``Configuration.retry_strategy.new(**overrides)``.

    Tests invoke a leaf command built by ``_build_command`` so the
    global-option pickup goes through ``CommandWithGlobalOptions.invoke``
    (which is what powers the leaf-level reach of every global option)
    rather than through ``main``. The retry override semantics are
    identical in both paths.
    """

    def test_empty_object_still_invokes_new(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression for the round-1 fix changing ``if runtime...:`` to
        ``if runtime... is not None:``. Passing ``--retry-strategy '{}'``
        is an intentional "user did pass the flag" signal — the session
        must call ``.new()`` (even though the resulting Retry is
        observationally identical to the SDK default)."""
        from urllib3.util.retry import Retry

        new_kwargs_seen: list[dict[str, Any]] = []
        original_new = Retry.new

        def spy_new(self: Retry, **kw: Any) -> Retry:
            new_kwargs_seen.append(kw)
            return original_new(self, **kw)

        monkeypatch.setattr(Retry, "new", spy_new)

        cmd = _build_command("TasksApi", "get_task")
        _patch(monkeypatch, "TasksApi", "get_task", return_value={"data": {}})
        result = make_runner().invoke(cmd, ["--retry-strategy", "{}", "--task", "T"])
        assert result.exit_code == 0, full_output(result)
        # The mocked SDK call never triggers urllib3's own retry path, so
        # the only ``.new(**{})`` call here is the one in session.py — and
        # it had to happen, otherwise the round-1 truthy-check regression
        # would resurface.
        assert {} in new_kwargs_seen

    def test_overrides_propagate_to_configuration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--retry-strategy total=7,backoff_factor=1.5`` reaches the SDK
        as a ``retry_strategy`` instance with the overridden fields, while
        list-typed defaults (``status_forcelist``) are preserved by the
        ``.new()`` partial-override semantics."""
        cmd = _build_command("TasksApi", "get_task")
        captured: list[Any] = []

        def patched(self_api: Any, *args: Any, **kwargs: Any) -> Any:
            captured.append(self_api.api_client.configuration.retry_strategy)
            return {"data": {}}

        monkeypatch.setattr(asana.TasksApi, "get_task", patched)
        result = make_runner().invoke(
            cmd,
            ["--retry-strategy", "total=7,backoff_factor=1.5", "--task", "T"],
        )
        assert result.exit_code == 0, full_output(result)
        assert len(captured) == 1
        rs = captured[0]
        assert rs.total == 7
        assert rs.backoff_factor == 1.5
        # SDK default not touched by the partial override.
        assert rs.status_forcelist == [429, 500, 502, 503, 504]


class TestDebugRedactorLifecycle:
    """The http.client debug redactor must stay installed for the duration
    of every paginated request, including the lazy per-page HTTP calls that
    `--all-items` triggers when the formatter iterates the SDK's
    PageIterator."""

    def test_all_items_with_debug_keeps_redactor_installed_during_iteration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression for v2.1.0: ``--all-items`` must consume the SDK
        PageIterator inside the ``AsanaSession`` ``with`` block. If the
        iterator is consumed after the session exits, the debug redactor
        is already gone and pages past the first leak the raw
        Authorization header to stderr.
        """
        redactor_states: list[bool] = []

        def _generator() -> Iterator[dict[str, Any]]:
            for i in range(3):
                current = http.client.__dict__.get("print")
                redactor_states.append(getattr(current, "_http_client_auth_redactor", False))
                yield {"gid": str(i)}

        cmd = _build_command("TasksApi", "get_tasks")
        _patch(monkeypatch, "TasksApi", "get_tasks", return_value=_generator())
        monkeypatch.setattr(runtime, "debug", True)

        saved_print = http.client.__dict__.get("print")
        saved_debuglevel = http.client.HTTPConnection.debuglevel
        try:
            result = make_runner().invoke(cmd, ["--all-items"])
            assert result.exit_code == 0, full_output(result)
            assert len(redactor_states) == 3
            assert all(redactor_states), (
                "redactor must be installed during every PageIterator yield; "
                f"observed states={redactor_states}"
            )
        finally:
            if saved_print is None:
                http.client.__dict__.pop("print", None)
            else:
                http.client.print = saved_print  # pyright: ignore[reportAttributeAccessIssue]
            http.client.HTTPConnection.debuglevel = saved_debuglevel


# ---------------------------------------------------------------------------
# Argument forwarding: positionals, body, opts
# ---------------------------------------------------------------------------


class TestArgumentForwarding:
    """Path positionals, ``--body``, and arbitrary opts flow to the SDK as documented."""

    def test_path_positional_is_first_call_arg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--task GID`` -> SDK called as ``get_task(GID, opts)``."""
        cmd = _build_command("TasksApi", "get_task")
        mock = _patch(
            monkeypatch,
            "TasksApi",
            "get_task",
            return_value={"data": {"gid": "TASK"}},
        )
        result = make_runner().invoke(cmd, ["--task", "TASK_GID"])
        assert result.exit_code == 0, full_output(result)
        assert mock.call_args_list[0].args[0] == "TASK_GID"

    def test_body_is_parsed_and_passed_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = _build_command("TasksApi", "create_task")
        mock = _patch(
            monkeypatch,
            "TasksApi",
            "create_task",
            return_value={"data": {"gid": "NEW"}},
        )
        body_json = '{"data": {"name": "x"}}'
        result = make_runner().invoke(cmd, ["--body", body_json])
        assert result.exit_code == 0, full_output(result)
        # ``body`` (parsed JSON) is the first positional, opts is the second.
        assert mock.call_args_list[0].args[0] == {"data": {"name": "x"}}

    def test_arbitrary_opt_param_reaches_opts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = _build_command("TasksApi", "get_task")
        mock = _patch(monkeypatch, "TasksApi", "get_task", return_value={"data": {}})
        result = make_runner().invoke(cmd, ["--task", "T", "--opt-fields", "name,gid"])
        assert result.exit_code == 0, full_output(result)
        opts = mock.call_args_list[0].args[1]
        assert opts["opt_fields"] == "name,gid"

    def test_optional_unset_is_omitted_from_opts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An optional opt the user did not supply must NOT be sent as ``None``."""
        cmd = _build_command("TasksApi", "get_task")
        mock = _patch(monkeypatch, "TasksApi", "get_task", return_value={"data": {}})
        result = make_runner().invoke(cmd, ["--task", "T"])
        assert result.exit_code == 0, full_output(result)
        opts = mock.call_args_list[0].args[1]
        assert "opt_fields" not in opts

    def test_method_without_opts_called_without_opts_dict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``delete_task`` has no ``opts`` parameter; the CLI must not pass one."""
        cmd = _build_command("TasksApi", "delete_task")
        mock = _patch(monkeypatch, "TasksApi", "delete_task", return_value={"data": {}})
        result = make_runner().invoke(cmd, ["--task", "T"])
        assert result.exit_code == 0, full_output(result)
        assert mock.call_args_list[0].args == ("T",)


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------


class TestWorkspaceResolution:
    """``--workspace`` resolves differently depending on whether the endpoint requires it."""

    def test_explicit_workspace_reaches_opts_when_optional(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``get-tasks`` exposes ``workspace`` as an optional opts param."""
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(monkeypatch, "TasksApi", "get_tasks", return_value=_page([]))
        result = make_runner().invoke(cmd, ["--workspace", "WS123"])
        assert result.exit_code == 0, full_output(result)
        assert mock.call_args_list[0].args[0]["workspace"] == "WS123"

    def test_env_var_not_used_when_workspace_is_optional(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``$ASANA_DEFAULT_WORKSPACE`` must not be auto-filled for optional endpoints."""
        monkeypatch.setenv("ASANA_DEFAULT_WORKSPACE", "ENV_WS")
        cmd = _build_command("TasksApi", "get_tasks")
        mock = _patch(monkeypatch, "TasksApi", "get_tasks", return_value=_page([]))
        result = make_runner().invoke(cmd, [])
        assert result.exit_code == 0, full_output(result)
        assert "workspace" not in mock.call_args_list[0].args[0]

    def test_env_var_fills_required_positional_workspace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``get-projects-for-workspace`` takes ``workspace_gid`` as a path positional."""
        monkeypatch.setenv("ASANA_DEFAULT_WORKSPACE", "ENV_WS")
        cmd = _build_command("ProjectsApi", "get_projects_for_workspace")
        mock = _patch(
            monkeypatch,
            "ProjectsApi",
            "get_projects_for_workspace",
            return_value=_page([]),
        )
        result = make_runner().invoke(cmd, [])
        assert result.exit_code == 0, full_output(result)
        # workspace_gid is positional, so it shows up as the first call arg.
        assert mock.call_args_list[0].args[0] == "ENV_WS"

    def test_explicit_workspace_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ASANA_DEFAULT_WORKSPACE", "ENV_WS")
        cmd = _build_command("ProjectsApi", "get_projects_for_workspace")
        mock = _patch(
            monkeypatch,
            "ProjectsApi",
            "get_projects_for_workspace",
            return_value=_page([]),
        )
        result = make_runner().invoke(cmd, ["--workspace", "EXPLICIT_WS"])
        assert result.exit_code == 0, full_output(result)
        assert mock.call_args_list[0].args[0] == "EXPLICIT_WS"

    def test_required_workspace_missing_exits_without_calling_sdk(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _build_command("ProjectsApi", "get_projects_for_workspace")
        mock = _patch(
            monkeypatch,
            "ProjectsApi",
            "get_projects_for_workspace",
            return_value=_page([]),
        )
        result = make_runner().invoke(cmd, [])
        assert result.exit_code != 0
        assert mock.call_count == 0
