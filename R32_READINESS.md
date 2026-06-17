# R32 Readiness Checklist — Post Pressure-Test R2

**Date**: 2026-06-17 (T-11 days from R32 first kickoff: 2026-06-28)
**Branch**: `hardening/r32-pressure-test-r2` (local — push human-gated per instruction)
**Suite**: **1059 passed**, 1 skipped, 0 failed, 0 xfailed (`tests/live/`)
**Σ-gate (real data)**: exit 0, |Δ| = 0.000e+00, teams = 48
**`AUTO_TIER_ACTIVE`**: False at `scripts/live/injury_adjustments.py:64`

---

## Checklist (every row backed by file:line + a pinning test)

| Row | Item | Status | File:line | Test |
|---|---|---|---|---|
| 1 | Knockout schedule loaded into suspension tracker (32 fixtures, m=73..104, stages r32..final) | ✓ | `suspension_tracker.py:121-152` + `_knockout.py:71` | `test_knockout_readiness.py::test_load_schedule_includes_knockout_fixtures` |
| 2 | Yellow-card QF→SF flush per FIFA rule (yellow cards wiped after QF) | ✓ | `suspension_tracker.py:307-313` | `test_knockout_readiness.py::test_qf_to_sf_yellow_does_not_ban_due_to_qf_flush`, `test_qf_flush_only_wipes_unconverted_carry`, `test_qf_yellow_alone_then_sf_yellow_no_final_ban` |
| 3 | Red card in R32 emits next-match ban | ✓ | `suspension_tracker.py:218-229` | `test_suspension_tracker.py::test_red_card_emits_immediate_ban` |
| 4 | Placeholder slots don't trigger phantom bans/polls | ✓ | `_knockout.py:46` + `suspension_tracker.py:155-186` + `fetch_lineups.py:103-130` | `test_knockout_readiness.py::test_next_match_for_team_skips_placeholder_slots` |
| 5 | Lineup polling covers knockout fixtures (32 KO matches now in polling set) | ✓ | `fetch_lineups.py:103-130` | `test_knockout_readiness.py::test_fetch_lineups_loads_group_and_knockout_schedule` |
| 6 | Same-player injury + suspension dedup (suspension wins) | ✓ | `apply_matchday_adjustments.py:537-629` (helper) + `:682` (call site) | `test_cross_subsystem_invariants.py::TestNoDoubleCountInjuryAndSuspension` (3 tests) |
| 7 | Per-KO-match advance probability exported (W + 0.5·D) | ✓ | `scripts/live/export_ko_advance.py:281-336` + `run_live_update.py:527` (wired) | `test_ko_advance_export.py` (6 tests) + `test_goal_grid_feed_agreement.py::test_ko_advance_agreement` |
| 8 | Goal Grid DC math is the SAME for group and knockout (90-min triple) | ✓ | `wc26-engine-gs/WC26_Engine_AppsScript_v2.3.1.gs` (stage-agnostic) | `test_goal_grid_node.py` (real `.gs` via node) + `test_goal_grid.py` (Python replica pinned to `.gs` at 1e-9) |
| 9 | Σ p_champion = 1.0 on real data, gate exit 0 | ✓ | `scripts/check_invariants.py` (strict 1e-6) | `test_check_invariants.py` (7 tests) + `test_check_invariants_adversarial.py` (19 tests incl. bool poisoning) |
| 10 | Σ-gate rejects bool poisoning (47 False + 1 True no longer sums to 1.0) | ✓ | `check_invariants.py:169` | `test_wave4_adversarial.py::TestCheckInvariantsRejectsBools` (3 tests) |
| 11 | Orchestrator gracefully degrades on per-record fail (skip-and-warn, no tick crash) | ✓ | `apply_matchday_adjustments.py` (via `_degrade.py`) | `test_apply_matchday_adjustments.py::TestRound5DegradationPerRecord` |
| 12 | Orchestrator gracefully degrades on per-subsystem fail | ✓ | `apply_matchday_adjustments.py` + `_degrade.py` | `TestRound5DegradationPerSubsystem` |
| 13 | Catastrophic fail (all subsystems degraded) returns exit 1 | ✓ | `apply_matchday_adjustments.py:1010+` | `TestRound5CatastrophicFailure` |
| 14 | Freshness guard fires when ref/sus/player_stats older than 6h | ✓ | `apply_matchday_adjustments.py:128-198` + 3 call sites | `TestFreshnessGuard` (7 tests) |
| 15 | `expires_at` malformed values emit `bad_expires_at` warning (no silent overlay) | ✓ | `apply_matchday_adjustments.py:282` | `test_wave4_adversarial.py::TestExpiresAtMalformedWarns` (6 tests) |
| 16 | Schema-drift detected on all 7 fetch_* endpoints (soft mode) | ✓ | `fetch_results.py:386, 517` + `fetch_injuries.py:142` + `fetch_lineups.py:224` + `fetch_match_stats.py:156` + `fetch_player_stats.py:269, 318` | `test_fetch_schema_wiring.py` (13 tests) + `test_fetch_results_schema.py` (6) + `test_wave4_adversarial.py::TestFetchWeatherSchemaWatchdog` |
| 17 | `fetch_weather.py` defensive `.get()` walk (no schema-watchdog needed) | ✓ documented contract | `fetch_weather.py` | `TestFetchWeatherSchemaWatchdog` (pins the contract) |
| 18 | `net_injury_elo` clamped to `[elo, 0]` (injury never beneficial, never worse than zero-replacement) | ✓ | `injury_adjustments.py:498` | `TestNetInjuryEloClamp` (4 tests) |
| 19 | `key_players_2026.json` validator (`replacement.elo_equiv ∈ [tier_elo, 0]`) over all 108 entries | ✓ green | `pre_flight.py:990` + `:984-993` (Phase 12 wire) | `test_key_players_config.py` (8 tests) |
| 20 | Key-player coverage 48/48 (Cape Verde, Curacao, Haiti added) | ✓ | `data/raw/key_players_2026.json` (S6 additions) | `test_key_players_coverage.py` (3 classes) |
| 21 | Real `.gs` Dixon-Coles harness EXECUTES via node (not silent-skipped) | ✓ | `tests/live/_node_resolver.py` + `wc26-engine-gs/test_harness.mjs` | `test_node_resolver.py` (5 tests incl. CI hard-fail subprocess) + `test_goal_grid_node.py` (11 tests + 1 skip) |
| 22 | CI hard-fail when node is missing on a CI runner | ✓ | `_node_resolver.py:41-47` | `test_node_resolver.py::test_ci_with_no_node_raises_at_import` (subprocess) |
| 23 | Phase 2/3/4 producers run on every 3h slow tick | ✓ | `.github/workflows/matchday-intel-slow.yml:106-162` | `test_workflow_yaml.py` (6 tests) |
| 24 | Phase 2/3/4 outputs committed to main on every 3h slow tick | ✓ | `.github/workflows/matchday-intel-slow.yml:263-265` | `test_workflow_yaml.py::test_git_add_includes_three_new_outputs` |
| 25 | `STATS_CAP_GROUP_TOTAL` renamed → `STATS_CAP_TOURNAMENT_TOTAL` (value 20.0 unchanged) | ✓ | `apply_matchday_adjustments.py:79` + 9 other sites | `test_stats_cap_name.py` (2 tests pinning rename) |
| 26 | launchd preview deployer points at the correct repo | ✓ | `scripts/launchd/run_if_tournament.sh:22-23` (script-dir-relative) + `:25-28` (.git guard) | `test_launchd_path.py` (3 tests) |
| 27 | Calibration probe baseline recorded; CWC2025 backtest log loss (0.957) stays the pre-tournament signal until N ≥ 30 | ✓ | `reports/calibration_baseline_pre_r32.md` | `test_calibration.py` (11 tests) |
| 28 | `AUTO_TIER_ACTIVE = False` (shadow mode per CORRECTIONS.md §7) | ✓ | `injury_adjustments.py:64` | `test_knockout_readiness.py::test_auto_tier_active_remains_false` |
| 29 | Round-4 math-layer raises preserved (orchestrator catches; math doesn't silently pass NaN/inf) | ✓ | `injury_adjustments.py` (NaN/inf checks), `stats_proxy_adjustments.py`, `referee_adjustments.py` | 34 silent-failures-to-positive tests across 8 adversarial files |
| 30 | `export_ko_advance` wired into the live runner (not just tested) | ✓ | `run_live_update.py:527` (Step 4b) | `test_wave4_adversarial.py::TestExportKoAdvanceWired` (2 tests: present + ordered correctly) |
| 31 | **NEW R2 (P1a):** KO export matrix cell-for-cell equal to production sim's `build_score_matrix` (no circular self-check) | ✓ | `scripts/live/export_ko_advance.py:140-165` proven = `scripts/03_simulate.py:186-208` | `test_ko_matrix_equality.py` (**55 tests**: 25-pair grid + WDL grid + end-to-end synthetic at ≤1e-12) |
| 32 | **NEW R2 (P1a):** Real-data coverage for KO export pre-R32 (not "passes by definition" with `match_predictions_ko` empty) | ✓ | `test_ko_matrix_equality.py:test_end_to_end_export_matches_sim_truth` | Synthetic resolved-KO fixture (Argentina vs Brazil + 3 λ variants), driven through `export()` entrypoint, asserted against production sim's matrix |
| 33 | **NEW R2 (P1c):** Fast-path freshness propagation — matchday staleness reaches `live_state.json` on all 6 exit paths | ✓ | `apply_matchday_adjustments.py:200-331` (helper) + `run_live_update.py:66-98` (safe-wrap) + `:435-498` (6 exit paths fold in `mf_warnings`) | `test_fast_path_freshness.py` (**14 tests**: 3 modes + clean + safe-wrap + 3 early-exit static pins) |
| 34 | **NEW R2:** Producer-failure resilience independently verified (`set +e ... exit 0` + `default={}` + `degrade_subsystem`) | ✓ verified-clean | `.github/workflows/matchday-intel-slow.yml:113-162` + `apply_matchday_adjustments.py:800-817` | Existing tests at `test_apply_matchday_adjustments.py:786-1115` (no new code needed) |
| 35 | Zero TODO/FIXME/XXX/HACK in `scripts/` and `scripts/live/` | ✓ | `grep -rn "TODO\|FIXME\|XXX\|HACK" scripts/` returns nothing | (manual probe) |
| 36 | Zero `xfail` in the suite | ✓ | `pytest --tb=no -q` reports 0 xfailed | (suite-wide) |
| 37 | Zero threshold/cap value changes (NB_ALPHA=5.0, DC_RHO=-0.13, MAX_G=10, STALENESS_MAX_AGE_HOURS=6.0 all unchanged) | ✓ | `test_ko_matrix_equality.py::test_export_constants_match_production` | + value-pinning tests |

---

## One-screen GO / NO-GO for merge to `main`

### GO

The 37-row checklist above is fully green on the local working tree. Cumulative suite arc:

- Pre-Round 4: 766 → Post-Round 6: 916 → Post-Round 1 pressure-test: 990 → **Post-Round 2 pressure-test: 1059** (+293 net tests).
- **+69 R2-specific tests**: 55 matrix-equality (P1a) + 14 freshness-propagation (P1c).
- **Σ-gate**: extended (MalformedJson + bool rejection), strict 1e-6 tolerance, real-data exit 0.
- **Round 2 closes the last two residual defects** from the independent audit:
  - P1a: no more circular self-check on the KO export's NB+DC matrix
  - P1c: no more silent staleness on the fast path's `live_state.json`
- **P1b verified-clean**: producer-failure resilience already correctly wired; no code change needed.
- **Independent-monitor catch in R2**: the fresh monitor flagged 3 early-exit guards (circuit breaker, fetch failure, input corruption) where the initial P1c implementation missed propagating freshness; fixed in same round + pinned by 3 new static-assertion tests.

### NO-GO (only items that genuinely cannot be closed pre-R32 — residual register)

| # | Item | Why it stays open | When it closes |
|---|---|---|---|
| R1 | First-tick re-baseline of 5 synthetic schema baselines | They're synthetic constructions from API-Football v3 docs + the parser's `.get()` chain. The first real live fetch may surface drift (logged in soft-mode, no crash) — user re-runs `python3 scripts/live/_schema_watchdog.py snapshot <response> <baseline>` to lock in the real shape. | Within 1 slow tick (3h) of merge |
| R2 | `match_predictions_ko[]` populated on real data | Today, 0 KO matches are resolved (group stage in progress; bracket has slot codes). The export pipeline is wired and idempotent; entries appear automatically once group standings lock and `knock_lambdas_table` is emitted by the sim. The R2 P1a synthetic fixture provides cross-module coverage in the meantime. | 2026-06-26 (group stage end) |
| R3 | Calibration baseline at N ≥ 30 | Today N=5 (m=1..5); the CWC2025 backtest log loss (0.957) is the pre-tournament signal. Calibration probe will auto-update as the live updater ticks and group fixtures complete. | 2026-06-23 (mid group stage onwards) |
| R4 | Branch push (`hardening/r32-pressure-test-r2`) | Per user instruction this round: "DONT TOUCH THE MAIN BRANCH AT ALL, AND DONT PUSH/DEPLOY." Branch committed locally; user pushes manually when ready. | User-paced |
| R5 | Vercel deploy | Human-gated by design. After merge to `main`, the Vercel deploy is triggered by the user. | User-paced |

**No residual items that would force a NO-GO on merge.** R1-R3 are time-natural progressions, not blockers. R4 + R5 are explicit human-gated steps per instruction.

---

## Recommendation

**The matchday layer is merge-ready.** R2 closes the two residual high/medium-severity defects from the independent audit and adds 69 new positive-assertion tests. The remaining residuals are either time-natural (R1-R3) or explicitly human-gated (R4, R5).

After the user pushes `hardening/r32-pressure-test-r2`, merges to `main`, and triggers a deploy, the next 3h `matchday-intel-slow.yml` tick will:

1. Refresh `data/live/{player_stats,referee,suspensions}_2026.json` for the first time on CI
2. Re-run `apply_matchday_adjustments.py` with the full hardened stack (now including the P1c freshness helper)
3. Auto-commit the producer outputs (now in the `git add` allow-list)
4. Trigger the next `live-matchday.yml` tick at the regular cadence — and any staleness on this or subsequent ticks now surfaces on `live_state.json` (P1c), not just `dashboard/matchday_intelligence.json`

R32 first kickoff (2026-06-28) is 11 days away — two slow-tick cycles to validate live behavior before the knockouts begin.
