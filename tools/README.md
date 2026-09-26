# Tools

Maintenance scripts for this repository. Run them from anywhere — each one
locates the repository root itself.

| Script | Purpose |
|---|---|
| [`test_python_versions.sh`](#test_python_versionssh) | Run the test suite on several Python versions |
| [`e2e_init.py`](#e2e_initpy) | Provision the standing fixtures the e2e tests need in the test workspace |
| [`tag_version.sh`](#tag_versionsh) | Create the annotated `vX.Y.Z` git tag from the `pyproject.toml` version |
| [`publish_pypi.sh`](#publish_pypish) | Build the sdist / wheel and upload them to PyPI by hand |

## `test_python_versions.sh`

Runs `pytest` once per Python version — by default 3.10 (the supported
minimum), 3.12, and 3.14.

```bash
bash tools/test_python_versions.sh                    # 3.10, 3.12, 3.14
bash tools/test_python_versions.sh 3.11 3.13          # chosen versions
bash tools/test_python_versions.sh --lowest           # declared minimum dependency versions
bash tools/test_python_versions.sh -- -x tests/test_codegen.py   # extra pytest args
bash tools/test_python_versions.sh --lowest 3.14 -- -k codegen   # combined
```

- Each version runs in a throwaway virtualenv that is deleted on exit, so the
  project's `.venv` and `uv.lock` are untouched.
- Dependencies are installed at the versions pinned in `uv.lock` (`--locked`).
  With `--lowest`, the direct dependencies are installed at the minimum versions
  `pyproject.toml` declares instead (`--resolution lowest-direct --isolated`;
  see [`tests/README.md` §Lower-bound versions](../tests/README.md#lower-bound-versions)),
  still without rewriting `uv.lock`.
- The installed versions of the runtime dependencies (`asana`, `click`, ...)
  are printed before each run, so the log shows what was actually tested.
- The interpreter is always a uv-managed CPython (`--managed-python`),
  downloaded on first use, so the result does not depend on which `python` is
  on `PATH`.
- Every version runs even if an earlier one fails. A summary is printed at the
  end, and the exit status is non-zero if any version failed.

## `e2e_init.py`

Creates the standing fixtures that some e2e tests rely on, in the workspace
named by `ASANA_PYTEST_WORKSPACE`. Needed only for live mode
(`pytest --live`), once per workspace; replay mode uses the committed
cassettes. See [`tests/e2e/README.md`](../tests/e2e/README.md) for the e2e
workflow.

```bash
export ASANA_ACCESS_TOKEN=...
export ASANA_PYTEST_WORKSPACE=<test-dedicated workspace gid>
uv run python tools/e2e_init.py
```

Provisioned per workspace:

- Project `pagination-test` with 1500 tasks (`ptest-0001` .. `ptest-1500`).
- Project `pagination-test-small` with 50 tasks (`psmall-0001` ..
  `psmall-0050`); used to verify `--full-payload` /
  `--no-return-page-iterator` behavior *below* Asana's per-response cap
  (~1000 items).

The script is idempotent — safe to re-run. Existing projects are reused,
missing tasks are created, and every other task in those projects (an
unexpected name or a duplicate) is deleted, so each project ends up with
exactly the expected set. Treat the projects as test-dedicated. Transient
failures (HTTP 5xx / 429) are retried with backoff, and writes are spaced at
least 0.5 s apart to stay under Asana's per-minute rate limit, so the first
full run takes roughly 12 minutes (1500 task creations × 0.5 s).

## `tag_version.sh`

Creates an annotated tag `v<version>` (message `Release <version>`) on the
current commit, reading the version from `pyproject.toml`. It shows the
version, tag, and commit and asks for confirmation first, and refuses if the
tag already exists. It does not push.

```bash
bash tools/tag_version.sh
```

In the release procedure ([`docs/release.md`](../docs/release.md)), the tag
goes on the merge commit on `main`, so run it with `main` checked out after
the merge.

## `publish_pypi.sh`

Builds the sdist and wheel and uploads them with twine, for publishing by
hand. The normal release path does not need it: pushing the tag triggers
`.github/workflows/publish.yml`, which builds and uploads to PyPI (see
[`docs/release.md`](../docs/release.md)).

```bash
bash tools/publish_pypi.sh          # upload to PyPI
bash tools/publish_pypi.sh --test   # upload to TestPyPI
```

It runs `uv run pytest` first and aborts on failure, wipes `build/`, `dist/`,
and `src/*.egg-info` so stale files cannot leak into the build, builds with
`python -m build`, lists the artifacts, and asks for confirmation before
uploading. Build and upload run through `uv run`, so the dev-group `build` /
`twine` are used. twine prompts for credentials: use `__token__` as the
username and an API token as the password.
