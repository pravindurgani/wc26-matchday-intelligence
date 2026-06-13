"""
04_evaluate.py — Calibration and backtest evaluation of the goal model.

Outputs:
  models/evaluation.json — summary metrics
  data/processed/calibration.json — calibration table for dashboard plot

Metrics:
  - Implied W/D/L log-loss (vs naive baselines)
  - Brier score (multiclass)
  - Calibration (10-bin reliability table per outcome)
  - Backtest: train on pre-2014, evaluate on 2014-2018-2022 World Cup matches
  - Always-home baseline, Elo-only baseline
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "data" / "processed"
MODELS = ROOT / "models"


def poisson_pmf(k: int, lam: float) -> float:
    return math.exp(-lam) * lam ** k / math.factorial(k)


def lambdas_to_wdl(lam_h: float, lam_a: float, max_g: int = 10) -> tuple:
    """Convert (λ_h, λ_a) → (p_away, p_draw, p_home)."""
    ph = np.array([poisson_pmf(k, lam_h) for k in range(max_g + 1)])
    pa = np.array([poisson_pmf(k, lam_a) for k in range(max_g + 1)])
    ph /= ph.sum(); pa /= pa.sum()
    mat = np.outer(ph, pa)
    p_home = float(np.tril(mat, -1).sum())
    p_draw = float(np.trace(mat))
    p_away = float(np.triu(mat, 1).sum())
    return (p_away, p_draw, p_home)


def wdl_label(home: int, away: int) -> int:
    return 2 if home > away else (1 if home == away else 0)


def log_loss(y_true: np.ndarray, probs: np.ndarray, eps: float = 1e-12) -> float:
    probs = np.clip(probs, eps, 1 - eps)
    return float(-np.mean(np.log(probs[np.arange(len(y_true)), y_true])))


def brier_score(y_true: np.ndarray, probs: np.ndarray) -> float:
    """Multiclass Brier = mean over examples of Σ (p_k - y_k)^2."""
    oh = np.zeros_like(probs)
    oh[np.arange(len(y_true)), y_true] = 1
    return float(np.mean(np.sum((probs - oh) ** 2, axis=1)))


def calibration_table(y_true: np.ndarray, probs: np.ndarray, label_idx: int,
                       n_bins: int = 10) -> list[dict]:
    """For a single class, bin predicted probabilities and report mean predicted
    vs actual frequency in each bin.
    """
    pred = probs[:, label_idx]
    actual = (y_true == label_idx).astype(int)
    bins = np.linspace(0, 1, n_bins + 1)
    out = []
    for i in range(n_bins):
        mask = (pred >= bins[i]) & (pred < bins[i + 1])
        if not mask.any():
            continue
        out.append({
            "bin_low": float(bins[i]), "bin_high": float(bins[i + 1]),
            "n": int(mask.sum()),
            "mean_pred": float(pred[mask].mean()),
            "actual_freq": float(actual[mask].mean()),
        })
    return out


def baseline_always_home(n: int) -> np.ndarray:
    """48% home, 26% draw, 26% away — empirical international football priors."""
    return np.tile(np.array([0.26, 0.26, 0.48]), (n, 1))


def baseline_elo(elo_home: np.ndarray, elo_away: np.ndarray) -> np.ndarray:
    """Logistic Elo W/L baseline, with fixed 26% draw."""
    eh = 1 / (1 + 10 ** ((elo_away - elo_home) / 400.0))
    p_home_wl = eh * 0.74
    p_away_wl = (1 - eh) * 0.74
    p_draw = np.full_like(eh, 0.26)
    return np.stack([p_away_wl, p_draw, p_home_wl], axis=1)


def main():
    print("[1/4] Loading models + features…")
    home_model = joblib.load(MODELS / "home_goals_model.joblib")
    away_model = joblib.load(MODELS / "away_goals_model.joblib")
    feature_cols = json.loads((MODELS / "feature_cols_v2.json").read_text())
    feats = pd.read_parquet(PROC / "match_features_v2.parquet")

    feats = feats.sort_values("date").reset_index(drop=True)
    feats["date"] = pd.to_datetime(feats["date"])

    # ---- Holdout (last 15% of full modern dataset) ----
    modern = feats[feats["date"] >= "1990-01-01"].copy().reset_index(drop=True)
    cutoff = int(len(modern) * 0.85)
    te = modern.iloc[cutoff:].copy()
    print(f"      Holdout test rows: {len(te):,}")

    X_te = te[feature_cols].values
    lam_h = np.clip(home_model.predict(X_te), 0.05, 7.0)
    lam_a = np.clip(away_model.predict(X_te), 0.05, 7.0)

    probs = np.array([lambdas_to_wdl(lh, la) for lh, la in zip(lam_h, lam_a)])
    y = np.array([wdl_label(int(h), int(a)) for h, a in zip(te["home_goals"], te["away_goals"])])

    ll = log_loss(y, probs)
    bs = brier_score(y, probs)
    acc = float(np.mean(probs.argmax(axis=1) == y))

    print(f"\n  === Holdout metrics (Goal Model) ===")
    print(f"  log-loss : {ll:.4f}")
    print(f"  Brier    : {bs:.4f}")
    print(f"  accuracy : {acc:.4f}")

    # Baselines on the same test set
    probs_naive = baseline_always_home(len(te))
    ll_naive = log_loss(y, probs_naive)
    bs_naive = brier_score(y, probs_naive)

    probs_elo = baseline_elo(te["elo_home"].values, te["elo_away"].values)
    ll_elo = log_loss(y, probs_elo)
    bs_elo = brier_score(y, probs_elo)

    print(f"\n  === Baselines ===")
    print(f"  always-home/draw priors : log-loss={ll_naive:.4f}  Brier={bs_naive:.4f}")
    print(f"  Elo-only logistic       : log-loss={ll_elo:.4f}  Brier={bs_elo:.4f}")
    print(f"  Goal model (ours)       : log-loss={ll:.4f}  Brier={bs:.4f}")
    print(f"  Lift vs naive  : log-loss {(ll_naive-ll):.3f}, Brier {(bs_naive-bs):.3f}")
    print(f"  Lift vs Elo    : log-loss {(ll_elo-ll):.3f}, Brier {(bs_elo-bs):.3f}")

    # Calibration tables
    cal_home = calibration_table(y, probs, 2)
    cal_draw = calibration_table(y, probs, 1)
    cal_away = calibration_table(y, probs, 0)

    # ---- World Cup-only backtest ----
    print(f"\n[2/4] World Cup match backtest (2010, 2014, 2018, 2022)…")
    wc_rows = feats[
        (feats["tournament"] == "FIFA World Cup") &
        (feats["date"] >= "2010-01-01")
    ].copy()
    print(f"      WC rows: {len(wc_rows)}")
    if len(wc_rows) > 0:
        X_wc = wc_rows[feature_cols].values
        lam_h_wc = np.clip(home_model.predict(X_wc), 0.05, 7.0)
        lam_a_wc = np.clip(away_model.predict(X_wc), 0.05, 7.0)
        probs_wc = np.array([lambdas_to_wdl(lh, la) for lh, la in zip(lam_h_wc, lam_a_wc)])
        y_wc = np.array([wdl_label(int(h), int(a))
                         for h, a in zip(wc_rows["home_goals"], wc_rows["away_goals"])])
        ll_wc = log_loss(y_wc, probs_wc)
        bs_wc = brier_score(y_wc, probs_wc)
        acc_wc = float(np.mean(probs_wc.argmax(axis=1) == y_wc))
        print(f"      WC log-loss={ll_wc:.4f}, Brier={bs_wc:.4f}, accuracy={acc_wc:.4f}")
    else:
        ll_wc = bs_wc = acc_wc = None

    print(f"\n[3/4] Save evaluation artifacts…")
    eval_out = {
        "n_test": int(len(te)),
        "holdout": {
            "log_loss": ll, "brier": bs, "accuracy": acc,
            "log_loss_naive": ll_naive, "log_loss_elo_baseline": ll_elo,
            "brier_naive": bs_naive, "brier_elo_baseline": bs_elo,
            "lift_log_loss_vs_naive": float(ll_naive - ll),
            "lift_log_loss_vs_elo": float(ll_elo - ll),
        },
        "wc_backtest": (
            None if ll_wc is None else
            {
                "log_loss": ll_wc, "brier": bs_wc, "accuracy": acc_wc,
                "n": int(len(wc_rows)),
                # The train/test split in 02_goal_model.py is "last 15% by date" of
                # all post-1990 matches. With the dataset extending into 2026, the
                # cutoff falls around 2017–2020 — meaning WC 2010 and 2014 (and
                # most of 2018) are in the training set when this block is computed.
                # That makes wc_backtest an OPTIMISTIC, in-sample sanity check, NOT
                # an out-of-sample backtest. For the true out-of-sample reading use
                # walk_forward.json (scripts/07_walk_forward.py), which retrains
                # before each WC. Dashboard intentionally surfaces walk_forward.json
                # in the Sensitivity/Appendix sections; this block is retained for
                # forensic continuity with the original v1 evaluation.
                "_validation_scope": "in_sample_check",
                "_note": "WC 2010-2018 likely in training set; use walk_forward.json for true out-of-sample.",
            }
        ),
        "calibration": {"home": cal_home, "draw": cal_draw, "away": cal_away},
    }
    (MODELS / "evaluation.json").write_text(json.dumps(eval_out, indent=2))
    (PROC / "calibration.json").write_text(json.dumps(eval_out, indent=2))

    print(f"\n[4/4] Calibration summary (home-win bin → predicted vs actual):")
    for r in cal_home:
        bar = "█" * int(r["actual_freq"] * 20)
        print(f"   [{r['bin_low']:.1f}-{r['bin_high']:.1f}] n={r['n']:>4d}  "
              f"pred={r['mean_pred']:.2f} actual={r['actual_freq']:.2f}  {bar}")

    print(f"\n[OK] evaluation.json written to {MODELS}")


if __name__ == "__main__":
    main()
