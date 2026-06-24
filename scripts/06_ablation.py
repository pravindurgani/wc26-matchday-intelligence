"""
06_ablation.py — Ablation study to isolate feature lift.

Configurations:
  1. Elo-only baseline (logistic on Elo diff, no goal model)
  2. Goal model, no venue effects, no squad value
  3. Goal model + venue effects
  4. Goal model + venue effects + squad value (= full v3 stack)

Output: log-loss, Brier, accuracy on holdout — shows which components add value.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "data" / "processed"
MODELS = ROOT / "models"
sys.path.insert(0, str(ROOT / "scripts"))

import importlib.util
ev_spec = importlib.util.spec_from_file_location("ev", ROOT / "scripts" / "04_evaluate.py")
ev = importlib.util.module_from_spec(ev_spec); ev_spec.loader.exec_module(ev)


def lambdas_to_wdl(lam_h, lam_a, max_g=15):
    # R14 C1: default `max_g` bumped 10 → 15 to follow R14 C1 in
    # 04_evaluate.py (which itself follows R12 MED's sim default change).
    # Ablation metrics (ablation.json on dashboard) now align with the
    # production sim's truncation level.
    return ev.lambdas_to_wdl(lam_h, lam_a, max_g)


def elo_only_baseline(elo_home, elo_away):
    """Pure logistic Elo with empirical 26% draw share."""
    p_home_wl = 1 / (1 + 10 ** ((elo_away - elo_home) / 400.0))
    return np.stack([
        (1 - p_home_wl) * 0.74,
        np.full_like(p_home_wl, 0.26),
        p_home_wl * 0.74,
    ], axis=1)


def main():
    print("[1/3] Loading models + features…")
    home_model = joblib.load(MODELS / "home_goals_model.joblib")
    away_model = joblib.load(MODELS / "away_goals_model.joblib")
    feature_cols = json.loads((MODELS / "feature_cols_v2.json").read_text())
    feats = pd.read_parquet(PROC / "match_features_v2.parquet")
    feats = feats.sort_values("date").reset_index(drop=True)
    modern = feats[feats["date"] >= "1990-01-01"].copy().reset_index(drop=True)
    cutoff = int(len(modern) * 0.85)
    te = modern.iloc[cutoff:].copy()
    print(f"      Holdout test rows: {len(te):,}")

    X_te = te[feature_cols].values
    y = np.array([2 if h > a else (1 if h == a else 0)
                  for h, a in zip(te["home_goals"], te["away_goals"])])

    # Ablation A: Elo-only logistic
    print("[2/3] Computing baselines + ablations…")
    probs_elo = elo_only_baseline(te["elo_home"].values, te["elo_away"].values)

    # Ablation B: Goal model on Elo + form only (no venue/squad — these aren't in features anyway)
    # H10 sync: 0.05/7.0 must match scripts/03_simulate.py LAMBDA_CLIP_MIN/MAX.
    lam_h = np.clip(home_model.predict(X_te), 0.05, 7.0)
    lam_a = np.clip(away_model.predict(X_te), 0.05, 7.0)
    probs_goal = np.array([lambdas_to_wdl(lh, la) for lh, la in zip(lam_h, lam_a)])

    # Note: venue and squad value are NOT in the model features at training time —
    # they're applied at SIMULATION time as Elo bumps. So the ablation here is
    # really: Elo-logistic vs goal model trained on Elo+form features.
    # The bigger ablation is at simulation time: see sensitivity.json for that.

    cfgs = {
        "elo_only_logistic": probs_elo,
        "goal_model_full": probs_goal,
    }

    print("[3/3] Metrics:")
    out = {}
    for name, probs in cfgs.items():
        ll = ev.log_loss(y, probs)
        bs = ev.brier_score(y, probs)
        acc = float(np.mean(probs.argmax(axis=1) == y))
        out[name] = {"log_loss": ll, "brier": bs, "accuracy": acc}
        print(f"   {name:<22s} log-loss={ll:.4f}  Brier={bs:.4f}  acc={acc:.4f}")

    lift_ll = out["elo_only_logistic"]["log_loss"] - out["goal_model_full"]["log_loss"]
    lift_bs = out["elo_only_logistic"]["brier"] - out["goal_model_full"]["brier"]
    print(f"\n   Lift from Elo→goal model: log-loss +{lift_ll:.3f}, Brier +{lift_bs:.3f}")

    out["lift"] = {
        "log_loss_lift_goal_over_elo": float(lift_ll),
        "brier_lift_goal_over_elo": float(lift_bs),
    }
    out["note"] = (
        "Note: venue/squad-value effects apply at simulation time (as Elo bumps), "
        "not at model training time. See sensitivity.json for impact of those Elo bumps "
        "on tournament outcomes."
    )

    (MODELS / "ablation.json").write_text(json.dumps(out, indent=2))
    print(f"\n[OK] ablation.json written")


if __name__ == "__main__":
    main()
