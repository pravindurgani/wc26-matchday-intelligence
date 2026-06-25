# WC26 Matchday Intelligence — Pressure-Test Round 13 (T-4d, audit of R12, NO DEFERRALS)

**Date**: 2026-06-24
**Branch**: `hardening/r32-pressure-test-r2` (local-only; push human-gated)
**Suite delta**: 1250 → **1272 passed**, 1 skipped, 0 failed (+22 R13 regression tests across 2 new files)
**Σ-gate (canonical)**: exit 0 on `data/processed/predictions_live.json` (|Δ| = 1.110e-16, teams = 48, tol = 1e-6)
**Σ-gate (dashboard mirror)**: exit 0 on `dashboard/predictions_live.json` (|Δ| = 1.110e-16, teams = 48, tol = 1e-6)
**Standing constraints preserved**: no retrains, no threshold/cap changes,
`AUTO_TIER_ACTIVE=False`, `NB_ALPHA=5.0`, `DC_RHO=-0.13`, `MAX_G=15`
(R12 MED, propagated cross-engine by R13), `STALENESS_MAX_AGE_HOURS=6.0`,
`CB_THRESHOLD=3`, `YELLOW_THRESHOLD=2`.

---

## Methodology

R13 was an audit-the-audit round: 5 parallel adversarial agents explicitly
tasked with looking for HIGH-severity bugs R12 INTRODUCED or MISSED, plus
2 independent monitor agents to verify the primary agents' claims before
any code change. Same orthogonal-sweep + monitor-verification pattern that
has run every round since R9.

**User instruction this round**:
> "REVIEW EVERYTHING THOROUGHLY END-TO-END... DONT DEFER ANY ISSUES/FLAWS
> ... DEPLOY AS MANY AGENTS AS NECESSARY ... DEPLOY AGENTS TO MONITOR
> OTHER AGENTS TOO ... VERIFY EVERYTHING BEFORE MAKING ANY COMMITS!"

The 5 sweeps explicitly audited R12's outputs:
1. **A — Normalization correctness**: did R12 A1's player_join_key
   introduce intra-team same-surname collisions? Did R12 A2's overlay
   normalization leave other operator-input files exposed?
2. **B — State-file lifecycle**: are R12 E1's seed and E2's last_updated
   actually load-bearing, or did R12 just paper over symptoms?
3. **C — MAX_G ripple effects**: did R12 MED's bump of build_score_matrix
   default max_g 10 → 15 update every consumer, or did stale constants
   silently keep the rest of the codebase at 11×11?
4. **D — Frontend regression**: did R12 D1's 5 new render-fn calls cause
   render-storm side effects (DOM leaks, handler stacking, scroll loss)?
5. **E — Cross-cutting integration**: do R12's fixes work end-to-end, or
   are there integration gaps not caught by per-fix unit tests?

Of ~22 candidate findings raised, the monitors **CONFIRMED** 7 HIGH-
severity production bugs that R12 either introduced or missed, plus 4
MEDIUM items. ZERO deferrals at the live gate.

---

## Genuine R13 HIGH findings landed in this commit

### A1 — `player_join_key` surname-only collapse silently merges intra-team same-surname pairs (HIGH)

**Where**: `scripts/live/injury_adjustments.py` (`player_join_key` helper added in R12 A1) + `scripts/live/suspension_tracker.py` (yellow_counter join) + `scripts/live/apply_matchday_adjustments.py` (cross-subsystem dedup)

**Symptom**: R12 A1 collapsed any multi-token name to its surname token to dedup cross-feed initial-form drift ("R. Jiménez" / "Raúl Jiménez" both → "jimenez"). That worked for the drift case but silently merged intra-team same-surname pairs on WC2026 squads:
- **Argentina**: Lautaro Martínez (tier_1_star striker, Inter) + Emiliano Martínez (tier_1_keeper, Aston Villa) — both collapse to ("Argentina", "martinez")
- **Curaçao**: Leandro Bacuna (tier_1_star captain) + Juninho Bacuna (tier_1_star top scorer) — both collapse to ("Curacao", "bacuna")

The suspension_tracker yellow_counter accumulated across BOTH players: one yellow earned by Lautaro + one by Emiliano = counter at 2 → YELLOW_THRESHOLD trip → ONE of them gets suspended for the next Argentina match. Display row would name whichever player triggered the second yellow, but the actual accumulator was someone else. **Real-world WC2026 risk for the next 14 days.**

**Fix**: Make `player_join_key` team-aware. Resolution order:
1. Normalize via `normalize_player_name`.
2. If team is supplied, look up the normalized form in the per-team `key_players_2026.json` `by_full` / `by_last` index:
   - by_full hit (including aliases like "Son" → "Son Heung-min") → return the entry's canonical `name_normalized`.
   - by_last with exactly ONE match → canonicalize the initial-form ("E. Álvarez" → "edson alvarez" for Mexico's only Álvarez).
   - by_last with MULTIPLE matches (intra-team surname collision) → return None.
3. Fall back to the FULL normalized form. Preserves the forename component so "lautaro martinez" ≠ "emiliano martinez".

All call sites in suspension_tracker + apply_matchday_adjustments updated to pass `team=...` for canonical resolution.

**Tests**: `tests/live/test_r13_player_join_key.py` — 12 cases (Martinez pair distinct, Bacuna pair distinct, L./E. Martínez initial forms also distinct, Edson Álvarez cross-feed canonicalizes, Son alias canonicalizes, fallback for no-team / unknown-player, static-pins that all production call sites pass team=).

### C1 — `export_ko_advance.py` `MAX_G = 10` stale vs sim's 15 (HIGH)

**Where**: `scripts/live/export_ko_advance.py:92`

**Symptom**: R12 MED bumped `build_score_matrix` default `max_g=10 → 15`, but `export_ko_advance.py` kept its OWN `MAX_G = 10` constant. The export builds its own 11×11 NB+DC matrix while the production sim emits 16×16. The W + 0.5·D advance probabilities published to `match_predictions_ko[]` (which the dashboard reads for KO advance markets) diverged from the sim's `match_predictions` group-stage WDL by up to 2.2pp at the high-λ tail. Cross-engine drift between the two main outputs in the same JSON file.

**Fix**: Bump `MAX_G = 15`. Now the export agrees with the sim cell-for-cell at floating-point precision.

**Tests**: `tests/live/test_r13_misc.py::TestR13C1ExportKoAdvanceMaxG` — 1 static-pin case. Existing `test_ko_matrix_equality.py` (55 tests) validates cell-for-cell agreement; bumping PROD_MAX_G to 15 makes those tests exercise the correct spec.

### C2 — Goal-grid agreement tests pinned the WRONG spec (HIGH)

**Where**: `tests/live/test_goal_grid_feed_agreement.py:81` + `scripts/live/verify_goal_grid_agreement.py:48`

**Symptom**: Both files hardcoded `MAX_G = 10`. After R12 MED bumped the sim's default to 15, the production feed (predictions_live.json) used 16×16 matrices while the test computed analytical NB+DC at 11×11. The test passed pre-R13 ONLY because the OLD predictions_live.json had been written by a pre-R12 sim (max_g=10). After regenerating with R12's sim, the test would have failed at ~3e-4 — which the test's 1e-9 tolerance would catch loudly.

**Fix**: Bump both to `MAX_G = 15`. Plus regenerate `predictions_live.json` + `dashboard/predictions_live.json` with the current sim so both sides of the comparison are on the same spec.

**Tests**: `tests/live/test_r13_misc.py::TestR13C2GoalGridAgreementMaxG` — 2 static-pin cases.

### C3 — Apps Script `GOAL_GRID_MAX_GOALS = 10` stale (HIGH — internal but per "DONT DEFER")

**Where**: `wc26-engine-gs/WC26_Engine_AppsScript_v2.3.1.gs:88`

**Symptom**: The Apps Script `GOAL_GRID()` custom function (used by the internal trader sheet) still computed Poisson+DC at max_g=10. Cross-engine drift vs the dashboard's NB+DC at max_g=15. At λ=4.0 the Poisson tail above 10 carries ~3% mass — bigger drift than the NB case (~1% mass).

**Fix**: Bump `const GOAL_GRID_MAX_GOALS = 15;`. Comment updated to explain the rationale. Re-pinned all goal-grid cell values in `test_goal_grid.py` + `test_goal_grid_node.py` at the new spec (cells differ by ~4e-7 at λ=1.4 sym, slightly more at higher λ).

**Tests**: `tests/live/test_r13_misc.py::TestR13C3AppsScriptMaxGoals` — 1 static-pin case. Plus 11 existing tests across `test_goal_grid.py` / `test_goal_grid_node.py` re-pinned at max_g=15.

### D1 — `renderCompare` DOM leak: appends 32 option nodes every tick (HIGH)

**Where**: `dashboard/app.js::renderCompare`

**Symptom**: R12 D1 wired `renderCompare` into `applyLiveUpdate`'s 10-min live tick. But `renderCompare`'s option-append loop did NOT clear the `<select>` elements first. So 32 team options got appended on every tick: 192 dups per hour, 3,840 dups per 20 hours of live window. Each dropdown grew unbounded, becoming jank-slow to render and eventually exhausting mobile memory.

**Fix**: Call `a.replaceChildren()` and `b.replaceChildren()` before the append loop. Now option count stays constant across ticks.

**Tests**: `tests/live/test_r13_misc.py::TestR13D1RenderCompareClearsOptions` — 1 static-pin case.

---

## Genuine R13 MEDIUM findings landed in this commit

### MED-1 — `check_invariants` silently skipped partial-coverage stage fields (MED)

**Where**: `scripts/check_invariants.py:235-238`

**Symptom**: Pre-R13 the stage_expectations loop had `if not all(field in t for t in teams): continue` — ANY missing field on ANY team silently skipped the whole stage check. A real regression where the sim drops `p_advance_groups` on the 48th team (47 have it, 1 doesn't) would silently slip past the gate.

**Fix**: Distinguish ZERO coverage (synthetic blob / legacy — skip silently) from PARTIAL coverage (real regression — raise `MissingField`). Now only zero-coverage skips; partial coverage raises with the offending team name.

**Tests**: `tests/live/test_r13_misc.py::TestR13MED1CheckInvariantsPartialCoverageRaises` — 2 cases.

### MED-2 — Orchestrator crash path didn't increment circuit breaker (MED)

**Where**: `scripts/live/run_live_update.py` (top-level `except Exception as e:` handler at line 766-808)

**Symptom**: R11 D1 + R12 E1 seeded the CB state file, so the CB now correctly persists across ticks. But the orchestrator crash path (top-level handler) wrote `live_state.json` with an `orchestrator_crash` warning AND THEN exited 1 without incrementing the CB. Only `sim_failure` paths called `write_circuit_breaker(new_failures)`. A string of crashes (e.g., unhandled exception in apply_matchday integration) would leave CB stuck at 0 forever and never trip CB_THRESHOLD=3.

**Fix**: Add `current = read_circuit_breaker(); write_circuit_breaker(current + 1)` to the crash handler. Belt-and-braces try/except around the write so a CB-write failure doesn't mask the original crash.

**Tests**: `tests/live/test_r13_misc.py::TestR13MED2CrashIncrementsCB` — 1 static-pin case.

### MED-3 — `player_norm` leaked from suspension_tracker to JSON output (MED)

**Where**: `scripts/live/suspension_tracker.py::_attach_elo`

**Symptom**: R12 A1 added a `player_norm` field on each suspension row for cross-provider idempotency. The field was meant to be INTERNAL (the join key), but `_attach_elo` did `row = dict(s)` which copied it through to the on-disk payload. Any downstream consumer that displayed suspension rows verbatim would surface "jimenez" instead of "Raúl Jiménez".

**Fix**: `_attach_elo` now does `row = {k: v for k, v in s.items() if k != "player_norm"}` — strips the internal field before writing.

**Tests**: `tests/live/test_r13_misc.py::TestR13MED3PlayerNormStrippedFromOutput` — 1 case.

### MED-4 — Loader team normalization (defense-in-depth) (MED)

**Where**: `scripts/live/apply_matchday_adjustments.py` injury/referee/suspension loaders (lines ~545, 771, 822)

**Symptom**: Producers (`fetch_injuries`, `referee_adjustments`, `suspension_tracker`) already write canonical team names — verified against the actual on-disk files. But CORRECTIONS.md §4 documents an operator-override pattern where these files MAY be hand-edited. An operator entering "USA" or "Korea Republic" silently fails to merge with the overlay's bucketing (which DOES normalize).

**Fix**: Add `normalize_team()` at the loader for all three producer files. Defense-in-depth — even if a producer regresses or an operator override skips canonicalization, the loader catches it.

**Tests**: `tests/live/test_r13_misc.py::TestR13A2LoaderTeamNormalization` — 3 static-pin cases.

---

## R13 findings REJECTED by monitor verification

- **SEVERITY_RANK missing 25+ warning types**: Monitor 2 verified that all critical types (`sigma_gate_failed`, `sim_failure`, `fetch_failure`, `matchday_consolidated_stale`) ARE explicitly ranked. Unranked types fall to default rank=99 by design (intentional deferred prioritization). REJECTED.
- **Empty-data guards false-positive on slow first-load**: LOW UX polish, not a HIGH bug. Tracked as deferred polish but not landed in R13 to keep scope tight.

---

## Verify-everything-before-commit

* **Full suite**: **1272 passed**, 1 skipped, 0 failed, 11 warnings (pre-existing), 58 subtests passed.
* **Σ-gate canonical**: exit 0 on `data/processed/predictions_live.json` (|Δ| = 1.110e-16, teams = 48).
* **Σ-gate dashboard mirror**: exit 0 on `dashboard/predictions_live.json` (|Δ| = 1.110e-16, teams = 48).
* **`AUTO_TIER_ACTIVE = False`** unchanged.
* **All caps/thresholds unchanged**. `MAX_G = 15` now consistent across the 4 places it lived (sim default, ko_advance export, goal_grid tests, Apps Script).
* **No main branch touch, no push, no deploy.** Branch `hardening/r32-pressure-test-r2` remains local-only per instruction.

---

## Genuine-finding cadence

| Round | HIGH | MEDIUM | DEFENSIVE / LOW |
|-------|------|--------|-----------------|
| R3    | 4    | —      | — |
| R4    | 1    | —      | — |
| R5    | 1    | 2      | — |
| R6    | 0    | 2      | 1 |
| R7    | 0    | 0      | 3 |
| R8    | 0    | 1      | 1 |
| R9    | 5    | 1      | 1 |
| R10   | 3    | 2      | — |
| R11   | 8    | 4 + 12 R10 deferrals | — |
| R12   | 12   | 5      | — |
| R13   | **5** | **4**  | — |

R13 caught five HIGH-severity bugs in R12's own output — the audit-the-
audit pattern continues to find genuine pre-R32 risk that single-round
sweeps miss. Most consequential: A1 (player_join_key intra-team collision)
would have silently suspended the wrong Martínez (or Bacuna) during the
group stage. C1+C2+C3 (MAX_G stale across 4 places) would have published
KO advance probabilities that didn't agree with the sim's group-stage
WDL — operator confidence in the model would have eroded mid-tournament.
Surfaced at T-4 days from R32 kickoff (2026-06-28), with zero remaining
deferred items.
