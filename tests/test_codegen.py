"""Equivalence tests for ``codegen.render_python`` (Phase 2 core: config + call + output).

The contract is that running the generated script is equivalent to running the
CLI: it must issue the same SDK call and print the same bytes. Each test
therefore drives the *real* CLI both ways — once in execute mode (SDK mocked,
``CliRunner``) and once in generate mode — then ``exec``s the generated script
against the same mock and compares. This is the C-13 / C-8 / C-17 drift guard:
if the call assembly (``cli.py``), the config mapping (``session.py``), or the
pure converters (``formatter.py``) change, regenerated code keeps matching only
if codegen tracks them.
"""

from __future__ import annotations

import io
import sys
from typing import Any, Callable

import asana
import pytest
from _cli_runner import full_output, make_runner

from asana_api_cli.cli import (
    _enumerate_api_classes,
    _make_command,
    _operations_for,
    main,
)
from asana_api_cli.click_ext import _SDK_HAS_RETRY_STRATEGY
from asana_api_cli.session import AsanaSession, runtime

Factory = Callable[[], Any]


class _CaptureStdout:
    """Stand-in for ``sys.stdout`` that records ``print`` (text) and
    ``.buffer.write`` (bytes, the csv path) as one byte stream, with a no-op
    ``reconfigure`` mirroring the real stream's UTF-8 setup."""

    encoding = "utf-8"

    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, s: str) -> int:
        self.buffer.write(s.encode(self.encoding))
        return len(s)

    def flush(self) -> None:
        pass

    def reconfigure(self, **kwargs: Any) -> None:
        pass


def _build_command(api_cls_name: str, method_name: str) -> Any:
    api_cls = next(c for c in _enumerate_api_classes() if c.__name__ == api_cls_name)
    op = next(o for o in _operations_for(api_cls) if o.method_name == method_name)
    return _make_command(api_cls, op)


def _command(api_cls_name: str, method_name: str) -> Any:
    """A command built once at import (see ``_COMMAND_CACHE``).

    Returning the cached object — rather than rebuilding — is what keeps the
    introspected option set tied to the *real* method signature: a test that
    patches an SDK method (to record or raise) before another helper asks for
    its command would otherwise rebuild from the stub's ``(*args, **kwargs)``.
    """
    return _COMMAND_CACHE[api_cls_name, method_name]


def _record(
    monkeypatch: pytest.MonkeyPatch, api_cls_name: str, method_name: str, factory: Factory
) -> dict[str, Any]:
    """Patch ``asana.<cls>.<method>`` to record the last call (args without
    ``self``, kwargs) and return a fresh ``factory()`` value each time."""
    seen: dict[str, Any] = {}

    def stub(self: Any, *args: Any, **kwargs: Any) -> Any:
        seen["args"] = args
        seen["kwargs"] = kwargs
        return factory()

    monkeypatch.setattr(getattr(asana, api_cls_name), method_name, stub)
    return seen


def _generate(argv: list[str]) -> str:
    """Generated script for ``argv`` (run on the first command of the path)."""
    # argv is [group-less] command flags; the command object is built directly.
    cmd_name, *flags = argv
    api_cls_name, method_name = _COMMANDS[cmd_name]
    result = make_runner().invoke(
        _command(api_cls_name, method_name), ["--generate-python", *flags]
    )
    assert result.exit_code == 0, full_output(result)
    # The invoke set ``runtime.generate_python`` (and the other globals from
    # *flags*); clear just the mode flag so a later execute-mode run in the same
    # test is not diverted into the generate branch. The config globals stay set
    # for callers that compare against a session built from the same runtime.
    runtime.generate_python = False
    return result.stdout


def _exec_generated(
    monkeypatch: pytest.MonkeyPatch, code: str, cmd_name: str, factory: Factory
) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    """Exec *code* with the SDK mocked; return (recorded call, stdout bytes, namespace)."""
    monkeypatch.setenv("ASANA_ACCESS_TOKEN", "dummy-token")
    api_cls_name, method_name = _COMMANDS[cmd_name]
    seen = _record(monkeypatch, api_cls_name, method_name, factory)
    cap = _CaptureStdout()
    monkeypatch.setattr(sys, "stdout", cap)
    namespace: dict[str, Any] = {}
    exec(compile(code, "<generated>", "exec"), namespace)  # noqa: S102
    return seen, cap.buffer.getvalue(), namespace


def _format_reference(
    monkeypatch: pytest.MonkeyPatch,
    data: Any,
    *,
    output_format: str,
    csv_bom: bool,
    jq_query: str | None = None,
) -> bytes:
    """The CLI's own output bytes for *data* — ``_format_output`` captured through
    the faithful byte stream. Used instead of a ``CliRunner`` invoke because
    ``CliRunner`` mangles the RFC 4180 CRLFs (``\\r\\n`` -> ``\\n``) that csv emits
    and a real terminal preserves."""
    from asana_api_cli.formatter import _format_output

    cap = _CaptureStdout()
    monkeypatch.setattr(sys, "stdout", cap)
    _format_output(data, output_format=output_format, jq_query=jq_query, csv_bom=csv_bom)
    return cap.buffer.getvalue()


def _exec_expecting_exit(
    monkeypatch: pytest.MonkeyPatch, code: str, cmd_name: str, factory: Factory
) -> tuple[bytes, int]:
    """Exec *code* whose SDK call raises or whose jq exits; return (stdout bytes,
    exit code). ``SystemExit`` from the generated ``sys.exit`` is caught here."""
    monkeypatch.setenv("ASANA_ACCESS_TOKEN", "dummy-token")
    api_cls_name, method_name = _COMMANDS[cmd_name]
    _record(monkeypatch, api_cls_name, method_name, factory)
    cap = _CaptureStdout()
    monkeypatch.setattr(sys, "stdout", cap)
    monkeypatch.setattr(sys, "stderr", _CaptureStdout())
    exit_code = 0
    try:
        exec(compile(code, "<generated>", "exec"), {})  # noqa: S102
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 1
    return cap.buffer.getvalue(), exit_code


def _cli_run(
    monkeypatch: pytest.MonkeyPatch, cmd_name: str, flags: list[str], factory: Factory
) -> tuple[int, str]:
    """Run the command in execute mode (SDK mocked), tolerating non-zero exits;
    return (exit code, stdout)."""
    monkeypatch.setenv("ASANA_ACCESS_TOKEN", "dummy-token")
    api_cls_name, method_name = _COMMANDS[cmd_name]
    cmd = _command(api_cls_name, method_name)
    _record(monkeypatch, api_cls_name, method_name, factory)
    result = make_runner().invoke(cmd, flags)
    return result.exit_code, result.stdout


def _cli_execute(
    monkeypatch: pytest.MonkeyPatch, cmd_name: str, flags: list[str], factory: Factory
) -> tuple[dict[str, Any], str]:
    """Run the command in execute mode (SDK mocked); return (recorded call, stdout)."""
    monkeypatch.setenv("ASANA_ACCESS_TOKEN", "dummy-token")
    api_cls_name, method_name = _COMMANDS[cmd_name]
    # Build the command from the real method first; ``_record`` then patches it
    # for the call (patching before build would introspect the stub's signature).
    cmd = _command(api_cls_name, method_name)
    seen = _record(monkeypatch, api_cls_name, method_name, factory)
    result = make_runner().invoke(cmd, flags)
    assert result.exit_code == 0, full_output(result)
    return seen, result.stdout


# command name -> (Api class, method). Kept tiny: just the endpoints these tests drive.
_COMMANDS = {
    "get-tasks": ("TasksApi", "get_tasks"),
    "get-task": ("TasksApi", "get_task"),
    "create-task": ("TasksApi", "create_task"),
    "delete-task": ("TasksApi", "delete_task"),
    "create-attachment-for-object": ("AttachmentsApi", "create_attachment_for_object"),
}

# Built once, at import, before any test patches an SDK method — so every
# command reflects the real signature no matter the order helpers build/patch in.
_COMMAND_CACHE: dict[tuple[str, str], Any] = {
    (cls, method): _build_command(cls, method) for cls, method in _COMMANDS.values()
}


class TestCallEquivalence:
    """The generated script issues the same SDK call as the CLI execute path."""

    @pytest.mark.parametrize(
        ("cmd", "flags", "factory"),
        [
            (
                "get-tasks",
                ["--workspace", "111", "--opt-fields", "name"],
                lambda: iter([{"gid": "1"}]),
            ),
            ("get-task", ["--task", "999", "--opt-fields", "name,notes"], lambda: {"gid": "999"}),
            ("create-task", ["--body", '{"data":{"name":"x"}}'], lambda: {"gid": "new"}),
            ("delete-task", ["--task", "5"], lambda: {}),
            ("get-tasks", ["--item-limit", "7", "--full-payload"], lambda: {"data": []}),
            (
                "get-tasks",
                ["--request-timeout", "30", "--header-params", '{"X-Req":"1"}'],
                lambda: iter([]),
            ),
        ],
    )
    def test_same_call(
        self,
        monkeypatch: pytest.MonkeyPatch,
        cmd: str,
        flags: list[str],
        factory: Factory,
    ) -> None:
        code = _generate([cmd, *flags])
        cli_call, _ = _cli_execute(monkeypatch, cmd, flags, factory)
        gen_call, _, _ = _exec_generated(monkeypatch, code, cmd, factory)
        assert gen_call == cli_call


class TestBodyForms:
    """``--body`` is rendered per input form (C-9): a JSON literal is inlined,
    while ``@file`` / ``-`` (stdin) become code that reads at the generated
    script's run time — never read at generation."""

    def test_literal_is_inlined(self) -> None:
        code = _generate(["create-task", "--body", '{"data": {"name": "X", "done": false}}'])
        assert "body = {'data': {'name': 'X', 'done': False}}" in code
        assert "json.load" not in code

    def test_file_reads_at_runtime(self) -> None:
        code = _generate(["create-task", "--body", "@payload.json"])
        assert "with open('payload.json', encoding=\"utf-8\") as f:" in code
        assert "    body = json.load(f)" in code

    def test_stdin_reads_at_runtime(self) -> None:
        code = _generate(["create-task", "--body", "-"])
        assert "body = json.load(sys.stdin)" in code

    def test_stdin_reconfigures_to_utf8(self) -> None:
        # The stdin body path must reconfigure stdin to UTF-8 before reading,
        # matching the CLI's startup — a piped UTF-8 body must not be misdecoded
        # with the locale code page on cp932 Windows (constitution #5). Present
        # regardless of --output (it lives in the body branch, not the output
        # reconfigure block, which --output none would otherwise omit).
        code = _generate(["create-task", "--body", "-", "--output", "none"])
        assert 'sys.stdin.reconfigure(encoding="utf-8")' in code
        assert code.index("sys.stdin.reconfigure") < code.index("json.load(sys.stdin)")

    def test_file_not_read_at_generation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A nonexistent @file still generates fine — the read is deferred to the
        # generated script (execute mode would exit 2; generation does not read it).
        monkeypatch.delenv("ASANA_ACCESS_TOKEN", raising=False)
        code = _generate(["create-task", "--body", "@/no/such/file.json"])
        assert "body = json.load(f)" in code

    def test_generated_file_body_reads_the_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        payload = tmp_path / "body.json"
        payload.write_text('{"data": {"name": "FromFile"}}', encoding="utf-8")
        code = _generate(["create-task", "--body", f"@{payload}"])
        seen, _, _ = _exec_generated(monkeypatch, code, "create-task", lambda: {"gid": "n"})
        assert seen["args"][0] == {"data": {"name": "FromFile"}}

    def test_generated_stdin_body_reads_stdin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code = _generate(["create-task", "--body", "-"])
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"data": {"name": "FromStdin"}}'))
        seen, _, _ = _exec_generated(monkeypatch, code, "create-task", lambda: {"gid": "n"})
        assert seen["args"][0] == {"data": {"name": "FromStdin"}}


class TestIteratorMaterialization:
    """``list(...)`` exactly when the live ``isinstance`` gate would fire."""

    def test_array_endpoint_wraps_in_list(self) -> None:
        code = _generate(["get-tasks", "--workspace", "1"])
        assert "result = list(api_instance.get_tasks(" in code

    def test_full_payload_does_not_wrap(self) -> None:
        code = _generate(["get-tasks", "--full-payload"])
        assert "result = api_instance.get_tasks(" in code
        assert "list(" not in code

    def test_no_iterator_flag_does_not_wrap(self) -> None:
        code = _generate(["get-tasks", "--no-return-page-iterator"])
        assert "result = api_instance.get_tasks(" in code
        assert "list(" not in code

    def test_single_object_endpoint_does_not_wrap(self) -> None:
        code = _generate(["get-task", "--task", "1"])
        assert "result = api_instance.get_task(" in code
        assert "list(" not in code


class TestOutputEquivalence:
    """For every ``--output`` format the generated script prints the CLI's bytes."""

    @pytest.mark.parametrize("fmt", ["json", "text", "table", "csv", "none"])
    @pytest.mark.parametrize(
        "factory",
        [
            pytest.param(
                lambda: iter([{"gid": "1", "name": "あ"}, {"gid": "2", "name": "b"}]), id="rows"
            ),
            pytest.param(lambda: iter([]), id="empty"),
        ],
    )
    def test_output_matches_cli(
        self, monkeypatch: pytest.MonkeyPatch, fmt: str, factory: Factory
    ) -> None:
        code = _generate(["get-tasks", "--workspace", "1", "--output", fmt])
        _, gen_bytes, _ = _exec_generated(monkeypatch, code, "get-tasks", factory)
        ref = _format_reference(monkeypatch, list(factory()), output_format=fmt, csv_bom=False)
        assert gen_bytes == ref

    def test_csv_bom_matches_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        factory: Factory = lambda: iter([{"gid": "1", "name": "x"}])  # noqa: E731
        code = _generate(["get-tasks", "--workspace", "1", "--output", "csv", "--csv-bom"])
        _, gen_bytes, _ = _exec_generated(monkeypatch, code, "get-tasks", factory)
        ref = _format_reference(monkeypatch, list(factory()), output_format="csv", csv_bom=True)
        assert gen_bytes.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM
        assert gen_bytes == ref

    @pytest.mark.parametrize("fmt", ["json", "text", "table", "csv", "none"])
    def test_single_object_output_matches_cli(
        self, monkeypatch: pytest.MonkeyPatch, fmt: str
    ) -> None:
        # A non-iterator endpoint returns a bare dict (not a list), exercising the
        # generated output's non-list / single-row branches (the ``else`` in the
        # text path, ``to_rows(dict)`` in table/csv) that the get-tasks cases above
        # do not reach.
        factory: Factory = lambda: {"gid": "1", "name": "あ", "notes": "x"}  # noqa: E731
        code = _generate(["get-task", "--task", "1", "--output", fmt])
        _, gen_bytes, _ = _exec_generated(monkeypatch, code, "get-task", factory)
        ref = _format_reference(monkeypatch, factory(), output_format=fmt, csv_bom=False)
        assert gen_bytes == ref


class TestConfigEquivalence:
    """The generated ``Configuration`` / ``ApiClient`` matches ``AsanaSession``."""

    @pytest.mark.parametrize(
        "globals_",
        [
            [],
            ["--no-return-page-iterator"],
            ["--page-limit", "25"],
            ["--host", "https://example.test/api"],
            ["--user-agent", "my-agent/1.0"],
            ["--set-default-header", "X-Trace=abc"],
        ],
    )
    def test_config_attributes_match(
        self, monkeypatch: pytest.MonkeyPatch, globals_: list[str]
    ) -> None:
        # The generate invocation writes the globals onto ``runtime``; build a
        # session from that same runtime and compare attribute-by-attribute.
        # ``--output none`` so the exec only builds the config (it does not format
        # the mock result, whose shape depends on the iterator toggle).
        code = _generate(["get-tasks", *globals_, "--output", "none"])
        _, _, namespace = _exec_generated(monkeypatch, code, "get-tasks", lambda: iter([]))
        gen_config = namespace["configuration"]
        gen_client = namespace["api_client"]

        session = AsanaSession.from_env()
        ref_config = session._config
        ref_client = session._client

        for attr in ("return_page_iterator", "page_limit", "host", "connection_pool_maxsize"):
            assert getattr(gen_config, attr) == getattr(ref_config, attr), attr
        assert gen_config.access_token == ref_config.access_token
        assert gen_client.user_agent == ref_client.user_agent
        assert gen_client.default_headers == ref_client.default_headers

    def test_access_token_literal_is_transcribed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code = _generate(["get-tasks", "--access-token", "dummy-pat", "--workspace", "1"])
        assert "configuration.access_token = 'dummy-pat'" in code
        assert "os.environ" not in code

    def test_access_token_defaults_to_env(self) -> None:
        code = _generate(["get-tasks", "--workspace", "1"])
        assert "configuration.access_token = os.environ['ASANA_ACCESS_TOKEN']" in code

    @pytest.mark.skipif(
        not _SDK_HAS_RETRY_STRATEGY,
        reason="installed python-asana has no Configuration.retry_strategy",
    )
    def test_retry_strategy_matches_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # retry is the one knob rendered as a transform (``.new(**overrides)``);
        # the exec'd result must equal what ``session.py`` builds from the override.
        code = _generate(
            ["get-tasks", "--retry-strategy", "total=3,backoff_factor=1", "--output", "none"]
        )
        _, _, namespace = _exec_generated(monkeypatch, code, "get-tasks", lambda: iter([]))
        gen = namespace["configuration"].retry_strategy
        ref = AsanaSession.from_env()._config.retry_strategy
        assert (gen.total, gen.backoff_factor) == (ref.total, ref.backoff_factor)

    def test_falsy_toggle_is_still_emitted(self) -> None:
        # C-8: an explicit ``False`` (here --no-return-page-iterator) must be
        # written, not skipped like an unset (None) knob.
        code = _generate(["get-tasks", "--no-return-page-iterator"])
        assert "configuration.return_page_iterator = False" in code


class TestGeneratedScriptHygiene:
    def test_compiles_and_is_self_contained(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["asana-api", "--generate-python", "tasks", "get-tasks"])
        code = _generate(["get-tasks", "--workspace", "1", "--output", "csv"])
        compile(code, "<generated>", "exec")  # valid Python
        # Self-contained: never imports the CLI package (constitution #6).
        assert "asana_api_cli" not in code
        # Header comments precede the future import (valid: comments are not statements).
        assert code.startswith("# Generated by asana-api ")
        assert "\nfrom __future__ import annotations\n" in code

    # json is excluded: it renders via ``json.dumps`` directly and inlines no
    # converter, so there is no ``Any`` annotation to resolve.
    @pytest.mark.parametrize("fmt", ["text", "table", "csv"])
    def test_inlined_converter_annotations_resolve(
        self, monkeypatch: pytest.MonkeyPatch, fmt: str
    ) -> None:
        # The inlined converters annotate with ``Any``; ``from __future__ import
        # annotations`` makes those annotations lazy strings, so a missing
        # ``from typing import Any`` would not break compile/exec — only later
        # annotation evaluation. ``get_type_hints`` forces that evaluation in the
        # script's own namespace, so it NameErrors if an annotation name is
        # undefined (the bug this guards: ``Any`` not imported).
        import inspect as _inspect
        import typing

        code = _generate(["get-tasks", "--workspace", "1", "--output", fmt])
        _, _, namespace = _exec_generated(monkeypatch, code, "get-tasks", lambda: iter([]))
        funcs = [obj for obj in namespace.values() if _inspect.isfunction(obj)]
        assert funcs, "expected at least one inlined converter function"
        for func in funcs:
            # Resolves annotation strings against the function's own globals (the
            # generated module's namespace); NameErrors if e.g. ``Any`` is unbound.
            typing.get_type_hints(func)


class TestHeader:
    """C-5: a provenance line and the original command."""

    def test_provenance_and_equivalent_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The "Equivalent to" line is read from sys.argv (minus --generate-python).
        monkeypatch.setattr(
            sys,
            "argv",
            ["asana-api", "--generate-python", "tasks", "get-tasks", "--workspace", "1"],
        )
        code = _generate(["get-tasks", "--workspace", "1"])
        lines = code.splitlines()
        assert lines[0].startswith("# Generated by asana-api ")
        assert lines[1] == "# Equivalent to: asana-api tasks get-tasks --workspace 1"

    # Python ends a comment at \n, \r, and \r\n alike, and shlex.join leaves any
    # of them literal — each must be wrapped, not just \n.
    @pytest.mark.parametrize("sep", ["\n", "\r", "\r\n"])
    def test_linebreak_argument_stays_inside_the_comment(
        self, monkeypatch: pytest.MonkeyPatch, sep: str
    ) -> None:
        # A line-break-bearing argument (here a multiline --query jq program) must
        # not end the "# Equivalent to:" comment and leak its tail into the script
        # as source — every continuation line is re-prefixed with "#", and the
        # script still compiles.
        argv = ["tasks", "get-tasks", "--workspace", "1", "--query", f".data{sep}  | map(.name)"]
        monkeypatch.setattr(sys, "argv", ["asana-api", "--generate-python", *argv])
        result = make_runner().invoke(main, ["--generate-python", *argv])
        assert result.exit_code == 0, full_output(result)
        code = result.stdout
        compile(code, "<gen>", "exec")  # a leaked tail would be a SyntaxError here
        header = code.split("\n\nfrom __future__", 1)[0]
        assert all(line.startswith("#") for line in header.splitlines())


class TestVersionCodegen:
    """C-15: ``--generate-python --version`` emits a script that prints the
    version (version.py inlined) instead of printing it directly."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["--generate-python", "--version"],
            ["--version", "--generate-python"],  # order-independent
        ],
    )
    def test_generates_version_script(
        self, monkeypatch: pytest.MonkeyPatch, argv: list[str]
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["asana-api", *argv])
        result = make_runner().invoke(main, argv)
        assert result.exit_code == 0, full_output(result)
        code = result.stdout
        assert code.startswith("# Generated by asana-api ")
        assert "# Equivalent to: asana-api --version" in code
        assert "def version_string() -> str:" in code  # version.py inlined
        assert code.rstrip().endswith("print(version_string())")

    def test_plain_version_unaffected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["asana-api", "--version"])
        result = make_runner().invoke(main, ["--version"])
        assert result.exit_code == 0, full_output(result)
        assert result.stdout.startswith("asana-api, version ")
        assert "def version_string" not in result.stdout


class TestQueryEquivalence:
    """``--query`` runs jq over the result in the generated script, exactly as the
    CLI does — same yields, same bytes; bad expressions exit 2."""

    # The page iterator materializes to a flat list, so queries run against the
    # list (``.[]``), not a ``{data:[...]}`` envelope.
    @pytest.mark.parametrize("query", [".[].name", "length", ".[0].name"])
    def test_query_output_matches_cli(self, monkeypatch: pytest.MonkeyPatch, query: str) -> None:
        factory: Factory = lambda: iter(  # noqa: E731
            [{"gid": "1", "name": "あ"}, {"gid": "2", "name": "b"}]
        )
        code = _generate(["get-tasks", "--workspace", "1", "--query", query])
        assert "import jq" in code
        _, gen_bytes, _ = _exec_generated(monkeypatch, code, "get-tasks", factory)
        ref = _format_reference(
            monkeypatch, list(factory()), output_format="json", csv_bom=False, jq_query=query
        )
        assert gen_bytes == ref

    def test_no_query_omits_jq_dependency(self) -> None:
        code = _generate(["get-tasks", "--workspace", "1"])
        assert "import jq" not in code

    def test_bad_query_exits_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        factory: Factory = lambda: iter([{"gid": "1"}])  # noqa: E731
        code = _generate(["get-tasks", "--workspace", "1", "--query", "{"])
        _, exit_code = _exec_expecting_exit(monkeypatch, code, "get-tasks", factory)
        assert exit_code == 2


def _api_exception() -> Any:
    """A representative ApiException (5-field envelope, with a nested headers dict
    and a non-ASCII body)."""
    from asana.rest import ApiException

    exc = ApiException(status=412, reason="Precondition Failed")
    exc.body = '{"errors":[{"message":"sync token expired: あ"}]}'
    exc.headers = {"Content-Type": "application/json"}
    return exc


def _envelope_for(exc: Exception) -> dict[str, Any]:
    """The envelope dict the CLI builds for *exc* — the reference the generated
    script's envelope must match (mirrors ``formatter._handle_exception``)."""
    from asana.rest import ApiException

    from asana_api_cli.formatter import _qualified_exception_name

    if isinstance(exc, ApiException):
        return {
            "exception": _qualified_exception_name(exc),
            "status": exc.status,
            "reason": exc.reason,
            "body": exc.body,
            "headers": dict(exc.headers) if exc.headers is not None else None,
        }
    return {"exception": _qualified_exception_name(exc), "reason": str(exc)}


class TestErrorEnvelopeEquivalence:
    """``--exception-output`` wraps the call in try/except and renders the same
    envelope (and exit 3) the CLI does — across every envelope format and with
    ``--exception-query``."""

    def _raise(self, exc: Exception) -> Factory:
        def factory() -> Any:
            raise exc

        return factory

    @pytest.mark.parametrize("fmt", ["json", "text", "csv", "table"])
    @pytest.mark.parametrize(
        "make_exc",
        [
            pytest.param(lambda: ValueError("boom"), id="generic"),
            pytest.param(_api_exception, id="api"),
        ],
    )
    def test_envelope_matches_cli(
        self, monkeypatch: pytest.MonkeyPatch, fmt: str, make_exc: Any
    ) -> None:
        # Compare to ``_format_output`` over the same envelope dict (the
        # CRLF-faithful reference) rather than a CliRunner invoke, so the csv
        # envelope is byte-exact too.
        factory = self._raise(make_exc())
        code = _generate(["get-task", "--task", "1", "--exception-output", fmt])
        gen_bytes, gen_exit = _exec_expecting_exit(monkeypatch, code, "get-task", factory)
        ref = _format_reference(
            monkeypatch, _envelope_for(make_exc()), output_format=fmt, csv_bom=False
        )
        assert gen_exit == 3
        assert gen_bytes == ref

    def test_exception_query_matches_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The error path runs jq over the single envelope dict (distinct shape
        # from the success path's materialized list).
        factory = self._raise(_api_exception())
        flags = ["--task", "1", "--exception-output", "json", "--exception-query", ".status"]
        code = _generate(["get-task", *flags])
        assert "import jq" in code
        gen_bytes, gen_exit = _exec_expecting_exit(monkeypatch, code, "get-task", factory)
        ref = _format_reference(
            monkeypatch,
            _envelope_for(_api_exception()),
            output_format="json",
            csv_bom=False,
            jq_query=".status",
        )
        assert gen_exit == 3
        assert gen_bytes == ref

    def test_exception_query_bad_expr_exits_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        factory = self._raise(ValueError("boom"))
        code = _generate(
            ["get-task", "--task", "1", "--exception-output", "json", "--exception-query", "{"]
        )
        _, gen_exit = _exec_expecting_exit(monkeypatch, code, "get-task", factory)
        assert gen_exit == 2

    def test_default_none_has_no_try_block(self) -> None:
        code = _generate(["get-task", "--task", "1"])
        assert "try:" not in code
        assert "except" not in code


class TestDebugLayer:
    """``--debug`` inlines redactor.py and wraps the call so the wire trace keeps
    the Authorization header masked (constitution #2)."""

    def test_inlines_redactor_and_wraps_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ASANA_ACCESS_TOKEN", raising=False)
        code = _generate(["get-task", "--task", "5", "--debug"])
        assert "configuration.debug = True" in code
        assert "class HttpClientAuthRedactor" in code  # whole module inlined
        assert "def _default_mask_token" in code
        assert "with HttpClientAuthRedactor():" in code
        # The call is inside the with block, and no token is in the source.
        assert "    result = api_instance.get_task('5', opts)" in code
        assert "os.environ['ASANA_ACCESS_TOKEN']" in code

    def test_debug_script_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code = _generate(["get-task", "--task", "5", "--debug"])
        _, _, namespace = _exec_generated(monkeypatch, code, "get-task", lambda: {"gid": "5"})
        # The inlined redactor is byte-identical to redactor.py (getsource), so it
        # masks by construction; just confirm it exec'd and is callable here.
        assert "HttpClientAuthRedactor" in namespace


class TestUploadLayer:
    """``--multibyte-filenames`` inlines multibyte_filename.py and wraps the call."""

    def test_inlines_support_and_wraps_call(self) -> None:
        code = _generate(
            ["create-attachment-for-object", "--file", "/tmp/あ.png", "--multibyte-filenames"]
        )
        assert "class MultibyteFilenameSupport" in code
        assert "with MultibyteFilenameSupport():" in code
        assert "    result = api_instance.create_attachment_for_object(opts)" in code

    def test_no_multibyte_omits_support(self) -> None:
        code = _generate(["create-attachment-for-object", "--file", "/tmp/x.png"])
        assert "MultibyteFilenameSupport" not in code
        assert "result = api_instance.create_attachment_for_object(opts)" in code

    def test_upload_script_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The inlined MultibyteFilenameSupport must work as a context manager: the
        # script enters it, calls the (mocked) SDK, and exits cleanly.
        code = _generate(
            ["create-attachment-for-object", "--file", "/tmp/x.png", "--multibyte-filenames"]
        )
        seen, _, namespace = _exec_generated(
            monkeypatch, code, "create-attachment-for-object", lambda: {"gid": "att"}
        )
        assert "MultibyteFilenameSupport" in namespace
        assert seen["args"] == ({"file": "/tmp/x.png"},)
