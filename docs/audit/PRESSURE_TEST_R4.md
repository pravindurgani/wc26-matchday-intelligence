# WC26 Matchday Intelligence — Pressure-Test Round 4 (deep adversarial sweep)

**Date**: 2026-06-17
**Branch**: `hardening/r32-pressure-test-r2` (local-only; push human-gated)
**Suite delta**: 1086 → **1086 passed**, 1 skipped, 0 failed (YAML-only change; no Python paths touched)
**Σ-gate**: exit 0 on `data/processed/predictions_live.json` (|Δ| = 0, teams = 48, tol = 1e-6)
**Standing constraints preserved**: no retrains, no threshold/cap changes,
`AUTO_TIER_ACTIVE=False`, `NB_ALPHA=5.0`, `DC_RHO=-0.13`, `MAX_G=10`,
`STALENESS_MAX_AGE_HOURS=6.0`.

## Context — why Round 4

R3 closed four HIGH-severity audit findings (H1 launchd, H2 crash-handler
freshness, H3 fetcher retries, H4 player-stats rate-limit). The user
ordered a fresh adversarial sweep with explicit instructions: deploy many
agents across orthogonal dimensions, deploy a monitor agent to verify them,
and only commit findings that survive verification.

Five parallel adversarial Explore agents probed:

1. R3 fix integration probe (regression hunting on the four R3 fixes)
2. Test suite quality + coverage gaps
3. Production reliability (CI workflows + observability)
4. Modeling validity + calibration audit
5. Knockout-stage + tournament-end edge cases

A sixth **monitor agent** independently verified the orchestrator's
filtering decisions on each agent's reported findings, file:line in hand.

## Triage outcome

| Agent | Reported severity | Verified severity | Action |
|-------|-------------------|-------------------|--------|
| 1 (R3 probe) | "CRITICAL × 4" | Mostly false positives (monotonic-clock, sys.path-race) | None |
| 2 (test suite) | "CRITICAL × 2 + HIGH × 2" | Standard pytest patterns / decorated-skip / already-tested | None |
| 3 (production) | "HIGH × 2 + MEDIUM × 1" | 1 genuine HIGH (G1 below); rest false positives | **G1** |
| 4 (modeling) | "CRITICAL × 3 + HIGH × 4" | Modeling-parameter choices (DC_RHO, NB_α, lambda_noise_α, ref_date freeze) — out of scope for ops pressure test | None |
| 5 (KO edges) | "CRITICAL × 1 + HIGH × 1" | Defensive code paths with graceful skip; not regression vectors | None |

**Single actionable HIGH after monitor verification: G1.**

## G1 — slow-workflow push failure swallowed

### Root cause

`.github/workflows/matchday-intel-slow.yml:278-280` swallowed both `git pull
--rebase` failure (via `|| echo`) and `git push` failure (via `|| echo
...; exit 0`). If the push 403'd or rebased onto a diverged remote, the
job went **green** while the slow workflow's matchday-intel files
(injuries / weather / lineups / referee / suspensions) silently failed to
land on `origin/main`. The next 3h tick would re-stage the same files —
recovery is automatic — but the failure was invisible to anyone watching
the Actions UI.

The fast workflow (`.github/workflows/live-matchday.yml:283-298`) was
hardened to the correct pattern in an earlier round; the slow workflow
was missed.

### Fix

`.github/workflows/matchday-intel-slow.yml:278-296` now mirrors the
fast-workflow pattern:

```yaml
git pull --rebase --autostash origin "$BRANCH" || echo "Rebase failed — pushing anyway."
if ! git push origin "HEAD:$BRANCH"; then
  echo "::error::git push to $BRANCH failed — matchday intel not refreshed this tick. Next scheduled tick will retry."
  exit 1
fi
```

`--autostash` is defensive-only here (the slow workflow doesn't run the
simulator, so `data/processed/` should stay clean), but matches the
fast-workflow shape so they stay symmetrical.

Recovery is still automatic — next tick re-runs all producers
idempotently and re-stages the equivalent commit — but the failure is now
visible in the Actions UI so a human can investigate auth / branch
protection / non-fast-forward storms instead of trusting a false-green.

### Verification

YAML syntax validated (`python3 -c "import yaml; yaml.safe_load(...)"`),
no Python paths touched. Full live suite `pytest tests/live/ -q` still
**1086 passed**, 1 skipped, 0 failed. Σ-gate exit 0 on
`data/processed/predictions_live.json` with |Δ| = 0.

## False positives — what the agents flagged that survived no verification

The monitor agent independently re-read each citation:

- **FP1** Agent 1 claimed `time.monotonic()` in `_http_client.py:132,138`
  has clock-jump risk. **CPython docs:** `monotonic()` cannot go backward
  regardless of NTP / DST / manual clock changes. Claim is wrong by
  definition.

- **FP2** Agent 1 claimed `sys.path.insert` race in the fetch_* shims.
  **Module load is single-threaded**; late imports run *after* the path
  mutation lands. Phantom race.

- **FP3** Agent 2 claimed module-level `sys.path.insert(0, ...)` in 33
  test files is a "CRITICAL test-isolation violation". This is the
  textbook pytest pattern for importing un-packaged scripts; pytest
  collection is deterministic; the suite has been passing on CI for
  months. Refactor candidate, not a regression vector.

- **FP4** Agent 2 claimed `test_goal_grid_node.py:284` is a vacuous test.
  Lines 278-285 wrap it in `@pytest.mark.skip(reason="...covered by
  test_goal_grid.py")`. Documented skip with explicit pointer to the
  covering test.

- **FP5** Agent 3 claimed `os.replace()` cross-filesystem risk at
  `run_live_update.py:620-622`. The temp file is built via
  `dst.with_suffix(dst.suffix + ".tmp")` — same parent directory by
  construction; cross-FS rename is structurally impossible.

- **FP6/FP7** Agent 4 flagged `DC_RHO=-0.13` and `lambda_noise_alpha=12.0`
  as "CRITICAL unfitted parameters". Both are **modeling choices**, not
  operational defects; pinned by `test_goal_grid_feed_agreement.py` at
  1e-9 tolerance so any drift is caught instantly. Re-fitting them would
  require a historical-tournament backtest effort outside the pressure-
  test scope.

- **FP8** Agent 4 flagged `ref_date=pd.Timestamp("2026-06-11")` in
  `scripts/03_simulate.py:681` as a "time-bomb after 2026-07-19".
  **Misread**: `precompute_form_cache` at `scripts/03_simulate.py:271-288`
  filters `matches_df` (the *historical training set* loaded from
  `matches_clean.parquet`) where `date < ref_date` and applies
  exponential decay relative to `ref_date`. The hardcoded date is the
  cutoff that *freezes the training window* so the model is deterministic
  across runs. After 2026-07-19 the form cache remains valid; nothing
  decays into a "future" date.

- **FP9** Agent 4 flagged `0.5 * p_draw` in
  `scripts/live/export_ko_advance.py:425` as a "model inconsistency"
  vs the sim's `pen_elo_slope`. The closed-form export aggregates the
  same MC pen model into a closed-form 50/50 expectation; documented in
  the inline comment at lines 422-425 and pinned by the agreement test.

- **FP10** Agent 5 flagged the L-slot resolution in
  `export_ko_advance.py:410-411` as lacking an integration test. The
  defensive `continue` silently drops an unresolved row — graceful
  degradation, not a crash vector. Annex C table has all 495 keys
  verified (`jq 'length'`), so the silent-skip path is effectively
  unreachable in production data.

## Verdict

**Status**: GREEN — operational pressure test passes.

**Single fix landed locally**: G1 — slow-workflow push observability.
This is a CI surfacing fix, not a behaviour change; the slow workflow
will continue to operate identically on a successful push and will now
fail loudly on a failed push instead of returning false-green.

**Modeling cleanups deferred**: The two micro-cleanups the monitor
suggested (lift `2026-06-11` to a named constant; log a WARNING on
L-slot silent-skip) are non-blocking. They are documented here for a
post-tournament cleanup pass; landing them now would touch the sim hot
path with no operational benefit before R32 kickoff (2026-06-28).

**Tests**: 1086 passed, 1 skipped, 0 failed.
**Σ-gate**: exit 0 on `data/processed/predictions_live.json`.
**Push**: NOT pushed; remains on `hardening/r32-pressure-test-r2`
local-only per instruction.
