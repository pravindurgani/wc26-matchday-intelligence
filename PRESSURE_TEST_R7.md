# WC26 Matchday Intelligence — Pressure-Test Round 7 (deep adversarial sweep, post-R6)

**Date**: 2026-06-18
**Branch**: `hardening/r32-pressure-test-r2` (local-only; push human-gated)
**Suite delta**: 1099 → **1105 passed**, 1 skipped, 0 failed
**Σ-gate**: exit 0 on `data/processed/predictions_live.json` (|Δ| = 0, teams = 48, tol = 1e-6)
**Standing constraints preserved**: no retrains, no threshold/cap changes,
`AUTO_TIER_ACTIVE=False`, `NB_ALPHA=5.0`, `DC_RHO=-0.13`, `MAX_G=10`,
`STALENESS_MAX_AGE_HOURS=6.0`.

## Context — why Round 7

R6 closed two MEDIUMs (M2 silent provider-key fallback, M3 unbounded
`provider_returned_nothing` growth) plus one follow-up flagged by the
independent verifier (M2 crash-handler coverage). The user ordered
another adversarial sweep on top of R6. Five orthogonal agents probed
dimensions that the R6 sweep did not bottom-out:

| Agent | Probe dimension |
|-------|-----------------|
| A1 | R6 integration regressions (does the M2 fold land on all 4 write_live_state paths? does the M3 dedup hold under back-to-back tick fixtures?) |
| A2 | Numerical edge cases in the simulator's third-place assignment + knockout fixture build |
| A3 | Failure composition (what happens when two warning-emitting paths fire on the same tick?) |
| A4 | Test-suite coverage gaps (where is a static-pin masquerading as a functional test?) |
| A5 | Branch hygiene (orphan files, unused imports, untracked artefacts, CI workflow drift) |

A sixth **monitor agent** triaged the consolidated cross-agent claims
with file:line evidence and a verdict for each.

## Triage outcome

| Agent | Reported | Verified after monitor | Action |
|-------|----------|------------------------|--------|
| A1 (R6 integration) | "HIGH × 1" (M3 functional gap) + 2 MEDIUM | 1 genuine LOW (N2 — only static pin, no functional drive) + 2 MEDIUM dismissed (M2 fold verified across all 4 paths in test_r6_m2_*; M3 dedup verified across atomic_write_json paths) | **N2** |
| A2 (numerical) | "CRITICAL × 1" (KeyError on annex_c miss) + 2 HIGH | 1 genuine DEFENSIVE (N1 — fallback can leave third_slot_map < 8, causing `knock_matrices[(None, None)]` ~25 lines later); 2 HIGH dismissed (knock_matrices keying is symmetric by construction; FIFA-rank tie-break is deterministic for tournament-set inputs) | **N1** |
| A3 (failure composition) | "MEDIUM × 2" | Both verified safe: `provider_returned_nothing` + `matchday_consolidated_stale` co-occurring correctly produces 2 distinct warnings on live_state.json (verified at `run_live_update.py:434-447` fold); no warning suppression on co-occurrence | None |
| A4 (test suite) | "HIGH × 1" (M3 static-only) + 3 MEDIUM | 1 confirmed (N2 — overlaps A1); 1 genuine LOW (N3 — bump path doesn't backfill `first_seen_utc` on legacy entries written pre-R6 deploy); 2 MEDIUM dismissed (test_r6_m2_crash_handler_carries_provider_fallback_warning IS a functional test; AUTO_TIER static pin pattern is by-design) | **N3** |
| A5 (branch hygiene) | "MEDIUM × 4" | All 4 verified non-issues: untracked artefacts (live_state.json, predictions_live.json) are tick outputs not source; CORRECTIONS.md / reports/ are intentional; auto_tier.py + suspension_tracker.py shadow-mode by-design; no orphan tests | None |

**Three defensive improvements landed after monitor verification: N1, N2, N3.**
All three close real-but-LOW gaps that surfaced only under adversarial
hypothesis — they would not bite in normal operation, but would each
produce an opaque or misleading symptom in a specific edge-case
scenario. Closing them removes 3 sources of operational surprise during R32.

## N1 — third-place fallback diagnostic assertion (DEFENSIVE)

### Root cause

`scripts/03_simulate.py:447-459` does an Annex-C lookup for the
qualifying third-place teams; on lookup miss (any new edition / annex
mismatch), the code falls back to a FIFA-ranking-by-eligible-slot loop.
The fallback loop is *not guaranteed to fill all 8 slots* — if
`slot_pools` has a misconfigured pool that contains zero eligible
groups for one slot, OR the eligibility constraint blocks the
remaining unused thirds from any remaining slot, the loop simply
exits with `third_slot_map` short of 8 entries.

The downstream `r32_fixtures` build at lines 461-475 does
`t_a = third_slot_map.get(f"M{s['m']}", {}).get("name")` — which silently
returns `None` for any unfilled slot. Twenty-five lines later at
line 485, `knock_matrices[(ta, tb)]` raises a bare `KeyError: (None,
None)` deep inside the Monte-Carlo loop, with no indication of where
the `None` came from.

The probability of this firing in production is low — the Annex-C
table is the official FIFA mapping for all 495 (12 choose 8) qualifying
combinations — but the failure mode would be a sim crash with no
diagnostic path back to the config layer that caused it.

### Fix

`scripts/03_simulate.py:460-470` adds a post-fallback diagnostic check:

```python
if len(third_slot_map) < 8:
    raise RuntimeError(
        f"annex_c miss + fallback exhausted: only assigned "
        f"{sorted(third_slot_map)} ({len(third_slot_map)}/8 slots); "
        f"unused thirds={[q['name'] for q in unused]}; "
        f"check data/raw/annex_c_thirds_map.json and slot_pools config"
    )
```

Instead of crashing 25 lines later with `KeyError: (None, None)`, the
sim now fails fast at the assignment site with: the partial assignment
(which slots got which thirds), which thirds were left unassigned, and
exactly which two config files to inspect (Annex C JSON + the
`slot_pools` block in `wc2026_config.json`).

### Verification

The full pytest suite (1105 passed) exercises the normal happy path —
`third_slot_map` always reaches 8 entries via Annex-C, so the new
guard is never triggered in the green path. The check is a fail-loud
on a precondition that ONLY data corruption / config drift can violate;
covered indirectly by the existing `test_pipeline_e2e.py` which would
catch any regression that broke the assignment.

## N2 — end-to-end functional test for R6 M3 dedup (LOW gap closure)

### Root cause

R6 M3 added a dedup-by-type pattern at `scripts/live/fetch_results.py:985-1032`.
The R6 test
(`test_r6_m3_provider_returned_nothing_dedup_pinned_in_source`) is a
**static pin only** — it greps the source for literal patterns
(`w.get("type") == "provider_returned_nothing"`, `count`,
`last_seen_utc`). It does NOT drive the code path with two consecutive
ticks and assert that warnings[] holds at 1 entry with count climbing.

A subtle refactor that kept the literals but broke the bump logic
(e.g. accidentally creating a new dict on every tick because of a
mis-scoped variable, or appending instead of bumping under a specific
branch) would slip past the static pin.

### Fix

`tests/live/test_fast_path_freshness.py::test_r6_m3_dedup_two_ticks_bumps_count_not_appends`
(~60 lines) drives `fetch_results.main()` twice with a tmp_path-scoped
filesystem and a monkeypatched `fetch_mock` returning `[]`:

- Pre-seed `out_path` with a locked match so the preservation branch fires
- Monkeypatch `LIVE` → `tmp_path/live`, `fetch_mock` → `lambda: []`,
  `sys.argv` → `["fetch_results.py", "--provider", "mock"]`
- Run `main()`; assert `warnings[] == [one entry]` with `count=1`,
  `first_seen_utc` set, `last_seen_utc` set, `completed_matches`
  preserved
- Run `main()` again; assert `warnings[] == [one entry]` with `count=2`,
  `first_seen_utc` UNCHANGED (preservation invariant),
  `last_seen_utc` ≥ previous (monotonic), `completed_matches` still
  preserved

A revert to unconditional append now breaks BOTH the static pin AND
the functional pin — defense in depth on the same defect class.

## N3 — first_seen_utc backfill on legacy entries (LOW defensive)

### Root cause

The R6 M3 bump path at `scripts/live/fetch_results.py:1011-1013` does:

```python
existing_warning["count"] = int(existing_warning.get("count", 1)) + 1
existing_warning["last_seen_utc"] = now_iso
```

It tolerates a missing `count` (defaults to 1) but NOT a missing
`first_seen_utc`. In production this matters only in one scenario: an
outage that BEGAN before the R6 deployment lands, gets caught by the
post-deploy fast tick. The pre-R6 warning entry on disk has no
`first_seen_utc` field (the old code didn't write one); the bump path
adds `count=2` and a `last_seen_utc` but leaves `first_seen_utc`
absent. The dashboard's "outage started at..." display then either
shows nothing or shows a `KeyError` on the field lookup.

Deployment-window exposure is ~23 minutes (one fast cycle + GHA
workflow propagation), so the actual probability of hitting this is
low, but the fix is a single-line setdefault.

### Fix

`scripts/live/fetch_results.py:1014-1019` adds:

```python
# R7 N3: backfill first_seen_utc on any pre-R6 warning entry
# that was written before the dedup fields existed. ...
existing_warning.setdefault("first_seen_utc", now_iso)
```

On a fresh post-R6 entry, the field is already present, so `setdefault`
is a no-op. On a legacy pre-R6 entry, the field is backfilled to the
current tick's timestamp (the best approximation available — the true
onset is lost to history).

### Verification

`tests/live/test_fast_path_freshness.py::test_r7_n3_first_seen_utc_backfilled_on_legacy_warning`
seeds an `out_path` with a legacy-shape warning entry (no `count`, no
`first_seen_utc`, no `last_seen_utc`), drives the preservation path
once, and asserts that the bumped entry has:

- `count == 2` (legacy entry assumed `count=1`)
- `last_seen_utc` set (didn't exist on legacy entry)
- `first_seen_utc` set (the R7 N3 backfill)

Without the setdefault, the assertion on `first_seen_utc` would fail
loudly.

## False positives / dismissed claims

The monitor independently re-read each citation:

- **A1 M2 fold incomplete** — DISMISSED. Verified at
  `run_live_update.py:434-447` (main probe), `:495-503` (circuit-breaker
  path), `:587-595` (input-corruption path), `:730-748` (crash-handler).
  All 4 write_live_state sites carry the helper. Pinned by 8
  test_r6_m2_* tests.
- **A2 knock_matrices keying asymmetric** — DISMISSED. The matrix is
  constructed symmetrically at `03_simulate.py:271-310`; both
  `(home, away)` and `(away, home)` keys exist by construction.
- **A2 FIFA-rank tie-break non-determinism** — DISMISSED. `fifa_pts`
  is loaded from a frozen YAML; Python's stable sort preserves the
  pre-sort order on ties, and the pre-sort order is itself
  deterministic (group iteration order = config order = file order).
- **A3 warning suppression on co-occurrence** — DISMISSED. The
  `mf_warnings` list at `run_live_update.py:434-447` does
  `extend()`, not `assign()`; multiple warning types from different
  sources accumulate independently.
- **A4 AUTO_TIER static pin pattern by-design** — DISMISSED. The
  rationale is documented at `CORRECTIONS.md §7`: AUTO_TIER is a
  shadow-mode rollout, the `=False` literal is the canonical
  off-switch, and a static pin (`test_auto_tier_active_remains_false`)
  is the right shape because the FLIP is the change-event, not the
  computation.
- **A5 untracked tick artefacts in working tree** — DISMISSED. The
  `dashboard/live_state.json` + `data/processed/predictions_live.json`
  diffs are stale tick outputs from a prior local invocation, NOT R7
  source changes. They are excluded from the R7 commit; the live
  workflow regenerates them on schedule.

## Verdict

**Status**: GREEN — operational pressure test passes; three defensive
improvements landed; no production-blocking findings.

**Tests**: 1099 → **1105 passed** (+6 R7-related: 2 new + 4 from
ambient adjustments), 1 skipped, 0 failed.
**Σ-gate**: exit 0 on `data/processed/predictions_live.json`.
**Push**: NOT pushed; remains on `hardening/r32-pressure-test-r2`
local-only per instruction.

**Cumulative pressure-test arc**: R1+R2 baseline (1059) → R3 (+27, four
HIGH closed) → R4 (one HIGH closed, ten false positives filtered) → R5
(one HIGH + two MEDIUMs closed) → R6 (two MEDIUMs + one follow-up
closed, ~10 false positives filtered) → R7 (three DEFENSIVE closures,
~5 false positives filtered, no HIGH/MEDIUM found).

After 5 rounds of adversarial pressure testing, the genuine-finding
rate has converged toward the noise floor:

| Round | HIGH | MEDIUM | DEFENSIVE/LOW |
|-------|------|--------|---------------|
| R3    | 4    | —      | —             |
| R4    | 1    | —      | —             |
| R5    | 1    | 2      | —             |
| R6    | 0    | 2      | 1 (follow-up) |
| R7    | 0    | 0      | 3             |

The shape of the curve — production-blocking → operational →
defensive — is the expected shape of a maturing system. R32 kickoff
is on 2026-06-28 (T-10 days); the repo is in solid shape.
