"""
07_walk_forward.py — Walk-forward World Cup backtest.

For each WC tournament 2010/2014/2018/2022:
  • Train the goal model on all matches BEFORE that tournament started
  • Evaluate predictions on the actual WC matches
  • Report log-loss, Brier, accuracy

Honest test of "given the data we'd have had at the time, how well would we do?"
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict, deque
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "data" / "processed"
MODELS = ROOT / "models"
sys.path.insert(0, str(ROOT / "scripts"))
import importlib.util

ev_spec = importlib.util.spec_from_file_location("ev", ROOT / "scripts" / "04_evaluate.py")
ev = importlib.util.module_from_spec(ev_spec); ev_spec.loader.exec_module(ev)

gm_spec = importlib.util.spec_from_file_location("gm", ROOT / "scripts" / "02_goal_model.py")
gm = importlib.util.module_from_spec(gm_spec); gm_spec.loader.exec_module(gm)


def make_models(X_tr, yh_tr, ya_tr):
    home_model = xgb.XGBRegressor(
        objective="count:poisson", n_estimators=400, max_depth=5,
        learning_rate=0.04, tree_method="hist", random_state=42,
        eval_metric="poisson-nloglik", n_jobs=-1,
    )
    away_model = xgb.XGBRegressor(
        objective="count:poisson", n_estimators=400, max_depth=5,
        learning_rate=0.04, tree_method="hist", random_state=42,
        eval_metric="poisson-nloglik", n_jobs=-1,
    )
    home_model.fit(X_tr, yh_tr, verbose=False)
    away_model.fit(X_tr, ya_tr, verbose=False)
    return home_model, away_model


def main():
    print("[1/3] Loading features…")
    feats = pd.read_parquet(PROC / "match_features_v2.parquet")
    feats = feats.sort_values("date").reset_index(drop=True)
    feats["date"] = pd.to_datetime(feats["date"])

    wc_dates = {
        2010: pd.Timestamp("2010-06-11"),
        2014: pd.Timestamp("2014-06-12"),
        2018: pd.Timestamp("2018-06-14"),
        2022: pd.Timestamp("2022-11-20"),
    }

    print("[2/3] Walk-forward training + WC-specific test:")
    results = {}
    for year, start in wc_dates.items():
        end = start + pd.Timedelta(days=35)
        train = feats[feats["date"] < start].copy()
        wc_matches = feats[
            (feats["tournament"] == "FIFA World Cup") &
            (feats["date"] >= start) & (feats["date"] <= end)
        ].copy()
        if len(wc_matches) == 0:
            print(f"   {year}: no test matches found, skipping")
            continue

        X_tr = train[gm.FEATURE_COLS].values
        yh_tr = train["home_goals"].values
        ya_tr = train["away_goals"].values
        X_te = wc_matches[gm.FEATURE_COLS].values
        yh_te = wc_matches["home_goals"].values
        ya_te = wc_matches["away_goals"].values

        home_model, away_model = make_models(X_tr, yh_tr, ya_tr)
        # H10 sync: keep 0.05/7.0 aligned with scripts/03_simulate.py LAMBDA_CLIP_*.
        lam_h = np.clip(home_model.predict(X_te), 0.05, 7.0)
        lam_a = np.clip(away_model.predict(X_te), 0.05, 7.0)
        probs = np.array([ev.lambdas_to_wdl(lh, la) for lh, la in zip(lam_h, lam_a)])
        y = np.array([2 if h > a else (1 if h == a else 0)
                      for h, a in zip(yh_te, ya_te)])

        ll = ev.log_loss(y, probs)
        bs = ev.brier_score(y, probs)
        acc = float(np.mean(probs.argmax(axis=1) == y))

        # Baseline: always-home
        probs_baseline = np.tile([0.26, 0.26, 0.48], (len(y), 1))
        ll_b = ev.log_loss(y, probs_baseline)

        results[year] = {
            "train_n": int(len(train)), "test_n": int(len(wc_matches)),
            "log_loss": ll, "brier": bs, "accuracy": acc,
            "log_loss_baseline": ll_b,
            "lift_vs_baseline": float(ll_b - ll),
        }
        print(f"   WC{year}: n_test={len(wc_matches):3d} "
              f"log-loss={ll:.3f} (baseline {ll_b:.3f}, lift +{ll_b-ll:.3f})  "
              f"Brier={bs:.3f}  acc={acc*100:.1f}%")

    print(f"\n[3/3] Saving…")
    (MODELS / "walk_forward.json").write_text(json.dumps(results, indent=2))
    print(f"[OK] walk_forward.json written ({len(results)} tournaments)")

    avg_ll = np.mean([r["log_loss"] for r in results.values()])
    avg_acc = np.mean([r["accuracy"] for r in results.values()])
    print(f"\n   Average across all WCs: log-loss={avg_ll:.3f}, accuracy={avg_acc*100:.1f}%")


if __name__ == "__main__":
    main()
