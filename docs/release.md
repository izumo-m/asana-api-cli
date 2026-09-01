# Release

Release steps run from a developer machine; PyPI publication is automated via tag push.

1. Refresh the development-side dependencies so the release isn't cut with
   stale or yanked dev tooling: `uv sync --upgrade-group dev`, review the
   resolver output for warnings (e.g. a yanked package), run `uv run pytest`
   (plus `ruff` / `basedpyright` if those moved — a new version may flag new
   findings), and commit the result as `chore: update locked dependencies`.
   Runtime dependencies (`asana`, `click`, ...) are deliberately **not** part
   of this refresh — the released package resolves them at install time, and
   an `asana` upgrade goes through
   [`development.md` §Bumping the asana SDK](development.md#bumping-the-asana-sdk).
2. On the working branch, make a single `chore: release X.Y.Z` commit that bumps:
   - `pyproject.toml` `version`
   - `uv.lock` (run `uv lock` to refresh)
   - `CHANGELOG.md`: rename the `[Unreleased]` heading to `[X.Y.Z] - YYYY-MM-DD`, then update the link section at the bottom — add a new `[X.Y.Z]: .../compare/v<prev>...vX.Y.Z` line and repoint `[Unreleased]` to `.../compare/vX.Y.Z...HEAD`.
3. Merge to `main` with `git merge --no-ff <branch>`, so the merge commit is preserved as a clear release boundary.
4. Tag the **merge commit on `main`** with `git tag vX.Y.Z`.
5. Push everything in one go: `git push origin main <branch> vX.Y.Z`.

The tag push triggers `.github/workflows/publish.yml`, which builds and uploads to PyPI.
