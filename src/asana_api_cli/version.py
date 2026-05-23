"""Version information for the CLI."""

from __future__ import annotations

from importlib.metadata import version


def version_string() -> str:
    """Return a version string including the python-asana SDK and click versions."""
    cli_ver = version("asana-api-cli")
    sdk_ver = version("asana")
    click_ver = version("click")
    return f"{cli_ver} (python-asana {sdk_ver}, click {click_ver})"
