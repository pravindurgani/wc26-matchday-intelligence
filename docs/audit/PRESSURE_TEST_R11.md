# WC26 Matchday Intelligence — Pressure-Test Round 11 (deep adversarial sweep, post-R10, NO DEFERRALS)

**Date**: 2026-06-18
**Branch**: `hardening/r32-pressure-test-r2` (local-only; push human-gated)
**Suite delta**: 1150 → **1206 passed**, 1 skipped, 0 failed (+56 R11 tests)
**Σ-gate (canonical)**: exit 0 on `data/processed/predictions_live.json` (|Δ| = 0, teams = 48, tol = 1e-6)
**Σ-gate (dashboard mirror)**: exit 0 on `dashboard/predictions_live.json` (|Δ| = 0, teams = 48, tol = 1e-6)
**Standing constraints preserved**: no retrains, no threshold/cap changes,
`AUTO_TIER_ACTIVE=False`, `NB_ALPHA=5.0`, `DC_RHO=-0.13`, `MAX_G=10`,
`STALENESS_MAX_AGE_HOURS=6.0`, `CB_THRESHOLD=3`.

---

## Methodology

R11 deployed **5 orthogonal adversarial agents** in parallel + **2 independent
monitor agents** to verify HIGH-severity claims before any implementation.

**User instruction this round (critical addition)**:
> "REVIEW EVERYTHING THOROUGHLY END-TO-END AND MAKE DECISIONS WITH THE RIGHT
> APPROACH APPROPRIATELY AND FUCKING IMPLEMENT UPDATES/CHANGES WITH BEST OF
> YOUR CAPABILITIES!!! ... **DONT DEFER ANYTHING!!**"

R11 therefore closed both the new R11 findings AND all 12 R10-deferred items
in the same commit. No new deferral queue.

The 5 sweeps targeted:
1. **R10 fix regressions + R10 deferral clean-up** (E1 → E10).
2. **Security / secrets hygiene** — env vars, log redaction, .env, API tokens, XSS surface.
3. **Network / HTTP edge cases** — retry logic, timeout handling, status codes, redirects.
4. **Observability / logging coverage** — silent-failure paths, structured warnings, GHA visibility.
5. **Data validation / schema enforcement** — provider response sanitization, schema invariants.

Of ~26 candidate findings raised across the five sweeps + 14 R10 deferrals,
the monitors **CONFIRMED** the 9 HIGH-severity production issues listed below
plus 12 R10-deferred items (1 false positive C2-old rejected, 1 documentation-
only E3-old dismissed). The most consequential finding by far is **E1, an
R32-blocker** that would have silently rejected every knockout result from
2026-06-28 onward.

---

## Genuine R11 findings landed in this commit

### E1 — **R32-BLOCKER**: `validate_match` uses groups-only schedule (HIGH)

**Where**: `scripts/live/fetch_results.py:867-893` (validate_match) + `:913` (main schedule load)

**Symptom**: Pre-R11 `main()` loaded `schedule = cfg.get("group_stage_schedule", [])` — 72 group rows only. `validate_match` resolves the fixture via `next((f for f in schedule if f["m"] == m["m"]), None)`. For any KO match (m=73..104), this returned None → `(False, "match {m} not in WC2026 schedule")` → the result hit the `rejected[]` bucket and was excluded from `valid[]`. The "preserve existing if shrinking" guard at line 1095 fires only when `len(valid) < existing_n`, so locked KO data was also never preserved.

**From 2026-06-28 onward**: every R32 / R16 / QF / SF / 3rd-place / Final result emitted by both `fetch_api_football` and `fetch_football_data` would have been rejected. Dashboard freezes at end-of-groups; suspensions never resolve from KO events; sim never sees locked KO winners — `decide_knockout` re-samples every completed KO 25,000× per tick.

**Fix**:
1. Extend `main()` schedule load: `cfg.get("group_stage_schedule", []) + load_knockout_fixtures()`.
2. In `validate_match`, early-return ok when `m["m"] >= 73`. The KO bracket carries slot codes ("1A", "W74") in `home`/`away`; the API-Football adapter at `fetch_results.py:661-667` already substitutes resolved team names for m>=73. Comparing resolved names to slot codes would reject every KO result forever.

**Tests**: `tests/live/test_r11_validate_match_ko.py` — 5 cases (KO accepted with resolved names, group still enforces home/away, static pin on schedule extension, static pin on the m>=73 early-return, unknown id still rejected).

### E3 — `fetch_match_stats` positional fallback swaps sides (HIGH)

**Where**: `scripts/live/fetch_match_stats.py:177-211` (build_match_entry)

**Symptom**: Pre-R11 `if not home_stats_raw and (team_name == home_team or len(response_sides) == 2)` triggered the positional fallback on EVERY normal fixture (always 2 sides). When the provider returned [away, home] order — API-Football has no documented ordering guarantee — `home_stats_raw` was set from the away side's `statistics`. `home_form_adjustment_elo` / `away_form_adjustment_elo` then carried the opposite team's shot/possession dominance. Worst case: the trailing team got a positive Elo boost in a match it dominated against.

**Fix**: Drop the positional fallback. Import `normalize_team` from `fetch_results` so canonical aliases ("USA" → "United States" etc.) resolve. Match by name only; unrecognized sides surface via a `side_match_warnings` list propagated up to the producer's warnings array.

**Tests**: `tests/live/test_r11_side_resolution.py::TestR11E3MatchStatsSideResolution` — 4 cases.

### E4 — `fetch_lineups` positional fallback swaps sides (HIGH)

**Where**: `scripts/live/fetch_lineups.py:251-309` (build_lineup_entry)

**Symptom**: Same class of bug as E3, even more brittle. `team_name` was fetched at line 259 but UNUSED. Pure positional `if not home_block: home_block = side_entry else: away_block = side_entry`. When provider returned [away, home], `home_block` was set from the away side. Downstream `compute_lineup_delta_elo` compared the AWAY GK to the home team's prior XI's GK → false GK swap (-8 Elo each), and 11 outfield "positions changed" → -3 weighted each → cap-hit `LINEUP_CAP=20` Elo delta per team on every mis-ordered fixture.

**Fix**: Same as E3 — import `normalize_team`, name-match both sides, drop positional assignment, propagate `side_match_warnings`.

**Tests**: `tests/live/test_r11_side_resolution.py::TestR11E4LineupsSideResolution` — 3 cases.

### D1 — Circuit breaker CANNOT trip across GHA ticks (HIGH)

**Where**: `scripts/live/run_live_update.py:41` (CB_PATH) + `.github/workflows/live-matchday.yml:247-253` (commit allow-list) + `.gitignore:41`

**Symptom**: `CB_PATH = data/live/circuit_breaker_state.json` was BOTH gitignored AND missing from the commit allow-list. Every GHA tick boots from `fetch-depth: 1` with no CB file → `read_circuit_breaker()` returns 0 every time. `CB_THRESHOLD=3` escalation only fired within a single Python process — 6 consecutive sim_failures over 6 cron ticks produced 6 separate "1/3" warnings with no human-required halt.

**Fix**: Explicitly UN-ignore `data/live/circuit_breaker_state.json` in `.gitignore` (keep the bare `circuit_breaker_state.json` line so root-level strays stay blocked). Add the path to the commit allow-list in `live-matchday.yml`.

**Tests**: `tests/live/test_r11_cb_persistence.py` — 3 cases (gitignore un-ignore pinned, commit allow-list pinned, root-level bare pattern still blocks strays).

### D2 — Vercel deploy silently fails in `daily-baseline.yml` (HIGH)

**Where**: `.github/workflows/daily-baseline.yml:141-167`

**Symptom**: Pre-R11 the deploy step used the pre-R4-G1 silent-failure pattern: `set +e` + trailing `exit 0`. A revoked `VERCEL_TOKEN`, project-moved error, build failure, or transient 5xx returned non-zero from `npx vercel deploy`, but the trailing `exit 0` overrode it. The daily-baseline job went GREEN while the dashboard silently stopped getting redeployed. Mirrors the silent-push-failure class that R4 G1 and R10 Q5 closed for the git push step.

**Fix**: Capture rc from both `vercel pull` and `vercel deploy` with `npx ...; rc=$?` pattern. On non-zero, emit `::error::vercel deploy failed` + `exit 1`. Preserve the intentional missing-VERCEL_TOKEN skip clause.

**Tests**: `tests/live/test_r11_vercel_deploy_surfacing.py` — 5 cases.

### D3 — Σ-gate failure invisible at live-tick (HIGH)

**Where**: `scripts/live/run_live_update.py:737`

**Symptom**: `run([sys.executable, "scripts/09_validate.py"])` — return code DISCARDED. If the R10 Q3 strict 1e-6 Σ-gate (canonical + dashboard mirror) failed on a LIVE tick, the corrupt `predictions_live.json` was already published in Step 7 (lines 720-734) one step earlier. Dashboard rendered invariant-violating data with zero operator signal.

**Fix**: Capture rc into `validate_rc`. On non-zero, append a `sigma_gate_failed` warning to live_state.warnings and re-write `live_state.json`. The corrupt publish is already in flight, but the operator's top pill now surfaces the failure (next tick re-runs validate against the next sim and converges).

**Tests**: `tests/live/test_r11_sigma_gate_warning.py` — 3 cases.

### D4 — 4 loaders missing `_check_freshness` (HIGH)

**Where**: `scripts/live/apply_matchday_adjustments.py` — `_load_weather_components` (625), `_load_lineup_components` (792), `_load_injury_components` (457), `_load_stats_components` (835).

**Symptom**: Pre-R11 only referee / suspension / player_stats had freshness guards. A multi-day-stale `weather_2026.json` / `lineups_2026.json` / `injuries_2026.json` / `match_stats_2026.json` snapshot was silently ingested with no `subsystem_stale` warning. The fast-path `matchday_subsystem_stale` lift only fires when the warning is EMBEDDED — without per-feed freshness checks the operator never sees it.

**Fix**: Add 4 `_check_freshness` calls at the top of each `_load_*_components` function, mirroring the existing pattern at lines 683 / 736 / 1047.

**Tests**: `tests/live/test_r11_freshness_loaders.py::TestR11D4AllSevenLoadersHaveFreshnessCheck` — 4 cases. The pre-existing `tests/live/test_apply_matchday_adjustments.py::TestFreshnessGuard::test_all_three_fresh_emits_no_subsystem_stale` was updated to scaffold all 7 feeds (was 3).

### D5 — `update_team_state.py` non-atomic write + missing `last_updated` (HIGH)

**Where**: `scripts/live/update_team_state.py:62, :109`

**Symptom**: Pre-R11 the file was written with bare `.write_text(json.dumps(...))`. A SIGKILL / OOM / disk-full mid-write left a partial JSON on disk that the simulator parses with bare `json.loads` at `03_simulate.py:698` → JSONDecodeError → tick crash. Additionally the output dict was missing `last_updated`, so `compute_input_hash` (at run_live_update.py:278 and 03_simulate.py:1171) always read empty string for this field. A stalled writer that re-emitted identical deltas across many ticks was invisible to the hash gate. The file fell outside the R9 P3 "12 boundary writers" allow_nan=False sweep entirely.

**Fix**: Introduce a local `_atomic_write_json` helper using `tempfile.NamedTemporaryFile` + `os.replace` (mirrors the pattern in `run_live_update.atomic_write_json`). Add `last_updated` field to both write branches (empty completed + populated).

**Tests**: `tests/live/test_r11_update_team_state_atomic.py` — 5 cases.

### C3 — Events fetch retries=2 + no Retry-After (HIGH-MEDIUM)

**Where**: `scripts/live/fetch_results.py:380` + `scripts/live/_http_client.py:82-100`

**Symptom**: `fetch_apifootball_events_for_fixture` explicitly passed `retries=2` (vs the default 3). Net retries: 2 attempts with one 1s sleep between. R32 burst of 8 KO matches in 24h would hit `/fixtures/events` back-to-back; a single 5xx with retries=2 means one attempt + 1s sleep + one attempt → suspension data missed for that match → no penalty for the player's next match. Combined with `_http_client.http_get_json` ignoring `Retry-After` on 429: a provider sending `Retry-After: 60` would just trigger 2^attempt = 1-2-4s backoff, then we'd hammer again and get rate-limited again.

**Fix**: Bump events fetch to `retries=3`. Honor `Retry-After` header in `_http_client.http_get_json` (cap at 60s so a misbehaving provider can't block a producer step past its budget). Retry 429 with backoff (pre-R11 it was treated as no-retry 4xx).

**Tests**: `tests/live/test_r11_http_retry_after.py` — 5 cases (Retry-After seconds honored, capped at 60s, 429 now retries 3 times, other 4xx still no-retry, events fetch retries=3 pinned).

### C1 — Retry-After parsing landed (MEDIUM; documented as part of C3 above)

**Where**: `scripts/live/_http_client.py:82-130` + new `_backoff_seconds` helper

**Symptom + fix**: See C3.

### C2 — Aggregate failure detector for `fetch_player_stats` (MEDIUM)

**Where**: `scripts/live/fetch_player_stats.py:368-421`

**Symptom**: Pre-R11 per-team failure recorded a warning but the outer 48-team fan-out continued with `out[team] = []` for that team. With no aggregate threshold detector the operator had no top-level signal that (e.g.) 14 of 48 teams ended up with empty rosters — auto_tier silently collapsed to `auto_no_data` for those teams without any pill lighting up.

**Fix**: Count `empty_teams` across the fan-out. When `len(empty_teams) > 5` (~10% of WC squad), append a single `player_stats_partial` aggregate warning.

**Tests**: `tests/live/test_r11_player_stats_partial.py` — 2 cases (fires above threshold, does NOT fire below threshold).

---

## R10 deferrals cleaned up this round (12 items)

| ID | Severity | Fix | File:line | Test |
|---|---|---|---|---|
| **A2** | MED | runtime `cfg["dc_rho"]` assert in `build_score_matrix` (was DEFAULTS-only) | `scripts/03_simulate.py:186-217` | `test_r11_r10_deferred.py::TestR11A2RuntimeDcRhoAssert` |
| **A4** | LOW | distinguish OSError from JSONDecodeError; corrupt JSON now surfaces `CorruptJSON` warning | `scripts/live/apply_matchday_adjustments.py:154-200` + `_check_freshness:280+` | `test_r11_freshness_loaders.py::TestR11A4CorruptJSONFallbackEmitsWarning` |
| **B2** | MED | `INTEL_TOP_BAR_TYPES` extended with 13 alert-grade types (subsystem_degraded, pipeline_unhealthy, matchday_consolidated_*, no_records_returned, provider_returned_nothing, side_match_unrecognized, lineup_side_unrecognized, sigma_gate_failed, etc.) | `dashboard/app.js:390-406` | `test_r11_r10_deferred.py::TestR11B2IntelTopBarTypesExtended` |
| **B3** | MED | `renderContenders` empty-array guard renders explicit "No team predictions available" placeholder row instead of silently disappearing on TypeError | `dashboard/app.js:716-735` | `test_r11_r10_deferred.py::TestR11B3RenderContendersEmptyGuard` |
| **B4** | LOW | `.last-updated` bounded with `max-width: 60ch` + `text-overflow: ellipsis`; tooltip carries full message | `dashboard/styles.css:243-256` | `test_r11_r10_deferred.py::TestR11B4WarningPillBounded` |
| **B5** | MED | match-head time render appends `' (local)'` suffix (WC2026 venues span 4 NA time zones; raw HH:MM is ambiguous) | `dashboard/app.js:1252` | `test_r11_r10_deferred.py::TestR11B5MatchTimeHasTZLabel` |
| **C3-old** | LOW (latent) | workflow step ordering: `Fetch injuries` now runs BEFORE `Build suspension tracker` (defense-in-depth) | `.github/workflows/matchday-intel-slow.yml:151-194` | `test_r11_r10_deferred.py::TestR11C3OldWorkflowOrdering` |
| **D2-old** | MED | KO venue ", ST" suffix normalized at load via `_normalize_venue` (strips state suffix so `venue_city_map` lookup is symmetric with group venues) | `scripts/live/_knockout.py:111-160` | `test_r11_r10_deferred.py::TestR11D2OldKOVenueNormalize` |
| **D3-old** | MED | `validate_venue_distance_indirection` startup validator — asserts every venue in group + KO schedule maps to a city present in distance_matrix | `scripts/03_simulate.py:619-668` + startup wire at `:1144-1156` | `test_r11_r10_deferred.py::TestR11D3OldDistanceMatrixValidator` |
| **E2-old** | MED | `annex_c_misses == 0` pinned in `check_invariants` (auto-applies to dashboard mirror via R10 Q3 wiring — no extra 09_validate.py line needed) | `scripts/check_invariants.py:193-204` | `test_r11_r10_deferred.py::TestR11E2OldAnnexCMissesInvariant` |
| **E10** | DEFENSIVE | per-stage Σ pinned (Σ p_reach_qf=8, p_reach_sf=4, p_reach_final=2, tol 1e-6) + INV1 per-team stacking (`p_champion ≤ p_reach_final ≤ p_reach_sf ≤ p_reach_qf`) | `scripts/check_invariants.py:206-260` | `test_r11_r10_deferred.py::TestR11E10PerStageSigmaAndStacking` |

---

## R11 dismissals (false positives / docs-only — explicit reject)

* **C2-old / `matchday-intel-slow.yml` `cancel-in-progress: true` truncating writes** — REJECTED. The R9 P3 atomic-write sweep (tempfile + os.replace) protects all 12 boundary writers. SIGTERM mid-`open().write()` would only corrupt non-atomic writers; the R11 D5 fix to `update_team_state.py` closes the last known one. The C2-old finding was correct at the time R10 raised it; now closed by transitive coverage.

* **E3-old / `p_third_place` field semantics** — DOCUMENTATION-ONLY. The field is the probability of winning the 3rd-place playoff (M103). `p_finish_3rd_group` is "finishes 3rd in group". The two are different concepts. The finding was a documentation/naming nit, not a code bug. Rename + docstring deferred indefinitely (out of pressure-test scope).

---

## Security sweep (R11-B): clean (no HIGH)

* **0 HIGH findings.** All `${{ secrets.* }}` references are env-only, never interpolated into bash. `workflow_dispatch` inputs (`provider`, `dry_run`, `hours_ahead`, `commit_samples`, `skip_injuries`) pass through `env:` with explicit allowlists. `pre_flight.py:308` enforces the env-only pattern as a CI gate.
* **1 MEDIUM**: `vercel.json:44` allows `script-src 'self' 'unsafe-inline'`. All ~20 innerHTML sinks ARE escaped via `escapeHtml`, but the combination is fragile to a future forgotten escape. Tightening CSP to a nonce/hash is a defense-in-depth follow-up outside pressure-test scope (no current exploit path; player names are constrained to a hard WC26 team allowlist server-side).
* **4 LOW** (URL-in-error-message hardening for future provider migrations with query-string auth; `--token=` argv exposure on `npx vercel`; etc.) — documented in the agent transcript but not landed (no exploit path on current providers, all header-auth-only).

---

## Constraints preserved

* No retrains. No model parameter changes. No threshold tuning.
* `AUTO_TIER_ACTIVE = False` at `scripts/live/injury_adjustments.py:64`.
* `NB_ALPHA = 5.0`, `DC_RHO = -0.13`, `MAX_G = 10`, `STALENESS_MAX_AGE_HOURS = 6.0`, `CB_THRESHOLD = 3`.
* No push, no main-branch touch, no deploy.

---

## Suite + gate summary

```
1150 → 1206 passed (+56 R11 regression tests), 1 skipped, 0 failed
Σ-gate canonical    exit 0  |Δ| = 0.000e+00  teams = 48
Σ-gate dashboard    exit 0  |Δ| = 0.000e+00  teams = 48
Branch hardening/r32-pressure-test-r2 unchanged
```

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
| R11   | **8** | **4** + **12 R10 deferrals** | — |

R11 has the highest HIGH-severity count of any round so far AND closes
the entire R10 deferral queue in the same commit per the user's "DONT
DEFER ANYTHING" instruction. The single most consequential finding is
**E1** — without this fix, every KO result from 2026-06-28 onward would
have been silently rejected, freezing the dashboard at end-of-groups
through R32 → Final. Surfaced at T-10 days from R32 kickoff, with no
remaining deferred items heading into the live window.
