/**
 * WC26 Value Betting Engine — Apps Script (v2.3.2)
 *
 * v2.3.2 (this file — single-CRIT bug-fix release on top of v2.3.1):
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
const STALE_MINUTES = 15;

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
};

// Bets column anchors — keep in sync with the v2.3.1 XLSX layout.
const BETS_COL = {
  matchNo: 1,         // A
  modelPHome: 9,      // I
  modelPDraw: 10,     // J
  modelPAway: 11,     // K
  pick: 29,           // AC
  decision: 34,       // AH
  engineStake: 36,    // AJ
  placed: 40,         // AN
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
    .addItem('Refresh everything', 'refreshAll')
    .addItem('Snapshot existing placed bets', 'snapshotExisting')
    .addSeparator()
    .addItem('Extend to knockout stage (run once before R32)', 'extendToKnockouts')
    .addSeparator()
    .addItem('Install auto-refresh (every ' + POLL_MINUTES + ' min)', 'installAutoRefresh')
    .addItem('Remove auto-refresh', 'removeAutoRefresh')
    .addSeparator()
    .addItem('Lock formulas (apply protections)', 'applyProtections')
    .addItem('Unlock all (remove protections)', 'removeProtections')
    .addSeparator()
    .addItem('First-time setup (run once)', 'installEngine')
    .addItem('Engine self-test', 'selfTest')
    .addToUi();
}

function installEngine() {
  _seedGoalMarketsMinEdge_();     // one-time: Method!B8 ← Method!B7 if blank
  refreshAll();
  refreshMatchContext();          // one-time write of altitude/climate/travel
  snapshotExisting();
  installAutoRefresh();
  applyProtections();
  SpreadsheetApp.getActive().toast(
    'WC26 Engine v2.3.2 installed. Auto-refresh every ' + POLL_MINUTES + ' min.',
    'Setup complete',
    8
  );
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
 * Master refresher. Order matters:
 *   1. Diagnostics — caches calibration bins so refreshModel can write BC:BE
 *      with corrected probs on this same cycle (fix MED #9).
 *   2. One predictions fetch — reused by refreshModel + refreshOutrights (fix MED #11).
 *   3. One live fetch — reused by refreshLive + refreshInPlay (fix MED #11).
 *   4. Each step in its own try/catch (fix MED #10).
 */
function refreshAll() {
  const ss = SpreadsheetApp.getActive();
  const errors = [];

  _tryStep_(errors, 'diagnostics', function() { refreshDiagnostics(); });

  // Pre-fetch shared payloads once.
  let predictions = null;
  try { predictions = _fetchJson_(_predictionsUrl_(ss)); }
  catch (e) { errors.push('predictions fetch: ' + e.message); }

  let liveState = null;
  try { liveState = _fetchJson_(_liveUrl_(ss)); }
  catch (e) { errors.push('live fetch: ' + e.message); }

  _tryStep_(errors, 'model',     function() { refreshModel(predictions); });
  _tryStep_(errors, 'intel',     function() { refreshIntel(); });
  _tryStep_(errors, 'live',      function() { refreshLive(liveState); });
  _tryStep_(errors, 'outrights', function() { refreshOutrights(predictions); });
  _tryStep_(errors, 'in-play',   function() { refreshInPlay(liveState); });
  _tryStep_(errors, 'goalGrid',  function() { refreshGoalGrid(predictions); });
  _tryStep_(errors, 'clv',       function() { refreshCLV(); });

  if (errors.length) {
    _writeStatus_('refreshAll: ' + errors.length + ' step(s) failed: ' + errors.join(' | '));
  }
}

function _tryStep_(errors, name, fn) {
  try { fn(); }
  catch (e) {
    errors.push(name + ': ' + (e.message || e));
    Logger.log('Step ' + name + ' failed: ' + e);
  }
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
    if (!fresh || fresh.some(function(v) { return !isFinite(v); })) {
      out.push(currentIJK[i]);
      missing++;
      continue;
    }
    out.push(fresh);
    updated++;
  }
  bets.getRange(BETS_FIRST_DATA_ROW, BETS_COL.modelPHome, out.length, 3).setValues(out);

  _writeCalibratedProbs_(bets, BETS_FIRST_DATA_ROW, out);

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
  const warnings = Array.isArray(state.warnings) ? state.warnings.join(' · ') : '';
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
  if (state.last_updated_utc) {
    const ageMin = (now.getTime() - new Date(state.last_updated_utc).getTime()) / 60000;
    if (isFinite(ageMin) && ageMin > STALE_MINUTES) {
      staleMsg = '⚠ STALE feed (' + ageMin.toFixed(0) + ' min old) — model paused';
      if (method) {
        method.getRange(M_CELL.staleKill).setValue(true);
        method.getRange(M_CELL.autoStatus).setValue('PAUSED · stale feed (' + ageMin.toFixed(0) + ' min)');
      }
    } else {
      // Feed is fresh — clear the kill flag if it was previously set.
      if (method) {
        const cur = method.getRange(M_CELL.staleKill).getValue();
        if (cur === true || String(cur).toUpperCase() === 'TRUE') {
          method.getRange(M_CELL.staleKill).setValue(false);
        }
      }
    }
  }
  live.getRange(LIVE_CELL.staleWarn).setValue(staleMsg);

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

function refreshIntel() {
  const ss = SpreadsheetApp.getActive();
  const live = ss.getSheetByName(SHEET.live);
  const method = ss.getSheetByName(SHEET.method);
  const bets = ss.getSheetByName(SHEET.bets);
  if (!live || !bets) return;

  let intel;
  try { intel = _fetchJson_(_intelUrl_(ss)); }
  catch (e) {
    live.getRange(LIVE_CELL.intelWarn).setValue('intel fetch failed: ' + e.message);
    return;
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
  try {
    const predPayload = _fetchJson_(_predictionsUrl_(ss));
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
    const rawMid = Number(a.match_id);
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

function refreshMatchContext() {
  const ss = SpreadsheetApp.getActive();
  const bets = ss.getSheetByName(SHEET.bets);
  if (!bets) return;
  const payload = _fetchJson_(_predictionsUrl_(ss));
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

  // Walk-forward
  try {
    const wf = _fetchJson_(_walkForwardUrl_(ss));
    const years = Object.keys(wf).filter(function(k) { return /^[0-9]{4}$/.test(k); });
    const losses = years.map(function(y) { return _num_(wf[y].log_loss); }).filter(isFinite);
    const meanLoss = losses.length ? losses.reduce(function(a,b){return a+b;}, 0) / losses.length : NaN;
    _setIfFinite_(method, M_CELL.walkForwardMeanLogLoss, meanLoss);
    const lift2022 = wf['2022'] && _num_(wf['2022'].lift_vs_baseline);
    _setIfFinite_(method, M_CELL.walkForward2022Lift, lift2022);
  } catch (e) {
    Logger.log('walk_forward fetch failed: ' + e.message);
  }

  // Calibration — cache via PropertiesService (works in custom function context).
  try {
    const cal = _fetchJson_(_calibrationUrl_(ss));
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
  }

  method.getRange(M_CELL.lastDiagnosticsRefresh).setValue(new Date());
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

  // Sort by p_champion desc.
  teams.sort(function(a, b) { return _num_(b.p_champion) - _num_(a.p_champion); });

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
  if (prevLast >= dataStart) {
    out.getRange(dataStart, 1, prevLast - dataStart + 1, 18).clearContent();
  }
  out.getRange(dataStart, 1, rows.length, 11).setValues(rows);

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
    if (!sh.getRange(r, 5).getValue() || !sh.getRange(r, 6).getValue()) continue;
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
    ' · MAX_GOALS=' + GOAL_GRID_MAX_GOALS
  );
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
// CLV REFRESH (Phase 5C) — Bets!AW:AY snapshot block → CLV sheet
//   Per-bet closing-line-value tracker. Operator types the closing odds in
//   col F after the market closes; the rolling 20-bet average (col I) is
//   the edge signal — positive CLV means you're beating the close.
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
  // Snapshot block is AW:AY = snapDecision, snapStake, snapPick (frozen at
  // bet-placement time so closing-line comparison is honest).
  const snaps = bets.getRange(BETS_FIRST_DATA_ROW, BETS_COL.snapDecision, nRows, 3).getValues();
  const ijk = bets.getRange(BETS_FIRST_DATA_ROW, BETS_COL.modelPHome, nRows, 3).getValues();

  // Collect placed bets (snapDecision = "BET" or non-empty + snapPick set).
  // Dedup on (#m, pick) so duplicate Bets rows don't double-count in the
  // rolling CLV window. Pre-filter out-of-scope picks (O/U, BTTS, CS) so
  // they don't inflate the placed-bet count with blank-modelOdds rows.
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
    // Model fair odds = 1 / corrected pick prob. Use I:K (model) since BC:BE
    // may not be populated for very fresh rows.
    const pHome = _num_(ijk[i][0]);
    const pDraw = _num_(ijk[i][1]);
    const pAway = _num_(ijk[i][2]);
    const pickU = pick.toUpperCase();
    let p = NaN;
    if (pickU === 'H' || pickU === 'HOME' || pickU === '1') p = pHome;
    else if (pickU === 'D' || pickU === 'DRAW' || pickU === 'X') p = pDraw;
    else if (pickU === 'A' || pickU === 'AWAY' || pickU === '2') p = pAway;
    const modelOdds = (isFinite(p) && p > 0) ? (1 / p) : '';
    placedRows.push({
      m: m,
      pick: pick,
      stake: isFinite(stake) ? stake : '',
      modelOdds: modelOdds,
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

  // Write columns A:F (bet_no, #m, pick, stake, model_odds, closing_odds).
  const rows = placedRows.map(function(p, idx) {
    const key = String(p.m) + '|' + String(p.pick).toUpperCase();
    return [
      idx + 1,
      p.m,
      p.pick,
      p.stake,
      p.modelOdds,
      (key in closingByKey) ? closingByKey[key] : '',
    ];
  });
  sh.getRange(dataStart, 1, rows.length, 6).setValues(rows);

  // Formulas in G:I — CLV%, rolling 20-bet CLV%, status pill.
  // CLV% = (model_odds - closing_odds) / closing_odds. Positive = you got
  // a better price than the market settled at. Pre-validate F>0 so a 0
  // doesn't emit #DIV/0! and collapse the whole rolling-window IFERROR.
  for (let i = 0; i < rows.length; i++) {
    const r = dataStart + i;
    sh.getRange(r, 7).setFormula(
      '=IF(OR(E' + r + '="",F' + r + '="",NOT(ISNUMBER(F' + r + ')),F' + r + '<=0),"",' +
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
  const p = String(pick || '').trim().toUpperCase();
  return p === 'H' || p === 'HOME' || p === '1'
      || p === 'D' || p === 'DRAW' || p === 'X'
      || p === 'A' || p === 'AWAY' || p === '2';
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
    'Source: Bets!AW:AY snapshot block · ' +
    'type closing odds in col F after market closes'
  );
  const hdr = [
    'Bet #', '#m', 'Pick', 'Stake',
    'Model odds', 'Closing odds',
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
      bcOut.push(['=I' + r, '=J' + r, '=K' + r]);
      seeded++;
    }
  }
  bcRange.setFormulas(bcOut);

  let msg = 'Extend to knockouts: added ' + added + ' new row(s), ' +
            skipped + ' already-correct, ' + seeded + ' BC:BE formula(s) seeded';
  if (conflicts) {
    msg += '. CONFLICT: ' + conflicts + ' row(s) hold a non-knockout match_no — ' +
           'inspect Bets rows ' + KNOCKOUT_FIRST_ROW + '..' + KNOCKOUT_LAST_ROW;
  }
  _writeStatus_(msg);
  return { added: added, skipped: skipped, seeded: seeded, conflicts: conflicts };
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

  const editable = {
    Matchday: ['E4:G75', 'L4:L75', 'N4:R75', 'T4:T75'],
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
    const p = sh.protect().setDescription('WC26 v2.3.2 — formulas locked');
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
 * Isotonic correction from /calibration.json reliability bins.
 *
 * v2.3.1: reads from PropertiesService (works in custom-function context).
 * Custom function does NOT call UrlFetchApp — that would blow the daily
 * 20,000-fetch quota if the formula is dragged across many cells. Bins are
 * cached by refreshDiagnostics() (runs every POLL_MINUTES via the time
 * trigger). If no bins are cached yet, returns the raw probability.
 *
 * @param {number} p Model probability in [0, 1].
 * @param {string} market "home" | "draw" | "away".
 * @return Corrected probability (linear interp between bin actual_freqs).
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

function _writeCalibratedProbs_(bets, firstRow, rawProbs) {
  const home = _calibrationBinsCached_('home');
  const draw = _calibrationBinsCached_('draw');
  const away = _calibrationBinsCached_('away');
  if (!home.length || !draw.length || !away.length) {
    // Mirror raw probs so BC:BE is never blank.
    bets.getRange(firstRow, BETS_COL.pHomeCorr, rawProbs.length, 3).setValues(rawProbs);
    return;
  }
  const out = rawProbs.map(function(row) {
    return [
      _interp_(row[0], home),
      _interp_(row[1], draw),
      _interp_(row[2], away),
    ];
  });
  bets.getRange(firstRow, BETS_COL.pHomeCorr, out.length, 3).setValues(out);
}

function _interp_(p, bins) {
  if (!isFinite(p)) return '';
  if (p <= bins[0].mean_pred) return bins[0].actual_freq;
  if (p >= bins[bins.length - 1].mean_pred) return bins[bins.length - 1].actual_freq;
  for (let i = 0; i < bins.length - 1; i++) {
    const a = bins[i], b = bins[i + 1];
    if (p >= a.mean_pred && p <= b.mean_pred) {
      const t = (p - a.mean_pred) / (b.mean_pred - a.mean_pred);
      return a.actual_freq + t * (b.actual_freq - a.actual_freq);
    }
  }
  return p;
}

function _col_(arr) { return arr.map(function(v) { return [v]; }); }

function _validateOdds_(o1, o2, o3) {
  const a = Number(o1), b = Number(o2), c = Number(o3);
  if (!isFinite(a) || !isFinite(b) || !isFinite(c)) throw new Error('odds must be numeric');
  if (a <= 1 || b <= 1 || c <= 1) throw new Error('decimal odds must be > 1');
  return [1 / a, 1 / b, 1 / c];
}

// =============================================================================
// GOAL_GRID — Dixon-Coles bivariate Poisson goal-market projector
// =============================================================================
// Mirrors scripts/03_simulate.py:dc_tau with ρ=GOAL_GRID_TAU (default −0.13).
// Returns the fair probability of the requested market from an MAX_GOALS+1
// square Poisson matrix with the DC low-score correction applied at the
// (0,0), (0,1), (1,0), (1,1) cells.
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
    // Ensure properties are warm so CALIBRATE works.
    if (cal.calibration) {
      const props = PropertiesService.getScriptProperties();
      props.setProperty(CALIBRATION_PROP_KEY, JSON.stringify(cal.calibration));
      props.setProperty(CALIBRATION_PROP_TS, String(Date.now()));
    }
    const c = CALIBRATE(0.65, 'home');
    checks.push('CALIBRATE(0.65, "home") = ' + (typeof c === 'number' ? (c*100).toFixed(1) + '%' : c) +
      ' (raw 65%, reads from ScriptProperties)');
  } catch (e) { checks.push('Calibration FAIL: ' + e.message); }

  try {
    const wf = _fetchJson_(DEFAULT_WALK_FORWARD_URL);
    const years = Object.keys(wf).filter(function(k) { return /^[0-9]{4}$/.test(k); });
    checks.push('Walk-forward feed OK — ' + years.length + ' WCs · 2022 lift=' +
      (wf['2022'] && wf['2022'].lift_vs_baseline !== undefined ?
        wf['2022'].lift_vs_baseline.toFixed(4) : 'n/a'));
  } catch (e) { checks.push('Walk-forward FAIL: ' + e.message); }

  SpreadsheetApp.getActive().toast(checks.join('\n'), 'Engine self-test (v2.3.2)', 30);
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
      if (mdRow < 4 || mdRow > 75) continue;
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
    SpreadsheetApp.getActive().toast(msg, 'WC26 Engine v2.3.2', 4);
  } catch (_) {}
}
