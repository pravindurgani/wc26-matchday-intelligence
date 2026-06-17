"""Pre-match WDL calibration probe.

Read-only quality measurement that joins pre-match WDL probabilities from
``data/processed/predictions_live.json`` against realized outcomes in
``data/live/results_2026.json`` and reports:

  * mean log loss (cross-entropy)
  * mean multi-class Brier score
  * reliability tables (predicted-prob deciles vs realized frequency)
    per outcome (home / draw / away)
  * uniform-1/3 and long-run (0.45/0.27/0.28) baselines for comparison

Exit status
-----------
  0  model log loss <= long-run baseline log loss (i.e. beats or matches it)
  1  model log loss > 1.05 (materially worse than naive)
  0  otherwise (between long-run baseline and 1.05 -- mediocre but not broken)

The script never writes to disk, never mutates inputs, never trains anything.
It is a pure read-only measurement intended for CI gating before knockouts.

Usage
-----
    python3 scripts/calibration.py \\
        --predictions data/processed/predictions_live.json \\
        --results data/live/results_2026.json

    # Machine-readable form (for downstream ingestion / CI):
    python3 scripts/calibration.py --predictions ... --results ... --json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# Baselines (constants -- never derived from the data being scored)
# ---------------------------------------------------------------------------
UNIFORM_LOG_LOSS = math.log(3.0)  # ~1.0986
# Long-run football base rate (home / draw / away). The same three-vector is
# used for every fixture; the resulting log loss depends on the realized class
# mix in the sample, so we compute it from the joined data instead of hard
# coding ~1.03.
LONG_RUN_RATE = (0.45, 0.27, 0.28)

# Materially-worse threshold. If the model crosses this it is broken and CI
# should fail loudly.
BROKEN_LOG_LOSS = 1.05

# Numerical safety floor for log(p).
EPS = 1e-15


# ---------------------------------------------------------------------------
# Core metric primitives
# ---------------------------------------------------------------------------
def log_loss(probs: Sequence[Sequence[float]], outcomes: Sequence[int]) -> float:
    """Mean categorical cross-entropy over ``len(probs)`` samples.

    ``probs[i]`` is a 3-vector ``(p_home, p_draw, p_away)`` and ``outcomes[i]``
    is the realized class index 0/1/2 for home/draw/away. Probabilities are
    clipped to ``[EPS, 1-EPS]`` before taking ``log`` so a stray 0 cannot
    poison the average.
    """
    if not probs:
        raise ValueError("log_loss requires at least one sample")
    if len(probs) != len(outcomes):
        raise ValueError("probs and outcomes must be the same length")
    total = 0.0
    for p, y in zip(probs, outcomes):
        p_y = max(EPS, min(1.0 - EPS, p[y]))
        total += -math.log(p_y)
    return total / len(probs)


def brier(probs: Sequence[Sequence[float]], outcomes: Sequence[int]) -> float:
    """Mean multi-class Brier score.

    For each sample, compute ``sum_i (p_i - y_i)^2`` where ``y`` is one-hot
    over the 3 outcomes; then average across samples. Bounded in ``[0, 2]``.
    """
    if not probs:
        raise ValueError("brier requires at least one sample")
    if len(probs) != len(outcomes):
        raise ValueError("probs and outcomes must be the same length")
    total = 0.0
    for p, y in zip(probs, outcomes):
        s = 0.0
        for i in range(3):
            target = 1.0 if i == y else 0.0
            s += (p[i] - target) ** 2
        total += s
    return total / len(probs)


def reliability_table(
    pred_probs: Sequence[float],
    realized: Sequence[int],
    n_bins: int = 10,
) -> list[dict]:
    """Bucket predicted probabilities into ``n_bins`` equal-width bins on
    ``[0, 1]`` and report the realized frequency of the positive class in each.

    Bin edges are ``[i/n_bins, (i+1)/n_bins)`` with the final bin including 1.0.
    """
    if len(pred_probs) != len(realized):
        raise ValueError("pred_probs and realized must be the same length")
    buckets: list[dict] = []
    for i in range(n_bins):
        lo = i / n_bins
        hi = (i + 1) / n_bins
        if i == n_bins - 1:
            in_bin = [(p, y) for p, y in zip(pred_probs, realized) if lo <= p <= hi]
        else:
            in_bin = [(p, y) for p, y in zip(pred_probs, realized) if lo <= p < hi]
        n = len(in_bin)
        mean_pred = sum(p for p, _ in in_bin) / n if n else 0.0
        actual_freq = sum(y for _, y in in_bin) / n if n else 0.0
        midpoint = (lo + hi) / 2.0
        buckets.append(
            {
                "bin_low": lo,
                "bin_high": hi,
                "bin_midpoint": midpoint,
                "n": n,
                "mean_pred": mean_pred,
                "actual_freq": actual_freq,
            }
        )
    return buckets


# ---------------------------------------------------------------------------
# I/O + join
# ---------------------------------------------------------------------------
def _winner_to_index(winner: object, home_score: object, away_score: object) -> int:
    """Map a results entry to a class index ``0=home, 1=draw, 2=away``.

    Prefer the ``winner`` field. Fall back to the score comparison if winner is
    missing (some upstream feeds omit it but always populate the scoreline).
    Raises ``ValueError`` if the fixture cannot be resolved.
    """
    if winner == "home":
        return 0
    if winner == "away":
        return 2
    if winner is None:
        # Draw OR missing -- disambiguate via score.
        if isinstance(home_score, int) and isinstance(away_score, int):
            if home_score > away_score:
                return 0
            if home_score < away_score:
                return 2
            return 1
        raise ValueError("winner=None with non-integer scores; cannot resolve")
    raise ValueError(f"unrecognized winner value: {winner!r}")


def load_predictions(path: Path) -> dict[int, tuple[float, float, float]]:
    """Read ``predictions_live.json`` and return ``{m: (p_h, p_d, p_a)}``."""
    payload = json.loads(path.read_text())
    out: dict[int, tuple[float, float, float]] = {}
    for entry in payload.get("match_predictions", []):
        m = entry["m"]
        p_h = float(entry["p_home_win"])
        p_d = float(entry["p_draw"])
        p_a = float(entry["p_away_win"])
        out[m] = (p_h, p_d, p_a)
    return out


def load_results(path: Path, max_m: int | None = 72) -> dict[int, int]:
    """Read ``results_2026.json`` and return ``{m: outcome_index}``.

    By default only fixtures with ``m <= 72`` (group stage) are returned;
    pass ``max_m=None`` to include everything.
    """
    payload = json.loads(path.read_text())
    out: dict[int, int] = {}
    for entry in payload.get("completed_matches", []):
        m = entry["m"]
        if max_m is not None and m > max_m:
            continue
        out[m] = _winner_to_index(
            entry.get("winner"), entry.get("home_score"), entry.get("away_score")
        )
    return out


def join(
    preds: Mapping[int, tuple[float, float, float]],
    results: Mapping[int, int],
) -> tuple[list[tuple[float, float, float]], list[int], list[int]]:
    """Inner-join predictions and results, returning aligned lists plus the
    sorted list of matched match numbers (for reporting / debugging)."""
    matched = sorted(set(preds.keys()) & set(results.keys()))
    probs = [preds[m] for m in matched]
    outcomes = [results[m] for m in matched]
    return probs, outcomes, matched


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------
def _baseline_long_run_log_loss(outcomes: Iterable[int]) -> float:
    """Log loss assuming every fixture is predicted with the long-run rate."""
    rate = LONG_RUN_RATE
    n = 0
    total = 0.0
    for y in outcomes:
        total += -math.log(max(EPS, rate[y]))
        n += 1
    return total / n if n else float("nan")


def _baseline_uniform_brier(outcomes: Iterable[int]) -> float:
    """Brier for the uniform-1/3 baseline -- depends only on N."""
    # Always 3 * (1/3 - 1/3*one_hot_i)^2 = (1 - 1/3)^2 + 2 * (1/3)^2 = 2/3
    n = 0
    for _ in outcomes:
        n += 1
    return 2.0 / 3.0 if n else float("nan")


def _baseline_long_run_brier(outcomes: Iterable[int]) -> float:
    rate = LONG_RUN_RATE
    total = 0.0
    n = 0
    for y in outcomes:
        s = 0.0
        for i in range(3):
            target = 1.0 if i == y else 0.0
            s += (rate[i] - target) ** 2
        total += s
        n += 1
    return total / n if n else float("nan")


def build_report(
    predictions_path: Path,
    results_path: Path,
    max_m: int | None = 72,
) -> dict:
    preds = load_predictions(predictions_path)
    results = load_results(results_path, max_m=max_m)
    probs, outcomes, matched = join(preds, results)

    if not matched:
        return {
            "predictions_path": str(predictions_path),
            "results_path": str(results_path),
            "n_completed": 0,
            "warning": "no matched fixtures between predictions and results",
        }

    model_ll = log_loss(probs, outcomes)
    model_brier = brier(probs, outcomes)
    uniform_ll = UNIFORM_LOG_LOSS
    long_run_ll = _baseline_long_run_log_loss(outcomes)
    uniform_brier = _baseline_uniform_brier(outcomes)
    long_run_brier = _baseline_long_run_brier(outcomes)

    # Per-outcome reliability tables.
    reliability = {
        "home": reliability_table(
            [p[0] for p in probs], [1 if y == 0 else 0 for y in outcomes]
        ),
        "draw": reliability_table(
            [p[1] for p in probs], [1 if y == 1 else 0 for y in outcomes]
        ),
        "away": reliability_table(
            [p[2] for p in probs], [1 if y == 2 else 0 for y in outcomes]
        ),
    }

    class_mix = {
        "home_wins": sum(1 for y in outcomes if y == 0),
        "draws": sum(1 for y in outcomes if y == 1),
        "away_wins": sum(1 for y in outcomes if y == 2),
    }

    verdict = _calibration_verdict(reliability)

    return {
        "predictions_path": str(predictions_path),
        "results_path": str(results_path),
        "max_m": max_m,
        "n_completed": len(matched),
        "matched_m": matched,
        "class_mix": class_mix,
        "model": {
            "log_loss": model_ll,
            "brier": model_brier,
        },
        "baselines": {
            "uniform": {"log_loss": uniform_ll, "brier": uniform_brier},
            "long_run_0.45_0.27_0.28": {
                "log_loss": long_run_ll,
                "brier": long_run_brier,
            },
        },
        "lift": {
            "log_loss_vs_uniform": uniform_ll - model_ll,
            "log_loss_vs_long_run": long_run_ll - model_ll,
            "brier_vs_uniform": uniform_brier - model_brier,
            "brier_vs_long_run": long_run_brier - model_brier,
        },
        "reliability": reliability,
        "calibration_verdict": verdict,
    }


def _calibration_verdict(reliability: Mapping[str, list[dict]]) -> str:
    """Crude verdict over occupied buckets.

    For each occupied bucket across all three outcomes, compare realized
    frequency vs the bucket midpoint. If every occupied bucket is within
    +/-0.10, call WELL-CALIBRATED. If high-prob buckets (midpoint >= 0.5)
    systematically miss low, OVERCONFIDENT. If low-prob buckets systematically
    hit high, UNDERCONFIDENT.
    """
    over = under = within = 0
    for buckets in reliability.values():
        for b in buckets:
            if b["n"] == 0:
                continue
            diff = b["actual_freq"] - b["bin_midpoint"]
            if abs(diff) <= 0.10:
                within += 1
            elif b["bin_midpoint"] >= 0.5 and diff < -0.10:
                over += 1
            elif b["bin_midpoint"] < 0.5 and diff > 0.10:
                under += 1
    occupied = over + under + within
    if occupied == 0:
        return "INSUFFICIENT_DATA"
    if within / occupied >= 0.8:
        return "WELL_CALIBRATED"
    if over > under:
        return "OVERCONFIDENT"
    if under > over:
        return "UNDERCONFIDENT"
    return "MIXED"


# ---------------------------------------------------------------------------
# Pretty-printer (human form)
# ---------------------------------------------------------------------------
def _fmt_reliability(name: str, buckets: list[dict]) -> str:
    lines = [f"  {name.upper()}: bin_lo  bin_hi    n  mean_pred  actual_freq"]
    for b in buckets:
        lines.append(
            f"    [{b['bin_low']:.2f}, {b['bin_high']:.2f})  "
            f"{b['n']:4d}  {b['mean_pred']:9.3f}  {b['actual_freq']:11.3f}"
        )
    return "\n".join(lines)


def print_human(report: dict) -> None:
    if report.get("n_completed", 0) == 0:
        print(f"No matched fixtures between {report['predictions_path']} "
              f"and {report['results_path']}.")
        return
    m = report["model"]
    bl = report["baselines"]
    print(f"Calibration report  ({report['n_completed']} completed fixtures, "
          f"m <= {report['max_m']})")
    print(f"  predictions: {report['predictions_path']}")
    print(f"  results:     {report['results_path']}")
    cm = report["class_mix"]
    print(f"  class mix:   home={cm['home_wins']}  draws={cm['draws']}  "
          f"away={cm['away_wins']}")
    print()
    print(f"  model log loss        {m['log_loss']:.4f}")
    print(f"  model brier           {m['brier']:.4f}")
    print(f"  uniform-1/3 log loss  {bl['uniform']['log_loss']:.4f}  "
          f"(brier {bl['uniform']['brier']:.4f})")
    print(f"  long-run    log loss  {bl['long_run_0.45_0.27_0.28']['log_loss']:.4f}  "
          f"(brier {bl['long_run_0.45_0.27_0.28']['brier']:.4f})")
    lift = report["lift"]
    print()
    print(f"  lift vs uniform    log_loss = {lift['log_loss_vs_uniform']:+.4f}   "
          f"brier = {lift['brier_vs_uniform']:+.4f}")
    print(f"  lift vs long-run   log_loss = {lift['log_loss_vs_long_run']:+.4f}   "
          f"brier = {lift['brier_vs_long_run']:+.4f}")
    print()
    print("Reliability tables:")
    print(_fmt_reliability("home", report["reliability"]["home"]))
    print(_fmt_reliability("draw", report["reliability"]["draw"]))
    print(_fmt_reliability("away", report["reliability"]["away"]))
    print()
    print(f"  verdict: {report['calibration_verdict']}")


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------
def _exit_code(report: dict) -> int:
    """0 if model beats long-run baseline (lower log loss).
    1 if model log loss > BROKEN_LOG_LOSS.
    0 otherwise."""
    if report.get("n_completed", 0) == 0:
        return 0
    ll = report["model"]["log_loss"]
    if ll > BROKEN_LOG_LOSS:
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="WDL calibration probe (read-only).",
    )
    parser.add_argument(
        "--predictions",
        type=Path,
        required=True,
        help="Path to predictions_live.json",
    )
    parser.add_argument(
        "--results",
        type=Path,
        required=True,
        help="Path to results_2026.json",
    )
    parser.add_argument(
        "--max-m",
        type=int,
        default=72,
        help="Cap match number (group stage = 72; pass 0 to disable)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON to stdout (machine-readable)",
    )
    args = parser.parse_args(argv)

    max_m = args.max_m if args.max_m > 0 else None
    report = build_report(args.predictions, args.results, max_m=max_m)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_human(report)
    return _exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
