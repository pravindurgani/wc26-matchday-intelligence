# WC26 Matchday Intelligence — Pressure-Test Round 5 (deep adversarial sweep, post-R4)

**Date**: 2026-06-18
**Branch**: `hardening/r32-pressure-test-r2` (local-only; push human-gated)
**Suite delta**: 1086 → **1090 passed**, 1 skipped, 0 failed
**Σ-gate**: exit 0 on `data/processed/predictions_live.json` (|Δ| = 0, teams = 48, tol = 1e-6)
**Standing constraints preserved**: no retrains, no threshold/cap changes,
`AUTO_TIER_ACTIVE=False`, `NB_ALPHA=5.0`, `DC_RHO=-0.13`, `MAX_G=10`,
`STALENESS_MAX_AGE_HOURS=6.0`.

## Context — why Round 5

R3 closed four HIGH audit findings. R4 closed one HIGH (slow-workflow
push observability) and filtered ten reported-but-not-genuine findings.
The user ordered a fresh adversarial sweep on top of R4 (commit
`21756aa`), explicitly: deploy many agents on **orthogonal** dimensions
not covered in R3/R4, deploy a monitor agent to verify, only commit
findings that survive both monitor verification and full-suite +
Σ-gate re-runs.

Five parallel adversarial Explore agents covered R5 dimensions:

1. **Concurrency + cron race conditions** (fast vs slow workflow, autostash, push races)
2. **Data integrity + idempotency + replay safety** (provider shrinkage, atomic writes, schema drift)
3. **Security + injection + secrets** (workflow secret handling, XSS, subprocess safety)
4. **Observability + alerting + silent failures** (warning surfacing, Σ-gate scope, Vercel deploy)
5. **Cross-file consistency + state synchronization** (predictions vs predictions_live, knock_lambdas_table, fixture map)

A sixth **monitor agent** independently verified the orchestrator's
filtering decisions on the consolidated cross-agent claims (file:line in
hand). A seventh **independent verifier** re-read the implementations
end-to-end after the fixes landed.

## Triage outcome

| Agent | Reported severity | Verified severity after monitor | Action |
|-------|-------------------|--------------------------------|--------|
| A1 (concurrency) | "HIGH × 1 + MEDIUM × 3 + LOW × 2" | All theoretical / GHA-ephemeral-VM non-issues / YAGNI retry loops | None |
| A2 (data integrity) | "CRITICAL × 1 + HIGH × 5 + MEDIUM × 3" | 1 genuine MEDIUM (C1); 1 false positive (C2 — atomic write makes "missing key" impossible) | **C1** |
| A3 (security) | All clean | All clean | None |
| A4 (observability) | "CRITICAL × 1 + HIGH × 3 + MEDIUM × 3" | 1 false positive (C5 — 09_validate DOES run Σ-gate); 1 genuine MEDIUM (C4); others by-design | **C4** |
| A5 (cross-file consistency) | "CRITICAL × 1 + HIGH × 4 + MEDIUM × 2" | 1 genuine HIGH (C6); others false positives (C8 — results keyed by stable m, not slot codes) or by-design (C7) | **C6** |

**Three genuine fixes landed after monitor verification: C1, C4, C6.**

## C6 — fast-path fetch_results missing `--with-events` (HIGH)

### Root cause

The fast (10-min) workflow's `run_live_update.py:447-451` built its
`fetch_cmd` as `[sys.executable, "scripts/live/fetch_results.py"]` —
no `--with-events`. By contrast `matchday-intel-slow.yml:115` explicitly
passes `--with-events`. The events enrichment block at
`fetch_results.py:966-980` is gated on `if args.with_events`.

Consequence: during a fast tick, a newly-locked match writes to
`results_2026.json:completed_matches[]` with score + status but NO
`events[]` array. Cards from that match are not available to
`suspension_tracker.py` until the next slow tick (up to 3h later). A
player who picks up his 2nd yellow during a fast-tick-locked match is
NOT in `suspensions_2026.json` for the next R32 opponent's win-prob
calculation. R32 starts **2026-06-28**; the gap directly impacts the
first KO round.

### Fix

`scripts/live/run_live_update.py:447-458` now includes `--with-events`
in the fetch_cmd literal:

```python
fetch_cmd = [sys.executable, "scripts/live/fetch_results.py", "--with-events"]
if args.dry_run:
    fetch_cmd.append("--dry-run")
```

Cost is bounded: `enrich_matches_with_events` skips matches that
already have events (via `existing_events_by_m` cache), so worst case
≈ 1–2 extra `/fixtures/events` calls per fast tick (one per new FT
match). Negligible against the 7,500 req/day Pro-plan budget.

### Verification

`tests/live/test_fast_path_freshness.py::test_r5_c6_fast_path_fetch_results_uses_with_events`
pins the contract via a static-source assertion against
`run_live_update.py`.

## C1 — silent provider zero-match preservation (MEDIUM)

### Root cause

`scripts/live/fetch_results.py:985-991` (pre-R5) entered the
preservation branch on `if not valid and not warnings_list and
out_path.exists()`, printed to stdout, and `return 0`. **No structured
warning was added** to `existing["warnings"]`, and the file was NOT
re-written. The orchestrator's `get_results_warnings()` returns `[]`,
and `live_state.json` carries no signal.

Most provider error paths DO produce warnings (HTTP-5xx, parser
exceptions). The silent-empty case (HTTP 200 + empty body, no
exception — e.g. an auth token silently expired, or an API serving
cached empty response) escapes both the parser-warning path AND the
file-mtime-based freshness guard (mtime didn't update because the
file wasn't written).

### Fix

`scripts/live/fetch_results.py:985-1010` now writes an explicit
warning into the preserved file and refreshes mtime:

```python
existing.setdefault("warnings", []).append({
    "type": "provider_returned_nothing",
    "message": f"Provider '{src}' returned 0 matches with no warnings; "
               f"existing locked matches preserved. Investigate the "
               f"adapter / provider token if this persists across ticks.",
})
existing["updated_at"] = datetime.now(timezone.utc).isoformat()
existing["source"] = src
atomic_write_json(out_path, existing)
return 0
```

### Verification

`tests/live/test_fast_path_freshness.py::test_r5_c1_provider_returned_nothing_warning_pinned_in_source`
asserts both the `"provider_returned_nothing"` literal and the
`atomic_write_json` call within the preservation branch body. A revert
to print-only or a forgotten persist call breaks the test loudly.

## C4 — per-record degradation rollup not surfaced (MEDIUM)

### Root cause

`scripts/live/apply_matchday_adjustments.py:308-330` (pre-R5)
filtered `degradation_warnings` only for `scope=="freshness"` or
`exception_class=="Stale"`. Per-record degradations
(`scope=="record"` — e.g. one NaN xG record, one malformed injury blob,
one ill-formed referee entry — caught and skipped by `_degrade.py:75-94`
to keep the tick alive) stayed embedded in
`dashboard/matchday_intelligence.json` with no signal to
`live_state.json`. A sustained stream of per-record failures (e.g.
provider schema drift breaking N parses every tick) was invisible to
the operator until someone manually inspected the consolidated file.

### Fix

`scripts/live/apply_matchday_adjustments.py:294-359` now scans for
`scope=="record"` entries, aggregates by subsystem, and emits a single
rollup warning so the dashboard surfaces the data-quality drop without
spamming `live_state.json` with one entry per record:

```python
out.append({
    "type": "matchday_record_degradation",
    "message": f"Per-record degradations: {total} records skipped across "
               f"subsystems ({breakdown}). ...",
    "count": total,
    "by_subsystem": record_degradations,
})
```

The pre-existing `matchday_subsystem_stale` warning (for subsystem-wide
collapse / freshness) remains unchanged — `scope=="record"` is handled
by an `elif` so a single warning cannot be double-counted.

### Verification

Two new tests:
- `test_r5_c4_per_record_degradation_rollup_emitted`: feeds 3 record-scope entries (2 injury + 1 referee); asserts `count==3`, `by_subsystem == {"injury": 2, "referee": 1}`, and crucially that `matchday_subsystem_stale` does NOT also fire (no false-positive on the unchanged path).
- `test_r5_c4_zero_record_degradations_no_rollup`: empty `degradation_warnings`, asserts rollup absent.

Pre-existing `test_subsystem_non_freshness_warnings_do_not_propagate`
still passes — it asserts only the absence of `matchday_subsystem_stale`,
not the absence of the new rollup. No regression.

## False positives — claims that survived no verification

The monitor agent independently re-read each citation:

- **A1 (concurrency)**: All 6 findings dismissed. Tempfile leaks on GHA = no real impact (ephemeral VMs); push-retry loops = YAGNI (next-tick recovery is documented and deterministic); cross-process rate-budget blindness = acceptable given low overlap probability.
- **A2 C2 (atomic-write breaks `knock_lambdas_table`)**: Disputed. `scripts/03_simulate.py:1276-1297` writes the key INSIDE the atomic block; the file either has the key or doesn't exist post-`os.replace`. The claimed "missing key" scenario is impossible.
- **A2 C5–C9**: Theoretical concerns or by-design choices (schema-watchdog soft-mode is intentional; duplicate-key collision is a provider-side concern; etc.).
- **A3 (security)**: Zero findings, audit clean across 12 dimensions. No action.
- **A4 C5 (Σ-gate scope on main sim)**: Disputed. `scripts/09_validate.py:102-119` runs `check_invariants` against the canonical post-sim file as Step 8 of the orchestrator; the agent missed the import wiring.
- **A4 Vercel + producer-failure delay claims**: Acceptable by design — Vercel webhook integration is a new feature, not a fix; producer failures degrade gracefully and surface within 6h via the freshness guard.
- **A5 C7–C8**: Disputed. Σ-gate IS run pre-publish via the export_ko_advance pipeline; results_2026.json is keyed by stable `match_num` (m=1..104), not by mutable slot codes, so the fixture-map rebuild has no re-keying problem.

## Verdict

**Status**: GREEN — operational pressure test passes; three real fixes landed.

**Tests**: 1086 → **1090 passed** (+4 R5-specific), 1 skipped, 0 failed.
**Σ-gate**: exit 0 on `data/processed/predictions_live.json`.
**Push**: NOT pushed; remains on `hardening/r32-pressure-test-r2`
local-only per instruction.

**Cumulative pressure-test arc**: R1+R2 baseline (1059) → R3 (+27, four
HIGH closed) → R4 (one HIGH closed, ten false positives filtered) → R5
(one HIGH + two MEDIUMs closed, ~10 false positives filtered, security
audit clean).
