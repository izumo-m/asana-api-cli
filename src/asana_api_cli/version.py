"""Version information for the CLI."""

from __future__ import annotations

from importlib.metadata import version


def version_string() -> str:
    """Return a version string including the python-asana SDK version."""
    cli_ver = version("asana-api-cli")
    sdk_ver = version("asana")
    return f"{cli_ver} (python-asana {sdk_ver})"
