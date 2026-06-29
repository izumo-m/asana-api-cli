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
(Configuration) or a common per-command ``(kwargs: ...)`` option, with the
matching SDK-destination label (see ``docs/cli-sdk-mapping.md``).

Two further guards prove the CLI's runtime *classifiers* still agree with the
SDK source on the installed version — each derives both sides from the SDK, so
neither needs maintenance across a bump:

* ``test_upload_detection_matches_multipart_population`` — the
  ``--multibyte-filenames`` flag is exposed only on upload commands, detected by
  a cheap proxy (an op declaring a ``file`` opt — see ``_Operation.does_upload``).
  The test proves that proxy equals the true signal (a source scan for assignment
  into ``local_var_files``).
* ``test_iterator_detection_matches_source_construction`` — array-response
  endpoints return a lazy ``PageIterator`` / ``EventIterator`` the runtime
  materializes with ``list(...)``; the runtime detects them by a cheap proxy
  (``:return:`` type ending in ``Array`` — see ``_Operation.returns_iterator``).
  The test proves that proxy equals the true signal (a source scan for the
  iterator constructor).

The *which-commands* question — the exact set of upload / iterator endpoints —
is snapshotted per-command in ``tests/fixtures/cli_surface.json`` (the
``does_upload`` / ``returns_iterator`` fields), so an SDK bump that adds or
removes such an endpoint surfaces in that fixture's diff during the normal regen
step, with no hand-maintained set to update here.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Iterator
from typing import Any

import asana

from asana_api_cli.cli import _enumerate_api_classes, _operations_for

# Identical across all ``*_with_http_info`` methods in python-asana 5.2.5.
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

# Settable attributes the CLI knows about on a default ``asana.Configuration()``.
# Not every one is a CLI flag: object-/callable-typed members (``logger*``,
# ``refresh_api_key_hook``) cannot be flags, and the inert auth fields
# (``username`` / ``password`` / ``api_key`` / ``api_key_prefix``) are
# deliberately NOT exposed — Asana auth is Bearer-token only, so they are
# swagger-codegen dead weight (see docs/sdk-deviations.md). They remain in this
# set because the SDK's Configuration still declares them; this guard tracks the
# SDK surface, not the CLI's. ``logger_format`` / ``logger_file`` reach the
# ``logger*`` members via @property setters.
#
# This is the union across the supported asana range (>=5.0.2), pinned at the
# latest. ``test_no_unknown_configuration_settable_attrs`` checks only one
# direction — that the installed SDK exposes nothing OUTSIDE this set, since a
# *new* attribute is the actionable drift (it likely needs a flag). Older SDKs
# initialize fewer attributes (``access_token`` arrived in 5.0.6,
# ``retry_strategy`` in 5.1.0); those absences are tolerated so the guard passes
# on the floor as well as the pinned version.
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


def _methods_populating_local_var_files() -> set[tuple[str, str]]:
    """Methods whose ``*_with_http_info`` source writes into ``local_var_files``.

    A subscript write — ``local_var_files['file'] = ...`` (assignment) or an
    augmented assignment — is the true "this method sends multipart/form-data
    with a file" signal, as opposed to the universal ``local_var_files = {}``
    initializer (a plain ``Name`` target, not a subscript). Returns
    ``(ApiClassName, public_method_name)`` pairs.
    """
    suffix = "_with_http_info"
    out: set[tuple[str, str]] = set()
    for cls_name, method_name, func in _with_http_info_methods():
        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            else:
                continue
            for target in targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "local_var_files"
                ):
                    out.add((cls_name, method_name[: -len(suffix)]))
    return out


def _upload_ops_via_does_upload() -> set[tuple[str, str]]:
    """Methods the runtime classifies as uploads via ``_Operation.does_upload``."""
    out: set[tuple[str, str]] = set()
    for cls in _enumerate_api_classes():
        for op in _operations_for(cls):
            if op.does_upload:
                out.add((cls.__name__, op.method_name))
    return out


# The constructors the SDK calls in the ``return_page_iterator`` branch of an
# array-response endpoint. The method returns their ``.items()`` generator, so
# the runtime materialization gate sees an iterator.
_ITERATOR_CTORS = {"PageIterator", "EventIterator"}


def _methods_constructing_iterator() -> set[tuple[str, str]]:
    """Methods whose ``*_with_http_info`` source constructs a lazy iterator.

    Constructing a ``PageIterator`` / ``EventIterator`` (the array-response
    branch) is the ground-truth source signal that the endpoint returns a lazy
    iterator — the method returns the constructor's ``.items()`` generator, which
    the runtime ``isinstance(result, Iterator)`` gate materializes. Detected by an
    AST scan for a call to either constructor (a bare ``Name``, as the SDK imports
    them). Returns ``(ApiClassName, public_method_name)`` pairs.
    """
    suffix = "_with_http_info"
    out: set[tuple[str, str]] = set()
    for cls_name, method_name, func in _with_http_info_methods():
        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in _ITERATOR_CTORS
            ):
                out.add((cls_name, method_name[: -len(suffix)]))
    return out


def _iterator_ops_via_returns_iterator() -> set[tuple[str, str]]:
    """Methods the runtime classifies as iterator-returning via
    ``_Operation.returns_iterator``."""
    out: set[tuple[str, str]] = set()
    for cls in _enumerate_api_classes():
        for op in _operations_for(cls):
            if op.returns_iterator:
                out.add((cls.__name__, op.method_name))
    return out


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
        "in _make_command with a (kwargs: <name>) label — not a global flag "
        "(or removal handling). See docs/cli-sdk-mapping.md."
    )


def test_no_unknown_configuration_settable_attrs() -> None:
    got = frozenset(k for k in vars(asana.Configuration()) if not k.startswith("_"))
    unknown = got - EXPECTED_CONFIGURATION_ATTRS
    assert not unknown, (
        "asana.Configuration exposes settable attributes the CLI does not know "
        f"about: {sorted(unknown)}\n"
        "A newly settable property likely needs a global flag + "
        "(Configuration: <name>) label in cli.py / click_ext.py, then adding it "
        "to EXPECTED_CONFIGURATION_ATTRS. See docs/cli-sdk-mapping.md."
    )


def test_upload_detection_matches_multipart_population() -> None:
    multipart = _methods_populating_local_var_files()
    assert multipart, "no method populates local_var_files — SDK introspection broke"
    via_proxy = _upload_ops_via_does_upload()
    assert via_proxy == multipart, (
        "_Operation.does_upload (the 'has a file opt' proxy) no longer matches "
        "the methods that actually populate local_var_files:\n"
        f"  proxy-only:     {sorted(via_proxy - multipart)}\n"
        f"  multipart-only: {sorted(multipart - via_proxy)}\n"
        "Update the does_upload predicate in cli.py so --multibyte-filenames "
        "lands on exactly the upload commands."
    )


def test_iterator_detection_matches_source_construction() -> None:
    constructed = _methods_constructing_iterator()
    assert constructed, (
        "no method constructs a PageIterator/EventIterator — SDK introspection broke"
    )
    via_proxy = _iterator_ops_via_returns_iterator()
    assert via_proxy == constructed, (
        "_Operation.returns_iterator (the ':return: *Array' proxy) no longer "
        "matches the methods that actually construct an iterator:\n"
        f"  proxy-only:  {sorted(via_proxy - constructed)}\n"
        f"  source-only: {sorted(constructed - via_proxy)}\n"
        "Update the returns_iterator predicate in cli.py."
    )


def test_pagination_items_are_generators() -> None:
    # The runtime gate (cli.py:execute_call_plan) materializes a result with
    # ``isinstance(result, collections.abc.Iterator)`` while the session — and the
    # --debug redactor — is still open (principle #2: pages 2..N must be fetched
    # in-session). Array endpoints return ``PageIterator(...).items()`` /
    # ``EventIterator(...).items()``, and ``.items()`` is a generator function, so
    # the result is a generator — hence always an Iterator — which is what makes
    # the gate fire. Pin that: a future SDK whose ``.items()`` stopped being a
    # generator (e.g. returned a list) would silently change materialization, and
    # under --debug could leak Authorization on later pages.
    from asana.pagination import EventIterator, PageIterator

    assert inspect.isgeneratorfunction(PageIterator.items)
    assert inspect.isgeneratorfunction(EventIterator.items)
