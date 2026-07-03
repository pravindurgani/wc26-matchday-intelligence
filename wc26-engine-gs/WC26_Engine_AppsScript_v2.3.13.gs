/**
 * WC26 Value Betting Engine — Apps Script (v2.3.13)
 *
 * v2.3.16 PATCH-FIX — fixture metadata sync for knockout rows. Existing
 * sheets could receive R32 model probabilities in Bets!I:K while Bets!B:H
 * stayed blank, so Matchday showed empty "v" rows. refreshAll now
 * idempotently ensures knockout rows exist, then imports date/group/team/
 * venue metadata from predictions_live.match_predictions without touching
 * operator-entered odds or bet logs. (Inline patch; version string unchanged.)
 * v2.3.15 PATCH-FIX — installEngine now invokes applyTailTrapGuardsToGroups so fresh-install operators auto-receive group-row tail-trap guards. (Inline patch; version string unchanged.)
 * v2.3.14 PATCH-FIX — KNOCKOUT GUARDS: two new auto-PASS gates (Method!B87/B88) for tail-prob overconfidence + applyTailTrapGuardsToGroups menu item. (Inline patch; version string unchanged.)
 *
 * v2.3.13 (this file — closes the TWO open warnings from the v2.3.12 audit
 *   sweep with zero-deferral discipline. Both fixes are operator-facing
 *   only — no model math, no calibration, no schema change. Drop-in
 *   replacement for v2.3.12):
 *   - [LOW-2] Method!B63 (autoStatus cell, visible on Method tab) used to
 *     persist a stale "PAUSED · stale feed (N min)" or "PAUSED · circuit-
 *     breaker upstream" message indefinitely after the trip cleared.
 *     refreshLive() only wrote B63 on the stale-branch (line ~1257); the
 *     fresh-branch was silent and never overwrote the trip message. Result:
 *     after B81 flipped FALSE and B55 flipped FALSE, the operator still
 *     saw a red PAUSED banner cosmetically clinging to B63 — confusing if
 *     you glance at Method without cross-checking B55/B81. Downstream
 *     formulas read B55/B81 directly (not B63), so betting was unaffected,
 *     but operator confidence took a hit. v2.3.13 makes the fresh-branch
 *     emit "ON · every " + POLL_MINUTES + " min" — mirroring what
 *     installAutoRefresh() writes at install time — and adds B63 to
 *     clearDiagnosticSurfaces()'s tryClear list so the menu-driven clear
 *     also wipes a stale banner. Self-healing on next refresh tick.
 *   - [LOW-3] Long-shot value-pick safety gate. Engine occasionally flags
 *     "BET" on heavy-underdog odds with absurd-looking edge — e.g. R32
 *     screenshot showed Scotland @ 10.00 vs Brazil EV 39.6%, Curacao @
 *     19.00 vs Ivory Coast EV 51.0%, Tunisia @ 26.00 vs Netherlands EV
 *     59.8%. The engine is doing exactly what it's designed to (devig →
 *     Dixon-Coles bivariate → Kelly-stake the edge) but the walk-forward
 *     calibration LUT thins out in the long tail (Live!B27 already flags
 *     home=1/72 OOB at extreme prices). Probabilities there are clamped
 *     to the last bin which can over-state true probability in the long-
 *     shot regime; EVs computed against clamped probs are over-confident;
 *     Kelly-staked off over-confident EVs over-bets. v2.3.13 introduces
 *     two new constants — LONG_SHOT_ODDS_MIN = 6.00 (~16.7% implied;
 *     traditional long-shot territory) and LONG_SHOT_EDGE_MIN = 0.15
 *     (15pp absolute edge) — and inserts a new branch into BOTH Bets!AT
 *     and Matchday!M:
 *       IF(AND(AE>=6.00, AG>=0.15) AND AH="BET", "🟡 WAIT — long-shot
 *         review: odds X.XX + edge Y.Y% — verify sharp-book + team news
 *         + stake comfort", <existing-BET-message>)
 *     The branch sits INSIDE the existing AH="BET" arm — it overrides the
 *     "Value found" message in Bets!AT and the "🟡 WAIT — check team
 *     news first" message in Matchday!M's L="" arm with a long-shot-
 *     specific WAIT that calls out the actual odds and edge so the
 *     operator can sanity-check against Pinnacle/Betfair Exchange before
 *     flipping L=Y. The PAUSE cascade (B55/B81/drawdown) still takes
 *     priority — long-shot gate fires only when the engine would
 *     otherwise have said "BET". Operator can override the gate by typing
 *     L=Y after running the 3-cell checklist documented in
 *     WC26_v2.3.13_PATCH_NOTES.md (sharp-book sanity, team news, stake
 *     comfort). Once L=Y is set, Matchday!M flips to "🟢 PLACE £..." as
 *     normal.
 *
 *   No schema changes. No XLSX changes. No model math changes. Drop-in
 *   replacement for v2.3.12.
 *
 *   Operator next step after install:
 *   1. Paste v2.3.13.gs into Apps Script → reload.
 *   2. Run menu ▸ First-time setup (or wait 10 min for next refresh tick) —
 *      refreshLive() will overwrite any lingering B63 stale banner.
 *   3. Open Matchday tab — any row with picked odds ≥ 6.00 AND edge ≥ 15%
 *      will now show "🟡 WAIT — long-shot review" in column M instead of
 *      "🟡 WAIT — check team news first" / "🟢 PLACE £...".
 *   4. For each long-shot WAIT, run the 3-cell checklist (sharp-book +
 *      team news + stake comfort). If all 3 pass, type L=Y on that row
 *      and the engine flips to "🟢 PLACE £..." with the original stake.
 *
 * v2.3.12 (carried forward — emergency hotfix for the PAUSED-everywhere production
 *   crisis that surfaced 4 hours after v2.3.11 install. Operator opened
 *   Matchday tab to log R32 bets and every row read "🛑 PAUSED — drawdown
 *   0.6% ≥ 15.0%" — a mathematically impossible attribution since
 *   0.6% < 15%. Root cause: Method!B55 PAUSE gate is an OR over multiple
 *   independent signals (drawdown_check, B81 stale-feed kill, ...) but the
 *   two operator-visible message formulas blindly assumed "drawdown" was
 *   the cause without inspecting which signal actually fired):
 *   - [CRIT-1] Bets!AT (line ~2179, status audit message column) and
 *     Matchday!M (line ~2226, the FINAL ACTION column on the Matchday tab
 *     — i.e. the ONLY operator-facing tab in the documented workflow) both
 *     emitted "🛑 PAUSED — drawdown ..." as a single hardcoded branch when
 *     Method!$B$55=TRUE. v2.3.12 rewrites both into a 3-branch cascade:
 *       IF(B81=TRUE, "feed stale > N min", IF(B51>=B52, "drawdown N% ≥ M%",
 *         "see Method!B49/B63 for cause"))
 *     Drawdown branches mention Method!B54; stale-feed and circuit-breaker
 *     branches stay hard stops so the message matches the real pause gate.
 *     STALE_MINUTES is baked into the message string at formula-write time
 *     (Apps Script JS const → literal number in the formula).
 *   - [HIGH-1] STALE_MINUTES bumped 15 → 25 to absorb a single missed
 *     upstream refresh cycle without tripping the kill switch. The crisis
 *     audit confirmed both live_state.json and predictions_live.json are
 *     refreshing on ~10-min cron, but a single missed tick produced a
 *     20-25 min gap that was triggering B81. 25 min still trips inside
 *     POLL_MINUTES*3 = 30 min — well before a real stall causes harm.
 *     Upstream pipeline self-recovered (live_state was fresh ~5 min when
 *     the audit agent re-curled it). No upstream code change required.
 *   - [LOW-1, deferred] Outrights and GoalGrid BET? formulas (lines 1673,
 *     1827) also reference Method!B55 but emit a generic "🛑 PAUSED" with
 *     no misattribution lie — they don't claim drawdown specifically. Not
 *     touched in v2.3.12. Carry-forward.
 *
 *   CARRIED-FORWARD KNOWN LIMITATIONS (unchanged from v2.3.11):
 *   - matchday_intelligence.json was 122 min stale at crisis time —
 *     separate Vercel cron, not the live_state/predictions refresh chain
 *     this engine depends on. Investigated; outside engine scope.
 *   - walk_forward.json schema brittleness — same as v2.3.11.
 *   - 3× LOW cosmetic in _seedKellyEdgeDisclaimer_ — same as v2.3.11.
 *   - GOAL_GRID Poisson-vs-NB+DC drift — same as v2.3.11 (A1 banner).
 *   - Walk-forward calibration LUT not PAV-isotonic — same as v2.3.11.
 *
 *   No schema changes. No XLSX changes. Drop-in replacement for v2.3.11.
 *   Validated: harness adds v242Regressions — cascade-attribution assertions
 *   on both Bets!AT and Matchday!M formulas (must contain B81 inspection,
 *   B51/B52 drawdown check, AND a fallback branch), STALE_MINUTES=25
 *   constant assertion, and version-string sanity for v2.3.12.
 *
 *   Operator next step after install:
 *   1. Paste v2.3.12.gs into Apps Script → reload.
 *   2. Run menu ▸ First-time setup (or just wait 10 min for next refresh
 *      tick) — refreshLive() will re-evaluate B81 against current feed age.
 *   3. If live_state.json is now < 25 min old, B81 flips FALSE → B55 flips
 *      FALSE → every PAUSED message clears automatically. R32 unblocked.
 *   4. If for any reason B55 remains TRUE, the operator now sees the
 *      ACTUAL cause attributed correctly and can act on it (or set B54=Y
 *      to override).
 *
 * v2.3.11 (carried forward — closes TWO post-v2.3.10 audit MEDs, defers ONE latent
 *   MED + 3 LOW cosmetics with documented reasoning. Path-1 choice on MED-B
 *   (zero migration risk over the elegant A4 row split — operator-typed K:M
 *   odds cannot be safely shifted at T-4d from R32):
 *   - [MED-A] clearDiagnosticSurfaces() did NOT take the same script lock
 *     that refreshAll holds (line ~781, tryLock(0)). Race window: operator
 *     clicks the menu item while a 10-min trigger tick is mid-flight in
 *     refreshAll — setValue('') on Live!B28 here ran ~50ms before
 *     refreshAll's tail wrote _setErrLog_(summary), so the operator saw a
 *     "Cleared 9 surfaces" toast and the error reappeared seconds later.
 *     v2.3.11 wraps the clear body in LockService.getScriptLock().tryLock(0);
 *     on contention, toast "refreshAll in flight — wait POLL_MINUTES and
 *     retry" and bail without clearing. No deadlock: clear is a discrete
 *     menu entrypoint, never calls refreshAll.
 *   - [MED-B] GOAL_GRID A1 banner (added v2.3.10 MED-2 with the drift
 *     warning) is ~250 chars in a single cell with no wrap and no row-height
 *     bump. Desktop overflows rightward through B1..N1 (visible while those
 *     cells are empty); mobile Sheets caps column-A width ~80-120px so the
 *     load-bearing "≥8pp positive edge" caveat sits at character ~180 and
 *     is truncated off-screen with no ellipsis indicator. v2.3.11 Path-1
 *     fix: setWrap(true) on A1 + setRowHeight(1, 60) on the GOAL_GRID
 *     sheet. The Path-2 alternative (move warning to A4, bump
 *     setFrozenRows(3)→4) was rejected: shifting the A:F data block down
 *     by one row would misalign operator-typed K:M bookie odds on existing
 *     v2.3.10 sheets that already have prior-round data in K4 — and that
 *     misalignment is irrecoverable without a one-shot migration block,
 *     which is exactly the kind of bug we don't ship 4 days from R32.
 *
 *   CARRIED-FORWARD KNOWN LIMITATIONS (documented; deferred past R32):
 *   - walk_forward.json schema brittleness — _seedKellyEdgeDisclaimer_
 *     reads B73 via Number(); a future upstream change publishing
 *     "1.6%" instead of 0.016 would silently degrade to "not yet read"
 *     with no error log. Visible (not silent corruption); only a
 *     concern if walk_forward.json schema ever changes. Documented
 *     here, not closed.
 *   - 3× LOW cosmetic in _seedKellyEdgeDisclaimer_: huge bogus lift
 *     (e.g. 1.5) prints "150.00% edge looks aligned"; empty B5 renders
 *     "B5= looks aligned"; -0 input falls through to one-tenth Kelly
 *     advice (correct, but inconsistent with negative-band branch).
 *     Disclaimer is operator-facing copy not betting math — defer past
 *     R32 to avoid disclaimer-copy churn at T-4d.
 *   - GOAL_GRID Poisson-vs-NB+DC drift (~2-9pp on OU2.5/BTTS) —
 *     mitigated v2.3.10 by the A1 banner caveat; full close still
 *     requires predictions_live.json to publish p_btts/p_over25/k_*.
 *     Upstream Python schema change; outside the engine.
 *   - Walk-forward calibration LUT is binned + piecewise-linear, not
 *     PAV-isotonic. v2.3.6 P0-B gates knockout calibration to fall
 *     through to Dixon-Coles τ when bins are out of order — conservative
 *     failure mode. PAV enforcement should live in the Python calibration
 *     producer; tracked for v2.4 after R32.
 *
 *   No schema changes. No XLSX changes. Drop-in replacement for v2.3.10.
 *   Validated: harness adds v241Regressions — lock-acquire assertion on
 *   clearDiagnosticSurfaces, setWrap+setRowHeight assertions on
 *   _seedGoalGridHeaders_, and version-string sanity for v2.3.11.
 *
 * v2.3.10 (carried forward — closes THREE residual items from the post-v2.3.9 known-
 *   limitations review, ahead of R32 (2026-06-28, T-4d). Two MED-class items
 *   are documented as upstream-required and explicitly carried forward with
 *   the reasoning written down so the operator knows the surface caveat):
 *   - [LOW-1] _seedKellyEdgeDisclaimer_ branched on `lift < 0` for the STOP-
 *     BETTING advice. The walk-forward 2022 sample is ~2,400 fixtures so
 *     the lift sampling SE is ~0.4-0.6pp. A noise-floor lift of -0.001 (i.e.
 *     well within the SE of zero) would trigger "STOP BETTING. Set B5 = 0"
 *     and wipe out the tournament edge for a regime that is statistically
 *     indistinguishable from break-even. v2.3.10 adds LIFT_NOISE_FLOOR=0.005
 *     and a 3-band branch: lift ≤ -0.005 → STOP, -0.005 < lift < 0 → AMBIGUOUS
 *     (keep current B5, log the regime as needing watch), 0 ≤ lift ≤ 0.02 →
 *     existing "consider B5 ≈ 0.10" advice, lift > 0.02 → existing keep-
 *     quarter-Kelly confirmation. Numeric threshold is documented at the
 *     LIFT_NOISE_FLOOR const declaration with sampling-SE math inline.
 *   - [LOW-2] No operator-facing way to clear the engine's diagnostic
 *     surfaces (errLog/intelWarn/calibrationOob/D5/B82..B86) without manually
 *     clicking each cell. Pre-v2.3.10 the only path was a successful refreshAll
 *     tail (which clears errLog) or waiting for the next stamp to overwrite.
 *     v2.3.10 adds `clearDiagnosticSurfaces()` to the WC26 menu — clears all
 *     9 engine-managed cells in a single tap, toasts the count cleared.
 *     Idempotent, defensively guarded per-cell so a partially-missing sheet
 *     can't break the operation.
 *   - [LOW-3] The v2.3.9 universal regression forbade direct `_setErrLog_(`
 *     calls in any refresh* sibling but did not force the catch to write
 *     SOMETHING persistent. A future patch could ship `catch (e) { Logger.log(e);
 *     return; }` — quietly drops the failure on the floor, no operator
 *     surface. v2.3.10 extends the universal scan to enumerate every catch
 *     block inside every refresh* sibling and assert each has at least one
 *     of: bind=`_` (deliberate ignore), empty body, `throw`, `.push(`,
 *     `setValue(`, `setValues(`, `setFormula(`, or non-bare `return <expr>`.
 *     Catches that contain only `Logger.log` or a bare `return;` are flagged.
 *
 *   CARRIED-FORWARD KNOWN LIMITATIONS (documented; not closed in this engine
 *   because the fix lives upstream in the Python sim, not in Apps Script):
 *   - GOAL_GRID Poisson-vs-NB+DC drift on OU2.5/BTTS (~2-9pp). The engine
 *     can't close this because predictions_live.json publishes only λ_h/λ_a
 *     (not p_btts, p_over25, k_home, k_away). Mitigated v2.3.10 by inlining
 *     "⚠ DRIFT" warning into the GOAL_GRID A1 banner with an explicit
 *     "require ≥8pp positive edge before backing a goal market off this
 *     sheet" operator instruction. Full close requires Python-side schema
 *     change to predictions_live.json.
 *   - Walk-forward calibration LUT is binned + piecewise-linear, not isotonic
 *     enforced via PAV. Risk: at edge ranges the bin-mean calibration can be
 *     locally non-monotone if the binning produces an out-of-order bin pair.
 *     v2.3.6 P0-B already gates knockout calibration to fall through to the
 *     static Dixon-Coles τ when bins are out of monotonic order — that's a
 *     conservative failure mode. A client-side PAV pass in _calibrationBinsCached_
 *     would touch betting math at T-4d from R32 with the wrong risk/reward.
 *     Tracked for v2.4 after R32; fix should live in the Python calibration
 *     producer, not the engine.
 *
 *   No schema changes. No XLSX changes. Drop-in replacement for v2.3.9.
 *   Validated: harness adds v240Regressions with LOW-1 noise-floor band
 *   assertions, LOW-2 menu-item + helper-defined assertions, and LOW-3
 *   per-catch silent-catch detection across every refresh* sibling.
 *
 * v2.3.9 (carried forward — closes ONE residual HIGH that v2.3.8 missed by applying
 *   the HIGH-2 race fix locally to refreshDiagnostics without sweeping the
 *   same pattern across every other refresh* sibling. The v2.3.8 meta-fix
 *   discipline section explicitly said "test for blast radius, not intent" —
 *   and then v2.3.8 demonstrated the meta-pattern *again* by fixing the race
 *   only inside refreshDiagnostics and missing the structurally identical
 *   race in refreshIntel. v2.3.9 closes that, and adds a *universal*
 *   regression that walks every refresh* function body except refreshAll's
 *   tail and asserts none of them calls _setErrLog_ directly. The pattern
 *   cannot recur silently again):
 *   - [HIGH-1] refreshIntel had the SAME race that v2.3.8 HIGH-2 closed
 *     for refreshDiagnostics. The catch wrote
 *     `_setErrLog_('intel fetch failed: ' + e.message); return;` — silently
 *     returning instead of throwing. refreshAll's _tryStep_ then saw a clean
 *     exit, errors.length stayed 0, and the success tail called
 *     _clearErrLog_() ~50ms later, wiping the intel-failure surface that
 *     the catch had just written. The matchday_intelligence endpoint could
 *     fail silently for days without surfacing past the 4-sec status toast.
 *     v2.3.9 replaces the racy block with a bare `throw e;` so the standard
 *     error-collection invariant carries the failure into the parent's
 *     _setErrLog_(summary). intelWarn (B24) is still written first as a
 *     best-effort local surface; the persistent surface now comes from the
 *     parent path with no race.
 *
 *   No schema changes. No XLSX changes. Drop-in replacement for v2.3.8.
 *   Validated: harness adds v239Regressions with both the targeted assertion
 *   (refreshIntel catch contains `throw`, does NOT contain `_setErrLog_`) and
 *   the universal assertion (every refresh* body except refreshAll is scanned
 *   and must be free of direct _setErrLog_ calls). Future refresh* siblings
 *   physically cannot reintroduce this race without the harness failing.
 *
 * v2.3.8 (carried forward — closes FIVE blast-radius bugs introduced by v2.3.7's
 *   own patches. Each v2.3.x release has shipped a new self-inflicted bug
 *   because patch tests covered the patch's intent and not its side effects.
 *   v2.3.8 closes that meta-pattern by testing for blast radius explicitly.
 *   None of these block staking math; all five degrade the operator surface
 *   the v2.3.7 patches were SUPPOSED to add):
 *   - [HIGH-1] _seedKellyEdgeDisclaimer_ destructively overwrote Method!C5.
 *     v2.3.7 wrote the walk-forward edge banner to C5 — but C5 in the xlsx
 *     master already carried the canonical operator note "Quarter-Kelly.
 *     Full Kelly assumes your probabilities are exactly right — they are
 *     not." (shared string idx 331). Pre-fix, the very first install wiped
 *     that load-bearing note. v2.3.8 relocates the dynamic banner to D5
 *     (verified empty in the xlsx master) and leaves C5 untouched.
 *   - [HIGH-2] refreshDiagnostics swallowed walk_forward / calibration
 *     fetch failures internally — called _setErrLog_('… failed') but did
 *     NOT re-throw or push into refreshAll's `errors` array. The very next
 *     line of refreshAll then called _clearErrLog_() because
 *     errors.length===0, wiping the diagnostic write ~50ms after it
 *     happened. The HIGH-1 errLog surface was silently defeated by its own
 *     clear path. v2.3.8 has refreshDiagnostics collect failures into a
 *     local array and throw a combined error at the tail; _tryStep_ then
 *     picks it up via the standard error-collection invariant.
 *   - [LOW-1] _seedKellyEdgeDisclaimer_ advised "consider B5≈0.10" when
 *     lift ≤ 2%. But "lift" can go negative — model UNDERPERFORMS the
 *     baseline — and on that path B5=0.10 is still wrong; the correct
 *     advice is "STOP BETTING — set B5=0". v2.3.8 splits the branch.
 *   - [LOW-2] _seedKellyEdgeDisclaimer_ only ran at install. If the
 *     walk-forward window changed between installs (e.g. after a model
 *     retrain ships a new walk_forward.json), the banner went stale.
 *     v2.3.8 re-runs the disclaimer at the tail of refreshDiagnostics so
 *     it refreshes every POLL_MINUTES.
 *   - [LOW-3] _stampEndpoint_ wrote raw `new Date()` (locale-dependent
 *     render) while _setErrLog_ wrote UTC-formatted ISO. Mixed conventions
 *     in the same diagnostics block made it hard to compare "endpoint last
 *     200-OK at X" against "errLog stamped at Y". v2.3.8 unifies on UTC
 *     ISO via Utilities.formatDate.
 *
 *   No schema changes. No XLSX changes. Drop-in replacement for v2.3.7.
 *   Validated end-to-end: harness adds v238Regressions covering D5 write +
 *   C5 preservation, refreshDiagnostics throw-on-fail, negative-lift STOP
 *   branch, disclaimer re-run from refreshDiagnostics, UTC stamp format.
 *
 * v2.3.7 (carried forward — closes the FIVE operator-visibility / install-safety
 *   gaps surfaced by the T-4d end-to-end adversarial review of v2.3.6. None
 *   of these block staking math; all five degrade the operator's ability to
 *   notice that the engine is paused, errored, or mis-installed):
 *   - [CRIT-1] installEngine() pre-flight sheet-existence check. Pre-fix,
 *     if the operator pasted the script into the WRONG spreadsheet (one
 *     missing Bets/Method/Matchday/Live/Outrights/In-Play), refreshAll
 *     swallowed each sheet-miss in its per-step try/catch, applyProtections
 *     no-op'd silently on the absent ranges, and the toast at the end said
 *     "Setup complete". The first symptom would be "no bets are appearing"
 *     12h into R32 day. Now installEngine throws BEFORE any side effects
 *     with an operator-actionable list of the missing tabs.
 *   - [HIGH-1] Persistent fetch-error surface at Live!errLog (B28). Pre-fix
 *     a 5xx from the Vercel endpoint surfaced via _writeStatus_ as a 4-sec
 *     ephemeral toast, which the next successful refresh's status toast
 *     immediately overwrote. An operator returning to the sheet 10 min after
 *     a transient failure had no record it happened. Now the error is
 *     stamped into Live!B28 with a UTC timestamp and persists until either
 *     (a) the next successful poll of that endpoint clears it, or (b) a new
 *     error overwrites it.
 *   - [HIGH-2] Circuit-breaker / stale-feed banner mirrored onto the
 *     Matchday tab at Matchday!W1. Pre-fix, when Method!B81 tripped (stale
 *     feed or upstream circuit_breaker), the warning was written only to
 *     Live!B26. CLAUDE.md guidance says "ONLY tab you use is MATCHDAY";
 *     an operator following that workflow would never see the pause. Now
 *     the same staleMsg is also written to Matchday!W1 (well outside the
 *     A:U data range) and cleared on healthy ticks.
 *   - [HIGH-3] Walk-forward edge disclaimer at Method!C5. The Method!B5
 *     Kelly-fraction default (0.25) was calibrated against a higher
 *     expected edge. The 2022 walk-forward window shows a 1.6%
 *     lift_vs_baseline; sizing a quarter-Kelly on a 1.6% edge is
 *     ~2x the Kelly-optimal stake. The disclaimer surfaces the 2022
 *     edge + the suggested fraction (~0.10) read directly from
 *     Method!B73 (which already carries walkForward2022Lift). Operator
 *     decides — no math change, no auto-adjust.
 *   - [MED-1] Stamp every endpoint that 200s onto Method!B82..B86 with
 *     UTC timestamps. Lets the operator distinguish "endpoint is down"
 *     from "everything is fine but no bet appeared because edge < 1.6%".
 *
 *   No schema changes. No XLSX changes. Drop-in replacement for v2.3.6.
 *   Validated end-to-end: harness covers installEngine sheet-miss throw,
 *   Live!B28 errLog set/clear, Matchday!W1 mirror, Method!C5 disclaimer
 *   computation, Method!B82..B86 endpoint timestamps.
 *
 * v2.3.6 (carried forward — closes the FOUR end-to-end blockers v2.3.5 left open
 *   when audited against the actual operator workflow. R32 kickoff is
 *   2026-06-28, T-4d. Without these fixes the operator could not place a
 *   single knockout bet via the documented Matchday UX):
 *   - [CRIT-A] extendToKnockouts() now seeds Bets L:N (operator-odds mirror)
 *     for rows 74-105. v2.3.5 wired the entire downstream staking matrix
 *     (O..AQ etc.) but never seeded L:M:N — the first column it depends on.
 *     Net pre-fix: every knockout row computed "" → "" → ... → AH="· enter
 *     odds" → AJ=0 → ZERO bets sized despite all the audit-closure work
 *     done in v2.3.2-v2.3.5.
 *   - [CRIT-B] extendToKnockouts() now extends the Matchday sheet rows
 *     76-107 with the operator-input row shape (11 mirror formulas +
 *     10 operator cells). v2.3.5 left Matchday rows 76-107 entirely empty
 *     — so even with Bets L:N seeded, the Matchday-side input cells the
 *     operator types odds into didn't exist. The documented "ONLY tab you
 *     use is MATCHDAY" workflow was unreachable for knockouts.
 *   - [CRIT-C] applyProtections() Matchday allow-list extended row 75 →
 *     row 107. Pre-fix the operator was locked out of typing odds in the
 *     newly-seeded Matchday knockout rows.
 *   - [CRIT-D] onEdit() snapshot bound bumped row 75 → row 107. Pre-fix
 *     the snapshot-on-N-flip handler silently no-op'd for knockout rows,
 *     so Bets AW:AY never captured the engine decision/stake/pick when
 *     the operator marked N76="Y".
 *   - [HIGH] _knockoutStakingFormulas_ AU formula concat fixed (line 1563
 *     was missing `&` between cell ref and quoted string on two occurrences;
 *     pre-fix produced `#ERROR!` in the audit-message column on every
 *     settled knockout bet).
 *   - [MED] installEngine() now auto-runs extendToKnockouts() — no more
 *     "operator forgets the menu item at T-4d" failure mode. Idempotent.
 *
 *   Validated end-to-end: harness covers Matchday row shape, L:N mirror,
 *   protection allow-list strings, onEdit row bound, AU concat byte-exact.
 *
 * v2.3.5 (carried forward — closes seven regressions surfaced by adversarial
 *   pressure-test of v2.3.4):
 *   - [P0-A] extendToKnockouts() now seeds the full staking-formula matrix
 *     for rows 74-105 (cols O..AQ, AR..AV, AZ..BA — 36 columns of fair
 *     prob / devig / EV / pick / decision / stake / settlement /
 *     matchday-mirror formulas), preserving any existing formulas/values.
 *     Pre-v2.3.5 the function only seeded col A (match_no) + BC:BE (calib
 *     mirror), leaving 33 staking columns blank. The shared-formula refs
 *     in the source xlsx all stop at row 73 ("R2:R73", "AB2:AB73", etc.),
 *     so on first R32 tick the Bets sheet would have populated I:K but
 *     emitted zero pick/decision/stake on every knockout row. Now the
 *     seeded formulas mirror master-row 2 with the same per-row anchors.
 *   - [P0-B] _writeCalibratedProbs_ gated on group-stage rows only.
 *     The calibration LUT (calibration.json) was trained on a binned
 *     reliability curve over GROUP-STAGE matches; knockouts have no draw
 *     outcome (penalties decide ties), so the draw-market bins don't
 *     generalise. v2.3.4 applied the same LUT to m=73+. Now refreshModel()
 *     threads `matchNos` through to _writeCalibratedProbs_, which short-
 *     circuits any row whose match_no >= KNOCKOUT_FIRST_M to a raw-mirror
 *     write (I:K → BC:BE) and bumps a separate `koSkipped` counter.
 *   - [HIGH H-1] _writeCalibratedProbs_ NaN→'' guard. v2.3.4's fallback
 *     branch wrote `[rh, rd, ra]` when (ch,cd,ca) were non-finite, but
 *     `rh = Number(row[0])` returns NaN when row[0] is undefined (e.g. a
 *     missing payload field). NaN passes through setValues() unchanged and
 *     Apps Script then throws "Invalid argument: value" for NaN cells in
 *     batches that succeeded — silent until the next refreshModel tick.
 *     Now the fallback explicitly coerces NaN/non-finite → '' before write.
 *   - [HIGH H-2] OOB clamp surface moves from Live!intelWarn (B24) to a
 *     dedicated Live!calibrationOob (B27). Pre-fix, refreshIntel and
 *     _writeCalibratedProbs_ both wrote B24 in the same refreshAll tick
 *     order — whichever ran later won, and the other's content was lost.
 *     v2.3.3 MED #12 (knockout-conflict surfacer) also writes B24,
 *     compounding the race. Splitting cells eliminates the contention.
 *   - [MED M-1] OOB message includes the denominator. v2.3.4 wrote
 *     "home=2 draw=5 away=1" with no row-count context — an operator
 *     reading 5 OOB-draws can't tell whether that's 5/48 (normal — draw
 *     bin tops out at ~0.30) or 5/8 (model in distress). Now the format
 *     is "home=2/48 draw=5/48 away=1/48".
 *   - [MED M-2] refreshLive() missing-timestamp branch no longer silently
 *     swallows a concurrent circuit_breaker signal. Pre-fix, when BOTH
 *     last_updated_utc was missing AND a CB trip was in warnings[], the
 *     staleMsg branch wrote "feed missing last_updated_utc" and the more
 *     actionable CB-trip context was lost. Now the missing-ts branch
 *     checks cbTripped and joins both messages so the operator sees the
 *     CB trip (which is the real root cause; missing timestamp is a
 *     downstream symptom of the breaker pausing the sim).
 *   - [P1] refreshOutrights sort tiebreak. When p_champion=0 (knockout-
 *     eliminated teams late in the tournament, or pre-tournament teams
 *     with no projection), the comparator returned 0 and v8 Array.sort
 *     is now stable so insertion order won. That's payload-order, which
 *     varies tick-to-tick on the Python side. Now: localeCompare(team)
 *     secondary key locks display order to alphabetical on p_champion
 *     ties — no visual flicker on the Outrights sheet between ticks.
 *   - No schema changes. No XLSX changes. Drop-in replacement for v2.3.4.
 *
 * v2.3.4 (carried forward — closed six regressions in v2.3.3):
 *   - [CRIT #1] refreshLive() circuit-breaker scan recognises object-shape
 *     warnings. v2.3.3 only matched bare strings, but Python's
 *     run_live_update.py:533/556/570 emits `{"type":"circuit_breaker",
 *     "message":"..."}` objects. v2.3.3's breaker scan was therefore dead
 *     code on every real-world payload — the breaker would trip upstream
 *     and the Bets sheet would carry on staking with stale model output.
 *     v2.3.4 matches both shapes (string substring OR object .type/.message).
 *   - [HIGH #2] Live!B11 / Method!warnings cells render warning objects
 *     via .message → .type → JSON, instead of writing "[object Object]"
 *     through `.join(' · ')`. Operator-visible warning channel restored.
 *   - [HIGH #3] _writeCalibratedProbs_ fallback: when `_interp_` returns
 *     '' (non-finite input), the v2.3.3 fallback wrote `['','','']` into
 *     BC:BE. `=BC*L-1` then evaluated to #VALUE!. Now the fallback uses
 *     the row's RAW (rh, rd, ra) probabilities so the calibrated triple
 *     remains numeric.
 *   - [HIGH #4] Calibration OOB clamp surfacing: rows whose model prob
 *     falls outside the calibrator's bin range silently clamp to the
 *     last bin's actual_freq (notably draw prob > ~0.30 → 0.246). The
 *     numerical clamp is correct (no signal above the trained range);
 *     the silence was the bug. Now per-tick OOB counts are appended to
 *     Live!intelWarn so the operator sees how many rows were affected.
 *   - [MED #5] refreshLive() strict date validation. v2.3.3 used a bare
 *     truthy check on state.last_updated_utc; the literal strings
 *     'null' / 'undefined' / 'Invalid Date' (which a misbehaving
 *     upstream serializer can emit) passed the gate, then `new Date(...)`
 *     returned NaN and execution fell through to the staleKill-CLEAR
 *     branch — the opposite of safe-fail. Now: explicit string-noise
 *     filter, then parse-and-isFinite, with the missing-timestamp branch
 *     handling all non-numeric cases identically.
 *   - [MED #6] refreshOutrights() col L preservation snapshots both
 *     getValues() AND getFormulas() in parallel. v2.3.3 captured only
 *     evaluated values, so an operator-typed live formula (e.g.
 *     `=IMPORTRANGE(...)` from a private odds feed) was converted to its
 *     last evaluated number on first tick and never re-evaluated. Now
 *     formulas take precedence over values when both exist on the source
 *     row, and setFormula() restores them on the post-sort target row.
 *   - No schema changes. No XLSX changes. Drop-in replacement for v2.3.3.
 *
 * v2.3.3 (carried forward — adversarial-audit closure release on top of v2.3.2):
 *   - [CRIT #1] refreshIntel() fan-out: pre-fix v2.3.2 routed every
 *     team-level entry to the empty string match-id bucket because
 *     `Number(null) === 0` short-circuited the finite-check and `mid === 0`
 *     was treated as a real match. Match #0 has no Bets row, so the
 *     "fix" never actually fanned out. Now: explicit null-check before
 *     finiteness, and the fan-out path is the only path for team-level
 *     entries. Validated under test_harness.mjs with a synthetic intel
 *     payload (team-level null entry credits N matches, not 0).
 *   - [CRIT #2] refreshOutrights() preserves operator-typed col L (bookie
 *     odds) across re-sorts. Pre-fix: clearContent() + setValues() blew
 *     away every L cell on each tick. Now: snapshot L by team before
 *     clear, restore onto the same-team row post-sort.
 *   - [HIGH #3] GOAL_GRID docstring rewritten to admit deliberate Poisson
 *     drift vs the production sim's NB+DC marginals. Drift on OU2.5/BTTS
 *     is 2-9pp at typical λ; for high-precision markets defer to
 *     predictions_live.match_predictions[].{p_btts,p_over25,...}.
 *   - [HIGH #4] CALIBRATE docstring: claim "isotonic correction" replaced
 *     by truthful "equal-width-bin reliability lookup" (the feed ships
 *     binned reliability counts, not an isotonic regressor). Per-row
 *     renormalisation added to _writeCalibratedProbs_ so independent
 *     bin remaps don't push BC:BE to 1.07.
 *   - [HIGH #5] extendToKnockouts seeds BC:BE with blank-safe IF guards
 *     so brand-new knockout rows don't surface #N/A in the calibrated
 *     probability columns before refreshModel() catches up.
 *   - [HIGH #6] refreshAll() now wraps the full tick under
 *     LockService.getScriptLock().tryLock(0). Overlapping ticks (long
 *     UrlFetch on a 10-min cadence) used to interleave Bets/Outrights
 *     writes; now the second tick exits with a status note instead.
 *   - [HIGH #7] refreshIntel() reuses the predictions payload already
 *     fetched by refreshAll() instead of issuing a second UrlFetchApp
 *     call. Halves the per-tick fetch count for the largest endpoint.
 *   - [HIGH #8] refreshGoalGrid() skip path clears stale G:J + N:P when
 *     λ is missing. Pre-fix, a row whose λ disappeared kept its old
 *     fair-prob and BET? cells.
 *   - [MED #9 + LOW #14] refreshLive() now PAUSES the engine when
 *     last_updated_utc is missing OR when a circuit_breaker warning is
 *     present in state.warnings[]. Safe-fail.
 *   - [MED #10] refreshModel() now gates on the freshness vector being
 *     numeric and finite. Pre-fix, a sentinel string slipped through to
 *     the model writes.
 *   - [MED #11] _isOneXTwoPick_ accepts 'HOME WIN'/'AWAY WIN' alongside
 *     existing labels (the feed surfaces both phrasings).
 *   - [MED #12] extendToKnockouts surfaces conflict counts in
 *     Live!intelWarn so the operator notices without re-running the menu
 *     item.
 *   - [LOW #13] selfTest no longer writes CALIBRATION_PROP_TS — doing so
 *     resets the production TTL and masks a stale feed between scheduled
 *     ticks. The test verifies CALIBRATE math against fresh bins
 *     directly; ScriptProperties stays under refreshAll's control.
 *   - No schema changes. No XLSX changes. Drop-in replacement for v2.3.2.
 *
 * v2.3.2 (carried forward — single-CRIT bug-fix release on top of v2.3.1):
 *   - [CRIT #1] refreshIntel() no longer silently drops team-level (match_id=null)
 *     entries from /matchday_intelligence.json. Pre-fix code had
 *     `if (!isFinite(mid)) return;` which discarded every injury + stats_proxy
 *     adjustment (bundled at (team, None) per apply_matchday_adjustments.py
 *     :1040-1048). On the live snapshot that dropped 48 of 61 active_adjustments
 *     and produced wrong context-elo deltas for ~72 matches. The fix builds a
 *     (team -> [match_ids]) map from the predictions_live payload and fans
 *     tournament-wide entries out to every match where the team plays. Result
 *     mirrors apply_matchday_adjustments.get_team_elo_adjustment() exactly
 *     (validated: 72 matches non-zero delta, 0 mismatches vs Python canonical).
 *   - No schema changes. No XLSX changes. Drop-in replacement for v2.3.1.
 *
 * v2.3.1 (carried forward — bug-fix release on top of v2.3):
 *   - [CRIT #1] Stale-feed kill switch now actually trips Method!B55. Sets a
 *     boolean kill flag in Method!B81 (mirrored to ScriptProperties); the
 *     Method!B55 PAUSE formula reads B81 via OR. v2.3 only updated the
 *     cosmetic status string, never the real pause gate.
 *   - [CRIT #2] applyProtections() now adds 'BB2:BI999' to the Bets editable
 *     range (covers BC/BD/BE calibrated probs + BG/BH/BI context columns) so
 *     script-run-as-collaborator writes don't silently fail.
 *   - [CRIT #3] Bets BC:BE now seeded with =I{r}/=J{r}/=K{r} formulas in the
 *     xlsx, so calibrated columns show raw model probs immediately on import
 *     and are then overwritten with the corrected values once
 *     refreshDiagnostics() caches the bins.
 *   - [CRIT #4] xlsx now ships Outrights pre-populated with 48 teams seeded
 *     from /predictions_live.json at build time. Apps Script refreshes deltas
 *     on each run instead of creating rows from scratch.
 *   - [CRIT #5] CALIBRATE() now reads from PropertiesService.getScriptProperties()
 *     (which works in custom-function context, unlike CacheService) and never
 *     issues a UrlFetchApp call from inside a custom function. Eliminates the
 *     quota-bomb risk if =CALIBRATE is dragged across many cells.
 *   - [HIGH #6] Method!B10/B47/B50 hardcoded row 73/75 → :AR1000/:U1000/:AS1000
 *     so knockouts are picked up once Bets is extended.
 *   - [HIGH #8] refreshOutrights() BET formula now includes the Method!B55
 *     PAUSE check so outright signals stop firing when the engine is paused.
 *   - [MED #9] refreshAll() now calls refreshDiagnostics() FIRST, so the
 *     calibration bins are warm before refreshModel() writes BC:BE.
 *   - [MED #10] Each refresh* inside refreshAll() is wrapped in try/catch so
 *     a single endpoint hiccup can't take the whole chain down.
 *   - [MED #11] refreshAll() pre-fetches the two big endpoints once and
 *     passes them into model/intel/outrights/in-play, halving the
 *     UrlFetchApp call count per cycle.
 *   - [MED #12] Patch notes counts realigned (see WC26_v2.3.1_PATCH_NOTES.md).
 *   - [MED #13] xlsx ships Method!B63 blank (was the stale 'ON · every 5 min'
 *     v2.2 leftover); installAutoRefresh() rewrites it.
 *
 * Plus accuracy boosters (v2.4 candidates pulled forward):
 *   - Bets BG/BH/BI: altitude_m, climate bucket, home travel km (from
 *     match_predictions[]). Adds the context the audit flagged as missing.
 *   - Outrights now also exports p_finish_1st_group + p_finish_2nd_group +
 *     p_third_place (cols P/Q/R) so Group Winner and To-Qualify markets are
 *     covered. Group Winner is a tighter-margin market than 1X2 outrights.
 *
 * Compatible with: https://wc26-matchday-intelligence.vercel.app/ (Rounds 11-16.1).
 *
 * --------------------------------------------
 * Paste this into Extensions ▸ Apps Script, save, then run installEngine()
 * once from the menu. Approve scopes (UrlFetchApp + triggers + properties).
 */

// ---- Endpoint defaults (override in Live!B3/B4/B5 if needed) ----
const DEFAULT_LIVE_URL = 'https://wc26-matchday-intelligence.vercel.app/live_state.json';
const DEFAULT_PREDICTIONS_URL = 'https://wc26-matchday-intelligence.vercel.app/predictions_live.json';
const DEFAULT_INTEL_URL = 'https://wc26-matchday-intelligence.vercel.app/matchday_intelligence.json';
const DEFAULT_CALIBRATION_URL = 'https://wc26-matchday-intelligence.vercel.app/calibration.json';
const DEFAULT_WALK_FORWARD_URL = 'https://wc26-matchday-intelligence.vercel.app/walk_forward.json';

const POLL_MINUTES = 10;            // matches production live-matchday CF Worker
const FETCH_DEADLINE_MS = 15000;
const FETCH_RETRIES = 1;
const FETCH_BACKOFF_MS = 2000;
// v2.3.12 HIGH-1: bumped 15 → 25 to absorb a single missed upstream refresh
// cycle without tripping the kill switch. Upstream live_state.json /
// predictions_live.json refresh on a ~10-min cron; a single missed tick
// produces a ~20-min gap that's not a real outage. 25 min still trips
// inside POLL_MINUTES*3 = 30 min — well before a real stall causes harm.
const STALE_MINUTES = 25;

// v2.3.13 LOW-3: long-shot value-pick safety gate. The model's walk-forward
// calibration LUT thins out at extreme prices (Live!B27 already flags OOB
// at ~1/72 home-bin density); probabilities there are clamped to the last
// bin, which can over-state true probability in the long-shot regime, and
// EVs computed against clamped probs are over-confident. When a bet has
// odds >= LONG_SHOT_ODDS_MIN AND edge >= LONG_SHOT_EDGE_MIN, the Bets!AT
// status and Matchday!M FINAL ACTION cells show "🟡 WAIT — long-shot
// review" instead of "BET" / "PLACE", forcing the operator through a
// sharp-book + team-news + stake-comfort checklist. The operator overrides
// by setting L=Y on the Matchday row after passing the checklist.
//
// LONG_SHOT_ODDS_MIN = 6.00 → ~16.7% implied prob, traditional long-shot
// territory where overround widens and sharp markets disagree most with
// public models.
// LONG_SHOT_EDGE_MIN = 0.15 → 15pp absolute edge above which the model is
// almost certainly outside its calibration envelope for the favorite/long-
// shot regime.
const LONG_SHOT_ODDS_MIN = 6;
const LONG_SHOT_EDGE_MIN = 0.15;

// v2.3.14 KNOCKOUT GUARDS — two new auto-PASS gates for tail-prob overconfidence.
// Backtest on the first 60 graded WC26 group matches (33 BET decisions, 11W/22L):
// the model picked under-dog draws/wins at long odds where its calibrated probability
// inflated 3–5× over the de-vigged market (Germany–Curacao draw at p=0.18 vs
// market 0.04; France–Iraq away at p=0.11 vs market 0.02; Portugal–Uzbekistan
// away at p=0.13 vs market 0.05). These yielded eye-watering displayed EVs
// (60–93%) and a 0-for-16 record. Two gates kill them all without touching
// any of the eleven winners:
//
//   EV_CEILING_DEFAULT    = 0.30  — auto-PASS when AF (EV %) > 30%. Anything
//                                   higher is almost certainly model tail
//                                   over-confidence, not real edge. Operator
//                                   overrides via Method!$B$87.
//   PROB_RATIO_MAX_DEFAULT = 2.5  — auto-PASS when calibrated model_p / devigged
//                                   market_p for the PICK outcome exceeds 2.5.
//                                   The walk-forward calibration LUT only
//                                   covers group-stage 1X2; knockouts mirror
//                                   raw I:K so 4× model/market gaps are
//                                   unbacked extrapolation. Method!$B$88.
//
// Both gates are surfaced via the WHY column (Bets!AT) and FINAL ACTION column
// (Matchday!M) with the actual triggering values so the operator can see WHY
// the engine refused to bet. Operator can relax either gate by editing the
// Method cell; setting B87 to 99 or B88 to 99 effectively disables the gate.
const EV_CEILING_DEFAULT = 0.30;
const PROB_RATIO_MAX_DEFAULT = 2.5;
const M_CELL_EV_CEILING = 'B87';
const M_CELL_PROB_RATIO = 'B88';

// PropertiesService keys (work in custom-function context — Cache does not)
const CALIBRATION_PROP_KEY = 'wc26_calibration_v1';
const CALIBRATION_PROP_TS  = 'wc26_calibration_ts_v1';
const CALIBRATION_TTL_MS = 6 * 60 * 60 * 1000;  // 6h

// Sheets are looked up by name. Don't rename them.
const SHEET = {
  bets: 'Bets',
  method: 'Method',
  live: 'Live',
  matchday: 'Matchday',
  outrights: 'Outrights',
  inplay: 'In-Play',
  goalGrid: 'Goal Grid',
  clv: 'CLV',
};

// Phase 5B — Goal Grid: Poisson + Dixon-Coles τ correction.
// τ = -0.13 matches scripts/03_simulate.py:96 (negative τ boosts low-score
// draws). MAX_GOALS = 15 (R13 C3, was 10 pre-R12) gives a 16×16 matrix
// (0..15 per side) which captures the Poisson tail at high-λ matchups
// without truncation — at λ=4.0 (realistic WC high), the pre-R12 truncation
// at 10 left ~3% tail mass unaccounted, drifting GOAL_GRID prices vs the
// production sim's 16×16 by up to 2pp on outright markets. Guard rail:
// λ_max × |τ| < 1 keeps the (0,0) cell strictly positive (with λ_clip≈7,
// 7 × 0.13 = 0.91 — same safe margin used by the Python sim).
const GOAL_GRID_MAX_GOALS = 15;
const GOAL_GRID_TAU = -0.13;

// v2.3.10 LOW-1: noise floor for the walk-forward STOP-BETTING advice.
// Pre-v2.3.10, _seedKellyEdgeDisclaimer_ branched on a strict `lift < 0`
// test. The walk-forward 2022 sample is ~2,400 fixtures so the lift's
// sampling standard error is ~0.4-0.6pp (depending on regime); a tick
// reading `lift = -0.001` is statistically indistinguishable from zero
// edge but still tripped the ⛔ STOP banner and told the operator to
// zero out Method!B5. The threshold is now `lift <= -LIFT_NOISE_FLOOR`
// so only a clearly-negative edge (≥0.5pp below baseline) flips to STOP.
const LIFT_NOISE_FLOOR = 0.005;

// Phase 5C — CLV: rolling closing-line-value window. 20 bets is the smallest
// sample that exposes a real edge above noise in WC-sized markets.
const CLV_ROLLING_WINDOW = 20;

// Method cell anchors — keep in sync with the v2.3.1 XLSX layout.
const M_CELL = {
  // v2 anchors
  lastModelRefresh: 'B57',
  lastLivePoll: 'B58',
  liveMode: 'B59',
  completedMatches: 'B60',
  providerMode: 'B61',
  warnings: 'B62',
  autoStatus: 'B63',
  // v2.3 diagnostics (A66 = section header; data B67:B79)
  intelGeneratedAt: 'B67',
  intelTotalComponents: 'B68',
  intelTeamsAffected: 'B69',
  intelMatchesAffected: 'B70',
  intelCapsHit: 'B71',
  walkForwardMeanLogLoss: 'B72',
  walkForward2022Lift: 'B73',
  holdoutLogLoss: 'B74',
  holdoutLiftElo: 'B75',
  calibrationValidationScope: 'B76',
  calibrationNTest: 'B77',
  lastDiagnosticsRefresh: 'B78',
  lastIntelRefresh: 'B79',
  // v2.3.1 — stale kill flag (the boolean Method!B55 now ORs in)
  staleKill: 'B81',
  // v2.3.7 MED-1 — per-endpoint "last 200 OK" timestamps. Operator can
  // distinguish "endpoint is down" from "endpoint is fine but no edge
  // available in current matches" by scanning B82..B86 against now().
  // Each refresh* function stamps its endpoint on a 200; _fetchJson_ does
  // NOT stamp on the per-attempt level because a refresh can succeed via
  // retry-after-backoff and we want the operator-visible value to reflect
  // the function-level success, not the individual HTTP attempt.
  endpointLiveTs:       'B82',
  endpointPredsTs:      'B83',
  endpointIntelTs:      'B84',
  endpointCalibTs:      'B85',
  endpointWalkFwdTs:    'B86',
};

const LIVE_CELL = {
  // v2 anchors
  lastPoll: 'B7',
  mode: 'B8',
  completed: 'B9',
  providerMode: 'B10',
  warnings: 'B11',
  source: 'B12',
  lastModel: 'B13',
  simRerun: 'B14',
  liveTs: 'B15',
  // v2.3 intel + in-play summary
  intelGenerated: 'B19',
  intelTotal: 'B20',
  intelTeams: 'B21',
  intelMatches: 'B22',
  intelCapsHit: 'B23',
  intelWarn: 'B24',
  inPlayCount: 'B25',
  staleWarn: 'B26',
  // v2.3.5 H-2: dedicated cell for calibration OOB clamp counts.
  // Pre-fix _writeCalibratedProbs_ wrote to LIVE_CELL.intelWarn (B24),
  // racing both refreshIntel() and extendToKnockouts() conflict-tagger
  // (both also write B24 in the same refreshAll tick). Whichever ran
  // later won; the other's content was overwritten silently. Splitting
  // OOB onto its own cell eliminates the contention.
  calibrationOob: 'B27',
  // v2.3.7 HIGH-1 — persistent fetch-error log. Replaces the ephemeral
  // 4-sec toast that _writeStatus_ used to be the only surface for fetch
  // failures. Stamped with UTC timestamp on every error; cleared when the
  // corresponding endpoint next polls 200.
  errLog: 'B28',
};

// Bets column anchors — keep in sync with the v2.3.1 XLSX layout.
const BETS_COL = {
  matchNo: 1,         // A
  date: 2,            // B
  group: 3,           // C
  home: 4,            // D
  away: 5,            // E
  venue: 6,           // F
  koLocal: 7,         // G
  koUk: 8,            // H
  modelPHome: 9,      // I
  modelPDraw: 10,     // J
  modelPAway: 11,     // K
  pickedOdds: 31,     // AE
  pick: 29,           // AC
  decision: 34,       // AH
  engineStake: 36,    // AJ
  placed: 40,         // AN
  backedOdds: 41,     // AO
  snapDecision: 49,   // AW
  snapStake: 50,      // AX
  snapPick: 51,       // AY
  // v2.3 intel + calibration block
  intelDelta: 54,     // BB  total elo Δ across all teams in this match
  pHomeCorr: 55,      // BC  isotonic-corrected pH
  pDrawCorr: 56,      // BD
  pAwayCorr: 57,      // BE
  intelBreakdown: 58, // BF  human-readable component list
  // v2.3.1 context block (read once at install, rarely changes)
  altitudeM: 59,      // BG  altitude_m
  climate: 60,        // BH  climate bucket
  homeTravelKm: 61,   // BI  home_travel_km
};
const BETS_HEADER_ROW = 1;
const BETS_FIRST_DATA_ROW = 2;

// =============================================================================
// MENU + INSTALL
// =============================================================================

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('WC26 Engine')
    .addItem('Refresh model now', 'refreshModel')
    .addItem('Refresh live state now', 'refreshLive')
    .addItem('Refresh intel now', 'refreshIntel')
    .addItem('Refresh diagnostics now', 'refreshDiagnostics')
    .addItem('Refresh outrights now', 'refreshOutrights')
    .addItem('Refresh in-play now', 'refreshInPlay')
    .addItem('Refresh goal grid now', 'refreshGoalGrid')
    .addItem('Refresh CLV now', 'refreshCLV')
    .addItem('Refresh context (altitude/climate/travel)', 'refreshMatchContext')
    .addItem('Refresh fixtures/matches now', 'refreshFixtures')
    .addItem('Refresh everything', 'refreshAll')
    .addItem('Snapshot existing placed bets', 'snapshotExisting')
    .addSeparator()
    .addItem('Extend to knockout stage (run once before R32)', 'extendToKnockouts')
    .addItem('Apply group formula fixes (calibration + guards)', 'applyTailTrapGuardsToGroups')
    .addSeparator()
    .addItem('Install auto-refresh (every ' + POLL_MINUTES + ' min)', 'installAutoRefresh')
    .addItem('Remove auto-refresh', 'removeAutoRefresh')
    .addSeparator()
    .addItem('Lock formulas (apply protections)', 'applyProtections')
    .addItem('Unlock all (remove protections)', 'removeProtections')
    .addItem('Clear diagnostic surfaces (errLog + edge banner + stamps)',
             'clearDiagnosticSurfaces')
    .addSeparator()
    .addItem('First-time setup (run once)', 'installEngine')
    .addItem('Engine self-test', 'selfTest')
    .addToUi();
}

function installEngine() {
  // v2.3.7 CRIT-1: pre-flight sheet-existence check. Pre-fix, if the
  // operator pasted the script into the wrong spreadsheet (e.g. a copy
  // missing one of the six required tabs), refreshAll's per-step
  // try/catch swallowed each "Sheet X not found" throw, applyProtections
  // no-op'd silently on the absent ranges, and the final toast still
  // said "Setup complete". The first symptom would be "no bets are
  // appearing" 12h into R32 day. Validate up front and fail loudly with
  // an operator-actionable list of the missing tabs. Order matters: this
  // MUST run before any side-effect call (refreshAll, applyProtections,
  // extendToKnockouts, snapshotExisting all assume these exist).
  _preflightSheets_();

  _seedGoalMarketsMinEdge_();     // one-time: Method!B8 ← Method!B7 if blank
  _seedKnockoutGuards_();         // v2.3.14: Method!B87 / B88 auto-PASS gates
  _seedPauseOverrideHelp_();      // clarify B54 only bypasses drawdown
  refreshAll();
  refreshMatchContext();          // one-time write of altitude/climate/travel
  // v2.3.6: auto-extend to knockouts at install time. Pre-fix the operator
  // had to discover and manually run "Extend to knockout stage" from the
  // menu before R32 — easy to forget at T-4d. Idempotent + safe to re-run.
  extendToKnockouts();
  snapshotExisting();
  installAutoRefresh();
  applyProtections();
  // v2.3.7 HIGH-3 / v2.3.8 HIGH-1: seed the walk-forward edge disclaimer
  // beside the Kelly fraction cell (Method!B5). Banner targets Method!D5
  // (v2.3.7 hit C5 destructively — C5 carries the canonical operator
  // Quarter-Kelly note in the xlsx master). Method!B73 carries
  // walkForward2022Lift set by refreshDiagnostics → refreshAll above.
  _seedKellyEdgeDisclaimer_();
  // v2.3.15 PATCH-FIX: extend the v2.3.14 tail-trap guards to group rows
  // (Bets 2..73) at install time. Pre-fix, applyTailTrapGuardsToGroups was
  // only wired to the WC26 menu item added in v2.3.14 (line ~825), so an
  // operator running a fresh First-time setup got the guards on knockout
  // rows (74..105, via _knockoutStakingFormulas_) but NOT on the group
  // window — leaving the same tail-trap over-bet risk on any pending group
  // fixture until the operator remembered to click the menu item. Wrap in
  // try/catch matching the _preflightSheets_ failure-loud pattern: a
  // missing Bets sheet has already thrown above, so the only realistic
  // failure here is a transient setFormulas race; log and continue so a
  // partial group-guard upgrade doesn't brick the rest of install. The
  // helper is idempotent (signature-check on Method!$B$87) — safe to
  // re-run from the menu if this step is skipped.
  try {
    applyTailTrapGuardsToGroups();
  } catch (e) {
    Logger.log('installEngine: applyTailTrapGuardsToGroups failed: ' + e);
  }
  SpreadsheetApp.getActive().toast(
    'WC26 Engine v2.3.13 installed (knockouts pre-seeded, sheets verified). ' +
      'Auto-refresh every ' + POLL_MINUTES + ' min.',
    'Setup complete',
    8
  );
}

/**
 * v2.3.7 CRIT-1: pre-flight check that every required sheet exists.
 * Throws with an operator-actionable list if any are missing — pasting
 * the script into the wrong spreadsheet is the most common install-time
 * failure mode and the v2.3.6 toast at the end silently masked it.
 */
function _preflightSheets_() {
  const ss = SpreadsheetApp.getActive();
  const required = ['bets', 'method', 'live', 'matchday', 'outrights', 'inplay'];
  const missing = [];
  required.forEach(function(key) {
    const name = SHEET[key];
    if (!name) { missing.push('(SHEET.' + key + ' not configured)'); return; }
    if (!ss.getSheetByName(name)) missing.push(name);
  });
  if (missing.length) {
    const msg = 'WC26 Engine install aborted — missing required sheet(s): ' +
                missing.join(', ') +
                '. Open the WC26_Value_Betting_Engine_AUTOMATED_v2.3.1.xlsx ' +
                'workbook and paste this script there, OR rename your existing ' +
                'tabs to match.';
    try { ss.toast(msg, 'WC26 Engine v2.3.13 install error', 30); } catch (_) {}
    throw new Error(msg);
  }
}

/**
 * v2.3.8 HIGH-1 + LOW-1: surface a Kelly-fraction edge disclaimer beside
 * Method!B5. Reads Method!B73 (walk_forward 2022 lift_vs_baseline) which
 * is populated by refreshDiagnostics, computes a Kelly-aligned suggestion,
 * and writes a human-readable banner to **Method!D5**.
 *
 * v2.3.7 wrote to C5 destructively, but C5 in the xlsx master carries
 * the canonical operator note "Quarter-Kelly. Full Kelly assumes your
 * probabilities are exactly right — they are not." (shared string idx
 * 331). Verified D5 empty in the xlsx, so the dynamic banner moves there
 * — left-to-right reads as: B5 (value) | C5 (static guidance) | D5
 * (dynamic edge-aligned advice). No destructive write.
 *
 * v2.3.8 LOW-1: lift can be NEGATIVE — model UNDERPERFORMS baseline. On
 * that path, "consider B5≈0.10" is still wrong — the correct advice is
 * "STOP BETTING — set B5=0". Branched explicitly.
 *
 * v2.3.8 LOW-2: also called from the tail of refreshDiagnostics so the
 * banner refreshes every POLL_MINUTES instead of only at install. The
 * write is idempotent (always overwrites D5 with the freshest read).
 *
 * The 2022 walk-forward lift is ~1.6% — sizing a quarter-Kelly (B5=0.25)
 * on a 1.6% edge is roughly 2x the Kelly-optimal stake. Kelly-optimal
 * for a small edge ε scales as ε/var; quarter-Kelly was a safer choice
 * back when the walk-forward edge looked closer to 5%.
 */
function _seedKellyEdgeDisclaimer_() {
  const ss = SpreadsheetApp.getActive();
  const method = ss.getSheetByName(SHEET.method);
  if (!method) return;
  const target = method.getRange('D5');
  const lift = _num_(method.getRange(M_CELL.walkForward2022Lift).getValue());
  if (!isFinite(lift)) {
    target.setValue(
      'Walk-forward edge not yet read (Diagnostics pending). Default Kelly ' +
      'fraction B5=0.25 — banner will refresh on next 10-min diagnostics tick.');
    return;
  }
  const liftPct = (lift * 100).toFixed(2) + '%';
  const noiseFloorPct = (LIFT_NOISE_FLOOR * 100).toFixed(2) + '%';
  let banner;
  if (lift <= -LIFT_NOISE_FLOOR) {
    // v2.3.8 LOW-1: model worse than baseline. Kelly-fraction guidance is
    // not "use a smaller fraction"; it is "do not bet at all" — any
    // positive stake on a negative edge is -EV. Spell it out.
    // v2.3.10 LOW-1: gated on -LIFT_NOISE_FLOOR (=-0.5%) rather than 0.
    // The walk-forward sample SE is ~0.4-0.6pp; a strict <0 test on a
    // -0.1% reading is noise, not signal — but it still flipped the
    // ⛔ STOP banner and asked the operator to zero B5. Tighter gate.
    banner = '⛔ STOP BETTING. Walk-forward 2022 edge = ' + liftPct +
             ' (model UNDERPERFORMS baseline by ≥' + noiseFloorPct +
             '). Set Method!B5 = 0 until the next model retrain ships ' +
             'a positive lift_vs_baseline.';
  } else if (lift < 0) {
    // v2.3.10 LOW-1: small-negative ambiguity zone (-LIFT_NOISE_FLOOR, 0).
    // Indistinguishable from zero edge at the walk-forward sample SE,
    // so the right advice is the same conservative shrinkage we give
    // for tiny positive edges — not STOP.
    banner = '⚠ Walk-forward 2022 edge = ' + liftPct +
             ' (within ±' + noiseFloorPct + ' noise floor — indistinguishable ' +
             'from zero). Consider B5≈0.10 (one-tenth Kelly) until next retrain.';
  } else if (lift <= 0.02) {
    banner = '⚠ Walk-forward 2022 edge = ' + liftPct +
             '. Quarter-Kelly (B5=0.25) is ~2x optimal on this edge — ' +
             'consider B5≈0.10 (one-tenth Kelly) until edge improves.';
  } else {
    banner = 'Walk-forward 2022 edge = ' + liftPct +
             '. Current Kelly fraction B5=' + method.getRange('B5').getValue() +
             ' looks aligned. Re-check after each diagnostics refresh.';
  }
  target.setValue(banner);
}

/**
 * One-time seed: Method!B8 = goal_markets_min_edge. Defaults to whatever
 * Method!B7 (1X2 min_edge) currently holds so live behavior is unchanged at
 * install. Operator can dial it independently once they've watched O/U +
 * BTTS pick distributions for a few matchdays. Idempotent: skips if B8 is
 * already populated.
 */
function _seedGoalMarketsMinEdge_() {
  const ss = SpreadsheetApp.getActive();
  const method = ss.getSheetByName(SHEET.method);
  if (!method) return;
  const b8 = method.getRange('B8').getValue();
  if (b8 !== '' && b8 !== null && isFinite(Number(b8))) return;
  const b7 = method.getRange('B7').getValue();
  if (b7 === '' || b7 === null || !isFinite(Number(b7))) return;
  method.getRange('B8').setValue(b7);
  method.getRange('A8').setValue('goal_markets_min_edge');
}

/**
 * v2.3.14 KNOCKOUT GUARDS — one-time seed for the two new auto-PASS knobs.
 * Idempotent: skips any cell that already holds a finite number, so the
 * operator can dial these freely after install without the next refresh
 * overwriting their tuning.
 *
 * B87 = EV ceiling (auto-PASS above)            default 0.30
 * B88 = max calibrated model_p / devigged market_p     default 2.5
 *
 * The labels in col A and the help text in col C are written unconditionally
 * (cheap, and keeps the layout self-documenting after a fresh install).
 */
function _seedKnockoutGuards_() {
  const ss = SpreadsheetApp.getActive();
  const method = ss.getSheetByName(SHEET.method);
  if (!method) return;
  const evCell = method.getRange(M_CELL_EV_CEILING);
  const ratioCell = method.getRange(M_CELL_PROB_RATIO);
  const evCur = evCell.getValue();
  const ratioCur = ratioCell.getValue();
  if (evCur === '' || evCur === null || !isFinite(Number(evCur))) {
    evCell.setValue(EV_CEILING_DEFAULT);
  }
  if (ratioCur === '' || ratioCur === null || !isFinite(Number(ratioCur))) {
    ratioCell.setValue(PROB_RATIO_MAX_DEFAULT);
  }
  method.getRange('A' + M_CELL_EV_CEILING.slice(1)).setValue('EV ceiling (auto-PASS above)');
  method.getRange('A' + M_CELL_PROB_RATIO.slice(1)).setValue('Max model/market prob ratio');
  method.getRange('C' + M_CELL_EV_CEILING.slice(1)).setValue(
    'Auto-PASS when displayed EV exceeds this. 0.30 = 30%. Above this is almost ' +
    'always model tail over-confidence at long odds — backtest on the first 60 ' +
    'WC26 group matches killed 9 BETs at 0W/9L with no winners lost. Set to 99 ' +
    'to disable.'
  );
  method.getRange('C' + M_CELL_PROB_RATIO.slice(1)).setValue(
    'Auto-PASS when calibrated model_p / devigged market_p for the picked outcome ' +
    'exceeds this ratio. 2.5 = model claims the outcome is 2.5× more likely ' +
    'than the market thinks. Knockouts mirror raw I:K (no calibration LUT), so ' +
    'tail extrapolation rides without a safety net otherwise. Set to 99 to disable.'
  );
}

function _seedPauseOverrideHelp_() {
  const ss = SpreadsheetApp.getActive();
  const method = ss.getSheetByName(SHEET.method);
  if (!method) return;
  method.getRange('C54').setValue(
    'Type Y to keep betting through drawdown only. Stale-feed and circuit-breaker pauses remain hard stops.'
  );
  method.getRange('C55').setValue(
    'TRUE pauses staking when drawdown is tripped without B54=Y, or when Live! feed/circuit-breaker safety is active.'
  );
}

/**
 * v2.3.14 KNOCKOUT GUARDS — extend the two new auto-PASS gates to the
 * group-stage rows (Bets 2..73). The xlsx ships group rows with the legacy
 * AH/AT formulas baked in, so until this runs only knockout rows (74..105,
 * written by _knockoutStakingFormulas_) get the new safety net. This helper
 * overwrites only managed formulas on the group window: Bets!V:X
 * (blended probabilities), Bets!AH (DECISION), Bets!AT (WHY), and
 * Matchday!M (FINAL ACTION). Operator-entry cells stay untouched.
 *
 * Idempotent: re-runs write the same formula set and report already-correct
 * cells as skipped. Backtest on the 33 already-graded BETs shows the guard
 * would have killed 9 of 22 losses with 0 winners lost.
 */
function applyTailTrapGuardsToGroups() {
  const ss = SpreadsheetApp.getActive();
  const bets = ss.getSheetByName(SHEET.bets);
  if (!bets) throw new Error('Sheet "' + SHEET.bets + '" not found');
  // v2.3.14 PATCH-FIX: seed B87/B88 BEFORE writing any guarded formula.
  // Sheets coerces a blank cell to 0 in numeric comparison, so an empty
  // Method!$B$87 turns "AF > B87" into "AF > 0" → every positive-EV row
  // auto-PASSes with "Tail trap". Without this call, an operator who
  // pastes the new .gs and clicks this menu item before First-time setup
  // ends up with a workbook that refuses to bet on anything. The seed is
  // idempotent — pre-existing operator-tuned values are preserved.
  _seedKnockoutGuards_();
  _seedPauseOverrideHelp_();
  const firstRow = BETS_FIRST_DATA_ROW;       // 2
  const lastRow = KNOCKOUT_FIRST_ROW - 1;     // 73
  const nRows = lastRow - firstRow + 1;
  // Read existing formulas for idempotent counts. These managed cells are
  // formula-owned; operator-entry cells are outside these ranges.
  const vxRange = bets.getRange(firstRow, 22, nRows, 3);  // cols V:X
  const ahRange = bets.getRange(firstRow, 34, nRows, 1);  // col AH
  const atRange = bets.getRange(firstRow, 46, nRows, 1);  // col AT
  const vxCur = vxRange.getFormulas();
  const ahCur = ahRange.getFormulas();
  const atCur = atRange.getFormulas();
  const vxOut = [];
  const ahOut = [];
  const atOut = [];
  let upgraded = 0, skipped = 0, matchdayUpgraded = 0, matchdaySkipped = 0;
  for (let i = 0; i < nRows; i++) {
    const r = firstRow + i;
    const blocks = _knockoutStakingFormulas_(r);
    const existingVx = vxCur[i];
    const targetVx = [blocks.O_AQ[7], blocks.O_AQ[8], blocks.O_AQ[9]];
    vxOut.push(targetVx);
    for (let j = 0; j < targetVx.length; j++) {
      if (existingVx[j] === targetVx[j]) skipped++;
      else upgraded++;
    }
    // _knockoutStakingFormulas_(r).O_AQ is the 29-col block starting at O.
    // Block indices: 19 = AH (DECISION), and AR..AV.AT lives in AR_AV
    // block at index 2.
    const targetAh = blocks.O_AQ[19];
    const targetAt = blocks.AR_AV[2];
    ahOut.push([targetAh]);
    atOut.push([targetAt]);
    if (ahCur[i][0] === targetAh) skipped++;
    else upgraded++;
    if (atCur[i][0] === targetAt) skipped++;
    else upgraded++;
  }
  vxRange.setFormulas(vxOut);
  ahRange.setFormulas(ahOut);
  atRange.setFormulas(atOut);

  const md = ss.getSheetByName(SHEET.matchday);
  if (md) {
    const mdRange = md.getRange(firstRow + 2, 13, nRows, 1); // Matchday M4:M75
    const mdCur = mdRange.getFormulas();
    const mdOut = [];
    for (let i = 0; i < nRows; i++) {
      const r = firstRow + i;
      const target = _matchdayKnockoutRowFormulas_(r)[12];
      mdOut.push([target]);
      if (mdCur[i][0] === target) matchdaySkipped++;
      else matchdayUpgraded++;
    }
    mdRange.setFormulas(mdOut);
  }

  _writeStatus_('Group formula upgrade: upgraded ' + upgraded +
                ' Bets formula cell(s), skipped ' + skipped +
                ', Matchday!M upgraded ' + matchdayUpgraded +
                ', skipped ' + matchdaySkipped);
}

/**
 * Master refresher. Order matters:
 *   1. Diagnostics — caches calibration bins so refreshModel can write BC:BE
 *      with corrected probs on this same cycle (fix MED #9).
 *   2. One predictions fetch — reused by refreshModel + refreshOutrights (fix MED #11).
 *   3. One live fetch — reused by refreshLive + refreshInPlay (fix MED #11).
 *   4. Each step in its own try/catch (fix MED #10).
 */
function refreshAll() {
  // v2.3.3 HIGH #6: LockService overlap guard. POLL_MINUTES=10 normally
  // leaves headroom for refreshAll (~30-90s), but when the upstream feed
  // is slow or a single _fetchJson_ retries through its backoff window,
  // two refreshAll triggers can overlap. Without a lock, the second
  // invocation re-clears cells the first is still writing → setValues
  // races → empty rows on Bets. tryLock(0) means "don't wait" — if a
  // refresh is already running, this tick bails immediately and the
  // next tick (10 min later) catches up.
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(0)) {
    _writeStatus_('refreshAll: skipped — previous tick still running (10-min back-off)');
    return;
  }

  try {
    const ss = SpreadsheetApp.getActive();
    const errors = [];

    // v2.3.14 PATCH-FIX: defensive seed of Method!B87 / B88 on every refresh.
    // installEngine seeds these once, but operators who paste the v2.3.14 code
    // into an existing project (the common case — re-running First-time setup
    // risks clobbering tuned settings) never trigger that path. Without seeded
    // values, blank cells coerce to 0 in numeric comparison and the new
    // "AF > Method!$B$87" gate fires on every positive-EV row → engine PASSes
    // on everything. Seeder is idempotent (only fills blank/non-numeric cells)
    // and cheap (two getValue + at most two setValue calls), so running it
    // inside the lock-guarded tick costs nothing on the steady state.
    _tryStep_(errors, 'seedGuards', function() { _seedKnockoutGuards_(); });

    _tryStep_(errors, 'diagnostics', function() { refreshDiagnostics(); });

    // Pre-fetch shared payloads once.
    let predictions = null;
    try {
      predictions = _fetchJson_(_predictionsUrl_(ss));
      _stampEndpoint_(M_CELL.endpointPredsTs);
    }
    catch (e) { errors.push('predictions fetch: ' + e.message); }

    let liveState = null;
    try {
      liveState = _fetchJson_(_liveUrl_(ss));
      _stampEndpoint_(M_CELL.endpointLiveTs);
    }
    catch (e) { errors.push('live fetch: ' + e.message); }

    // v2.3.16 PATCH-FIX: make ordinary refresh ticks self-heal existing
    // sheets for knockouts. extendToKnockouts creates rows 74..105 when an
    // operator only pasted a newer .gs into an older workbook; refreshFixtures
    // then imports B:H match metadata so Matchday can show R32 team names.
    _tryStep_(errors, 'extendKnockouts', function() { extendToKnockouts(); });
    _tryStep_(errors, 'knockoutFormulas', function() { _upgradeKnockoutManagedFormulas_(); });
    _tryStep_(errors, 'fixtures',  function() { refreshFixtures(predictions); });
    _tryStep_(errors, 'context',   function() { refreshMatchContext(predictions); });
    _tryStep_(errors, 'model',     function() { refreshModel(predictions); });
    // v2.3.3 HIGH #7: thread predictions payload into refreshIntel so it
    // doesn't re-fetch the same JSON we just pulled. Saves one UrlFetchApp
    // call per tick (144/day at POLL_MINUTES=10) — meaningful headroom on
    // the 20k/day quota when ops uses Sheets in parallel with other apps.
    _tryStep_(errors, 'intel',     function() { refreshIntel(predictions); });
    _tryStep_(errors, 'live',      function() { refreshLive(liveState); });
    _tryStep_(errors, 'outrights', function() { refreshOutrights(predictions); });
    _tryStep_(errors, 'in-play',   function() { refreshInPlay(liveState); });
    _tryStep_(errors, 'goalGrid',  function() { refreshGoalGrid(predictions); });
    _tryStep_(errors, 'clv',       function() { refreshCLV(); });

    if (errors.length) {
      const summary = 'refreshAll: ' + errors.length + ' step(s) failed: ' + errors.join(' | ');
      _writeStatus_(summary);
      // v2.3.7 HIGH-1: stamp persistent errLog so the operator sees the
      // failure even after the 4-sec toast disappears. Includes UTC
      // timestamp so a healed-then-broken-again pattern is visible.
      _setErrLog_(summary);
    } else {
      // v2.3.7 HIGH-1: clear errLog on a clean tick. Pre-fix the cell
      // would carry a stale error indefinitely once written.
      _clearErrLog_();
    }
  } finally {
    lock.releaseLock();
  }
}

/**
 * v2.3.7 HIGH-1: persistent error surface helpers. The 4-sec toast that
 * _writeStatus_ produces is invisible to an operator who's not staring at
 * the sheet at the moment a poll fails. Live!B28 retains the last error
 * with a UTC timestamp until either (a) the next clean refreshAll wipes
 * it or (b) a new error overwrites it.
 */
function _setErrLog_(msg) {
  try {
    const ss = SpreadsheetApp.getActive();
    const live = ss.getSheetByName(SHEET.live);
    if (!live) return;
    const ts = Utilities.formatDate(new Date(), 'UTC', "yyyy-MM-dd'T'HH:mm:ss'Z'");
    live.getRange(LIVE_CELL.errLog).setValue('[' + ts + '] ' + msg);
  } catch (_) {}
}

function _clearErrLog_() {
  try {
    const ss = SpreadsheetApp.getActive();
    const live = ss.getSheetByName(SHEET.live);
    if (!live) return;
    live.getRange(LIVE_CELL.errLog).setValue('');
  } catch (_) {}
}

/**
 * v2.3.10 LOW-2: operator-triggered clear of every engine-managed
 * diagnostic surface. Wired into the WC26 Engine menu so the operator
 * can reset stale banners after a known-good redeploy without having
 * to remove the entire protection set (`removeProtections`) first.
 * The script owns the protection (it applied it), so setValue() on a
 * script-protected range still succeeds in script context.
 *
 * Surfaces cleared:
 *   - Live!B28 (LIVE_CELL.errLog) — refreshAll's persistent error band.
 *   - Live!B24 (LIVE_CELL.intelWarn) — refreshIntel best-effort warn.
 *   - Live!B27 (LIVE_CELL.calibrationOob) — calibration OOB-clamp warn.
 *   - Method!D5 — walk-forward edge banner from _seedKellyEdgeDisclaimer_.
 *   - Method!B82..B86 — endpoint UTC ISO timestamps from _stampEndpoint_.
 *
 * Operator-owned cells (Method!C5 static guidance, Method!B5 kelly knob,
 * etc.) are NEVER touched here — only cells the engine itself writes.
 * The toast confirms the count of cells cleared so the operator can
 * spot a silent permission failure.
 */
function clearDiagnosticSurfaces() {
  // v2.3.11 MED-A: acquire the same script lock that refreshAll takes at
  // line ~781. Pre-v2.3.11 the operator could click this menu item while a
  // 10-min trigger tick was mid-flight: setValue('') on Live!B28 here ran
  // ~50ms before refreshAll's tail wrote _setErrLog_(summary), so the
  // operator saw a "Cleared 9 surfaces" toast and then the error
  // reappeared a second later. tryLock(0) means "don't wait": if a refresh
  // is in flight, bail and tell the operator to retry after POLL_MINUTES.
  // Same lock the parent owns → zero risk of clobbering an in-progress
  // write. No deadlock risk: the menu handler is a discrete entrypoint, it
  // doesn't itself call refreshAll.
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(0)) {
    const ss0 = SpreadsheetApp.getActive();
    try {
      ss0.toast(
        'refreshAll in flight — wait ' + POLL_MINUTES +
        ' min and retry. No surfaces cleared.',
        'WC26 Engine v2.3.13', 5);
    } catch (_) {}
    return;
  }
  try {
    const ss = SpreadsheetApp.getActive();
    const live = ss.getSheetByName(SHEET.live);
    const method = ss.getSheetByName(SHEET.method);
    let cleared = 0;
    const tryClear = function(sheet, a1) {
      if (!sheet || !a1) return;
      try { sheet.getRange(a1).setValue(''); cleared++; } catch (_) {}
    };
    tryClear(live,   LIVE_CELL.errLog);            // B28
    tryClear(live,   LIVE_CELL.intelWarn);         // B24
    tryClear(live,   LIVE_CELL.calibrationOob);    // B27
    tryClear(method, 'D5');                        // walk-forward edge banner
    tryClear(method, M_CELL.endpointLiveTs);       // B82
    tryClear(method, M_CELL.endpointPredsTs);      // B83
    tryClear(method, M_CELL.endpointIntelTs);      // B84
    tryClear(method, M_CELL.endpointCalibTs);      // B85
    tryClear(method, M_CELL.endpointWalkFwdTs);    // B86
    tryClear(method, M_CELL.autoStatus);           // B63 (v2.3.13 LOW-2 — refreshLive will repopulate next tick)
    try {
      ss.toast(
        'Cleared ' + cleared + ' diagnostic surfaces. Next refreshAll tick ' +
        'will repopulate any that the engine still writes.',
        'WC26 Engine v2.3.13', 5);
    } catch (_) {}
  } finally {
    lock.releaseLock();
  }
}

/**
 * v2.3.7 MED-1 / v2.3.8 LOW-3: stamp Method!B82..B86 on each successful
 * endpoint poll. Operator can scan now() vs these cells to distinguish
 * "endpoint down" from "endpoint healthy but no match-edge in the current
 * window". v2.3.8: write UTC ISO via Utilities.formatDate to match the
 * convention _setErrLog_ already uses on Live!B28. Pre-v2.3.8 we wrote
 * raw `new Date()` which renders in the spreadsheet locale (often
 * sheet-author timezone) — mixed UTC vs local in the same diagnostics
 * block made cross-cell timing comparisons unreliable.
 */
function _stampEndpoint_(cell) {
  try {
    const ss = SpreadsheetApp.getActive();
    const method = ss.getSheetByName(SHEET.method);
    if (!method) return;
    const ts = Utilities.formatDate(new Date(), 'UTC', "yyyy-MM-dd'T'HH:mm:ss'Z'");
    method.getRange(cell).setValue(ts);
  } catch (_) {}
}

function _tryStep_(errors, name, fn) {
  try { fn(); }
  catch (e) {
    errors.push(name + ': ' + (e.message || e));
    Logger.log('Step ' + name + ' failed: ' + e);
  }
}

// =============================================================================
// FIXTURE REFRESH — pulls /predictions_live.json metadata into Bets!B:H
// =============================================================================

function refreshFixtures(payload) {
  const ss = SpreadsheetApp.getActive();
  const bets = ss.getSheetByName(SHEET.bets);
  if (!bets) throw new Error('Sheet "' + SHEET.bets + '" not found');

  if (!payload) payload = _fetchJson_(_predictionsUrl_(ss));
  const matchPreds = Array.isArray(payload && payload.match_predictions)
    ? payload.match_predictions : [];
  if (!matchPreds.length) {
    _writeStatus_('Fixture refresh: no match_predictions in feed');
    return;
  }

  const fixturesByM = {};
  matchPreds.forEach(function(mp) {
    const m = Number(mp.m);
    if (!isFinite(m)) return;
    fixturesByM[m] = _fixtureMetadataRow_(mp);
  });

  const lastRow = _findLastBetsRow_(bets);
  const nRows = lastRow - BETS_FIRST_DATA_ROW + 1;
  if (nRows <= 0) {
    _writeStatus_('Fixture refresh: Bets sheet has no data rows');
    return;
  }

  const matchNos = bets.getRange(BETS_FIRST_DATA_ROW, BETS_COL.matchNo, nRows, 1).getValues();
  const current = bets.getRange(BETS_FIRST_DATA_ROW, BETS_COL.date, nRows, 7).getValues();
  const out = [];
  let updated = 0, preserved = 0, missing = 0, conflicts = 0;

  for (let i = 0; i < nRows; i++) {
    const m = Number(matchNos[i][0]);
    const desired = fixturesByM[m];
    const curRow = current[i];
    if (!desired) {
      out.push(curRow);
      missing++;
      continue;
    }

    const nextRow = [];
    for (let c = 0; c < 7; c++) {
      const choice = _fixtureCellChoice_(curRow[c], desired[c]);
      nextRow.push(choice.value);
      if (choice.action === 'updated') updated++;
      else if (choice.action === 'conflict') conflicts++;
      else preserved++;
    }
    out.push(nextRow);
  }

  bets.getRange(BETS_FIRST_DATA_ROW, BETS_COL.date, out.length, 7).setValues(out);
  if (conflicts) {
    _appendLiveIntelWarning_('fixture sync: ' + conflicts + ' nonblank conflict cell(s) preserved');
  }
  _writeStatus_('Fixture refresh: updated ' + updated + ', preserved ' +
                preserved + ', missing ' + missing + ', conflicts ' + conflicts);
}

function _fixtureMetadataRow_(mp) {
  return [
    _fixtureDate_(mp.date),
    _fixtureStageLabel_(mp.group || mp.stage),
    _cleanFixtureText_(mp.home),
    _cleanFixtureText_(mp.away),
    _cleanFixtureText_(mp.venue),
    _cleanFixtureText_(mp.time || mp.ko_local || mp.kickoff_local || mp.local_time),
    '',
  ];
}

function _fixtureDate_(value) {
  if (value === null || value === undefined || value === '') return '';
  const s = String(value).trim();
  return s;
}

function _fixtureStageLabel_(value) {
  const raw = _cleanFixtureText_(value);
  if (!raw) return '';
  const key = raw.toLowerCase().replace(/[\s_.-]+/g, '');
  const map = {
    r32: 'R32',
    roundof32: 'R32',
    r16: 'R16',
    roundof16: 'R16',
    qf: 'QF',
    quarterfinal: 'QF',
    quarterfinals: 'QF',
    sf: 'SF',
    semifinal: 'SF',
    semifinals: 'SF',
    '3rd': '3rd',
    third: '3rd',
    thirdplace: '3rd',
    final: 'Final',
  };
  return map[key] || raw;
}

function _fixtureCellChoice_(current, desired) {
  if (desired === null || desired === undefined || desired === '') {
    return { value: current, action: 'preserved' };
  }
  if (_isBlankCell_(current) || _isFixturePlaceholder_(current)) {
    return { value: desired, action: 'updated' };
  }
  if (_normalFixtureCell_(current) === _normalFixtureCell_(desired)) {
    return { value: current, action: 'preserved' };
  }
  return { value: current, action: 'conflict' };
}

function _isBlankCell_(value) {
  return value === null || value === undefined || value === '';
}

function _isFixturePlaceholder_(value) {
  if (_isBlankCell_(value)) return true;
  const s = String(value).trim().toUpperCase();
  if (!s) return true;
  if (s === 'TBD' || s === 'TBC' || s === 'BRACKET TBD') return true;
  if (/^W\d{1,3}$/.test(s)) return true;
  if (/^[123][A-L](?:\/[A-L])*$/.test(s)) return true;
  return false;
}

function _normalFixtureCell_(value) {
  if (value instanceof Date) {
    return Utilities.formatDate(value, Session.getScriptTimeZone(), 'yyyy-MM-dd');
  }
  return _cleanFixtureText_(value).replace(/\s+/g, ' ').toLowerCase();
}

function _cleanFixtureText_(value) {
  if (value === null || value === undefined) return '';
  return String(value).trim();
}

function _appendLiveIntelWarning_(msg) {
  try {
    const live = SpreadsheetApp.getActive().getSheetByName(SHEET.live);
    if (!live) return;
    const cell = live.getRange(LIVE_CELL.intelWarn);
    const cur = String(cell.getValue() || '');
    if (cur.indexOf(msg) !== -1) return;
    cell.setValue(cur ? (cur + ' · ' + msg) : msg);
  } catch (_) {}
}

// =============================================================================
// MODEL REFRESH — pulls /predictions_live.json into Bets!I:K (+ BC:BE corrected)
// =============================================================================

function refreshModel(payload) {
  const ss = SpreadsheetApp.getActive();
  const bets = ss.getSheetByName(SHEET.bets);
  const method = ss.getSheetByName(SHEET.method);
  if (!bets) throw new Error('Sheet "' + SHEET.bets + '" not found');

  if (!payload) payload = _fetchJson_(_predictionsUrl_(ss));
  const matchPreds = payload.match_predictions || [];
  if (!matchPreds.length) {
    _writeStatus_('No match_predictions in feed');
    return;
  }

  const probsByM = {};
  matchPreds.forEach(function(mp) {
    probsByM[Number(mp.m)] = [
      _num_(mp.p_home_win),
      _num_(mp.p_draw),
      _num_(mp.p_away_win),
    ];
  });

  const lastRow = _findLastBetsRow_(bets);
  const nRows = lastRow - BETS_FIRST_DATA_ROW + 1;
  if (nRows <= 0) {
    _writeStatus_('Bets sheet has no data rows');
    return;
  }
  const matchNos = bets.getRange(BETS_FIRST_DATA_ROW, BETS_COL.matchNo, nRows, 1).getValues();
  const placedCol = bets.getRange(BETS_FIRST_DATA_ROW, BETS_COL.placed, nRows, 1).getValues();
  const currentIJK = bets.getRange(BETS_FIRST_DATA_ROW, BETS_COL.modelPHome, nRows, 3).getValues();
  const out = [];
  let updated = 0, frozen = 0, missing = 0;
  for (let i = 0; i < currentIJK.length; i++) {
    const m = Number(matchNos[i][0]);
    const placed = String(placedCol[i][0] || '').trim().toUpperCase();
    if (placed === 'Y') {
      out.push(currentIJK[i]);
      frozen++;
      continue;
    }
    const fresh = probsByM[m];
    // v2.3.3 MED #10: tighten freshness gate with typeof check. Pre-fix,
    // `!isFinite(v)` alone accepted v=true (isFinite(true)===true coerces
    // to 1) and v="0.55" (string-coerces). Both signal corrupt upstream
    // payload — fail closed and preserve currentIJK rather than write a
    // bool or string into the I/J/K probability cells.
    if (!fresh || fresh.some(function(v) {
      return typeof v !== 'number' || !isFinite(v);
    })) {
      out.push(currentIJK[i]);
      missing++;
      continue;
    }
    out.push(fresh);
    updated++;
  }
  bets.getRange(BETS_FIRST_DATA_ROW, BETS_COL.modelPHome, out.length, 3).setValues(out);

  // v2.3.5 P0-B: thread matchNos through so _writeCalibratedProbs_ can
  // gate on m<KNOCKOUT_FIRST_M (calibration LUT was trained on group-
  // stage matches; the draw-market bins don't generalise to knockout
  // matches where the draw outcome is decided by penalties).
  _writeCalibratedProbs_(bets, BETS_FIRST_DATA_ROW, out, matchNos);

  if (method) method.getRange(M_CELL.lastModelRefresh).setValue(new Date());
  _writeStatus_('Model refresh: updated ' + updated + ', frozen ' + frozen + ', missing ' + missing);
}

// =============================================================================
// LIVE REFRESH — pulls /live_state.json into Live + Method
// =============================================================================

function refreshLive(state) {
  const ss = SpreadsheetApp.getActive();
  const live = ss.getSheetByName(SHEET.live);
  const method = ss.getSheetByName(SHEET.method);
  if (!live) throw new Error('Sheet "' + SHEET.live + '" not found');

  if (!state) state = _fetchJson_(_liveUrl_(ss));
  const now = new Date();
  // v2.3.4 HIGH #2: Python's run_live_update emits warnings as objects
  // (`{"type": "circuit_breaker", "message": "..."}`, see
  // scripts/live/run_live_update.py:533,556,570), not bare strings. v2.3.3
  // joined them directly which produced "[object Object] · ..." in Live!B11
  // and made the operator-visible warning channel useless. Render each
  // warning via .message → .type → JSON fallback so the row is legible.
  function _renderWarn_(w) {
    if (w == null) return '';
    if (typeof w === 'string') return w;
    if (typeof w === 'object') {
      if (w.message) return String(w.message);
      if (w.type) return String(w.type);
      try { return JSON.stringify(w); } catch (e) { return String(w); }
    }
    return String(w);
  }
  const warnings = Array.isArray(state.warnings)
    ? state.warnings.map(_renderWarn_).filter(Boolean).join(' · ')
    : '';
  const inPlayCount = _num_(state.in_play_count) ||
                      (Array.isArray(state.in_play) ? state.in_play.length : 0);

  _setIfFinite_(live, LIVE_CELL.lastPoll, now);
  live.getRange(LIVE_CELL.mode).setValue(state.mode || '');
  _setIfFinite_(live, LIVE_CELL.completed, _num_(state.completed_matches_count));
  live.getRange(LIVE_CELL.providerMode).setValue(state.provider_mode || '');
  live.getRange(LIVE_CELL.warnings).setValue(warnings);
  live.getRange(LIVE_CELL.source).setValue(state.source || '');
  live.getRange(LIVE_CELL.simRerun).setValue(state.simulation_rerun_this_tick === true);
  live.getRange(LIVE_CELL.liveTs).setValue(state.last_updated_utc || '');
  _setIfFinite_(live, LIVE_CELL.inPlayCount, inPlayCount);

  // ─── Stale-data kill switch (FIX CRIT #1) ───
  // Flip Method!B81 = TRUE when the feed is stale. The Method!B55 PAUSE formula
  // ORs in B81 (see xlsx), so Bets!AJ stake formulas now actually stop emitting
  // new stakes when the model is paused. v2.3 only wrote a cosmetic string.
  let staleMsg = '';
  // v2.3.3 LOW #14 / v2.3.4 CRIT #1: also kill on circuit-breaker.
  // live_state.warnings[] may carry circuit_breaker entries from the
  // upstream sim when its provider-quota guard fires. Treat any such
  // entry as a stale signal — the model output we'd otherwise hand the
  // Bets sheet is the LAST known-good state from before the breaker
  // tripped, which is exactly the case staleKill exists to defend against.
  //
  // v2.3.4 CRIT #1: pre-fix v2.3.3 only matched bare strings, but
  // run_live_update.py:533,556,570 emit objects of the form
  // `{"type": "circuit_breaker", "message": "..."}`. That made v2.3.3's
  // breaker scan dead code on every real-world payload. Now match both
  // shapes — string substring OR object whose .type/.message says so.
  const warnArr = Array.isArray(state.warnings) ? state.warnings : [];
  const cbTripped = warnArr.some(function(w) {
    if (typeof w === 'string') {
      return w.toLowerCase().indexOf('circuit_breaker') !== -1;
    }
    if (w && typeof w === 'object') {
      const t = String(w.type || '').toLowerCase();
      const m = String(w.message || '').toLowerCase();
      return t.indexOf('circuit_breaker') !== -1 ||
             m.indexOf('circuit_breaker') !== -1 ||
             m.indexOf('circuit breaker') !== -1;
    }
    return false;
  });

  // v2.3.4 MED #5: strict date validation. v2.3.3 used a bare truthy
  // check, so the literal strings 'null' / 'undefined' / 'Invalid Date'
  // (which a misbehaving upstream serializer can produce) all passed the
  // gate, then `new Date('null')` returned NaN, isFinite(ageMin) failed,
  // the `if` short-circuited via the `|| cbTripped` arm (which would be
  // false in that case), and execution fell through to the else branch
  // that CLEARS staleKill — the opposite of safe-fail. Now: parse first,
  // gate on a finite timestamp, and treat any non-numeric date as a
  // missing-timestamp safe-fail (matches the explicit-missing branch).
  let _luRaw = state.last_updated_utc;
  let _luMs = NaN;
  if (_luRaw != null && _luRaw !== '' &&
      String(_luRaw).toLowerCase() !== 'null' &&
      String(_luRaw).toLowerCase() !== 'undefined' &&
      String(_luRaw).toLowerCase() !== 'invalid date') {
    const _luParsed = new Date(_luRaw);
    const _luT = _luParsed.getTime();
    if (isFinite(_luT)) _luMs = _luT;
  }

  if (isFinite(_luMs)) {
    const ageMin = (now.getTime() - _luMs) / 60000;
    if ((isFinite(ageMin) && ageMin > STALE_MINUTES) || cbTripped) {
      staleMsg = cbTripped
        ? '⚠ circuit-breaker tripped upstream — model paused'
        : '⚠ STALE feed (' + ageMin.toFixed(0) + ' min old) — model paused';
      if (method) {
        method.getRange(M_CELL.staleKill).setValue(true);
        method.getRange(M_CELL.autoStatus).setValue(
          cbTripped
            ? 'PAUSED · circuit-breaker upstream'
            : 'PAUSED · stale feed (' + ageMin.toFixed(0) + ' min)');
      }
    } else {
      // Feed is fresh — clear the kill flag if it was previously set.
      if (method) {
        const cur = method.getRange(M_CELL.staleKill).getValue();
        if (cur === true || String(cur).toUpperCase() === 'TRUE') {
          method.getRange(M_CELL.staleKill).setValue(false);
        }
        // v2.3.13 LOW-2: overwrite B63 with the steady-state "ON" banner
        // so a stale-trip message from a prior tick doesn't cling after
        // the kill flag clears. Mirrors installAutoRefresh()'s writer.
        method.getRange(M_CELL.autoStatus).setValue('ON · every ' + POLL_MINUTES + ' min');
      }
    }
  } else {
    // v2.3.3 MED #9: missing last_updated_utc means we cannot reason
    // about freshness at all. Pre-fix this branch was an unconditional
    // no-op, leaving staleKill in whatever state the last tick set —
    // i.e. a stuck-TRUE kill flag stayed stuck, and a stuck-FALSE flag
    // failed to engage. Treat missing-timestamp as STALE (safer side).
    //
    // v2.3.5 M-2: when cbTripped is ALSO true in this same payload, the
    // pre-v2.3.5 branch wrote only the missing-timestamp message and the
    // CB-trip context was silently lost. Missing-ts is usually a downstream
    // symptom of the breaker (the sim paused → no fresh timestamp), so
    // the CB-trip is the actionable root cause. Surface BOTH.
    if (cbTripped) {
      staleMsg = '⚠ circuit-breaker tripped upstream AND feed missing last_updated_utc — model paused';
    } else {
      staleMsg = '⚠ feed missing last_updated_utc — model paused (safe-fail)';
    }
    if (method) {
      method.getRange(M_CELL.staleKill).setValue(true);
      method.getRange(M_CELL.autoStatus).setValue(
        cbTripped
          ? 'PAUSED · circuit-breaker upstream + missing timestamp'
          : 'PAUSED · feed missing timestamp');
    }
  }
  live.getRange(LIVE_CELL.staleWarn).setValue(staleMsg);

  // v2.3.7 HIGH-2: mirror the stale/CB banner onto the Matchday tab so
  // the operator working in their documented "ONLY tab" workflow notices
  // the pause without switching to Live. W1 is well outside the A:U data
  // range used by _matchdayKnockoutRowFormulas_ so it can't collide with
  // operator input. Clear on healthy ticks.
  try {
    const mdSh = SpreadsheetApp.getActive().getSheetByName(SHEET.matchday);
    if (mdSh) mdSh.getRange('W1').setValue(staleMsg || '');
  } catch (_) {}

  if (method) {
    method.getRange(M_CELL.lastLivePoll).setValue(now);
    method.getRange(M_CELL.liveMode).setValue(state.mode || '');
    _setIfFinite_(method, M_CELL.completedMatches, _num_(state.completed_matches_count));
    method.getRange(M_CELL.providerMode).setValue(state.provider_mode || '');
    method.getRange(M_CELL.warnings).setValue(warnings);
  }

  _writeStatus_('Live poll OK · mode=' + (state.mode || '?') +
    ' · completed=' + (state.completed_matches_count || 0) +
    ' · in_play=' + inPlayCount + (staleMsg ? ' · ' + staleMsg : ''));
}

// =============================================================================
// INTEL REFRESH — /matchday_intelligence.json → Live + Bets BB:BF
// =============================================================================

function refreshIntel(predictionsPayload) {
  const ss = SpreadsheetApp.getActive();
  const live = ss.getSheetByName(SHEET.live);
  const method = ss.getSheetByName(SHEET.method);
  const bets = ss.getSheetByName(SHEET.bets);
  if (!live || !bets) return;

  let intel;
  try {
    intel = _fetchJson_(_intelUrl_(ss));
    _stampEndpoint_(M_CELL.endpointIntelTs);
  }
  catch (e) {
    // v2.3.9 HIGH-1: best-effort local surface to Live!intelWarn (B24) for
    // the operator who happens to be on the Live tab right now, then RE-THROW
    // so refreshAll's _tryStep_ collects 'intel' into errors[] and the
    // success/fail tail at refreshAll lines ~706-716 writes the durable
    // _setErrLog_(summary) to Live!B28 from the parent path. Direct
    // _setErrLog_ here is racy: refreshAll then sees errors.length===0 and
    // calls _clearErrLog_() ~50ms later, wiping the persistent surface.
    // This is the same race v2.3.8 HIGH-2 closed for refreshDiagnostics —
    // v2.3.9 closes it for refreshIntel. See header docstring for the
    // universal v239 regression that prevents this pattern from recurring
    // silently in any other refresh* sibling.
    try { live.getRange(LIVE_CELL.intelWarn).setValue('intel fetch failed: ' + e.message); } catch (_) {}
    throw e;
  }

  const summary = intel.summary || {};
  const warns = Array.isArray(intel.warnings) ? intel.warnings.join(' · ') : '';

  live.getRange(LIVE_CELL.intelGenerated).setValue(intel.generated_at || '');
  _setIfFinite_(live, LIVE_CELL.intelTotal, _num_(summary.total_active_components));
  _setIfFinite_(live, LIVE_CELL.intelTeams, _num_(summary.teams_affected));
  _setIfFinite_(live, LIVE_CELL.intelMatches, _num_(summary.matches_affected));
  _setIfFinite_(live, LIVE_CELL.intelCapsHit, _num_(summary.aggregate_caps_hit));
  live.getRange(LIVE_CELL.intelWarn).setValue(warns);

  if (method) {
    method.getRange(M_CELL.intelGeneratedAt).setValue(intel.generated_at || '');
    _setIfFinite_(method, M_CELL.intelTotalComponents, _num_(summary.total_active_components));
    _setIfFinite_(method, M_CELL.intelTeamsAffected, _num_(summary.teams_affected));
    _setIfFinite_(method, M_CELL.intelMatchesAffected, _num_(summary.matches_affected));
    _setIfFinite_(method, M_CELL.intelCapsHit, _num_(summary.aggregate_caps_hit));
    method.getRange(M_CELL.lastIntelRefresh).setValue(new Date());
  }

  // Per-match intel index.
  //
  // v2.3.2 CRIT (R15): pre-fix code dropped every entry where
  // `a.match_id` was null — but per the Python canonical at
  // scripts/live/apply_matchday_adjustments.py:1497-1503, entries with
  // `match_id=null` are TOURNAMENT-WIDE (injury + stats_proxy bucketed
  // under (team, None) per line 1048) and apply to EVERY match where
  // that team plays. Match-level entries (weather etc.) apply only to
  // that single match. Without this fix, ~48/61 active components
  // (the big injury/stats_proxy deltas, e.g. Belgium -22.6, Algeria
  // -11.08) never reached the Bets sheet — only ~13 match-level
  // weather rows did.
  //
  // To distribute team-level entries we need to know which team plays
  // in which match. Source of truth = predictions_live.match_predictions
  // (mp.m, mp.home, mp.away). We fetch it once here.
  const adjustments = Array.isArray(intel.active_adjustments) ? intel.active_adjustments : [];
  const matchTeams = {};   // m → {home, away}
  // v2.3.3 HIGH #7: prefer payload threaded from refreshAll. Only fall
  // back to a fresh _fetchJson_ when this is called standalone (operator
  // menu, selfTest). Pre-fix this re-fetched predictions every 10 min on
  // top of refreshAll's existing fetch — 144 wasted calls/day.
  try {
    let predPayload = predictionsPayload;
    if (!predPayload) predPayload = _fetchJson_(_predictionsUrl_(ss));
    const mps = Array.isArray(predPayload.match_predictions) ? predPayload.match_predictions : [];
    mps.forEach(function(mp) {
      const mn = Number(mp.m);
      if (!isFinite(mn)) return;
      matchTeams[mn] = { home: String(mp.home || ''), away: String(mp.away || '') };
    });
  } catch (e) {
    // Match map not available → fall back to match-level entries only
    // (matches pre-v2.3.2 behavior). Surface the cause in the warn cell.
    live.getRange(LIVE_CELL.intelWarn).setValue(
      (warns ? warns + ' · ' : '') + 'intel team-fanout disabled: ' + e.message);
  }

  const byMatch = {};
  function _pushPart_(mid, team, type, elo) {
    if (!byMatch[mid]) byMatch[mid] = { delta: 0, parts: [] };
    byMatch[mid].delta += elo;
    const sign = elo >= 0 ? '+' : '';
    byMatch[mid].parts.push(String(team) + ' ' + String(type) + ': ' + sign + elo.toFixed(0));
  }
  adjustments.forEach(function(a) {
    const totalElo = _num_(a.total_elo_adjustment) || 0;
    if (totalElo === 0) return;
    const team = String(a.team || '');
    // v2.3.3 CRIT #1: use _num_() not Number(). Number(null) === 0 (and
    // Number(true) === 1, Number("") === 0) — all pass isFinite(), so the
    // pre-fix code shoved every team-level (match_id=null) entry into the
    // match-level branch with rawMid=0, then _pushPart_ wrote them to a
    // ghost match #0 that no Bets row has. Net: the v2.3.2 fan-out branch
    // was dead code. _num_() returns NaN for null/undefined/"" so the
    // isFinite gate now correctly routes null match_ids into the
    // tournament-wide fan-out at line 519+.
    const rawMid = _num_(a.match_id);
    if (isFinite(rawMid)) {
      // Match-level entry: bucket components under their single match.
      const comps = Array.isArray(a.components) ? a.components : [];
      comps.forEach(function(c) {
        const v = _num_(c.capped_elo);
        if (!isFinite(v) || v === 0) return;
        _pushPart_(rawMid, team, c.type, v);
      });
      // Edge: components may have been suppressed (post-cap = 0) but the
      // entry's total is still informational — record the delta even if
      // the breakdown has no rows.
      if ((Array.isArray(a.components) ? a.components : []).length === 0) {
        _pushPart_(rawMid, team, 'aggregate', totalElo);
      }
    } else {
      // Team-level (tournament-wide) entry: fan out to every match
      // where `team` plays as home or away. Apply `total_elo_adjustment`
      // (the entry's aggregate-capped sum) once per match. Component
      // breakdown is summarized as a single line per match for the
      // breakdown column to keep the cell readable.
      const targetMs = [];
      for (const m in matchTeams) {
        if (matchTeams[m].home === team || matchTeams[m].away === team) {
          targetMs.push(Number(m));
        }
      }
      targetMs.forEach(function(mid) {
        _pushPart_(mid, team, 'team-wide', totalElo);
      });
    }
  });

  const lastRow = _findLastBetsRow_(bets);
  const nRows = lastRow - BETS_FIRST_DATA_ROW + 1;
  if (nRows <= 0) return;
  const matchNos = bets.getRange(BETS_FIRST_DATA_ROW, BETS_COL.matchNo, nRows, 1).getValues();
  const deltaOut = [];
  const breakdownOut = [];
  for (let i = 0; i < nRows; i++) {
    const m = Number(matchNos[i][0]);
    const e = byMatch[m];
    if (e) {
      deltaOut.push([isFinite(e.delta) ? e.delta : 0]);
      breakdownOut.push([e.parts.join(' · ')]);
    } else {
      deltaOut.push([0]);
      breakdownOut.push(['']);
    }
  }
  bets.getRange(BETS_FIRST_DATA_ROW, BETS_COL.intelDelta, nRows, 1).setValues(deltaOut);
  bets.getRange(BETS_FIRST_DATA_ROW, BETS_COL.intelBreakdown, nRows, 1).setValues(breakdownOut);

  _writeStatus_('Intel refresh OK · components=' + (summary.total_active_components || 0) +
    ' · teams=' + (summary.teams_affected || 0) +
    ' · matches=' + (summary.matches_affected || 0));
}

// =============================================================================
// MATCH CONTEXT — altitude/climate/home travel km (rarely changes)
// =============================================================================

function refreshMatchContext(payload) {
  const ss = SpreadsheetApp.getActive();
  const bets = ss.getSheetByName(SHEET.bets);
  if (!bets) return;
  if (!payload) payload = _fetchJson_(_predictionsUrl_(ss));
  const matchPreds = payload.match_predictions || [];
  if (!matchPreds.length) return;

  const byM = {};
  matchPreds.forEach(function(mp) {
    byM[Number(mp.m)] = mp;
  });

  const lastRow = _findLastBetsRow_(bets);
  const nRows = lastRow - BETS_FIRST_DATA_ROW + 1;
  if (nRows <= 0) return;
  const matchNos = bets.getRange(BETS_FIRST_DATA_ROW, BETS_COL.matchNo, nRows, 1).getValues();
  const out = [];
  for (let i = 0; i < nRows; i++) {
    const m = Number(matchNos[i][0]);
    const mp = byM[m];
    if (mp) {
      out.push([
        _num_safe_(mp.altitude_m),
        mp.climate || '',
        _num_safe_(mp.home_travel_km),
      ]);
    } else {
      out.push(['', '', '']);
    }
  }
  bets.getRange(BETS_FIRST_DATA_ROW, BETS_COL.altitudeM, nRows, 3).setValues(out);
  _writeStatus_('Match context refresh OK · ' + nRows + ' rows');
}

// =============================================================================
// DIAGNOSTICS REFRESH — walk_forward + calibration → Method
// =============================================================================

function refreshDiagnostics() {
  const ss = SpreadsheetApp.getActive();
  const method = ss.getSheetByName(SHEET.method);
  if (!method) return;

  // v2.3.8 HIGH-2: collect per-endpoint failures locally and throw a
  // combined error at the tail if any. Pre-v2.3.8 each catch called
  // _setErrLog_ directly but did NOT re-throw or push to refreshAll's
  // `errors` array — refreshAll's clean path then called _clearErrLog_
  // ~50ms later, wiping the diagnostic write before the operator could
  // see it. By collecting + throwing at the end, we (a) let both
  // endpoints attempt independently (a walk_forward fail does not skip
  // calibration), AND (b) restore the single error-collection invariant
  // owned by _tryStep_ in refreshAll. _setErrLog_ is NOT called here —
  // refreshAll's catch path will stamp the combined summary into B28.
  const diagErrors = [];

  // Walk-forward
  try {
    const wf = _fetchJson_(_walkForwardUrl_(ss));
    _stampEndpoint_(M_CELL.endpointWalkFwdTs);
    const years = Object.keys(wf).filter(function(k) { return /^[0-9]{4}$/.test(k); });
    const losses = years.map(function(y) { return _num_(wf[y].log_loss); }).filter(isFinite);
    const meanLoss = losses.length ? losses.reduce(function(a,b){return a+b;}, 0) / losses.length : NaN;
    _setIfFinite_(method, M_CELL.walkForwardMeanLogLoss, meanLoss);
    const lift2022 = wf['2022'] && _num_(wf['2022'].lift_vs_baseline);
    _setIfFinite_(method, M_CELL.walkForward2022Lift, lift2022);
  } catch (e) {
    Logger.log('walk_forward fetch failed: ' + e.message);
    diagErrors.push('walk_forward: ' + e.message);
  }

  // Calibration — cache via PropertiesService (works in custom function context).
  try {
    const cal = _fetchJson_(_calibrationUrl_(ss));
    _stampEndpoint_(M_CELL.endpointCalibTs);
    const holdout = cal.holdout || {};
    _setIfFinite_(method, M_CELL.holdoutLogLoss, _num_(holdout.log_loss));
    _setIfFinite_(method, M_CELL.holdoutLiftElo, _num_(holdout.lift_log_loss_vs_elo));
    const wcb = cal.wc_backtest || {};
    method.getRange(M_CELL.calibrationValidationScope).setValue(
      (wcb._validation_scope || 'unknown') +
      (wcb._note ? ' — ' + wcb._note : '')
    );
    _setIfFinite_(method, M_CELL.calibrationNTest, _num_(cal.n_test));

    if (cal.calibration && cal.calibration.home) {
      const props = PropertiesService.getScriptProperties();
      props.setProperty(CALIBRATION_PROP_KEY, JSON.stringify(cal.calibration));
      props.setProperty(CALIBRATION_PROP_TS, String(Date.now()));
    }
  } catch (e) {
    Logger.log('calibration fetch failed: ' + e.message);
    diagErrors.push('calibration: ' + e.message);
  }

  method.getRange(M_CELL.lastDiagnosticsRefresh).setValue(new Date());

  // v2.3.8 LOW-2: refresh the Kelly-edge banner at Method!D5 every tick,
  // not only at install. If walk_forward.json shipped a new lift after a
  // model retrain, this picks it up at the next POLL_MINUTES tick. Safe
  // even when walk-forward fetch failed above (the function reads B73,
  // which is unchanged from the prior healthy tick; banner just stays
  // stale until the next healthy fetch).
  try { _seedKellyEdgeDisclaimer_(); } catch (_) {}

  if (diagErrors.length) {
    // v2.3.8 HIGH-2: throw a combined summary so refreshAll's _tryStep_
    // collects it into the parent errors array. This ensures _clearErrLog_
    // does NOT wipe the persistent surface on a tick where diagnostics
    // partially failed.
    throw new Error('diagnostics: ' + diagErrors.join(' | '));
  }

  _writeStatus_('Diagnostics refresh OK');
}

// =============================================================================
// OUTRIGHTS REFRESH — team_predictions[] → Outrights sheet
// =============================================================================

function refreshOutrights(payload) {
  const ss = SpreadsheetApp.getActive();
  let out = ss.getSheetByName(SHEET.outrights);
  if (!out) {
    out = ss.insertSheet(SHEET.outrights);
    _seedOutrightsHeaders_(out);
  }

  if (!payload) payload = _fetchJson_(_predictionsUrl_(ss));
  const teams = Array.isArray(payload.team_predictions) ? payload.team_predictions : [];
  if (!teams.length) {
    _writeStatus_('No team_predictions in feed');
    return;
  }

  // Sort by p_champion desc, alpha-by-team for tiebreak.
  // v2.3.5 P1: when p_champion=0 (knockout-eliminated teams late in the
  // tournament, or pre-tournament teams with no projection), pre-fix
  // returned 0 from the comparator → V8 Array.sort is stable and fell
  // back to payload order, which is whatever order the Python sim
  // serialised that tick. Result: the Outrights sheet rows visibly
  // re-ordered between ticks on the zero-p_champion tail. Locking the
  // secondary key to team.localeCompare gives a deterministic display.
  teams.sort(function(a, b) {
    const d = _num_(b.p_champion) - _num_(a.p_champion);
    if (d !== 0) return d;
    return String(a.team || '').localeCompare(String(b.team || ''));
  });

  const rows = teams.map(function(t) {
    const pCh = _num_(t.p_champion);
    const lo = _num_(t.p_champion_p05);
    const hi = _num_(t.p_champion_p95);
    const spread = (isFinite(lo) && isFinite(hi)) ? (hi - lo) : '';
    return [
      t.team || '',
      t.group || '',
      isFinite(pCh) ? pCh : '',
      isFinite(lo) ? lo : '',
      isFinite(hi) ? hi : '',
      spread,
      _num_safe_(t.p_advance_groups),
      _num_safe_(t.p_reach_r16),
      _num_safe_(t.p_reach_qf),
      _num_safe_(t.p_reach_sf),
      _num_safe_(t.p_reach_final),
    ];
  });

  const dataStart = 4;
  const prevLast = Math.max(out.getLastRow(), dataStart);

  // v2.3.3 CRIT #2 / v2.3.4 MED #6: preserve operator-typed bookie odds
  // in col L across re-sorts. The pre-v2.3.3 clearContent(…1..18) wiped
  // every L cell on each tick so the BET? formula at col O reverted to ""
  // until odds were re-typed.
  //
  // v2.3.4 MED #6: snapshot BOTH .getValues() AND .getFormulas() in
  // parallel and restore whichever was set on the source row. v2.3.3
  // captured only values — so if an operator typed a live formula in L
  // (e.g. `=IMPORTRANGE(...)` from a private odds feed), the snapshot
  // captured the EVALUATED number, the clear wiped the formula, and the
  // restore put the stale number back. Subsequent ticks then never
  // re-evaluated. Now: formula wins over value when both exist; restore
  // uses setFormula for cells that originally held a formula.
  const oddsValByTeam = {};
  const oddsFormByTeam = {};
  if (prevLast >= dataStart) {
    const prevRangeR = out.getRange(dataStart, 1, prevLast - dataStart + 1, 12);
    const prevValues = prevRangeR.getValues();
    const prevFormulas = prevRangeR.getFormulas();
    for (let i = 0; i < prevValues.length; i++) {
      const team = prevValues[i][0];           // col A
      const oddsV = prevValues[i][11];         // col L value
      const oddsF = prevFormulas[i][11];       // col L formula (or '')
      if (!team) continue;
      const key = String(team);
      if (oddsF) {
        oddsFormByTeam[key] = oddsF;
      } else if (oddsV !== '' && oddsV !== null && isFinite(_num_(oddsV))) {
        oddsValByTeam[key] = oddsV;
      }
    }
    out.getRange(dataStart, 1, prevLast - dataStart + 1, 18).clearContent();
  }
  out.getRange(dataStart, 1, rows.length, 11).setValues(rows);

  // Restore preserved bookie odds onto whichever row now holds the same team.
  // Formulas take precedence (operator may have typed an IMPORTRANGE-backed
  // odds feed); plain values are restored only when no formula was captured.
  for (let i = 0; i < rows.length; i++) {
    const team = rows[i][0];
    if (!team) continue;
    const key = String(team);
    const cell = out.getRange(dataStart + i, 12);
    if (Object.prototype.hasOwnProperty.call(oddsFormByTeam, key)) {
      cell.setFormula(oddsFormByTeam[key]);
    } else if (Object.prototype.hasOwnProperty.call(oddsValByTeam, key)) {
      cell.setValue(oddsValByTeam[key]);
    }
  }

  // Group Winner / To-Qualify section (cols P/Q/R — accuracy booster from v2.4 candidates).
  const grpRows = teams.map(function(t) {
    return [
      _num_safe_(t.p_finish_1st_group),
      _num_safe_(t.p_finish_2nd_group),
      _num_safe_(t.p_third_place),
    ];
  });
  out.getRange(dataStart, 16, grpRows.length, 3).setValues(grpRows);

  // L:O = bookie odds (input), implied, EV%, BET? — wire formulas for each row.
  // BET? now respects Method!B55 PAUSE (fix HIGH #8).
  for (let i = 0; i < rows.length; i++) {
    const r = dataStart + i;
    if (!out.getRange(r, 13).getFormula() && !out.getRange(r, 13).getValue()) {
      out.getRange(r, 13).setFormula('=IF(L' + r + '="","",1/L' + r + ')');
    }
    if (!out.getRange(r, 14).getFormula() && !out.getRange(r, 14).getValue()) {
      out.getRange(r, 14).setFormula('=IF(OR(L' + r + '="",C' + r + '=""),"",C' + r + '*L' + r + '-1)');
    }
    if (!out.getRange(r, 15).getFormula() && !out.getRange(r, 15).getValue()) {
      out.getRange(r, 15).setFormula(
        '=IF(Method!$B$55=TRUE(),"🛑 PAUSED",' +
        'IF(N' + r + '="","",' +
        'IF(N' + r + '<Method!$B$7,"PASS","BET")))'
      );
    }
  }
  _writeStatus_('Outrights refresh OK · ' + rows.length + ' teams · group-winner cols populated');
}

function _seedOutrightsHeaders_(sh) {
  sh.getRange('A1').setValue('OUTRIGHTS — champion / advance / per-stage / group-winner / to-qualify');
  sh.getRange('A2').setValue('Source: /predictions_live.json · team_predictions[] · type bookie odds in L for champion BET? signal');
  const hdr = ['Team', 'Group', 'p_champion', 'p_champ_p05', 'p_champ_p95', 'Spread',
               'p_advance', 'p_R16', 'p_QF', 'p_SF', 'p_Final',
               'Bookie odds', 'Implied', 'EV %', 'BET?',
               'p_winner_grp', 'p_2nd_grp', 'p_3rd_place'];
  sh.getRange(3, 1, 1, hdr.length).setValues([hdr]);
  sh.setFrozenRows(3);
}

// =============================================================================
// IN-PLAY REFRESH — live_state.in_play[] → In-Play sheet
// =============================================================================

function refreshInPlay(state) {
  const ss = SpreadsheetApp.getActive();
  let sh = ss.getSheetByName(SHEET.inplay);
  if (!sh) {
    sh = ss.insertSheet(SHEET.inplay);
    _seedInPlayHeaders_(sh);
  }

  if (!state) state = _fetchJson_(_liveUrl_(ss));
  const inPlay = Array.isArray(state.in_play) ? state.in_play : [];
  const dataStart = 4;
  const prevLast = Math.max(sh.getLastRow(), dataStart);
  if (prevLast >= dataStart) {
    sh.getRange(dataStart, 1, prevLast - dataStart + 1, 9).clearContent();
  }

  sh.getRange(2, 2).setValue(new Date());

  if (!inPlay.length) {
    sh.getRange(dataStart, 1).setValue('(no matches in play right now)');
    return;
  }

  const rows = inPlay.map(function(p) {
    return [
      _num_(p.m),
      p.home || '',
      p.away || '',
      _num_(p.home_score),
      _num_(p.away_score),
      _num_(p.elapsed),
      p.status || '',
      p.status_long || '',
      new Date(),
    ];
  });
  sh.getRange(dataStart, 1, rows.length, 9).setValues(rows);
  _writeStatus_('In-play refresh OK · ' + rows.length + ' live match(es)');
}

function _seedInPlayHeaders_(sh) {
  sh.getRange('A1').setValue('IN-PLAY — live match score + elapsed (from /live_state.json:in_play[])');
  sh.getRange('A2').setValue('Last refresh (UTC)');
  const hdr = ['#m', 'Home', 'Away', 'Home score', 'Away score', 'Elapsed', 'Status', 'Status (long)', 'Pulled at'];
  sh.getRange(3, 1, 1, hdr.length).setValues([hdr]);
  sh.setFrozenRows(3);
}

// =============================================================================
// GOAL GRID REFRESH (Phase 5B) — predictions_live.match_predictions[]
//   → Goal Grid sheet (one row per match, λ_h/λ_a + fair goal-market probs)
// Idempotent: rewrites col E:J in place, leaves K:P (book odds / edge cells)
// untouched so the operator can keep their typed prices across refreshes.
// =============================================================================

function refreshGoalGrid(payload) {
  const ss = SpreadsheetApp.getActive();
  let sh = ss.getSheetByName(SHEET.goalGrid);
  if (!sh) {
    sh = ss.insertSheet(SHEET.goalGrid);
    _seedGoalGridHeaders_(sh);
  }

  if (!payload) payload = _fetchJson_(_predictionsUrl_(ss));
  const matchPreds = Array.isArray(payload.match_predictions)
    ? payload.match_predictions : [];
  if (!matchPreds.length) {
    _writeStatus_('Goal Grid: no match_predictions in feed');
    return;
  }

  // Sort by match number so row order matches the Bets tab.
  matchPreds.sort(function(a, b) { return _num_(a.m) - _num_(b.m); });

  const dataStart = 4;
  const rows = matchPreds.map(function(mp) {
    const lamH = _num_(mp.lam_home);
    const lamA = _num_(mp.lam_away);
    const haveLam = isFinite(lamH) && isFinite(lamA);
    return [
      _num_(mp.m),
      mp.date || mp.date_utc || '',
      mp.home || '',
      mp.away || '',
      haveLam ? lamH : '',
      haveLam ? lamA : '',
    ];
  });

  // Clear A:F for the existing data block, then write fresh A:F. Leaves
  // G:P (formulas + typed book odds) untouched — see _seedGoalGridHeaders_.
  const prevLast = Math.max(sh.getLastRow(), dataStart);
  if (prevLast >= dataStart) {
    sh.getRange(dataStart, 1, prevLast - dataStart + 1, 6).clearContent();
  }
  sh.getRange(dataStart, 1, rows.length, 6).setValues(rows);

  // Wire formulas for G:P on each row. Re-seed every time so the operator
  // can't accidentally break a row by deleting a formula — typed book odds
  // in K:M survive because we only setFormula on G:J, N, O, P.
  for (let i = 0; i < rows.length; i++) {
    const r = dataStart + i;
    const lamRef = 'E' + r + ',F' + r;
    if (!sh.getRange(r, 5).getValue() || !sh.getRange(r, 6).getValue()) {
      // v2.3.3 HIGH #8: clear stale G:J + N:P when λ is missing for this
      // row. Pre-fix, if λ_home/λ_away dropped out between ticks (e.g. a
      // late match-id renumber or upstream sim partial-write), the prior
      // tick's GOAL_GRID outputs lingered on screen and looked current.
      // K:M (typed bookie odds) are deliberately preserved.
      sh.getRange(r, 7, 1, 4).clearContent();    // G:J fair probs
      sh.getRange(r, 14, 1, 3).clearContent();   // N:P implied/edge/BET?
      continue;
    }
    sh.getRange(r, 7).setFormula('=IFERROR(GOAL_GRID(' + lamRef + ',"ou25"),"")');
    sh.getRange(r, 8).setFormula('=IFERROR(GOAL_GRID(' + lamRef + ',"btts"),"")');
    sh.getRange(r, 9).setFormula('=IFERROR(GOAL_GRID(' + lamRef + ',"ou15"),"")');
    sh.getRange(r, 10).setFormula('=IFERROR(GOAL_GRID(' + lamRef + ',"ou35"),"")');
    // N: implied prob from typed Over 2.5 book odds in K (single-side de-vig
    // not appropriate for 2-way O/U — use 1/K as the raw implied).
    sh.getRange(r, 14).setFormula('=IF(K' + r + '="","",1/K' + r + ')');
    // O: edge = fair − implied (positive = value on Over).
    sh.getRange(r, 15).setFormula(
      '=IF(OR(G' + r + '="",N' + r + '=""),"",G' + r + '-N' + r + ')'
    );
    // P: BET? signal — respects Method!B55 PAUSE (matches refreshOutrights).
    // Goal markets get their own edge threshold (Method!$B$8 =
    // goal_markets_min_edge) — O/U and BTTS have tighter overround than 1X2,
    // so reusing the 1X2 knob (Method!$B$7) would either over-filter goal
    // bets or under-filter 1X2 bets. Defaults to same value as $B$7 at seed.
    sh.getRange(r, 16).setFormula(
      '=IF(Method!$B$55=TRUE(),"🛑 PAUSED",' +
      'IF(O' + r + '="","",' +
      'IF(O' + r + '<Method!$B$8,"PASS","BET")))'
    );
  }
  _writeStatus_('Goal Grid refresh OK · ' + rows.length + ' matches');
}

function _seedGoalGridHeaders_(sh) {
  sh.getRange('A1').setValue(
    'GOAL GRID — fair P for O/U, BTTS, AH from λ_h/λ_a · ' +
    'Poisson + Dixon-Coles τ=' + GOAL_GRID_TAU +
    ' · MAX_GOALS=' + GOAL_GRID_MAX_GOALS +
    // v2.3.10 MED-2: surface the Poisson-vs-NB+DC drift band inline so the
    // operator's BET? column is read with the right caveat. Production sim
    // (03_simulate.py) uses NB marginals with team-specific dispersion;
    // the feed does NOT publish p_btts / p_over25 / k_home / k_away, so
    // client-side falls back to Poisson and drifts ~2-9pp on OU2.5/BTTS at
    // high-λ matchups. A full close requires upstream to expose those
    // fields in predictions_live.json — outside the engine's surface.
    ' · ⚠ DRIFT: Poisson vs prod NB+DC ≈2-9pp on OU2.5/BTTS; BET? uses ' +
    'Method!B8 edge threshold (raise B8 to 0.08 for an 8pp drift buffer)'
  );
  // v2.3.11 MED-B: A1 banner is ~250 chars. Without wrap, the load-bearing
  // "≥8pp" caveat is truncated off-screen on mobile Sheets (col A ~80-120px).
  // setWrap(true) reflows into multiple visual lines; setRowHeight(1, 60)
  // gives ~3 lines of vertical space so the warning is fully readable on
  // both desktop and mobile without column-A width gymnastics.
  sh.getRange('A1').setWrap(true);
  sh.setRowHeight(1, 60);
  sh.getRange('A2').setValue(
    'Source: /predictions_live.json · match_predictions[] · ' +
    'type Over 2.5 book odds in K for BET? signal'
  );
  const hdr = [
    '#m', 'Date', 'Home', 'Away',
    'λ_home', 'λ_away',
    'Fair O/U 2.5', 'Fair BTTS', 'Fair O/U 1.5', 'Fair O/U 3.5',
    'Book O 2.5', 'Book U 2.5', 'Book BTTS Y',
    'Implied O 2.5', 'Edge %', 'BET?',
  ];
  sh.getRange(3, 1, 1, hdr.length).setValues([hdr]);
  sh.setFrozenRows(3);
}

// =============================================================================
// CLV REFRESH (Phase 5C) — Bets snapshots + logged odds → CLV sheet
//   Per-bet closing-line-value tracker. Operator types the closing odds in
//   col F after the market closes; the rolling 20-bet average (col I) is
//   the edge signal — positive CLV means the odds you took beat the close.
// Idempotent: matches existing rows by (#m, pick) so the operator's typed
// closing odds (col F) survive subsequent refreshes.
// =============================================================================

function refreshCLV() {
  const ss = SpreadsheetApp.getActive();
  const bets = ss.getSheetByName(SHEET.bets);
  if (!bets) throw new Error('Sheet "' + SHEET.bets + '" not found');
  let sh = ss.getSheetByName(SHEET.clv);
  if (!sh) {
    sh = ss.insertSheet(SHEET.clv);
    _seedCLVHeaders_(sh);
  }

  const lastBet = _findLastBetsRow_(bets);
  const nRows = lastBet - BETS_FIRST_DATA_ROW + 1;
  if (nRows <= 0) {
    _writeStatus_('CLV refresh: Bets sheet empty');
    return;
  }
  const matchNos = bets.getRange(BETS_FIRST_DATA_ROW, BETS_COL.matchNo, nRows, 1).getValues();
  // Snapshot block is AW:AY = snapDecision, snapStake, snapPick. AO is the
  // operator-logged odds actually taken; AE is a legacy fallback for rows
  // placed before AO was filled.
  const snaps = bets.getRange(BETS_FIRST_DATA_ROW, BETS_COL.snapDecision, nRows, 3).getValues();
  const backedOdds = bets.getRange(BETS_FIRST_DATA_ROW, BETS_COL.backedOdds, nRows, 1).getValues();
  const pickedOdds = bets.getRange(BETS_FIRST_DATA_ROW, BETS_COL.pickedOdds, nRows, 1).getValues();

  // Collect placed bets (snapDecision = "BET" or non-empty + snapPick set).
  // Dedup on (#m, pick) so duplicate Bets rows don't double-count in the
  // rolling CLV window. Pre-filter out-of-scope picks (O/U, BTTS, CS) so
  // they don't inflate the placed-bet count with non-1X2 rows.
  const placedRows = [];
  const seenKeys = {};
  for (let i = 0; i < nRows; i++) {
    const dec = String(snaps[i][0] || '').trim().toUpperCase();
    const pick = String(snaps[i][2] || '').trim();
    if (!pick) continue;
    if (!_isOneXTwoPick_(pick)) continue;
    if (dec && dec.indexOf('BET') < 0 && dec !== 'YES') continue;
    const m = Number(matchNos[i][0]);
    if (!isFinite(m)) continue;
    const dedupKey = String(m) + '|' + pick.toUpperCase();
    if (seenKeys[dedupKey]) continue;
    seenKeys[dedupKey] = true;
    const stake = _num_(snaps[i][1]);
    const loggedOdds = _num_(backedOdds[i][0]);
    const fallbackOdds = _num_(pickedOdds[i][0]);
    const takenOdds = (isFinite(loggedOdds) && loggedOdds > 0)
      ? loggedOdds
      : ((isFinite(fallbackOdds) && fallbackOdds > 0) ? fallbackOdds : '');
    placedRows.push({
      m: m,
      pick: pick,
      stake: isFinite(stake) ? stake : '',
      takenOdds: takenOdds,
    });
  }

  // Preserve operator-typed closing odds (col F) by matching on (#m, pick).
  const dataStart = 4;
  const prevLast = Math.max(sh.getLastRow(), dataStart);
  const closingByKey = {};
  if (prevLast >= dataStart) {
    const prev = sh.getRange(dataStart, 1, prevLast - dataStart + 1, 6).getValues();
    for (let i = 0; i < prev.length; i++) {
      const key = String(prev[i][1]) + '|' + String(prev[i][2]).toUpperCase();
      const closing = prev[i][5];
      if (closing !== '' && closing !== null) closingByKey[key] = closing;
    }
    sh.getRange(dataStart, 1, prev.length, 9).clearContent();
  }

  if (!placedRows.length) {
    sh.getRange(dataStart, 1).setValue('(no placed bets yet — snapshot block is empty)');
    _writeStatus_('CLV refresh: 0 placed bets');
    return;
  }

  // Write columns A:F (bet_no, #m, pick, stake, taken_odds, closing_odds).
  const rows = placedRows.map(function(p, idx) {
    const key = String(p.m) + '|' + String(p.pick).toUpperCase();
    return [
      idx + 1,
      p.m,
      p.pick,
      p.stake,
      p.takenOdds,
      (key in closingByKey) ? closingByKey[key] : '',
    ];
  });
  sh.getRange(dataStart, 1, rows.length, 6).setValues(rows);

  // Formulas in G:I — CLV%, rolling 20-bet CLV%, status pill.
  // CLV% = (taken_odds - closing_odds) / closing_odds. Positive = you got
  // a better price than the market settled at. Pre-validate E/F>0 so a 0
  // doesn't emit #DIV/0! and collapse the whole rolling-window IFERROR.
  for (let i = 0; i < rows.length; i++) {
    const r = dataStart + i;
    sh.getRange(r, 7).setFormula(
      '=IF(OR(E' + r + '="",F' + r + '="",NOT(ISNUMBER(E' + r + ')),NOT(ISNUMBER(F' + r + ')),E' + r + '<=0,F' + r + '<=0),"",' +
      '(E' + r + '-F' + r + ')/F' + r + ')'
    );
    // Rolling window: AVERAGE of last CLV_ROLLING_WINDOW CLV% cells up to
    // this row. Includes the current row, so first WINDOW-1 rows are
    // narrower averages. Label the result with the actual sample size when
    // n<CLV_ROLLING_WINDOW so the operator can't mistake "n=3" for "n=20".
    const winStart = Math.max(dataStart, r - CLV_ROLLING_WINDOW + 1);
    const winRange = 'G' + winStart + ':G' + r;
    sh.getRange(r, 8).setFormula(
      '=IFERROR(' +
        'IF(COUNT(' + winRange + ')<' + CLV_ROLLING_WINDOW + ',' +
          '"n="&COUNT(' + winRange + ')&": "&AVERAGE(' + winRange + '),' +
          'AVERAGE(' + winRange + ')' +
        '),"")'
    );
    sh.getRange(r, 9).setFormula(
      '=IF(H' + r + '="","",' +
      'IF(ISNUMBER(H' + r + '),' +
      'IF(H' + r + '>0,"BEATING CLOSE","BELOW CLOSE"),' +
      'IF(IFERROR(VALUE(REGEXEXTRACT(H' + r + ',": (.*)$")),0)>0,' +
      '"BEATING CLOSE (n<' + CLV_ROLLING_WINDOW + ')",' +
      '"BELOW CLOSE (n<' + CLV_ROLLING_WINDOW + ')")))'
    );
  }
  _writeStatus_('CLV refresh OK · ' + rows.length + ' placed bet(s) · ' +
    'rolling window ' + CLV_ROLLING_WINDOW);
}

// CLV is 1X2-only in v2.3.1 (per _seedCLVHeaders_ scope note). Centralised
// gate so refreshCLV doesn't have to repeat the whitelist inline; goal-market
// picks (O/U, BTTS, Correct Score) return false and are excluded from
// placedRows entirely.
function _isOneXTwoPick_(pick) {
  // v2.3.3 MED #11: accept extended textual picks. Operators frequently
  // type "Home Win" / "Away Win" / "Home win" from the Bets sheet's
  // own validation dropdown — pre-fix CLV silently skipped those rows
  // (rejected → 0 CLV rolling avg → false PASS on diagnostics tab).
  const p = String(pick || '').trim().toUpperCase();
  return p === 'H' || p === 'HOME' || p === '1' || p === 'HOME WIN'
      || p === 'D' || p === 'DRAW' || p === 'X'
      || p === 'A' || p === 'AWAY' || p === '2' || p === 'AWAY WIN';
}

function _seedCLVHeaders_(sh) {
  sh.getRange('A1').setValue(
    'CLV — closing-line-value tracker · rolling ' + CLV_ROLLING_WINDOW +
    '-bet average · positive CLV = beating the close'
  );
  sh.getRange('B1').setValue(
    'CLV tracked for 1X2 picks only — O/U and BTTS coming in v2.4'
  );
  sh.getRange('A2').setValue(
    'Source: Bets!AO backed odds + AW:AY snapshot block · ' +
    'type closing odds in col F after market closes'
  );
  const hdr = [
    'Bet #', '#m', 'Pick', 'Stake',
    'Taken odds', 'Closing odds',
    'CLV %', 'Rolling ' + CLV_ROLLING_WINDOW + ' CLV %', 'Status',
  ];
  sh.getRange(3, 1, 1, hdr.length).setValues([hdr]);
  sh.setFrozenRows(3);
}

// =============================================================================
// RETRO-SNAPSHOT (unchanged from v2.3)
// =============================================================================

function snapshotExisting() {
  const ss = SpreadsheetApp.getActive();
  const mdSh = ss.getSheetByName(SHEET.matchday);
  const bets = ss.getSheetByName(SHEET.bets);
  if (!mdSh || !bets) return;
  const lastBet = _findLastBetsRow_(bets);
  const nRows = lastBet - BETS_FIRST_DATA_ROW + 1;
  if (nRows <= 0) return;
  const placed = mdSh.getRange(4, 14, nRows, 1).getValues();
  const snaps = bets.getRange(BETS_FIRST_DATA_ROW, BETS_COL.snapDecision, nRows, 3).getValues();
  const dec = bets.getRange(BETS_FIRST_DATA_ROW, BETS_COL.decision, nRows, 1).getValues();
  const stake = bets.getRange(BETS_FIRST_DATA_ROW, BETS_COL.engineStake, nRows, 1).getValues();
  const pick = bets.getRange(BETS_FIRST_DATA_ROW, BETS_COL.pick, nRows, 1).getValues();
  let wrote = 0;
  for (let i = 0; i < nRows; i++) {
    const isPlaced = String(placed[i][0] || '').trim().toUpperCase() === 'Y';
    const blank = snaps[i].every(function(v) { return v === '' || v === null; });
    if (isPlaced && blank) {
      bets.getRange(BETS_FIRST_DATA_ROW + i, BETS_COL.snapDecision, 1, 3)
          .setValues([[dec[i][0], stake[i][0], pick[i][0]]]);
      wrote++;
    }
  }
  _writeStatus_('Retro-snapshot: captured ' + wrote + ' pre-existing placed bet(s)');
}

// =============================================================================
// KNOCKOUT EXTENSION — Phase 5A
// =============================================================================
//
// Group stage occupies Bets rows 2..73 (match_no 1..72). Knockout matches
// m=73..104 (32 fixtures: 16 R32 + 8 R16 + 4 QF + 2 SF + 1 3rd-place + 1
// Final) need rows 74..105. This function appends those rows idempotently:
//
//   - Col A          (match_no) populated with 73..104
//   - Col A note     stage label so the user sees R32/R16/QF/SF/3rd/Final
//                    at a glance (e.g. "R32 — bracket slot 1")
//   - BC:BE          seeded with =I{r}/=J{r}/=K{r} mirror formulas, matching
//                    the v2.3.1 group-stage seed pattern (CRIT #3) so that
//                    if calibration bins are unavailable refreshModel's
//                    BC:BE writes still keep these cells in sync with I:K.
//   - BG:BI          left blank — refreshMatchContext will populate them
//                    once venue/host city is decided per knockout slot.
//   - B..H           NOT touched — these hold date/home/away/venue/group
//                    metadata that the user (or future refreshModel
//                    extension) will fill once the bracket resolves.
//
// Re-running is safe: rows whose col A already matches the target match_no
// are skipped. Only blank rows in the 74..105 window are populated.
// =============================================================================

// Knockout stage layout: each entry covers a contiguous match_no range.
// First match in each stage tagged "slot 1" via its index within the stage.
const KNOCKOUT_STAGES = [
  { stage: 'R32',   first: 73,  last: 88 },
  { stage: 'R16',   first: 89,  last: 96 },
  { stage: 'QF',    first: 97,  last: 100 },
  { stage: 'SF',    first: 101, last: 102 },
  { stage: '3rd',   first: 103, last: 103 },
  { stage: 'Final', first: 104, last: 104 },
];
const KNOCKOUT_FIRST_M = 73;
const KNOCKOUT_LAST_M  = 104;
const KNOCKOUT_FIRST_ROW = BETS_FIRST_DATA_ROW + (KNOCKOUT_FIRST_M - 1);  // 74
const KNOCKOUT_LAST_ROW  = BETS_FIRST_DATA_ROW + (KNOCKOUT_LAST_M  - 1);  // 105

function _knockoutStageFor_(m) {
  for (let i = 0; i < KNOCKOUT_STAGES.length; i++) {
    const s = KNOCKOUT_STAGES[i];
    if (m >= s.first && m <= s.last) {
      return { stage: s.stage, slot: m - s.first + 1 };
    }
  }
  return null;
}

// v2.3.5 P0-A: build the 36-column staking-formula matrix for one
// knockout row. Mirrors the master formulas on Bets row 2 with per-row
// anchors. Returns three contiguous blocks matching the column gaps
// (operator-typed cells AO/AP/AQ now-also-seeded; AW/AX/AY left blank
// for manual entry):
//   - Block 1: O..AQ (29 cols) — fair probs / devig / EV / pick / decision
//                                / stake / settlement / matchday-mirror
//   - Block 2: AR..AV (5 cols) — settle / running pnl / msg / audit / backed
//   - Block 3: AZ..BA (2 cols) — snap-or-current decision / stake
// The Matchday-sheet references use rMatchday = r + 2 (Bets row 2 in the
// xlsx points to Matchday row 4 — Matchday has two extra header rows).
function _knockoutStakingFormulas_(r) {
  const rm = r + 2;
  const block_O_AQ = [
    '=IF(L' + r + '="","",1/L' + r + ')',
    '=IF(M' + r + '="","",1/M' + r + ')',
    '=IF(N' + r + '="","",1/N' + r + ')',
    '=IF(COUNT(L' + r + ':N' + r + ')<3,"",O' + r + '+P' + r + '+Q' + r + ')',
    '=IF($R' + r + '="","",IF(Method!$B$56="proportional",O' + r + '/$R' + r +
      ',IF(Method!$B$56="power",IFERROR(INDEX(power_devig($L' + r + ',$M' + r + ',$N' + r + '),1,1),O' + r + '/$R' + r +
      '),IFERROR(INDEX(shin($L' + r + ',$M' + r + ',$N' + r + '),1,1),O' + r + '/$R' + r + '))))',
    '=IF($R' + r + '="","",IF(Method!$B$56="proportional",P' + r + '/$R' + r +
      ',IF(Method!$B$56="power",IFERROR(INDEX(power_devig($L' + r + ',$M' + r + ',$N' + r + '),2,1),P' + r + '/$R' + r +
      '),IFERROR(INDEX(shin($L' + r + ',$M' + r + ',$N' + r + '),2,1),P' + r + '/$R' + r + '))))',
    '=IF($R' + r + '="","",IF(Method!$B$56="proportional",Q' + r + '/$R' + r +
      ',IF(Method!$B$56="power",IFERROR(INDEX(power_devig($L' + r + ',$M' + r + ',$N' + r + '),3,1),Q' + r + '/$R' + r +
      '),IFERROR(INDEX(shin($L' + r + ',$M' + r + ',$N' + r + '),3,1),Q' + r + '/$R' + r + '))))',
    '=IF(S' + r + '="","",Method!$B$4*IF(BC' + r + '="",I' + r + ',BC' + r + ')+(1-Method!$B$4)*S' + r + '/($S' + r + '+$T' + r + '+$U' + r + '))',
    '=IF(T' + r + '="","",Method!$B$4*IF(BD' + r + '="",J' + r + ',BD' + r + ')+(1-Method!$B$4)*T' + r + '/($S' + r + '+$T' + r + '+$U' + r + '))',
    '=IF(U' + r + '="","",Method!$B$4*IF(BE' + r + '="",K' + r + ',BE' + r + ')+(1-Method!$B$4)*U' + r + '/($S' + r + '+$T' + r + '+$U' + r + '))',
    '=IF(V' + r + '="","",V' + r + '*L' + r + '-1)',
    '=IF(W' + r + '="","",W' + r + '*M' + r + '-1)',
    '=IF(X' + r + '="","",X' + r + '*N' + r + '-1)',
    '=IF(Y' + r + '="","",MATCH(MAX(Y' + r + ':AA' + r + '),Y' + r + ':AA' + r + ',0))',
    '=IF(AB' + r + '="","",CHOOSE(AB' + r + ',"H","D","A"))',
    '=IF(AB' + r + '="","",CHOOSE(AB' + r + ',"1  "&D' + r + ',"X  Draw","2  "&E' + r + '))',
    '=IF(AB' + r + '="","",INDEX(L' + r + ':N' + r + ',1,AB' + r + '))',
    '=IF(AB' + r + '="","",INDEX(Y' + r + ':AA' + r + ',1,AB' + r + '))',
    '=IF(AB' + r + '="","",INDEX(V' + r + ':X' + r + ',1,AB' + r + ')-INDEX(S' + r + ':U' + r + ',1,AB' + r + '))',
    // v2.3.14 KNOCKOUT GUARDS: two new auto-PASS gates run BEFORE the BET
    // check. If either trips, decision = PASS regardless of EV/edge. See WHY
    // formula below for the surfaced reason. Method!$B$87 = EV ceiling.
    // Method!$B$88 = max calibrated model_p / devigged market_p for the pick.
    '=IF(AB' + r + '="","· enter odds",IF(OR(AF' + r + '>Method!$B$87,' +
      'IFERROR(INDEX(BC' + r + ':BE' + r + ',1,AB' + r + ')/INDEX(S' + r + ':U' + r + ',1,AB' + r + '),0)>Method!$B$88),' +
      '"PASS",IF(AND(AF' + r + '>=Method!$B$7,AG' + r + '>=Method!$B$8),"BET","PASS")))',
    '=IF(AH' + r + '<>"BET",0,ROUND(MIN(Method!$B$6,Method!$B$5*AL' + r + ')*100,1))',
    '=IF(OR(AH' + r + '<>"BET",Method!$B$55=TRUE()),0,IF(AI' + r + '*Method!$B$10/100<Method!$B$9,0,ROUND(AI' + r + '*Method!$B$10/100,0)))',
    '=IF(AB' + r + '="","",INDEX(V' + r + ':X' + r + ',1,AB' + r + '))',
    '=IF(AH' + r + '<>"BET","",MAX(0,(AK' + r + '*AE' + r + '-1)/(AE' + r + '-1)))',
    '=IF(AH' + r + '="BET",AF' + r + '+A' + r + '/1000000,"")',
    '=IF(Matchday!N' + rm + '="","",Matchday!N' + rm + ')',
    '=IF(Matchday!P' + rm + '="","",Matchday!P' + rm + ')',
    '=IF(Matchday!Q' + rm + '="","",Matchday!Q' + rm + ')',
    '=IF(Matchday!R' + rm + '="","",Matchday!R' + rm + ')',
  ];
  const block_AR_AV = [
    '=IF(OR(AN' + r + '<>"Y",AQ' + r + '="",AV' + r + '=""),"",IF(AQ' + r + '="Void",0,IF(AQ' + r + '=AV' + r + ',ROUND(AP' + r + '*(AO' + r + '-1),2),-AP' + r + ')))',
    '=IF(AR' + r + '="","",SUM($AR$2:AR' + r + '))',
    // v2.3.12 CRIT-1: was 'IF(Method!$B$55=TRUE(),"🛑 PAUSED — drawdown "&...,...)'.
    // Method!B55 is OR(drawdown, B81 stale-feed, ...) — the single-branch text
    // lied "drawdown" even when B81 stale-feed was the actual cause (operator
    // saw "drawdown 0.6% ≥ 15.0%" while B51=0.6% and B52=15% — mathematically
    // impossible). The cascade below inspects B81 first, then drawdown,
    // and emits a generic-with-pointer fallback for anything else.
    // v2.3.13 LOW-3: long-shot value-pick safety gate inserted inside the
    // AH="BET" arm. When odds (AE) >= LONG_SHOT_ODDS_MIN AND edge (AG) >=
    // LONG_SHOT_EDGE_MIN, override "Value found" with a long-shot WAIT
    // that surfaces the actual odds + edge so the operator can sanity-
    // check sharp-book consensus before flipping L=Y on the Matchday row.
    '=IF(AB' + r + '="","Type the 3 odds on the Matchday tab",IF(Method!$B$55=TRUE(),IF(Method!$B$81=TRUE(),"🛑 PAUSED — feed stale > ' + STALE_MINUTES + ' min — check Live tab; refresh feed before betting.",IF(Method!$B$51>=Method!$B$52,"🛑 PAUSED — drawdown "&TEXT(Method!$B$51,"0.0%")&" ≥ "&TEXT(Method!$B$52,"0.0%")&". Set Method!B54=Y to override.","🛑 PAUSED — see Method!B49/B63 for cause; do not override stale/circuit-breaker pauses.")),IF(AH' + r + '="BET",IF(AJ' + r + '=0,"Edge found but stake rounds under £"&Method!$B$9&" — skip",IF(AND(AE' + r + '>=' + LONG_SHOT_ODDS_MIN + ',AG' + r + '>=' + LONG_SHOT_EDGE_MIN + '),"🟡 WAIT — long-shot review: odds "&TEXT(AE' + r + ',"0.00")&" + edge "&TEXT(AG' + r + ',"0.0%")&" — verify sharp-book + team news + stake comfort before Placed=Y","Value found: EV "&TEXT(AF' + r + ',"0.0%")&", edge "&TEXT(AG' + r + ',"0.0%"))),IF(OR(AF' + r + '>Method!$B$87,IFERROR(INDEX(BC' + r + ':BE' + r + ',1,AB' + r + ')/INDEX(S' + r + ':U' + r + ',1,AB' + r + '),0)>Method!$B$88),"🛑 Tail trap — auto-PASS · EV "&TEXT(AF' + r + ',"0.0%")&" (cap "&TEXT(Method!$B$87,"0%")&") · model/market "&TEXT(IFERROR(INDEX(BC' + r + ':BE' + r + ',1,AB' + r + ')/INDEX(S' + r + ':U' + r + ',1,AB' + r + '),0),"0.0")&"× (cap "&TEXT(Method!$B$88,"0.0")&"×)",IF(AF' + r + '<Method!$B$7,"Price too short — EV "&TEXT(AF' + r + ',"0.0%")&" (need "&TEXT(Method!$B$7,"0.0%")&"+)","Model ≈ market — edge "&TEXT(AG' + r + ',"0.0%")&" (need "&TEXT(Method!$B$8,"0.0%")&"+)")))))',
    '=IF(AN' + r + '<>"Y","",IF(AH' + r + '="· enter odds","⚠ type the 3 odds (1/X/2) to grade this bet",IF(OR(AV' + r + '="",AO' + r + '="",AP' + r + '=""),"⚠ log backed pick, odds & stake",IF(AZ' + r + '<>"BET","⚠ FUN BET — engine said pass · staked £"&AP' + r + '&" vs engine £"&BA' + r + ',IF(AV' + r + '<>IF(AY' + r + '="",AC' + r + ',AY' + r + '),"⚠ backed "&AV' + r + '&" but engine pick was "&IF(AY' + r + '="",AC' + r + ',AY' + r + '),IF(AP' + r + '>BA' + r + ',"⚠ stake over engine £"&BA' + r + ',IF(ABS(AO' + r + '-IFERROR(INDEX(L' + r + ':N' + r + ',1,MATCH(AV' + r + ',{"H","D","A"},0)),AE' + r + '))>0.02,"⚠ odds moved — re-check value","✓ engine bet")))))))',
    '=IF(Matchday!O' + rm + '="","",Matchday!O' + rm + ')',
  ];
  const block_AZ_BA = [
    '=IF(AW' + r + '="",AH' + r + ',AW' + r + ')',
    '=IF(AX' + r + '<>"",AX' + r + ',IF(AH' + r + '<>"BET",0,IF(AI' + r + '*Method!$B$10/100<Method!$B$9,0,ROUND(AI' + r + '*Method!$B$10/100,0))))',
  ];
  // v2.3.6 CRIT — L:M:N seeding. The xlsx master row 2 has L2/M2/N2 as
  // Matchday-mirror formulas. v2.3.5 P0-A seeded the entire staking matrix
  // but the very first column it depends on (L=home odds) stayed blank,
  // because the xlsx shared formula `ref="L3:L73"` stops at row 73 and the
  // post-v2.3.5 seeder never wrote L/M/N. Without these three formulas,
  // every downstream EV/devig/pick/decision/stake cell collapses to "" →
  // the engine produces ZERO bets on R32 day. The Matchday-side cells
  // (E/F/G) are the operator-typed odds — they only become reachable when
  // _matchdayKnockoutRowFormulas_ extends the Matchday sheet below row 75.
  const block_L_N = [
    '=IF(Matchday!E' + rm + '="","",Matchday!E' + rm + ')',
    '=IF(Matchday!F' + rm + '="","",Matchday!F' + rm + ')',
    '=IF(Matchday!G' + rm + '="","",Matchday!G' + rm + ')',
  ];
  return { O_AQ: block_O_AQ, AR_AV: block_AR_AV, AZ_BA: block_AZ_BA, L_N: block_L_N };
}

// v2.3.6 CRIT — build the Matchday operator-view row for one knockout Bets
// row `r` (Matchday row = r+2). Mirrors the xlsx master Matchday row 4
// shape: formulas for A/B/C/D/H/I/J/K/M/S/U (auto-derived from Bets row r);
// nulls mark operator-input cells (E/F/G odds, L team-news flag,
// N/O/P/Q/R bet management, T closing odds) which we deliberately leave
// blank so the operator can type into them. Without this helper, Matchday
// rows 76-107 stay completely empty — the documented "operator types odds
// into Matchday" workflow has no surface for knockouts, and the L:N block
// above resolves to nothing.
function _matchdayKnockoutRowFormulas_(r) {
  const rm = r + 2;
  return [
    '=Bets!A' + r,                                                        // A: match #
    '=Bets!B' + r,                                                        // B: date
    '=IF(Bets!H' + r + '="","TBC",Bets!H' + r + ')',                      // C: time
    '=Bets!D' + r + '&"  v  "&Bets!E' + r,                                // D: home v away
    null, null, null,                                                     // E/F/G: ODDS (operator)
    '=Bets!AD' + r,                                                       // H: pick label
    '=Bets!AH' + r,                                                       // I: decision
    '=Bets!AJ' + r,                                                       // J: stake
    '=Bets!AT' + r,                                                       // K: status msg
    null,                                                                 // L: team-news flag (operator)
    // v2.3.12 CRIT-1: Matchday!M was 'IF(Method!$B$55=TRUE(),"🛑 PAUSED —
    // drawdown protection. Override Method!B54=Y if intended.",...)'. Same
    // misattribution bug as Bets!AT (line 2179) — B55 is OR(drawdown, B81,
    // ...) but the message blindly said "drawdown protection." Operator on
    // the Matchday tab (the documented "only tab you touch") saw "drawdown
    // protection" while the actual cause was a stale upstream feed. The
    // cascade below inspects B81 first, then drawdown, then a fallback.
    // v2.3.13 LOW-3: long-shot value-pick safety gate inserted inside the
    // L="" arm of the team-news cascade. When odds (Bets!AE) >=
    // LONG_SHOT_ODDS_MIN AND edge (Bets!AG) >= LONG_SHOT_EDGE_MIN, override
    // "🟡 WAIT — check team news first" with a long-shot WAIT that calls
    // out the actual odds + edge. Operator runs the 3-cell checklist
    // (sharp-book / team news / stake comfort) and types L=Y to override
    // → engine flips to "🟢 PLACE £..." as normal.
    '=IF(Bets!AH' + r + '="· enter odds","",IF(Method!$B$55=TRUE(),IF(Method!$B$81=TRUE(),"🛑 PAUSED — feed stale (>' + STALE_MINUTES + ' min); refresh feed before betting.",IF(Method!$B$51>=Method!$B$52,"🛑 PAUSED — drawdown "&TEXT(Method!$B$51,"0.0%")&" ≥ "&TEXT(Method!$B$52,"0.0%")&". Set Method!B54=Y to override.","🛑 PAUSED — see Method!B49/B63 for cause; do not override stale/circuit-breaker pauses.")),IF(Bets!AH' + r + '<>"BET",IF(OR(Bets!AF' + r + '>Method!$B$87,IFERROR(INDEX(Bets!BC' + r + ':BE' + r + ',1,Bets!AB' + r + ')/INDEX(Bets!S' + r + ':U' + r + ',1,Bets!AB' + r + '),0)>Method!$B$88),"🛑 NO BET — tail trap (EV "&TEXT(Bets!AF' + r + ',"0.0%")&" / model "&TEXT(IFERROR(INDEX(Bets!BC' + r + ':BE' + r + ',1,Bets!AB' + r + ')/INDEX(Bets!S' + r + ':U' + r + ',1,Bets!AB' + r + '),0),"0.0")&"× market)","❌ NO BET — price not good enough"),IF(Bets!AJ' + r + '=0,"❌ NO BET — stake under minimum",IF(L' + rm + '="",IF(AND(Bets!AE' + r + '>=' + LONG_SHOT_ODDS_MIN + ',Bets!AG' + r + '>=' + LONG_SHOT_EDGE_MIN + '),"🟡 WAIT — long-shot review: odds "&TEXT(Bets!AE' + r + ',"0.00")&" + edge "&TEXT(Bets!AG' + r + ',"0.0%")&" — verify sharp-book + team news + stake comfort","🟡 WAIT — check team news first"),IF(L' + rm + '="N","🔴 SKIP — team news concern",IF(L' + rm + '="Y","🟢 PLACE £"&Bets!AJ' + r + '&" on "&Bets!AD' + r + ',"")))))))',  // M: decision msg
    null, null, null, null, null,                                         // N/O/P/Q/R: bet mgmt (operator)
    '=Bets!AU' + r,                                                       // S: audit msg
    null,                                                                 // T: closing odds (operator)
    '=IF(OR(T' + rm + '="",P' + rm + '=""),"",P' + rm + '/T' + rm + '-1)', // U: CLV
  ];
}

function extendToKnockouts() {
  const ss = SpreadsheetApp.getActive();
  const bets = ss.getSheetByName(SHEET.bets);
  if (!bets) throw new Error('Sheet "' + SHEET.bets + '" not found');

  // Read the full target window in one shot (32 rows × 1 col).
  const nWindow = KNOCKOUT_LAST_ROW - KNOCKOUT_FIRST_ROW + 1;
  const existing = bets.getRange(KNOCKOUT_FIRST_ROW, BETS_COL.matchNo, nWindow, 1).getValues();

  let added = 0, skipped = 0, conflicts = 0;
  const matchNoOut = [];
  const notesOut = [];
  for (let i = 0; i < nWindow; i++) {
    const targetM = KNOCKOUT_FIRST_M + i;
    const cell = existing[i][0];
    if (cell === '' || cell === null) {
      const meta = _knockoutStageFor_(targetM);
      matchNoOut.push([targetM]);
      notesOut.push([meta ? (meta.stage + ' — bracket slot ' + meta.slot) : '']);
      added++;
    } else if (Number(cell) === targetM) {
      matchNoOut.push([cell]);
      notesOut.push([null]);   // leave existing note alone
      skipped++;
    } else {
      // Row already holds a *different* match_no — refuse to overwrite. Tag
      // it and surface in status so the operator can reconcile manually.
      matchNoOut.push([cell]);
      notesOut.push([null]);
      conflicts++;
    }
  }

  // Write match_no column only for rows we're populating (preserves existing
  // values byte-for-byte where skipped/conflicted).
  bets.getRange(KNOCKOUT_FIRST_ROW, BETS_COL.matchNo, nWindow, 1).setValues(matchNoOut);

  // Apply notes individually — setNotes on the range would clobber non-null
  // existing notes, so we only touch cells where we set a non-null note.
  for (let i = 0; i < notesOut.length; i++) {
    if (notesOut[i][0] !== null) {
      bets.getRange(KNOCKOUT_FIRST_ROW + i, BETS_COL.matchNo).setNote(notesOut[i][0]);
    }
  }

  // Seed BC:BE mirror formulas for any row that doesn't already carry them.
  // This matches v2.3.1 CRIT #3 — _writeCalibratedProbs_ falls back to raw
  // I:K, so the mirror formula is the safe initial state until calibration
  // bins land for this row.
  const bcRange = bets.getRange(KNOCKOUT_FIRST_ROW, BETS_COL.pHomeCorr, nWindow, 3);
  const bcExisting = bcRange.getFormulas();
  const bcOut = [];
  let seeded = 0;
  for (let i = 0; i < nWindow; i++) {
    const r = KNOCKOUT_FIRST_ROW + i;
    const row = bcExisting[i];
    if (row[0] || row[1] || row[2]) {
      bcOut.push([row[0], row[1], row[2]]);
    } else {
      // v2.3.3 HIGH #5: blank-safe mirror formulas. Pre-fix, `=Ir` rendered
      // 0 in cells where I/J/K were blank (group-stage rows haven't been
      // touched yet, or refreshAll has skipped this match because λ was
      // stale). A literal 0 in BC:BE then poisoned downstream EV math
      // (BC * L - 1 = -1 → false PASS signals). IF guard surfaces blank.
      bcOut.push([
        '=IF(I' + r + '="","",I' + r + ')',
        '=IF(J' + r + '="","",J' + r + ')',
        '=IF(K' + r + '="","",K' + r + ')',
      ]);
      seeded++;
    }
  }
  bcRange.setFormulas(bcOut);

  // v2.3.5 P0-A: seed the full staking-formula matrix on the 32 knockout rows.
  // Pre-fix, extendToKnockouts only seeded match_no + BC:BE — leaving cols
  // O..AQ, AR..AV, AZ..BA completely blank → no devig, no EV, no pick, no
  // decision, no stake on the day R32 starts. xlsx shared formulas stop at
  // row 73 (ref="R2:R73"), so they do NOT autofill into rows 74..105.
  // Apply same preservation pattern as BC:BE: if the row in this block
  // already holds ANY formula, leave the whole block untouched (operator
  // may have hand-edited a single row); else seed all cells.
  let stakingSeeded_O_AQ = 0, stakingSeeded_AR_AV = 0, stakingSeeded_AZ_BA = 0;

  // Block 1: O..AQ (29 cols, col 15..43)
  const oqRange = bets.getRange(KNOCKOUT_FIRST_ROW, 15, nWindow, 29);
  const oqExisting = oqRange.getFormulas();
  const oqOut = [];
  for (let i = 0; i < nWindow; i++) {
    const r = KNOCKOUT_FIRST_ROW + i;
    const row = oqExisting[i];
    let hasAny = false;
    for (let c = 0; c < 29; c++) { if (row[c]) { hasAny = true; break; } }
    if (hasAny) {
      oqOut.push(row);
    } else {
      oqOut.push(_knockoutStakingFormulas_(r).O_AQ);
      stakingSeeded_O_AQ++;
    }
  }
  oqRange.setFormulas(oqOut);

  // Block 2: AR..AV (5 cols, col 44..48)
  const arRange = bets.getRange(KNOCKOUT_FIRST_ROW, 44, nWindow, 5);
  const arExisting = arRange.getFormulas();
  const arOut = [];
  for (let i = 0; i < nWindow; i++) {
    const r = KNOCKOUT_FIRST_ROW + i;
    const row = arExisting[i];
    let hasAny = false;
    for (let c = 0; c < 5; c++) { if (row[c]) { hasAny = true; break; } }
    if (hasAny) {
      arOut.push(row);
    } else {
      arOut.push(_knockoutStakingFormulas_(r).AR_AV);
      stakingSeeded_AR_AV++;
    }
  }
  arRange.setFormulas(arOut);

  // Block 3: AZ..BA (2 cols, col 52..53)
  const azRange = bets.getRange(KNOCKOUT_FIRST_ROW, 52, nWindow, 2);
  const azExisting = azRange.getFormulas();
  const azOut = [];
  for (let i = 0; i < nWindow; i++) {
    const r = KNOCKOUT_FIRST_ROW + i;
    const row = azExisting[i];
    if (row[0] || row[1]) {
      azOut.push(row);
    } else {
      azOut.push(_knockoutStakingFormulas_(r).AZ_BA);
      stakingSeeded_AZ_BA++;
    }
  }
  azRange.setFormulas(azOut);

  // v2.3.6 CRIT — Block 4: L..N (12..14) operator-odds mirror. Seeds the
  // Matchday-mirror formulas that v2.3.5 P0-A assumed would already exist
  // but never wrote. With L:N blank, the entire staking matrix downstream
  // collapses to "" → engine produces ZERO bets on R32 day.
  let stakingSeeded_L_N = 0;
  const lnRange = bets.getRange(KNOCKOUT_FIRST_ROW, 12, nWindow, 3);
  const lnExisting = lnRange.getFormulas();
  const lnOut = [];
  for (let i = 0; i < nWindow; i++) {
    const r = KNOCKOUT_FIRST_ROW + i;
    const row = lnExisting[i];
    if (row[0] || row[1] || row[2]) {
      lnOut.push(row);
    } else {
      lnOut.push(_knockoutStakingFormulas_(r).L_N);
      stakingSeeded_L_N++;
    }
  }
  lnRange.setFormulas(lnOut);

  // v2.3.6 CRIT — Matchday extension. Without seeding rows 76-107 on the
  // Matchday sheet, the operator's documented input surface (E/F/G for the
  // 1/X/2 odds, L for the team-news flag, N..T for bet management) is
  // entirely absent for knockouts. The L..N mirror above resolves to ""
  // until the matching Matchday row exists with operator-typed E/F/G.
  let matchdaySeeded = 0;
  let matchdaySkipped = 0;
  const md = ss.getSheetByName(SHEET.matchday);
  if (md) {
    // Matchday row = Bets row + 2 (Matchday has two extra header rows).
    const mdFirstRow = KNOCKOUT_FIRST_ROW + 2;            // 76
    const mdRange = md.getRange(mdFirstRow, 1, nWindow, 21);   // A..U
    const mdExisting = mdRange.getFormulas();
    const mdValues = mdRange.getValues();
    const mdOut = [];
    for (let i = 0; i < nWindow; i++) {
      const r = KNOCKOUT_FIRST_ROW + i;
      const existingRow = mdExisting[i];
      const valuesRow = mdValues[i];
      // If ANY cell already holds a formula OR an operator-typed value, leave
      // the whole row untouched. This is conservative: if operator pasted
      // odds into E76 already, we won't clobber the rest of the row.
      let hasAny = false;
      for (let c = 0; c < 21; c++) {
        if (existingRow[c] || (valuesRow[c] !== '' && valuesRow[c] !== null)) {
          hasAny = true; break;
        }
      }
      if (hasAny) {
        mdOut.push(existingRow.map(function(f, c) {
          return f || valuesRow[c];   // preserve formulas; fallback to values
        }));
        matchdaySkipped++;
      } else {
        const newRow = _matchdayKnockoutRowFormulas_(r);
        // Convert nulls (operator-input markers) to '' so setValues accepts.
        mdOut.push(newRow.map(function(v) { return v === null ? '' : v; }));
        matchdaySeeded++;
      }
    }
    // setValues handles both formulas (strings starting with =) and literals.
    mdRange.setValues(mdOut);
  }

  let msg = 'Extend to knockouts: added ' + added + ' new row(s), ' +
            skipped + ' already-correct, ' + seeded + ' BC:BE formula(s) seeded, ' +
            'staking O..AQ=' + stakingSeeded_O_AQ + ' AR..AV=' + stakingSeeded_AR_AV +
            ' AZ..BA=' + stakingSeeded_AZ_BA + ' L..N=' + stakingSeeded_L_N +
            ' row(s) seeded · Matchday rows seeded=' + matchdaySeeded +
            ' preserved=' + matchdaySkipped;
  if (conflicts) {
    msg += '. CONFLICT: ' + conflicts + ' row(s) hold a non-knockout match_no — ' +
           'inspect Bets rows ' + KNOCKOUT_FIRST_ROW + '..' + KNOCKOUT_LAST_ROW;
    // v2.3.3 MED #12: also surface in Live!intelWarn so the operator
    // notices without re-running the menu item. Pre-fix the conflict
    // counter only appeared in _writeStatus_, which gets overwritten on
    // the next tick.
    const ss = SpreadsheetApp.getActive();
    const live = ss.getSheetByName(SHEET.live);
    if (live) {
      const cur = String(live.getRange(LIVE_CELL.intelWarn).getValue() || '');
      const tag = 'knockout extend: ' + conflicts + ' conflict row(s) rows ' +
                  KNOCKOUT_FIRST_ROW + '..' + KNOCKOUT_LAST_ROW;
      live.getRange(LIVE_CELL.intelWarn).setValue(cur ? (cur + ' · ' + tag) : tag);
    }
  }
  _writeStatus_(msg);
  return {
    added: added, skipped: skipped, seeded: seeded, conflicts: conflicts,
    stakingSeeded: {
      O_AQ: stakingSeeded_O_AQ,
      AR_AV: stakingSeeded_AR_AV,
      AZ_BA: stakingSeeded_AZ_BA,
      L_N: stakingSeeded_L_N,
    },
    matchday: { seeded: matchdaySeeded, skipped: matchdaySkipped },
  };
}

function _upgradeKnockoutManagedFormulas_() {
  const ss = SpreadsheetApp.getActive();
  const bets = ss.getSheetByName(SHEET.bets);
  if (!bets) throw new Error('Sheet "' + SHEET.bets + '" not found');

  let upgraded = 0, skipped = 0, conflicts = 0;
  for (let r = KNOCKOUT_FIRST_ROW; r <= KNOCKOUT_LAST_ROW; r++) {
    const f = _knockoutStakingFormulas_(r);

    for (let i = 0; i < 3; i++) {
      const res = _setManagedFormula_(bets, r, 12 + i, f.L_N[i]);      // L:N
      if (res === 'upgraded') upgraded++;
      else if (res === 'conflict') conflicts++;
      else skipped++;
    }

    const blendCols = [22, 23, 24]; // V:X — blended calibrated model + market probs
    for (let i = 0; i < blendCols.length; i++) {
      const col = blendCols[i];
      const res = _setManagedFormula_(bets, r, col, f.O_AQ[col - 15]);
      if (res === 'upgraded') upgraded++;
      else if (res === 'conflict') conflicts++;
      else skipped++;
    }

    // AH:AJ — decision, raw Kelly %, stake. These carry the v2.3.14
    // tail-trap gates and Method!B55 pause behavior.
    const decisionCols = [34, 35, 36];
    for (let i = 0; i < decisionCols.length; i++) {
      const col = decisionCols[i];
      const res = _setManagedFormula_(bets, r, col, f.O_AQ[col - 15]);
      if (res === 'upgraded') upgraded++;
      else if (res === 'conflict') conflicts++;
      else skipped++;
    }

    // AT:AU — operator-facing WHY + settlement audit. AT is where the
    // v2.3.12 stale-vs-drawdown cascade and v2.3.13 long-shot review live.
    const msgCols = [46, 47];
    for (let i = 0; i < msgCols.length; i++) {
      const col = msgCols[i];
      const res = _setManagedFormula_(bets, r, col, f.AR_AV[col - 44]);
      if (res === 'upgraded') upgraded++;
      else if (res === 'conflict') conflicts++;
      else skipped++;
    }

    // AZ:BA — snapshot-or-current decision/stake, safe to upgrade when the
    // cells are formulas/blank. Direct operator overrides are preserved.
    const snapCols = [52, 53];
    for (let i = 0; i < snapCols.length; i++) {
      const col = snapCols[i];
      const res = _setManagedFormula_(bets, r, col, f.AZ_BA[col - 52]);
      if (res === 'upgraded') upgraded++;
      else if (res === 'conflict') conflicts++;
      else skipped++;
    }

    const mirror = [
      '=IF(I' + r + '="","",I' + r + ')',
      '=IF(J' + r + '="","",J' + r + ')',
      '=IF(K' + r + '="","",K' + r + ')',
    ];
    for (let i = 0; i < 3; i++) {
      const res = _setManagedFormula_(bets, r, BETS_COL.pHomeCorr + i, mirror[i]);
      if (res === 'upgraded') upgraded++;
      else if (res === 'conflict') conflicts++;
      else skipped++;
    }
  }

  const md = ss.getSheetByName(SHEET.matchday);
  if (md) {
    const managedMatchdayCols = [1, 2, 3, 4, 8, 9, 10, 11, 13, 19, 21];
    for (let r = KNOCKOUT_FIRST_ROW; r <= KNOCKOUT_LAST_ROW; r++) {
      const rm = r + 2;
      const row = _matchdayKnockoutRowFormulas_(r);
      for (let i = 0; i < managedMatchdayCols.length; i++) {
        const col = managedMatchdayCols[i];
        const formula = row[col - 1];
        if (!formula) continue;
        const res = _setManagedFormula_(md, rm, col, formula);
        if (res === 'upgraded') upgraded++;
        else if (res === 'conflict') conflicts++;
        else skipped++;
      }
    }
  }

  if (conflicts) {
    _appendLiveIntelWarning_('knockout formula upgrade: ' + conflicts +
                            ' direct-value cell(s) preserved');
  }
  _writeStatus_('Knockout formula upgrade: upgraded ' + upgraded +
                ', skipped ' + skipped + ', conflicts ' + conflicts);
  return { upgraded: upgraded, skipped: skipped, conflicts: conflicts };
}

function _setManagedFormula_(sheet, row, col, formula) {
  const cell = sheet.getRange(row, col);
  const existingFormula = cell.getFormula();
  if (existingFormula === formula) return 'skipped';

  const existingValue = cell.getValue();
  if (existingFormula || existingValue === '' || existingValue === null) {
    cell.setFormula(formula);
    return 'upgraded';
  }
  return 'conflict';
}

// =============================================================================
// AUTO-REFRESH TRIGGERS
// =============================================================================

function installAutoRefresh() {
  removeAutoRefresh();
  ScriptApp.newTrigger('refreshAll')
    .timeBased()
    .everyMinutes(POLL_MINUTES)
    .create();
  const method = SpreadsheetApp.getActive().getSheetByName(SHEET.method);
  if (method) method.getRange(M_CELL.autoStatus).setValue('ON · every ' + POLL_MINUTES + ' min');
  _writeStatus_('Auto-refresh installed (every ' + POLL_MINUTES + ' min)');
}

function removeAutoRefresh() {
  const triggers = ScriptApp.getProjectTriggers();
  let removed = 0;
  triggers.forEach(function(t) {
    const h = t.getHandlerFunction();
    if (h === 'refreshAll' || h === 'refreshModel' || h === 'refreshLive'
        || h === 'refreshIntel' || h === 'refreshDiagnostics'
        || h === 'refreshOutrights' || h === 'refreshInPlay'
        || h === 'refreshMatchContext') {
      ScriptApp.deleteTrigger(t);
      removed++;
    }
  });
  const method = SpreadsheetApp.getActive().getSheetByName(SHEET.method);
  if (method) method.getRange(M_CELL.autoStatus).setValue('OFF');
  _writeStatus_('Auto-refresh removed (' + removed + ' triggers)');
}

// =============================================================================
// SHEET PROTECTION — v2.3.1: include Bets BB:BI in editable (fix CRIT #2)
// =============================================================================

function applyProtections() {
  const ss = SpreadsheetApp.getActive();
  const me = Session.getEffectiveUser();

  // v2.3.6 CRIT: extend Matchday allow-list to row 107 (Bets row 105 = m=104,
  // the Final). Pre-fix the operator's input range stopped at row 75 — even
  // with the Matchday rows now extended for knockouts, the operator was
  // locked out of typing odds in E76..G107. Bumping bounds to :107.
  const editable = {
    Matchday: ['E4:G107', 'L4:L107', 'N4:R107', 'T4:T107'],
    Method:   ['B3', 'B5:B9', 'B13:B14', 'B46', 'B48:B49', 'B52', 'B54', 'B56'],
    'Model Refresh': ['B8:D79'],
    'Model vs Market': ['G4:G15'],
    Bets: ['AW2:AY999', 'BB2:BI999'],     // include script-managed v2.3.1 cols
    Live: ['B3:B5'],
    Outrights: ['L4:L100'],
    'In-Play': [],
  };

  Object.keys(editable).forEach(function(sheetName) {
    const sh = ss.getSheetByName(sheetName);
    if (!sh) return;
    sh.getProtections(SpreadsheetApp.ProtectionType.SHEET).forEach(function(p) {
      if (p.canEdit()) p.remove();
    });
    const p = sh.protect().setDescription('WC26 v2.3.6 — formulas locked');
    const ranges = editable[sheetName].map(function(a1) { return sh.getRange(a1); });
    if (ranges.length) p.setUnprotectedRanges(ranges);
    p.addEditor(me);
    p.removeEditors(p.getEditors().filter(function(u) { return u.getEmail() !== me.getEmail(); }));
    if (p.canDomainEdit && typeof p.canDomainEdit === 'function') {
      try { p.setDomainEdit(false); } catch (_) {}
    }
    p.setWarningOnly(false);
  });
  _writeStatus_('Protections applied to ' + Object.keys(editable).length + ' sheet(s)');
}

function removeProtections() {
  const ss = SpreadsheetApp.getActive();
  ss.getSheets().forEach(function(sh) {
    sh.getProtections(SpreadsheetApp.ProtectionType.SHEET).forEach(function(p) {
      if (p.canEdit() && p.getDescription().indexOf('WC26') === 0) p.remove();
    });
  });
  _writeStatus_('All WC26 protections removed');
}

// =============================================================================
// CUSTOM FUNCTIONS — SHIN, POWER_DEVIG, CALIBRATE
// =============================================================================

/** @customfunction */
function SHIN(oHome, oDraw, oAway) {
  const pis = _validateOdds_(oHome, oDraw, oAway);
  const V = pis.reduce(function(a, b) { return a + b; }, 0);
  if (V <= 1.0001) return _col_(pis.map(function(p) { return p / V; }));
  const N = pis.length;
  const target = function(z) {
    let s = 0;
    for (let i = 0; i < N; i++) s += Math.sqrt(z * z + 4 * (1 - z) / V * pis[i] * pis[i]);
    return s - (2 + (N - 2) * z);
  };
  let lo = 0.0, hi = 0.999;
  for (let i = 0; i < 200; i++) {
    const mid = 0.5 * (lo + hi);
    const f = target(mid);
    if (Math.abs(f) < 1e-12) { lo = hi = mid; break; }
    if (f > 0) lo = mid; else hi = mid;
  }
  const z = 0.5 * (lo + hi);
  const out = pis.map(function(p) {
    if (z >= 0.999) return p / V;
    return (Math.sqrt(z * z + 4 * (1 - z) / V * p * p) - z) / (2 * (1 - z));
  });
  const s = out.reduce(function(a, b) { return a + b; }, 0);
  return _col_(out.map(function(o) { return o / s; }));
}

/** @customfunction */
function POWER_DEVIG(oHome, oDraw, oAway) {
  const pis = _validateOdds_(oHome, oDraw, oAway);
  const sumPi = pis.reduce(function(a, b) { return a + b; }, 0);
  if (sumPi <= 1.0001) return _col_(pis.map(function(p) { return p / sumPi; }));
  let lo = 0.5, hi = 5.0;
  for (let i = 0; i < 200; i++) {
    const k = 0.5 * (lo + hi);
    let s = 0;
    for (let j = 0; j < pis.length; j++) s += Math.pow(pis[j], k);
    if (Math.abs(s - 1) < 1e-12) { lo = hi = k; break; }
    if (s > 1) lo = k; else hi = k;
  }
  const k = 0.5 * (lo + hi);
  const raw = pis.map(function(p) { return Math.pow(p, k); });
  const s = raw.reduce(function(a, b) { return a + b; }, 0);
  return _col_(raw.map(function(p) { return p / s; }));
}

/**
 * Equal-width-bin reliability lookup from /calibration.json.
 *
 * v2.3.3 HIGH #4: docstring fix — this is NOT isotonic regression. The
 * feed exposes pre-aggregated equal-width bins (mean_pred, actual_freq)
 * per market; _interp_() does piecewise-linear interpolation between the
 * bin centres. True isotonic regression would require either the raw
 * (predicted, outcome) pairs (not exposed) or PAVA-monotonised bins
 * (the feed does not pre-monotonise). The output is therefore a raw
 * reliability remap, NOT a guaranteed monotone calibrator.
 *
 * Consequence: callers must not assume CALIBRATE(home) + CALIBRATE(draw)
 * + CALIBRATE(away) ≈ 1 — bin-wise remaps in three independent markets
 * routinely sum to 0.94-1.07 in live data. _writeCalibratedProbs_ at
 * line ~1424 renormalises the triple after lookup; ad-hoc spreadsheet
 * usage of CALIBRATE() in isolation must renormalise downstream.
 *
 * v2.3.1: reads from PropertiesService (works in custom-function context).
 * Custom function does NOT call UrlFetchApp — that would blow the daily
 * 20,000-fetch quota if the formula is dragged across many cells. Bins are
 * cached by refreshDiagnostics() (runs every POLL_MINUTES via the time
 * trigger). If no bins are cached yet, returns the raw probability.
 *
 * @param {number} p Model probability in [0, 1].
 * @param {string} market "home" | "draw" | "away".
 * @return Reliability-remapped probability (linear interp between bin actual_freqs).
 * @customfunction
 */
function CALIBRATE(p, market) {
  const pp = Number(p);
  if (!isFinite(pp) || pp < 0 || pp > 1) return p;
  const m = String(market || '').toLowerCase();
  if (m !== 'home' && m !== 'draw' && m !== 'away') return p;
  const bins = _calibrationBinsCached_(m);
  if (!bins || !bins.length) return p;
  return _interp_(pp, bins);
}

function _calibrationBinsCached_(market) {
  // Custom-function-safe: PropertiesService is readable in custom-function
  // context. CacheService.getDocumentCache() returns null there.
  try {
    const props = PropertiesService.getScriptProperties();
    const ts = Number(props.getProperty(CALIBRATION_PROP_TS));
    if (isFinite(ts) && (Date.now() - ts) < CALIBRATION_TTL_MS * 2) {
      const raw = props.getProperty(CALIBRATION_PROP_KEY);
      if (raw) {
        const obj = JSON.parse(raw);
        return obj[market] || [];
      }
    }
  } catch (_) {}
  return [];
}

function _writeCalibratedProbs_(bets, firstRow, rawProbs, matchNos) {
  const home = _calibrationBinsCached_('home');
  const draw = _calibrationBinsCached_('draw');
  const away = _calibrationBinsCached_('away');
  // v2.3.5 H-1 helper: NaN/non-finite → '' (Apps Script setValues throws
  // "Invalid argument" on NaN; '' is the cell-empty marker).
  function _safe_(v) {
    const n = Number(v);
    return isFinite(n) ? n : '';
  }
  function _mirror_(rawProbs) {
    return rawProbs.map(function(row) {
      return [_safe_(row[0]), _safe_(row[1]), _safe_(row[2])];
    });
  }
  if (!home.length || !draw.length || !away.length) {
    // Mirror raw probs so BC:BE is never blank. v2.3.5 H-1: coerce
    // NaN/non-finite to '' so setValues doesn't throw on partial payloads.
    bets.getRange(firstRow, BETS_COL.pHomeCorr, rawProbs.length, 3)
        .setValues(_mirror_(rawProbs));
    return;
  }
  // v2.3.3 HIGH #4: per-row renormalisation. Independent bin-wise remaps
  // of (home, draw, away) routinely sum to 0.94-1.07 because the three
  // markets are calibrated independently. Pre-fix, BC:BE could push to
  // 1.07 and contaminate downstream EV calculations. Renormalising on
  // the row makes the calibrated triple a proper probability vector.
  //
  // v2.3.4 HIGH #3: fallback bug — v2.3.3 returned `[ch, cd, ca]` on the
  // non-finite path. But `_interp_` returns the empty string '' when its
  // input is non-finite. `Number('') === 0` and `isFinite(0) === true`,
  // so the non-finite guard didn't catch it; the second guard (sum) saw
  // s === 0 and DID catch it, but then returned `['','','']` anyway.
  // BC:BE got literal '' strings → `=BC*L-1` evaluated to #VALUE! → the
  // EV / staking columns silently broke for any row whose model prob
  // collapsed to NaN/undefined. Now the fallback uses the row's raw
  // probabilities so BC:BE remains a valid number triple.
  //
  // v2.3.4 HIGH #4: cap inputs to bin range and surface a soft warning
  // when a row's calibration input lands outside the trained range. The
  // bins for the draw market top out near 0.30 in current calibration,
  // so any model draw prob > 0.30 silently clamped to the last bin's
  // actual_freq (~0.246). That's the right thing to DO numerically (the
  // calibrator has no signal above 0.30), but the operator never saw
  // that it happened. Track per-row OOB events and surface a count on
  // Live!intelWarn via the closure below.
  const oobCounts = { home: 0, draw: 0, away: 0 };
  let calibratedRows = 0, koSkipped = 0;
  const out = rawProbs.map(function(row, i) {
    // v2.3.5 P0-B: short-circuit knockout rows. The calibration LUT is a
    // binned reliability curve trained on group-stage matches only; the
    // draw bins (top out near 0.30 mean_pred) don't generalise to
    // knockouts where the draw outcome is decided by penalties (i.e. the
    // "draw" market resolves on 90 min only, with a very different prior).
    // Mirror raw I:K → BC:BE for knockout rows so downstream EV math stays
    // valid without applying a mis-trained correction.
    const m = matchNos ? Number(matchNos[i] && matchNos[i][0]) : NaN;
    if (isFinite(m) && m >= KNOCKOUT_FIRST_M) {
      koSkipped++;
      return [_safe_(row[0]), _safe_(row[1]), _safe_(row[2])];
    }
    const rh = Number(row[0]), rd = Number(row[1]), ra = Number(row[2]);
    if (isFinite(rh) && _isOutOfBinRange_(rh, home)) oobCounts.home++;
    if (isFinite(rd) && _isOutOfBinRange_(rd, draw)) oobCounts.draw++;
    if (isFinite(ra) && _isOutOfBinRange_(ra, away)) oobCounts.away++;
    const ch = _interp_(row[0], home);
    const cd = _interp_(row[1], draw);
    const ca = _interp_(row[2], away);
    const nh = Number(ch), nd = Number(cd), na = Number(ca);
    // Fallback to RAW probs on any degeneracy.
    // v2.3.4 HIGH #3 + v2.3.5 H-1: coerce NaN/non-finite to '' on the
    // fallback row because Apps Script setValues throws "Invalid argument"
    // on NaN cells. `Number(undefined)===NaN` so this triggers whenever
    // a payload field is missing.
    const allFinite = isFinite(nh) && isFinite(nd) && isFinite(na) &&
                      ch !== '' && cd !== '' && ca !== '';
    if (!allFinite) return [_safe_(rh), _safe_(rd), _safe_(ra)];
    const s = nh + nd + na;
    if (!(s > 0) || !isFinite(s)) return [_safe_(rh), _safe_(rd), _safe_(ra)];
    calibratedRows++;
    return [nh / s, nd / s, na / s];
  });
  bets.getRange(firstRow, BETS_COL.pHomeCorr, out.length, 3).setValues(out);
  // v2.3.4 HIGH #4 / v2.3.5 H-2 + M-1: surface OOB-clamp counts on a
  // DEDICATED cell (LIVE_CELL.calibrationOob = B27), not on intelWarn
  // (B24 was already shared by refreshIntel and extendToKnockouts; whichever
  // ran later in refreshAll won and the others' messages were lost).
  // Denominator added (home=X/N) so the operator can distinguish
  // "5 OOB / 48 rows" (normal — draw bin tops at ~0.30) from "5 OOB / 8 rows"
  // (model in distress).
  try {
    const total = oobCounts.home + oobCounts.draw + oobCounts.away;
    const denom = calibratedRows; // group-stage rows actually calibrated this tick
    const ss = SpreadsheetApp.getActive();
    const live = ss.getSheetByName(SHEET.live);
    if (live) {
      const cell = live.getRange(LIVE_CELL.calibrationOob);
      if (total > 0 && denom > 0) {
        const msg = '⚠ calibration OOB (clamp to last bin): ' +
                    'home=' + oobCounts.home + '/' + denom +
                    ' draw=' + oobCounts.draw + '/' + denom +
                    ' away=' + oobCounts.away + '/' + denom +
                    (koSkipped > 0 ? ' · ' + koSkipped + ' knockout row(s) skipped' : '');
        cell.setValue(msg);
      } else if (koSkipped > 0) {
        cell.setValue(koSkipped + ' knockout row(s) skipped (no calib LUT) · group OOB: 0');
      } else {
        cell.setValue('');
      }
    }
  } catch (_) { /* non-fatal: warning surface only */ }
}

// v2.3.4 HIGH #4 helper: returns true iff p is outside [first_bin.mean_pred,
// last_bin.mean_pred], which is where _interp_'s clamp behaviour kicks in.
// Used by _writeCalibratedProbs_ to count silent-clamp events per market.
function _isOutOfBinRange_(p, bins) {
  if (!bins || !bins.length) return false;
  const lo = bins[0].mean_pred, hi = bins[bins.length - 1].mean_pred;
  return p < lo || p > hi;
}

function _interp_(p, bins) {
  // v2.3.4 HIGH #3: explicitly reject sheet-empty markers BEFORE the
  // numeric guard. isFinite('') is true because Number('')===0 — pre-v2.3.4
  // an empty-string input would coerce to 0, pass the isFinite check, and
  // return bins[0].actual_freq (the bottom of the calibration curve),
  // silently substituting "no signal" for "0% probability". Callers depend
  // on '' propagating so _writeCalibratedProbs_ can detect the missing
  // signal and fall back to raw probs.
  if (p === '' || p === null || p === undefined) return '';
  const n = Number(p);
  if (!isFinite(n)) return '';
  if (n <= bins[0].mean_pred) return bins[0].actual_freq;
  if (n >= bins[bins.length - 1].mean_pred) return bins[bins.length - 1].actual_freq;
  for (let i = 0; i < bins.length - 1; i++) {
    const a = bins[i], b = bins[i + 1];
    if (n >= a.mean_pred && n <= b.mean_pred) {
      const t = (n - a.mean_pred) / (b.mean_pred - a.mean_pred);
      return a.actual_freq + t * (b.actual_freq - a.actual_freq);
    }
  }
  return n;
}

function _col_(arr) { return arr.map(function(v) { return [v]; }); }

function _validateOdds_(o1, o2, o3) {
  const a = Number(o1), b = Number(o2), c = Number(o3);
  if (!isFinite(a) || !isFinite(b) || !isFinite(c)) throw new Error('odds must be numeric');
  if (a <= 1 || b <= 1 || c <= 1) throw new Error('decimal odds must be > 1');
  return [1 / a, 1 / b, 1 / c];
}

// =============================================================================
// GOAL_GRID — Dixon-Coles bivariate POISSON goal-market projector
// =============================================================================
// Returns the fair probability of the requested market from an MAX_GOALS+1
// square POISSON matrix with the DC low-score correction applied at the
// (0,0), (0,1), (1,0), (1,1) cells using ρ=GOAL_GRID_TAU (default −0.13).
//
// v2.3.3 HIGH #3 — DELIBERATE DRIFT vs scripts/03_simulate.py:
//   The production sim builds its score matrix from a NEGATIVE BINOMIAL
//   (NB) marginal with team-specific dispersion (k_home, k_away) read
//   from goal_model_artifacts. The feed at /predictions_live.json does
//   NOT expose those dispersions, so this projector cannot replicate
//   the sim's NB marginals from spreadsheet inputs alone — and falling
//   back to Poisson is the only honest option available client-side.
//
//   Empirical drift Poisson-vs-NB at λ≈1.4, k≈8 (typical group-stage
//   regime): ~2-9 pp on OU2.5 and BTTS, with the magnitude rising as
//   λ approaches the NB clip. Operators consuming GOAL_GRID for stake
//   decisioning should treat its outputs as an INDEPENDENT Poisson
//   read of the same λ pair, not as a sim-replicating oracle.
//   For markets where 2-9 pp matters, defer to predictions_live's
//   pre-computed p_btts / p_over25 fields (which come from the NB sim).
//
// Boundary guard: λ_max × |τ| must be < 1 to keep all τ-corrected cells
// non-negative. With λ_clip ≈ 7 (predictions sanitised upstream) and
// τ = −0.13 → 7 × 0.13 = 0.91 < 1 ✓.
//
// Supported markets (case-insensitive):
//   "ou25"  — P(total goals > 2.5)
//   "ou15"  — P(total goals > 1.5)
//   "ou35"  — P(total goals > 3.5)
//   "btts"  — P(both teams to score)
//   "ah0"   — P(home wins) (Asian handicap 0 / home Win)
//   "csHA"  — correct-score P(home=H, away=A) for digits 0..MAX_GOALS

/**
 * @param {number} lam_h Home expected goals (λ_home).
 * @param {number} lam_a Away expected goals (λ_away).
 * @param {string} market Market key — see header.
 * @return Fair probability in [0, 1] or '' if inputs invalid.
 * @customfunction
 */
function GOAL_GRID(lam_h, lam_a, market) {
  const lh = Number(lam_h), la = Number(lam_a);
  if (!isFinite(lh) || !isFinite(la) || lh < 0 || la < 0) return '';
  // Upper bound to defend against degenerate-matrix collapse when exp(-λ)
  // underflows to 0 (~λ≥745 in IEEE 754). Production λ is clipped to ~7
  // upstream; 50 is a generous defense-in-depth ceiling.
  if (lh > 50 || la > 50) return '';
  const m = String(market || '').toLowerCase();
  const M = _buildScoreMatrix_(lh, la, GOAL_GRID_MAX_GOALS, GOAL_GRID_TAU);
  if (m === 'ou25') return _sumMatrix_(M, function(h, a) { return (h + a) > 2.5; });
  if (m === 'ou15') return _sumMatrix_(M, function(h, a) { return (h + a) > 1.5; });
  if (m === 'ou35') return _sumMatrix_(M, function(h, a) { return (h + a) > 3.5; });
  if (m === 'btts') return _sumMatrix_(M, function(h, a) { return h > 0 && a > 0; });
  if (m === 'ah0')  return _sumMatrix_(M, function(h, a) { return h > a; });
  const cs = m.match(/^cs(\d+)(\d+)$/);
  if (cs) {
    const H = Number(cs[1]), A = Number(cs[2]);
    if (H < 0 || A < 0 || H > GOAL_GRID_MAX_GOALS || A > GOAL_GRID_MAX_GOALS) return '';
    return M[H][A];
  }
  return '';
}

function _poissonPmf_(lam, k) {
  if (lam <= 0) return k === 0 ? 1 : 0;
  return Math.exp(-lam) * Math.pow(lam, k) / _factorial_(k);
}

function _factorial_(n) {
  // Cache the small ints we actually hit (0..MAX_GOALS).
  if (n < 0) return NaN;
  if (n < 2) return 1;
  let f = 1;
  for (let i = 2; i <= n; i++) f *= i;
  return f;
}

function _dcTau_(h, a, lam_h, lam_a, rho) {
  // Dixon-Coles correction at the four low-score cells. Mirrors
  // scripts/03_simulate.py:dc_tau exactly so the spreadsheet view
  // and the model's stored probabilities stay numerically aligned.
  if (h === 0 && a === 0) return 1 - lam_h * lam_a * rho;
  if (h === 0 && a === 1) return 1 + lam_a * rho;
  if (h === 1 && a === 0) return 1 + lam_h * rho;
  if (h === 1 && a === 1) return 1 - rho;
  return 1;
}

function _buildScoreMatrix_(lam_h, lam_a, maxGoals, rho) {
  // Pre-compute marginal Poisson PMFs once per side, then build the
  // joint matrix with DC correction. We renormalize after correction
  // so the matrix still sums to 1 (the DC adjustment moves mass
  // between four cells; rounding + truncation at maxGoals leak the
  // rest of the tail, so explicit renorm is the safe move).
  const ph = new Array(maxGoals + 1);
  const pa = new Array(maxGoals + 1);
  for (let i = 0; i <= maxGoals; i++) {
    ph[i] = _poissonPmf_(lam_h, i);
    pa[i] = _poissonPmf_(lam_a, i);
  }
  const M = new Array(maxGoals + 1);
  let total = 0;
  for (let h = 0; h <= maxGoals; h++) {
    M[h] = new Array(maxGoals + 1);
    for (let a = 0; a <= maxGoals; a++) {
      const v = ph[h] * pa[a] * _dcTau_(h, a, lam_h, lam_a, rho);
      M[h][a] = v < 0 ? 0 : v;
      total += M[h][a];
    }
  }
  if (total > 0) {
    for (let h = 0; h <= maxGoals; h++) {
      for (let a = 0; a <= maxGoals; a++) M[h][a] /= total;
    }
  } else if (total === 0) {
    // Degenerate: every cell underflowed to 0 (e.g. λ ≥ ~745 in IEEE 754).
    // Returning an all-zero matrix would silently make every market 0%.
    // Fail loudly so the caller can surface "invalid λ" instead.
    // NaN propagation (NaN λ) falls through this else-if untouched — the
    // matrix already carries NaN and the public GOAL_GRID wrapper guards
    // NaN λ at the entry, so downstream sees the existing isFinite gate.
    throw new Error('matrix collapsed to zero — λ too large');
  }
  return M;
}

function _sumMatrix_(M, pred) {
  let s = 0;
  const n = M.length;
  for (let h = 0; h < n; h++) {
    const row = M[h];
    for (let a = 0; a < n; a++) if (pred(h, a)) s += row[a];
  }
  return s;
}

// =============================================================================
// SELF-TEST
// =============================================================================

function selfTest() {
  const checks = [];

  try {
    const shinCol = SHIN(1.50, 4.00, 7.00);
    if (!Array.isArray(shinCol) || shinCol.length !== 3 || shinCol[0].length !== 1)
      throw new Error('SHIN must return a 3x1 column');
    const shin = shinCol.map(function(r) { return r[0]; });
    const sum = shin.reduce(function(a, b) { return a + b; }, 0);
    checks.push('SHIN(1.50/4.00/7.00) = ' + shin.map(function(x) { return (x*100).toFixed(1)+'%'; }).join(' / ') +
      ' (sum=' + sum.toFixed(4) + ')');
    if (Math.abs(sum - 1) > 1e-4) throw new Error('SHIN does not sum to 1');
    if (shin[0] < 0.60 || shin[0] > 0.68) throw new Error('SHIN favourite outside 60-68% band');
  } catch (e) { checks.push('SHIN FAIL: ' + e.message); }

  try {
    const pwr = POWER_DEVIG(1.50, 4.00, 7.00).map(function(r) { return r[0]; });
    checks.push('POWER_DEVIG(1.50/4.00/7.00) = ' + pwr.map(function(x) { return (x*100).toFixed(1)+'%'; }).join(' / '));
  } catch (e) { checks.push('POWER_DEVIG FAIL: ' + e.message); }

  try {
    const live = _fetchJson_(DEFAULT_LIVE_URL);
    checks.push('Live feed OK — mode=' + live.mode + ' completed=' + (live.completed_matches_count || 0) +
      ' in_play=' + (live.in_play_count || 0));
  } catch (e) { checks.push('Live feed FAIL: ' + e.message); }

  try {
    const pred = _fetchJson_(DEFAULT_PREDICTIONS_URL);
    checks.push('Predictions feed OK — ' + (pred.match_predictions || []).length + ' matches · ' +
      (pred.team_predictions || []).length + ' teams');
  } catch (e) { checks.push('Predictions feed FAIL: ' + e.message); }

  try {
    const intel = _fetchJson_(DEFAULT_INTEL_URL);
    checks.push('Intel feed OK — components=' +
      ((intel.summary && intel.summary.total_active_components) || 0));
  } catch (e) { checks.push('Intel feed FAIL: ' + e.message); }

  try {
    const cal = _fetchJson_(DEFAULT_CALIBRATION_URL);
    const nBins = (cal.calibration && cal.calibration.home && cal.calibration.home.length) || 0;
    checks.push('Calibration feed OK — ' + nBins + ' home bins · holdout LL=' +
      (cal.holdout && cal.holdout.log_loss ? cal.holdout.log_loss.toFixed(3) : 'n/a'));
    // v2.3.3 LOW #13: selfTest must NOT write CALIBRATION_PROP_TS — doing so
    // resets the production TTL clock and masks a feed that has gone stale
    // between scheduled refreshAll ticks. Validate the calibration math
    // directly against the freshly-fetched bins instead, so the test verifies
    // end-to-end (fetch + interp + monotone clamp) without touching the
    // production cache. ScriptProperties stays under refreshAll's exclusive
    // control.
    let calOut = 'cache cold';
    if (cal.calibration && Array.isArray(cal.calibration.home) && cal.calibration.home.length) {
      const fresh = _interp_(0.65, cal.calibration.home);
      calOut = (typeof fresh === 'number') ? ((fresh * 100).toFixed(1) + '%') : String(fresh);
    }
    checks.push('CALIBRATE(0.65, "home") = ' + calOut +
      ' (raw 65%, fresh-bin interp; production cache untouched)');
  } catch (e) { checks.push('Calibration FAIL: ' + e.message); }

  try {
    const wf = _fetchJson_(DEFAULT_WALK_FORWARD_URL);
    const years = Object.keys(wf).filter(function(k) { return /^[0-9]{4}$/.test(k); });
    checks.push('Walk-forward feed OK — ' + years.length + ' WCs · 2022 lift=' +
      (wf['2022'] && wf['2022'].lift_vs_baseline !== undefined ?
        wf['2022'].lift_vs_baseline.toFixed(4) : 'n/a'));
  } catch (e) { checks.push('Walk-forward FAIL: ' + e.message); }

  SpreadsheetApp.getActive().toast(checks.join('\n'), 'Engine self-test (v2.3.13)', 30);
  Logger.log(checks.join('\n'));
}

// =============================================================================
// SNAPSHOT AT BET TIME — onEdit (unchanged from v2.2/v2.3)
// =============================================================================

function onEdit(e) {
  try {
    if (!e || !e.range) return;
    const sh = e.range.getSheet();
    if (sh.getName() !== SHEET.matchday) return;
    const PLACED_COL = 14;
    const first = e.range.getColumn();
    const last = first + e.range.getNumColumns() - 1;
    if (PLACED_COL < first || PLACED_COL > last) return;
    const bets = e.source.getSheetByName(SHEET.bets);
    if (!bets) return;
    const r0 = e.range.getRow();
    for (let i = 0; i < e.range.getNumRows(); i++) {
      const mdRow = r0 + i;
      // v2.3.6 CRIT: bump upper bound from 75 → 107 (Matchday row 107 =
      // Bets row 105 = m=104 Final). Without this the onEdit snapshot
      // never fires on knockout bets — operator marks N76="Y" and nothing
      // captures the engine decision/stake/pick into Bets AW:AY.
      if (mdRow < 4 || mdRow > 107) continue;
      const betsRow = mdRow - 2;
      const placed = String(sh.getRange(mdRow, PLACED_COL).getValue() || '').trim().toUpperCase();
      const snap = bets.getRange(betsRow, BETS_COL.snapDecision, 1, 3);
      if (placed === 'Y') {
        const dec = bets.getRange(betsRow, BETS_COL.decision).getValue();
        const stake = bets.getRange(betsRow, BETS_COL.engineStake).getValue();
        const pick = bets.getRange(betsRow, BETS_COL.pick).getValue();
        snap.setValues([[dec, stake, pick]]);
      } else {
        snap.clearContent();
      }
    }
  } catch (err) {
    Logger.log('onEdit snapshot error: ' + err);
  }
}

// =============================================================================
// HELPERS
// =============================================================================

function _fetchJson_(url) {
  let lastErr = null;
  for (let attempt = 0; attempt <= FETCH_RETRIES; attempt++) {
    try {
      const resp = UrlFetchApp.fetch(url, {
        method: 'get',
        muteHttpExceptions: true,
        followRedirects: true,
        headers: { 'Accept': 'application/json' },
        validateHttpsCertificates: true,
        deadline: FETCH_DEADLINE_MS / 1000,
      });
      const code = resp.getResponseCode();
      if (code < 200 || code >= 300) throw new Error('HTTP ' + code + ' from ' + url);
      const txt = resp.getContentText();
      try { return JSON.parse(txt); }
      catch (e) { throw new Error('Bad JSON from ' + url + ': ' + e.message); }
    } catch (e) {
      lastErr = e;
      if (attempt < FETCH_RETRIES) Utilities.sleep(FETCH_BACKOFF_MS);
    }
  }
  throw lastErr || new Error('fetch failed: ' + url);
}

function _liveUrl_(ss)        { return _overrideOrDefault_(ss, 'B3', DEFAULT_LIVE_URL); }
function _predictionsUrl_(ss) { return _overrideOrDefault_(ss, 'B4', DEFAULT_PREDICTIONS_URL); }
function _intelUrl_(ss)       { return _overrideOrDefault_(ss, 'B5', DEFAULT_INTEL_URL); }
function _calibrationUrl_(ss) { return DEFAULT_CALIBRATION_URL; }
function _walkForwardUrl_(ss) { return DEFAULT_WALK_FORWARD_URL; }

function _overrideOrDefault_(ss, a1, def) {
  const live = ss.getSheetByName(SHEET.live);
  if (live) {
    const v = String(live.getRange(a1).getValue() || '').trim();
    if (v) return v;
  }
  return def;
}

function _num_(v) {
  if (v === null || v === undefined || v === '') return NaN;
  const n = Number(v);
  return isFinite(n) ? n : NaN;
}

function _num_safe_(v) {
  const n = _num_(v);
  return isFinite(n) ? n : '';
}

function _setIfFinite_(sh, a1, value) {
  if (value instanceof Date) { sh.getRange(a1).setValue(value); return; }
  if (typeof value === 'number' && !isFinite(value)) { sh.getRange(a1).setValue(''); return; }
  sh.getRange(a1).setValue(value);
}

function _findLastBetsRow_(bets) {
  const lastSheetRow = bets.getLastRow();
  if (lastSheetRow < BETS_FIRST_DATA_ROW) return BETS_FIRST_DATA_ROW - 1;
  const vals = bets.getRange(BETS_FIRST_DATA_ROW, BETS_COL.matchNo, lastSheetRow - BETS_FIRST_DATA_ROW + 1, 1).getValues();
  for (let i = vals.length - 1; i >= 0; i--) {
    if (vals[i][0] !== '' && vals[i][0] !== null) return BETS_FIRST_DATA_ROW + i;
  }
  return BETS_FIRST_DATA_ROW - 1;
}

function _writeStatus_(msg) {
  Logger.log(msg);
  try {
    SpreadsheetApp.getActive().toast(msg, 'WC26 Engine v2.3.13', 4);
  } catch (_) {}
}
