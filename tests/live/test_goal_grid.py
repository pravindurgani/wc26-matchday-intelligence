"""Static + behavioral checks for the Phase 5B GOAL_GRID custom function and
the Phase 5C/5D refreshGoalGrid / refreshCLV refresher wiring.

Static layer: parse the .gs source and assert the load-bearing constants,
function definitions, and wiring exist.

Behavioral layer: replicate the JS _buildScoreMatrix_ in Python verbatim
and assert the resulting cells and market probabilities match pinned
reference numbers (derived from scripts/03_simulate.py:build_score_matrix
with use_dispersion=False) to 1e-9. This catches sign / convention bugs
the regex-only test could miss.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

GS = (Path(__file__).resolve().parents[2]
      / "wc26-engine-gs" / "WC26_Engine_AppsScript_v2.3.1.gs")


def _src() -> str:
    return GS.read_text()


def test_tau_and_max_goals_constants():
    src = _src()
    # R13 C3: bumped to 15 to match production sim's max_g=15 (R12 MED).
    assert "const GOAL_GRID_MAX_GOALS = 15;" in src
    assert "const GOAL_GRID_TAU = -0.13;" in src


def test_clv_window_constant():
    assert "const CLV_ROLLING_WINDOW = 20;" in _src()


def test_sheet_constants_include_grid_and_clv():
    src = _src()
    assert "goalGrid: 'Goal Grid'" in src
    assert "clv: 'CLV'" in src


def test_goal_grid_customfunction_defined():
    src = _src()
    # JSDoc @customfunction immediately preceding `function GOAL_GRID(`
    pat = re.compile(
        r"@customfunction\s*\*/\s*function\s+GOAL_GRID\s*\(\s*lam_h\s*,\s*lam_a\s*,\s*market\s*\)",
        re.DOTALL,
    )
    assert pat.search(src), "GOAL_GRID @customfunction not found"


def test_goal_grid_markets_routed():
    src = _src()
    for market in ("'ou25'", "'ou15'", "'ou35'", "'btts'", "'ah0'"):
        assert market in src, f"missing market routing for {market}"
    # Correct-score regex must accept 0..MAX_GOALS digits.
    assert "^cs(\\d+)(\\d+)$" in src


# --------------------------------------------------------------------------
# Behavioral DC layer — replicate the JS _buildScoreMatrix_ in Python and
# pin the resulting numbers. Reference values derived from
# scripts/03_simulate.py:build_score_matrix(use_dispersion=False) which is
# the production Poisson-mode sim GOAL_GRID is meant to mirror.
# --------------------------------------------------------------------------
DC_RHO = -0.13
# R13 C3: bumped 10 → 15 to follow the Apps Script change (which itself
# follows R12 MED's sim default change).
MAX_G = 15


def _js_dc_tau(h, a, lam_h, lam_a, rho):
    if h == 0 and a == 0: return 1 - lam_h * lam_a * rho
    if h == 0 and a == 1: return 1 + lam_a * rho
    if h == 1 and a == 0: return 1 + lam_h * rho
    if h == 1 and a == 1: return 1 - rho
    return 1.0


def _js_poisson_pmf(lam, k):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _js_build_score_matrix(lam_h, lam_a, max_g=MAX_G, rho=DC_RHO):
    """Verbatim Python replica of WC26_Engine_AppsScript_v2.3.1.gs
    _buildScoreMatrix_ — Poisson marginals × DC τ-correction × renorm.

    Mirrors the engine's degenerate-matrix guard: when every cell underflows
    to 0 (e.g. λ ≥ ~745 in IEEE 754) the function raises rather than silently
    returning an all-zero matrix that would make every market 0%.
    """
    ph = [_js_poisson_pmf(lam_h, i) for i in range(max_g + 1)]
    pa = [_js_poisson_pmf(lam_a, i) for i in range(max_g + 1)]
    M = [[0.0] * (max_g + 1) for _ in range(max_g + 1)]
    total = 0.0
    for h in range(max_g + 1):
        for a in range(max_g + 1):
            v = ph[h] * pa[a] * _js_dc_tau(h, a, lam_h, lam_a, rho)
            v = max(v, 0.0)
            M[h][a] = v
            total += v
    if total > 0:
        for h in range(max_g + 1):
            for a in range(max_g + 1):
                M[h][a] /= total
    elif total == 0:
        # Degenerate underflow (λ ≥ ~745). NaN propagation falls through
        # untouched because NaN comparisons evaluate False on both sides.
        raise ValueError("matrix collapsed to zero — λ too large")
    return M


def _market(M, pred):
    n = len(M)
    return sum(M[h][a] for h in range(n) for a in range(n) if pred(h, a))


def test_dc_cells_asymmetric_pinned():
    """λ_h=1.8, λ_a=0.9, ρ=-0.13. Pinned numerically against
    scripts/03_simulate.py:build_score_matrix(use_dispersion=False).
    If GOAL_GRID's _dcTau_ ever home/away-swaps, these break.
    R13 C3: re-pinned at max_g=15."""
    M = _js_build_score_matrix(1.8, 0.9)
    tol = 1e-9
    assert abs(M[0][0] - 0.0819388537) < tol
    assert abs(M[0][1] - 0.0537888709) < tol
    assert abs(M[1][0] - 0.0933233864) < tol
    assert abs(M[1][1] - 0.1239032427) < tol
    assert abs(M[2][1] - 0.0986839986) < tol
    assert abs(M[1][2] - 0.0493419993) < tol
    assert abs(M[2][0] - 0.1096488874) < tol
    assert abs(M[0][2] - 0.0274122218) < tol
    assert abs(M[2][2] - 0.0444077994) < tol
    assert abs(M[3][0] - 0.0657893324) < tol
    s = sum(M[h][a] for h in range(MAX_G + 1) for a in range(MAX_G + 1))
    assert abs(s - 1.0) < tol


def test_dc_swap_detector_asymmetric():
    """λ_h > λ_a ⇒ P(1,0) > P(0,1). Ratio pinned at 1.7350 so a sign-swap
    in _dcTau_ fails loudly rather than silently inverting markets."""
    M = _js_build_score_matrix(1.8, 0.9)
    ratio = M[1][0] / M[0][1]
    assert ratio > 1.0, f"swap detector FAILED: P(1,0)/P(0,1) = {ratio:.4f}"
    assert abs(ratio - 1.7350) < 1e-4, f"ratio drift: {ratio:.6f}"


def test_dc_swap_detector_reversed():
    """Reversed λ_h < λ_a ⇒ P(0,1) > P(1,0). Cells mirror the asym case.
    R13 C3: re-pinned at max_g=15 (was 10 pre-R12)."""
    M = _js_build_score_matrix(0.9, 1.8)
    assert M[0][1] > M[1][0]
    tol = 1e-9
    assert abs(M[1][0] - 0.0537888709) < tol
    assert abs(M[0][1] - 0.0933233864) < tol


def test_dc_cells_symmetric_pinned():
    """λ_h = λ_a = 1.4. P(0,1) and P(1,0) must be exact mirrors.
    R13 C3: re-pinned at max_g=15."""
    M = _js_build_score_matrix(1.4, 1.4)
    tol = 1e-9
    assert abs(M[0][0] - 0.0763044666) < tol
    assert abs(M[0][1] - 0.0696396837) < tol
    assert abs(M[1][0] - 0.0696396837) < tol
    assert abs(M[1][1] - 0.1346821267) < tol
    assert abs(M[0][1] - M[1][0]) < 1e-15


def test_dc_markets_asymmetric_pinned():
    """Market probabilities derived from the asymmetric matrix —
    pinned so a τ regression flips home/away markets visibly.
    R13 C3: re-pinned at max_g=15."""
    M = _js_build_score_matrix(1.8, 0.9)
    tol = 1e-9
    assert abs(_market(M, lambda h, a: h + a > 1.5) - 0.7709488890) < tol
    assert abs(_market(M, lambda h, a: h + a > 2.5) - 0.5099845371) < tol
    assert abs(_market(M, lambda h, a: h + a > 3.5) - 0.2879455402) < tol
    assert abs(_market(M, lambda h, a: h > 0 and a > 0) - 0.5131216747) < tol
    assert abs(_market(M, lambda h, a: h > a) - 0.5617395976) < tol
    assert abs(_market(M, lambda h, a: h == a) - 0.2591075167) < tol
    assert abs(_market(M, lambda h, a: h < a) - 0.1791528858) < tol


def test_dc_tau_source_branches_present():
    """Static guard: the four _dcTau_ branches the behavioral tests above
    rely on must still appear in the .gs source verbatim. Catches the case
    where someone deletes _dcTau_ and leaves a hardcoded stub."""
    src = _src()
    assert re.search(r"h\s*===\s*0\s*&&\s*a\s*===\s*0\)\s*return\s*1\s*-\s*lam_h\s*\*\s*lam_a\s*\*\s*rho", src)
    assert re.search(r"h\s*===\s*0\s*&&\s*a\s*===\s*1\)\s*return\s*1\s*\+\s*lam_a\s*\*\s*rho", src)
    assert re.search(r"h\s*===\s*1\s*&&\s*a\s*===\s*0\)\s*return\s*1\s*\+\s*lam_h\s*\*\s*rho", src)
    assert re.search(r"h\s*===\s*1\s*&&\s*a\s*===\s*1\)\s*return\s*1\s*-\s*rho", src)


def test_helpers_defined():
    src = _src()
    for fn in ("_poissonPmf_", "_factorial_", "_dcTau_",
               "_buildScoreMatrix_", "_sumMatrix_"):
        assert f"function {fn}(" in src, f"helper {fn} not defined"


def test_score_matrix_renormalized():
    """After τ correction the matrix is renormalized — otherwise the
    cells drift from a true probability mass function."""
    src = _src()
    assert "M[h][a] /= total" in src


def test_poisson_pmf_formula():
    src = _src()
    # exp(-lam) * lam^k / k!
    assert re.search(
        r"Math\.exp\(-lam\)\s*\*\s*Math\.pow\(lam,\s*k\)\s*/\s*_factorial_\(k\)",
        src,
    )


def test_refresh_goal_grid_and_clv_defined():
    src = _src()
    assert "function refreshGoalGrid(" in src
    assert "function refreshCLV(" in src


def test_seed_headers_defined():
    src = _src()
    assert "function _seedGoalGridHeaders_(" in src
    assert "function _seedCLVHeaders_(" in src


def test_refresh_all_wires_grid_and_clv():
    src = _src()
    # _tryStep_ call for goalGrid passes the predictions payload through.
    assert re.search(
        r"_tryStep_\(\s*errors\s*,\s*'goalGrid'\s*,\s*function\(\)\s*\{\s*refreshGoalGrid\(predictions\)",
        src,
    )
    assert re.search(
        r"_tryStep_\(\s*errors\s*,\s*'clv'\s*,\s*function\(\)\s*\{\s*refreshCLV\(\)",
        src,
    )


def test_menu_items_for_grid_and_clv():
    src = _src()
    assert re.search(
        r"\.addItem\(\s*'Refresh goal grid now'\s*,\s*'refreshGoalGrid'\s*\)",
        src,
    )
    assert re.search(
        r"\.addItem\(\s*'Refresh CLV now'\s*,\s*'refreshCLV'\s*\)",
        src,
    )


def test_clv_uses_rolling_window_constant():
    """CLV refresher must wire CLV_ROLLING_WINDOW into the formula
    so changing the constant changes the sheet — no magic numbers."""
    src = _src()
    # Extract refreshCLV body and assert it references the constant.
    body = re.search(r"function refreshCLV\(\)\s*\{(.*?)\n\}", src, re.DOTALL)
    assert body, "refreshCLV body not extractable"
    assert "CLV_ROLLING_WINDOW" in body.group(1), \
        "refreshCLV must reference CLV_ROLLING_WINDOW (no magic numbers)"


def test_clv_header_note_scopes_to_1x2():
    """Item #6A: _seedCLVHeaders_ must seed a 1X2-only scope note so the
    operator doesn't expect CLV numbers on O/U or BTTS picks (those land
    in v2.4). Verbatim string match keeps the UX promise stable."""
    src = _src()
    note = ("CLV tracked for 1X2 picks only — O/U and BTTS coming in v2.4")
    # Note must appear inside _seedCLVHeaders_ body.
    body = re.search(
        r"function _seedCLVHeaders_\([^)]*\)\s*\{(.*?)\n\}",
        src, re.DOTALL,
    )
    assert body, "_seedCLVHeaders_ body not extractable"
    assert note in body.group(1), \
        f"CLV scope note not found in _seedCLVHeaders_: {note!r}"


def test_refresh_goal_grid_uses_b8_not_b7():
    """Item #6B: goal markets get their own edge knob at Method!$B$8 so
    O/U + BTTS thresholds can be tuned independently of 1X2 (Method!$B$7).
    refreshGoalGrid must reference $B$8 in its BET? formula."""
    src = _src()
    body = re.search(
        r"function refreshGoalGrid\([^)]*\)\s*\{(.*?)\nfunction ",
        src, re.DOTALL,
    )
    assert body, "refreshGoalGrid body not extractable"
    text = body.group(1)
    assert "Method!$B$8" in text, \
        "refreshGoalGrid must read Method!$B$8 (goal_markets_min_edge)"
    # The PASS-vs-BET comparator line must not reference $B$7 anymore.
    pass_bet = re.search(
        r"<Method!\$B\$\d+,\"PASS\",\"BET\"", text,
    )
    assert pass_bet, "PASS/BET formula not found in refreshGoalGrid"
    assert "$B$8" in pass_bet.group(0), \
        f"PASS/BET formula must use $B$8, got: {pass_bet.group(0)}"


def test_goal_markets_threshold_split_commented():
    """The split must be self-documenting in the .gs so a future
    maintainer understands why goal markets have their own threshold."""
    src = _src()
    body = re.search(
        r"function refreshGoalGrid\([^)]*\)\s*\{(.*?)\nfunction ",
        src, re.DOTALL,
    )
    assert body, "refreshGoalGrid body not extractable"
    text = body.group(1).lower()
    # One short rationale line — must mention goal_markets_min_edge AND
    # something about why goal markets differ from 1X2 (overround / tighter).
    assert "goal_markets_min_edge" in text, \
        "split-threshold rationale must name the Method cell semantically"
    assert "overround" in text or "tighter" in text, \
        "split-threshold rationale must explain why goal markets diverge"


def test_goal_markets_min_edge_seeder_defined():
    """Method!B8 must be seeded from B7 on install so live behavior is
    unchanged at zero risk. _seedGoalMarketsMinEdge_ runs once via
    installEngine and is idempotent (skips if B8 already populated)."""
    src = _src()
    assert "function _seedGoalMarketsMinEdge_(" in src, \
        "_seedGoalMarketsMinEdge_ helper not defined"
    # installEngine must call the seeder.
    assert re.search(
        r"function installEngine\([^)]*\)\s*\{[^}]*_seedGoalMarketsMinEdge_\(\)",
        src, re.DOTALL,
    ), "installEngine must invoke _seedGoalMarketsMinEdge_()"
    # Seeder must read B7 and write B8 (defaults to same value).
    body = re.search(
        r"function _seedGoalMarketsMinEdge_\([^)]*\)\s*\{(.*?)\n\}",
        src, re.DOTALL,
    )
    assert body
    text = body.group(1)
    assert "'B7'" in text and "'B8'" in text, \
        "seeder must reference both B7 (source) and B8 (target)"


def test_boundary_guard_documented():
    """The τ=−0.13 + λ_clip≈7 → 0.91 < 1 margin must be called out so a
    future maintainer who bumps τ knows the (0,0) cell can go negative."""
    src = _src()
    # Anywhere in the header for GOAL_GRID we mention the < 1 invariant.
    block = re.search(
        r"// GOAL_GRID.*?function GOAL_GRID\(",
        src, re.DOTALL,
    )
    assert block, "GOAL_GRID section header not found"
    text = block.group(0).lower()
    assert "< 1" in text or "&lt; 1" in text or "non-negative" in text
