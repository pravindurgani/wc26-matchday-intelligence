"""
02_goal_model.py — Replace the W/D/L classifier with a proper goal model.

We train TWO Poisson regressors:
  home_goals_model: predicts E[home goals]
  away_goals_model: predicts E[away goals]

Then in simulation we sample scorelines from independent Poissons with the
predicted rates. This is the modern XGBoost equivalent of a Dixon-Coles model.
It naturally produces draws at realistic rates and gives correct goal-difference
distributions for FIFA tiebreakers.

Feature engineering improvements over the v1 classifier:
  - Exponential time-decay form (half-life 180 days) instead of flat last-10 avg
  - Attacking and defensive form computed separately
  - Importance buckets tuned per competition
  - All features computed using only pre-match information (no leakage)
"""
from __future__ import annotations

import json
import math
from collections import defaultdict, deque
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import log_loss, mean_poisson_deviance

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "data" / "processed"
RAW = ROOT / "data" / "raw"
MODELS = ROOT / "models"
MODELS.mkdir(parents=True, exist_ok=True)

TOURNAMENT_IMPORTANCE = {
    "Friendly": 0.10,
    "FIFA World Cup": 1.00,
    "FIFA World Cup qualification": 0.60,
    "UEFA Euro": 0.85,
    "UEFA Euro qualification": 0.55,
    "Copa América": 0.80,
    "African Cup of Nations": 0.75,
    "AFC Asian Cup": 0.70,
    "CONCACAF Gold Cup": 0.60,
    "UEFA Nations League": 0.55,
    "Confederations Cup": 0.65,
}
DEFAULT_IMPORTANCE = 0.30
HALF_LIFE_DAYS = 180.0  # exponential decay half-life for form features


def decayed_form(team_history: deque, ref_date: pd.Timestamp) -> dict:
    """Compute exponentially time-decayed attacking and defensive form.

    team_history items: {"date", "gf", "ga", "pts"}
    """
    if not team_history:
        return {"att_form": 1.0, "def_form": 1.0, "pts_form": 1.0, "n_recent": 0}
    total_w = 0.0
    att = def_ = pts = 0.0
    for m in team_history:
        days = max(0, (ref_date - m["date"]).days)
        w = 0.5 ** (days / HALF_LIFE_DAYS)
        att += w * m["gf"]
        def_ += w * m["ga"]
        pts += w * m["pts"]
        total_w += w
    if total_w == 0:
        return {"att_form": 1.0, "def_form": 1.0, "pts_form": 1.0, "n_recent": 0}
    return {
        "att_form": att / total_w,
        "def_form": def_ / total_w,
        "pts_form": pts / total_w,
        "n_recent": len(team_history),
    }


def build_features(matches: pd.DataFrame) -> pd.DataFrame:
    """Walk history once, computing features and updating Elo + form state."""
    from importlib import util
    spec = util.spec_from_file_location("p1", ROOT / "scripts" / "01_prepare_data.py")
    p1 = util.module_from_spec(spec)
    spec.loader.exec_module(p1)

    INITIAL_ELO = p1.INITIAL_ELO
    expected_score = p1.expected_score
    margin_multiplier = p1.margin_multiplier
    TOURNAMENT_K = p1.TOURNAMENT_K
    DEFAULT_K = p1.DEFAULT_K
    HOME_ADV = 65.0

    elo: dict[str, float] = defaultdict(lambda: INITIAL_ELO)
    history: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
    last_date: dict[str, pd.Timestamp] = {}

    rows: list[dict] = []
    matches = matches.sort_values("date").reset_index(drop=True)

    for r in matches.itertuples(index=False):
        if pd.isna(r.home_score) or pd.isna(r.away_score):
            continue
        h, a, date = r.home_team, r.away_team, r.date

        # ---- features (pre-match state) ----
        eh, ea_ = elo[h], elo[a]
        f_h = decayed_form(history[h], date)
        f_a = decayed_form(history[a], date)
        rest_h = (date - last_date[h]).days if h in last_date else 30
        rest_a = (date - last_date[a]).days if a in last_date else 30
        importance = TOURNAMENT_IMPORTANCE.get(r.tournament, DEFAULT_IMPORTANCE)

        rows.append({
            "date": date,
            "home_team": h, "away_team": a,
            "tournament": r.tournament, "neutral": bool(r.neutral),
            "elo_home": eh, "elo_away": ea_, "elo_diff": eh - ea_,
            "att_form_home": f_h["att_form"], "att_form_away": f_a["att_form"],
            "def_form_home": f_h["def_form"], "def_form_away": f_a["def_form"],
            "pts_form_home": f_h["pts_form"], "pts_form_away": f_a["pts_form"],
            "rest_home": min(rest_h, 60), "rest_away": min(rest_a, 60),
            "is_neutral": int(bool(r.neutral)),
            "importance": importance,
            "home_goals": int(r.home_score),
            "away_goals": int(r.away_score),
        })

        # ---- update state ----
        k = TOURNAMENT_K.get(r.tournament, DEFAULT_K)
        ha = 0.0 if r.neutral else HOME_ADV
        exp_h = expected_score(eh, ea_, ha)
        if r.home_score > r.away_score:
            sh, sa = 1.0, 0.0
            pts_h, pts_a = 3, 0
        elif r.home_score < r.away_score:
            sh, sa = 0.0, 1.0
            pts_h, pts_a = 0, 3
        else:
            sh = sa = 0.5
            pts_h = pts_a = 1
        mm = margin_multiplier(int(abs(r.home_score - r.away_score)))
        elo[h] = eh + k * mm * (sh - exp_h)
        elo[a] = ea_ + k * mm * (sa - (1 - exp_h))

        history[h].append({"date": date, "gf": r.home_score, "ga": r.away_score, "pts": pts_h})
        history[a].append({"date": date, "gf": r.away_score, "ga": r.home_score, "pts": pts_a})
        last_date[h] = date
        last_date[a] = date

    return pd.DataFrame(rows)


FEATURE_COLS = [
    "elo_home", "elo_away", "elo_diff",
    "att_form_home", "att_form_away",
    "def_form_home", "def_form_away",
    "pts_form_home", "pts_form_away",
    "rest_home", "rest_away",
    "is_neutral", "importance",
]


def fit_poisson(X_tr, y_tr, X_te, y_te, label: str):
    model = xgb.XGBRegressor(
        objective="count:poisson",
        n_estimators=500,
        max_depth=5,
        learning_rate=0.04,
        tree_method="hist",
        random_state=42,
        eval_metric="poisson-nloglik",
        n_jobs=-1,
    )
    model.fit(X_tr, y_tr, eval_set=[(X_te, y_te)], verbose=False)
    preds = model.predict(X_te)
    preds = np.clip(preds, 1e-6, None)
    dev = mean_poisson_deviance(np.maximum(y_te, 0.001), preds)
    print(f"      [{label}] mean Poisson deviance: {dev:.4f}  "
          f"mean(y)={y_te.mean():.3f}  mean(pred)={preds.mean():.3f}")
    return model, dev


def equivalent_wdl_logloss(home_model, away_model, X_te, y_home, y_away) -> float:
    """Convert predicted lambdas to W/D/L probabilities via Skellam-style Poisson
    score-matrix, then compute log-loss against actual outcomes."""
    lam_h = np.clip(home_model.predict(X_te), 1e-6, 10)
    lam_a = np.clip(away_model.predict(X_te), 1e-6, 10)

    # Build score matrix up to 15-15.
    # R14 C2: bumped 8 → 15 to align with production sim's max_g=15 (R12 MED)
    # and 04_evaluate.py's lambdas_to_wdl default (R14 C1). At λ=4 (clip max
    # is 10 here), Poisson tail mass above 8 was ~5%; above 15 it's ~1e-3.
    # Training-time WDL log-loss diagnostic now uses the same truncation as
    # the downstream production sim, eliminating a cross-pipeline drift.
    max_g = 15
    proba = np.zeros((len(X_te), 3))  # away, draw, home
    for i in range(len(X_te)):
        p_h = np.array([math.exp(-lam_h[i]) * lam_h[i] ** k / math.factorial(k) for k in range(max_g + 1)])
        p_a = np.array([math.exp(-lam_a[i]) * lam_a[i] ** k / math.factorial(k) for k in range(max_g + 1)])
        # Normalize (truncation correction)
        p_h /= p_h.sum(); p_a /= p_a.sum()
        mat = np.outer(p_h, p_a)
        p_home_win = np.tril(mat, -1).sum()
        p_draw = np.trace(mat)
        p_away_win = np.triu(mat, 1).sum()
        proba[i] = [p_away_win, p_draw, p_home_win]

    y = np.array([2 if h > a else (1 if h == a else 0) for h, a in zip(y_home, y_away)])
    return log_loss(y, proba)


def main() -> None:
    print("[1/4] Loading clean matches & building features…")
    df = pd.read_parquet(PROC / "matches_clean.parquet")
    feats = build_features(df)
    feats.to_parquet(PROC / "match_features_v2.parquet", index=False)
    print(f"      {len(feats):,} feature rows produced.")

    train_pool = feats[feats["date"] >= "1990-01-01"].copy()
    print(f"      Modern subset: {len(train_pool):,} matches.")

    # Chronological split: last 15% as test
    cutoff = int(len(train_pool) * 0.85)
    train_pool_sorted = train_pool.sort_values("date").reset_index(drop=True)
    X = train_pool_sorted[FEATURE_COLS].values
    yh = train_pool_sorted["home_goals"].values
    ya = train_pool_sorted["away_goals"].values

    X_tr, X_te = X[:cutoff], X[cutoff:]
    yh_tr, yh_te = yh[:cutoff], yh[cutoff:]
    ya_tr, ya_te = ya[:cutoff], ya[cutoff:]

    print(f"[2/4] Training home-goals Poisson regressor on {len(X_tr):,} rows…")
    home_model, dev_h = fit_poisson(X_tr, yh_tr, X_te, yh_te, "HOME")

    print(f"[3/4] Training away-goals Poisson regressor on {len(X_tr):,} rows…")
    away_model, dev_a = fit_poisson(X_tr, ya_tr, X_te, ya_te, "AWAY")

    print("[4/4] Equivalent W/D/L log-loss via Skellam matrix…")
    ll = equivalent_wdl_logloss(home_model, away_model, X_te, yh_te, ya_te)
    print(f"      Implied W/D/L log-loss: {ll:.4f}  (v1 classifier was 0.883)")

    fi_home = dict(zip(FEATURE_COLS, home_model.feature_importances_.tolist()))
    fi_away = dict(zip(FEATURE_COLS, away_model.feature_importances_.tolist()))
    fi_home_sorted = dict(sorted(fi_home.items(), key=lambda kv: kv[1], reverse=True))
    print("\n      Home-goals feature importances:")
    for k, v in fi_home_sorted.items():
        print(f"      {k:<22s} {v:.4f}")

    joblib.dump(home_model, MODELS / "home_goals_model.joblib")
    joblib.dump(away_model, MODELS / "away_goals_model.joblib")
    (MODELS / "feature_cols_v2.json").write_text(json.dumps(FEATURE_COLS))
    (MODELS / "metrics_v2.json").write_text(json.dumps({
        "home_poisson_deviance": float(dev_h),
        "away_poisson_deviance": float(dev_a),
        "implied_wdl_log_loss": float(ll),
        "n_train": int(len(X_tr)),
        "n_test": int(len(X_te)),
        "feature_importances_home": fi_home_sorted,
        "feature_importances_away": dict(sorted(fi_away.items(), key=lambda kv: kv[1], reverse=True)),
        "model_type": "Dixon-Coles-style XGBoost Poisson regressors (independent)",
    }, indent=2))
    print(f"\n[OK] Models saved to {MODELS}")


if __name__ == "__main__":
    main()
