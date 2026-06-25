# Production Parity Proof — `hardening/r32-pressure-test-r2` vs `origin/main`

**Date**: 2026-06-17 (Round 2 update)
**Local branch**: `hardening/r32-pressure-test-r2` (to be created locally; NOT pushed per current instruction "DONT TOUCH MAIN, DONT PUSH/DEPLOY"). Round-1 sibling `hardening/r32-pressure-test` documented in `COMMIT_PLAN.md`.
**`origin/main` keeps moving**: data-only auto-commits arrive every ~10 minutes from the live workflow. The headline diff below describes the **logical parity state** between the round-2 hardening layer and current `origin/main` — file paths and counts are stable across the data-only churn (all auto-commits touch only `dashboard/*.json`, `data/live/*.json`, `data/processed/*.json`, `README.md`).

---

## Headline number (Round 2)

| Surface | `origin/main` (data-only auto-commit lineage) | Local working tree (post-R2) | Delta |
|---|---|---|---|
| `pytest tests/live/` | **269 passed**, 26 subtests, 0 failed | **1059 passed**, 1 skipped, 0 failed, 0 xfailed | **+790 tests** (round-1 +721 + round-2 +69) |
| `scripts/live/*.py` count | 14 | **23** | +9 modules |
| `tests/live/*.py` count | 9 | **44** | +35 test files (R2 added 2: `test_ko_matrix_equality.py` + `test_fast_path_freshness.py`) |
| `wc26-engine-gs/` | absent | present (Apps Script source + node harness) | new dir |
| `data/live/_provider_schemas/` | absent | 9 shape baselines | new dir |
| `data/live/{player_stats,referee,suspensions}_2026.json` | absent | present (Phase 2/3/4 producer outputs) | 3 new live snapshots |
| `Σ-gate` (`scripts/check_invariants.py`) | absent | present, exit 0 on real data, strict 1e-6, exit-code contract 2/3/4/5/6/7 + MalformedJson + bool-poisoning rejection | new gate |
| `AUTO_TIER_ACTIVE` | n/a (the file doesn't exist with this flag on origin) | `False` at `injury_adjustments.py:64` | shadow mode preserved |
| **R2-NEW (P1a)**: `test_ko_matrix_equality.py` — sim ↔ export matrix proven equal at ≤1e-12 | absent | 55 tests | non-circular matrix coverage |
| **R2-NEW (P1c)**: `get_matchday_freshness_warnings()` helper | absent | `apply_matchday_adjustments.py:224-331` | fast-path freshness propagation |
| **R2-NEW (P1c)**: `_matchday_freshness_warnings_safe()` safe-wrap | absent | `run_live_update.py:66-98` | crash-safe orchestrator hook |
| **R2-NEW (P1c)**: freshness probed on all 6 `write_live_state` paths | n/a | CB / fetch-fail / input-corruption / unchanged / sim-fail / success — all fold in `mf_warnings` | early-exit propagation closed |
| **R2-NEW (P1c)**: `test_fast_path_freshness.py` | absent | 14 tests | freshness contract pinned |

The clean clone of `origin/main` was performed at `/tmp/wc26-cleanclone` (round 1) and verified to:
- be at the data-only lineage of `origin/main` (auto-commits touch only dashboard / data / README — no source-code conflict with this branch's payload)
- run pytest → 269 passed, no failures, 26 subtests passed (clean clone does NOT have any of the matchday-intelligence layer)

---

## What `origin/main` is missing (modules + tests + data)

### New scripts/live/ modules (9 files)

| Path | Provenance | Purpose |
|---|---|---|
| `scripts/live/_degrade.py` | Round 5 | Orchestrator graceful-degradation helper; `_make_warning(subsystem, scope, …)` record builder; per-record + per-subsystem try/except wrappers caching `(ValueError, TypeError, KeyError, OverflowError)` |
| `scripts/live/_knockout.py` | Round 6 | Knockout fixture loader from `data/raw/knockout_bracket_2026.json` with stage tags + `is_placeholder_slot()` regex for slot codes like `"1A"`, `"3A/B/C/D/F"`, `"W74"`, `"L101"`, `"TBD"` |
| `scripts/live/_schema_watchdog.py` | Round 5 | Provider response shape-hash detector. `compute_shape_hash` (value-independent, dict-key-order-independent), `assert_shape(payload, baseline_path)` soft-mode (logs warning on drift; never raises), `SchemaDriftError` for opt-in strict mode, exit code 8 reserved on CLI |
| `scripts/live/auto_tier.py` | Phase 5 | Auto-tier engine in **shadow mode**. `MIN_TEAM_TOP_MINUTES=200`, `tier_1_star_minutes_share_gk=0.90` (justified at p92 of GK team-minutes-share). `AUTO_TIER_ACTIVE = False` per `CORRECTIONS.md §7` |
| `scripts/live/auto_tier_diff.py` | Phase 5 | Auto-tier vs manual-tier diff CLI (read-only; useful for shadow-mode validation) |
| `scripts/live/fetch_player_stats.py` | Phase 3 | API-Football `/teams` + `/players` producer. Writes `data/live/player_stats_2026.json` |
| `scripts/live/referee_adjustments.py` | Phase 2 | Referee bias baseline + per-match contribution. Writes `data/live/referee_2026.json` |
| `scripts/live/suspension_tracker.py` | Phase 4 + Round 6 | Yellow/red accumulator with knockout schedule + FIFA QF-flush rule + placeholder-slot skip. Writes `data/live/suspensions_2026.json` |
| `scripts/live/verify_goal_grid_agreement.py` | Phase 5 | Goal Grid vs sim feed agreement CLI (read-only verification tool) |
| `scripts/live/export_ko_advance.py` | S7 (this session) | Per-KO-match advance probability post-processor. Computes `p_advance_match = p_home_win + 0.5 * p_draw` (FIFA penalty-shootout 50/50 assumption) from the same Poisson+DC matrix the sim uses. Idempotent. Embeds Σ-gate at end. Wired into `run_live_update.py:514` (Step 4b) |

### New scripts/ modules (2 files)

| Path | Purpose |
|---|---|
| `scripts/check_invariants.py` | Σ-gate (strict 1e-6); exit-code contract 0/2/3/4/5/6/7 + MalformedJson; explicit bool-rejection (Wave 4 finding) |
| `scripts/calibration.py` | Log loss / Brier / reliability CLI; `--json` flag; exit 1 if log loss > 1.05 (model genuinely broken) |

### New tests/live/ files (33 files + 1 helper)

| Path | Tests | Purpose |
|---|---:|---|
| `tests/live/_node_resolver.py` | n/a (helper) | `NODE_BIN` resolver: `WC26_NODE_BIN` env → `shutil.which("node")` → None. Import-time RuntimeError when `CI=true` and unresolved |
| `tests/live/test_node_resolver.py` | 5 | Env override, `which` fallback, CI hard-fail subprocess |
| `tests/live/test_auto_tier.py` | varies | Auto-tier baseline tests |
| `tests/live/test_auto_tier_floor.py` | 9 | `MIN_TEAM_TOP_MINUTES=200` floor — returns `(None, "auto_insufficient_sample", …)` below floor |
| `tests/live/test_auto_tier_gk.py` | 7 | GK signal switched to minutes-share-only; threshold justified at p92 |
| `tests/live/test_calibration.py` | 11 | Hand-computed metric correctness + CLI integration |
| `tests/live/test_check_invariants.py` | 7 | Σ-gate happy + 6 adversarial blobs |
| `tests/live/test_check_invariants_adversarial.py` | 19 | NaN/negative-compensated/duplicate-codes/malformed-JSON (Round 6 converted to positive PASS) |
| `tests/live/test_clv_adversarial.py` | 30 | Python mirror of refreshCLV.gs — closing-odds=0, rolling-window <20, duplicate `(#m, pick)`, O/U scope leak |
| `tests/live/test_cross_subsystem_invariants.py` | 13 | No double-counting (injury+suspension dedup), bounded total adjustment, one-sided referee bonus |
| `tests/live/test_extend_to_knockouts.py` | varies | 32-fixture FIFA WC 2026 stage layout (16+8+4+2+1+1) |
| `tests/live/test_fetch_events.py` | varies | API-Football events parsing |
| `tests/live/test_fetch_player_stats.py` | varies | Phase 3 producer happy + adversarial |
| `tests/live/test_fetch_results_schema.py` | 6 | `/fixtures` + `/fixtures/events` schema-watchdog wiring |
| `tests/live/test_fetch_schema_wiring.py` | 13 | All 5 newly-wired endpoints (S4) |
| `tests/live/test_goal_grid.py` | 24 | DC cells pinned to 1e-9, swap detector, market sums |
| `tests/live/test_goal_grid_adversarial.py` | 38 | λ=0/inf/NaN, ρ=±1, max_g extremes, WDL invariant |
| `tests/live/test_goal_grid_feed_agreement.py` | 144 + 1 | Feed-WDL ≡ analytical NB+DC @ 1e-9 (72 fixtures); JS-replica ≡ analytical Poisson+DC @ 1e-10 (72); `test_ko_advance_agreement` (S7) |
| `tests/live/test_goal_grid_node.py` | 11 + 1 skip | Real `.gs` source executed under node via subprocess harness |
| `tests/live/test_injury_adversarial.py` | 25 | Round 4 hardening (9 silent failures converted to positive PASS) |
| `tests/live/test_key_players_config.py` | 8 | S5 validator |
| `tests/live/test_key_players_coverage.py` | 3 classes | S6 48/48 coverage |
| `tests/live/test_knockout_readiness.py` | 29 | KO schedule loader, QF-flush rule, placeholder-slot skip, KO lineup polling (Round 6) |
| `tests/live/test_ko_advance_export.py` | 6 | S7: single-resolved-KO, idempotency, Σ-gate-passes, placeholder-only empty, no-λ skip silently, real-repo round-trip |
| `tests/live/test_launchd_path.py` | 3 | S8 |
| `tests/live/test_lineup_adversarial.py` | 23 | Round 4 hardening (6 silent failures converted) |
| `tests/live/test_pipeline_e2e.py` | 11 | Full pipeline on synthetic France→Senegal + Brazil→Croatia fixtures |
| `tests/live/test_referee_adjustments.py` | varies | Phase 2 baseline |
| `tests/live/test_referee_adversarial.py` | 16 | Round 4 hardening (3 silent failures converted) |
| `tests/live/test_schema_watchdog.py` | 34 | Hash stability, key add/remove/type-change, nested rename, CLI |
| `tests/live/test_stats_cap_name.py` | 2 | S9 rename pinning |
| `tests/live/test_stats_proxy_adversarial.py` | 28 | Round 4 hardening (3 silent failures converted) |
| `tests/live/test_suspension_adversarial.py` | 14 | Round 4 hardening (4 silent failures converted) |
| `tests/live/test_suspension_tracker.py` | varies | Phase 4 baseline + Round 6 KO + QF-flush |
| `tests/live/test_wave4_adversarial.py` | 15 | Wave 4 findings (expires_at, bool poisoning, export wiring contract) |
| `tests/live/test_workflow_yaml.py` | 6 | `matchday-intel-slow.yml` step ordering + git add allow-list (S1) |

### New data assets

| Path | Provenance | Purpose |
|---|---|---|
| `wc26-engine-gs/WC26_Engine_AppsScript_v2.3.1.gs` | Phase 2/3 | The real Apps Script source — Dixon-Coles ground truth |
| `wc26-engine-gs/test_harness.mjs` | Round 5 | Node harness loading the `.gs` via `fs.readFileSync` + `vm.runInThisContext` |
| `wc26-engine-gs/WC26_v2.3.1_PATCH_NOTES.md` | Phase 2/3 | Apps Script changelog |
| `wc26-engine-gs/WC26_Value_Betting_Engine_AUTOMATED_v2.3.1.xlsx` | Phase 2/3 | The Sheet (Excel export) |
| `data/live/_provider_schemas/*.shape.json` | Round 5 (4 originals) + S4 (5 added this session) | 9 shape baselines for API-Football endpoints |
| `data/live/player_stats_2026.json` | Phase 3 | First capture of `/players` aggregated team data |
| `data/live/referee_2026.json` | Phase 2 | First capture of referee bias baseline |
| `data/live/suspensions_2026.json` | Phase 4 | First capture of yellow/red accumulator output |
| `tests/live/provider_samples/apifootball_events_sample.json` | Round 5 | One real `/fixtures/events` response for the schema-watchdog baseline |
| `CORRECTIONS.md` | Phases 1-5 | Running incident log; §7 documents the `AUTO_TIER_ACTIVE = False` shadow rollout; §8 documents the Phase-4 marked-done-but-unbuilt incident |
| `reports/calibration_baseline_pre_r32.md` | S9 (this session) | Calibration probe baseline pre-R32; log loss 1.0352 at N=5 |

### Modifications to existing tracked files

| Path | Summary of changes |
|---|---|
| `scripts/03_simulate.py` | One ~18-line additive export hook at `:1267-1283` to emit `knock_lambdas_table`; no MC/seed/threshold change |
| `scripts/09_validate.py` | Wired `check_invariants` strict gate after the existing summary writers |
| `scripts/live/apply_matchday_adjustments.py` | Round 5 graceful degradation (`_degrade.py` integration), Round 6 injury+suspension dedup, S1 freshness guard (`STALENESS_MAX_AGE_HOURS = 6.0`), S9 `STATS_CAP_TOURNAMENT_TOTAL` rename, Wave 4 `expires_at` type-check |
| `scripts/live/fetch_injuries.py` | Round 4 type guards + dedup loop + case-folded team match + `team_case_mismatch` warning, S4 schema-watchdog wiring at `:142` |
| `scripts/live/fetch_lineups.py` | Round 6 knockout schedule merge + venue state-suffix strip + placeholder-slot skip, S4 watchdog wiring at `:224` |
| `scripts/live/fetch_match_stats.py` | S4 watchdog wiring at `:156` |
| `scripts/live/fetch_results.py` | Round 5 watchdog wiring (`:386` events, `:517` fixtures) — already present in this layer |
| `scripts/live/injury_adjustments.py` | Round 4 type guards + `math.isfinite` on elo + 9 silent-failures-to-loud, Round 5 `AUTO_TIER_ACTIVE = False` constant at `:64`, S5 `net_injury_elo` clamp `[elo, 0]` at `:498` |
| `scripts/live/lineup_adjustments.py` | Round 4 multi-GK detector, malformed-XI warn, duplicate-id dedup, `prior_gk is not None` truthiness fix, `_coerce_id` for str/int outfield IDs |
| `scripts/live/stats_proxy_adjustments.py` | Round 4 `math.isfinite(own_xg) and math.isfinite(opp_xg)` raise, `_possession_signal` NaN early-return |
| `scripts/live/run_live_update.py` | Wave 4 Step 4b: invoke `export_ko_advance` between sim and dashboard publish; non-fatal failure |
| `scripts/pre_flight.py` | S5 `validate_key_players_replacements()` at `:990` + phase 12 wiring + `validate-key-players` CLI subcommand |
| `scripts/launchd/run_if_tournament.sh` | S8 `$0`-relative `REPO_ROOT` + `.git` guard |
| `data/raw/key_players_2026.json` | S6 Cape Verde + Curacao + Haiti entries (9 new players, 48/48 coverage) |
| `tests/live/test_apply_matchday_adjustments.py` | Extended for graceful degradation + freshness guard (S1) + expires_at adversarial |
| `tests/live/test_injury_adjustments.py` | Extended for type guards + `net_injury_elo` clamp (S5) |
| `tests/live/test_lineup_adjustments.py` | Extended for multi-GK + dedup + id coercion |
| `tests/live/test_stats_proxy.py` | Extended for finite check + possession NaN |
| `.github/workflows/matchday-intel-slow.yml` | S1: 4 new producer steps (`fetch_results --with-events`, `fetch_player_stats`, `referee_adjustments`, `suspension_tracker`) at `:106-162`; `git add` allow-list extended at `:263-265` |

---

## Will the scheduled workflows now run the hardened code?

After the branch is pushed and merged to `main`:

### `live-matchday.yml` (every 10 min during tournament window)

Unchanged in this branch (it already ran `fetch_results` + `apply_matchday_adjustments`). After merge, `apply_matchday_adjustments` will be the hardened version with:
- Round 5 graceful degradation (per-record skip-and-warn)
- Round 6 injury+suspension dedup
- S1 freshness guard
- S9 STATS_CAP rename
- Wave 4 expires_at validation
- All Round 4 math-layer raises

### `matchday-intel-slow.yml` (every 3h)

**Changed.** New step ordering at `:106-162`:
1. `fetch_results.py --with-events` (refreshes results + per-match events; events feed suspension_tracker)
2. **`fetch_player_stats.py`** (NEW)
3. **`referee_adjustments.py`** (NEW)
4. **`suspension_tracker.py`** (NEW)
5. `fetch_injuries.py` (existing)
6. `fetch_weather.py` (existing)
7. `fetch_lineups.py` (existing — now also merges knockout bracket per Round 6)
8. `fetch_match_stats.py` (existing)
9. `apply_matchday_adjustments.py` (existing — now hardened)

**`git add` allow-list extended at `:263-265`**:
```yaml
                  data/live/player_stats_2026.json \
                  data/live/referee_2026.json \
                  data/live/suspensions_2026.json \
```

So the 3 new producer outputs will land on `main` every 3h, and the freshness guard in `apply_matchday_adjustments.py` will surface a `subsystem_stale` warning on the next tick if any producer fails to refresh within 6h (= 2 missed 3h ticks).

---

## Vercel parity (deploy artifact)

`scripts/live/run_live_update.py:514` now invokes `export_ko_advance` as Step 4b. This produces:
- `data/processed/predictions_live.json::match_predictions_ko[]` — populated per KO match once teams resolve
- `data/processed/predictions_live.json::knock_lambdas_table[]` — populated once the sim emits the new hook output

The Vercel deploy consumes `data/processed/predictions_live.json`. After branch merge and the first sim tick, the new fields will appear in the deployed dashboard. Until KO teams resolve (post-group-stage), both fields will be empty arrays — which is correct.

`wc26-matchday-intelligence.vercel.app/live_state.json` is the single source of truth for verifying production state and will reflect this branch after merge.

---

## Risk register at the branch boundary

| Risk | Severity | Mitigation |
|---|---|---|
| First tick after merge produces missing-producer-output warnings | LOW | Freshness guard emits `subsystem_stale` and degrades gracefully (no crash). User reads `degradation_warnings` field on the first tick and ratifies. |
| Schema-watchdog drift warning on first live fetch (5 baselines are synthetic) | EXPECTED | All 5 new baselines have `_comment: "synthetic baseline; first live tick re-captures"`. The first real fetch logs a `[schema_watchdog] SHAPE DRIFT` warning (soft mode); user manually re-baselines via `python3 scripts/live/_schema_watchdog.py snapshot ...` to lock in the real shape. |
| Node not present on CI runner | DEFENDED | `tests/live/_node_resolver.py` import-time RuntimeError when `CI=true` and `NODE_BIN is None`. Either the CI image installs node, or `WC26_NODE_BIN` is set in workflow env. |
| `export_ko_advance` step in `run_live_update.py` fails | NON-FATAL | Documented non-fatal in Wave 4 finding 4.3; sim's `predictions_live.json` is retained without the new `match_predictions_ko` field. Warning logged to `live_state`. |
| Σ-gate exit codes change downstream consumer behavior | LOW | Contract extended (added MalformedJson, exit code 7) not weakened. `scripts/09_validate.py` catches the base `InvariantError` — new subclasses propagate transparently. |

---

## What this PR is NOT

- Not a retrain. `home_goals_model.joblib` / `away_goals_model.joblib` / `feature_cols_v2.json` untouched.
- Not a Vercel deploy. The deploy artifact will reflect the change after the first tick post-merge — that's a separate human gate.
- Not a threshold/cap change. Cap-audit clean (Wave 1 monitor verified). The 6h freshness threshold is new POLICY, not a Elo cap.
- Not a Sheet rewrite. The `.gs` engine source is in this branch as ground truth for the node harness; the Sheet itself is unchanged.
