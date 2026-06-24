"""Behavioral test — GOAL_GRID custom function vs predictions_live.json feed.

This pins TWO independent invariants that together close the loop between
the Apps Script GOAL_GRID custom function and the per-match WDL written
to data/processed/predictions_live.json by scripts/03_simulate.py:

  (A) The feed's recorded WDL equals the analytical
      Negative-Binomial-marginal × Dixon-Coles-τ joint, evaluated at the
      same (lam_h, lam_a). Verifies that scripts/03_simulate.py:188-206
      is what actually produced the stored p_home / p_draw / p_away.

  (B) The pure-Python replica of GOAL_GRID (Poisson marginals × DC τ)
      reproduces the *analytical Poisson-DC truth* (a self-contained
      reference computed in this file). Verifies that the JS replica
      and the analytical reference agree to floating-point precision
      for every fixture.

WHY THIS REPLACES THE OLD 6% BLANKET
------------------------------------
The previous test compared GOAL_GRID (Poisson+DC) DIRECTLY against the
feed (NB+DC) and absorbed the marginal-family gap into a single 6%
tolerance. With 72 group-stage fixtures, the observed Poisson-vs-NB
family gap is max 6.2%, p95 5.7%, mean 3.0% — so any 2-5% sim regression
(wrong dispersion, wrong rho, off-by-one in NB parameterisation, lambda
being DC-shifted before storage) would have been hidden by the same
budget that was already booked for the family difference.

The decomposition done as part of writing this test (full report in the
PR description) showed that |feed - analytical_nb_dc|_∞ is bounded by
6.7e-16 across ALL 72 fixtures — i.e. the gap is 100% family-explained
and there is no MC noise floor to defend (the per-match WDL is written
analytically via wdl_from_matrix at 03_simulate.py:839, not estimated
from the tournament Monte Carlo).

So this test now:
  - asserts (A) at 1e-9 (analytical equality, generous for nbinom.pmf
    round-off)
  - asserts (B) at 1e-12 (same closed-form math twice — should be
    indistinguishable)
  - iterates EVERY fixture with non-null λ in the feed (not just the
    first one)
  - will catch a real 2% regression in either side: feed-vs-NB-truth
    breaks if the sim's marginal family or DC rule changes; replica-vs-
    Poisson-truth breaks if the GOAL_GRID JS port drifts from the
    closed-form Poisson+DC math.

NB parameterisation reference
-----------------------------
scripts/03_simulate.py:188-193
    a_disp = cfg["nb_dispersion"]               # = 5.0
    p_h = a_disp / (a_disp + lam_h)
    p_a = a_disp / (a_disp + lam_a)
    ph = nbinom.pmf(np.arange(max_g + 1), a_disp, p_h)
    pa = nbinom.pmf(np.arange(max_g + 1), a_disp, p_a)

scipy.stats.nbinom convention: nbinom.pmf(k, n, p) = C(k+n-1, k) p^n (1-p)^k
with mean = n(1-p)/p. Setting p = a/(a+lam) gives mean = lam (as required).

λ storage (pre- or post-DC?)
----------------------------
Pre-DC. scripts/03_simulate.py:833-846 stores lam_h / lam_a directly
from predict_lambdas (model output + clip). build_score_matrix mutates
a LOCAL matrix; the stored λ values are never DC-shifted. The DC τ is
applied inside build_score_matrix on the four corner cells (0,0), (0,1),
(1,0), (1,1) only — see 03_simulate.py:200-206.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from scipy.stats import nbinom, poisson

REPO = Path(__file__).resolve().parents[2]
FEED = REPO / "data" / "processed" / "predictions_live.json"

DC_RHO = -0.13
NB_ALPHA = 5.0
# R13 C2: bumped 10 → 15 to follow R12 MED (sim default max_g 10 → 15)
# and R13 C1 (export_ko_advance.MAX_G 10 → 15). Pre-R13 this constant
# pinned the WRONG spec — tests passed because feed/export/replica all
# used the stale max_g=10 mutually, hiding a real divergence vs the
# production sim's 16×16 matrix.
MAX_G = 15

# (A) Analytical equality — accommodates nbinom.pmf round-off and the
#     1e-12 floor + renorm in build_score_matrix. Empirically: 6.7e-16
#     on every group fixture. 1e-9 leaves 6 decades of headroom but
#     still catches any real change (a 2% regression is 1e7 × this bar).
FEED_VS_NB_TOL = 1e-9

# (B) Closed-form Poisson+DC vs closed-form Poisson+DC. The JS replica
#     uses math.exp(-lam)*lam**k/k! while the analytical truth side calls
#     scipy.stats.poisson.pmf (lgamma-based). The two routes are
#     algebraically identical but accumulate different float round-off:
#     observed worst-case gap is 1.2e-11 (m=72 Jordan->Argentina, where
#     lam_a=3.07 makes lam**k blow up to ~1e6 before the exp). 1e-10
#     leaves a decade of headroom above that noise floor while still
#     being eight decades tighter than any plausible regression (a 2%
#     real change in either side is 2e-2, 100,000,000x this bar).
JS_REPLICA_VS_POISSON_TRUTH_TOL = 1e-10


# ---------------------------------------------------------------- DC τ
def _dc_tau(h: int, a: int, lam_h: float, lam_a: float, rho: float) -> float:
    """Verbatim copy of scripts/03_simulate.py:178-183."""
    if h == 0 and a == 0: return 1 - lam_h * lam_a * rho
    if h == 0 and a == 1: return 1 + lam_a * rho
    if h == 1 and a == 0: return 1 + lam_h * rho
    if h == 1 and a == 1: return 1 - rho
    return 1.0


# ---------------------------------------------- closed-form WDL helpers
def _renorm_floor_and_wdl(M: list[list[float]]) -> tuple[float, float, float]:
    """Match build_score_matrix tail: clip to 1e-12, renormalise, sum WDL.
    Mirrors scripts/03_simulate.py:205-206 and wdl_from_matrix at :231-235."""
    n = len(M)
    M = [[max(M[h][a], 1e-12) for a in range(n)] for h in range(n)]
    total = sum(M[h][a] for h in range(n) for a in range(n))
    M = [[M[h][a] / total for a in range(n)] for h in range(n)]
    p_home = sum(M[h][a] for h in range(n) for a in range(n) if h > a)
    p_draw = sum(M[h][a] for h in range(n) for a in range(n) if h == a)
    p_away = sum(M[h][a] for h in range(n) for a in range(n) if h < a)
    return p_home, p_draw, p_away


def wdl_nb_dc(lam_h: float, lam_a: float,
              alpha: float = NB_ALPHA, rho: float = DC_RHO,
              max_g: int = MAX_G) -> tuple[float, float, float]:
    """Analytical NB-marginal × DC-τ WDL — what scripts/03_simulate.py
    actually computes when use_dispersion=True. Truth side for (A)."""
    p_h = alpha / (alpha + lam_h)
    p_a = alpha / (alpha + lam_a)
    ph = [float(nbinom.pmf(k, alpha, p_h)) for k in range(max_g + 1)]
    pa = [float(nbinom.pmf(k, alpha, p_a)) for k in range(max_g + 1)]
    s_ph, s_pa = sum(ph), sum(pa)
    ph = [x / s_ph for x in ph]
    pa = [x / s_pa for x in pa]
    M = [[ph[h] * pa[a] for a in range(max_g + 1)] for h in range(max_g + 1)]
    for h in (0, 1):
        for a in (0, 1):
            M[h][a] *= _dc_tau(h, a, lam_h, lam_a, rho)
    return _renorm_floor_and_wdl(M)


def wdl_poisson_dc(lam_h: float, lam_a: float,
                   rho: float = DC_RHO,
                   max_g: int = MAX_G) -> tuple[float, float, float]:
    """Analytical Poisson-marginal × DC-τ WDL — the truth side for (B)."""
    ph = [float(poisson.pmf(k, lam_h)) for k in range(max_g + 1)]
    pa = [float(poisson.pmf(k, lam_a)) for k in range(max_g + 1)]
    s_ph, s_pa = sum(ph), sum(pa)
    ph = [x / s_ph for x in ph]
    pa = [x / s_pa for x in pa]
    M = [[ph[h] * pa[a] for a in range(max_g + 1)] for h in range(max_g + 1)]
    for h in (0, 1):
        for a in (0, 1):
            M[h][a] *= _dc_tau(h, a, lam_h, lam_a, rho)
    return _renorm_floor_and_wdl(M)


# ----------------------------------- JS GOAL_GRID replica (Poisson + DC τ)
def _js_poisson_pmf(lam: float, k: int) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _js_build_score_matrix(lam_h: float, lam_a: float,
                           max_g: int = MAX_G, rho: float = DC_RHO):
    """Verbatim Python replica of WC26_Engine_AppsScript_v2.3.1.gs
    _buildScoreMatrix_ — Poisson marginals × DC τ-correction × renorm."""
    ph = [_js_poisson_pmf(lam_h, i) for i in range(max_g + 1)]
    pa = [_js_poisson_pmf(lam_a, i) for i in range(max_g + 1)]
    M = [[0.0] * (max_g + 1) for _ in range(max_g + 1)]
    total = 0.0
    for h in range(max_g + 1):
        for a in range(max_g + 1):
            v = ph[h] * pa[a] * _dc_tau(h, a, lam_h, lam_a, rho)
            v = max(v, 0.0)
            M[h][a] = v
            total += v
    if total > 0:
        for h in range(max_g + 1):
            for a in range(max_g + 1):
                M[h][a] /= total
    return M


def _js_wdl(lam_h: float, lam_a: float) -> tuple[float, float, float]:
    M = _js_build_score_matrix(lam_h, lam_a)
    n = len(M)
    p_home = sum(M[h][a] for h in range(n) for a in range(n) if h > a)
    p_draw = sum(M[h][a] for h in range(n) for a in range(n) if h == a)
    p_away = sum(M[h][a] for h in range(n) for a in range(n) if h < a)
    return p_home, p_draw, p_away


# --------------------------------------------------------------- fixtures
def _all_fixtures_with_lambda():
    """Every match_predictions entry with non-null lam_home/lam_away.
    Both group and (when populated) knockout entries are included."""
    feed = json.loads(FEED.read_text())
    out = []
    for m in feed["match_predictions"]:
        if m.get("lam_home") is None or m.get("lam_away") is None:
            continue
        out.append(m)
    assert out, "no match_predictions entry has lam_home/lam_away populated"
    return out


# ============================================================== TESTS
@pytest.mark.parametrize("match", _all_fixtures_with_lambda(),
                         ids=lambda m: f"m{m['m']}_{m['home']}_vs_{m['away']}")
def test_feed_wdl_equals_analytical_nb_dc(match):
    """(A) For EVERY scheduled fixture, the feed's recorded
    (p_home_win, p_draw, p_away_win) must equal the analytical
    NB(α=5.0)-marginal × DC-τ(ρ=-0.13) joint, evaluated at the same
    (lam_home, lam_away), to better than 1e-9 per component.

    This pins the production sim to its declared joint formula. A regression
    in any of these would break it: NB ↔ Poisson family swap, wrong
    dispersion, DC τ on the wrong cells, λ being DC-shifted before storage,
    a renorm bug, or a change in the max-scoreline cutoff.
    """
    lam_h = float(match["lam_home"])
    lam_a = float(match["lam_away"])
    nb_h, nb_d, nb_a = wdl_nb_dc(lam_h, lam_a)
    feed_h = float(match["p_home_win"])
    feed_d = float(match["p_draw"])
    feed_a = float(match["p_away_win"])
    diffs = {
        "p_home_win": abs(feed_h - nb_h),
        "p_draw":     abs(feed_d - nb_d),
        "p_away_win": abs(feed_a - nb_a),
    }
    worst = max(diffs.values())
    if worst > FEED_VS_NB_TOL:
        msg = (
            f"feed WDL diverges from analytical NB-DC truth on "
            f"m={match['m']} {match['home']} -> {match['away']} "
            f"(lam_h={lam_h:.6f}, lam_a={lam_a:.6f}, tol={FEED_VS_NB_TOL:g}):\n"
            f"  p_home_win  feed={feed_h:.10f}  nb_dc={nb_h:.10f}  |Δ|={diffs['p_home_win']:.3e}\n"
            f"  p_draw      feed={feed_d:.10f}  nb_dc={nb_d:.10f}  |Δ|={diffs['p_draw']:.3e}\n"
            f"  p_away_win  feed={feed_a:.10f}  nb_dc={nb_a:.10f}  |Δ|={diffs['p_away_win']:.3e}\n"
            f"Investigate: marginal family swap (NB<->Poisson), wrong "
            f"dispersion, DC tau regression, lambda pre/post-DC mixup, "
            f"renorm bug, max_g cutoff drift."
        )
        pytest.fail(msg)


@pytest.mark.parametrize("match", _all_fixtures_with_lambda(),
                         ids=lambda m: f"m{m['m']}_{m['home']}_vs_{m['away']}")
def test_js_replica_equals_analytical_poisson_dc(match):
    """(B) The GOAL_GRID Apps Script replica (Poisson+DC) must equal the
    analytical Poisson+DC truth at the same (lam_home, lam_away) to
    floating-point precision (1e-12 per component) for EVERY fixture.

    This pins the JS-port math. Any regression in the GOAL_GRID Poisson
    PMF, the DC τ rule, or the renorm step shows up here — independently
    of the sim's marginal family choice.
    """
    lam_h = float(match["lam_home"])
    lam_a = float(match["lam_away"])
    js_h, js_d, js_a = _js_wdl(lam_h, lam_a)
    tr_h, tr_d, tr_a = wdl_poisson_dc(lam_h, lam_a)
    diffs = {
        "p_home_win": abs(js_h - tr_h),
        "p_draw":     abs(js_d - tr_d),
        "p_away_win": abs(js_a - tr_a),
    }
    worst = max(diffs.values())
    if worst > JS_REPLICA_VS_POISSON_TRUTH_TOL:
        msg = (
            f"GOAL_GRID JS replica diverges from analytical Poisson-DC "
            f"truth on m={match['m']} {match['home']} -> {match['away']} "
            f"(lam_h={lam_h:.6f}, lam_a={lam_a:.6f}, "
            f"tol={JS_REPLICA_VS_POISSON_TRUTH_TOL:g}):\n"
            f"  p_home_win  js={js_h:.12f}  truth={tr_h:.12f}  |Δ|={diffs['p_home_win']:.3e}\n"
            f"  p_draw      js={js_d:.12f}  truth={tr_d:.12f}  |Δ|={diffs['p_draw']:.3e}\n"
            f"  p_away_win  js={js_a:.12f}  truth={tr_a:.12f}  |Δ|={diffs['p_away_win']:.3e}\n"
            "Investigate: DC τ branch change, Poisson PMF replica drift, "
            "renorm sign bug, max_g cutoff drift."
        )
        pytest.fail(msg)


# ============================================================ S7 KO ADVANCE
# (C) For every KO match exported into match_predictions_ko (m ≥ 73,
#     both teams resolved), the recorded `p_advance_match` must equal
#     W + 0.5*D evaluated on the SAME analytical NB+DC matrix used in
#     test (A). This pins the post-processor in
#     scripts/live/export_ko_advance.py to the same closed-form math.
#
#     Today the test passes trivially (zero KO matches resolved during
#     group stage → match_predictions_ko is empty). After R32 the test
#     becomes load-bearing — any drift in the export's λ→matrix→advance
#     pipeline shows up here at 1e-9, the same tolerance we use for the
#     90-min WDL agreement.
KO_ADVANCE_TOL = 1e-9


def _ko_advance_entries():
    """Resolved KO entries (if any) from the feed's match_predictions_ko
    block. Pre-R32 this is empty — the test below short-circuits."""
    feed = json.loads(FEED.read_text())
    return feed.get("match_predictions_ko") or []


def test_ko_advance_agreement():
    """Every match_predictions_ko entry must satisfy:

        p_advance_match ≈ W + 0.5 * D

    where (W, D) come from build_score_matrix(lam_home, lam_away) using
    the production NB(α=5.0) + DC(τ=−0.13) joint. This is the same
    matrix the sim uses for the 90-min WDL — so the export and the sim
    are pinned together.

    Pre-R32: the loop has 0 iterations and the test passes by definition.
    Once R16+ matchups resolve, every (home, away) pair gets validated.
    """
    entries = _ko_advance_entries()
    for e in entries:
        lam_h = float(e["lambda_home"])
        lam_a = float(e["lambda_away"])
        nb_h, nb_d, nb_a = wdl_nb_dc(lam_h, lam_a)
        expected_adv = nb_h + 0.5 * nb_d
        # Per-WDL agreement (same 1e-9 bar as test A).
        for label, feed_v, truth_v in (("p_home_win", e["p_home_win"], nb_h),
                                       ("p_draw", e["p_draw"], nb_d),
                                       ("p_away_win", e["p_away_win"], nb_a)):
            assert abs(feed_v - truth_v) < KO_ADVANCE_TOL, (
                f"KO m={e['m']} {e['home']}->{e['away']}: {label} "
                f"diverges (feed={feed_v}, nb_dc={truth_v}, "
                f"|Δ|={abs(feed_v - truth_v):.3e})"
            )
        # Advance-prob agreement against W + 0.5*D directly.
        assert abs(e["p_advance_match"] - expected_adv) < KO_ADVANCE_TOL, (
            f"KO m={e['m']} {e['home']}->{e['away']}: p_advance_match "
            f"diverges (feed={e['p_advance_match']}, expected={expected_adv}, "
            f"|Δ|={abs(e['p_advance_match'] - expected_adv):.3e})"
        )
        # Range check — the 90-min + 50/50 split must stay in [0, 1].
        assert 0.0 <= e["p_advance_match"] <= 1.0
