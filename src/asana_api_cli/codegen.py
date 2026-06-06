"""Render a CLI invocation as standalone python-asana code.

The ``--generate-python`` mode (``runtime.generate_python``) collects the SDK
call into a session-free :class:`~asana_api_cli.cli.CallPlan` and hands it here
to be rendered as a self-contained script instead of being executed.
``formatter.py:formatted`` calls :func:`render_python` in place of
``_format_output`` whenever the mode is active.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: importing ``cli`` at runtime would cycle (cli -> formatter ->
    # codegen). ``render_python`` reads the plan's attributes structurally, so it
    # never needs the class object — only the annotation, which
    # ``from __future__ import annotations`` keeps as a string.
    from asana_api_cli.cli import CallPlan


def render_python(
    plan: CallPlan,
    *,
    output_format: str,
    jq_query: str | None,
    csv_bom: bool,
    exception_output: str,
    exception_query: str | None,
) -> str:
    """Render *plan* as standalone python-asana code (Phase 1 stub).

    The finished renderer returns a self-contained script that performs the same
    SDK call the command would, reproducing the global configuration and the
    output options (``output_format`` / ``jq_query`` / ``csv_bom`` /
    ``exception_output`` / ``exception_query``). Those are built out in later
    phases; for now this emits a placeholder naming the target call so the
    ``--generate-python`` plumbing — mode flag, session-free ``CallPlan``, and
    this entry point bypassing the formatter — can be exercised end to end.
    """
    target = f"{plan.api_cls.__name__}.{plan.method_name}"
    return (
        "# asana-api --generate-python: not implemented yet.\n"
        f"# Would emit python-asana code for {target}."
    )
