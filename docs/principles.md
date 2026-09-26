# Project principles

## Constitution

1. **Parity with the `python-asana` SDK is the top priority.** Both surface (group / command / option) and behavior (pagination semantics, authentication, error types, response shape) follow the SDK. CLI-specific extensions or deviations are kept minimal and admitted only when (a) they close a concrete gap that an SDK-faithful CLI cannot fill (output formatting, shell ergonomics, SDK gaps, process-model mismatches), (b) SDK-faithful behavior remains reachable (by default, or via an opt-in flag), and (c) they are cataloged in [`sdk-deviations.md`](sdk-deviations.md) with their rationale.

2. **Security overrides parity.** Credentials and other secrets must never appear in user-visible output, even when raw SDK behavior would expose them. The `--debug` HTTP log redaction (`HttpClientAuthRedactor` in `redactor.py`) is the canonical example: it deviates from the SDK's verbose HTTP logging to mask authorization headers.

3. **The command-line interface is resolved at runtime.** The command tree and options are built at startup by introspecting the installed `python-asana` package. An SDK bump completes with a dependency update and a snapshot fixture refresh; nothing else.

4. **Python 3.10+ is supported.** `pyproject.toml`'s `requires-python = ">=3.10"` is maintained.

5. **Windows is a first-class citizen.** cp932 / UTF-8 / Excel CSV / WSL workflows must work. With no CI, Windows verification is manual.

6. **The `asana-api` CLI is the only supported interface.** Importing `asana_api_cli` as a Python library is not a supported use case. Its module layout, classes, and functions (e.g. `AsanaSession`) are internal implementation details: they carry no stability guarantee, may change at any time without a deprecation period, and such changes are neither announced nor recorded (not in the `CHANGELOG`, not via deprecation warnings). Only the command-line surface — commands, options, output formats, and exit codes — is the product whose compatibility this project manages.

7. **Environments up to three years old are supported, but none from before 2023-12-16.** The oldest supported date is the later of "three years ago" and 2023-12-16, the release of python-asana 5.0.2 (the SDK version this project targets). An environment as of a date means the package releases PyPI offered that day. `pip install asana-api-cli` must work in such an environment without upgrading the packages it already has. Each runtime dependency floor in `pyproject.toml` therefore stays at a release that was already available on the oldest supported date. A floor is never raised for developer convenience, e.g. because an old release has no wheel for a newer Python — pip there picks a newer release. The Python interpreter itself is bounded by #4. What is guaranteed is that the CLI *works*, not that it behaves identically everywhere: behavior follows the installed packages. Wrapping an older python-asana, the CLI behaves as that SDK does (the parity of #1 is with the installed SDK), which may differ from newer SDK releases. Likewise, `--query` runs the jq language of the installed `jq` package — e.g. `jq` < 1.9.1 rejects a carriage return between tokens.

## Terminology

| Term | Refers to |
|---|---|
| **`python-asana` SDK** (short: **the SDK**) | The official Asana Python client. Distributed on PyPI as `python-asana`, imported as `asana`. |
| **`asana-api-cli`** | This project / pip-installable package name. |
| **`asana-api`** | The CLI executable produced by this project (what users actually run). |
