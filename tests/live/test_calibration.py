"""Tests for ``scripts/calibration.py``.

Two layers:

1. **Metric primitives** -- log_loss / brier / reliability_table tested against
   tiny hand-computed examples so any change to the math is caught immediately.
2. **End-to-end CLI** -- invoke the script as a subprocess against the real
   files in this repo, assert exit code, parse the ``--json`` output, and apply
   a loose sanity bound (log loss < 1.10). If the model is so broken it can't
   even beat that, something far more serious is wrong than calibration.
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "calibration.py"
PREDICTIONS = REPO / "data" / "processed" / "predictions_live.json"
RESULTS = REPO / "data" / "live" / "results_2026.json"

# Ensure ``scripts/`` is importable for the unit-level tests.
sys.path.insert(0, str(REPO))

from scripts.calibration import (  # noqa: E402  (sys.path mutation above)
    brier,
    log_loss,
    reliability_table,
    _winner_to_index,
    build_report,
    UNIFORM_LOG_LOSS,
)

# ---------------------------------------------------------------------------
# Unit-level: metric primitives against hand computations
# ---------------------------------------------------------------------------
def test_log_loss_matches_hand_computation():
    """5 fixtures, each a perfectly confident prediction.

    For (0.7, 0.2, 0.1) on a home win the loss is -log(0.7).
    Sum of -log(p) values divided by 5 should match to 1e-9.
    """
    probs = [
        (0.7, 0.2, 0.1),  # outcome 0 (home)  -> -log(0.7)
        (0.2, 0.6, 0.2),  # outcome 1 (draw)  -> -log(0.6)
        (0.1, 0.3, 0.6),  # outcome 2 (away)  -> -log(0.6)
        (0.5, 0.3, 0.2),  # outcome 0 (home)  -> -log(0.5)
        (0.34, 0.33, 0.33),  # outcome 1 (draw) -> -log(0.33)
    ]
    outcomes = [0, 1, 2, 0, 1]
    expected = (
        -math.log(0.7)
        - math.log(0.6)
        - math.log(0.6)
        - math.log(0.5)
        - math.log(0.33)
    ) / 5.0
    assert log_loss(probs, outcomes) == pytest.approx(expected, abs=1e-9)


def test_log_loss_uniform_is_log3():
    """Uniform 1/3 prediction on any single outcome gives log(3)."""
    probs = [(1 / 3, 1 / 3, 1 / 3)]
    for y in (0, 1, 2):
        assert log_loss(probs, [y]) == pytest.approx(math.log(3.0), abs=1e-12)
    assert UNIFORM_LOG_LOSS == pytest.approx(math.log(3.0), abs=1e-12)


def test_log_loss_clips_zero_probability():
    """A zero-probability prediction on the realized class must not blow up."""
    probs = [(0.0, 0.5, 0.5)]
    ll = log_loss(probs, [0])
    # Clipped to EPS=1e-15; -log(1e-15) ~= 34.539
    assert math.isfinite(ll)
    assert ll > 30.0


def test_brier_matches_hand_computation():
    """Brier on a perfect prediction is 0; on a maximally-wrong one is 2."""
    # Perfect prediction for home win.
    assert brier([(1.0, 0.0, 0.0)], [0]) == pytest.approx(0.0, abs=1e-12)
    # Maximally wrong (all mass on away, realized home).
    # (0-1)^2 + (0-0)^2 + (1-0)^2 = 2
    assert brier([(0.0, 0.0, 1.0)], [0]) == pytest.approx(2.0, abs=1e-12)
    # Hand-computed mid case: predict (0.5, 0.3, 0.2), realized home.
    # (0.5-1)^2 + (0.3-0)^2 + (0.2-0)^2 = 0.25 + 0.09 + 0.04 = 0.38
    assert brier([(0.5, 0.3, 0.2)], [0]) == pytest.approx(0.38, abs=1e-12)


def test_brier_uniform_is_two_thirds():
    """Uniform 1/3 prediction gives Brier = 2/3 on every outcome."""
    for y in (0, 1, 2):
        assert brier([(1 / 3, 1 / 3, 1 / 3)], [y]) == pytest.approx(
            2.0 / 3.0, abs=1e-12
        )


def test_reliability_table_buckets_and_counts():
    """3 predictions land in two bins; verify n, mean_pred, actual_freq."""
    preds = [0.05, 0.15, 0.18]
    realized = [0, 1, 0]
    table = reliability_table(preds, realized, n_bins=10)
    # bin 0: [0.0, 0.1)  -> preds 0.05, realized 0
    assert table[0]["n"] == 1
    assert table[0]["mean_pred"] == pytest.approx(0.05, abs=1e-12)
    assert table[0]["actual_freq"] == pytest.approx(0.0, abs=1e-12)
    # bin 1: [0.1, 0.2)  -> preds 0.15 / 0.18, realized 1 + 0
    assert table[1]["n"] == 2
    assert table[1]["mean_pred"] == pytest.approx((0.15 + 0.18) / 2.0, abs=1e-12)
    assert table[1]["actual_freq"] == pytest.approx(0.5, abs=1e-12)
    # all other bins empty
    for i in range(2, 10):
        assert table[i]["n"] == 0


def test_winner_to_index_score_fallback():
    """winner=None with non-equal score should resolve via score comparison."""
    assert _winner_to_index(None, 2, 1) == 0  # home
    assert _winner_to_index(None, 0, 3) == 2  # away
    assert _winner_to_index(None, 1, 1) == 1  # draw
    assert _winner_to_index("home", 2, 0) == 0
    assert _winner_to_index("away", 0, 2) == 2


def test_input_length_mismatch_raises():
    with pytest.raises(ValueError):
        log_loss([(0.5, 0.3, 0.2)], [0, 1])
    with pytest.raises(ValueError):
        brier([(0.5, 0.3, 0.2)], [0, 1])
    with pytest.raises(ValueError):
        reliability_table([0.1, 0.2], [0])


# ---------------------------------------------------------------------------
# Integration-level: build_report on the real data
# ---------------------------------------------------------------------------
def test_build_report_shape():
    """The structured report has all the keys downstream consumers need."""
    if not PREDICTIONS.exists() or not RESULTS.exists():
        pytest.skip("data files missing -- expected only off the live machine")
    report = build_report(PREDICTIONS, RESULTS, max_m=72)
    if report["n_completed"] == 0:
        pytest.skip("no completed group-stage fixtures yet")
    assert "model" in report
    assert "log_loss" in report["model"]
    assert "brier" in report["model"]
    assert "baselines" in report
    assert "uniform" in report["baselines"]
    assert "reliability" in report
    assert set(report["reliability"]) == {"home", "draw", "away"}
    # 10 buckets per outcome.
    for outcome in ("home", "draw", "away"):
        assert len(report["reliability"][outcome]) == 10


# ---------------------------------------------------------------------------
# End-to-end CLI invocation against the real files
# ---------------------------------------------------------------------------
def test_cli_runs_against_real_data():
    """Invoke the script as a subprocess, assert exit 0, parse JSON, sanity-bound."""
    if not PREDICTIONS.exists() or not RESULTS.exists():
        pytest.skip("data files missing")
    res = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--predictions",
            str(PREDICTIONS),
            "--results",
            str(RESULTS),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, (
        f"calibration CLI exited {res.returncode}\n"
        f"stdout={res.stdout}\nstderr={res.stderr}"
    )
    payload = json.loads(res.stdout)
    if payload.get("n_completed", 0) == 0:
        pytest.skip("no completed fixtures yet -- nothing to score")
    ll = payload["model"]["log_loss"]
    br = payload["model"]["brier"]
    assert math.isfinite(ll), f"log loss not finite: {ll}"
    assert math.isfinite(br), f"brier not finite: {br}"
    # Loose sanity bound: if the model is worse than this it is broken, not
    # merely poorly calibrated.
    assert ll < 1.10, f"log loss {ll} suggests the model is broken"
    # Brier is bounded in [0, 2]; the uniform baseline is 2/3 ~= 0.667.
    # Anything above ~0.9 would be alarming.
    assert br < 0.95, f"brier {br} is alarmingly high"


def test_cli_human_output_runs():
    """Default (non-JSON) human-readable output also runs to completion."""
    if not PREDICTIONS.exists() or not RESULTS.exists():
        pytest.skip("data files missing")
    res = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--predictions",
            str(PREDICTIONS),
            "--results",
            str(RESULTS),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0
    assert "Calibration report" in res.stdout
    assert "log loss" in res.stdout
    assert "Reliability tables" in res.stdout
