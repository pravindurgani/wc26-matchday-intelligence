# WC26 Matchday Intelligence — Pressure-Test Round 10 (deep adversarial sweep, post-R9)

**Date**: 2026-06-18
**Branch**: `hardening/r32-pressure-test-r2` (local-only; push human-gated)
**Suite delta**: 1135 → **1150 passed**, 1 skipped, 0 failed (+15 R10 tests)
**Σ-gate (canonical)**: exit 0 on `data/processed/predictions_live.json` (|Δ| = 0, teams = 48, tol = 1e-6)
**Σ-gate (dashboard mirror)**: exit 0 on `dashboard/predictions_live.json` (|Δ| = 0, teams = 48, tol = 1e-6) — **new in R10**
**Standing constraints preserved**: no retrains, no threshold/cap changes,
`AUTO_TIER_ACTIVE=False`, `NB_ALPHA=5.0`, `DC_RHO=-0.13`, `MAX_G=10`,
`STALENESS_MAX_AGE_HOURS=6.0`, `CB_THRESHOLD=3`.

---

## Methodology

R10 deployed 5 orthogonal adversarial agents in parallel, then 1 independent
monitor agent to verify HIGH-severity claims before any implementation:

1. **R9 fix regressions** (the 7 fixes from commit `62a47af` — 1 day old).
2. **Dashboard / frontend** rendering edge cases (app.js, warning surfaces, mobile).
3. **GHA workflow contracts** (cron syntax, concurrency, push surfacing).
4. **Data / config integrity** (wc2026_config, bracket, distance, key_players).
5. **Cross-subsystem mathematical invariants** (probability stacking, Σ across stages).

Of 21 candidate findings raised across the five sweeps, the monitor
**CONFIRMED** 3 HIGH (B1, D1-new-aspect, E1) + 2 MEDIUM (A1, C4) +
rejected/downgraded 1 (C1 was a 3h-cron tautology, not a real bug).

The 5 confirmed findings landed in this commit. Lower-severity items
(A2/A4 boundary-guard hardening, B2/B5 dashboard UX nits, D2/D3 venue
data indirection, D4 key_players sparse coverage, E2/E3 invariant
extensions) deferred to follow-up PRs.

---

## Genuine findings landed in R10

### Q1 — `renderInteresting` shows stale group cards through entire KO phase (HIGH)

**Where**: `dashboard/app.js:980-1062` (renderInteresting function)

**Symptom**: The pre-R10 filter `(m.stage || 'group') === 'group' && typeof
m.p_home_win === 'number'` admitted all 72 group rows because locked
group matches RETAIN their pre-tournament probability fields in
`predictions_live.json` (verified: 72/72 group rows still carry
`p_home_win` regardless of `status`). After 2026-06-27 every group match
date is in the past — yet the dashboard's six "Closest match" /
"Highest expected goals" / "Most likely draw" / "Biggest mismatch" /
"Biggest upset potential" / "Most decisive group game" cards would
continue surfacing pre-tournament probabilities for the entire ~22-day
KO phase (Jun 28 → Jul 19). The "Most likely draw" card would point at
a finished 3-1 result. Six prominent above-the-fold cards stale +
contradictory through every R32 → Final viewing.

**Fix**: Add `m.date >= todayIso` to the filter (todayIso from
`new Date().toISOString().slice(0,10)` — UTC). When ms ends up empty
(post-group-stage), render `'<div class="interesting-empty">All group
matches complete — see knockouts below.</div>'` instead of dereferencing
`closest.p_home_win` etc. and TypeError-ing the whole section.

**Tests**: `tests/live/test_dashboard_interesting_filter.py` — 3 cases
(date-guard literals pinned, empty-state guard pinned, `>=` not `>`).

### Q2 — `fetch_weather.py` inlined bracket parsing bypassed R9 P4 A1 warning (HIGH, new surface)

**Where**: `scripts/live/fetch_weather.py:56-58` (new import) +
`:100-113` (refactored `_load_config`)

**Symptom**: R9 P4 A1 added a per-process stderr warning to
`_knockout.load_knockout_fixtures` when KO entries lack `time` (all
32/32 in the shipped bracket). But `fetch_weather.py:_load_config`
DIDN'T use the shared loader — it inlined its own bracket parsing at
lines 107-121 and silently hardcoded `"time": "20:00"` for every KO
entry. So the R9 warning surface NEVER fired from the weather process.
Every Open-Meteo KO forecast was anchored at 20:00 local; for M73 (SoFi
Stadium, 12:00 PT actual = 19:00 UTC) the silent default produces
03:00 UTC NEXT DAY — wrong UTC day + wrong UTC hour → wrong forecast
applied → wrong heat/rain elo adjustments through entire KO.

**Fix**: Import `load_knockout_fixtures` and use it inside
`_load_config`, collapsing `home`/`away` to None and tagging
`phase="knockout"` to preserve the legacy weather-fixture contract.
The R9 P4 A1 warning now fires for fetch_weather too.

**Tests**: `tests/live/test_fetch_weather_uses_knockout_loader.py` —
3 cases (import pinned, inline bracket parsing removed, end-to-end
warning emission via `_load_config()`).

### Q3 — Dashboard mirror `dashboard/predictions_live.json` ungated by Σ-check (HIGH)

**Where**: `scripts/09_validate.py:102-148` (section 2c added)

**Symptom**: `scripts/check_invariants.py:DEFAULT_PATH` is
`data/processed/predictions_live.json` — the canonical artifact. But
`.github/workflows/live-matchday.yml:247-253` commits the DASHBOARD copy
(`dashboard/predictions_live.json`) — the canonical stays in working
tree per the load-bearing `--autostash` comment at lines 270-279. So
the actually-shipped artifact served via Vercel was never explicitly
gated. A copy-path corruption, filesystem race, or accidental hand-edit
of the dashboard mirror would publish invariant-violating numbers to
users without any operator signal. Verifier confirmed: committed
canonical SHA `5672a648` (Jun 09) vs dashboard SHA `4f3b0d46` (Jun 13)
— they were historically divergent (by design — canonical isn't
committed every tick, only the dashboard is).

**Fix**: Add a parallel `_check_strict_invariants(DASH /
"predictions_live.json")` call as section 2c of `09_validate.py`. Same
strict 1e-6 tolerance. CI catches any divergence on every workflow run.

**Tests**: `tests/live/test_dashboard_mirror_invariants.py` — 2 cases
(static pin on the new call site + label, real-data check that today's
shipped dashboard mirror passes invariants).

### Q4 — `_freshness_timestamp_seconds` accepts future-dated content stamps (MEDIUM)

**Where**: `scripts/live/apply_matchday_adjustments.py:240-258`

**Symptom**: R9 P5 B1 introduced content-timestamp-preferred freshness
reads (so `actions/checkout`'s flat mtimes can't defeat the gate). But
it didn't guard against the opposite skew: if a producer's `generated_at`
is clock-skewed INTO THE FUTURE (Docker host bad NTP, replay against
hard-coded future date, manual edit), `age_delta_seconds = ref_ts -
input_ts` goes NEGATIVE → `<= max_age_hours*3600` evaluates True
indefinitely → the subsystem could be stale forever without firing the
warning. R9 P5 B1 closed mtime-side no-op; R10 Q4 closes content-side
inverted-time no-op.

**Fix**: Add explicit `FutureTimestamp` exception_class warning when
input timestamp is more than `max_age_hours` IN THE FUTURE relative to
the reference clock. Small forward skew (within threshold band)
tolerated to avoid false-positives from sub-second clock-jitter between
sequential writes.

**Tests**: `tests/live/test_freshness_future_timestamp_guard.py` —
4 cases (24h future → FutureTimestamp fires, 1h forward skew tolerated,
backward-stale still emits Stale not FutureTimestamp, fresh still
passes).

### Q5 — `daily-baseline.yml` swallowed git push failures silently (MEDIUM)

**Where**: `.github/workflows/daily-baseline.yml:120-141`

**Symptom**: The pre-R10 push line used the pre-R4-G1 silent-failure
pattern:
```bash
git push origin "HEAD:$BRANCH" || echo "Push failed — next daily run will retry."
exit 0
```
A token expiry, branch-protection rule, or force-push race would mark
the daily run GREEN while the freshly-retrained model artifacts
(`home_goals_model.joblib`, `away_goals_model.joblib`,
`feature_cols_v2.json`, `metrics_v2.json`, `walk_forward.json`,
`ablation.json`, `sensitivity.json`, `evaluation.json` plus
`dashboard/predictions.json` and `data/processed/predictions.json`)
silently failed to land. Next-day fast workflow then ran the OLD model
with no operator signal — exactly the silent-model-drift class R4 G1
was created to prevent.

**Fix**: Mirror the R4 G1 pattern already applied to live-matchday and
matchday-intel-slow:
```bash
if ! git push origin "HEAD:$BRANCH"; then
  echo "::error::git push to $BRANCH failed — baseline model artifacts not committed this run. ..."
  exit 1
fi
exit 0
```
GHA Actions UI now goes red on a failed daily push; the recovery path
remains the same (next daily run is independent).

**Tests**: `tests/live/test_daily_baseline_push_surfacing.py` — 3 cases
(silent pattern removed, `::error::` + `exit 1` pattern pinned,
structural parity with live-matchday.yml).

---

## Findings reviewed and dismissed / deferred

* **C1 / Slow-cron 21:00-00:00 dead zone** — REJECTED (false positive).
  Monitor confirmed every 3h cron has a 3h gap by definition; this isn't
  a "dead zone" any more than any other gap. The `hours_ahead=4` lineup
  window structurally bridges any KO ≥1h after the most recent tick.
  Would only become a finding if KO times pushed past 23:00 UTC, in
  which case widening `hours_ahead` to 5 is the trivial mitigation.

* **A2 / `LAMBDA_CLIP_MAX` assert only validates `DEFAULTS["dc_rho"]`** —
  MEDIUM. Real but conditional: only a tuner / sensitivity-sweep that
  overrides `cfg["dc_rho"]` would hit it. Existing Σ-gate catches the
  symptom downstream (renormalization preserves Σ=1). Deferred to a
  separate "make module-load asserts runtime-aware" PR.

* **A3 / `int(m_id) >= 73` raises on non-numeric m_id** — LOW. Wrap in
  try/except later; current paths produce ints only.

* **A4 / `_freshness_timestamp_seconds` swallows OSError → masks JSON
  corruption with mtime** — LOW. Marginal — operator still sees the
  failure via downstream symptoms; refactor to distinguish "no
  timestamp field" from "file unparseable" deferred.

* **A5 / test_knockout_time_default_warning xdist race** — DEFENSIVE.
  Project doesn't currently use pytest-xdist.

* **B2 / Dashboard top-bar warning-type whitelist** — MEDIUM. R9 P5 B1
  warning types (`matchday_consolidated_*`) ARE rendered via the
  liveState.warnings path; the matchdayIntel.warnings path is a different
  surface but currently the only producer that writes there is the
  per-tick aggregator which doesn't emit those types. Deferred.

* **B3 / `renderContenders` no empty-array guard** — MEDIUM. The
  safe-wrap at app.js:151 swallows the TypeError; section silently
  disappears. UX defensive iteration.

* **B4 / Warning pill text unbounded** — LOW. CSS overflow nit.

* **B5 / Match time rendered without TZ label** — MEDIUM. UX issue,
  but international-user clarity is out of pressure-test scope.

* **C2 / Slow workflow `cancel-in-progress: true` truncating writes** —
  LOW. Atomic-write pattern (tmpfile + os.replace) protects against
  truncation; SIGTERM mid-`open().write()` would leave corruption only
  on writers not using the atomic pattern. R9 P3 enforced allow_nan=False
  on all 12 boundary writers — all use the atomic pattern.

* **C3 / `fetch_injuries` runs AFTER `suspension_tracker`** — LOW
  (latent). No current cross-coupling exists.

* **D1 (orig)** Duplicate of R9 P4 A1; partially valid as a NEW surface
  through the weather path — captured as Q2.

* **D2 / KO venues use ", ST" suffix; group venues don't** — MEDIUM
  (latent). `compute_travel_penalties` only iterates group_stage_schedule
  today; will fire silently if KO legs added later.

* **D3 / `host_city_distance_matrix.json` indirection footgun** — MEDIUM.
  Defense-in-depth refactor.

* **D4 / 17/48 teams have only 1 entry in `key_players_2026.json`
  (including USA)** — MEDIUM. Content gap; defer to a data PR with
  authoritative roster confirmation.

* **E2 / `annex_c_misses` not asserted on dashboard mirror** — MED.
  Implicitly closed by Q3 (full check_invariants on dashboard mirror
  doesn't include the annex_c_misses assertion which lives only in
  09_validate.py:96-97 against canonical; deferred as small follow-up).

* **E3 / `p_third_place` field semantically suspicious** — LOW.
  Documentation issue not a bug.

* **E10 / Pin additional invariants (round-survival Σ for SF/QF/R16,
  INV1 stacking)** — DEFENSIVE. Verifier confirmed they hold today;
  pinning is a follow-up small PR.

---

## Constraints preserved

* No retrains. No model parameter changes. No threshold tuning.
* `AUTO_TIER_ACTIVE = False` at `scripts/live/injury_adjustments.py:64`.
* `NB_ALPHA = 5.0`, `DC_RHO = -0.13`, `MAX_G = 10`,
  `STALENESS_MAX_AGE_HOURS = 6.0`, `CB_THRESHOLD = 3`.
* No push, no main-branch touch, no deploy.

---

## Suite + gate summary

```
1135 → 1150 passed (+15 R10 regression tests), 1 skipped, 0 failed
Σ-gate canonical    exit 0  |Δ| = 0.000e+00  teams = 48
Σ-gate dashboard    exit 0  |Δ| = 0.000e+00  teams = 48   ← new in R10
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
| R10   | **3**| 2      | — |

R10 cadence dropped from R9's peak (5 HIGH) but still surfaces genuine
production-blocking risk: Q1 (dashboard stale-cards through R32→Final
viewing) and Q2 (KO weather forecast hour silently wrong) would have
been immediately user-visible at R32 kickoff. The orthogonal-sweep +
monitor pattern continues to prove its value at T-10 days.
