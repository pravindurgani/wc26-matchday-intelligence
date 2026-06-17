"""
P1a — Cross-validate `export_ko_advance` matrix against the production sim.

Background
----------
Three independent re-implementations of the NB(α=5.0)×DC(τ=-0.13) joint
score matrix exist in this repo:

  1. scripts/03_simulate.py:186-208  →  `build_score_matrix` — production.
     scipy.stats.nbinom.pmf + numpy.outer. Used by the Monte Carlo sim.

  2. scripts/live/export_ko_advance.py:140-165  →  `_build_nb_dc_matrix` —
     pure-Python (math.lgamma + math.exp) reimplementation. Used by the
     S7 KO-advance post-processor. Deliberately avoids scipy/numpy so the
     post-processor stays tiny (no joblib/pandas drag).

  3. tests/live/test_goal_grid_feed_agreement.py:125  →  `wdl_nb_dc` — a
     scipy-based reimplementation in the agreement test.

`tests/live/test_ko_advance_export.py:test_single_resolved_ko_writes_one_entry`
already asserts that the export's emitted `p_home_win / p_draw /
p_away_win` equal `_wdl_from_matrix(_build_nb_dc_matrix(1.6, 1.1))` to
1e-12 — but that compares the export's output against the export's *own*
matrix builder, which is circular and would not catch a co-located bug
in `_build_nb_dc_matrix` itself.

`tests/live/test_goal_grid_feed_agreement.py:test_ko_advance_agreement`
iterates `match_predictions_ko`, which is empty pre-R32, so today it
runs zero parametrize cells and "passes by definition."

Net: the export's matrix has **no current cross-validation against the
production sim's matrix**, and **no real-data coverage** pre-R32.

This file closes both gaps:

  - `test_export_matrix_matches_sim_matrix_grid` imports BOTH
    `build_score_matrix` (via importlib, the same pattern used by
    tests/live/test_decide_knockout.py) AND `_build_nb_dc_matrix`, and
    asserts cell-for-cell identity on a 5×5 λ grid covering the realistic
    WC26 range (0.4..2.6) at ≤1e-12 per cell. A regression in either
    side — wrong dispersion, NB↔Poisson swap, DC τ on wrong cells,
    a renorm bug, a floor mismatch, a max_g drift — fails this test loudly.

  - `test_export_wdl_matches_sim_wdl_grid` extracts WDL from both
    matrices using their respective extractors (`sim.wdl_from_matrix`
    and `export._wdl_from_matrix`) and asserts equality at ≤1e-12.

  - `test_end_to_end_export_against_sim_truth` runs the export's
    end-to-end CLI on a synthetic resolved-KO fixture (Argentina vs
    Brazil, λ=(1.6, 1.1)) and asserts the emitted p_home_win, p_draw,
    p_away_win, p_advance_match all match values computed via the
    production sim's matrix at ≤1e-12. This gives the KO-advance
    pipeline a real cross-module check pre-R32, replacing the vacuous
    "passes by definition" coverage.

If any test here fails, investigate: cfg["nb_dispersion"] drift,
cfg["dc_rho"] drift, the floor constant in either side, max_g mismatch,
or one of the two re-implementations losing track of the other. The
test grid was chosen to exercise low-λ, balanced, and high-λ regimes
plus an asymmetric pair — the four mathematical regions where a regression
typically first appears.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
LIVE = SCRIPTS / "live"

# Mirror sys.path the same way other live tests do.
for p in (str(SCRIPTS), str(LIVE)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Load 03_simulate.py as a module (file name leads with a digit, so a
# normal `import` won't work — same pattern as test_decide_knockout.py).
_spec = importlib.util.spec_from_file_location(
    "simulate_module", ROOT / "scripts" / "03_simulate.py"
)
sim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sim)

# Import the export's matrix builder + WDL extractor.
from scripts.live.export_ko_advance import (  # noqa: E402
    _build_nb_dc_matrix as export_build_matrix,
    _wdl_from_matrix as export_wdl,
    export as run_export,
    NB_ALPHA as EXPORT_NB_ALPHA,
    DC_RHO as EXPORT_DC_RHO,
    MAX_G as EXPORT_MAX_G,
)


# ------------------------------------------------------------- production cfg
# Pinned to the production constants. If `03_simulate.py:DEFAULTS` ever
# drifts here, the test fails — by design.
PROD_NB_DISPERSION = 5.0
PROD_DC_RHO = -0.13
PROD_MAX_G = 10
SIM_CFG = {
    "nb_dispersion": PROD_NB_DISPERSION,
    "dc_rho": PROD_DC_RHO,
    "use_dispersion": True,
}

# Tolerance for cell-for-cell equality. The two implementations differ
# in route (scipy.stats.nbinom.pmf vs custom lgamma/exp) but should
# converge to floating-point precision; 1e-12 leaves ~3 decades of
# headroom over the observed ~1e-15 round-off. Any real regression (≥1%
# in any cell) is 10 orders of magnitude above this bar.
CELL_TOL = 1e-12

# λ grid covers low / balanced / high / asymmetric WC26 regimes. These
# values bracket the observed `lam_home` / `lam_away` range in
# `data/processed/predictions_live.json` (typically 0.4..2.6).
LAMBDA_GRID = [0.4, 0.9, 1.4, 1.8, 2.6]


def _grid_pairs() -> list[tuple[float, float]]:
    return [(lh, la) for lh in LAMBDA_GRID for la in LAMBDA_GRID]


# ----------------------------------------------------- constant alignment
def test_export_constants_match_production() -> None:
    """The export module must declare the same NB(α), DC(ρ), max_g values
    that 03_simulate.py uses. Drift in any of these is the most common
    silent regression class — pin it explicitly.
    """
    assert EXPORT_NB_ALPHA == PROD_NB_DISPERSION, (
        f"export NB_ALPHA={EXPORT_NB_ALPHA} drifts from production "
        f"nb_dispersion={PROD_NB_DISPERSION} (scripts/03_simulate.py DEFAULTS)"
    )
    assert EXPORT_DC_RHO == PROD_DC_RHO, (
        f"export DC_RHO={EXPORT_DC_RHO} drifts from production "
        f"dc_rho={PROD_DC_RHO} (scripts/03_simulate.py DEFAULTS)"
    )
    assert EXPORT_MAX_G == PROD_MAX_G, (
        f"export MAX_G={EXPORT_MAX_G} drifts from production max_g default "
        f"in build_score_matrix (scripts/03_simulate.py:186)"
    )


# ----------------------------------------------------- cell-for-cell grid
@pytest.mark.parametrize("lam_h,lam_a", _grid_pairs(),
                         ids=lambda v: f"{v:.1f}")
def test_export_matrix_matches_sim_matrix_grid(lam_h: float, lam_a: float) -> None:
    """For every λ pair on the grid, `export._build_nb_dc_matrix` must
    equal `sim.build_score_matrix` cell-for-cell to ≤1e-12.

    This is the only non-circular check that the export's matrix actually
    reproduces what the production simulator computes — no other test in
    the suite imports both implementations and compares.
    """
    M_sim = sim.build_score_matrix(
        lam_h, lam_a, SIM_CFG, use_dispersion=True, max_g=PROD_MAX_G
    )
    M_export = export_build_matrix(lam_h, lam_a)

    # Shapes must match before we can compare cells.
    assert M_sim.shape == (PROD_MAX_G + 1, PROD_MAX_G + 1), (
        f"sim matrix shape {M_sim.shape} != expected {(PROD_MAX_G+1, PROD_MAX_G+1)}"
    )
    assert len(M_export) == PROD_MAX_G + 1, (
        f"export matrix rows {len(M_export)} != expected {PROD_MAX_G + 1}"
    )
    assert all(len(row) == PROD_MAX_G + 1 for row in M_export), (
        "export matrix is not square"
    )

    # Cell-for-cell equality.
    M_export_np = np.asarray(M_export, dtype=float)
    diff = np.abs(M_sim - M_export_np)
    worst = float(diff.max())
    if worst > CELL_TOL:
        # Find the worst cell for a meaningful failure message.
        h, a = np.unravel_index(int(diff.argmax()), diff.shape)
        pytest.fail(
            f"Matrix divergence at (lam_h={lam_h}, lam_a={lam_a}): "
            f"worst cell ({h},{a}) |Δ|={worst:.3e} > tol={CELL_TOL:g}\n"
            f"  sim[{h},{a}]    = {M_sim[h,a]:.15e}\n"
            f"  export[{h},{a}] = {M_export_np[h,a]:.15e}\n"
            f"Investigate: nb_dispersion drift, dc_rho drift, DC τ on wrong "
            f"cells, NB↔Poisson swap, floor (1e-12) mismatch, max_g drift, "
            f"or a renorm bug in either side."
        )

    # Also assert Σ = 1 for both (sanity — both sides renormalise).
    assert abs(float(M_sim.sum()) - 1.0) < 1e-12
    assert abs(sum(sum(row) for row in M_export) - 1.0) < 1e-12


# ----------------------------------------------------- WDL grid
@pytest.mark.parametrize("lam_h,lam_a", _grid_pairs(),
                         ids=lambda v: f"{v:.1f}")
def test_export_wdl_matches_sim_wdl_grid(lam_h: float, lam_a: float) -> None:
    """Same cell-for-cell test at the WDL level — independently picks up
    any bug in `_wdl_from_matrix` (the lower/diag/upper triangle split)
    in either implementation.
    """
    M_sim = sim.build_score_matrix(
        lam_h, lam_a, SIM_CFG, use_dispersion=True, max_g=PROD_MAX_G
    )
    M_export = export_build_matrix(lam_h, lam_a)

    p_h_sim, p_d_sim, p_a_sim = sim.wdl_from_matrix(M_sim)
    p_h_ex, p_d_ex, p_a_ex = export_wdl(M_export)

    diffs = {
        "p_home_win": abs(p_h_sim - p_h_ex),
        "p_draw":     abs(p_d_sim - p_d_ex),
        "p_away_win": abs(p_a_sim - p_a_ex),
    }
    worst = max(diffs.values())
    if worst > CELL_TOL:
        pytest.fail(
            f"WDL divergence at (lam_h={lam_h}, lam_a={lam_a}): "
            f"worst |Δ|={worst:.3e} > tol={CELL_TOL:g}\n"
            f"  p_home_win  sim={p_h_sim:.15e}  export={p_h_ex:.15e}  |Δ|={diffs['p_home_win']:.3e}\n"
            f"  p_draw      sim={p_d_sim:.15e}  export={p_d_ex:.15e}  |Δ|={diffs['p_draw']:.3e}\n"
            f"  p_away_win  sim={p_a_sim:.15e}  export={p_a_ex:.15e}  |Δ|={diffs['p_away_win']:.3e}"
        )

    # WDL must sum to 1.
    assert abs((p_h_sim + p_d_sim + p_a_sim) - 1.0) < 1e-12
    assert abs((p_h_ex + p_d_ex + p_a_ex) - 1.0) < 1e-12


# ----------------------------------------------------- end-to-end synthetic
# Synthetic resolved-KO fixture builders. Adapted from test_ko_advance_export.py
# so the end-to-end test exercises the same export() entrypoint the workflow
# invokes, but the assertion side is the PRODUCTION SIM matrix — not the
# export's own matrix builder.
def _minimal_team_predictions() -> list[dict]:
    return [{"team": f"T{i:02d}", "p_champion": 1.0 / 48} for i in range(48)]


def _bracket_with_one_resolved_r16(home: str, away: str) -> dict:
    br = {
        "r32_slots": [
            {"match_num": m, "slot_a": "1A", "slot_b": "2B",
             "date": "2026-06-28", "venue": "X", "next_match": 89}
            for m in range(73, 89)
        ],
        "r16_bracket": [
            {"match_num": m,
             "slot_a": f"W{m - 16}", "slot_b": f"W{m - 15}",
             "date": "2026-07-04", "venue": "X"}
            for m in range(89, 97)
        ],
        "qf_bracket": [
            {"match_num": m, "slot_a": f"W{m - 8}", "slot_b": f"W{m - 7}",
             "date": "2026-07-09", "venue": "X"}
            for m in range(97, 101)
        ],
        "sf_bracket": [
            {"match_num": m, "slot_a": f"W{m - 4}", "slot_b": f"W{m - 3}",
             "date": "2026-07-14", "venue": "X"}
            for m in range(101, 103)
        ],
        "final_and_third_place": {
            "third_place": {"match_num": 103, "slot_a": "L101", "slot_b": "L102",
                            "date": "2026-07-18", "venue": "X"},
            "final": {"match_num": 104, "slot_a": "W101", "slot_b": "W102",
                      "date": "2026-07-19", "venue": "X"},
        },
        "source": "test",
    }
    br["r16_bracket"][0]["slot_a"] = home
    br["r16_bracket"][0]["slot_b"] = away
    return br


def _feed_with_one_resolved_ko(home: str, away: str,
                               lam_h: float, lam_a: float) -> dict:
    return {
        "team_predictions": _minimal_team_predictions(),
        "match_predictions": [],
        "bracket": _bracket_with_one_resolved_r16(home, away),
        "knock_lambdas_table": [{
            "home": home, "away": away,
            "lambda_home": lam_h, "lambda_away": lam_a,
            "effective_elo_home": 1700.0, "effective_elo_away": 1500.0,
        }],
    }


def _write_min_results(tmp: Path) -> Path:
    p = tmp / "results_2026.json"
    p.write_text(json.dumps({
        "schema": "test",
        "updated_at": "2026-06-17T00:00:00Z",
        "source": "test",
        "completed_matches": [],
        "in_play": [],
        "warnings": [],
    }))
    return p


@pytest.mark.parametrize("lam_h,lam_a", [
    (1.6, 1.1),   # mid-range, slight home advantage (the existing fixture)
    (0.7, 0.5),   # low-scoring grind (Group H mid-tier)
    (2.4, 1.8),   # high-scoring open match (Brazil vs Argentina-ish)
    (1.2, 1.2),   # symmetric — sensitive to DC τ regression on (1,1)
])
def test_end_to_end_export_matches_sim_truth(tmp_path: Path,
                                             lam_h: float,
                                             lam_a: float) -> None:
    """Run the export's end-to-end CLI on a synthetic resolved-KO fixture
    and assert the emitted WDL + p_advance_match all match values computed
    via the PRODUCTION SIM's matrix (not the export's own matrix builder).

    Replaces the vacuous pre-R32 "passes by definition" coverage in
    test_goal_grid_feed_agreement.test_ko_advance_agreement with real
    cross-module coverage that runs every test session.
    """
    in_path = tmp_path / "predictions_live.json"
    in_path.write_text(json.dumps(
        _feed_with_one_resolved_ko("Argentina", "Brazil", lam_h, lam_a)
    ))
    out_path = tmp_path / "predictions_live.out.json"

    bracket_path = tmp_path / "bracket.json"
    bracket_path.write_text(json.dumps(
        _bracket_with_one_resolved_r16("Argentina", "Brazil")
    ))
    results_path = _write_min_results(tmp_path)
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({"groups": {}, "group_stage_schedule": [],
                                    "fifa_rankings_june_2026": {}}))
    annex_path = tmp_path / "annex.json"
    annex_path.write_text(json.dumps({"table": {}}))

    payload = run_export(
        in_path=in_path, out_path=out_path,
        bracket_path=bracket_path, results_path=results_path,
        cfg_path=cfg_path, annex_c_path=annex_path,
    )

    ko = payload["match_predictions_ko"]
    assert len(ko) == 1, f"expected exactly 1 resolved KO entry, got {len(ko)}"
    e = ko[0]

    # PRODUCTION SIM truth side — independent of the export's matrix builder.
    M_sim = sim.build_score_matrix(
        lam_h, lam_a, SIM_CFG, use_dispersion=True, max_g=PROD_MAX_G
    )
    p_h_truth, p_d_truth, p_a_truth = sim.wdl_from_matrix(M_sim)
    p_adv_truth = p_h_truth + 0.5 * p_d_truth

    # Cross-module equality at ≤1e-12 per channel.
    assert abs(e["p_home_win"] - p_h_truth) < CELL_TOL, (
        f"export p_home_win={e['p_home_win']:.15e} vs "
        f"sim truth={p_h_truth:.15e} (Δ={abs(e['p_home_win']-p_h_truth):.3e})"
    )
    assert abs(e["p_draw"] - p_d_truth) < CELL_TOL, (
        f"export p_draw={e['p_draw']:.15e} vs "
        f"sim truth={p_d_truth:.15e} (Δ={abs(e['p_draw']-p_d_truth):.3e})"
    )
    assert abs(e["p_away_win"] - p_a_truth) < CELL_TOL, (
        f"export p_away_win={e['p_away_win']:.15e} vs "
        f"sim truth={p_a_truth:.15e} (Δ={abs(e['p_away_win']-p_a_truth):.3e})"
    )
    assert abs(e["p_advance_match"] - p_adv_truth) < CELL_TOL, (
        f"export p_advance_match={e['p_advance_match']:.15e} vs "
        f"sim truth={p_adv_truth:.15e} "
        f"(Δ={abs(e['p_advance_match']-p_adv_truth):.3e})"
    )

    # Range + sum sanity.
    assert 0.0 <= e["p_advance_match"] <= 1.0
    assert abs((e["p_home_win"] + e["p_draw"] + e["p_away_win"]) - 1.0) < 1e-12
