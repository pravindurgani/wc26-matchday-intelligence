# WC26 Matchday Intelligence — Pressure-Test Round 9 (deep adversarial sweep, post-R8)

**Date**: 2026-06-18
**Branch**: `hardening/r32-pressure-test-r2` (local-only; push human-gated)
**Suite delta**: 1110 → **1135 passed**, 1 skipped, 0 failed (+25 R9 tests)
**Σ-gate**: exit 0 on `data/processed/predictions_live.json` (|Δ| = 0, teams = 48, tol = 1e-6)
**Standing constraints preserved**: no retrains, no threshold/cap changes,
`AUTO_TIER_ACTIVE=False`, `NB_ALPHA=5.0`, `DC_RHO=-0.13`, `MAX_G=10`,
`STALENESS_MAX_AGE_HOURS=6.0`.

---

## Methodology

R9 deployed 5 orthogonal adversarial agents in parallel:

1. **Time/clock attack surface** — tournament-window edge cases (UTC/local
   day boundary, KO scheduling math, stale thresholds at matchday cluster).
2. **Concurrency / pipeline orchestration** — fast+slow workflow collision,
   shared-JSON RMW, CI checkout semantics.
3. **Crash recovery / partial state** — mid-write SIGKILL, half-fetched
   provider responses, schema-drift accumulator integrity.
4. **Numerical / statistical stability** — Poisson tails, DC-τ boundary,
   tie-breaking, seed independence.
5. **R7+R8 integration regressions** — KeyError surfaces in R7 N1, NaN
   fig-leaf in R8 O2, stderr leak in R8 O1.

Two monitor agents then independently verified each HIGH-severity claim
end-to-end, reading source, reproducing numerically where feasible, and
distinguishing "real now" from "latent". Of the 23 distinct findings the
agents raised, the monitors **CONFIRMED** 7 (5 HIGH, 1 MEDIUM, 1 PARTIAL)
and rejected/dismissed 16 as duplicates, latent-only, or scoped to
follow-up data work.

Six implementation tasks landed in this commit. The R9 closure focuses
narrowly on the seven CONFIRMED findings with minimum-blast-radius fixes
plus pinned regression tests; data-side changes (sourcing FIFA's
official KO kickoff times into `data/raw/knockout_bracket_2026.json`)
are explicitly deferred to a separate atomic data PR with authoritative
source confirmation — code defenses are in place to surface the gap
loudly until then.

---

## Genuine findings landed in R9

### P1 — `lookup_third_place_assignment` raised opaque `KeyError` on partial annex_c corruption (HIGH)

**Where**: `scripts/03_simulate.py:359-381`

**Symptom**: R7 N1 added a friendly `RuntimeError` diagnostic
("annex_c miss + fallback exhausted: ... check `data/raw/annex_c_thirds_map.json`")
that ONLY fires when `lookup_third_place_assignment` returns `None` (full
table-key miss). A truncated annex_c file (or one written mid-crash) that
has the *outer* joined-group key (e.g. `"ABCDEFGH"`) but is missing one
or more of the inner `"3X"` slot entries would hit
`out[mapping[slot_key]] = q` and raise `KeyError('3H')` synchronously —
bypassing R7 N1's whole "fail-loud-but-fallback" promise. Operators saw
a bare KeyError traceback with no actionable hint.

**Fix**: Wrap the inner `mapping[slot_key]` in `mapping.get(...)`. Return
`None` on partial corruption — the R7 N1 fallback path then fires with
the full diagnostic message.

**Tests**: `tests/live/test_annex_c_lookup.py` — 5 cases
(happy path, full-key miss, last-slot omission, first-slot omission,
empty-inner-mapping). All green.

### P2 — `sample_score_with_noise` post-noise λ violated DC-τ boundary in ~33% of high-λ calls (HIGH)

**Where**: `scripts/03_simulate.py:217-239` and load-time assert at `:115`

**Symptom**: The module-load assert guards
`LAMBDA_CLIP_MAX × |DC_RHO| < 1.0` (= 7.0 × 0.13 = 0.91, 9% margin
before the critical τ-boundary 1/|ρ| = 7.69). But `sample_score_with_noise`
multiplies the already-clipped λ by `rng.gamma(α=12, 1/α)` which has an
*unbounded right tail*. Verified by simulation: for base λ=7.0 (top-tier
attack), **33.48%** of post-noise effective λ exceed 7.69. At those
breaches, `τ(0,1) = 1 + λ_a × ρ` goes negative (observed min ≈ −1.47);
`build_score_matrix` silently clips the negative cells via
`np.maximum(mat, 1e-12)` and renormalizes — so Σ stays 1 (Σ-gate didn't
catch it) but mat[0,1] / mat[1,0] systematically collapse to 1e-12 / Σ
for blowout-favorite matches. Low-score outcomes (0-1, 1-0) under-counted
for the most lopsided fixtures.

**Fix**: Re-apply `min(LAMBDA_CLIP_MAX, max(LAMBDA_CLIP_MIN, λ*noise))`
*after* the gamma multiplier. The module-load assert now meaningfully
guards the actual numerical contract; the bug rate drops from 33.48% to
0% (saturation at 7.0).

**Tests**: `tests/live/test_dc_tau_boundary.py` — 4 cases
(post-clip never exceeds CLIP_MAX, pre-fix breach rate measured
quantitatively, end-to-end smoke at λ=CLIP_MAX, static pin on the post-noise
clip identifiers). All green.

### P3 — R8 O2 fig-leaf: 7+ upstream producers wrote NaN/Infinity with `allow_nan=True` default (MEDIUM-HIGH)

**Where**: 11 boundary writers across `scripts/live/*.py` + `scripts/03_simulate.py`

**Symptom**: R8 O2 added `allow_nan=False` only to the matchday
intelligence aggregator at `apply_matchday_adjustments.py:101`. But CPython
silently round-trips Infinity/NaN through `json` by default — so the
7 upstream writers (`fetch_injuries.py`, `fetch_match_stats.py`,
`fetch_lineups.py`, `fetch_player_stats.py`, `fetch_weather.py`,
`referee_adjustments.py`, `suspension_tracker.py`) plus `fetch_results.py`,
`run_live_update.py`, `export_ko_advance.py`, the audit-log writer at
`apply_matchday_adjustments.py:1246`, and the final `03_simulate.py:1339`
predictions writer ALL accepted NaN at the producer boundary. A NaN
upstream poisoned the file on disk; the aggregator then crashed at R8 O2
ONLY after the upstream file was already corrupted — next tick re-read
the NaN and the crash repeated. R8 O2 was the *last* defense; with the
upstream writers unguarded, the first defense was missing.

**Fix**: Add `allow_nan=False` to all 12 boundary writers. Single-line
change per file. Clean ticks unaffected; non-finite floats now fail
loudly at the producer boundary, preventing on-disk poisoning.

**Tests**: `tests/live/test_producer_allow_nan_false.py` — 2 cases
(static pin sweeping all 12 producers; R8 O2 + R9 P3 dual-coverage on
the matchday writer). All green.

### P4 A1 — KO bracket lacks kickoff times; silent default to "20:00" local mis-aligns lineup window + weather forecast (HIGH→DEFENSIVE)

**Where**: `scripts/live/_knockout.py:71-141` and `scripts/live/fetch_weather.py:174-198`

**Symptom**: All 32 KO entries in `data/raw/knockout_bracket_2026.json`
have `time: None`. `_knockout.load_knockout_fixtures` and
`fetch_weather._kickoff_utc_dt` both default missing time to `"20:00"`
silently. For M73 (SoFi Stadium, Inglewood PT — actual kickoff
12:00 PT = 19:00 UTC per public FIFA schedule), the default produces
20:00 PT = 03:00 UTC *next day* — off by 8 hours. Downstream:
`fetch_lineups.fixtures_in_window` (4h pre-kickoff window) never includes
M73 during its actual pre-match window; `fetch_weather` requests
Open-Meteo for the wrong UTC day + hour, falling back to climate-bucket
for the entire KO phase.

**Fix (R9 scope — defensive only)**: Surface a *single* per-process
summary stderr warning at `_knockout.load_knockout_fixtures` naming the
affected match_nums, the consequence (pre-KO lineup-fetch window +
weather forecast hour silently shifted), the file to fix, and the
deadline (R32 kickoff 2026-06-28). Dedup via process-local set so a
10-min fast tick doesn't bloat logs. *The genuine fix is sourcing
FIFA's official KO kickoff times into `data/raw/knockout_bracket_2026.json`
— this defensive warning makes the silent gap visible to operators
until that data PR lands.*

**Tests**: `tests/live/test_knockout_time_default_warning.py` — 3 cases
(warning emitted with full message structure, dedup across repeated
calls, negative case where times are populated and no warning fires).
All green.

### P4 A2 — `fetch_results.py` silently dropped all R32→Final results because schedule loader never included KO (HIGH)

**Where**: `scripts/live/fetch_results.py:60` (import) + `:579-595`
(api-football adapter) + `:728-733` (football-data adapter)
+ `:660-680` (api-football result emission) + `:828-841`
(football-data result emission) + `:684-694`, `:851-862` (warnings)

**Symptom**: Both `fetch_apifootball` and `fetch_football_data` loaded
`schedule = cfg["group_stage_schedule"]` (72 rows only — m=1..72) and
built `schedule_by_id` from that alone. `data/live/provider_fixture_map.json`
likewise contains only m=1..72 entries (generated 2026-06-11, before KO
teams resolved). When an R32 fixture arrives from the provider, neither
the fixture_map nor the name+date fallback can map it; even if the
fixture_map were rebuilt with m=73..104 entries, `sched =
schedule_by_id.get(m_id)` at line 607 / 776 would still return None
because schedule_by_id had no KO entries. Net pre-R9: entire knockout
phase invisible to `fetch_results`; `results_2026.json.completed_matches`
freezes at 72; `predictions_live.json` never updates for any KO outcome;
dashboard locks at end-of-groups state through R32 → Final.

**Fix**: Import `load_knockout_fixtures` from `_knockout`. Extend
`schedule` to `cfg["group_stage_schedule"] + load_knockout_fixtures()`
in BOTH adapters. At the result-emission step, use the provider-
normalised team names (local `home`/`away`) for KO matches because
`sched["home"]` / `sched["away"]` carry bracket slot codes (`"1A"`,
`"W74"`) until results resolve them. Add a CRITICAL stderr warning when
any unmapped fixture's date falls in the KO window (`>= 2026-06-28`),
naming the remediation step (rebuild provider_fixture_map.json) — so
operators have an actionable signal instead of the routine info-level
"unmapped (likely friendlies)" message.

**Tests**: `tests/live/test_fetch_results_knockout.py` — 5 cases
(KO bracket loader surfaces all 32 entries; import pinned; both
adapters call the KO loader; KO-window unmapped warning pinned in both
adapters; KO output uses provider names not slot codes). All green.

### P5 B1 — `_check_freshness` used `stat().st_mtime` which `actions/checkout` flattens, making the freshness guard a no-op in CI (HIGH)

**Where**: `scripts/live/apply_matchday_adjustments.py:135-217` (helper +
`_check_freshness`) and `:288-302` (`get_matchday_freshness_warnings`)

**Symptom**: Both freshness paths compared filesystem mtimes
(`input_path.stat().st_mtime` vs `reference_path.stat().st_mtime`).
`actions/checkout@v6` resets every checked-out file's mtime to checkout
time (within microseconds of each other), so `age_delta_seconds ≈ 0`
ALWAYS in CI — passing the 6h threshold regardless of the data's actual
age. A subsystem could be stale for *days* without firing
`subsystem_stale` / `matchday_consolidated_stale` /
`matchday_subsystem_stale`. The entire Wave-2 S1 freshness defense
was bypassed on the actual production runner.

**Fix**: New helper `_freshness_timestamp_seconds(path) → (epoch, source)`
that reads `generated_at` / `updated_at` / `last_updated_utc` /
`last_updated` from the JSON content as the primary source, falling
back to mtime only if the file lacks a content timestamp. Both
`_check_freshness` and `get_matchday_freshness_warnings` use the new
helper. Producer outputs already write `generated_at`; results files
write `updated_at`; the helper handles both. Local dev / replays still
work because they carry honest content timestamps.

**Tests**: `tests/live/test_freshness_content_timestamp.py` — 6 cases
(helper prefers content over mtime; mtime fallback when no content ts;
`updated_at` for results files; Z-suffixed ISO parsing; end-to-end CI
scenario with flat mtime and stale content; negative case for fresh
content). All green. Existing tests in `test_fast_path_freshness.py`
updated to drive both mtime AND content timestamp in sync (otherwise
the content-preferring helper would ignore the mtime manipulation).

---

## Findings reviewed and dismissed as not-genuine for R9

The orthogonal sweep produced 23 candidate findings; 16 were rejected
or deferred after monitor verification. Recording these here so future
rounds don't re-audit the same surface:

* **A3 / Suspension freshness threshold 6h is too tolerant** — MEDIUM. Per-stage thresholds during the R32 cluster days
  was raised. Confirmed structurally but threshold tuning is a config
  PR with audit-time experimental validation; doesn't belong in a
  bug-fix round.
* **A4 / Circuit breaker no time-based reset** — MEDIUM. A transient
  late-Saturday sim failure could keep CB tripped through the entire
  KO phase. Confirmed structurally but the fix touches the CB
  semantics (TTL + auto-retry + notification) which needs design, not
  patch. Deferred to a CB redesign PR.
* **A5 / Freshness reference clock false-positive during stage gaps** —
  DEFENSIVE. Resolved at scope of P5 B1 (content-timestamp removes
  most of the mtime-side false-positives anyway).
* **B2 / Slow workflow rewrites `results_2026.json` but never commits
  it (wasted API calls)** — LOW. Real but tiny budget impact; not a
  correctness issue.
* **B3 / `git pull --rebase --autostash || echo` continues past halt** —
  LOW (latent). Currently safe due to disjoint commit allow-lists
  between fast and slow workflows; becomes live the moment any
  overlap is introduced. Deferred as a CI hardening PR.
* **B4 / `run_capture` stderr leaks subprocess env via published
  warning** — LOW (latent). Currently no code path produces traceback
  with env values, but the contract is foot-gun-friendly. Adding a
  regex-scrub layer is the proper fix; out of scope for R9.
* **C3 / Orphaned `.tmp` files accumulate** — MEDIUM. GHA ephemeral
  runners self-clean (`fetch-depth: 1`) so production is safe.
  Future CF Workers persistent migration would need cleanup but that
  belongs in the migration PR.
* **C5 / `write_live_state` deploy-churn guard drops crash warnings** —
  MEDIUM. Construct-test isolation issue; the path is reachable only
  under exact-duplicate warning payloads. Investigated; the guard
  intentionally drops identical payloads — a separate "force update
  on warnings change" detector belongs in a UX iteration.
* **C6 / `validate_results_file` 16-byte check too low** — MEDIUM.
  The validator was hardened for 64-byte minimum in R5; the 16-byte
  threshold remains as a fast trivial-rejection layer.
* **C7 / `write_live_state` `existing.get` not guarded against non-
  dict shape** — MEDIUM. Latent — would require manual corruption or
  future schema migration writing `[]`. Defense-in-depth gap that's
  out of pressure-test scope (it's a re-shape-defense, not an active
  bug).
* **D2 / Σ-gate covers only `predictions_live.json`** — MEDIUM.
  Defense-in-depth gap; pre-tournament `predictions.json` could drift
  silently. Pre-flight asserts annex_c_misses == 0 there; full Σ-gate
  extension is a small-PR follow-up.
* **D3 / Group standings lack random tiebreak** — PARTIAL (MEDIUM
  structurally, LOW materially). Confirmed deterministic; FIFA Reg
  19.6 random tiebreak would fire only if ALL prior tiebreakers tie
  including FIFA ranking. Probability over a 25k-sim run is sub-1%
  per affected group; deferred to a small atomic PR after baseline
  empirical measurement.
* **D4 / `fifa_points.get(default=0)` silently demotes missing teams** —
  LOW. Currently all 48 teams have FIFA ranking; latent guard against
  future config swap typo. Defer to defense-in-depth PR.
* **E2 / R7 N1 fallback can double-assign third-placers via greedy
  iteration** — MEDIUM. Verified — the greedy fill in slot_pools
  iteration can fail for pathological draws. But the current `len(
  third_slot_map) < 8` check at `:475` catches and raises with full
  diagnostic, so this is observable (loud crash, not silent corruption).
  Mitigated by R7 N1 already; a separate "smarter fallback than
  greedy" PR is a model-improvement project.
* **F2 / R7 N2/N3 dedup not race-safe** — LOW. Requires concurrent
  invocation (manual operator triage during automated tick). Real but
  scope creep — file locking around the dedup RMW is its own design
  decision (advisory vs mandatory lock semantics).
* **G2 / R8 O1 stderr could leak via dashboard publish** — Resolved
  conceptually via Finding B4 — same root cause, same deferred
  redaction-layer fix.

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
1110 → 1135 passed (+25 R9 regression tests), 1 skipped, 0 failed
Σ-gate exit 0 on data/processed/predictions_live.json
  |Δ| = 0.000e+00, tol = 1e-06, teams = 48
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
| R9    | **5**| 1      | 1 |

R9 surfaced more HIGH than any round since R3 — the orthogonal-sweep
approach with monitor verification continues to find genuine pre-tournament
risk even at this depth. Five of the seven landed fixes (P1, P2, P3, P4 A2,
P5 B1) close concrete production-blocking failure modes for the R32 window;
P4 A1 is a defensive surfacing layer for a data gap that needs a separate
authoritative-source PR.
