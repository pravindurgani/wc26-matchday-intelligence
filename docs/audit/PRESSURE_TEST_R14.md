# WC26 Matchday Intelligence — Pressure-Test Round 14 (T-4d, audit of R13, NO DEFERRALS)

**Date**: 2026-06-24
**Branch**: `hardening/r32-pressure-test-r2` (local-only; push human-gated)
**Suite delta**: 1272 → **1283 passed**, 1 skipped, 0 failed (+11 R14 regression tests; +1 existing test extended for Korean-name convention)
**Σ-gate (canonical)**: exit 0 on `data/processed/predictions_live.json` (|Δ| = 1.110e-16, teams = 48, tol = 1e-6)
**Σ-gate (dashboard mirror)**: exit 0 on `dashboard/predictions_live.json` (|Δ| = 1.110e-16, teams = 48, tol = 1e-6)
**Standing constraints preserved**: no retrains, no threshold/cap changes,
`AUTO_TIER_ACTIVE=False`, `NB_ALPHA=5.0`, `DC_RHO=-0.13`,
`STALENESS_MAX_AGE_HOURS=6.0`, `CB_THRESHOLD=3`, `YELLOW_THRESHOLD=2`.
`max_g=15` now consistent across **6 places** (sim, ko_advance export,
goal_grid tests, Apps Script, daily-baseline evaluate, daily-baseline
ablation, model training diagnostic).

---

## Methodology

R14 was another audit-the-audit round: 5 parallel adversarial agents
explicitly tasked with finding HIGH-severity bugs R13 introduced or
missed, plus 2 independent monitor agents to verify the primary agents'
claims before any code change. Same orthogonal-sweep + monitor-
verification pattern that has run every round since R9.

**User instruction this round**:
> "REVIEW EVERYTHING THOROUGHLY END-TO-END... DONT DEFER ANY ISSUES/FLAWS
> ... DEPLOY AS MANY AGENTS AS NECESSARY ... DEPLOY AGENTS TO MONITOR
> OTHER AGENTS TOO ... VERIFY EVERYTHING BEFORE MAKING ANY COMMITS!"

The 5 sweeps explicitly audited R13's outputs:
1. **A — player_join_key residual gaps**: mononym initial-form drift,
   missing-file degradation, alias correctness, hyphenated surnames.
2. **B — regenerated predictions_live.json completeness**: all expected
   fields present, dashboard consumes correctly, byte-identical mirror.
3. **C — MED hardening edge cases**: partial-coverage at n_with=1,
   triggering_match_id leak, other loaders without normalize_team.
4. **D — MAX_G ripple completeness**: R13 fixed 3 places (export, tests,
   Apps Script) — what about scripts/04_evaluate.py, 06_ablation.py,
   02_goal_model.py, walk_forward, training pipelines?
5. **E — other dashboard render-fn DOM leaks**: R13 D1 fixed
   renderCompare — did the same pattern hide in renderContenders,
   renderMatches, renderInteresting, etc.?

Of ~16 candidate findings raised, the monitors **CONFIRMED** 4 HIGH-
severity production bugs (3 of which R13 missed; 1 hidden behind R13's
own normalization fix) plus 5 MEDIUM items. ZERO deferrals.

---

## Genuine R14 HIGH findings landed in this commit

### D2 — `renderContenders` DOM leak: same pattern R13 D1 fixed but missed (HIGH)

**Where**: `dashboard/app.js::renderContenders` (lines 826-829)

**Symptom**: R12 D1 wired `renderContenders` into `applyLiveUpdate`'s 60s tick. R13 D1 caught the same pattern in `renderCompare` and added `replaceChildren()` before the append loop. But R13 missed `renderContenders` — it has an IDENTICAL pattern: the function appends group-name options to `groupSel` (the `#team-group` dropdown) on every tick without clearing. ~8 groups × 1,200 ticks per 20h = ~9,600 duplicate `<option>` nodes accumulating in the dropdown. Mobile OOM risk and progressively-slower dropdown render.

**Fix**: Use the `while (groupSel.options.length > 1) groupSel.remove(1);` pattern (preserves the default `<option value="all">All</option>` at index 0, removes only the dynamically-appended group options). Same pattern as `renderMatches` at line 1241. Cannot use `replaceChildren()` directly here because the default option is HTML-baked and would be wiped.

**Tests**: `tests/live/test_r14_misc.py::TestR14D2RenderContendersClearsOptions` — 1 case (static-pin for the clearing loop).

### C1 — `lambdas_to_wdl` default `max_g=10` stale vs sim's 15 in TWO daily-baseline scripts (HIGH)

**Where**: `scripts/04_evaluate.py:34` + `scripts/06_ablation.py:33`

**Symptom**: R12 MED bumped `build_score_matrix` default `max_g=10 → 15`. R13 C1/C2/C3 propagated to export_ko_advance + goal_grid tests + Apps Script. But the DAILY BASELINE evaluation and ablation scripts kept their own `lambdas_to_wdl(..., max_g=10)` default. Both scripts are run by `daily-baseline.yml` to produce `evaluation.json`, `calibration.json`, `ablation.json` — all shipped to the dashboard. So the production sim ran at 16×16 NB+DC while the dashboard's calibration / ablation curves were computed at 11×11 Poisson. At λ=4.0 the Poisson tail above 10 carries ~3% mass — calibration metrics were biased by that truncation.

**Fix**: Bump `lambdas_to_wdl` default to `max_g=15` in both files. Call sites in `04_evaluate.py:121, 166`, `06_ablation.py:71`, `07_walk_forward.py:88` all rely on the default → automatically pick up 15.

**Tests**: `tests/live/test_r14_misc.py::TestR14C1LambdasToWdlMaxG` — 2 static-pin cases.

### C2 — `02_goal_model.equivalent_wdl_logloss` hardcoded `max_g=8` (HIGH — training-time alignment)

**Where**: `scripts/02_goal_model.py:192`

**Symptom**: The training-time WDL log-loss diagnostic hardcoded `max_g = 8` — EVEN MORE truncated than the daily-baseline (10) or the sim (15). At λ=4 Poisson tail above 8 carries ~5% mass. Model training reported a WDL log-loss that disagreed with both daily-baseline (max_g=10) and production sim (max_g=15). Cross-pipeline drift on a core training metric.

**Fix**: Bump to `max_g = 15`. Aligns model-training diagnostic with both downstream consumers.

**Tests**: `tests/live/test_r14_misc.py::TestR14C2GoalModelEquivalentWdlMaxG` — 1 static-pin case.

---

## Genuine R14 MEDIUM findings landed in this commit

### MED-1 — Son Heung-min `last_name_normalized` carried hyphenated GIVEN name (MED)

**Where**: `data/raw/key_players_2026.json:154` + `tests/live/test_injury_adjustments.py::test_stored_last_name_normalized_is_a_window_of_full`

**Symptom**: The Son Heung-min entry carried `last_name_normalized: "heung-min"` (the trailing-token of the normalized name). For Korean names the convention is SURNAME-FIRST (Son is the family name, Heung-min the given name). The `by_last` index then keyed Son under "heung-min" instead of "son" — defeating canonical resolution for the most common API forms ("Son" mononym, "H. Son" initial-form). Hidden behind R13's alias handling (which registers "Son", "H. Son", etc. in `by_full`), but if a NEW API form appeared without an alias match, it would fall back to full norm instead of canonicalizing.

**Fix**:
1. Data file: change Son's `last_name_normalized` from "heung-min" to "son".
2. Test invariant: `test_stored_last_name_normalized_is_a_window_of_full` extended to accept BOTH trailing AND leading windows of the full name (since Korean surnames are leading).
3. Verified post-fix: `by_last["son"] = [Son entry]`, and `player_join_key("S. Heung-min", team="South Korea")` falls back cleanly while "Son" / "H. Son" / "Son Heung-min" all canonicalize to "son heung-min".

### MED-2 — `triggering_match_id` leaked to suspensions_2026.json output (MED)

**Where**: `scripts/live/suspension_tracker.py::_attach_elo`

**Symptom**: R13 MED-3 added a `player_norm` strip but missed `triggering_match_id` — another internal field added in R6 for per-match dedup. The on-disk schema docstring at lines 22-50 does NOT list `triggering_match_id`. Downstream consumers use `evidence_match_ids` (canonical list).

**Fix**: Generalize the strip to `_INTERNAL_FIELDS = {"player_norm", "triggering_match_id"}` and filter both.

**Tests**: `tests/live/test_r14_misc.py::TestR14MEDTriggeringMatchIdStripped` — 1 case.

### MED-3 — `normalize_team` defense-in-depth at weather/lineup/stats loaders (MED)

**Where**: `scripts/live/apply_matchday_adjustments.py` `_load_weather_components`, `_load_lineup_components`, `_load_stats_components`

**Symptom**: R13 A2 added `normalize_team` defense-in-depth to injury/referee/suspension loaders. But weather, lineup, stats loaders STILL extracted team without normalization. fetch_weather / fetch_lineups / fetch_match_stats DO normalize before writing, so this is defense-in-depth for operator manual-edit overrides (CORRECTIONS.md §4). Same hardening as R13 A2.

**Fix**: Add `raw_team = ...; team = normalize_team(raw_team)` pattern to all three loaders. Symmetric with R13 A2.

**Tests**: `tests/live/test_r14_misc.py::TestR14MEDLoaderTeamNormalization` — 3 static-pin cases.

### MED-4 — `check_invariants` partial-coverage test gap for `n_with=1` (MED)

**Where**: `tests/live/test_r13_misc.py::TestR13MED1CheckInvariantsPartialCoverageRaises`

**Symptom**: R13 MED-1 hardened the partial-coverage check to raise on `n_with < len(teams)` — i.e., ANY partial coverage. The test covered the n_with=47 case (1 team missing the field). But the symmetric n_with=1 case (only 1 team has the field, 47 don't) was untested. Both should raise.

**Fix**: Add `tests/live/test_r14_misc.py::TestR14MEDPartialCoverageNWith1::test_n_with_1_raises` — synthetic blob with p_advance_groups on only team T0, error message must include "1/48" to confirm reporting accuracy.

### MED-5 — Missing `key_players_2026.json` file degradation test (MED)

**Where**: `tests/live/test_r14_misc.py::TestR14MEDMissingKeyPlayersFallback`

**Symptom**: R13's `_load_key_players_index` returns `{}` if the file is missing (verified at line 293). `player_join_key` falls back to the full normalized form. No crash, no false matches — graceful degradation. But the scenario was NEVER tested.

**Fix**: Add a regression test that rebinds `KEY_PLAYERS_PATH` to a non-existent path, resets the cache, verifies graceful fallback (Lautaro/Emiliano Martínez still distinct, cross-feed dedup degrades silently as accepted trade-off).

---

## R14 findings REJECTED by monitor verification

- **Mononym initial-form drift "P. Pedri"**: Architectural trade-off, documented in `player_join_key` docstring. classify_tier handles this via `_resolve_from_last_match` forename matching. Real-world API-Football forms don't include "P. Pedri" for mononyms. REJECTED.
- **match_predictions_ko field absent**: Expected during group stage — the field is only populated after KO slots resolve (post-2026-06-26). REJECTED.

---

## Verify-everything-before-commit

* **Full suite**: **1283 passed**, 1 skipped, 0 failed, 11 warnings (pre-existing), 58 subtests passed.
* **Σ-gate canonical**: exit 0 on `data/processed/predictions_live.json` (|Δ| = 1.110e-16, teams = 48).
* **Σ-gate dashboard mirror**: exit 0 on `dashboard/predictions_live.json` (|Δ| = 1.110e-16, teams = 48).
* **`AUTO_TIER_ACTIVE = False`** unchanged.
* **All caps/thresholds unchanged**. `max_g=15` now consistent across 6 places: sim, ko_advance export, goal_grid tests, Apps Script, 04_evaluate, 06_ablation, 02_goal_model. No threshold/cap value changes.
* **No main branch touch, no push, no deploy.**

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
| R13   | 5    | 4      | — |
| R14   | **3** | **5**  | — |

R14 caught a critical class of bug — daily-baseline metrics (calibration,
ablation, model-training WDL log-loss) running at the WRONG max_g for
multiple rounds (since R12 MED bumped the sim default). Three pipelines
silently producing dashboard metrics under a different distribution
assumption than the production sim. Surfaced at T-4 days from R32 kickoff
(2026-06-28), with zero remaining deferred items. The audit-the-audit
pattern continues to find genuine pre-R32 risk that single-round sweeps
miss — R13 caught 5 HIGHs in R12's output; R14 caught 3 HIGHs in R13's
output. Likely R15 will find at most 0-1 HIGHs as the surface tightens.
