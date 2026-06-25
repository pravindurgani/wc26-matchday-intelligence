"""
Adversarial audit of the GoalGrid math in
wc26-engine-gs/WC26_Engine_AppsScript_v*.gs (latest version)
  - _poissonPmf_   (line 1373)
  - _factorial_    (line 1378)
  - _dcTau_        (line 1387)
  - _buildScoreMatrix_  (line 1398)
  - GOAL_GRID public custom-function  (line 1354)

We exercise the math through the Python replica in tests/live/test_goal_grid.py
(`_js_build_score_matrix`, `_js_dc_tau`, `_js_poisson_pmf`) which mirrors
the .gs source verbatim. Where the production GOAL_GRID public wrapper
guards inputs but the internal builder does not, we note that distinction.

Convention:
  * pytest.raises(...) — LOUD failure on bad input.
  * @pytest.mark.xfail(strict=True, reason=...) — SILENT BUG: replica
    (and therefore the inner builder it mirrors) returns a plausible
    but wrong number. xfail(strict) ensures the test FAILS if the
    silent bug is later fixed, prompting us to flip the marker.
  * plain asserts — LOUD-by-spec or by-design behavior we want pinned.

Read-only: does NOT modify the .gs file.
"""
from __future__ import annotations

import math
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "live"))

from test_goal_grid import (  # noqa: E402
    DC_RHO,
    MAX_G,
    _js_build_score_matrix,
    _js_dc_tau,
    _js_poisson_pmf,
    _market,
)
from _node_resolver import NODE_BIN  # noqa: E402

WDL_TOL = 1e-9

# Node binary for driving the REAL .gs engine (proves the fix landed in
# the actual JavaScript, not just the Python replica). NODE_BIN is
# resolved via _node_resolver (env override + PATH lookup); under CI
# the resolver itself errors at import if node is missing, so a
# silent skip cannot mask a missing ground-truth check.
def _engine_gs_path() -> Path:
    """Find the latest WC26_Engine_AppsScript_v*.gs file (version-agnostic)."""
    root = ROOT / "wc26-engine-gs"
    candidates = sorted(root.glob("WC26_Engine_AppsScript_v*.gs"))
    if not candidates:
        raise FileNotFoundError(f"No WC26_Engine_AppsScript_v*.gs in {root}")
    return candidates[-1]  # natural sort picks highest version


GS_PATH = _engine_gs_path()


def _wdl(M) -> tuple[float, float, float]:
    """Return (P(home win), P(draw), P(away win)) over the matrix."""
    n = len(M)
    hw = sum(M[h][a] for h in range(n) for a in range(n) if h > a)
    dr = sum(M[h][a] for h in range(n) for a in range(n) if h == a)
    aw = sum(M[h][a] for h in range(n) for a in range(n) if h < a)
    return hw, dr, aw


def _assert_wdl_sums_to_one(M, label: str, tol: float = WDL_TOL):
    hw, dr, aw = _wdl(M)
    s = hw + dr + aw
    assert math.isclose(s, 1.0, abs_tol=tol), \
        f"[{label}] W+D+L sum = {s!r} drifted from 1.0 by {abs(s-1.0):.2e}"


# ======================================================================
# A. λ EXTREMES — zero, near-zero, large, NaN, negative
# ======================================================================
class TestLambdaExtremes:
    def test_both_lambdas_zero_is_certain_zero_zero(self):
        """LOUD-by-spec: λ_h = λ_a = 0 means both teams expected
        zero goals. Matrix collapses to a point mass at (0,0)."""
        M = _js_build_score_matrix(0.0, 0.0)
        assert M[0][0] == 1.0
        # All other cells zero.
        for h in range(MAX_G + 1):
            for a in range(MAX_G + 1):
                if (h, a) != (0, 0):
                    assert M[h][a] == 0.0
        _assert_wdl_sums_to_one(M, "λ=0,λ=0")

    def test_underflow_lambdas_still_sum_to_one(self):
        """LOUD-by-spec: λ = 0.001 — practically zero. (0,0) ≈ 0.998,
        renorm catches any rounding drift."""
        M = _js_build_score_matrix(0.001, 0.001)
        assert M[0][0] > 0.997
        _assert_wdl_sums_to_one(M, "λ=0.001")

    def test_large_lambdas_still_sum_to_one(self):
        """LOUD-by-spec: λ = 10 (way above the λ_clip ≈ 7 guard, but
        the inner builder must still be numerically safe). exp(-10) is
        small but representable; matrix sums to 1 after renorm."""
        M = _js_build_score_matrix(10.0, 10.0)
        _assert_wdl_sums_to_one(M, "λ=10")

    def test_extreme_lambda_does_not_collapse_to_zero(self):
        """FIXED (was xfail): when λ ≥ ~745 every cell underflows to 0,
        total=0, and the function used to silently return an all-zero
        matrix — every market call then returned a confident 0%. The
        Python replica now mirrors the .gs hardening: when total === 0
        the builder raises ValueError instead of pretending the matrix
        is a distribution. See test_gs_engine_throws_on_extreme_lambda
        below for the corresponding node-harness assertion proving the
        real .gs throws too."""
        with pytest.raises(ValueError, match="matrix collapsed"):
            _js_build_score_matrix(1000.0, 1000.0)

    @pytest.mark.skipif(
        NODE_BIN is None,
        reason="node not available - set WC26_NODE_BIN or install node",
    )
    def test_gs_engine_throws_on_extreme_lambda(self):
        """REAL .gs assertion: drive the actual Apps Script source under
        Node and confirm _buildScoreMatrix_(1000, 1000, ...) throws.
        Also confirms the public GOAL_GRID wrapper returns '' (the upper
        finite cap at line 1356) so a sheet cell never silently shows 0%.
        This proves the fix landed in the JavaScript, not just the Python
        replica."""
        if not GS_PATH.is_file():
            pytest.skip(f"Engine source missing at {GS_PATH}")
        # Inline JS harness — reuses the same shim/wrap strategy as
        # wc26-engine-gs/test_harness.mjs but specialised for the
        # degenerate-λ assertion.
        script = textwrap.dedent(f"""
            import fs from 'node:fs';
            import vm from 'node:vm';
            const src = fs.readFileSync({str(GS_PATH)!r}, 'utf8');
            const shimPrelude = `
              const SpreadsheetApp  = {{ getActive: () => null, getUi: () => ({{ createMenu: () => ({{ addItem: () => ({{}}), addSeparator: () => ({{}}), addToUi: () => {{}} }}) }}), ProtectionType: {{ SHEET: 'SHEET' }} }};
              const CacheService    = {{ getScriptCache: () => ({{ get: () => null, put: () => {{}} }}) }};
              const PropertiesService = {{ getScriptProperties: () => ({{ getProperty: () => null, setProperty: () => {{}}, setProperties: () => {{}} }}) }};
              const Logger          = {{ log: () => {{}} }};
              const UrlFetchApp     = {{ fetch: () => null }};
              const ScriptApp       = {{ newTrigger: () => ({{}}), getProjectTriggers: () => [], deleteTrigger: () => {{}} }};
              const Session         = {{ getEffectiveUser: () => ({{ getEmail: () => 'noop' }}) }};
              const Utilities       = {{ sleep: () => {{}}, formatDate: (d) => String(d) }};
              const HtmlService     = {{}};
              const ContentService  = {{}};
            `;
            const wrapped = `(function(){{${{shimPrelude}}${{src}};globalThis.__exp={{B:_buildScoreMatrix_,G:GOAL_GRID,M:GOAL_GRID_MAX_GOALS,R:GOAL_GRID_TAU}};}})()`;
            vm.runInThisContext(wrapped, {{ filename: 'engine.gs' }});
            const E = globalThis.__exp;
            const report = {{
              builderThrew: false,
              builderError: null,
              wrapperResult: null,
            }};
            try {{
              E.B(1000, 1000, E.M, E.R);
            }} catch (e) {{
              report.builderThrew = true;
              report.builderError = String(e && e.message ? e.message : e);
            }}
            // Public wrapper must NOT throw — the lh/la > 50 guard at
            // GOAL_GRID() short-circuits to '' before _buildScoreMatrix_.
            try {{
              report.wrapperResult = E.G(1000, 1000, 'ou25');
            }} catch (e) {{
              report.wrapperResult = 'THREW: ' + String(e && e.message ? e.message : e);
            }}
            process.stdout.write(JSON.stringify(report));
        """).strip()
        proc = subprocess.run(
            [NODE_BIN, "--input-type=module", "-e", script],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, (
            f"node harness exited {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout[:1500]}\n"
            f"--- stderr ---\n{proc.stderr[:1500]}"
        )
        import json as _json
        report = _json.loads(proc.stdout)
        assert report["builderThrew"] is True, (
            f"REAL .gs _buildScoreMatrix_(1000,1000) did NOT throw — "
            f"report={report!r}. Fix at line 1420-1430 of "
            f"WC26_Engine_AppsScript_v*.gs (latest) is missing."
        )
        assert "collapsed" in (report["builderError"] or "").lower(), (
            f"unexpected error: {report['builderError']!r}"
        )
        # Wrapper short-circuits to '' (the lh/la > 50 cap) — operator
        # sees blank cell, not a confident 0%.
        assert report["wrapperResult"] == "", (
            f"GOAL_GRID(1000, 1000, 'ou25') should return '' (the upper-"
            f"finite cap at line 1356), got: {report['wrapperResult']!r}"
        )

    def test_nan_lambda_in_inner_builder_propagates_nan(self):
        """SILENT in the inner builder: NaN λ produces a NaN matrix
        (NaN * anything = NaN, including in PMF). The PUBLIC wrapper
        GOAL_GRID() at line 1356 guards via `!isFinite(lh)` → returns ''.
        This test documents the public guard works AND the inner
        builder propagates NaN visibly (rather than e.g. clamping)."""
        M = _js_build_score_matrix(float("nan"), 1.0)
        # Inner builder: every cell NaN, sum NaN.
        s = sum(sum(r) for r in M)
        assert math.isnan(s), \
            "inner builder should propagate NaN visibly, not silently clamp"

    def test_negative_lambda_caught_by_pmf_short_circuit(self):
        """LOUD-by-construction: _poissonPmf_(lam, k) at line 1374
        returns k==0?1:0 when lam<=0. So negative λ collapses to (0,0)
        — same as λ=0. The public GOAL_GRID() also blocks negative λ
        at line 1356 with `lh < 0`. Belt-and-braces."""
        M = _js_build_score_matrix(-1.0, 1.0)
        # Inner: ph = [1, 0, 0...], so only row h=0 has mass.
        for h in range(1, MAX_G + 1):
            for a in range(MAX_G + 1):
                assert M[h][a] == 0.0
        _assert_wdl_sums_to_one(M, "λ_h=-1")


# ======================================================================
# B. ρ (Dixon-Coles correlation) EXTREMES
# ======================================================================
class TestRhoExtremes:
    def test_rho_plus_one_corner_cells_clip_then_renorm(self):
        """LOUD-by-spec: at ρ=+1 the (0,0) τ is (1 - λ_h*λ_a) which goes
        very negative for λ>1; the (1,1) τ is (1 - 1) = 0. Negative
        cells are clipped to 0 then renorm rescales — matrix still
        sums to 1, no NaN. Documents the safety net."""
        M = _js_build_score_matrix(1.5, 1.5, rho=1.0)
        _assert_wdl_sums_to_one(M, "ρ=+1")
        # (1,1) cell collapses to 0 by τ.
        assert M[1][1] == 0.0

    def test_rho_minus_one_inflates_corners_renorm_holds(self):
        """LOUD-by-spec: ρ=-1 makes (0,1) and (1,0) τ = (1 - λ) which
        is negative for λ>1, clipped to 0; (1,1) τ = 2. Matrix still
        sums to 1 after renorm."""
        M = _js_build_score_matrix(1.5, 1.5, rho=-1.0)
        _assert_wdl_sums_to_one(M, "ρ=-1")
        assert M[0][1] == 0.0
        assert M[1][0] == 0.0

    def test_rho_out_of_unit_interval_silently_accepted(self):
        """SILENT-by-design: ρ outside [-1, 1] has no theoretical
        meaning, but the math accepts it. The clip-then-renorm path
        keeps the matrix probabilistic. Documents the gap — the .gs
        does NOT validate ρ at GOAL_GRID_TAU define time (line 89)."""
        M = _js_build_score_matrix(1.5, 1.5, rho=10.0)
        _assert_wdl_sums_to_one(M, "ρ=10")


# ======================================================================
# C. max_g (matrix size) EDGE CASES
# ======================================================================
class TestMaxGoalsEdges:
    def test_max_g_zero_returns_singleton(self):
        """LOUD-by-spec: max_g=0 ⇒ 1x1 matrix = [[1.0]]. Means every
        match is 0-0 by construction. Sum is 1, draw is 1."""
        M = _js_build_score_matrix(1.5, 1.5, max_g=0)
        assert len(M) == 1 and len(M[0]) == 1
        assert M[0][0] == 1.0
        hw, dr, aw = _wdl(M)
        assert dr == 1.0 and hw == 0 and aw == 0

    def test_max_g_one_two_by_two_still_sums_to_one(self):
        """LOUD-by-spec: max_g=1 ⇒ 2x2 matrix covers only {0,1}². Most
        of the probability mass is leaked (truncated at 2-2 ceiling),
        but renorm rescales the remainder so the matrix still sums to 1.
        This is a known-and-documented behavior."""
        M = _js_build_score_matrix(1.5, 1.5, max_g=1)
        assert len(M) == 2
        s = sum(sum(r) for r in M)
        assert math.isclose(s, 1.0, abs_tol=WDL_TOL)
        _assert_wdl_sums_to_one(M, "max_g=1")

    def test_max_g_hundred_performance_and_sum(self):
        """LOUD-by-spec: max_g=100 produces a 101x101 matrix. Must
        still sum to 1 (no precision loss above 1e-6) and complete
        quickly."""
        import time
        t0 = time.perf_counter()
        M = _js_build_score_matrix(1.5, 1.5, max_g=100)
        elapsed = time.perf_counter() - t0
        assert len(M) == 101
        s = sum(sum(r) for r in M)
        assert math.isclose(s, 1.0, abs_tol=1e-6), \
            f"precision loss at max_g=100: sum={s}"
        assert elapsed < 1.0, f"max_g=100 too slow: {elapsed:.3f}s"
        _assert_wdl_sums_to_one(M, "max_g=100", tol=1e-6)


# ======================================================================
# D. RENORM SAFETY — the load-bearing invariant
# ======================================================================
class TestRenormSafety:
    def test_renorm_after_severe_tau_overcorrection(self):
        """LOUD-by-spec: at λ=5, ρ=-1, the (0,0) τ = 1+25 = 26x and
        (1,1) τ = 2x — raw sum before renorm is far above 1. The
        explicit `if (total > 0) ... /= total` at line 1420-1424 must
        renormalize correctly. Without renorm the matrix would NOT be
        a probability distribution."""
        M = _js_build_score_matrix(5.0, 5.0, rho=-1.0)
        s = sum(sum(r) for r in M)
        assert math.isclose(s, 1.0, abs_tol=WDL_TOL)
        _assert_wdl_sums_to_one(M, "renorm-overcorrect")

    def test_negative_cells_clipped_to_zero(self):
        """LOUD-by-construction: line 1416 `M[h][a] = v < 0 ? 0 : v`
        guarantees no cell goes negative even when τ produces a negative
        product. Test by inducing it: λ=10, ρ=-0.13 makes (0,0)
        τ = 1 - 10*10*(-0.13) = 14, no negative; we need ρ=+0.13
        with large λ to drive τ negative."""
        # ρ=+0.5, λ=5: τ_00 = 1 - 5*5*0.5 = -11.5 ⇒ M[0][0] would be neg
        M = _js_build_score_matrix(5.0, 5.0, rho=0.5)
        for h in range(MAX_G + 1):
            for a in range(MAX_G + 1):
                assert M[h][a] >= 0, f"M[{h}][{a}] = {M[h][a]} < 0"


# ======================================================================
# E. WDL CLOSURE INVARIANT — must hold for all valid inputs
# ======================================================================
class TestWDLInvariantSweep:
    """For every valid (λ_h, λ_a, ρ, max_g) the markets MUST satisfy
    P(home win) + P(draw) + P(away win) = 1 ± 1e-9. This is the load-
    bearing identity downstream (CLV computations rely on it)."""

    @pytest.mark.parametrize("lam_h,lam_a", [
        (0.5, 0.5),
        (1.0, 1.0),
        (1.4, 1.4),
        (1.8, 0.9),
        (0.9, 1.8),
        (2.5, 0.3),
        (0.3, 2.5),
        (3.0, 3.0),
        (5.0, 5.0),
        (7.0, 7.0),   # at λ_clip boundary
    ])
    def test_wdl_sums_to_one(self, lam_h, lam_a):
        M = _js_build_score_matrix(lam_h, lam_a)
        _assert_wdl_sums_to_one(M, f"λ=({lam_h},{lam_a})")

    @pytest.mark.parametrize("rho", [-0.5, -0.13, -0.05, 0.0, 0.05, 0.13])
    def test_wdl_sums_to_one_over_rho(self, rho):
        M = _js_build_score_matrix(1.8, 0.9, rho=rho)
        _assert_wdl_sums_to_one(M, f"ρ={rho}")

    def test_market_complement_identity(self):
        """LOUD-by-spec: P(home win) + P(home does not win) = 1.
        Equivalently P(ah0) + (P(draw) + P(away win)) = 1."""
        M = _js_build_score_matrix(1.8, 0.9)
        ah0 = _market(M, lambda h, a: h > a)
        not_h = _market(M, lambda h, a: h <= a)
        assert math.isclose(ah0 + not_h, 1.0, abs_tol=WDL_TOL)

    def test_ou_complementary_pairs(self):
        """LOUD-by-spec: P(over 2.5) + P(under 2.5) = 1."""
        M = _js_build_score_matrix(1.8, 0.9)
        over = _market(M, lambda h, a: (h + a) > 2.5)
        under = _market(M, lambda h, a: (h + a) <= 2.5)
        assert math.isclose(over + under, 1.0, abs_tol=WDL_TOL)


# ======================================================================
# F. _dcTau_ branch invariants
# ======================================================================
class TestDCTauInvariants:
    def test_tau_default_branch_returns_one(self):
        """LOUD-by-construction: every (h,a) outside the four low-score
        cells returns τ = 1. Test all (h,a) with h>=2 or a>=2."""
        for h in range(2, 5):
            for a in range(2, 5):
                assert _js_dc_tau(h, a, 1.5, 1.5, -0.13) == 1.0

    def test_tau_corner_cell_signs_at_negative_rho(self):
        """LOUD-by-spec: at ρ<0 (production default -0.13) DC inflates
        the (0,0) and (1,1) cells, deflates the (0,1) and (1,0) cells.
        Sign-check pins the convention."""
        lh, la, rho = 1.5, 1.5, -0.13
        assert _js_dc_tau(0, 0, lh, la, rho) > 1.0  # inflated
        assert _js_dc_tau(1, 1, lh, la, rho) > 1.0  # inflated
        assert _js_dc_tau(0, 1, lh, la, rho) < 1.0  # deflated
        assert _js_dc_tau(1, 0, lh, la, rho) < 1.0  # deflated

    def test_tau_corner_cell_signs_flip_at_positive_rho(self):
        """LOUD-by-spec: a positive ρ would invert all four cell
        adjustments. Catches a stray sign flip in the constant."""
        lh, la, rho = 1.5, 1.5, +0.13
        assert _js_dc_tau(0, 0, lh, la, rho) < 1.0
        assert _js_dc_tau(1, 1, lh, la, rho) < 1.0
        assert _js_dc_tau(0, 1, lh, la, rho) > 1.0
        assert _js_dc_tau(1, 0, lh, la, rho) > 1.0


# ======================================================================
# G. _poissonPmf_ edge invariants
# ======================================================================
class TestPoissonPMFInvariants:
    def test_pmf_at_lambda_zero(self):
        """LOUD-by-construction: line 1374 short-circuit."""
        assert _js_poisson_pmf(0.0, 0) == 1.0
        assert _js_poisson_pmf(0.0, 5) == 0.0

    def test_pmf_at_negative_lambda_short_circuit(self):
        assert _js_poisson_pmf(-1.0, 0) == 1.0
        assert _js_poisson_pmf(-1.0, 3) == 0.0

    def test_pmf_marginal_sums_to_one(self):
        """LOUD-by-spec: marginal Poisson over 0..MAX_GOALS should sum
        very close to 1 for sane λ (truncation tail ~3e-6 at λ=1.8,
        MAX_G=10). This is the exact reason _buildScoreMatrix_ has
        an explicit renorm step — the truncated tail leaks mass."""
        s = sum(_js_poisson_pmf(1.8, k) for k in range(MAX_G + 1))
        # Truncation tail at λ=1.8, k=11..∞ ≈ 3.1e-6 — by design,
        # renormalized away in _buildScoreMatrix_.
        assert math.isclose(s, 1.0, abs_tol=1e-5)
        # And the leak is small enough that it's < 0.001% of mass.
        assert (1.0 - s) < 1e-5
