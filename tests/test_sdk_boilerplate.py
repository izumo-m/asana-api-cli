"""Pin the SDK's uniform inputs so a python-asana bump can't slip a new one past us.

The CLI derives two SDK-uniform input families from introspection:

* the boilerplate ``**kwargs`` every generated method accepts — its
  ``all_params`` list. The user-facing four (``item_limit`` / ``full_payload``
  / ``header_params`` / ``_request_timeout``) become common per-command
  options; ``async_req`` / ``_return_http_data_only`` / ``_preload_content``
  stay SDK-internal;
* the settable ``asana.Configuration`` properties, which become global flags
  (minus object-/callable-typed members and the inert auth fields — see
  ``docs/sdk-deviations.md``).

Both families are deliberately **outside** ``tests/fixtures/cli_surface.json``
(see ``test_cli_surface.py``: synthetic / global options are intentionally not
in the manifest). So a bump that adds a new boilerplate kwarg or Configuration
property would otherwise go unnoticed. These two tests pin both sets so such a
bump fails loudly and forces a conscious classification — a global flag
(Configuration) or a common per-command ``(kwarg: ...)`` option, with the
matching SDK-destination label (see ``docs/cli-sdk-mapping.md``).
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Iterator
from typing import Any

import asana

# Identical across all ``*_with_http_info`` methods in python-asana 5.2.4.
EXPECTED_ALL_PARAMS: frozenset[str] = frozenset(
    {
        "async_req",
        "header_params",
        "_return_http_data_only",
        "_preload_content",
        "_request_timeout",
        "full_payload",
        "item_limit",
    }
)

# Public attributes on a default ``asana.Configuration()``. Not every settable
# attribute is a CLI flag: object-/callable-typed members (``logger*``,
# ``refresh_api_key_hook``) cannot be flags, and the inert auth fields
# (``username`` / ``password`` / ``api_key`` / ``api_key_prefix``) are
# deliberately NOT exposed — Asana auth is Bearer-token only, so they are
# swagger-codegen dead weight (see docs/sdk-deviations.md). They remain in this
# set because the SDK's Configuration still declares them; this guard tracks the
# SDK surface, not the CLI's. ``logger_format`` / ``logger_file`` reach the
# ``logger*`` members via @property setters.
EXPECTED_CONFIGURATION_ATTRS: frozenset[str] = frozenset(
    {
        "access_token",
        "api_key",
        "api_key_prefix",
        "assert_hostname",
        "cert_file",
        "connection_pool_maxsize",
        "host",
        "key_file",
        "logger",
        "logger_file_handler",
        "logger_formatter",
        "logger_stream_handler",
        "page_limit",
        "password",
        "proxy",
        "refresh_api_key_hook",
        "retry_strategy",
        "return_page_iterator",
        "safe_chars_for_path_param",
        "ssl_ca_cert",
        "temp_folder_path",
        "username",
        "verify_ssl",
    }
)


def _all_params_for(func: Any) -> frozenset[str]:
    """Collect the ``all_params.append('x')`` string literals from a method body.

    ``all_params`` is a local variable built at call time, so it can only be
    read from the source: parse the AST and gather every
    ``all_params.append(<str literal>)`` argument.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    names: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "append" or not isinstance(node.func.value, ast.Name):
            continue
        if node.func.value.id != "all_params" or not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            names.append(arg.value)
    return frozenset(names)


def _with_http_info_methods() -> Iterator[tuple[str, str, Any]]:
    for cls_name, cls in sorted(vars(asana).items(), key=lambda kv: kv[0]):
        if not (inspect.isclass(cls) and cls_name.endswith("Api")):
            continue
        for method_name in sorted(vars(cls)):
            if method_name.endswith("_with_http_info"):
                yield cls_name, method_name, vars(cls)[method_name]


def test_all_methods_share_expected_all_params() -> None:
    offenders: dict[str, list[str]] = {}
    count = 0
    for cls_name, method_name, func in _with_http_info_methods():
        count += 1
        got = _all_params_for(func)
        if got != EXPECTED_ALL_PARAMS:
            offenders[f"{cls_name}.{method_name}"] = sorted(got ^ EXPECTED_ALL_PARAMS)

    assert count > 0, "no *_with_http_info methods found — SDK introspection broke"
    assert not offenders, (
        "all_params drifted from EXPECTED_ALL_PARAMS (symmetric diff per method):\n"
        + "\n".join(f"  {name}: {diff}" for name, diff in offenders.items())
        + "\nA new boilerplate kwarg likely needs a common per-command option "
        "in _make_command with a (kwarg: <name>) label — not a global flag "
        "(or removal handling). See docs/cli-sdk-mapping.md."
    )


def test_configuration_settable_attrs_match() -> None:
    got = frozenset(k for k in vars(asana.Configuration()) if not k.startswith("_"))
    assert got == EXPECTED_CONFIGURATION_ATTRS, (
        "asana.Configuration public attributes drifted:\n"
        f"  added:   {sorted(got - EXPECTED_CONFIGURATION_ATTRS)}\n"
        f"  removed: {sorted(EXPECTED_CONFIGURATION_ATTRS - got)}\n"
        "A newly settable property likely needs a global flag + "
        "(Configuration: <name>) label in cli.py / click_ext.py. "
        "See docs/cli-sdk-mapping.md."
    )
