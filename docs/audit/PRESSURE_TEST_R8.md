# WC26 Matchday Intelligence — Pressure-Test Round 8 (deep adversarial sweep, post-R7)

**Date**: 2026-06-18
**Branch**: `hardening/r32-pressure-test-r2` (local-only; push human-gated)
**Suite delta**: 1105 → **1110 passed**, 1 skipped, 0 failed
**Σ-gate**: exit 0 on `data/processed/predictions_live.json` (|Δ| = 0, teams = 48, tol = 1e-6)
**Standing constraints preserved**: no retrains, no threshold/cap changes,
`AUTO_TIER_ACTIVE=False`, `NB_ALPHA=5.0`, `DC_RHO=-0.13`, `MAX_G=10`,
`STALENESS_MAX_AGE_HOURS=6.0`.

## Context — why Round 8

R7 closed three defensive items (N1 third-place fallback diagnostic, N2 M3
functional test, N3 first_seen_utc backfill). The user ordered another
adversarial sweep on top of R7. Five orthogonal agents probed dimensions
not deeply covered in R3-R7:

| Agent | Probe dimension |
|-------|-----------------|
| A1 | Concurrency + atomicity (atomic_write_json edge cases, fast/slow workflow collision, lockfiles, subprocess SIGTERM, GHA concurrency blocks, git push collisions) |
| A2 | Resource exhaustion + pathological inputs (memory growth, FD leaks, huge ints/strings, recursion depth, JSON parse complexity, NaN/Inf propagation) |
| A3 | Schema evolution + defensive deserialization (schema_version field, watchdog baselines, type coercion, missing-field silent defaults, future-dated entries) |
| A4 | Operator UX + warning observability (severity ranking, dedup parity, ordering, message actionability, hidden failure modes, payload bloat) |
| A5 | R7 integration probe + documentation drift (N1/N2/N3 in-prod behavior, R32_READINESS file:line accuracy, PRESSURE_TEST_R7 internal consistency) |

A sixth **monitor agent** independently triaged the top 5 candidate
findings with file:line evidence and a verdict for each.

## Triage outcome

| Agent | Reported | Verified after monitor | Action |
|-------|----------|------------------------|--------|
| A1 (concurrency) | 2 HIGH + 2 MEDIUM + 1 LOW + 1 DEFENSIVE | All dismissed: H1 tempfile leak (`.tmp` is gitignored + short-lived runners), H2 slow-workflow result re-fetch (operational waste, not safety), M1/M2 launchd-only theoretical, L1 rebase noise. | None |
| A2 (resource) | 2 MEDIUM + 2 LOW + 2 DEFENSIVE | 1 genuine DEFENSIVE (O2 — NaN/Infinity round-trip through json into model lookups); F1 (m-id overflow), F2 (audit log unbounded), F3 (watchdog recursion bomb), F4 (event warning spam), F5 (form_cache O(n²)) all LOW / post-R32. | **O2** |
| A3 (schema) | 2 CRITICAL + 3 HIGH + 3 MEDIUM | 1 CONFIRMED-defensive (C1 schema_version drift — low impact, documented only); C2 (stat-fallback inversion) DISMISSED on close reading (only fires on µs-scale fs race; sibling probes still independent); H1-H2 LOW-MEDIUM, deferred. | document |
| A4 (operator UX) | 1 CRITICAL + 4 HIGH + 2 MEDIUM + 1 LOW + 1 DEFENSIVE | All CONFIRMED but UX-shape — severity/dedup/cap/ordering are real gaps but constitute a planned UX iteration, not a hardening fix. Deferred. | document |
| A5 (R7 integration) | 1 MEDIUM + 3 LOW + 2 DOC-DRIFT | 1 CONFIRMED (O1 — sim subprocess stderr lost between simulator and dashboard; operators see generic sim_failure pill with no R7 N1 diagnostic context); R8-2/3/4 LOW or doc-drift. | **O1** |

**Two genuine fixes landed after monitor verification: O1, O2.**
**Two CONFIRMED-defensive findings (C1, A4 severity/dedup gaps)
documented for a planned UX iteration but NOT closed in R8 to keep
scope tight.**

## O1 — sim subprocess stderr capture (MEDIUM)

### Root cause

The R7 N1 RuntimeError at `scripts/03_simulate.py:466-471` emits a rich
diagnostic when the annex_c lookup misses and the FIFA-rank fallback
cannot fill all 8 third-place slots:

```python
raise RuntimeError(
    f"annex_c miss + fallback exhausted: only assigned "
    f"{sorted(third_slot_map)} ({len(third_slot_map)}/8 slots); "
    f"unused thirds={[q['name'] for q in unused]}; "
    f"check data/raw/annex_c_thirds_map.json and slot_pools config"
)
```

But `scripts/live/run_live_update.py:run()` at lines 70-72 invokes the
simulator with `subprocess.run(cmd, cwd=str(ROOT)).returncode` —
NO `capture_output=True`. The R7 N1 message prints to the orchestrator's
inherited stderr (CI logs only); it never reaches `live_state.json:warnings[]`.
The operator-facing `sim_failure` pill at lines 637-641 carries only:

```
Live simulation failed ({new_failures}/{CB_THRESHOLD});
previous predictions_live.json retained.
```

— with zero hint that the underlying cause was a third-place fallback
exhaustion or any other specific Python error. An on-call operator at
3am during R32 would have to ssh into a GHA logs viewer to find the
real cause.

### Fix

`scripts/live/run_live_update.py` adds a new `run_capture()` helper
(lines 75-94) that wraps `subprocess.run` with `capture_output=True,
text=True`, tees captured stderr to the parent's stderr (so CI logs
still see it), and returns `(rc, stderr_text)`. Only the sim invocation
(lines 633-639) switches to `run_capture()`; all other subprocess calls
remain on the plain `run()`.

The `sim_failure` warning construction at lines 644-651 now folds the
last 500 chars of stderr into the message:

```python
sim_msg = f"Live simulation failed ({new_failures}/{CB_THRESHOLD}); ..."
stderr_tail = (sim_stderr or "").strip()
if stderr_tail:
    sim_msg += f" Last stderr: {stderr_tail[-500:]}"
```

The 500-char cap bounds the live_state.json payload (Vercel-fetched by
every dashboard visitor); the suffix `:` rather than `;` is deliberate
so the dashboard pill can word-wrap on the colon.

### Verification

- `test_r8_o1_run_capture_helper_exists_and_returns_rc_plus_stderr` —
  functional unit test: invokes a child Python that writes to stderr
  and exits non-zero; verifies `(rc, stderr_text)` is returned correctly.
- `test_r8_o1_sim_failure_warning_includes_stderr_tail_pinned_in_source` —
  static pin: the sim subprocess invocation must use `run_capture()`,
  AND the `sim_failure` warning construction must fold captured stderr
  into the message. A revert to plain `run()` breaks both pins.

## O2 — allow_nan=False on matchday writer (DEFENSIVE)

### Root cause

CPython `json.loads('Infinity')` returns `float('inf')` and
`json.dumps(float('inf'))` returns `'Infinity'` — silent round-trip,
not standard JSON. The producer of `dashboard/matchday_intelligence.json`
(`scripts/live/apply_matchday_adjustments.py:_atomic_write_json` at
lines 84-93) used plain `json.dump(payload, tmp, indent=2,
ensure_ascii=False)` with no `allow_nan=False` guard.

A numerical edge case in any upstream rollup (injuries, suspensions,
referee, weather, stats-proxy) that produced `nan` or `inf` would:
1. Round-trip silently through the writer
2. Be read back by `scripts/03_simulate.py:820-828` via
   `base_intel_plus_state.get(h, 0.0)` with NO `math.isfinite()` guard
3. Propagate as Inf into `predict_lambdas` → `nbinom.pmf` → NaN matrix
4. Yield NaN `p_champion` in `data/processed/predictions_live.json`
5. Trip the Σ-gate at the very end (good), OR slip through if the gate
   is bypassed for any reason

The R4 `math.isfinite` guard at `injury_adjustments.py:481` covers ONE
input (per-team injury elo from `_load_injury_components`) — not the
consolidated matchday state. R8 closes the gap at the producer instead
of the consumer (fewer touchpoints; same fail-loud semantics).

### Fix

Single-line change at `scripts/live/apply_matchday_adjustments.py:91`:

```python
json.dump(payload, tmp, indent=2, ensure_ascii=False, allow_nan=False)
```

Clean runs (every tick since the project began) are unaffected —
`allow_nan=False` only fires when an upstream has already produced NaN
or Infinity, in which case raising at the WRITE boundary is far better
than corrupting the model lookup silently. The exception propagates
out of `apply_matchday_adjustments.main()`, which is invoked as a
subprocess by the slow workflow; the slow workflow's GitHub Actions
runner marks the job red, surfacing the failure loudly.

### Verification

Three new tests in `tests/live/test_apply_matchday_adjustments.py`:

- `test_atomic_write_rejects_infinity` — payload with `inf` raises ValueError;
  no atomically-replaced file lingers
- `test_atomic_write_rejects_nan` — payload with `nan` raises ValueError
- `test_atomic_write_accepts_clean_finite_floats` — negative case:
  normal floats round-trip cleanly; R8 O2 changes nothing on the
  happy path

## Documented but not closed (planned UX iteration)

### A4 — Severity / dedup / cap / ordering across warning types

The audit surfaced 5 real operator-UX gaps:

1. **No severity field anywhere** — every `warnings.append({...})` emits
   `{type, message, ...}` but no `severity`. The dashboard pill at
   `dashboard/app.js:417` displays `warnings[0]` in insertion order;
   a LOW warning can occlude a CRITICAL.
2. **Dedup parity** — R6 M3 closed dedup for `provider_returned_nothing`
   only. `fetch_failure`, `matchday_consolidated_stale`,
   `matchday_subsystem_stale`, `circuit_breaker`, `orchestrator_crash`,
   `provider_key_missing` all still emit a fresh dict every tick.
3. **No `warnings[]` length cap** — a 24h outage with 6 warning types
   × 144 fast ticks ≈ 860 entries; payload growth unbounded.
4. **No silent-failure warning for missing key player** — typo in
   `data/raw/key_players_2026.json` silently demotes a star to
   `tier_2`/`tier_3` via `DEFAULT_TIER`; audit log records
   `source="default"` but no warning bubbles to `live_state.json`.
5. **Schema watchdog soft-mode log is GHA-only** — drift detected but
   never lands in `live_state.json:warnings[]`.

**Why deferred to a planned UX iteration**: these constitute a coherent
warning-system redesign (~30-line shared helper + dashboard sort logic +
~10 emission-site touch-ups). Each individual fix is small but the
shape of the change is a new contract on warning shape, not a defensive
patch. Pressure-test rounds intentionally stay narrow to keep R32 risk
low; the UX iteration belongs in a separate planning + implementation
cycle.

### A3 C1 — schema_version written but never read

`data/raw/key_players_2026.json:2` carries `schema_version: 2`; every
other JSON producer uses `schema_version: 1`; NO consumer reads the
field. A drift between v1 / v2 producers and consumers goes silent.
Today's impact is low because v2 is a strict superset (adds `replacement`
block) and consumers use `.get("replacement", {})` chains that silently
no-op on v1 entries. Worth fixing in the warning-system iteration above
(would naturally land as a `schema_version_mismatch` warning type).

## False positives / dismissed claims

The monitor independently re-read each citation:

- **A1 H2** (slow workflow's `--with-events` result discarded at commit
  time) — confirmed but operational/cost, not safety. Slow workflow
  consumes the locally-fresh copy correctly within its own runner; the
  discarded commit only wastes ~8 API calls/day. Document, don't fix.
- **A1 M1/M2** (read-modify-write race) — requires launchd autopilot
  to manifest; no launchd plist in the repo. Theoretical.
- **A3 C2** (stat-fallback inversion at `apply_matchday_adjustments.py:278`)
  — the fallback only fires when `results_path.exists()` is True BUT
  a follow-up `.stat()` race-fails (microseconds window). The sibling
  warnings (`matchday_consolidated_missing`, subsystem-stale) still
  fire independently, so the "silenced staleness" would suppress only
  the type (2) warning for one tick. Risk near zero.
- **A4 datetime.now() naive** — grep clean across `scripts/live/*.py`;
  all timestamps use `datetime.now(timezone.utc).isoformat()`.
  Defensive CI ripgrep ban would be nice but not blocking.
- **A5 R8-4** (explicit `first_seen_utc: None` survives setdefault)
  — only an operator hand-edit could produce that state; warnings[]
  is not an operator-touch surface. Acceptable.

## Verdict

**Status**: GREEN — operational pressure test passes; two genuine
defensive improvements landed; no production-blocking findings.

**Tests**: 1105 → **1110 passed** (+5 R8 tests: 3 for O2, 2 for O1),
1 skipped, 0 failed.
**Σ-gate**: exit 0 on `data/processed/predictions_live.json`.
**Push**: NOT pushed; remains on `hardening/r32-pressure-test-r2`
local-only per instruction.

**Cumulative pressure-test arc**:

| Round | HIGH | MEDIUM | DEFENSIVE/LOW |
|-------|------|--------|---------------|
| R3    | 4    | —      | —             |
| R4    | 1    | —      | —             |
| R5    | 1    | 2      | —             |
| R6    | 0    | 2      | 1 (follow-up) |
| R7    | 0    | 0      | 3             |
| R8    | 0    | 1      | 1             |

The genuine-finding rate has flattened at the noise floor as expected.
R8's O1 (sim stderr capture) is the highest-ROI find this round —
small code change, large operator visibility improvement for the R7 N1
diagnostic surface. O2 (allow_nan=False) closes a NaN-propagation gap
that the existing math.isfinite guards do not cover.

R32 kickoff is on 2026-06-28 (T-10 days); the repo is in solid shape.
