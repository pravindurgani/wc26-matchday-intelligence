# WC26 Matchday Intelligence — Pressure-Test Round 2

**Date**: 2026-06-17
**Branch**: `hardening/r32-pressure-test-r2` (local — push gated per instruction)
**Suite delta**: 990 → **1059 passed**, 1 skipped, 0 failed, 0 xfailed (`tests/live/`)
**Σ-gate**: exit 0 on `data/processed/predictions_live.json` (|Δ| = 0, teams = 48, tol = 1e-6)
**Standing constraints preserved**: no retrains, no threshold/cap changes, `AUTO_TIER_ACTIVE=False`
(`scripts/live/injury_adjustments.py:64`), `NB_ALPHA=5.0`, `DC_RHO=-0.13`, `MAX_G=10`,
`STALENESS_MAX_AGE_HOURS=6.0`.

## What this round did

Round 1 (S0–S9 + Wave 4) closed all pre-triaged findings, raised the suite from 916 → 990,
and produced four deliverables locally. Round 2 — driven by an independent audit — closes the
two residual gaps that audit confirmed:

| ID | Severity | Finding | Status |
|----|----------|---------|--------|
| **P1a** | High | `export_ko_advance._build_nb_dc_matrix` had no test comparing it to `03_simulate.build_score_matrix`. Existing KO agreement test ran 0 iterations pre-R32 (vacuous coverage). | **Closed** |
| **P1b** | Low (already wired) | Producer-failure resilience: workflow `set +e ... exit 0` + apply's `default={}` + degrade_subsystem already correct; existing tests at `test_apply_matchday_adjustments.py:786-1115` already pin the contract. | **Verified-clean** |
| **P1c** | Medium | Fast-path freshness propagation: `get_team_elo_adjustment()` returns a float — no warning channel; stale matchday adjustments silently applied. | **Closed** |
| **Monitor-catch** | Medium | Initial P1c implementation propagated freshness only to *late* `write_live_state` paths; the three early-exit guards (circuit breaker, fetch failure, input corruption) were missed. | **Closed in same round** |

---

## P1a — KO export matrix vs production sim's matrix

### Root cause

Three independent re-implementations of the NB(α=5.0) × DC(τ=−0.13) joint matrix existed:

1. `scripts/03_simulate.py:186-208` — `build_score_matrix` (scipy + numpy, production).
2. `scripts/live/export_ko_advance.py:140-165` — `_build_nb_dc_matrix` (pure-Python, lgamma-based;
   deliberately avoids scipy/numpy so the post-processor stays tiny).
3. `tests/live/test_goal_grid_feed_agreement.py:125+` — local `wdl_nb_dc` (scipy-based).

`tests/live/test_ko_advance_export.py:test_single_resolved_ko_writes_one_entry` asserted that
the export's emitted WDL equals `_wdl_from_matrix(_build_nb_dc_matrix(1.6, 1.1))` — **circular**
(both come from the same module).

`tests/live/test_goal_grid_feed_agreement.py::test_ko_advance_agreement` iterates
`match_predictions_ko` which is empty pre-R32 → 0 parametrize cells → "passes by definition."

### Fix

New file `tests/live/test_ko_matrix_equality.py` (399 lines, 55 tests):

- `test_export_constants_match_production`: pins NB_ALPHA / DC_RHO / MAX_G drift detection.
- `test_export_matrix_matches_sim_matrix_grid[lam_h-lam_a]`: cell-for-cell equality between
  `sim.build_score_matrix` (loaded via `importlib.spec_from_file_location` — same pattern as
  `tests/live/test_decide_knockout.py:30-35`) and `export._build_nb_dc_matrix`, on a 5×5 λ grid
  {0.4, 0.9, 1.4, 1.8, 2.6} covering the realistic WC26 range. Tolerance ≤1e-12 per cell.
- `test_export_wdl_matches_sim_wdl_grid[lam_h-lam_a]`: same grid, comparing the two
  `wdl_from_matrix` extractors at ≤1e-12.
- `test_end_to_end_export_matches_sim_truth[lam_h-lam_a]`: drives the export's
  `export()` entrypoint on a synthetic resolved-KO fixture (Argentina vs Brazil + variants)
  and asserts the emitted `p_home_win / p_draw / p_away_win / p_advance_match` match values
  computed via `sim.build_score_matrix` + `sim.wdl_from_matrix` at ≤1e-12 — replaces the
  vacuous pre-R32 coverage with real cross-module pinning.

### Verification

```
python3 -m pytest tests/live/test_ko_matrix_equality.py -v
========== 55 passed in 0.51s ==========
```

Empirically observed worst-case cell |Δ| over the entire grid: **5.5e-17** (well below the
1e-12 tolerance — three decades of headroom against floating-point round-off, ten decades
against any real regression).

---

## P1c — Fast-path freshness propagation

### Root cause

The fast workflow (`scripts/live/run_live_update.py`, every ~10 min) calls
`scripts/03_simulate.py`, which calls
`scripts/live/apply_matchday_adjustments.py:get_team_elo_adjustment(team)`. That function
returns a **float** — no warning channel. The freshness guard at
`scripts/live/apply_matchday_adjustments.py:128-198` (added in round 1) populates
`state["degradation_warnings"]`, but those warnings never reach `live_state.json`. Net: if the
slow workflow (every 3h) stalled, the fast tick silently kept applying stale matchday Elo.

Confirmed independently by an investigation agent reading
`scripts/03_simulate.py:693` + `scripts/live/apply_matchday_adjustments.py:1087-1109`.

### Fix

**`scripts/live/apply_matchday_adjustments.py:200-331`** — new helper
`get_matchday_freshness_warnings()` returning a list of live_state-shaped `{type, message}`
dicts. Surfaces three failure modes:

- `matchday_consolidated_missing`: `dashboard/matchday_intelligence.json` absent (slow
  workflow never ran on this host).
- `matchday_consolidated_stale`: consolidated file's mtime is more than
  `STALENESS_MAX_AGE_HOURS=6.0` (= 2 slow-cron ticks) older than `results_2026.json`'s mtime.
- `matchday_subsystem_stale`: consolidated file is itself current but its
  `degradation_warnings` contain `subsystem_stale` entries from referee / suspension /
  player_stats producers underneath.

Plus defensive handling: `matchday_consolidated_unreadable` (stat failure) and
`matchday_consolidated_unparseable` (truncated JSON) — the helper never raises.

**`scripts/live/run_live_update.py:66-98`** — new safe-wrapper
`_matchday_freshness_warnings_safe()`. Catches any exception from the helper and degrades to a
single `matchday_freshness_check_error` warning. A freshness probe that crashes the tick would
defeat the entire point of the probe.

**`scripts/live/run_live_update.py:431-498`** — probe ONCE at the top of `main()` so all six
`write_live_state(...)` exit paths carry the signal:

- Circuit-breaker tripped (line 442)
- Fetch failure (line 455)
- Input corruption (line 469)
- Unchanged inputs / dry-run / missing artifacts (lines 491, 499, 519)
- Sim failure / success (lines 559, 600)

### Verification

```
python3 -m pytest tests/live/test_fast_path_freshness.py -v
========== 14 passed in 0.02s ==========
```

The 14 tests pin (a) missing file, (b) stale file, (c) embedded subsystem_stale,
(d) clean tick, (e) helper crash → safe-wrapper, (f) static early-exit propagation
(circuit breaker, fetch failure, input corruption all merge `mf_warnings`).

### Independent monitor catch + fix in same round

The fresh monitor that verified the P1a fix correctly identified that the initial P1c
implementation appended `mf_warnings` *after* `get_results_warnings()` — past the three
early-exit guards. Three minutes later the fix moved the probe to the top of `main()` and
folded `mf_warnings` into each guard's isolated warning array. Three new static-assertion
tests (`test_circuit_breaker_exit_merges_mf_warnings`,
`test_fetch_failure_exit_merges_mf_warnings`,
`test_input_corruption_exit_merges_mf_warnings`) pin the early-exit propagation against future
refactors.

---

## P1b — Producer-failure resilience (verified-clean, no change)

Investigation agent confirmed the degrade-don't-crash contract is already wired:

- `.github/workflows/matchday-intel-slow.yml:113-162` — all 4 new producers (fetch_results
  --with-events, fetch_player_stats, referee_adjustments, suspension_tracker) wrapped in
  `set +e ... exit 0`. One producer failure does NOT block subsequent producers or `apply`.
- `scripts/live/apply_matchday_adjustments.py:96-105` (`_read_json` with `default={}`) +
  `128-198` (`_check_freshness`) + the `degrade_subsystem()` wrappers at
  `apply_matchday_adjustments.py:800-817` — missing/empty producer output → neutral zero
  adjustment + `subsystem_stale` warning + `apply` still emits a valid consolidated file.
- Existing tests at `tests/live/test_apply_matchday_adjustments.py:786-1115`
  (`TestRound5DegradationPerSubsystem` + `TestRound5CatastrophicFailure` + `TestFreshnessGuard`)
  already pin every producer-failure mode. Each missing/stale producer is tested; the
  catastrophic exit-1 path only triggers when ALL subsystems fail simultaneously.

No production code change in this area. The P1b audit-flag closes as "already correctly
wired."

---

## Σ-gate + standing-constraint check

| Invariant | Value | Status |
|-----------|-------|--------|
| Σ p_champion across 48 teams | 1.0 (\|Δ\| = 0.000e+00) | ✓ |
| `check_invariants.py` exit code | 0 | ✓ |
| Σ-gate tolerance | 1e-6 (strict) | ✓ |
| `AUTO_TIER_ACTIVE` | False (`scripts/live/injury_adjustments.py:64`) | ✓ |
| `NB_ALPHA` (dispersion) | 5.0 | ✓ |
| `DC_RHO` | −0.13 | ✓ |
| `GOAL_GRID_MAX_GOALS` | 10 | ✓ |
| `STATS_CAP_TOURNAMENT_TOTAL` | 20.0 | ✓ |
| `GRAND_TOTAL_CAP` | 45.0 | ✓ |
| `STALENESS_MAX_AGE_HOURS` | 6.0 | ✓ |
| TODO / FIXME / XXX / HACK in `scripts/` & `scripts/live/` | 0 | ✓ |
| xfailed tests | 0 | ✓ |
| failed tests | 0 | ✓ |
| Documented skips | 1 (the pure-Sheets-I/O `.gs` helper with no math core) | ✓ |

---

## Bottom line

Round 2 closes the only residual high-severity defect (P1a: matrix circularity) and the
one medium-severity gap (P1c: fast-path freshness blind spot), both confirmed by an
independent audit. The independent verification monitor caught a subtle early-exit gap in the
initial P1c implementation; that gap was closed in the same round and pinned by three
positive-assertion tests. The matchday layer is ready for branch commit (push remains
human-gated per instruction).
