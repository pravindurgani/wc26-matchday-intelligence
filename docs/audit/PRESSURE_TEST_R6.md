# WC26 Matchday Intelligence — Pressure-Test Round 6 (deep adversarial sweep, post-R5)

**Date**: 2026-06-18
**Branch**: `hardening/r32-pressure-test-r2` (local-only; push human-gated)
**Suite delta**: 1090 → **1099 passed**, 1 skipped, 0 failed
**Σ-gate**: exit 0 on `data/processed/predictions_live.json` (|Δ| = 0, teams = 48, tol = 1e-6)
**Standing constraints preserved**: no retrains, no threshold/cap changes,
`AUTO_TIER_ACTIVE=False`, `NB_ALPHA=5.0`, `DC_RHO=-0.13`, `MAX_G=10`,
`STALENESS_MAX_AGE_HOURS=6.0`.

## Context — why Round 6

R5 closed three findings (C6 fast-path events, C1 silent preserve, C4
record-degradation rollup). The user ordered another adversarial sweep
on top of R5 (commit `6def9ff`). Five orthogonal agents probed
dimensions not deeply covered in R3/R4/R5: frontend rendering,
configuration/env vars, time/timezone, determinism, R5 integration.

A sixth **monitor agent** verified the consolidated cross-agent claims
with file:line evidence. A seventh **independent verifier** re-read the
implementations after the fixes landed and flagged a real gap in the
crash-handler path.

## Triage outcome

| Agent | Reported | Verified after monitor + verifier | Action |
|-------|----------|------------------------------------|--------|
| A1 (frontend) | Mostly GREEN, 12 dimensions clean | All clean (warning surfacing, XSS, staleness thresholds, data-switch guards, cache strategy verified solid) | None |
| A2 (config) | "CRITICAL × 2 + HIGH × 4" | 1 genuine MEDIUM (M2 — silent provider-key fallback); 1 dismissed (M1 — dual-key alias by design); 4 HIGH theoretical (constants duplication = pinned by 1e-9 agreement test; AUTO_TIER_ACTIVE = test-gated; STALENESS magic = well-documented; league_id = config-coupled) | **M2** |
| A3 (time/tz) | "MEDIUM × 1" | All verified safe (no DST bite in June-July tournament window; monotonic clocks; ISO 8601 consistent; no naive datetimes) | None |
| A4 (determinism) | "MEDIUM × 2" | Both advisory (local PYTHONHASHSEED is set on CI; json sort_keys is stylistic) — no regression risk | None |
| A5 (R5 integration) | "MEDIUM × 1" | 1 genuine MEDIUM (M3 — R5 C1 introduced unbounded warning growth) | **M3** |

**Two genuine MEDIUM fixes landed after monitor verification: M2, M3.**
**One follow-up landed after the independent verifier flagged the crash-handler gap.**

## M2 — silent provider-key fallback (MEDIUM)

### Root cause

`scripts/live/run_live_update.py:detect_provider_source` (lines 271-292)
silently returns `("manual/mock", "manual")` when the operator sets
`FOOTBALL_PROVIDER=api_football` (or any real provider) but the
corresponding key is unset/empty. The dashboard reads
`source="manual/mock"` and `provider_mode="manual"`, but no warning
explains WHY the fallback fired. An operator investigating a rotated
secret, deleted env var, or workflow misconfiguration sees no signal
on `live_state.json:warnings[]`.

### Fix

New helper `_provider_fallback_warnings()` at
`scripts/live/run_live_update.py:295-355` emits a structured
`provider_key_missing` warning with `requested_provider` +
`missing_env_var` fields. Coverage:

- All three real providers (api_football, football_data, sportmonks)
- Legacy alias env vars (WC_APIFOOTBALL_KEY, WC_FOOTBALL_DATA_TOKEN, WC_SPORTMONKS_TOKEN)
- Returns `[]` for explicit mock / unset FOOTBALL_PROVIDER (no spurious warning)
- Exception-safe: internal failure → returns `provider_fallback_check_error` warning, never raises

The helper is folded into the `mf_warnings` probe at
`run_live_update.py:434-447` so EVERY write_live_state path (circuit
breaker, fetch failure, input corruption, normal happy path) carries
the signal without per-site changes.

After the independent verifier flagged the gap, the helper is also
folded into the crash handler at `run_live_update.py:730-748` so an
`orchestrator_crash` + missing-key combo surfaces BOTH signals (the
crash hints WHAT failed; the provider-fallback hints WHY).

### Verification

Seven new tests in `tests/live/test_fast_path_freshness.py`:

- `test_r6_m2_provider_fallback_helper_exists` — helper + warning type present
- `test_r6_m2_provider_fallback_merged_into_mf_warnings` — fold into orchestrator state
- `test_r6_m2_fallback_warning_emitted_when_api_football_key_missing` — functional positive
- `test_r6_m2_no_warning_when_provider_mock` — negative case (explicit mock)
- `test_r6_m2_no_warning_when_key_present` — negative case (happy path)
- `test_r6_m2_legacy_alias_satisfies_key_check` — `WC_APIFOOTBALL_KEY` alias respected
- `test_r6_m2_sportmonks_provider_missing_key` — multi-provider coverage
- `test_r6_m2_crash_handler_carries_provider_fallback_warning` — crash-handler pin

## M3 — R5 C1 unbounded warning growth (MEDIUM)

### Root cause

R5 C1 added `existing.setdefault("warnings", []).append({...})` to the
preservation branch at `scripts/live/fetch_results.py:996-1004`. The
append was unconditional — every tick that hit the silent-empty path
(provider returned HTTP 200 + empty body + no parser warnings) added
a duplicate `provider_returned_nothing` entry. A 3h sustained provider
outage = ~18 fast ticks = 18 duplicate entries appended to
`results_2026.json:warnings[]`. Over a full matchday with a degraded
provider, the warnings array could balloon to 100+ entries; the file
grows unboundedly; git diffs and dashboard payload bloat.

### Fix

`scripts/live/fetch_results.py:985-1032` now uses a dedup-by-type
pattern:

```python
existing_warning = next(
    (w for w in warnings if isinstance(w, dict)
     and w.get("type") == "provider_returned_nothing"),
    None,
)
if existing_warning is not None:
    existing_warning["count"] = int(existing_warning.get("count", 1)) + 1
    existing_warning["last_seen_utc"] = now_iso
else:
    warnings.append({
        "type": "provider_returned_nothing",
        "message": ...,
        "count": 1,
        "first_seen_utc": now_iso,
        "last_seen_utc": now_iso,
    })
```

First silent-empty tick: append a fresh entry with `count=1` and
`first_seen_utc`. Subsequent ticks: bump `count` and refresh
`last_seen_utc`. Single warning entry per outage carries duration +
occurrence count instead of N duplicate entries.

### Verification

`test_r6_m3_provider_returned_nothing_dedup_pinned_in_source` pins
both the type-keyed `next()` lookup pattern and the presence of
`count` + `last_seen_utc` fields. A revert to unconditional append
breaks the test loudly.

R5 C1 pin (`test_r5_c1_provider_returned_nothing_warning_pinned_in_source`)
still passes — the `"provider_returned_nothing"` literal and
`atomic_write_json(out_path, existing)` calls remain in the branch.

## False positives / dismissed claims

The monitor + verifier independently re-read each citation:

- **A2 M1 dual-key collision** — DISMISSED. `API_FOOTBALL_KEY` vs `WC_APIFOOTBALL_KEY` is an alias-by-design; the workflow sets both to the SAME secret value (`live-matchday.yml:201-202`), so production cannot hit a collision. Documented in workflow header.
- **A2 constants duplication** — `nb_dispersion=5.0` / `NB_ALPHA=5.0` etc. duplicated across `03_simulate.py` and `export_ko_advance.py` is pinned by the `test_ko_advance_agreement` test at 1e-9 tolerance; drift is caught instantly.
- **A2 AUTO_TIER_ACTIVE not code-gated** — test-gated via `test_pipeline_e2e.py:261` + `test_knockout_readiness.py:658`; force-pushes bypassing CI is a process concern, not a code defect.
- **A2 STALENESS_MAX_AGE_HOURS magic** — well-documented inline at `apply_matchday_adjustments.py:125` with explicit "2× slow-cron interval" coupling note.
- **A3 tournament-end boundary** — at 2026-07-20T00:00:00Z exactly, the gate ALLOWS execution, but the cron schedule doesn't fire that day; manual dispatches bypass the gate by design. Not a regression vector.
- **A4 PYTHONHASHSEED local-pytest** — set in CI; local devs need to set manually OR accept set-iteration non-determinism. Advisory, not code change.
- **A4 json.dump sort_keys** — stylistic; Σ-gate at 1e-6 absorbs any reordering effects; insertion order is deterministic in Py3.7+.
- **A5 C6 race on results_2026.json** — last-write-wins by design; mitigated by `git diff --quiet --cached` guard and the rebase logic on push.

## Verdict

**Status**: GREEN — operational pressure test passes; two real fixes + one follow-up landed.

**Tests**: 1090 → **1099 passed** (+9 R6-specific), 1 skipped, 0 failed.
**Σ-gate**: exit 0 on `data/processed/predictions_live.json`.
**Push**: NOT pushed; remains on `hardening/r32-pressure-test-r2`
local-only per instruction.

**Cumulative pressure-test arc**: R1+R2 baseline (1059) → R3 (+27, four
HIGH closed) → R4 (one HIGH closed, ten false positives filtered) → R5
(one HIGH + two MEDIUMs closed) → R6 (two MEDIUMs + one follow-up
closed, ~10 false positives filtered, security audit re-clean,
frontend audit clean).

After 4 rounds of adversarial pressure testing, the genuine-finding rate
has tapered: R3 produced 4 HIGHs, R4 produced 1 HIGH, R5 produced 1 HIGH
+ 2 MEDIUMs, R6 produced 0 HIGHs + 2 MEDIUMs. The repo is in solid shape
for R32 kickoff on 2026-06-28 (T-10 days).
