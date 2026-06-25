# CORRECTIONS — Deviations from CLAUDE_CODE_PROMPT_WC26_IMPROVEMENTS.md

Recorded before any code change in Wave B. Each deviation traces to a verified
discovery finding (see Wave-A discovery report from 2026-06-16). The originating
plan file is `CLAUDE_CODE_PROMPT_WC26_IMPROVEMENTS.md`; this document records
every binding deviation from that spec and why.

---

## 1. Phase 1B integration site moves from apply-layer to fetch-layer

**Plan said:** modify `_load_injury_components()` in `apply_matchday_adjustments.py`
to call `net_injury_elo()` instead of `discounted_elo()`.

**Reality:** `discounted_elo()` is only ever called in `fetch_injuries.py:203`.
The apply layer reads a precomputed `injuries_2026.json` — it does not recompute
Elo per player. Wiring `net_injury_elo()` into the apply layer would be a no-op.

**Decision:**
- Wire `net_injury_elo()` at `fetch_injuries.py:203`, immediately after
  `classify_tier()` returns.
- Extend the per-player schema in `injuries_2026.json` to carry all three values:
  - `elo` — raw out-player penalty (unchanged; preserves backward compat with
    existing readers and tests)
  - `replacement_elo` — the `elo_equiv` of the named replacement, or `null` if
    no replacement data
  - `net_elo` — `elo - replacement_elo` (or `elo` when replacement is `null`)
- Add a top-level `net_elo_active: true` flag in `injuries_2026.json` so the
  apply layer can branch explicitly on which value it consumes downstream.
- `discounted_elo()` itself is untouched — existing tests depend on it and the
  fallback path needs it.

---

## 2. Phase 2 — referee field availability

**Plan said:** API-Football fixture object includes `referee`.

**Reality:** Confirmed. `probe_apifootball_knockouts.py:144` already strips it
out of the probe output with the comment "not needed for the probe", proving
the field is present in the raw response.

**Decision:** Proceed with Phase 2 as written — extract referee in
`fetch_results.py`, persist on each match, build `referee_adjustments.py`,
mirror the `_load_weather_components()` integration pattern at
`apply_matchday_adjustments.py:199-232`, cap at `REFEREE_CAP = 8.0`.

---

## 3. Phase 3 — real xG is not available; Phase 3A pivots to three explicit deliverables

**Plan said:** if `own.get("Expected Goals")` is present in API stats, use
`(own_xg - opp_xg) * 8.0`.

**Reality:** API-Football's `expected_goals` field is a shot-count proxy, not
per-shot true xG (which requires shot-location data the API does not expose).
`fetch_match_stats.py:160` hardcodes `true_xg_available = False`, and
`pre_flight.py:628-629` enforces that this flag remains `False` as a quality
gate. Faking real xG would violate the guard and corrupt downstream caching.

**Decision:** Phase 3 ships exactly three deliverables and **does not touch the
pre_flight guard**:

  (a) **Honesty flags**: propagate `xg_attempted: bool` and `xg_found: bool`
      from `fetch_match_stats.py` → `match_stats_2026.json` →
      `predictions_live.json` so the dashboard surfaces the limitation visibly.

  (b) **Proxy refinement**: tighten the existing shots/possession/corners
      formula in `stats_proxy_adjustments.py:compute_form_delta()`. Document
      the weighting rationale inline (one short comment max, per project
      conventions). The cap (`STATS_PROXY_RAW_CAP = 12.0` → ±8 per match
      downstream) does not change.

  (c) **Flag-gated real-xG code path**: write the real-xG branch behind a
      `true_xg_available` check, dead-by-default. Ship a unit test that
      exercises that branch with mock data so the day a true-xG feed lands,
      flipping the flag is a one-line change with proven behavior.

The pre_flight assertion at `pre_flight.py:628-629` stays exactly as written.

---

## 4. Phase 4 — card events are absent; insert prerequisite step B3

**Plan said:** read card events from `results_2026.json`.

**Reality:** `results_2026.json` completed matches have no `events` field.
`fetch_results.py` never calls API-Football's `/fixtures/events` endpoint.
Building `suspension_tracker.py` against the current pipeline would produce
an empty tracker.

**Decision:**
- **Step B3 (new prerequisite)**: extend `fetch_results.py` to call
  `GET /fixtures/events?fixture={id}` for each completed fixture and persist
  `events: [{type, team, player, time}, ...]` on each match record. Follow
  the existing helper pattern around `extract_pens_and_winner()` (lines 208-257).
- Phase 4A (`suspension_tracker.py`) only proceeds AFTER B3 lands AND a
  dry-run confirms card events are actually flowing for completed WC matches.
- If `/fixtures/events` returns empty for completed WC matches, surface that
  finding (write it to the discovery report and to a `warnings` field in
  `injuries_2026.json`) rather than ship an empty tracker.

---

## 5. Phase 5 — Goal Grid must mirror the production simulator's Dixon-Coles constants

**Plan said:** `TAU = 0.10`, `MAX_GOALS = 8` in the Node.js goal-grid tests.

**Reality:** `scripts/03_simulate.py:96` defines `dc_rho = -0.13` and
`:186` uses `max_g = 10` as the score-matrix dimension. The plan's test
constants would diverge from production.

**Decision:**
- All Goal Grid code (`.gs` `GOAL_GRID()` custom function) AND the Node.js
  tests use **`τ = -0.13`** and **`MAX_GOALS = 10`**.
- **Sign-convention proof-of-equivalence**: before declaring 5B done, print a
  side-by-side of the four Dixon-Coles–corrected cells (0-0, 0-1, 1-0, 1-1)
  produced by `03_simulate.py`'s `build_score_matrix()` and by the JS
  `GOAL_GRID()`/`buildScoreMatrix()` for an identical λ pair (e.g.
  λh=1.2, λa=0.9). The four cells must match to ≤1e-6.
- If the Python simulator applies `(1 - λh·λa·ρ)` with `ρ = -0.13`, the JS
  `_dcTau()` helper must apply the same expression with the same sign. Do
  not normalise τ to a positive value silently — sign mismatches will look
  like a passing test but produce a different score distribution.

---

## 6. Phase 2 referee gate — sub-20-match referees emit zero bonus, not a noisy one

**Plan said:** apply `home_elo_bonus = (ref_home_win_rate − 0.58) × 35` to every
referee assignment in the fixture list.

**Reality:** the Wave-A referee baseline (`data/raw/_proposals/referee_wc2026.json`)
includes referees with as few as 6–15 prior senior-international matches. At
that sample size `ref_home_win_rate` is dominated by noise: a single 5–0
home rout shifts the rate by ~7 percentage points, which the scaling factor
of 35 then amplifies into a ±2.5 Elo swing on no evidence. Shipping that as a
"medium-confidence" bonus would launder noise into the model.

**Decision:**
- `MIN_MATCHES_FOR_BONUS = 20` (`scripts/live/referee_adjustments.py:39`) gates
  every bonus. Below the floor, `raw_bonus = 0.0` and `confidence = "none"` —
  the row is still emitted so the dashboard surfaces the assignment, but no
  Elo moves.
- `_confidence_for()` returns `"high"` ≥100, `"medium"` ≥20, `"none"` below.
  The cap (`REFEREE_CAP = 8.0`) stays unchanged from §2.
- The gate is enforced at the per-row level inside `referee_adjustments.py`,
  not at the apply layer — so `apply_matchday_adjustments._load_referee_components()`
  sees a clean `team_adjustment_elo = 0.0` for sub-floor refs and never has
  to re-implement the threshold.
- The honesty floor matches the proposal methodology (Wave-A), so any future
  baseline refresh inherits the same gate without code changes.

---

## 7. Phase 6 auto-tier — ships in shadow mode (`AUTO_TIER_ACTIVE = False`)

**Plan said:** flip injury-tier classification from the hand-curated whitelist
to an auto-tier model driven by minutes-share from API-Football
`/players?team=&season=` immediately on Phase 6 landing.

**Reality:** the whitelist (`data/raw/key_players_2026.json`) was hand-reviewed
in Wave-A and is the authoritative source for tier_1 designations. Switching
the active classifier without a side-by-side disagreement audit would silently
re-tier players the operator has already vetted — including edge cases like
backup keepers and rotation forwards where minutes-share underestimates
importance.

**Decision:**
- `AUTO_TIER_ACTIVE = False` (`scripts/live/injury_adjustments.py:63`) is the
  shipped default. `classify_tier_with_overrides()` still **computes** the
  auto-tier value on every call and attaches it to the per-player components
  block, so the disagreement-diff CLI (`scripts/live/auto_tier_diff.py`) can
  surface the delta against the whitelist.
- The priority chain is `override > auto_tier > DEFAULT_TIER`. In shadow mode
  (`AUTO_TIER_ACTIVE = False`) the fallback is `DEFAULT_TIER` — i.e. the
  pre-Phase-6 behaviour is preserved bit-for-bit. The auto-tier value is
  carried through the components map purely for observability.
- Flipping `AUTO_TIER_ACTIVE = True` is a one-line change. Pre-flip protocol:
  run `auto_tier_diff.py` against a fresh `data/live/player_stats_2026.json`
  snapshot, eyeball the diff, document the disagreement count in the next
  CORRECTIONS entry, then flip — the four standing gates must still pass.
- If `auto_tier.py` is unimportable (e.g. the minutes-share fetch has not
  populated `player_stats_2026.json`), the auto path degrades to
  `auto_no_data` rather than raising — the override / DEFAULT_TIER fallback
  carries the active tier regardless.

---

## Constraints honored throughout (binding, every phase)

- No `git commit`, `git push`, `git merge`, or any git write operation
- No `vercel deploy` or any deployment command
- No `scripts/live/run_live_update.py`
- No edits to `daily-baseline.yml` and no model retrain
- Cap constants unchanged: `GRAND_TOTAL_CAP=45.0`, `INJURY_CAP_NORMAL=25.0`,
  `INJURY_CAP_EXTREME=35.0`, `LINEUP_CAP=20.0`, `WEATHER_CAP=15.0`,
  `STATS_CAP_PER_MATCH=8.0`
- New caps introduced by this plan: `REFEREE_CAP=8.0` (Phase 2),
  pre-tournament form ±5.0 (Phase 3B)
- No hardcoded player/team names that aren't already in the codebase or in
  the human-reviewed Wave-A proposal files

## Verification protocol applies to every Wave-B phase

read → verify gap → pre-change snapshot → implement → post-change snapshot →
module unit tests → all four gates (`09_validate.py`, `pre_flight.py`,
`pytest tests/ -q`, Σ p_champion ≈ 1.0) → phase eval → Wave-C adversarial
audit (Opus, read-only, never reviews its own work).

---

## 8. Phase 4 was marked "done" before the deliverables existed on disk

**Plan said:** Phase 4 ships three artefacts — (a) `fetch_results.py` extended
to pull `GET /fixtures/events?fixture={id}` and persist an `events: [...]`
array on each completed match (Step B3 from §4 above), (b) a new
`scripts/live/suspension_tracker.py` module that consumes those events to
maintain a per-player yellow/red accumulator and surface next-match
suspensions, and (c) an integration test `tests/live/test_suspension_tracker.py`
that imports the tracker and asserts at least one numerical behavior against
fixture event data.

**Reality:** when Phase 4 was first ticked as "complete" on the progress
board, the on-disk state did not match the claim:
- `scripts/live/suspension_tracker.py` was either absent or a stub with no
  card-accumulator logic.
- `scripts/live/fetch_results.py` did not call `/fixtures/events` — completed
  match records still lacked the `events` array §4 requires.
- `tests/live/test_suspension_tracker.py` did not exist, so the "green test
  board" was green only because no test was looking for the module.

The verification pattern at the time was "pytest stays green and the
progress board shows the row ticked". Neither check inspected whether the
named module file existed at the declared path, nor whether any test
imported it and exercised real card-event data. A phase could therefore go
green by deleting (or never writing) the test that would have failed.

Current on-disk state, recorded as evidence after the recurrence guard
landed:
- `scripts/live/suspension_tracker.py` — present (13,520 bytes).
- `scripts/live/fetch_results.py` — present, with `/fixtures/events`
  references at lines 279, 358, 365, 401, 832, 946.
- `tests/live/test_suspension_tracker.py` — present (14,847 bytes),
  `import suspension_tracker as st` at line 26, behavioral.

**Decision:**
- A phase is only "done" when (a) the named module file exists at the
  declared path on disk, AND (b) a named test in `tests/live/` imports that
  module (not just imports the package) and asserts at least one numerical
  behavior against representative input.
- The final test-sweep checklist (Item #7 of this remediation) carries one
  row per phase classifying its test as `behavioral` or `static-parse-only`.
  Static-parse-only rows do not satisfy the "done" bar and must be upgraded
  before the phase ships.
- The Σ-invariant strict gate (`scripts/check_invariants.py`, Item #3 of
  this remediation) is part of the four-gate run alongside `09_validate.py`,
  `pre_flight.py`, and `pytest tests/ -q`. A passing test suite without a
  passing invariants gate does not constitute "done".
- Any phase claim where the user cannot see the module file at the declared
  path on disk is treated as a regression, not a documentation gap — the
  fix is to land the missing code and its behavioral test, not to edit the
  progress board.
