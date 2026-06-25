# Releasing

Process for cutting a tagged release of this repo.

## When to cut a release

- Merge to `main` is green (CI passing).
- `scripts/09_validate.py` exits clean.
- `pre_flight` (matchday-intel Phase 12) passes.
- Simulator stability harness passes — same seeds, same hash.
- No open HIGH/CRITICAL findings in the latest `PRESSURE_TEST_R*.md`.

## Semantic versioning rules

- **Major (X.0.0)** — model or schema break. Backtests change. Dashboard
  JSON contract changes. Consumers must rebuild. Examples: switching from
  XGBoost Poisson to a different family, breaking the `live_state.json`
  shape, dropping a column from `predictions.json`.
- **Minor (x.Y.0)** — new feature, additive. Existing JSON shapes preserved.
  Examples: new live-intelligence layer, new dashboard section, new audit
  output.
- **Patch (x.y.Z)** — bug fix, hardening, doc-only changes, CI fix. No
  behavioural change for honest inputs.

## Cut the release

```bash
# 1. Update the changelog
$EDITOR CHANGELOG.md

# 2. Write the release notes
cp docs/releases/RELEASE_NOTES_v3.0.0.md \
   docs/releases/RELEASE_NOTES_vX.Y.Z.md
$EDITOR docs/releases/RELEASE_NOTES_vX.Y.Z.md

# 3. Commit
git add CHANGELOG.md docs/releases/RELEASE_NOTES_vX.Y.Z.md
git commit -m "release: vX.Y.Z"

# 4. Tag (annotated, signed if configured)
git tag -a vX.Y.Z -m "vX.Y.Z — <one-line summary>"

# 5. Push tag + branch
git push origin main
git push origin vX.Y.Z          # or: git push --tags

# 6. Publish the GitHub release
gh release create vX.Y.Z \
    --title "vX.Y.Z — <one-line summary>" \
    --notes-file docs/releases/RELEASE_NOTES_vX.Y.Z.md
```

## Pre-release checklist

- [ ] `scripts/09_validate.py` passes
- [ ] `pre_flight` Phase 12 passes
- [ ] Simulator stability harness passes (deterministic hash)
- [ ] `CHANGELOG.md` updated with user-visible changes
- [ ] `docs/releases/RELEASE_NOTES_vX.Y.Z.md` written
- [ ] No HIGH/CRITICAL audit findings open
- [ ] Live `live_state.json` endpoint reachable and current
