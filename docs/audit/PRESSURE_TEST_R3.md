# WC26 Matchday Intelligence — Pressure-Test Round 3 (post-audit hardening)

**Date**: 2026-06-17
**Branch**: `hardening/r32-pressure-test-r2` (continues local-only commit history;
push remains human-gated per instruction)
**Suite delta**: 1059 → **1086 passed**, 1 skipped, 0 failed, 0 xfailed (`tests/live/`)
**Σ-gate**: exit 0 on `data/processed/predictions_live.json` (|Δ| = 0, teams = 48, tol = 1e-6)
**Standing constraints preserved**: no retrains, no threshold/cap changes,
`AUTO_TIER_ACTIVE=False` (`scripts/live/injury_adjustments.py:64`), `NB_ALPHA=5.0`,
`DC_RHO=-0.13`, `MAX_G=10`, `STALENESS_MAX_AGE_HOURS=6.0`.

## Context — what triggered Round 3

After R2 landed locally and was pushed to `origin/hardening/r32-pressure-test-r2`,
an independent adversarial audit deployed 4 specialised agents (hardcoded values,
data fetching, mathematical correctness, CI/CD) that surfaced **4 HIGH-severity
findings** plus a long tail of MEDIUM/LOW items. The HIGH findings were a CONDITIONAL
YES → merge-to-main was blocked until they were closed. Round 3 closes all 4.

| ID | Severity | Finding | Status |
|----|----------|---------|--------|
| **H1** | High | launchd plist still hardcoded `/Users/prav/Desktop/personal-projects/fifa-wc-26-prediction/...` at lines 13,17,36,38 — defeats the R2 portability fix to `run_if_tournament.sh`. Autopilot would still fail silently from this checkout. | **Closed** |
| **H2** | High | P1c freshness propagation missed the outer `except Exception` orchestrator-crash handler at `run_live_update.py:646-649` — `mf_warnings` was out of scope, so an orchestrator crash silently dropped the freshness signal. Exactly the failure mode P1c was meant to close. | **Closed** |
| **H3** | High | 4 of 5 fetchers (`fetch_injuries`, `fetch_lineups`, `fetch_match_stats`, `fetch_player_stats`) used bare `urllib.urlopen` with no retries. A single transient 5xx escalated straight to subsystem_stale. Only `fetch_results.py:171` had retries. | **Closed** |
| **H4** | High | `fetch_player_stats.py` lacks any rate limiter; the per-team fan-out fires 48+ /players?team=X requests per tick. API-Football free tier is 10/min → 429 cascade → degraded subsystem → stale matchday data. | **Closed** |

The full audit findings (11 HIGH, 21 MEDIUM, 19 LOW — 0 CRITICAL) were synthesized
by four parallel agents and verified end-to-end before this round began.

---

## H1 — launchd plist portability

### Root cause

`scripts/launchd/com.prav.wc26-preview.plist` hardcoded the deprecated repo path
`/Users/prav/Desktop/personal-projects/fifa-wc-26-prediction/` at:

- L13 `ProgramArguments` → `run_if_tournament.sh` lookup
- L17 `WorkingDirectory`
- L36 `StandardOutPath` → `logs/launchd-stdout.log`
- L38 `StandardErrorPath` → `logs/launchd-stderr.log`

R2 round 2 made `run_if_tournament.sh` portable via `$(cd "$(dirname "$0")"
&& pwd)`, but launchd reads the plist FIRST and tries to exec the wrong script.

### Fix

`scripts/launchd/com.prav.wc26-preview.plist` becomes a **template** with
`__REPO_ROOT__` placeholders at all four paths. The XML header documents
that direct installation is forbidden.

`scripts/launchd/install.sh` substitutes `__REPO_ROOT__` via `sed
"s|__REPO_ROOT__|$REPO_ROOT_RESOLVED|g"` when copying to
`~/Library/LaunchAgents/`. Pre-flight checks:

1. Refuse to install if the source template has no `__REPO_ROOT__` markers
   (catches a partial revert).
2. Refuse if substitution leaves any unresolved marker
   (catches `sed` failures).

### Verification

`tests/live/test_launchd_path.py` extended with 5 new tests:

- `test_h1_plist_body_does_not_hardcode_old_personal_projects_path`
- `test_h1_plist_uses_repo_root_template_markers`
- `test_h1_installer_substitutes_repo_root_marker` (runs sed in a tmpdir)
- `test_h1_installer_rejects_unsubstituted_plist` (subprocess install.sh, expects exit≠0)
- `test_h1_installer_refuses_template_without_markers`

---

## H2 — orchestrator-crash freshness propagation

### Root cause

`scripts/live/run_live_update.py:638-652` (outer `try/except Exception` after
`sys.exit(main())`) called `write_live_state(...)` with `warnings=[{"type":
"orchestrator_crash", ...}]` — a one-shot literal. The `mf_warnings` variable
from `main()` was out of scope when `main()` raised, so the audit-confirmed
P1c freshness signal silently dropped on orchestrator crashes.

### Fix

`scripts/live/run_live_update.py` end-of-module crash handler now builds a
`crash_warnings` list, RE-PROBES freshness via `_matchday_freshness_warnings_safe()`,
extends the list, and passes `warnings=crash_warnings` to `write_live_state`.
A nested try/except ensures a freshness-probe failure inside the crash handler
doesn't mask the orchestrator_crash signal (belt-and-braces — the safe wrapper
already swallows its own exceptions, but the orchestrator-crash path is where we
need maximum signal preservation).

### Verification

`tests/live/test_fast_path_freshness.py` extended with 2 new static-pin tests:

- `test_h2_crash_handler_probes_freshness_via_safe_wrapper`
- `test_h2_crash_handler_appends_freshness_to_crash_warning`

---

## H3 — shared HTTP retry helper across 4 fetchers

### Root cause

`fetch_results.py:171-189` had a proven retry helper (`http_get_json` — 3 attempts,
exponential backoff on 5xx/URLError/TimeoutError/ConnectionError, no retry on 4xx).
The other 4 fetchers each had their own bare `_http_get_json` with NO retries:

- `fetch_injuries.py:110`
- `fetch_lineups.py:103`
- `fetch_match_stats.py:109`
- `fetch_player_stats.py:229`

P1b "resilience" relied on `degrade_subsystem` swallowing failures, but a
1-second `time.sleep(1); retry` would have prevented degradation in the first place
for transient errors. The audit was correct that this is a HIGH-severity gap.

### Fix

New module `scripts/live/_http_client.py`:

- `http_get_json(url, headers, timeout=15, retries=3)` — direct port of the
  proven `fetch_results.py:171-189` implementation
- `RateLimiter(min_interval_seconds)` — minimal token-bucket-of-one for H4

Each of the 4 fetcher modules keeps its local `_http_get_json` signature (no
call-site changes) but its body now delegates to
`_http_client.http_get_json` via a **late import**:

```python
def _http_get_json(url: str, headers: dict, timeout: int = 15) -> dict:
    from _http_client import http_get_json  # noqa: PLC0415
    return http_get_json(url, headers, timeout=timeout)
```

The late import is intentional: it makes monkeypatching the shared helper from
tests trivial (no need to patch each fetcher's captured reference).

### Verification

`tests/live/test_http_client.py` (NEW, 20 tests):

- `test_h3a_5xx_then_recovery_succeeds` — 5xx-5xx-200 retry cascade
- `test_h3b_4xx_does_not_retry[400/401/403/404/422/429]` (parametrized) —
  4xx including 429 raises immediately, no sleep
- `test_h3c_persistent_urlerror_exhausts_retries` — URLError 3x → re-raises
- `test_h3c_timeout_error_retried` — TimeoutError 2x → re-raises
- `test_h3d_fetcher_delegates_to_shared_http_get_json[fetch_injuries/lineups/match_stats/player_stats]`
  (parametrized) — pins all 4 delegations

---

## H4 — rate-limiter for fetch_player_stats per-team fan-out

### Root cause

`fetch_player_stats.fetch_apifootball_player_stats` iterates 48 WC2026 teams →
~48 /teams + 48 /players calls + pagination ≈ 60-100 requests per tick at network
speed (~100 req/s burst). API-Football's free tier is 10/min → 429 cascade.
Paid tier is 300/min ≈ 5 req/s → still over budget on burst.

### Fix

`scripts/live/_http_client.RateLimiter`:

```python
class RateLimiter:
    def __init__(self, min_interval_seconds: float = 0.15) -> None:
        if min_interval_seconds < 0:
            raise ValueError(...)
        self.min_interval = float(min_interval_seconds)
        self._last_call: float | None = None

    def acquire(self) -> None:
        """Block until at least `min_interval_seconds` since previous acquire()."""
```

`fetch_player_stats.fetch_apifootball_player_stats` now:

1. Accepts `sleep_between: float = 0.15` (matches the existing pattern at
   `fetch_results.py:enrich_matches_with_events:406`).
2. Creates a SINGLE shared `RateLimiter(sleep_between)` for the entire sweep.
3. Passes it to every `fetch_team_players(...)` call.

`fetch_player_stats.fetch_team_players` now accepts `rate_limiter=None` and
calls `acquire()` **before each paginated /players HTTP request** (not just
the first page). So the throttle applies globally — 48 teams × 2-3 pages each ≈
14-22 seconds of evenly-spaced calls at 6.7 req/s, instead of a 100 req/s burst.

### Verification

`tests/live/test_http_client.py` (H4-specific tests):

- `test_h4a_zero_interval_is_noop`
- `test_h4b_enforces_minimum_gap`
- `test_h4c_rejects_negative_interval`
- `test_h4_first_acquire_is_immediate`
- `test_h4d_fetch_team_players_calls_acquire_before_each_request` —
  pagination loop: 3 pages → 3 acquires (1:1 with HTTP calls)
- `test_h4e_fetch_apifootball_player_stats_creates_shared_limiter` —
  sleep_between=0 → None limiter (skip throttle)
- `test_h4e_nonzero_interval_creates_shared_limiter` — sleep_between>0 →
  SAME limiter object across all teams (global rate ceiling, not per-team)

---

## Independent monitor verdict

A fresh Explore agent (did NOT author any of the H1-H4 fixes) re-verified all 20
claims (H1×5 + H2×4 + H3×4 + H4×4 + boundary×4) by reading files and grepping
the branch. Verdict: **"H1-H4 ALL cleanly closed ✓"**. Each claim was backed by
file:line evidence.

## Σ-gate + standing-constraint check (post-R3)

| Invariant | Value | Status |
|-----------|-------|--------|
| Σ p_champion across 48 teams | 1.0 (\|Δ\| = 0.000e+00) | ✓ |
| `check_invariants.py` exit code | 0 | ✓ |
| Σ-gate tolerance | 1e-6 (strict) | ✓ |
| `AUTO_TIER_ACTIVE` | False (`scripts/live/injury_adjustments.py:64`) | ✓ |
| `NB_ALPHA` (dispersion) | 5.0 | ✓ |
| `DC_RHO` | −0.13 | ✓ |
| `GOAL_GRID_MAX_GOALS` | 10 | ✓ |
| `STATS_CAP_TOURNAMENT_TOTAL` | 20.0 | ✓ |
| `GRAND_TOTAL_CAP` | 45.0 | ✓ |
| `STALENESS_MAX_AGE_HOURS` | 6.0 | ✓ |
| TODO / FIXME / XXX / HACK in `scripts/` & `scripts/live/` | 0 | ✓ |
| xfailed tests | 0 | ✓ |
| failed tests | 0 | ✓ |
| Documented skips | 1 (pure-Sheets-I/O `.gs` helper, no math core) | ✓ |
| pytest tests/live/ count | **1086 passed** (+27 vs R2) | ✓ |

## Files changed in R3

NEW:
- `scripts/live/_http_client.py` (H3 + H4 foundation)
- `tests/live/test_http_client.py` (20 tests: H3a/b/c/d + H4a-e)

MODIFIED:
- `scripts/launchd/com.prav.wc26-preview.plist` (H1 template)
- `scripts/launchd/install.sh` (H1 sed substitution + pre-flight guards)
- `scripts/live/fetch_injuries.py` (H3 shim)
- `scripts/live/fetch_lineups.py` (H3 shim)
- `scripts/live/fetch_match_stats.py` (H3 shim)
- `scripts/live/fetch_player_stats.py` (H3 shim + H4 rate-limit wiring)
- `scripts/live/run_live_update.py` (H2 crash-handler freshness probe)
- `tests/live/test_launchd_path.py` (H1 — 5 new tests)
- `tests/live/test_fast_path_freshness.py` (H2 — 2 new static-pin tests)

## Bottom line

Round 3 closes every HIGH-severity finding from the post-R2 adversarial audit
without changing any production constant, retraining any model, or weakening
any existing gate. The matchday layer is now also more robust under network
turbulence (H3 retries) and API rate-limit pressure (H4 throttle), and the
freshness signal cannot be silently dropped by any of the 7 possible
`write_live_state` paths (P1c × early-exits × crash-handler).
