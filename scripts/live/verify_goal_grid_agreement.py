"""Verify GOAL_GRID Apps Script custom function (Poisson + DC τ) agrees
with per-match market probabilities in data/processed/predictions_live.json
when fed the SAME λ_home/λ_away.

Background
----------
- The production sim (scripts/03_simulate.py:build_score_matrix) uses
  Negative Binomial marginals by default (use_dispersion=True,
  nb_dispersion=5.0) + Dixon-Coles τ correction.
- GOAL_GRID in wc26-engine-gs/WC26_Engine_AppsScript_v2.3.1.gs uses
  Poisson marginals + the SAME DC τ correction (ρ=-0.13, MAX_G=10).
- Both consume the SAME λ_home/λ_away. The expected systematic gap is
  the NB vs Poisson marginal difference (NB is overdispersed → more
  mass in the right tail → marginally higher over25/over35).

What this script does
---------------------
1. Loads data/processed/predictions_live.json.
2. Picks 10 representative MATCHES (sorted by λ_home + λ_away, spanning
   low / medium / high totals) whose result is not yet locked.
3. For each match, replicates the JS _buildScoreMatrix_ in Python
   (verbatim copy of tests/live/test_goal_grid.py._js_build_score_matrix)
   using Poisson marginals + DC τ.
4. Computes JS-replica market probs: over25, over15, over35, btts,
   home (ah0), draw, away.
5. Compares to the feed's per-match probs. The FEED only carries
   p_home_win / p_draw / p_away — over/btts/ah0 are NOT in the feed,
   so the agreement check applies to the WDL markets.
6. Prints a table + verdict.

λ pre/post DC
-------------
scripts/03_simulate.py line 833-846 stores lam_h / lam_a directly from
predict_lambdas (the goal model output, clipped). build_score_matrix
applies τ AFTER, on a temporary matrix. The feed's λ values are the
PRE-DC marginal Poisson/NB means.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FEED = REPO / "data" / "processed" / "predictions_live.json"

DC_RHO = -0.13
MAX_G = 10
TOLERANCE = 0.01   # 1% reporting tolerance for PASS/FAIL column
N_SAMPLE = 10


# ---- JS _buildScoreMatrix_ replica (verbatim from tests/live/test_goal_grid.py)
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
    return M


def _market(M, pred):
    n = len(M)
    return sum(M[h][a] for h in range(n) for a in range(n) if pred(h, a))


def grid_market_probs(lam_h, lam_a):
    M = _js_build_score_matrix(lam_h, lam_a)
    return {
        "p_home_win": _market(M, lambda h, a: h > a),
        "p_draw":     _market(M, lambda h, a: h == a),
        "p_away_win": _market(M, lambda h, a: h < a),
        "over15":     _market(M, lambda h, a: h + a > 1.5),
        "over25":     _market(M, lambda h, a: h + a > 2.5),
        "over35":     _market(M, lambda h, a: h + a > 3.5),
        "btts":       _market(M, lambda h, a: h > 0 and a > 0),
    }


def pick_sample(matches: list[dict], n: int = N_SAMPLE) -> list[dict]:
    """Pick n matches with non-null λ and no locked score, spanning the
    full range of λ_home + λ_away (low → medium → high)."""
    candidates = [
        m for m in matches
        if m.get("lam_home") is not None
        and m.get("lam_away") is not None
        and m.get("locked_score") is None
    ]
    candidates.sort(key=lambda m: m["lam_home"] + m["lam_away"])
    if len(candidates) <= n:
        return candidates
    # Even stride across the sorted list to span the λ-total range.
    step = (len(candidates) - 1) / (n - 1)
    return [candidates[round(i * step)] for i in range(n)]


def main():
    feed = json.loads(FEED.read_text())
    matches = feed["match_predictions"]
    cfg = feed.get("config", {})
    sample = pick_sample(matches, N_SAMPLE)

    print(f"Feed: {FEED}")
    print(f"  generated_at: {feed.get('generated_at')}")
    print(f"  config.use_dispersion: {cfg.get('use_dispersion')}  "
          f"(True → NB marginals; GOAL_GRID uses Poisson)")
    print(f"  config.nb_dispersion: {cfg.get('nb_dispersion')}")
    print(f"  config.dc_rho:        {cfg.get('dc_rho')}  "
          f"(GOAL_GRID hardcodes {DC_RHO})")
    print()
    print(f"λ pre/post-DC verdict: PRE-DC marginals.")
    print(f"  Evidence: scripts/03_simulate.py:833-846 — lam_h, lam_a are")
    print(f"  the direct output of predict_lambdas (model + clip), stored")
    print(f"  into the per-match dict BEFORE build_score_matrix applies τ.")
    print(f"  build_score_matrix mutates a LOCAL matrix; the stored λ values")
    print(f"  are never DC-shifted.")
    print()

    # ---- per-match table for available WDL markets in the feed
    feed_markets = ("p_home_win", "p_draw", "p_away_win")
    grid_only_markets = ("over15", "over25", "over35", "btts")

    print("=" * 132)
    print(f"{'#':<4}{'home → away':<32}{'λ_h':>6}{'λ_a':>6}  "
          f"{'market':<11}{'model':>9}{'grid':>9}{'|Δ|':>9}  {'verdict':<6}")
    print("-" * 132)

    all_deltas = []  # (match_label, market, delta)
    fails_in_tol = 0
    total_compared = 0

    for i, m in enumerate(sample, 1):
        lam_h = m["lam_home"]; lam_a = m["lam_away"]
        gp = grid_market_probs(lam_h, lam_a)
        label = f"{m['home']} → {m['away']}"
        first = True
        for mk in feed_markets:
            model_p = m[mk]
            grid_p = gp[mk]
            d = abs(model_p - grid_p)
            all_deltas.append((label, mk, d))
            total_compared += 1
            if d > TOLERANCE:
                fails_in_tol += 1
            verdict = "PASS" if d <= TOLERANCE else "FAIL"
            prefix = (f"{i:<4}{label[:30]:<32}{lam_h:>6.2f}{lam_a:>6.2f}  "
                      if first else f"{'':<4}{'':<32}{'':>6}{'':>6}  ")
            print(f"{prefix}{mk:<11}{model_p:>9.4f}{grid_p:>9.4f}{d:>9.4f}  {verdict:<6}")
            first = False
        # Show grid-only over/btts probs (no feed counterpart) for context.
        for mk in grid_only_markets:
            grid_p = gp[mk]
            print(f"{'':<4}{'':<32}{'':>6}{'':>6}  "
                  f"{mk:<11}{'—':>9}{grid_p:>9.4f}{'—':>9}  {'(n/a)':<6}")
        print("-" * 132)

    # ---- summary
    if all_deltas:
        worst = max(all_deltas, key=lambda t: t[2])
        max_d = worst[2]
        wdl_deltas = [t for t in all_deltas]
        max_by_market = {}
        for label, mk, d in all_deltas:
            if d > max_by_market.get(mk, (None, -1))[1]:
                max_by_market[mk] = (label, d)
    else:
        max_d = 0.0; worst = ("-", "-", 0.0); max_by_market = {}

    print()
    print("SUMMARY")
    print(f"  matches sampled:    {len(sample)}")
    print(f"  market comparisons: {total_compared} (3 WDL markets per match)")
    print(f"  within ±{TOLERANCE:.0%}:      {total_compared - fails_in_tol} / {total_compared}")
    print(f"  max |Δ|:            {max_d:.4f}")
    print(f"  worst case:         {worst[0]}  market={worst[1]}  |Δ|={worst[2]:.4f}")
    print()
    print("  max |Δ| per market:")
    for mk in feed_markets:
        if mk in max_by_market:
            lbl, d = max_by_market[mk]
            print(f"    {mk:<12} {d:.4f}   ({lbl})")
    print()
    print("NOTE: over25 / over35 / btts cannot be compared against the feed —")
    print("the feed does not export those markets per match. Their values in")
    print("the table above come from the Poisson+DC grid only and are shown")
    print("so the human can eyeball them against intuition.")
    print()
    if max_d <= TOLERANCE:
        print(f"VERDICT: PASS — all sampled WDL probabilities agree within ±{TOLERANCE:.0%}.")
    elif max_d <= 0.025:
        print(f"VERDICT: SOFT PASS — max gap {max_d:.4f} > {TOLERANCE:.0%} but ≤ 2.5%; "
              f"consistent with NB vs Poisson marginal difference.")
    else:
        print(f"VERDICT: FAIL — max gap {max_d:.4f} > 2.5%. "
              f"Larger than the expected NB-vs-Poisson systematic.")


if __name__ == "__main__":
    main()
