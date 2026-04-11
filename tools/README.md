# Code generation

`codegen.py` auto-generates the CLI modules under `src/asana_api_cli/cli/` by
introspecting the official [python-asana](https://github.com/Asana/python-asana)
SDK at runtime. click arguments, options, and help text are derived mechanically
from each `*Api` method's signature and docstring (`:param` lines), so no
OpenAPI definition file is required.

## Regenerating

After installing or upgrading the official SDK (`asana` package):

```bash
.venv/bin/python tools/codegen.py
```

This overwrites everything under `src/asana_api_cli/cli/`. Do not edit those
files by hand.

## How it works

1. Enumerate all `*Api` classes exported by the `asana` package.
2. For each class, inspect every public method (skip `_`-prefixed and
   `*_with_http_info` variants).
3. Parse the method signature to identify positional parameters (`body`,
   path GIDs) and whether an `opts` dict is accepted.
4. Parse the docstring `:param` lines to extract query-parameter names, types,
   descriptions, and required flags.
5. Emit a click command group per API class and a click command per method.
6. Write `cli/__init__.py` with the top-level `main` group that registers every
   sub-group and exposes global options (`--debug`, `--host`, `--timeout`, etc.).
