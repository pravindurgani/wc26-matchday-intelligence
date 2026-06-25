"""Execute the REAL Apps Script GOAL_GRID source under Node and assert it
agrees with the pinned numbers in test_goal_grid.py to 1e-9.

Why this exists
---------------
`tests/live/test_goal_grid.py` defines `_js_build_score_matrix` as a Python
port of `_buildScoreMatrix_` and pins numbers derived from that port. That
test is therefore circular — if the port is wrong in the same way as the
source, the test still passes. This test breaks the loop by invoking the
ACTUAL .gs source via Node and pinning against the same numerical claims.

If this test ever drifts from `test_goal_grid.py`, the .gs source IS the
ground truth — `test_goal_grid.py` is the one to update.
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "wc26-engine-gs" / "test_harness.mjs"

# Resolver lives in tests/live/_node_resolver.py — same dir as this
# file. Import via sys.path injection (no __init__.py in tests/live).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _node_resolver import NODE_BIN  # noqa: E402

# Pinned numbers — copied verbatim from tests/live/test_goal_grid.py.
# If the real engine disagrees with these, the engine wins and that file
# is what needs updating (see module docstring).
# R13 C3: re-pinned at max_g=15 (was 10 pre-R12). Differences are tiny
# (~4e-7 on the (0,0) cell at λ=1.4, growing slightly at higher λ where
# the Poisson tail above 10 carries more mass).
PINNED_ASYM = {                                  # λ_h=1.8, λ_a=0.9
    (0, 0): 0.0819388537,
    (0, 1): 0.0537888709,
    (1, 0): 0.0933233864,
    (1, 1): 0.1239032427,
    (2, 1): 0.0986839986,
    (1, 2): 0.0493419993,
    (2, 0): 0.1096488874,
    (0, 2): 0.0274122218,
    (2, 2): 0.0444077994,
    (3, 0): 0.0657893324,
}
PINNED_SWAP_RATIO = 1.7350                       # M[1][0]/M[0][1] @ (1.8,0.9) — unchanged at 4dp
PINNED_REV = {                                   # λ_h=0.9, λ_a=1.8 (mirror)
    (1, 0): 0.0537888709,
    (0, 1): 0.0933233864,
}
PINNED_SYM = {                                   # λ_h=λ_a=1.4
    (0, 0): 0.0763044666,
    (0, 1): 0.0696396837,
    (1, 0): 0.0696396837,
    (1, 1): 0.1346821267,
}
PINNED_MARKETS_ASYM = {                          # via test_goal_grid.py
    "ou15": 0.7709488890,
    "ou25": 0.5099845371,
    "ou35": 0.2879455402,
    "btts": 0.5131216747,
    "home": 0.5617395976,                        # ah0
    "draw": 0.2591075167,
    "away": 0.1791528858,
}

ABS_TOL = 1e-9


# --------------------------------------------------------------------------
# Harness invocation
# --------------------------------------------------------------------------
# Under CI the import of `_node_resolver` already raises if NODE_BIN is
# None — so reaching this point under CI guarantees NODE_BIN is set.
# On dev machines without node, NODE_BIN may be None; skipif handles it.
pytestmark = pytest.mark.skipif(
    NODE_BIN is None,
    reason="node not available - set WC26_NODE_BIN or install node",
)


@pytest.fixture(scope="module")
def engine_report() -> dict:
    if not HARNESS.is_file():
        pytest.fail(f"Harness missing: {HARNESS}")
    proc = subprocess.run(
        [NODE_BIN, str(HARNESS)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"Node harness exited {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout[:2000]}\n"
            f"--- stderr ---\n{proc.stderr[:2000]}"
        )
    # The harness emits a human-readable pass/fail console log AND a
    # sentinel-delimited JSON report block. Slice between the sentinels
    # before json.loads — feeding the whole stdout to json.loads fails on
    # the banner ("=== Version sanity ==="). Sentinels are exact strings
    # matched in wc26-engine-gs/test_harness.mjs::buildEngineReport tail.
    BEGIN = "===JSON_REPORT_BEGIN==="
    END = "===JSON_REPORT_END==="
    b = proc.stdout.find(BEGIN)
    e = proc.stdout.find(END)
    if b < 0 or e < 0 or e <= b:
        pytest.fail(
            "Harness output missing JSON report sentinels — check "
            f"test_harness.mjs::buildEngineReport.\n"
            f"BEGIN found at {b}, END found at {e}\n"
            f"First 500 chars: {proc.stdout[:500]}"
        )
    payload = proc.stdout[b + len(BEGIN):e].strip()
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        pytest.fail(f"JSON report payload not valid JSON: {exc}\nPayload (first 500): {payload[:500]}")


# --------------------------------------------------------------------------
# Matrix-cell pin tests — REAL engine vs PINNED Python-replica numbers.
# --------------------------------------------------------------------------
def _matrix(report: dict, key: str) -> list[list[float]]:
    return report["scenarios"][key]["matrix"]


def test_engine_loaded_and_constants(engine_report):
    # R13 C3: bumped 10 → 15 to follow R12 MED's sim default change.
    assert engine_report["max_goals"] == 15
    assert engine_report["rho"] == -0.13
    assert "asym_1p8_0p9" in engine_report["scenarios"]
    assert "asym_0p9_1p8" in engine_report["scenarios"]
    assert "sym_1p4_1p4"  in engine_report["scenarios"]


def test_engine_matrix_renormalised(engine_report):
    # All three matrices must sum to 1.0 within 1e-12 — proves the engine's
    # explicit-renormalize branch ran.
    for key in ("asym_1p8_0p9", "asym_0p9_1p8", "sym_1p4_1p4"):
        total = engine_report["scenarios"][key]["total"]
        assert math.isclose(total, 1.0, abs_tol=1e-12), f"{key}: total = {total}"


def test_engine_matches_pinned_asymmetric_cells(engine_report):
    """λ_h=1.8, λ_a=0.9 — every pinned cell from test_goal_grid.py must match
    the REAL engine's output to 1e-9."""
    M = _matrix(engine_report, "asym_1p8_0p9")
    failures = []
    for (h, a), pinned in PINNED_ASYM.items():
        got = M[h][a]
        if not math.isclose(got, pinned, abs_tol=ABS_TOL):
            failures.append(f"  M[{h}][{a}]: engine={got!r}, pinned={pinned!r}, |Δ|={abs(got-pinned):.2e}")
    assert not failures, "REAL engine disagrees with pinned values:\n" + "\n".join(failures)


def test_engine_matches_pinned_symmetric_cells(engine_report):
    """λ_h = λ_a = 1.4 — symmetric mirror invariant + four pinned cells."""
    M = _matrix(engine_report, "sym_1p4_1p4")
    for (h, a), pinned in PINNED_SYM.items():
        got = M[h][a]
        assert math.isclose(got, pinned, abs_tol=ABS_TOL), (
            f"M[{h}][{a}]: engine={got!r}, pinned={pinned!r}, |Δ|={abs(got-pinned):.2e}"
        )
    # Mirror identity must hold exactly (no τ-branch asymmetry).
    assert math.isclose(M[0][1], M[1][0], abs_tol=1e-15)
    assert math.isclose(M[0][2], M[2][0], abs_tol=1e-15)
    assert math.isclose(M[1][2], M[2][1], abs_tol=1e-15)


def test_engine_matches_pinned_reversed_cells(engine_report):
    """λ_h=0.9, λ_a=1.8 — must mirror the asymmetric case."""
    M = _matrix(engine_report, "asym_0p9_1p8")
    for (h, a), pinned in PINNED_REV.items():
        got = M[h][a]
        assert math.isclose(got, pinned, abs_tol=ABS_TOL), (
            f"M[{h}][{a}]: engine={got!r}, pinned={pinned!r}, |Δ|={abs(got-pinned):.2e}"
        )
    # The reversed matrix must be the transpose of the asymmetric matrix.
    M_asym = _matrix(engine_report, "asym_1p8_0p9")
    for h in range(11):
        for a in range(11):
            assert math.isclose(M[h][a], M_asym[a][h], abs_tol=1e-15), (
                f"transpose violation at ({h},{a})"
            )


# --------------------------------------------------------------------------
# Swap-detector tests — proves no sign / home-away flip lurks in _dcTau_.
# --------------------------------------------------------------------------
def test_engine_swap_ratio_asymmetric(engine_report):
    """λ_h > λ_a ⇒ P(1,0) > P(0,1). Pinned at 1.7350 (test_goal_grid.py)."""
    ratio = engine_report["scenarios"]["asym_1p8_0p9"]["swap_ratio"]
    assert ratio > 1.0, f"swap detector FAILED: ratio={ratio:.4f}"
    assert math.isclose(ratio, PINNED_SWAP_RATIO, abs_tol=1e-4), (
        f"ratio drift: engine={ratio:.6f}, pinned={PINNED_SWAP_RATIO}"
    )


def test_engine_swap_ratio_reversed(engine_report):
    """λ_h < λ_a ⇒ ratio < 1 (mirror)."""
    ratio = engine_report["scenarios"]["asym_0p9_1p8"]["swap_ratio"]
    assert ratio < 1.0, f"reversed swap detector FAILED: ratio={ratio:.4f}"
    # Mirror: 1/1.7350 ≈ 0.5764.
    assert math.isclose(ratio, 1.0 / PINNED_SWAP_RATIO, abs_tol=1e-4)


def test_engine_swap_ratio_symmetric(engine_report):
    """λ_h = λ_a ⇒ ratio ≈ 1.0 exactly (within fp epsilon)."""
    ratio = engine_report["scenarios"]["sym_1p4_1p4"]["swap_ratio"]
    assert math.isclose(ratio, 1.0, abs_tol=1e-12), (
        f"symmetric swap detector failed: ratio={ratio!r}"
    )


# --------------------------------------------------------------------------
# GOAL_GRID top-level wrapper — every market route must match the matrix.
# --------------------------------------------------------------------------
def test_engine_goal_grid_routes_match_market_sums(engine_report):
    """GOAL_GRID(λ_h, λ_a, market) must give the same number as summing the
    matrix cells. If the wrapper drifts from _sumMatrix_ + _buildScoreMatrix_,
    the spreadsheet view and the model probabilities silently diverge."""
    sc = engine_report["scenarios"]["asym_1p8_0p9"]
    routes = sc["goal_grid_routes"]
    mkts = sc["markets"]
    for k in ("ou15", "ou25", "ou35", "btts"):
        assert math.isclose(routes[k], mkts[k], abs_tol=1e-15), (
            f"GOAL_GRID({k}) drift: route={routes[k]!r}, market={mkts[k]!r}"
        )
    # ah0 is the home-win route in the wrapper.
    assert math.isclose(routes["ah0"], mkts["home"], abs_tol=1e-15)
    # Correct-score routes must equal the raw matrix cell.
    M = sc["matrix"]
    assert math.isclose(routes["cs10"], M[1][0], abs_tol=1e-15)
    assert math.isclose(routes["cs01"], M[0][1], abs_tol=1e-15)
    assert math.isclose(routes["cs11"], M[1][1], abs_tol=1e-15)
    assert math.isclose(routes["cs22"], M[2][2], abs_tol=1e-15)


def test_engine_market_probabilities_match_pinned(engine_report):
    """The market aggregates that the spreadsheet displays must match the
    pinned numbers from test_goal_grid.py to 1e-9."""
    mkts = engine_report["scenarios"]["asym_1p8_0p9"]["markets"]
    for k, expected in PINNED_MARKETS_ASYM.items():
        got = mkts[k]
        assert math.isclose(got, expected, abs_tol=ABS_TOL), (
            f"market {k!r}: engine={got!r}, pinned={expected!r}, |Δ|={abs(got-expected):.2e}"
        )


# --------------------------------------------------------------------------
# extendToKnockouts — pure-logic core only (_knockoutStageFor_).
# The full extendToKnockouts() body is Sheets I/O and cannot run under Node.
# --------------------------------------------------------------------------
def test_engine_knockout_stage_classifier_pure_core(engine_report):
    """The pure-logic core of extendToKnockouts is _knockoutStageFor_(m).
    The wrapping function (read/write Bets!A:…) is SpreadsheetApp-only and
    is the subject of test_extend_to_knockouts.py (static parse-tests).

    Here we drive the classifier for every m∈[73..104] under the REAL engine
    and assert the FIFA WC 2026 stage layout (16+8+4+2+1+1 = 32)."""
    survey = engine_report["knockout_pure_core"]
    assert survey["runnable"] is True, survey.get("reason", "")
    by_match = survey["byMatch"]
    expected = {
        "R32":   (73,  88, 16),
        "R16":   (89,  96, 8),
        "QF":    (97, 100, 4),
        "SF":    (101, 102, 2),
        "3rd":   (103, 103, 1),
        "Final": (104, 104, 1),
    }
    counts = {}
    for stage, (first, last, n) in expected.items():
        for m in range(first, last + 1):
            entry = by_match[str(m)]
            assert entry is not None, f"m={m} unclassified"
            assert entry["stage"] == stage, (
                f"m={m}: engine says stage={entry['stage']!r}, expected {stage!r}"
            )
            # Slot is 1-indexed within stage.
            assert entry["slot"] == m - first + 1
        counts[stage] = n
    assert sum(counts.values()) == 32
    # Edge cases — below + above must return null.
    assert by_match["below"] is None
    assert by_match["above"] is None


# --------------------------------------------------------------------------
# CLV / _seedGoalMarketsMinEdge_ — pure SpreadsheetApp I/O, no math.
# --------------------------------------------------------------------------
@pytest.mark.skip(
    reason="_seedGoalMarketsMinEdge_ is pure SpreadsheetApp I/O "
           "(reads Method!B7, writes Method!B8). No pure-logic core to "
           "exercise under Node. Static parse-tests in test_goal_grid.py "
           "(test_goal_markets_min_edge_seeder_defined) cover this helper."
)
def test_engine_clv_seed_helper_under_node():
    pass
