# Project principles

## Constitution

1. **Parity with the `python-asana` SDK is the top priority.** Both surface (group / command / option) and behavior (pagination semantics, authentication, error types, response shape) follow the SDK. CLI-specific additions are kept minimal — output formats, `--query`, Windows ergonomics.

2. **Security overrides parity.** Credentials and other secrets must never appear in user-visible output, even when raw SDK behavior would expose them. The `--debug` HTTP log redaction (`HttpClientPrintRedactor` in `session.py`) is the canonical example: it deviates from the SDK's verbose HTTP logging to mask authorization headers.

3. **The command-line interface is resolved at runtime.** The command tree and options are built at startup by introspecting the installed `python-asana` package. An SDK bump completes with a dependency update and a snapshot fixture refresh; nothing else.

4. **Python 3.10+ is supported.** `pyproject.toml`'s `requires-python = ">=3.10"` is maintained.

5. **Windows is a first-class citizen.** cp932 / UTF-8 / Excel CSV / WSL workflows must work. With no CI, Windows verification is manual.

## Terminology

| Term | Refers to |
|---|---|
| **`python-asana` SDK** (short: **the SDK**) | The official Asana Python client. Distributed on PyPI as `python-asana`, imported as `asana`. |
| **`asana-api-cli`** | This project / pip-installable package name. |
| **`asana-api`** | The CLI executable produced by this project (what users actually run). |
