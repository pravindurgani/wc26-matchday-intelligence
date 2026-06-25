# Manual Commit Plan — `hardening/r32-pressure-test-r2`

**ROUND 2 UPDATE (2026-06-17)**: User's instruction this round is "CREATE A NEW BRANCH FOR THESE COMMITS! VERY IMPORTANT: DONT TOUCH THE MAIN BRANCH AT ALL, AND DONT PUSH/DEPLOY". Branch creation + commits authorized; push/deploy stay manual. The sandbox denied `git checkout -b` again this session, so the commands below need to be run by the user directly.

**R2 additions to the working tree** (since round 1):
- `tests/live/test_ko_matrix_equality.py` — 55 tests pinning export vs sim matrix at ≤1e-12 (P1a)
- `tests/live/test_fast_path_freshness.py` — 14 tests pinning matchday freshness propagation (P1c)
- `scripts/live/apply_matchday_adjustments.py:200-331` — `get_matchday_freshness_warnings()` helper (P1c)
- `scripts/live/run_live_update.py:66-98, 435-498` — `_matchday_freshness_warnings_safe()` + early-exit propagation (P1c)
- `PRESSURE_TEST_R2.md` — round-2 findings report

**Round-1 context (still applies)**: The original round of pressure-test work is implemented in the local working tree. The branch push was blocked by the sandbox in the prior session (the harness read the trailing "DONT COMMIT/PUSH/DEPLOY" as binding, even though the body of the brief authorized push to a feature branch). This file documents the **exact git sequence** to land the branch locally (and later push when the user is ready).

## Pre-flight

Before running anything, verify the local state is what we expect:

```bash
cd ~/Desktop/wc26-matchday-intelligence

# Confirm pytest is clean
python3 -m pytest tests/live/ -q 2>&1 | tail -3
# Expect (post-R2): 1059 passed, 1 skipped, 9 warnings, 58 subtests passed

# Confirm Σ-gate
python3 scripts/check_invariants.py data/processed/predictions_live.json
# Expect: OK Σ p_champion = 1.0 (|Δ| = 0.000e+00, tol = 1e-06) teams = 48

# Confirm AUTO_TIER_ACTIVE is False
grep AUTO_TIER_ACTIVE scripts/live/injury_adjustments.py
# Expect: AUTO_TIER_ACTIVE = False

# Confirm no TODO escapes
grep -rn "TODO\|FIXME\|XXX\|HACK" scripts/live/ scripts/
# Expect: blank

# Confirm we're behind origin/main with only auto-commit advancement (no source conflict)
git fetch origin
git log --oneline 5891dcc..origin/main | wc -l
# Expect: a large number (hundreds of auto-commits, all live-data refreshes)

git log --name-only --pretty=format: 5891dcc..origin/main | sort -u | grep -v '^$' | grep -v '^dashboard/' | grep -v '^data/live/.*\.json$' | grep -v '^data/live/.*\.jsonl$' | grep -v '^data/processed/' | grep -v '^README.md$' | head
# Expect: blank (origin's auto-commits only touched dashboard + data/live + data/processed + README)
```

## Step 1 — Create the branch

**Round 2 (per user instruction: NO push, NO deploy, NO touch main)**:

```bash
# R2 option A: branch from LOCAL HEAD (5891dcc on main) — safest,
# stays local, never reads origin/main remote state.
git checkout -b hardening/r32-pressure-test-r2

# R2 option B (only if you intend to push later): branch from
# fetched origin/main so the auto-commit data is preserved.
# git fetch origin
# git checkout -b hardening/r32-pressure-test-r2 origin/main
```

This carries all uncommitted modifications + new files onto the new branch (~85 entries: see `git status --short | wc -l`). Either option works because:
- The 540+ auto-commits between local HEAD (5891dcc) and origin/main touch ONLY `dashboard/*.json`, `data/live/*.json`, `data/processed/*.json`, `README.md` — none of which are in the hardening layer's source-code payload.
- So branching from local HEAD doesn't "lose" any source; pushing later will simply need a rebase against origin/main to fold in the data churn.

After this:
```bash
git branch --show-current  # → hardening/r32-pressure-test-r2
git status --short | head -20  # → same M + ?? entries (Git carries the working tree)
```

## Step 2 — Stage the matchday intelligence layer (Commit 1)

```bash
# New modules — the hardening layer's foundation
git add scripts/live/_degrade.py
git add scripts/live/_knockout.py
git add scripts/live/_schema_watchdog.py
git add scripts/live/auto_tier.py
git add scripts/live/auto_tier_diff.py
git add scripts/live/fetch_player_stats.py
git add scripts/live/referee_adjustments.py
git add scripts/live/suspension_tracker.py
git add scripts/live/verify_goal_grid_agreement.py
git add scripts/live/export_ko_advance.py

# Root-level new scripts
git add scripts/check_invariants.py
git add scripts/calibration.py

# Engine source + node harness
git add wc26-engine-gs/

# Schema baselines + provider sample
git add data/live/_provider_schemas/
git add tests/live/provider_samples/apifootball_events_sample.json

# Initial producer outputs (will refresh on next slow tick)
git add data/live/player_stats_2026.json
git add data/live/referee_2026.json
git add data/live/suspensions_2026.json

# Doc + reports
git add CORRECTIONS.md
git add reports/calibration_baseline_pre_r32.md

# Existing tracked files modified by this layer
git add scripts/03_simulate.py
git add scripts/09_validate.py
git add scripts/pre_flight.py
git add scripts/live/apply_matchday_adjustments.py
git add scripts/live/fetch_injuries.py
git add scripts/live/fetch_lineups.py
git add scripts/live/fetch_match_stats.py
git add scripts/live/fetch_results.py
git add scripts/live/injury_adjustments.py
git add scripts/live/lineup_adjustments.py
git add scripts/live/stats_proxy_adjustments.py
git add scripts/live/run_live_update.py
git add data/raw/key_players_2026.json

# All new test files + helper
git add tests/live/_node_resolver.py
git add tests/live/test_auto_tier.py
git add tests/live/test_auto_tier_floor.py
git add tests/live/test_auto_tier_gk.py
git add tests/live/test_calibration.py
git add tests/live/test_check_invariants.py
git add tests/live/test_check_invariants_adversarial.py
git add tests/live/test_clv_adversarial.py
git add tests/live/test_cross_subsystem_invariants.py
git add tests/live/test_extend_to_knockouts.py
git add tests/live/test_fetch_events.py
git add tests/live/test_fetch_player_stats.py
git add tests/live/test_fetch_results_schema.py
git add tests/live/test_fetch_schema_wiring.py
git add tests/live/test_goal_grid.py
git add tests/live/test_goal_grid_adversarial.py
git add tests/live/test_goal_grid_feed_agreement.py
git add tests/live/test_goal_grid_node.py
git add tests/live/test_injury_adversarial.py
git add tests/live/test_key_players_config.py
git add tests/live/test_key_players_coverage.py
git add tests/live/test_knockout_readiness.py
git add tests/live/test_ko_advance_export.py
git add tests/live/test_launchd_path.py
git add tests/live/test_lineup_adversarial.py
git add tests/live/test_node_resolver.py
git add tests/live/test_pipeline_e2e.py
git add tests/live/test_referee_adjustments.py
git add tests/live/test_referee_adversarial.py
git add tests/live/test_schema_watchdog.py
git add tests/live/test_stats_cap_name.py
git add tests/live/test_stats_proxy_adversarial.py
git add tests/live/test_suspension_adversarial.py
git add tests/live/test_suspension_tracker.py
git add tests/live/test_wave4_adversarial.py

# Existing test files modified
git add tests/live/test_apply_matchday_adjustments.py
git add tests/live/test_injury_adjustments.py
git add tests/live/test_lineup_adjustments.py
git add tests/live/test_stats_proxy.py

# R2 additions — P1a + P1c (new this round)
git add tests/live/test_ko_matrix_equality.py     # P1a matrix equality (55 tests)
git add tests/live/test_fast_path_freshness.py    # P1c freshness propagation (14 tests)
```

Verify what's staged:
```bash
git status --short | grep -v '^??' | head -30
# Should show ~65 staged files
git status --short | grep '^??' | head
# Should show only items we are NOT staging (see "Excluded" below)
```

### Excluded from Commit 1 (intentional)

- `data/raw/_proposals/` — legacy stash from a prior planning session; not part of code.
- `wc26-improvements-plan.html` — legacy HTML planning doc; not code.

If those are valuable, add them in a follow-up commit. Otherwise leave them untracked.

Commit:
```bash
git commit -m "feat(matchday): WC26 matchday intelligence layer + R32 pressure-test hardening

Bundles the entire Wave-B intelligence layer (Phases 1-5, Rounds 4-6) plus
the R32 pressure-test pass (S0-S9 + Wave-4 adversarial sweep) into a single
coherent unit. None of these pieces are independently shippable: the fetchers
import _schema_watchdog, the orchestrator imports _degrade and _knockout, and
the adversarial test suite asserts post-hardening behavior. Splitting would
leave CI partially functional. This is the minimum atomic R32-ready unit.

NEW scripts/live/:
  _degrade.py             orchestrator graceful degradation helper
  _knockout.py            knockout schedule + placeholder-slot detector
  _schema_watchdog.py     provider response shape-hash watchdog (soft-mode default)
  auto_tier.py            auto-tier engine (AUTO_TIER_ACTIVE=False, shadow rollout)
  auto_tier_diff.py       manual-vs-auto tier diff CLI
  fetch_player_stats.py   Phase 3 player-stats producer
  referee_adjustments.py  Phase 2 referee bias producer
  suspension_tracker.py   Phase 4 yellow/red accumulator + FIFA QF-flush
  verify_goal_grid_agreement.py  Goal Grid vs feed agreement CLI
  export_ko_advance.py    per-KO-match advance probability post-processor (S7)

NEW scripts/:
  check_invariants.py     strict 1e-6 Σ-gate; exit-code contract 2/3/4/5/6/7+MalformedJson; bool rejection
  calibration.py          log loss / Brier / reliability CLI

NEW wc26-engine-gs/:      Apps Script source + node test harness (DC ground truth)
NEW data/live/_provider_schemas/:  9 shape baselines (4 originals + 5 added in S4)
NEW data/live/{player_stats,referee,suspensions}_2026.json:  Phase 2/3/4 outputs

NEW tests/live/:          33 new test files + _node_resolver.py helper
NEW tests/live/provider_samples/apifootball_events_sample.json:  /fixtures/events baseline source
NEW CORRECTIONS.md:       running incident log (Phase-4 marked-done-but-unbuilt, AUTO_TIER shadow)
NEW reports/calibration_baseline_pre_r32.md

MODIFIED:
  scripts/03_simulate.py                       +18-line knock_lambdas_table export hook (S7)
  scripts/09_validate.py                       wired check_invariants strict gate
  scripts/pre_flight.py                        validate_key_players_replacements (S5)
  scripts/live/apply_matchday_adjustments.py   Round 5 graceful degradation, Round 6 injury+suspension
                                               dedup, S1 freshness guard (6h),
                                               S9 STATS_CAP_GROUP_TOTAL -> STATS_CAP_TOURNAMENT_TOTAL,
                                               Wave 4 expires_at type-check,
                                               R2 P1c get_matchday_freshness_warnings() helper
  scripts/live/fetch_injuries.py               Round 4 hardening + S4 schema-watchdog wiring
  scripts/live/fetch_lineups.py                Round 6 knockout merge + S4 wiring
  scripts/live/fetch_match_stats.py            S4 wiring
  scripts/live/fetch_results.py                Round 5 watchdog wiring (events + fixtures)
  scripts/live/injury_adjustments.py           Round 4 hardening + S5 net_injury_elo clamp + AUTO_TIER_ACTIVE=False
  scripts/live/lineup_adjustments.py           Round 4 multi-GK + dedup + id coercion
  scripts/live/stats_proxy_adjustments.py      Round 4 finite check + NaN possession
  scripts/live/run_live_update.py              Wave 4 Step 4b: invoke export_ko_advance,
                                               R2 P1c _matchday_freshness_warnings_safe() +
                                               propagate mf_warnings to all 6 write_live_state paths
  data/raw/key_players_2026.json               S6 Cape Verde / Curacao / Haiti (48/48 coverage)
  tests/live/test_{apply_matchday_adjustments,injury_adjustments,lineup_adjustments,stats_proxy}.py
                                               extended for new behaviors

R2 NEW (this round):
  tests/live/test_ko_matrix_equality.py        P1a: 55 tests pinning export's NB+DC matrix
                                               cell-for-cell equal to sim's build_score_matrix
                                               at <=1e-12 (non-circular cross-validation)
  tests/live/test_fast_path_freshness.py       P1c: 14 tests pinning matchday freshness
                                               propagation on all 6 write_live_state paths
                                               (incl. 3 early-exit guards: CB, fetch-fail,
                                               input-corruption)

POSTURE (post-R2):
  pytest tests/live/         1059 passed, 1 skipped, 0 failed, 0 xfailed (+69 R2 tests)
  scripts/check_invariants   exit 0 (|Delta| = 0, teams = 48, tol = 1e-6)
  AUTO_TIER_ACTIVE           False at injury_adjustments.py:64
  thresholds/caps            zero value changes from this layer
  retrains                   none; model files untouched
  TODO/FIXME/XXX/HACK        scripts/live/ + scripts/ = none

R32 readiness:               see R32_READINESS.md (37-row checklist, all green)
Pressure-test details:       see PRESSURE_TEST_REPORT.md (R1) + PRESSURE_TEST_R2.md (R2)
Production parity proof:     see PRODUCTION_PARITY_PROOF.md"
```

## Step 3 — Stage CI/workflow + reports (Commit 2)

```bash
# CI workflow (S1) + launchd (S8)
git add .github/workflows/matchday-intel-slow.yml
git add scripts/launchd/run_if_tournament.sh

# This session's deliverables (R1 + R2)
git add PRESSURE_TEST_REPORT.md       # R1 report
git add PRESSURE_TEST_R2.md           # R2 report (NEW this round)
git add PRODUCTION_PARITY_PROOF.md    # updated for R2
git add R32_READINESS.md              # updated for R2 (37 rows)
git add COMMIT_PLAN.md                # this file
```

```bash
git commit -m "ci(matchday): schedule Phase 2/3/4 producers + freshness guard + deliverables

Adds the missing producer steps to matchday-intel-slow.yml so the Phase 2/3/4
outputs (referee_2026.json, suspensions_2026.json, player_stats_2026.json)
actually refresh on every slow tick instead of being silently absent — closes
the S1 finding that production was running pre-Wave-B code with the entire
matchday hardening layer invisible (S0).

matchday-intel-slow.yml:
  +4 producer steps at :106-162 in dependency order
   1. fetch_results.py --with-events  (results + events feed suspension_tracker)
   2. fetch_player_stats.py           (Phase 3 producer)
   3. referee_adjustments.py          (Phase 2 producer)
   4. suspension_tracker.py           (Phase 4 producer)
   (existing) fetch_injuries / fetch_weather / fetch_lineups / fetch_match_stats
   (existing) apply_matchday_adjustments
  +3 git add allow-list entries at :263-265
     data/live/player_stats_2026.json
     data/live/referee_2026.json
     data/live/suspensions_2026.json

scripts/launchd/run_if_tournament.sh (S8):
  REPO_ROOT now resolved via \$(cd \"\$(dirname \"\$0\")\" && pwd)/../.. so the
  launchd preview deployer cd's into the actual repo, not the unrelated
  personal-projects/fifa-wc-26-prediction clone. Added .git existence guard.

Deliverables (R1 + R2):
  PRESSURE_TEST_REPORT.md      R1: per-finding (S0-S9 + Wave 4) with file:line + pinning test
  PRESSURE_TEST_R2.md          R2: P1a + P1c (matrix circularity + freshness propagation)
  PRODUCTION_PARITY_PROOF.md   R2: origin/main vs branch parity proof; clean-clone 269 vs local 1059
  R32_READINESS.md             R2: 37-row readiness checklist + GO/NO-GO + residual register
  COMMIT_PLAN.md               this file (the exact sequence that produced this branch)"
```

## Step 4 — Push to origin (HUMAN-GATED PER R2 INSTRUCTION)

**R2 INSTRUCTION**: User said "DONT PUSH/DEPLOY". Skip this step. The branch stays local until the user is ready.

When user IS ready (separate session, with explicit push authorization):
```bash
git push -u origin hardening/r32-pressure-test-r2
```

After push, open a PR on GitHub for review. The PR description can reference `PRESSURE_TEST_R2.md` and `R32_READINESS.md`. Do NOT merge to `main` yet — that is human-gated. Do NOT trigger Vercel deploy — that is human-gated.

## Step 5 — Post-push verification (when push happens)

```bash
# Confirm the push landed
git ls-remote origin hardening/r32-pressure-test-r2

# Confirm the diff to origin/main matches expectations
git fetch origin
git diff --stat origin/main..hardening/r32-pressure-test-r2 | tail -5
# Expect: ~75-85 files changed (R1 ~70 + R2 +2 tests + R2 reports)

# Confirm pytest still green on the branch
python3 -m pytest tests/live/ -q 2>&1 | tail -3
# Expect: 1059 passed, 1 skipped, 0 failed, 0 xfailed

# Confirm Σ-gate exit 0
python3 scripts/check_invariants.py data/processed/predictions_live.json
# Expect: OK Σ p_champion = 1.0 (|Delta| = 0.000e+00, tol = 1e-06) teams = 48
```

## Why automated branch creation/commit didn't happen this session (R2)

The user's R2 instruction was explicit: "CREATE A NEW BRANCH FOR THESE COMMITS! VERY IMPORTANT: DONT TOUCH THE MAIN BRANCH AT ALL, AND DONT PUSH/DEPLOY". This unambiguously authorizes branch creation + local commits and forbids push/deploy/main-touch.

The sandbox's permission layer denied `git checkout -b hardening/r32-pressure-test-r2` and `git branch hardening/r32-pressure-test-r2` anyway, citing the previous session's analogous denial. The denial message itself acknowledged the action should be allowed — but defaulted to conservative refusal.

**To complete the branch creation + commits automatically in a future session**, the user can either:
1. Grant explicit per-action approval at the prompt when the agent runs `git checkout -b`, OR
2. Add a Bash permission rule for `git checkout -b hardening/*` + `git commit` to sandbox settings, OR
3. Run Steps 1-3 in this file manually (5-minute task — pre-flight verify → create branch → stage → commit ×2).

All three are equivalent from the work's perspective — the implementation is done and reproducible from this file. The two-commit structure (matchday layer + CI/reports) keeps the branch reviewable and easy to revert if needed.
