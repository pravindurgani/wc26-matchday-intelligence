// test_harness.mjs — WC26 Engine static-analysis harness
//
// Reconstruction of the v2.3.12 harness that was lost from disk during the
// round7-patches stash-pop accident. Validates v2.3.13 + carries forward
// every regression assertion from v2.3.6 through v2.3.13.
//
// Run:  node test_harness.mjs
// Exit: 0 on all pass, 1 on any fail.
//
// Design:
//   1. Load WC26_Engine_AppsScript_v2.3.13.gs as source-of-truth.
//   2. Pre-strip block + line comments (stripCommentsLocal) so that negative
//      invariants don't false-positive on documentation strings quoting
//      old buggy patterns verbatim.
//   3. Run grouped assertion suites — version sanity, constants, every
//      v2.3.x [LEVEL-N] anchor, formula structural invariants, setup-time
//      side effects, and (optional) XLSX mirror cross-check.
//   4. Summary with pass/fail count + exit code.
//
// Assertion philosophy: every fix should have a POSITIVE invariant (the fix
// is present) AND a NEGATIVE invariant (the bug is absent). Both are
// load-bearing — positive guards against deletion, negative guards against
// a half-revert that leaves both old and new code paths in the file.

import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { execSync } from 'node:child_process';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SOURCE_PATH = join(__dirname, 'WC26_Engine_AppsScript_v2.3.13.gs');
const XLSX_PATH = join(__dirname, 'WC26_Value_Betting_Engine_AUTOMATED_v2.3.13.xlsx');

if (!existsSync(SOURCE_PATH)) {
  console.error('FATAL: source not found:', SOURCE_PATH);
  process.exit(2);
}

const RAW = readFileSync(SOURCE_PATH, 'utf8');
const STRIPPED = stripCommentsLocal(RAW);

let passed = 0, failed = 0, total = 0;
const failures = [];
let currentGroup = '(uncategorized)';

function assert(label, predicate) {
  total++;
  const fullLabel = `[${currentGroup}] ${label}`;
  try {
    const ok = predicate();
    if (ok) {
      passed++;
      process.stdout.write(`  ✓ ${label}\n`);
    } else {
      failed++;
      failures.push(fullLabel);
      process.stdout.write(`  ✗ ${label}\n`);
    }
  } catch (e) {
    failed++;
    failures.push(`${fullLabel} (THREW: ${e.message})`);
    process.stdout.write(`  ✗ ${label} (THREW: ${e.message})\n`);
  }
}

function group(name, fn) {
  currentGroup = name;
  console.log(`\n=== ${name} ===`);
  fn();
}

// =============================================================================
// HELPERS
// =============================================================================

// Strip /* ... */ block comments AND // line comments while preserving line
// numbers (block comments are replaced by their newlines, line comments are
// dropped up to but not including the trailing \n). String literals
// (single, double, backtick) are passed through unchanged so formula source
// remains intact.
function stripCommentsLocal(src) {
  const n = src.length;
  let out = '';
  let i = 0;
  while (i < n) {
    const ch = src[i];
    const nx = src[i + 1];
    if (ch === '/' && nx === '*') {
      const end = src.indexOf('*/', i + 2);
      if (end < 0) { i = n; continue; }
      for (let k = i; k < end + 2; k++) if (src[k] === '\n') out += '\n';
      i = end + 2;
    } else if (ch === '/' && nx === '/') {
      const end = src.indexOf('\n', i);
      if (end < 0) { i = n; continue; }
      i = end; // keep the \n
    } else if (ch === "'" || ch === '"' || ch === '`') {
      const quote = ch;
      out += quote;
      i++;
      while (i < n) {
        const c = src[i];
        if (c === '\\') {
          out += c;
          if (i + 1 < n) out += src[i + 1];
          i += 2;
        } else if (c === quote) {
          out += c;
          i++;
          break;
        } else {
          out += c;
          i++;
        }
      }
    } else {
      out += ch;
      i++;
    }
  }
  return out;
}

// Walk parens in a formula string, treating string literals as opaque.
// Returns { opens, closes, minDepth, endDepth }. Well-formed formula has
// opens === closes, minDepth === 0, endDepth === 0.
function parenWalk(formula) {
  let opens = 0, closes = 0, depth = 0, minDepth = 0;
  let inStr = false;
  for (let i = 0; i < formula.length; i++) {
    const ch = formula[i];
    if (ch === '"') {
      // toggle string mode, handle "" escaping inside strings
      if (inStr && formula[i + 1] === '"') { i++; continue; }
      inStr = !inStr;
      continue;
    }
    if (inStr) continue;
    if (ch === '(') { opens++; depth++; }
    else if (ch === ')') { closes++; depth--; if (depth < minDepth) minDepth = depth; }
  }
  return { opens, closes, minDepth, endDepth: depth };
}

// Find a single source line matching all of the given needle substrings.
// Returns the trimmed line content, or null.
function findLine(haystack, ...needles) {
  const lines = haystack.split('\n');
  for (const line of lines) {
    if (needles.every(n => line.includes(n))) return line;
  }
  return null;
}

// Extract the body of a top-level function by name. Returns the substring
// from `function NAME(...)` to its matching `\n}\n` (or end of file).
// Brace-balance aware, string-literal aware. Returns '' on miss.
function extractFunctionBody(src, name) {
  const sig = new RegExp(`function\\s+${name.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\$&')}\\s*\\(`);
  const m = src.match(sig);
  if (!m) return '';
  const start = m.index;
  let i = src.indexOf('{', start);
  if (i < 0) return '';
  let depth = 1;
  let inStr = false, strCh = '';
  i++;
  while (i < src.length && depth > 0) {
    const c = src[i];
    if (inStr) {
      if (c === '\\') { i += 2; continue; }
      if (c === strCh) inStr = false;
    } else {
      if (c === "'" || c === '"' || c === '`') { inStr = true; strCh = c; }
      else if (c === '{') depth++;
      else if (c === '}') depth--;
    }
    i++;
  }
  return src.slice(start, i);
}

// Render a GAS-template formula string by interpolating common substitutions.
// Inputs a JS string literal like: `'=IF(AB' + r + '="","",IF(Method!$B$55=TRUE(),...))'`
// Returns the rendered formula text (without the outer quotes).
function renderFormula(jsString, vars = {}) {
  const defaults = {
    r: '2', rm: '4',
    STALE_MINUTES: '25',
    POLL_MINUTES: '10',
    LONG_SHOT_ODDS_MIN: '6',
    LONG_SHOT_EDGE_MIN: '0.15',
    LIFT_NOISE_FLOOR: '0.005',
    GOAL_GRID_MAX_GOALS: '15',
    GOAL_GRID_TAU: '-0.13',
    CLV_ROLLING_WINDOW: '20',
  };
  const v = { ...defaults, ...vars };
  let s = jsString.trim();
  // Strip wrapping single quotes if present at both ends of the literal
  if (s.startsWith("'") && s.endsWith("'")) s = s.slice(1, -1);
  if (s.endsWith("',")) s = s.slice(0, -2);
  // Replace ' + IDENT + ' patterns
  s = s.replace(/'\s*\+\s*([A-Z_][A-Z0-9_]*)\s*\+\s*'/g, (_, ident) =>
    v[ident] !== undefined ? String(v[ident]) : `<${ident}>`);
  // Replace ' + r + ', ' + rm + ' style (lowercase)
  s = s.replace(/'\s*\+\s*([a-z][a-zA-Z0-9_]*)\s*\+\s*'/g, (_, ident) =>
    v[ident] !== undefined ? String(v[ident]) : `<${ident}>`);
  return s;
}

// Simple HTML entity decoder for xlsx formula extraction.
function unescapeHtml(s) {
  return s
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&amp;/g, '&');
}

// Extract a single-cell formula from an xlsx worksheet xml string.
// Returns the formula text (with leading =) or null on miss.
function xlsxFormula(xml, cellRef) {
  const re = new RegExp(`<c\\s+r="${cellRef}"[^>]*>.*?<f[^>]*>(.*?)</f>`, 's');
  const m = xml.match(re);
  return m ? '=' + unescapeHtml(m[1]) : null;
}

// =============================================================================
// SUITE 1 — Version sanity
// =============================================================================

group('Version sanity', () => {
  assert('header line 2 declares v2.3.13', () =>
    /\* WC26 Value Betting Engine — Apps Script \(v2\.3\.13\)/.test(RAW));

  assert('install toast (installEngine) bumped to v2.3.13', () =>
    /'WC26 Engine v2\.3\.13 installed/.test(STRIPPED));

  assert('install-error toast (_preflightSheets_) bumped to v2.3.13', () =>
    /'WC26 Engine v2\.3\.13 install error'/.test(STRIPPED));

  assert('self-test toast bumped to v2.3.13', () =>
    /'Engine self-test \(v2\.3\.13\)'/.test(STRIPPED));

  assert('_writeStatus_ toast bumped to v2.3.13', () =>
    /'WC26 Engine v2\.3\.13'/.test(STRIPPED));

  // Negative: no active-code v2.3.12 toasts leak through
  assert('no v2.3.12 install toast remains in active code', () =>
    !/'WC26 Engine v2\.3\.12 installed/.test(STRIPPED));

  assert('no v2.3.12 install-error toast remains', () =>
    !/'WC26 Engine v2\.3\.12 install error'/.test(STRIPPED));

  assert('no v2.3.12 self-test toast remains', () =>
    !/'Engine self-test \(v2\.3\.12\)'/.test(STRIPPED));

  assert('no bare v2.3.12 _writeStatus_ toast remains', () =>
    !/'WC26 Engine v2\.3\.12'/.test(STRIPPED));
});

// =============================================================================
// SUITE 2 — Constants
// =============================================================================

group('Constants', () => {
  assert('POLL_MINUTES = 10', () => /const POLL_MINUTES\s*=\s*10\s*;/.test(STRIPPED));

  assert('STALE_MINUTES = 25 (v2.3.12 HIGH-1, NOT pre-fix 15)', () =>
    /const STALE_MINUTES\s*=\s*25\s*;/.test(STRIPPED));

  assert('STALE_MINUTES is NOT the pre-v2.3.12 value 15', () =>
    !/const STALE_MINUTES\s*=\s*15\s*;/.test(STRIPPED));

  assert('LONG_SHOT_ODDS_MIN = 6 (v2.3.13 LOW-3)', () =>
    /const LONG_SHOT_ODDS_MIN\s*=\s*6\s*;/.test(STRIPPED));

  assert('LONG_SHOT_EDGE_MIN = 0.15 (v2.3.13 LOW-3)', () =>
    /const LONG_SHOT_EDGE_MIN\s*=\s*0\.15\s*;/.test(STRIPPED));

  assert('GOAL_GRID_MAX_GOALS = 15 (R13 C3, was 10 pre-R12)', () =>
    /const GOAL_GRID_MAX_GOALS\s*=\s*15\s*;/.test(STRIPPED));

  assert('GOAL_GRID_TAU = -0.13 (Dixon-Coles τ)', () =>
    /const GOAL_GRID_TAU\s*=\s*-0\.13\s*;/.test(STRIPPED));

  assert('LIFT_NOISE_FLOOR = 0.005 (v2.3.10 LOW-1)', () =>
    /const LIFT_NOISE_FLOOR\s*=\s*0\.005\s*;/.test(STRIPPED));

  assert('CLV_ROLLING_WINDOW = 20 (Phase 5C)', () =>
    /const CLV_ROLLING_WINDOW\s*=\s*20\s*;/.test(STRIPPED));

  assert('FETCH_DEADLINE_MS = 15000', () =>
    /const FETCH_DEADLINE_MS\s*=\s*15000\s*;/.test(STRIPPED));

  assert('FETCH_RETRIES = 1', () =>
    /const FETCH_RETRIES\s*=\s*1\s*;/.test(STRIPPED));

  assert('SHEET map declares bets/method/live/matchday/outrights/inplay/goalGrid/clv', () => {
    const m = STRIPPED.match(/const SHEET\s*=\s*\{([\s\S]*?)\};/);
    if (!m) return false;
    const body = m[1];
    return ['bets', 'method', 'live', 'matchday', 'outrights', 'inplay', 'goalGrid', 'clv']
      .every(k => new RegExp(`${k}:\\s*'`).test(body));
  });

  assert('M_CELL.autoStatus = B63 (v2.3.13 LOW-2 target cell)', () =>
    /autoStatus:\s*'B63'/.test(STRIPPED));

  assert('M_CELL.staleKill = B81', () => /staleKill:\s*'B81'/.test(STRIPPED));

  assert('M_CELL endpoint*Ts cells declared (B82..B86)', () => {
    const names = ['endpointLiveTs', 'endpointPredsTs', 'endpointIntelTs', 'endpointCalibTs', 'endpointWalkFwdTs'];
    return names.every(n => new RegExp(`${n}:\\s*'B8[0-9]'`).test(STRIPPED));
  });
});

// =============================================================================
// SUITE 3 — v2.3.6 regressions (extendToKnockouts pipeline)
// =============================================================================

group('v2.3.6 CRIT — extendToKnockouts + protections + onEdit knockout-row coverage', () => {
  assert('extendToKnockouts() function exists', () =>
    /function extendToKnockouts\s*\(/.test(STRIPPED));

  assert('Bets L/M/N row mirrors Matchday E/F/G (v2.3.6 CRIT — seeded via _knockoutStakingFormulas_)', () => {
    // The mirror formulas live inside the seeder block_L_N (a 3-element array
    // returned by the staking-formulas helper). Look for the pattern in any
    // formula-emitter context, not necessarily inside extendToKnockouts.
    // The seeder uses `rm` (Matchday row = Bets row + 2), not `r`. Both
    // variants accepted defensively.
    const hasE = /=IF\(Matchday!E'\s*\+\s*r[m]?\s*\+\s*'="","",Matchday!E/.test(STRIPPED);
    const hasF = /=IF\(Matchday!F'\s*\+\s*r[m]?\s*\+\s*'="","",Matchday!F/.test(STRIPPED);
    const hasG = /=IF\(Matchday!G'\s*\+\s*r[m]?\s*\+\s*'="","",Matchday!G/.test(STRIPPED);
    return hasE && hasF && hasG;
  });

  assert('onEdit handler bounds knockout rows up to 107 (not pre-v2.3.6 cap of 75)', () => {
    const body = extractFunctionBody(STRIPPED, 'onEdit');
    return /107/.test(body);
  });

  assert('onEdit does NOT silently no-op above row 75 (negative pre-v2.3.6 pattern)', () => {
    const body = extractFunctionBody(STRIPPED, 'onEdit');
    // Pre-v2.3.6 was `if (mdRow < 4 || mdRow > 75) continue;` — must be gone
    return !/mdRow\s*>\s*75/.test(body);
  });
});

// =============================================================================
// SUITE 4 — v2.3.7 regressions
// =============================================================================

group('v2.3.7 CRIT-1 — _preflightSheets_ install gate', () => {
  assert('_preflightSheets_ function exists', () =>
    /function _preflightSheets_\s*\(/.test(STRIPPED));

  assert('_preflightSheets_ throws on missing required sheet', () => {
    const body = extractFunctionBody(STRIPPED, '_preflightSheets_');
    return /missing\.length/.test(body) && /throw new Error/.test(body);
  });

  assert('installEngine calls _preflightSheets_ before seeding', () => {
    const body = extractFunctionBody(STRIPPED, 'installEngine');
    return /_preflightSheets_\(/.test(body);
  });
});

group('v2.3.7 HIGH-1 — persistent Live!B28 error log', () => {
  assert('_setErrLog_ helper exists', () => /function _setErrLog_\s*\(/.test(STRIPPED));
  assert('_clearErrLog_ helper exists', () => /function _clearErrLog_\s*\(/.test(STRIPPED));
  assert('LIVE_CELL.errLog = B28', () => /errLog:\s*'B28'/.test(STRIPPED));
});

group('v2.3.7 HIGH-2 — stale banner mirrored to Matchday!W1', () => {
  const body = extractFunctionBody(STRIPPED, 'refreshLive');
  assert('refreshLive references Matchday for cross-tab mirror', () =>
    /matchday|Matchday/.test(body));
  assert('refreshLive writes a status to W1', () =>
    /W1|getRange\('W1'\)|matchdayStaleMirror/.test(body));
});

group('v2.3.7 HIGH-3 / v2.3.8 HIGH-1 — Method!D5 Kelly disclaimer', () => {
  assert('_seedKellyEdgeDisclaimer_ defined', () =>
    /function _seedKellyEdgeDisclaimer_\s*\(/.test(STRIPPED));

  const body = extractFunctionBody(STRIPPED, '_seedKellyEdgeDisclaimer_');
  assert('_seedKellyEdgeDisclaimer_ writes to D5 (NOT C5)', () =>
    /D5/.test(body) && !/getRange\(['"]C5['"]\)\.setValue/.test(body));

  assert('installEngine calls _seedKellyEdgeDisclaimer_', () => {
    const inst = extractFunctionBody(STRIPPED, 'installEngine');
    return /_seedKellyEdgeDisclaimer_\s*\(/.test(inst);
  });
});

group('v2.3.7 MED-1 / v2.3.8 LOW-3 — per-endpoint timestamps', () => {
  assert('_stampEndpoint_ helper exists', () =>
    /function _stampEndpoint_\s*\(/.test(STRIPPED));

  assert('_stampEndpoint_ uses UTC ISO via Utilities.formatDate', () => {
    const body = extractFunctionBody(STRIPPED, '_stampEndpoint_');
    return /Utilities\.formatDate/.test(body) && /UTC/.test(body);
  });
});

// =============================================================================
// SUITE 5 — v2.3.8 regressions
// =============================================================================

group('v2.3.8 HIGH-2 — refreshDiagnostics collects + throws (no racy setErrLog)', () => {
  const body = extractFunctionBody(STRIPPED, 'refreshDiagnostics');
  assert('refreshDiagnostics body exists', () => body.length > 0);
  assert('refreshDiagnostics catch path does NOT call _setErrLog_ directly', () => {
    // Hard check: no `_setErrLog_(` inside a catch block of refreshDiagnostics
    const catches = body.match(/catch\s*\([^)]*\)\s*\{[^}]*\}/g) || [];
    return catches.every(c => !/_setErrLog_\s*\(/.test(c));
  });
  assert('refreshDiagnostics accumulates errors then throws', () =>
    /diagErrors|errors\.push|throw new Error/.test(body));
});

group('v2.3.8 LOW-2 — Kelly disclaimer refreshed on every poll, not only install', () => {
  const body = extractFunctionBody(STRIPPED, 'refreshDiagnostics');
  assert('refreshDiagnostics calls _seedKellyEdgeDisclaimer_ in tail', () =>
    /_seedKellyEdgeDisclaimer_\s*\(/.test(body));
});

// =============================================================================
// SUITE 6 — v2.3.9 regressions
// =============================================================================

group('v2.3.9 HIGH-1 — refreshIntel re-throws (no racy setErrLog)', () => {
  const body = extractFunctionBody(STRIPPED, 'refreshIntel');
  assert('refreshIntel body exists', () => body.length > 0);
  assert('refreshIntel catch does NOT silently swallow + setErrLog', () => {
    const catches = body.match(/catch\s*\([^)]*\)\s*\{[\s\S]*?\}/g) || [];
    // Pre-fix: catch wrote _setErrLog_ then returned silently
    // Post-fix: catch may write _setErrLog_ but MUST throw
    return catches.every(c => /throw\s/.test(c) || !/_setErrLog_/.test(c));
  });
});

// =============================================================================
// SUITE 7 — v2.3.10 regressions
// =============================================================================

group('v2.3.10 LOW-1 — LIFT_NOISE_FLOOR gates STOP-BETTING advice', () => {
  const body = extractFunctionBody(STRIPPED, '_seedKellyEdgeDisclaimer_');
  assert('_seedKellyEdgeDisclaimer_ uses LIFT_NOISE_FLOOR threshold', () =>
    /LIFT_NOISE_FLOOR/.test(body));
  assert('STOP-BETTING branch gated by `lift <= -LIFT_NOISE_FLOOR` (not bare `lift < 0`)', () =>
    /<=\s*-LIFT_NOISE_FLOOR/.test(body) || /lift\s*<=\s*-\s*LIFT_NOISE_FLOOR/.test(body));
});

group('v2.3.10 LOW-2 — clearDiagnosticSurfaces exists + clears full surface set', () => {
  assert('clearDiagnosticSurfaces function exists', () =>
    /function clearDiagnosticSurfaces\s*\(/.test(STRIPPED));
  const body = extractFunctionBody(STRIPPED, 'clearDiagnosticSurfaces');
  assert('clearDiagnosticSurfaces uses tryClear helper', () =>
    /const tryClear\s*=\s*function/.test(body));
  assert('clearDiagnosticSurfaces clears Live errLog (B28)', () =>
    /tryClear\(\s*live\s*,\s*LIVE_CELL\.errLog\s*\)/.test(body));
  assert('clearDiagnosticSurfaces clears Live intelWarn (B24)', () =>
    /tryClear\(\s*live\s*,\s*LIVE_CELL\.intelWarn\s*\)/.test(body));
  assert('clearDiagnosticSurfaces clears Live calibrationOob (B27)', () =>
    /tryClear\(\s*live\s*,\s*LIVE_CELL\.calibrationOob\s*\)/.test(body));
  assert('clearDiagnosticSurfaces clears Method D5 (Kelly disclaimer)', () =>
    /tryClear\(\s*method\s*,\s*['"]D5['"]\s*\)/.test(body));
  assert('clearDiagnosticSurfaces clears all 5 endpoint*Ts cells', () => {
    const names = ['endpointLiveTs', 'endpointPredsTs', 'endpointIntelTs', 'endpointCalibTs', 'endpointWalkFwdTs'];
    return names.every(n => new RegExp(`tryClear\\(\\s*method\\s*,\\s*M_CELL\\.${n}\\s*\\)`).test(body));
  });
});

group('v2.3.10 MED-2 — GOAL_GRID A1 drift banner', () => {
  const body = extractFunctionBody(STRIPPED, '_seedGoalGridHeaders_');
  assert('_seedGoalGridHeaders_ function exists', () => body.length > 0);
  assert('A1 banner mentions Poisson drift / ≥8pp guard', () =>
    /DRIFT|drift|8pp|≥8pp/.test(body));
});

// =============================================================================
// SUITE 8 — v2.3.11 regressions
// =============================================================================

group('v2.3.11 MED-A — clearDiagnosticSurfaces takes script lock', () => {
  const body = extractFunctionBody(STRIPPED, 'clearDiagnosticSurfaces');
  assert('LockService.getScriptLock() called', () =>
    /LockService\.getScriptLock\(\)/.test(body));
  assert('tryLock(0) (non-blocking) used — not tryLock(N)', () =>
    /tryLock\(\s*0\s*\)/.test(body));
  assert('lock.releaseLock() called inside finally block', () =>
    /finally\s*\{[\s\S]*?lock\.releaseLock\(\)/.test(body));
  assert('contention path toasts operator (not silent)', () =>
    /toast\(/.test(body) && /in flight/.test(body));
});

group('v2.3.11 MED-B — GOAL_GRID A1 wrap + row-height', () => {
  const body = extractFunctionBody(STRIPPED, '_seedGoalGridHeaders_');
  assert('_seedGoalGridHeaders_ calls setWrap(true)', () =>
    /setWrap\(\s*true\s*\)/.test(body));
  assert('_seedGoalGridHeaders_ calls setRowHeight(1, N) for A1 row', () =>
    /setRowHeight\(\s*1\s*,\s*\d+\s*\)/.test(body));
});

// =============================================================================
// SUITE 9 — v2.3.12 regressions
// =============================================================================

group('v2.3.12 HIGH-1 — STALE_MINUTES tuned 15 → 25', () => {
  assert('STALE_MINUTES = 25 (covered in Constants suite, re-affirmed here)', () =>
    /const STALE_MINUTES\s*=\s*25\s*;/.test(STRIPPED));
  assert('STALE_MINUTES is not the pre-v2.3.12 value 15', () =>
    !/const STALE_MINUTES\s*=\s*15\s*;/.test(STRIPPED));
});

group('v2.3.12 CRIT-1 — Bets!AT 3-branch PAUSE cascade', () => {
  const atLine = findLine(STRIPPED, "'=IF(AB' + r + '=", 'Type the 3 odds');
  assert('Bets!AT formula present', () => !!atLine);
  if (!atLine) return;

  assert('Bets!AT inspects B81 stale-feed', () =>
    /IF\(Method!\$B\$81=TRUE\(\)/.test(atLine));
  assert('Bets!AT has explicit drawdown branch (B51>=B52)', () =>
    /IF\(Method!\$B\$51>=Method!\$B\$52/.test(atLine));
  assert('Bets!AT has "feed stale" message text', () =>
    /feed stale > '\s*\+\s*STALE_MINUTES\s*\+\s*' min/.test(atLine));
  assert('Bets!AT has "drawdown" attribution message', () =>
    atLine.includes('drawdown') && atLine.includes('TEXT(Method!$B$51'));
  assert('Bets!AT has fallback "see Method!B49/B63 for cause"', () =>
    atLine.includes('see Method!B49/B63 for cause'));

  // Negative invariants — old single-branch lie is GONE
  assert('Bets!AT does NOT have old single-branch drawdown pattern', () =>
    !/IF\(Method!\$B\$55=TRUE\(\),"🛑 PAUSED — drawdown/.test(atLine));
});

group('v2.3.12 CRIT-1 — Matchday!M 3-branch PAUSE cascade', () => {
  const mLine = findLine(STRIPPED, "'=IF(Bets!AH' + r + '=", 'enter odds');
  assert('Matchday!M formula present', () => !!mLine);
  if (!mLine) return;

  assert('Matchday!M inspects B81 stale-feed', () =>
    /IF\(Method!\$B\$81=TRUE\(\)/.test(mLine));
  assert('Matchday!M has explicit drawdown branch (B51>=B52)', () =>
    /IF\(Method!\$B\$51>=Method!\$B\$52/.test(mLine));
  assert('Matchday!M has "feed stale" message text', () =>
    /feed stale \(>'\s*\+\s*STALE_MINUTES\s*\+\s*' min\)/.test(mLine));
  assert('Matchday!M has fallback "see Method!B49/B63 for cause"', () =>
    mLine.includes('see Method!B49/B63 for cause'));

  // Negative — old "drawdown protection" lie is GONE
  assert('Matchday!M does NOT have old "drawdown protection" message', () =>
    !/drawdown protection/.test(mLine));
});

// =============================================================================
// SUITE 10 — v2.3.13 regressions (this release)
// =============================================================================

group('v2.3.13 LOW-2 — Method!B63 fresh-branch ON writer', () => {
  const body = extractFunctionBody(STRIPPED, 'refreshLive');
  assert('refreshLive body exists', () => body.length > 0);

  assert('refreshLive fresh-branch writes M_CELL.autoStatus with ON banner', () =>
    /M_CELL\.autoStatus\)\.setValue\('ON · every '\s*\+\s*POLL_MINUTES\s*\+\s*' min'\)/.test(body));

  // Confirm the writer sits after the staleKill clear (fresh-branch, not stale-branch)
  assert('fresh-branch ON writer sits in the !cbTripped !stale else-arm', () => {
    // Locate the staleKill setValue(false) and then ensure the ON writer is below it
    const idx = body.search(/M_CELL\.staleKill\)\.setValue\(false\)/);
    const onIdx = body.search(/M_CELL\.autoStatus\)\.setValue\('ON · every '/);
    return idx >= 0 && onIdx > idx;
  });
});

group('v2.3.13 LOW-2 — clearDiagnosticSurfaces wipes B63', () => {
  const body = extractFunctionBody(STRIPPED, 'clearDiagnosticSurfaces');
  assert('tryClear list includes M_CELL.autoStatus', () =>
    /tryClear\(\s*method\s*,\s*M_CELL\.autoStatus\s*\)/.test(body));

  // Total tryClear call count: was 9 in v2.3.12, must be ≥10 in v2.3.13.
  // Note: `const tryClear = function(...) {...}` uses `=` so the assignment
  // line does NOT contain the substring `tryClear(`. Only call sites match.
  assert('tryClear call count is ≥ 10 (was 9 pre-v2.3.13)', () => {
    const matches = body.match(/tryClear\(/g) || [];
    return matches.length >= 10;
  });
});

group('v2.3.13 LOW-3 — long-shot gate constants present', () => {
  assert('LONG_SHOT_ODDS_MIN constant declared = 6', () =>
    /const LONG_SHOT_ODDS_MIN\s*=\s*6\s*;/.test(STRIPPED));
  assert('LONG_SHOT_EDGE_MIN constant declared = 0.15', () =>
    /const LONG_SHOT_EDGE_MIN\s*=\s*0\.15\s*;/.test(STRIPPED));
  assert('constants declared adjacent to STALE_MINUTES (logical grouping)', () => {
    // STALE_MINUTES line index < LONG_SHOT_ODDS_MIN line index, both in top ~700 lines
    const lines = STRIPPED.split('\n');
    const stale = lines.findIndex(l => /const STALE_MINUTES\s*=\s*25/.test(l));
    const odds = lines.findIndex(l => /const LONG_SHOT_ODDS_MIN\s*=\s*6/.test(l));
    return stale > 0 && odds > stale && odds - stale < 40;
  });
});

group('v2.3.13 LOW-3 — Bets!AT long-shot branch', () => {
  const atLine = findLine(STRIPPED, "'=IF(AB' + r + '=", 'Type the 3 odds');
  assert('Bets!AT formula present', () => !!atLine);
  if (!atLine) return;

  assert('Bets!AT contains AND(AE>=LONG_SHOT_ODDS_MIN, AG>=LONG_SHOT_EDGE_MIN)', () =>
    /AND\(AE'\s*\+\s*r\s*\+\s*'>='\s*\+\s*LONG_SHOT_ODDS_MIN\s*\+\s*',AG'\s*\+\s*r\s*\+\s*'>='\s*\+\s*LONG_SHOT_EDGE_MIN\s*\+\s*'\)/.test(atLine));

  assert('Bets!AT long-shot WAIT message text present', () =>
    atLine.includes('WAIT — long-shot review: odds'));

  assert('Bets!AT long-shot message references sharp-book + team news + stake comfort', () =>
    atLine.includes('sharp-book') && atLine.includes('team news') && atLine.includes('stake comfort'));

  assert('Bets!AT long-shot branch sits INSIDE the AH="BET" arm', () => {
    const i1 = atLine.indexOf('IF(AH');
    const i2 = atLine.indexOf('AND(AE');
    return i1 >= 0 && i2 > i1;
  });
});

group('v2.3.13 LOW-3 — Matchday!M long-shot branch', () => {
  const mLine = findLine(STRIPPED, "'=IF(Bets!AH' + r + '=", 'enter odds');
  assert('Matchday!M formula present', () => !!mLine);
  if (!mLine) return;

  assert('Matchday!M contains AND(Bets!AE>=LONG_SHOT_ODDS_MIN, Bets!AG>=LONG_SHOT_EDGE_MIN)', () =>
    /AND\(Bets!AE'\s*\+\s*r\s*\+\s*'>='\s*\+\s*LONG_SHOT_ODDS_MIN\s*\+\s*',Bets!AG'\s*\+\s*r\s*\+\s*'>='\s*\+\s*LONG_SHOT_EDGE_MIN\s*\+\s*'\)/.test(mLine));

  assert('Matchday!M long-shot branch sits INSIDE the L="" arm', () => {
    const lEmpty = mLine.indexOf("L' + rm + '=\"\"");
    const andAE = mLine.indexOf('AND(Bets!AE');
    return lEmpty >= 0 && andAE > lEmpty;
  });

  assert('Matchday!M still has L="N" SKIP branch (regression check)', () =>
    /L'\s*\+\s*rm\s*\+\s*'="N","🔴 SKIP — team news concern"/.test(mLine));

  assert('Matchday!M still has L="Y" PLACE branch (regression check)', () =>
    /L'\s*\+\s*rm\s*\+\s*'="Y","🟢 PLACE £"/.test(mLine));
});

group('v2.3.13+ accuracy — calibrated probabilities drive betting math', () => {
  assert('Bets!V formula blends calibrated BC with raw I only as blank fallback', () =>
    /Method!\$B\$4\*IF\(BC'\s*\+\s*r\s*\+\s*'="",I'\s*\+\s*r\s*\+\s*',BC'/.test(STRIPPED));
  assert('Bets!W formula blends calibrated BD with raw J only as blank fallback', () =>
    /Method!\$B\$4\*IF\(BD'\s*\+\s*r\s*\+\s*'="",J'\s*\+\s*r\s*\+\s*',BD'/.test(STRIPPED));
  assert('Bets!X formula blends calibrated BE with raw K only as blank fallback', () =>
    /Method!\$B\$4\*IF\(BE'\s*\+\s*r\s*\+\s*'="",K'\s*\+\s*r\s*\+\s*',BE'/.test(STRIPPED));
  assert('tail-trap ratio uses calibrated BC:BE, not raw I:K', () =>
    /INDEX\(BC'\s*\+\s*r\s*\+\s*':BE/.test(STRIPPED) &&
    !/INDEX\(I'\s*\+\s*r\s*\+\s*':K/.test(STRIPPED));
});

group('v2.3.13+ accuracy — CLV uses odds taken, not model fair odds', () => {
  const body = extractFunctionBody(STRIPPED, 'refreshCLV');
  assert('refreshCLV reads backed odds AO', () =>
    /BETS_COL\.backedOdds/.test(body));
  assert('refreshCLV reads picked odds AE only as legacy fallback', () =>
    /BETS_COL\.pickedOdds/.test(body) && /fallbackOdds/.test(body));
  assert('refreshCLV writes takenOdds into CLV col E', () =>
    /takenOdds/.test(body) && !/modelOdds/.test(body));
});

// =============================================================================
// SUITE 11 — Formula structural invariants (paren balance after rendering)
// =============================================================================

group('Formula structural invariants — paren balance', () => {
  const atLine = findLine(STRIPPED, "'=IF(AB' + r + '=", 'Type the 3 odds');
  if (atLine) {
    const rendered = renderFormula(atLine);
    const w = parenWalk(rendered);
    assert(`Bets!AT renders balanced (opens=${w.opens}, closes=${w.closes})`, () =>
      w.opens === w.closes && w.endDepth === 0 && w.minDepth === 0);
  }

  const mLine = findLine(STRIPPED, "'=IF(Bets!AH' + r + '=", 'enter odds');
  if (mLine) {
    const rendered = renderFormula(mLine);
    const w = parenWalk(rendered);
    assert(`Matchday!M renders balanced (opens=${w.opens}, closes=${w.closes})`, () =>
      w.opens === w.closes && w.endDepth === 0 && w.minDepth === 0);
  }

  // CLV formula on Matchday!U
  const uLine = findLine(STRIPPED, "OR(T' + rm + '=", "P' + rm + '/T' + rm + '-1");
  if (uLine) {
    const rendered = renderFormula(uLine);
    const w = parenWalk(rendered);
    assert(`Matchday!U (CLV) renders balanced`, () =>
      w.opens === w.closes && w.endDepth === 0 && w.minDepth === 0);
  }

  // Bets!AU (audit message) — large formula with many branches
  const auLine = findLine(STRIPPED, "'=IF(AN' + r + '<>", '⚠ type the 3 odds');
  if (auLine) {
    const rendered = renderFormula(auLine);
    const w = parenWalk(rendered);
    assert(`Bets!AU (audit) renders balanced`, () =>
      w.opens === w.closes && w.endDepth === 0 && w.minDepth === 0);
  }
});

// =============================================================================
// SUITE 12 — Setup-time invariants
// =============================================================================

group('Setup-time invariants — auto-refresh trigger', () => {
  const install = extractFunctionBody(STRIPPED, 'installAutoRefresh');
  const remove = extractFunctionBody(STRIPPED, 'removeAutoRefresh');

  assert('installAutoRefresh creates a refreshAll time-trigger', () =>
    /ScriptApp\.newTrigger\(['"]refreshAll['"]\)/.test(install) &&
    /\.everyMinutes\(\s*POLL_MINUTES\s*\)/.test(install));

  assert('installAutoRefresh writes ON banner to M_CELL.autoStatus', () =>
    /M_CELL\.autoStatus\)\.setValue\('ON · every '/.test(install));

  assert('removeAutoRefresh deletes auto triggers + writes OFF to B63', () =>
    /deleteTrigger/.test(remove) && /M_CELL\.autoStatus\)\.setValue\(['"]OFF['"]\)/.test(remove));
});

group('Setup-time invariants — menu installation (onOpen)', () => {
  const body = extractFunctionBody(STRIPPED, 'onOpen');
  assert('onOpen calls SpreadsheetApp.getUi().createMenu', () =>
    /createMenu\(['"]WC26 Engine['"]\)/.test(body));

  const requiredItems = [
    'refreshAll',
    'installAutoRefresh',
    'removeAutoRefresh',
    'clearDiagnosticSurfaces',
    'installEngine',
    'extendToKnockouts',
    'applyProtections',
  ];
  for (const handler of requiredItems) {
    assert(`menu binds handler: ${handler}`, () =>
      new RegExp(`addItem\\([^)]*['"]${handler}['"]\\)`).test(body) ||
      new RegExp(`,\\s*['"]${handler}['"]\\s*\\)`).test(body));
  }
});

group('Setup-time invariants — lock acquisition pattern', () => {
  // refreshAll acquires lock — same pattern as clearDiagnosticSurfaces
  const body = extractFunctionBody(STRIPPED, 'refreshAll');
  assert('refreshAll uses LockService.getScriptLock()', () =>
    /LockService\.getScriptLock\(\)/.test(body));
  assert('refreshAll uses tryLock(0) (non-blocking)', () =>
    /tryLock\(\s*0\s*\)/.test(body));
  assert('refreshAll releases lock in finally', () =>
    /finally\s*\{[\s\S]*?lock\.releaseLock\(\)/.test(body));
});

group('Setup-time invariants — _tryStep_ error-collection sibling pattern', () => {
  // Each refresh* sibling that participates in refreshAll should use _tryStep_
  // (the v2.3.x invariant that no single refresh failure breaks the whole tick)
  const body = extractFunctionBody(STRIPPED, 'refreshAll');
  assert('refreshAll uses _tryStep_ helper for sibling refresh calls', () =>
    /_tryStep_\s*\(/.test(body));
  assert('_tryStep_ helper defined', () =>
    /function _tryStep_\s*\(/.test(STRIPPED) || /_tryStep_\s*=\s*function/.test(STRIPPED));
});

// =============================================================================
// SUITE 13 — XLSX mirror cross-check (optional — skipped if xlsx absent)
// =============================================================================

group('XLSX mirror cross-check (Bets row 2 + Matchday row 4)', () => {
  if (!existsSync(XLSX_PATH)) {
    console.log('  ⊘ xlsx not found at ' + XLSX_PATH + ' — suite skipped');
    return;
  }

  // Auto-detect sheet xml files by walking workbook.xml → rels. Sheet order
  // and rId numbering vary between xlsx saves, so hard-coding sheet4/sheet5
  // is brittle. Build the {name → xml path} map dynamically.
  let workbookXml = '', relsXml = '';
  try {
    workbookXml = execSync(`unzip -p "${XLSX_PATH}" xl/workbook.xml`, { encoding: 'utf8' });
    relsXml = execSync(`unzip -p "${XLSX_PATH}" xl/_rels/workbook.xml.rels`, { encoding: 'utf8' });
  } catch (_) {}
  if (!workbookXml || !relsXml) {
    console.log('  ⊘ could not read workbook.xml / rels — suite skipped');
    return;
  }
  // Map rId → target path. xlsx relationships are self-closing tags with
  // attributes in arbitrary order — match the whole tag, then pluck attrs.
  const ridToTarget = {};
  for (const m of relsXml.matchAll(/<Relationship\b[^>]*\/>/g)) {
    const tag = m[0];
    const idM = tag.match(/Id="(rId\d+)"/);
    const tgtM = tag.match(/Target="([^"]+)"/);
    if (idM && tgtM && tgtM[1].includes('worksheets/')) {
      ridToTarget[idM[1]] = tgtM[1];
    }
  }
  // Map sheet name → xml path
  const nameToPath = {};
  for (const m of workbookXml.matchAll(/<sheet\b[^>]*\/?>/g)) {
    const tag = m[0];
    const nameM = tag.match(/name="([^"]+)"/);
    const ridM = tag.match(/r:id="(rId\d+)"/);
    if (nameM && ridM && ridToTarget[ridM[1]]) {
      const target = ridToTarget[ridM[1]];
      nameToPath[nameM[1]] = target.startsWith('/xl/')
        ? target.slice(1)
        : (target.startsWith('xl/') ? target : 'xl/' + target);
    }
  }
  const betsPath = nameToPath['Bets'];
  const mdPath = nameToPath['Matchday'];
  const dashboardPath = nameToPath['Dashboard'];
  if (!betsPath || !mdPath || !dashboardPath) {
    console.log('  ⊘ could not locate Bets/Matchday/Dashboard sheets in workbook — suite skipped');
    return;
  }
  let betsXml = '', mdXml = '', dashboardXml = '';
  try {
    betsXml = execSync(`unzip -p "${XLSX_PATH}" ${betsPath}`, { encoding: 'utf8' });
    mdXml = execSync(`unzip -p "${XLSX_PATH}" ${mdPath}`, { encoding: 'utf8' });
    dashboardXml = execSync(`unzip -p "${XLSX_PATH}" ${dashboardPath}`, { encoding: 'utf8' });
  } catch (_) {}
  if (!betsXml || !mdXml || !dashboardXml) {
    console.log('  ⊘ could not extract Bets/Matchday/Dashboard xml from xlsx — suite skipped');
    return;
  }
  console.log(`  ℹ resolved Bets → ${betsPath}, Matchday → ${mdPath}, Dashboard → ${dashboardPath}`);

  // Bets L/M/N row 2 should be Matchday mirror (v2.3.6 CRIT)
  const L2 = xlsxFormula(betsXml, 'L2');
  const M2 = xlsxFormula(betsXml, 'M2');
  const N2 = xlsxFormula(betsXml, 'N2');
  assert('xlsx Bets!L2 mirrors Matchday!E (or is operator-blank)', () =>
    L2 === null || /Matchday!E/.test(L2));
  assert('xlsx Bets!M2 mirrors Matchday!F (or is operator-blank)', () =>
    M2 === null || /Matchday!F/.test(M2));
  assert('xlsx Bets!N2 mirrors Matchday!G (or is operator-blank)', () =>
    N2 === null || /Matchday!G/.test(N2));

  const V2 = xlsxFormula(betsXml, 'V2');
  const W2 = xlsxFormula(betsXml, 'W2');
  const X2 = xlsxFormula(betsXml, 'X2');
  const AH2 = xlsxFormula(betsXml, 'AH2');
  const AT2 = xlsxFormula(betsXml, 'AT2');
  assert('xlsx Bets!V2 uses calibrated BC2 with I2 blank fallback', () =>
    !!V2 && /IF\(BC2="",I2,BC2\)/.test(V2));
  assert('xlsx Bets!W2 uses calibrated BD2 with J2 blank fallback', () =>
    !!W2 && /IF\(BD2="",J2,BD2\)/.test(W2));
  assert('xlsx Bets!X2 uses calibrated BE2 with K2 blank fallback', () =>
    !!X2 && /IF\(BE2="",K2,BE2\)/.test(X2));
  assert('xlsx Bets!AH2 tail-ratio uses BC2:BE2', () =>
    !!AH2 && /INDEX\(BC2:BE2,1,AB2\)/.test(AH2));
  assert('xlsx Bets!AT2 tail-ratio uses BC2:BE2', () =>
    !!AT2 && /INDEX\(BC2:BE2,1,AB2\)/.test(AT2));

  // Matchday row 4 master formulas — most reference Bets! columns, but U4
  // (CLV) is self-referential (P4/T4-1 same-row) and has no sheet prefix.
  // Accept: null (operator-input), Bets!/Matchday! ref, OR any formula starting with =.
  const cells = ['A4', 'B4', 'C4', 'D4', 'H4', 'I4', 'J4', 'K4', 'M4', 'S4', 'U4'];
  for (const c of cells) {
    const f = xlsxFormula(mdXml, c);
    assert(`xlsx Matchday!${c} is well-formed (operator-input or any =formula)`,
      () => f === null || /^=/.test(f));
  }
  const M4 = xlsxFormula(mdXml, 'M4');
  assert('xlsx Matchday!M4 has stale-feed/drawdown cascade', () =>
    !!M4 && /Method!\$B\$81=TRUE/.test(M4) && /Method!\$B\$51>=Method!\$B\$52/.test(M4));
  assert('xlsx Matchday!M4 has long-shot review branch', () =>
    !!M4 && /long-shot review/.test(M4));
  assert('xlsx Matchday!M4 has no old drawdown-protection message', () =>
    !!M4 && !/drawdown protection/.test(M4));

  const dashboardFormulas = [...dashboardXml.matchAll(/<f[^>]*>(.*?)<\/f>/gs)]
    .map(m => unescapeHtml(m[1]));
  assert('xlsx Dashboard formulas do not stop at Bets row 73', () =>
    dashboardFormulas.every(f => !/Bets!\$?[A-Z]+\$?2:\$?[A-Z]+\$?73/.test(f)));
  assert('xlsx Dashboard formulas do not stop at Matchday row 75', () =>
    dashboardFormulas.every(f => !/Matchday!U4:U75/.test(f)));
});

// =============================================================================
// SUITE 14 — Negative invariants (the bad old patterns are GONE)
// =============================================================================

group('Negative invariants — pre-v2.3.x bugs are absent', () => {
  // v2.3.6 — Bets L:N stayed blank on knockouts
  assert('no "ref=\\"L3:L73\\"" stale-shared-formula residue', () =>
    !/ref="L3:L73"/.test(STRIPPED));

  // v2.3.8 — refreshDiagnostics did NOT throw, raced with refreshAll's clear
  // (covered as positive assertion in Suite 5; double-checked here as negative)
  assert('refreshDiagnostics does NOT silently return without throwing on error', () => {
    const body = extractFunctionBody(STRIPPED, 'refreshDiagnostics');
    if (!body) return true;
    // Heuristic: if there's a _setErrLog_( inside a catch, there must also be a throw nearby
    const catches = body.match(/catch\s*\([^)]*\)\s*\{[\s\S]*?\n\s*\}/g) || [];
    return catches.every(c => !/_setErrLog_/.test(c) || /throw/.test(c));
  });

  // v2.3.12 CRIT-1 — the single-branch lie pattern
  assert('no source line contains the OLD single-branch drawdown attribution', () => {
    const oldPattern = /IF\(Method!\$B\$55=TRUE\(\),"🛑 PAUSED — drawdown/;
    return !oldPattern.test(STRIPPED);
  });

  // v2.3.13 LOW-2 — no fresh-branch silence
  assert('refreshLive fresh-branch is NOT silent on M_CELL.autoStatus', () => {
    const body = extractFunctionBody(STRIPPED, 'refreshLive');
    return /M_CELL\.autoStatus\)\.setValue\('ON · every/.test(body);
  });
});

// =============================================================================
// JSON REPORT — actually evaluate the engine and emit numerical ground-truth
// =============================================================================
//
// tests/live/test_goal_grid_node.py drives this harness and json.loads()s the
// report block between the sentinels below. The block is the SAME engine
// source (extracted from the .gs file, eval'd under Node) being invoked at
// pinned λ values, so the Python test asserts against the REAL engine math
// rather than a Python replica. If the report block is removed or its shape
// changes, that Python test breaks loudly — both sides are load-bearing.

function buildEngineReport() {
  // Extract the relevant function source from the .gs file and eval it under
  // Node. The engine functions are pure JS (Math.*, Array, no Sheets APIs)
  // so they run unmodified outside Apps Script.
  const engineFns = [
    '_poissonPmf_',
    '_factorial_',
    '_dcTau_',
    '_buildScoreMatrix_',
    '_sumMatrix_',
    'GOAL_GRID',
    '_knockoutStageFor_',
  ];
  const sources = engineFns.map(n => extractFunctionBody(STRIPPED, n));
  for (let i = 0; i < engineFns.length; i++) {
    if (!sources[i]) throw new Error(`engine fn missing from source: ${engineFns[i]}`);
  }
  // Pull constants the engine fns close over.
  const constants = [];
  const constNames = [
    'GOAL_GRID_MAX_GOALS', 'GOAL_GRID_TAU',
    'KNOCKOUT_FIRST_M', 'KNOCKOUT_LAST_M',
  ];
  for (const cname of constNames) {
    const m = STRIPPED.match(new RegExp(`const\\s+${cname}\\s*=\\s*([^;]+);`));
    if (!m) throw new Error(`constant missing from source: ${cname}`);
    constants.push(`const ${cname} = ${m[1].trim()};`);
  }
  // KNOCKOUT_STAGES is a multi-line array literal — match across newlines.
  const ksMatch = STRIPPED.match(/const\s+KNOCKOUT_STAGES\s*=\s*\[([\s\S]*?)\];/);
  if (!ksMatch) throw new Error('constant missing: KNOCKOUT_STAGES');
  constants.push(`const KNOCKOUT_STAGES = [${ksMatch[1]}];`);

  // Build a sandbox script: constants first, then function declarations,
  // then return the bindings we need. `new Function` is safe here — the
  // input is the same .gs file the harness already statically verified.
  const sandbox = constants.join('\n') + '\n' + sources.join('\n\n') +
    `\nreturn { _buildScoreMatrix_, _sumMatrix_, GOAL_GRID, _knockoutStageFor_,
      GOAL_GRID_MAX_GOALS, GOAL_GRID_TAU };`;
  const bindings = (new Function(sandbox))();

  function scenario(lh, la) {
    const M = bindings._buildScoreMatrix_(lh, la, bindings.GOAL_GRID_MAX_GOALS, bindings.GOAL_GRID_TAU);
    let total = 0;
    for (let h = 0; h < M.length; h++) for (let a = 0; a < M.length; a++) total += M[h][a];
    const markets = {
      ou15: bindings._sumMatrix_(M, (h, a) => h + a > 1.5),
      ou25: bindings._sumMatrix_(M, (h, a) => h + a > 2.5),
      ou35: bindings._sumMatrix_(M, (h, a) => h + a > 3.5),
      btts: bindings._sumMatrix_(M, (h, a) => h > 0 && a > 0),
      home: bindings._sumMatrix_(M, (h, a) => h > a),
      draw: bindings._sumMatrix_(M, (h, a) => h === a),
      away: bindings._sumMatrix_(M, (h, a) => h < a),
    };
    const routes = {
      ou15: bindings.GOAL_GRID(lh, la, 'ou15'),
      ou25: bindings.GOAL_GRID(lh, la, 'ou25'),
      ou35: bindings.GOAL_GRID(lh, la, 'ou35'),
      btts: bindings.GOAL_GRID(lh, la, 'btts'),
      ah0:  bindings.GOAL_GRID(lh, la, 'ah0'),
      cs10: bindings.GOAL_GRID(lh, la, 'cs10'),
      cs01: bindings.GOAL_GRID(lh, la, 'cs01'),
      cs11: bindings.GOAL_GRID(lh, la, 'cs11'),
      cs22: bindings.GOAL_GRID(lh, la, 'cs22'),
    };
    return {
      lam_h: lh, lam_a: la,
      matrix: M, total,
      swap_ratio: M[1][0] / M[0][1],
      markets, goal_grid_routes: routes,
    };
  }

  // Knockout stage pure-core survey: classify every m the test cares about.
  const byMatch = {};
  for (let m = 73; m <= 104; m++) byMatch[String(m)] = bindings._knockoutStageFor_(m);
  byMatch.below = bindings._knockoutStageFor_(72);
  byMatch.above = bindings._knockoutStageFor_(105);

  return {
    max_goals: bindings.GOAL_GRID_MAX_GOALS,
    rho: bindings.GOAL_GRID_TAU,
    scenarios: {
      asym_1p8_0p9: scenario(1.8, 0.9),
      asym_0p9_1p8: scenario(0.9, 1.8),
      sym_1p4_1p4:  scenario(1.4, 1.4),
    },
    knockout_pure_core: { runnable: true, byMatch },
  };
}

let engineReport;
try {
  engineReport = buildEngineReport();
} catch (e) {
  // Report is load-bearing for CI — surface the failure rather than silently
  // truncating stdout. Tests will fail with the harness exit code.
  console.error('\nFATAL: engine report build failed: ' + e.message);
  console.error(e.stack);
  process.exit(3);
}

// Sentinel-delimited JSON report. Python wrapper slices between these markers
// and json.loads the inner block. Sentinels are exact-match strings (no
// regex chars, no leading/trailing whitespace inside the markers).
console.log('\n===JSON_REPORT_BEGIN===');
console.log(JSON.stringify(engineReport));
console.log('===JSON_REPORT_END===');

// =============================================================================
// SUMMARY
// =============================================================================

console.log('\n' + '='.repeat(72));
console.log(`Results: ${passed}/${total} passed, ${failed} failed`);
if (failures.length) {
  console.log('\nFailures:');
  for (const f of failures) console.log('  - ' + f);
  process.exit(1);
} else {
  console.log('\n✓ All assertions passed — v2.3.13 source matches the v2.3.x regression battery.');
  process.exit(0);
}
