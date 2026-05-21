# Release

Release steps run from a developer machine; PyPI publication is automated via tag push.

1. On the working branch, make a single `chore: release X.Y.Z` commit that bumps:
   - `pyproject.toml` `version`
   - `uv.lock` (run `uv lock` to refresh)
   - `CHANGELOG.md`: rename the `[Unreleased]` heading to `[X.Y.Z] - YYYY-MM-DD`, then update the link section at the bottom — add a new `[X.Y.Z]: .../compare/v<prev>...vX.Y.Z` line and repoint `[Unreleased]` to `.../compare/vX.Y.Z...HEAD`.
2. Merge to `main` with `git merge --no-ff <branch>`, so the merge commit is preserved as a clear release boundary.
3. Tag the **merge commit on `main`** with `git tag vX.Y.Z`.
4. Push everything in one go: `git push origin main <branch> vX.Y.Z`.

The tag push triggers `.github/workflows/publish.yml`, which builds and uploads to PyPI.
