"""
03_simulate.py (v3) — Monte Carlo World Cup 2026 simulation.

Key v3 changes over v2 (addresses two careful reviews):

  • True Dixon-Coles τ low-score correction on the joint score matrix
  • Negative Binomial marginals (instead of Poisson) for over-dispersion —
    fixes the Spain/Argentina "favourite explosion" problem
  • Exact FIFA Annex C lookup (495 combinations) for third-place slot assignment
    — fail-loud if a combination is missing
  • Travel-fatigue penalty (Haversine + rest days), configurable on/off
  • Multi-seed Monte Carlo — outputs 5/50/95 percentile simulation ranges
    (sampling noise across independent rollouts; NOT parameter CIs)

CLI:
  python 03_simulate.py                  # default 5k sims × 5 seeds = 25k tourneys (= production)
  python 03_simulate.py --quick          # 2k sims × 3 seeds (faster)
  python 03_simulate.py --no-travel      # disable travel fatigue
  python 03_simulate.py --no-dispersion  # disable Negative Binomial (Poisson)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from scipy.stats import nbinom, poisson

# C3: grand-total cap for (live_team_state + matchday_intel) per team. Mirrors
# GRAND_TOTAL_CAP in apply_matchday_adjustments.py — kept in sync; if you
# change one, change both.
GRAND_TOTAL_CAP_ELO = 45.0

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
PROC = ROOT / "data" / "processed"
MODELS = ROOT / "models"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "live"))   # B.1: for apply_matchday_adjustments
from tiebreakers import rank_group, rank_third_placed

HOST_COUNTRIES = {"Mexico", "United States", "Canada"}
# P2: South Africa added — Johannesburg sits at ~1750m, so the SAFA squad
# pool plays much of its football at altitude and shouldn't take the full
# very-hot acclimatisation penalty either (handled separately via HEAT_ADAPTED).
ALTITUDE_ADAPTED = {"Bolivia", "Ecuador", "Peru", "Colombia", "Mexico", "South Africa"}

# P2: confederations + countries that train and play in extreme heat year-round
# and shouldn't take the European-temperate-side heat penalty in very-hot WC
# venues (Houston, Miami, Monterrey, Arlington indoor-cooling-or-not, etc.).
# CONMEBOL/CAF/Gulf-AFC pulled in via climate_penalty_elo's confed filter; this
# set covers explicit team-name overrides (AFC sides outside the Gulf or the
# CONCACAF teams that grew up in heat).
HEAT_ADAPTED = {
    "Saudi Arabia", "Qatar", "Iraq", "Jordan", "Iran",
    "United States", "Mexico", "Panama", "Haiti", "Curacao",
    "Australia",
}

# Model defaults — tunable via sensitivity analysis
DEFAULTS = dict(
    host_boost_home=50.0,        # Elo bump for host team playing at home
    host_boost_away=15.0,        # bump for host team playing in sister-host country
    altitude_penalty_scale=25.0, # Elo penalty for un-adapted teams at 2240m
    heat_penalty=15.0,           # Elo penalty for European/temperate teams in hot venues
    squad_value_cap=20.0,        # max ±Elo from squad-value prior
    pen_elo_slope=600.0,         # higher = penalties become more random (closer to 50/50)
    nb_dispersion=5.0,           # Negative Binomial dispersion (lower = more upset variance — empirically tuned)
    dc_rho=-0.13,                # Dixon-Coles τ parameter (negative boosts low-score draws)
    squad_value_cap_override=10.0,  # reduced from 20 — squad value should nudge, not double Elo's job
    lambda_noise_per_match=True, # add Gamma noise to lambdas per match per sim (more variance)
    lambda_noise_alpha=12.0,     # higher = less per-match lambda noise
    travel_per_1000km=4.0,       # Elo penalty per 1000km of recent travel
    short_rest_penalty=8.0,      # extra penalty if rest_days <= 3
)


# ---------- Geometry & venue adjustments -----------------------------------
def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def altitude_penalty_elo(team, altitude_m, cfg):
    if altitude_m < 1500: return 0.0
    if team in ALTITUDE_ADAPTED: return 0.0
    return -cfg["altitude_penalty_scale"] * (altitude_m / 2240.0)


def climate_penalty_elo(team, climate, confed, cfg):
    if "very_hot" not in (climate or ""): return 0.0
    if confed.get(team) in {"CONMEBOL", "CAF"}: return 0.0
    if confed.get(team) == "CONCACAF" and team != "Canada": return 0.0
    # P2: explicit team-name overrides for heat-adapted nations the confed
    # filter alone would miss (Gulf AFC sides, Australia, Iran, etc.).
    if team in HEAT_ADAPTED: return 0.0
    return -cfg["heat_penalty"]


def host_boost_elo(team, venue_country, cfg):
    if team in HOST_COUNTRIES and venue_country == team:
        return cfg["host_boost_home"]
    if team in HOST_COUNTRIES:
        return cfg["host_boost_away"]
    return 0.0


def squad_value_elo(team, squad_values, log_mean, log_std, cfg):
    v = squad_values.get(team)
    if not v or v <= 0: return 0.0
    z = (math.log(v) - log_mean) / max(log_std, 0.5)
    cap = cfg.get("squad_value_cap_override", cfg["squad_value_cap"])
    return cap * max(-2.0, min(2.0, z)) / 2.0


def travel_penalty_elo(prev_city_coord, new_city_coord, rest_days, cfg):
    if prev_city_coord is None: return 0.0
    dist = haversine_km(*prev_city_coord, *new_city_coord)
    penalty = -cfg["travel_per_1000km"] * (dist / 1000.0)
    if rest_days <= 3:
        penalty -= cfg["short_rest_penalty"]
    return penalty


# ---------- Dixon-Coles τ + Negative Binomial score matrix -----------------
def dc_tau(h, a, lam_h, lam_a, rho):
    if h == 0 and a == 0: return 1 - lam_h * lam_a * rho
    if h == 0 and a == 1: return 1 + lam_a * rho
    if h == 1 and a == 0: return 1 + lam_h * rho
    if h == 1 and a == 1: return 1 - rho
    return 1.0


def build_score_matrix(lam_h, lam_a, cfg, use_dispersion=True, max_g=10):
    """Joint distribution P[h, a]. Uses Negative Binomial marginals + DC τ correction."""
    if use_dispersion:
        a_disp = cfg["nb_dispersion"]
        p_h = a_disp / (a_disp + lam_h)
        p_a = a_disp / (a_disp + lam_a)
        ph = nbinom.pmf(np.arange(max_g + 1), a_disp, p_h)
        pa = nbinom.pmf(np.arange(max_g + 1), a_disp, p_a)
    else:
        ph = poisson.pmf(np.arange(max_g + 1), lam_h)
        pa = poisson.pmf(np.arange(max_g + 1), lam_a)
    ph = ph / ph.sum(); pa = pa / pa.sum()
    mat = np.outer(ph, pa)

    # Dixon-Coles τ correction for low-score outcomes
    rho = cfg["dc_rho"]
    for h in (0, 1):
        for a in (0, 1):
            mat[h, a] *= dc_tau(h, a, lam_h, lam_a, rho)
    mat = np.maximum(mat, 1e-12)
    mat /= mat.sum()

    return mat


def sample_from_matrix(mat, rng):
    flat = mat.flatten()
    idx = rng.choice(len(flat), p=flat)
    return int(idx // mat.shape[1]), int(idx % mat.shape[1])


def sample_score_with_noise(lam_h, lam_a, cfg, rng, max_g=10):
    """Per-match lambda noise via Gamma multipliers, then NB+τ matrix sample.
    Adds compound variance beyond just NB dispersion — captures the fact that
    on any given day a 'strong' team plays at 70-130% of average strength."""
    if cfg.get("lambda_noise_per_match"):
        alpha = cfg["lambda_noise_alpha"]
        noise_h = rng.gamma(alpha, 1.0 / alpha)
        noise_a = rng.gamma(alpha, 1.0 / alpha)
        lam_h = max(0.05, lam_h * noise_h)
        lam_a = max(0.05, lam_a * noise_a)
    mat = build_score_matrix(lam_h, lam_a, cfg, use_dispersion=cfg["use_dispersion"], max_g=max_g)
    return sample_from_matrix(mat, rng)


def wdl_from_matrix(mat):
    p_home = float(np.tril(mat, -1).sum())
    p_draw = float(np.trace(mat))
    p_away = float(np.triu(mat, 1).sum())
    return p_home, p_draw, p_away


# ---------- Goal model wrappers --------------------------------------------
def predict_lambdas(home, away, home_model, away_model, feature_cols, elo, form_cache,
                    home_elo_bonus=0.0, away_elo_bonus=0.0,
                    is_neutral=True, importance=1.0, rest_h=7, rest_a=7):
    e_h = elo.get(home, 1500) + home_elo_bonus
    e_a = elo.get(away, 1500) + away_elo_bonus
    f_h = form_cache.get(home, {"att_form": 1.5, "def_form": 1.2, "pts_form": 1.0})
    f_a = form_cache.get(away, {"att_form": 1.5, "def_form": 1.2, "pts_form": 1.0})
    feats = {
        "elo_home": e_h, "elo_away": e_a, "elo_diff": e_h - e_a,
        "att_form_home": f_h["att_form"], "att_form_away": f_a["att_form"],
        "def_form_home": f_h["def_form"], "def_form_away": f_a["def_form"],
        "pts_form_home": f_h["pts_form"], "pts_form_away": f_a["pts_form"],
        "rest_home": rest_h, "rest_away": rest_a,
        "is_neutral": int(is_neutral), "importance": importance,
    }
    x = np.array([[feats[c] for c in feature_cols]])
    lam_h = float(np.clip(home_model.predict(x)[0], 0.05, 7.0))
    lam_a = float(np.clip(away_model.predict(x)[0], 0.05, 7.0))
    return lam_h, lam_a


def precompute_form_cache(matches_df, teams, ref_date, half_life_days=180.0):
    out = {}
    for team in teams:
        mask = ((matches_df["home_team"] == team) | (matches_df["away_team"] == team)) & \
               (matches_df["date"] < ref_date)
        recent = matches_df.loc[mask].tail(20)
        if recent.empty:
            out[team] = {"att_form": 1.5, "def_form": 1.2, "pts_form": 1.0}; continue
        total_w = att = def_ = pts = 0.0
        for r in recent.itertuples(index=False):
            days = max(0, (ref_date - r.date).days)
            w = 0.5 ** (days / half_life_days)
            if r.home_team == team: gf, ga = r.home_score, r.away_score
            else: gf, ga = r.away_score, r.home_score
            p = 3 if gf > ga else (1 if gf == ga else 0)
            att += w * gf; def_ += w * ga; pts += w * p; total_w += w
        out[team] = {"att_form": att / total_w, "def_form": def_ / total_w, "pts_form": pts / total_w}
    return out


# ---------- Knockout resolution --------------------------------------------
def resolve_knockout(mat_90, lam_h, lam_a, elo_h, elo_a, cfg, rng):
    """Returns (h, a, decided_by)."""
    # If lambda-noise is on, resample with noise each time. Otherwise use precomputed matrix.
    if cfg.get("lambda_noise_per_match"):
        h, a = sample_score_with_noise(lam_h, lam_a, cfg, rng)
    else:
        h, a = sample_from_matrix(mat_90, rng)
    if h != a:
        return h, a, "90"
    # 30-min ET — independent Poisson at 1/3 rates
    eh = int(rng.poisson(lam_h / 3))
    ea = int(rng.poisson(lam_a / 3))
    h += eh; a += ea
    if h != a:
        return h, a, "et"
    # Penalty shootout — small Elo edge
    p_home = 1 / (1 + 10 ** ((elo_a - elo_h) / cfg["pen_elo_slope"]))
    if rng.random() < p_home:
        return h + 1, a, "pens"
    else:
        return h, a + 1, "pens"


def decide_knockout(team_a, team_b, m_num, locked, mat, lam_h, lam_a, e_h, e_a, cfg, rng):
    """Return (home_score, away_score, winner_name) for one knockout match.

    A.3: collapses post-tournament uncertainty by using the REAL result for
    locked knockout matches (FT/AET/PEN) instead of re-simulating them.
    Without this, completed knockouts get re-sampled every Monte Carlo
    iteration — the simulator pretends Spain might lose to Slovakia 1000
    times after the match already happened.

    Locked-match handling:
      - The `winner` field stored by fetch_results (A.2) is the source of
        truth — it correctly reflects shootout outcomes that score
        comparison alone can't decode (0-0 (3-0 pens) reads as 0-0).
      - Score comparison is the fallback only when `winner` is missing,
        which shouldn't happen for a real knockout but is defensive.

    `locked` is the {match_num: completed_record} dict from
    load_completed_matches(). `m_num` is the FIFA match number (73-104).
    """
    if m_num in locked:
        lk = locked[m_num]
        h = lk["home_score"]
        a = lk["away_score"]
        w = lk.get("winner")
        if w == "home":
            return h, a, team_a
        if w == "away":
            return h, a, team_b
        # No winner field on a locked knockout — fall back to score comparison.
        # In practice fetch_results refuses to lock a PEN match without a
        # winner (see A.2), so this path triggers only for group-stage drafts
        # accidentally indexed here.
        # M6: log loud so this defensive branch never silently swallows a
        # bad fixture record. Awarding team_b on a tie without a winner is a
        # last-resort guess — the operator should see it.
        print(f"[decide_knockout] WARN: locked knockout m={m_num} {team_a} vs {team_b} "
              f"has no winner field; tie ({h}-{a}) defaults to {'team_a' if h > a else 'team_b'}",
              file=sys.stderr)
        return h, a, (team_a if h > a else team_b)
    # Not locked — sample as before.
    h, a, _ = resolve_knockout(mat, lam_h, lam_a, e_h, e_a, cfg, rng)
    return h, a, (team_a if h > a else team_b)


# ---------- Annex C lookup --------------------------------------------------
def lookup_third_place_assignment(qualifying_thirds, annex_c_table):
    """Given the 8 qualifying third-placers, look up their R32 slot assignment."""
    groups = sorted([t["group"] for t in qualifying_thirds])
    key = "".join(groups)
    if key not in annex_c_table:
        return None
    mapping = annex_c_table[key]   # "3A" → "M79", etc.
    out = {}
    for q in qualifying_thirds:
        slot_key = f"3{q['group']}"
        out[mapping[slot_key]] = q
    return out


# ---------- Run a single seed ----------------------------------------------
def run_single_seed(seed, cfg, n_sims, ctx):
    rng = np.random.default_rng(seed)
    bracket = ctx["bracket"]
    annex_c = ctx["annex_c"]
    cfg_teams = ctx["cfg_teams"]
    fifa_pts = ctx["fifa_pts"]
    all_teams = ctx["all_teams"]

    # Counts
    finishes = defaultdict(lambda: defaultdict(int))
    qualified_count = defaultdict(int)
    r16_count = defaultdict(int)
    qf_count = defaultdict(int)
    semi_count = defaultdict(int)
    finalist_count = defaultdict(int)
    champion_count = defaultdict(int)
    third_place_winners = defaultdict(int)
    annex_c_misses = 0

    # Pre-parse R32 slots
    r32_slots_parsed = []
    for slot in bracket["r32_slots"]:
        r32_slots_parsed.append({
            "m": slot["match_num"], "slot_a": slot["slot_a"], "slot_b": slot["slot_b"],
            "next_match": slot["next_match"],
        })

    group_matrices = ctx["group_matrices"]
    knock_matrices = ctx["knock_matrices"]
    knock_lambdas = ctx["knock_lambdas"]
    # A.3: locked knockout results (M73-M104). Each entry is the dict from
    # load_completed_matches with {home_score, away_score, home_pens,
    # away_pens, winner, status}. Used by decide_knockout() to skip the
    # Monte Carlo sample for matches that have already happened.
    locked_knockouts = ctx.get("completed_matches", {}) or {}

    for sim in range(n_sims):
        # --- Sample all 72 group scorelines (locked if completed in live mode) ---
        sim_match_results = []
        for ml in group_matrices:
            if ml.get("locked_score"):
                hs = ml["locked_score"]["home_score"]
                as_ = ml["locked_score"]["away_score"]
            elif cfg.get("lambda_noise_per_match"):
                hs, as_ = sample_score_with_noise(ml["lam_home"], ml["lam_away"], cfg, rng)
            else:
                hs, as_ = sample_from_matrix(ml["matrix"], rng)
            sim_match_results.append({
                "group": ml["group"], "home": ml["home"], "away": ml["away"],
                "home_score": hs, "away_score": as_,
            })

        # --- Rank groups ---
        group_results = {}
        for g, teams in cfg_teams.items():
            g_matches = [r for r in sim_match_results if r["group"] == g]
            ranked = rank_group(teams, g_matches, fifa_pts)
            for pos, s in enumerate(ranked, 1):
                s["pos"] = pos; s["group"] = g
                finishes[s["name"]][pos] += 1
            group_results[g] = ranked

        winners = {g: gr[0]["name"] for g, gr in group_results.items()}
        runners = {g: gr[1]["name"] for g, gr in group_results.items()}
        all_thirds = [dict(gr[2], group=g) for g, gr in group_results.items()]
        ranked_thirds = rank_third_placed(all_thirds, fifa_pts)
        qualifying_thirds = ranked_thirds[:8]

        for n in winners.values(): qualified_count[n] += 1
        for n in runners.values(): qualified_count[n] += 1
        for q in qualifying_thirds: qualified_count[q["name"]] += 1

        # --- Annex C lookup (exact, fail-loud-but-fallback if missing) ---
        third_slot_map = lookup_third_place_assignment(qualifying_thirds, annex_c)
        if third_slot_map is None:
            annex_c_misses += 1
            # Fallback: assign by FIFA-ranking order to allowed slots (best third → best slot)
            slot_pools = ctx["slot_pools"]
            third_slot_map = {}
            unused = list(qualifying_thirds)
            for slot, pool in slot_pools.items():
                for q in sorted(unused, key=lambda x: -fifa_pts.get(x["name"], 0)):
                    if q["group"] in pool:
                        third_slot_map[slot] = q
                        unused.remove(q); break

        # --- Build R32 fixtures ---
        r32_fixtures = []
        for s in r32_slots_parsed:
            # slot_a
            sa = s["slot_a"]
            if sa.startswith("1"): t_a = winners[sa[1]]
            elif sa.startswith("2"): t_a = runners[sa[1]]
            else:  # third
                t_a = third_slot_map.get(f"M{s['m']}", {}).get("name")
            sb = s["slot_b"]
            if sb.startswith("1"): t_b = winners[sb[1]]
            elif sb.startswith("2"): t_b = runners[sb[1]]
            else:
                t_b = third_slot_map.get(f"M{s['m']}", {}).get("name")
            r32_fixtures.append({"m": s["m"], "team_a": t_a, "team_b": t_b})

        # --- Play R32 ---
        # A.3: decide_knockout() short-circuits to the locked real result
        # when ctx.completed_matches has the m_num, otherwise samples as
        # before. Group-stage Monte Carlo for the same n_sims still produces
        # the right uncertainty band for OTHER possible bracket paths.
        winners_by_match = {}
        for f in r32_fixtures:
            ta, tb = f["team_a"], f["team_b"]
            mat = knock_matrices[(ta, tb)]
            lam_h, lam_a, e_h, e_a = knock_lambdas[(ta, tb)]
            h, a, winner = decide_knockout(ta, tb, f["m"], locked_knockouts,
                                           mat, lam_h, lam_a, e_h, e_a, cfg, rng)
            winners_by_match[f["m"]] = winner
            r16_count[winner] += 1

        # --- R16 ---
        r16_winners = {}
        for f in bracket["r16_bracket"]:
            ma, mb = int(f["slot_a"][1:]), int(f["slot_b"][1:])
            ta, tb = winners_by_match[ma], winners_by_match[mb]
            mat = knock_matrices[(ta, tb)]
            lam_h, lam_a, e_h, e_a = knock_lambdas[(ta, tb)]
            h, a, winner = decide_knockout(ta, tb, f["match_num"], locked_knockouts,
                                           mat, lam_h, lam_a, e_h, e_a, cfg, rng)
            r16_winners[f["match_num"]] = winner
            qf_count[winner] += 1

        # --- QF ---
        qf_winners = {}
        for f in bracket["qf_bracket"]:
            ma, mb = int(f["slot_a"][1:]), int(f["slot_b"][1:])
            ta, tb = r16_winners[ma], r16_winners[mb]
            mat = knock_matrices[(ta, tb)]
            lam_h, lam_a, e_h, e_a = knock_lambdas[(ta, tb)]
            h, a, winner = decide_knockout(ta, tb, f["match_num"], locked_knockouts,
                                           mat, lam_h, lam_a, e_h, e_a, cfg, rng)
            qf_winners[f["match_num"]] = winner
            semi_count[winner] += 1

        # --- SF ---
        sf_winners = {}
        sf_losers = {}
        for f in bracket["sf_bracket"]:
            ma, mb = int(f["slot_a"][1:]), int(f["slot_b"][1:])
            ta, tb = qf_winners[ma], qf_winners[mb]
            mat = knock_matrices[(ta, tb)]
            lam_h, lam_a, e_h, e_a = knock_lambdas[(ta, tb)]
            h, a, winner = decide_knockout(ta, tb, f["match_num"], locked_knockouts,
                                           mat, lam_h, lam_a, e_h, e_a, cfg, rng)
            loser = tb if winner == ta else ta
            sf_winners[f["match_num"]] = winner
            sf_losers[f["match_num"]] = loser
            finalist_count[winner] += 1

        # --- Final ---
        final = bracket["final_and_third_place"]["final"]
        ma, mb = int(final["slot_a"][1:]), int(final["slot_b"][1:])
        ta, tb = sf_winners[ma], sf_winners[mb]
        mat = knock_matrices[(ta, tb)]
        lam_h, lam_a, e_h, e_a = knock_lambdas[(ta, tb)]
        h, a, champion = decide_knockout(ta, tb, final["match_num"], locked_knockouts,
                                         mat, lam_h, lam_a, e_h, e_a, cfg, rng)
        champion_count[champion] += 1

        # --- 3rd place ---
        tp = bracket["final_and_third_place"]["third_place"]
        ta = sf_losers[int(tp["slot_a"][1:])]
        tb = sf_losers[int(tp["slot_b"][1:])]
        mat = knock_matrices[(ta, tb)]
        lam_h, lam_a, e_h, e_a = knock_lambdas[(ta, tb)]
        h, a, tp_winner = decide_knockout(ta, tb, tp["match_num"], locked_knockouts,
                                          mat, lam_h, lam_a, e_h, e_a, cfg, rng)
        third_place_winners[tp_winner] += 1

    return {
        "seed": seed, "n_sims": n_sims, "annex_c_misses": annex_c_misses,
        "finishes": {t: dict(finishes[t]) for t in all_teams},
        "qualified": {t: qualified_count[t] for t in all_teams},
        "r16": {t: r16_count[t] for t in all_teams},
        "qf": {t: qf_count[t] for t in all_teams},
        "sf": {t: semi_count[t] for t in all_teams},
        "final": {t: finalist_count[t] for t in all_teams},
        "champion": {t: champion_count[t] for t in all_teams},
        "third_place": {t: third_place_winners[t] for t in all_teams},
    }


def compute_travel_penalties(cfg_data, distance_matrix, cfg):
    """For each scheduled group match, compute the Elo travel penalty for both teams.
    Penalty = travel_per_1000km × km/1000 + short_rest_penalty (if rest ≤ 3 days), capped.
    Returns {match_num: {"home_km": ..., "home_rest": ..., "home_penalty": ..., away_*}}.
    """
    venue_city_map = cfg_data["venue_city_map"]
    # Build per-team timeline of group matches in date order
    per_team_matches = defaultdict(list)
    for m in cfg_data["group_stage_schedule"]:
        per_team_matches[m["home"]].append(m)
        per_team_matches[m["away"]].append(m)
    for t in per_team_matches:
        per_team_matches[t].sort(key=lambda x: x["date"])

    # Compute travel per fixture per team
    fixture_travel = {}
    for m in cfg_data["group_stage_schedule"]:
        fixture_travel[m["m"]] = {"home_km": 0, "away_km": 0,
                                  "home_rest": 30, "away_rest": 30,
                                  "home_penalty": 0.0, "away_penalty": 0.0}

    for team, matches in per_team_matches.items():
        for i, m in enumerate(matches):
            if i == 0:
                continue
            prev = matches[i - 1]
            prev_city = venue_city_map.get(prev["venue"], prev["venue"])
            cur_city = venue_city_map.get(m["venue"], m["venue"])
            try:
                km = distance_matrix["distance_km"][prev_city][cur_city]
            except KeyError:
                km = 0
            d_prev = pd.Timestamp(prev["date"])
            d_cur = pd.Timestamp(m["date"])
            rest = (d_cur - d_prev).days
            penalty = -cfg["travel_per_1000km"] * (km / 1000.0)
            if rest <= 3:
                penalty -= cfg["short_rest_penalty"]
            penalty = max(penalty, -25.0)  # cap

            slot = "home" if m["home"] == team else "away"
            fixture_travel[m["m"]][f"{slot}_km"] = km
            fixture_travel[m["m"]][f"{slot}_rest"] = rest
            fixture_travel[m["m"]][f"{slot}_penalty"] = penalty
    return fixture_travel


def load_injury_adjustments(path):
    """Read team_adjustments.json, filter expired, return {team: total_elo_penalty}."""
    if not path.exists():
        return {}
    d = json.loads(path.read_text())
    now = pd.Timestamp.now(tz="UTC")
    out = defaultdict(float)
    for adj in d.get("adjustments", []):
        exp = adj.get("expires_at")
        if exp:
            try:
                if pd.Timestamp(exp, tz="UTC") < now:
                    continue
            except Exception:
                pass
        amount = adj.get("adjustment_elo", 0)
        if adj.get("status") == "doubtful":
            amount = amount * 0.5
        out[adj["team"]] += amount
    return dict(out)


def load_completed_matches(path):
    """Read results_2026.json, return {match_num: completed_record}.

    A.3: also captures `home_pens`, `away_pens`, and `winner` so the
    simulator can lock knockout matches correctly. For group fixtures
    these fields are None and behavior is unchanged. For knockouts,
    `winner` ("home"/"away") is the source of truth — see decide_knockout().
    """
    if not path.exists():
        return {}
    d = json.loads(path.read_text())
    out = {}
    for m in d.get("completed_matches", []):
        out[m["m"]] = {
            "home_score": m["home_score"],
            "away_score": m["away_score"],
            "home_pens": m.get("home_pens"),
            "away_pens": m.get("away_pens"),
            "winner": m.get("winner"),
            "status": m.get("status"),
        }
    return out


def load_live_team_state(path):
    """Read live_team_state.json, return {team: delta_elo}. Empty if missing/disabled."""
    if not path.exists():
        return {}
    d = json.loads(path.read_text())
    return d.get("deltas", {})


def precompute_context(cfg_data, bracket, annex_c, squad_vals, elo, home_model, away_model,
                       feature_cols, matches_df, cfg, distance_matrix=None,
                       injury_adjustments=None, completed_matches=None,
                       live_team_state=None):
    """Build everything that's identical across simulations."""
    venue_city_map = cfg_data["venue_city_map"]
    host_city_meta = {hc["city"]: hc for hc in cfg_data["host_cities"]}
    all_teams = [t for grp in cfg_data["groups"].values() for t in grp]

    injury_adjustments = injury_adjustments or {}
    completed_matches = completed_matches or {}
    live_team_state = live_team_state or {}

    sv_logs = [math.log(v) for v in squad_vals.values() if v > 0]
    sv_log_mean, sv_log_std = float(np.mean(sv_logs)), float(np.std(sv_logs))

    form_cache = precompute_form_cache(matches_df, all_teams, pd.Timestamp("2026-06-11"))

    confed_map = CONFED_MAP

    # B.3: matchday-intelligence module is now the single source of truth for
    # injuries (API-Football + manual overlay), weather, lineups, and stats
    # proxy. The legacy `injury_adjustments` dict is still computed upstream
    # for the `injury_adjustments_active` field in the output JSON (so the
    # dashboard can show "what we knew at sim time"), but it is NOT added to
    # elo_eff_base — that would double-count, since _matchday_intel pulls
    # the same source via injuries_2026.json + team_adjustments.json overlay.
    try:
        from apply_matchday_adjustments import get_team_elo_adjustment as _matchday_intel_raw
    except ImportError:
        # If the module is missing for any reason (test env, partial
        # checkout), fail closed: zero adjustment.
        def _matchday_intel_raw(team, match_id=None, reload=False):
            return 0.0

    # H3: gate matchday intel behind --no-adjustments. Without this, the
    # baseline run (which is meant to be the clean static reference that
    # live_delta diffs against) silently picks up whatever weather/lineup
    # signal exists at sim time, contaminating the delta.
    use_intel = bool(cfg.get("use_adjustments", True))

    def _matchday_intel(team, match_id=None):
        if not use_intel:
            return 0.0
        return _matchday_intel_raw(team, match_id)

    # C2/C3: compute tournament-wide intel and clamp combined (live_team_state
    # + tournament_wide_intel) at GRAND_TOTAL_CAP. This becomes the base used
    # by knockout matrices (whose venue/match context is unknown until the
    # bracket plays out). Group matches get a *match-scoped* delta added on
    # top, then re-clamped against the cap per-match.
    tournament_wide_intel = {t: float(_matchday_intel(t)) for t in all_teams}
    live_state_by_team = {t: float(live_team_state.get(t, 0.0)) for t in all_teams}
    base_intel_plus_state = {}
    grand_cap_applied: dict[str, dict] = {}
    for t in all_teams:
        raw_combined = live_state_by_team[t] + tournament_wide_intel[t]
        capped = max(-GRAND_TOTAL_CAP_ELO, min(GRAND_TOTAL_CAP_ELO, raw_combined))
        base_intel_plus_state[t] = capped
        if abs(raw_combined - capped) > 1e-6:
            grand_cap_applied[t] = {
                "raw_elo": round(raw_combined, 3),
                "applied_elo": round(capped, 3),
                "cap_elo": GRAND_TOTAL_CAP_ELO,
                "live_team_state": round(live_state_by_team[t], 3),
                "tournament_wide_intel": round(tournament_wide_intel[t], 3),
            }

    elo_eff_base = {
        t: (elo.get(t, 1500)
            + squad_value_elo(t, squad_vals, sv_log_mean, sv_log_std, cfg)
            + base_intel_plus_state[t])
        for t in all_teams
    }

    # H2: weather forecasts supersede the static climate prior. Build a set
    # of (match_id, team) pairs that have a *real* (non-fallback) weather
    # entry so the per-match loop can zero the static heat penalty there.
    forecast_weather_keys: set[tuple[int, str]] = set()
    try:
        weather_raw_path = ROOT / "data" / "live" / "weather_2026.json"
        if weather_raw_path.exists():
            _wdata = json.loads(weather_raw_path.read_text())
            for w in (_wdata.get("weather") or []):
                if w.get("confidence") == "static_fallback":
                    continue
                m_id = w.get("match_id")
                if m_id is None:
                    continue
                for side in ("home", "away"):
                    t_name = w.get(f"{side}_team")
                    if t_name:
                        forecast_weather_keys.add((m_id, t_name))
    except Exception as _e:
        print(f"[03_simulate] weather precedence: ignored failure reading weather_2026.json: {_e}")

    # Travel pre-computation
    fixture_travel = {}
    if cfg.get("use_travel") and distance_matrix is not None:
        fixture_travel = compute_travel_penalties(cfg_data, distance_matrix, cfg)

    # Group match precomputed matrices + lambdas
    group_matrices = []
    per_match_intel_meta: list[dict] = []
    for m in cfg_data["group_stage_schedule"]:
        h, a = m["home"], m["away"]
        m_id = m["m"]
        venue = m["venue"]
        city = venue_city_map.get(venue, venue)
        city_meta = host_city_meta.get(city, {})
        venue_country = city_meta.get("country")
        altitude = city_meta.get("altitude_m", 0)
        climate = city_meta.get("climate", "")
        host_b_h = host_boost_elo(h, venue_country, cfg)
        host_b_a = host_boost_elo(a, venue_country, cfg)
        alt_h = altitude_penalty_elo(h, altitude, cfg)
        alt_a = altitude_penalty_elo(a, altitude, cfg)
        clim_h = climate_penalty_elo(h, climate, confed_map, cfg)
        clim_a = climate_penalty_elo(a, climate, confed_map, cfg)
        # H2: forecast supersedes climate prior. If we have a non-fallback
        # weather entry for this team-match, zero the static climate term —
        # the live weather layer (heat_pp etc.) will reapply the *current*
        # acclimatisation cost. Without this, hot-venue UEFA teams would
        # take the static −15 AND the live weather penalty on top of it.
        if (m_id, h) in forecast_weather_keys:
            clim_h = 0.0
        if (m_id, a) in forecast_weather_keys:
            clim_a = 0.0
        travel = fixture_travel.get(m_id, {})
        trav_h = travel.get("home_penalty", 0.0)
        trav_a = travel.get("away_penalty", 0.0)

        # C2: match-scoped matchday intel (weather, lineups for this fixture).
        # The function returns tournament-wide + match-scoped; subtract the
        # already-baked-in tournament-wide component to get the delta.
        match_total_h = float(_matchday_intel(h, m_id))
        match_total_a = float(_matchday_intel(a, m_id))
        match_scoped_h = match_total_h - tournament_wide_intel.get(h, 0.0)
        match_scoped_a = match_total_a - tournament_wide_intel.get(a, 0.0)
        # C3: respect GRAND_TOTAL_CAP for the per-match total (live + intel).
        raw_combined_h = live_state_by_team.get(h, 0.0) + match_total_h
        raw_combined_a = live_state_by_team.get(a, 0.0) + match_total_a
        capped_combined_h = max(-GRAND_TOTAL_CAP_ELO, min(GRAND_TOTAL_CAP_ELO, raw_combined_h))
        capped_combined_a = max(-GRAND_TOTAL_CAP_ELO, min(GRAND_TOTAL_CAP_ELO, raw_combined_a))
        # The bonus added on top of elo_eff_base (which already includes
        # base_intel_plus_state for the team) is the difference between the
        # per-match capped combined and the precomputed base.
        intel_match_bonus_h = capped_combined_h - base_intel_plus_state.get(h, 0.0)
        intel_match_bonus_a = capped_combined_a - base_intel_plus_state.get(a, 0.0)

        e_h_bonus = host_b_h + alt_h + clim_h + trav_h + intel_match_bonus_h
        e_a_bonus = host_b_a + alt_a + clim_a + trav_a + intel_match_bonus_a

        per_match_intel_meta.append({
            "match_id": m_id,
            "home_match_total_intel": round(match_total_h, 3),
            "away_match_total_intel": round(match_total_a, 3),
            "home_match_scoped_delta": round(match_scoped_h, 3),
            "away_match_scoped_delta": round(match_scoped_a, 3),
            "home_intel_bonus_applied": round(intel_match_bonus_h, 3),
            "away_intel_bonus_applied": round(intel_match_bonus_a, 3),
            "home_grand_cap_hit": abs(raw_combined_h - capped_combined_h) > 1e-6,
            "away_grand_cap_hit": abs(raw_combined_a - capped_combined_a) > 1e-6,
            "climate_static_h_zeroed_by_forecast": (m_id, h) in forecast_weather_keys,
            "climate_static_a_zeroed_by_forecast": (m_id, a) in forecast_weather_keys,
        })

        is_neutral = (venue_country != h)
        lam_h, lam_a = predict_lambdas(
            h, a, home_model, away_model, feature_cols,
            {h: elo_eff_base[h], a: elo_eff_base[a]}, form_cache,
            home_elo_bonus=e_h_bonus, away_elo_bonus=e_a_bonus,
            is_neutral=is_neutral, importance=1.0)
        mat = build_score_matrix(lam_h, lam_a, cfg, use_dispersion=cfg["use_dispersion"])
        p_home, p_draw, p_away = wdl_from_matrix(mat)
        group_matrices.append({
            "m": m["m"], "date": m["date"], "time": m["time"], "group": m["group"],
            "home": h, "away": a, "venue": venue, "venue_country": venue_country,
            "altitude_m": altitude, "climate": climate,
            "stage": "group",
            "lam_home": lam_h, "lam_away": lam_a,
            "p_home_win": p_home, "p_draw": p_draw, "p_away_win": p_away,
            "elo_home": float(elo.get(h, 1500)), "elo_away": float(elo.get(a, 1500)),
            "effective_elo_home": elo_eff_base[h] + e_h_bonus,
            "effective_elo_away": elo_eff_base[a] + e_a_bonus,
            "matrix": mat,
            "home_travel_km": travel.get("home_km", 0),
            "away_travel_km": travel.get("away_km", 0),
            "home_rest_days": travel.get("home_rest", 0),
            "away_rest_days": travel.get("away_rest", 0),
            "home_travel_penalty": float(trav_h),
            "away_travel_penalty": float(trav_a),
            "locked_score": completed_matches.get(m["m"]),
        })

    # P1-D: knockout fixtures export. Pre-resolution we don't know the actual
    # teams in each slot (e.g. "1A vs 3A/B/C/D/F"), so we can't give a per-
    # fixture probability distribution — but the dashboard needs to render
    # the bracket with venue + date so the Matches view doesn't go dark on
    # 28 Jun. Once fetch_results locks an FT/AET/PEN knockout, the
    # `locked_score` field carries through and the card displays the real
    # outcome. Venue metadata is included so the existing tag chips (hot,
    # altitude, travel) still light up for knockout cards.
    def _venue_meta(venue_str: str) -> tuple[str, dict]:
        """Resolve a bracket `venue` string to (city_name, host_city_meta)."""
        city = venue_city_map.get(venue_str, venue_str.split(",")[0].strip())
        return city, host_city_meta.get(city, {})

    knockout_predictions: list[dict] = []
    STAGE_LABELS = {
        "r32_slots":   "r32",
        "r16_bracket": "r16",
        "qf_bracket":  "qf",
        "sf_bracket":  "sf",
    }
    for key, stage_tag in STAGE_LABELS.items():
        for slot in bracket.get(key, []):
            m_num = slot.get("match_num")
            venue = slot.get("venue") or ""
            city, city_meta = _venue_meta(venue)
            locked = completed_matches.get(m_num) if m_num is not None else None
            knockout_predictions.append({
                "m": m_num,
                "stage": stage_tag,
                "date": slot.get("date", ""),
                "time": slot.get("time", ""),
                "group": stage_tag.upper(),
                "venue": venue,
                "venue_country": city_meta.get("country"),
                "altitude_m": city_meta.get("altitude_m", 0),
                "climate": city_meta.get("climate", ""),
                # Slot codes (e.g. "1A", "3A/B/C/D/F") — show as placeholder
                # team names until a locked_score arrives with the actual teams.
                "slot_a": slot.get("slot_a"),
                "slot_b": slot.get("slot_b"),
                "home": (locked or {}).get("home") or slot.get("slot_a") or "TBD",
                "away": (locked or {}).get("away") or slot.get("slot_b") or "TBD",
                "locked_score": locked,
            })
    # Final + third place sit in a different sub-tree.
    ft_block = bracket.get("final_and_third_place", {}) or {}
    for tag, key in (("3rd", "third_place"), ("final", "final")):
        slot = ft_block.get(key)
        if not slot:
            continue
        m_num = slot.get("match_num")
        venue = slot.get("venue") or ""
        city, city_meta = _venue_meta(venue)
        locked = completed_matches.get(m_num) if m_num is not None else None
        knockout_predictions.append({
            "m": m_num,
            "stage": tag,
            "date": slot.get("date", ""),
            "time": slot.get("time", ""),
            "group": tag.upper(),
            "venue": venue,
            "venue_country": city_meta.get("country"),
            "altitude_m": city_meta.get("altitude_m", 0),
            "climate": city_meta.get("climate", ""),
            "slot_a": slot.get("slot_a"),
            "slot_b": slot.get("slot_b"),
            "home": (locked or {}).get("home") or slot.get("slot_a") or "TBD",
            "away": (locked or {}).get("away") or slot.get("slot_b") or "TBD",
            "locked_score": locked,
        })

    # Knockout matrices for every team pair (neutral).
    # H4: symmetrize so P(A beats B | slot order (A,B)) equals
    # P(A beats B | slot order (B,A)). Residual home-side bias in the
    # goal model would otherwise leak into whichever team happens to be
    # the bracket's "slot A". We compute the raw model output for every
    # directed pair first, then average each pair with its transpose.
    raw_lams: dict[tuple[str, str], tuple[float, float]] = {}
    raw_mats: dict[tuple[str, str], np.ndarray] = {}
    for h in all_teams:
        for a in all_teams:
            if h == a:
                continue
            e_h = elo_eff_base[h]; e_a = elo_eff_base[a]
            host_b_h = cfg["host_boost_away"] if h in HOST_COUNTRIES else 0.0
            host_b_a = cfg["host_boost_away"] if a in HOST_COUNTRIES else 0.0
            lam_h, lam_a = predict_lambdas(
                h, a, home_model, away_model, feature_cols,
                {h: e_h, a: e_a}, form_cache,
                home_elo_bonus=host_b_h, away_elo_bonus=host_b_a,
                is_neutral=True, importance=1.0)
            mat = build_score_matrix(lam_h, lam_a, cfg, use_dispersion=cfg["use_dispersion"])
            raw_lams[(h, a)] = (lam_h, lam_a, e_h + host_b_h, e_a + host_b_a)
            raw_mats[(h, a)] = mat

    knock_matrices = {}
    knock_lambdas = {}
    for h in all_teams:
        for a in all_teams:
            if h == a:
                continue
            mat_ha = raw_mats[(h, a)]
            mat_ah = raw_mats[(a, h)]
            # Symmetrize: average P[(h_score, a_score) | (h,a)] with
            # P[(a_score, h_score) | (a,h)] swapped via transpose. This
            # equalises directed-pair outcomes without changing the joint
            # score-distribution shape.
            mat_sym = 0.5 * (mat_ha + mat_ah.T)
            mat_sym = np.maximum(mat_sym, 1e-12)
            mat_sym /= mat_sym.sum()
            lam_h_ha, lam_a_ha, eff_h_ha, eff_a_ha = raw_lams[(h, a)]
            lam_h_ah, lam_a_ah, eff_h_ah, eff_a_ah = raw_lams[(a, h)]
            # Symmetric lambdas: average h's two views (as slot-A and slot-B)
            # of the same matchup. Same for a.
            lam_h_sym = 0.5 * (lam_h_ha + lam_a_ah)
            lam_a_sym = 0.5 * (lam_a_ha + lam_h_ah)
            eff_h_sym = 0.5 * (eff_h_ha + eff_a_ah)
            eff_a_sym = 0.5 * (eff_a_ha + eff_h_ah)
            knock_matrices[(h, a)] = mat_sym
            knock_lambdas[(h, a)] = (lam_h_sym, lam_a_sym, eff_h_sym, eff_a_sym)

    slot_pools = {
        "M74": set("ABCDF"), "M77": set("CDFGH"), "M79": set("CEFHI"),
        "M80": set("EHIJK"), "M81": set("BEFIJ"), "M82": set("AEHIJ"),
        "M85": set("EFGIJ"), "M87": set("DEIJL"),
    }

    return {
        "bracket": bracket, "annex_c": annex_c["table"],
        "cfg_teams": cfg_data["groups"], "fifa_pts": cfg_data["fifa_rankings_june_2026"],
        "all_teams": all_teams, "group_matrices": group_matrices,
        "knock_matrices": knock_matrices, "knock_lambdas": knock_lambdas,
        "slot_pools": slot_pools,
        # A.3: knockout-aware locked-results lookup. Group fixtures already
        # get locked via group_matrices[i]["locked_score"] at line ~599;
        # knockouts are looked up by match_num inside run_single_seed.
        "completed_matches": completed_matches,
        # C3: per-team report of any clamp at GRAND_TOTAL_CAP.
        "grand_cap_applied": grand_cap_applied,
        # C2 audit: per-match intel bonus values (for dashboard inspection).
        "per_match_intel_meta": per_match_intel_meta,
        # P1-D: knockout fixture metadata (32 fixtures: 16 R32 + 8 R16 + 4 QF
        # + 2 SF + 3rd + final). Pre-resolution slots carry placeholder
        # labels (e.g. "1A", "W101"); locked results promote them to real
        # team names.
        "knockout_predictions": knockout_predictions,
    }


CONFED_MAP = {
  "Argentina": "CONMEBOL", "Brazil": "CONMEBOL", "Colombia": "CONMEBOL",
  "Uruguay": "CONMEBOL", "Ecuador": "CONMEBOL", "Paraguay": "CONMEBOL",
  "England": "UEFA", "France": "UEFA", "Spain": "UEFA", "Portugal": "UEFA",
  "Germany": "UEFA", "Netherlands": "UEFA", "Belgium": "UEFA", "Croatia": "UEFA",
  "Switzerland": "UEFA", "Norway": "UEFA", "Sweden": "UEFA", "Austria": "UEFA",
  "Czechia": "UEFA", "Scotland": "UEFA", "Italy": "UEFA",
  "Bosnia and Herzegovina": "UEFA", "Turkey": "UEFA",
  "United States": "CONCACAF", "Mexico": "CONCACAF", "Canada": "CONCACAF",
  "Panama": "CONCACAF", "Haiti": "CONCACAF", "Curacao": "CONCACAF",
  "Morocco": "CAF", "Egypt": "CAF", "Senegal": "CAF", "Ivory Coast": "CAF",
  "Tunisia": "CAF", "Algeria": "CAF", "DR Congo": "CAF", "Cape Verde": "CAF",
  "Ghana": "CAF", "South Africa": "CAF",
  "Japan": "AFC", "South Korea": "AFC", "Iran": "AFC", "Australia": "AFC",
  "Saudi Arabia": "AFC", "Qatar": "AFC", "Iraq": "AFC", "Jordan": "AFC",
  "Uzbekistan": "AFC", "New Zealand": "OFC",
}


def aggregate_runs(runs, all_teams, n_per_run):
    """From per-seed counts compute mean + 5/50/95 percentiles per team per outcome."""
    out_per_team = {}
    for t in all_teams:
        out_per_team[t] = {}
        for key in ("qualified", "r16", "qf", "sf", "final", "champion", "third_place"):
            vals = np.array([r[key].get(t, 0) / n_per_run for r in runs])
            out_per_team[t][key] = {
                "mean": float(vals.mean()),
                "p05": float(np.percentile(vals, 5)),
                "p50": float(np.percentile(vals, 50)),
                "p95": float(np.percentile(vals, 95)),
                "std": float(vals.std()),
            }
        # finishes (positions)
        for pos in (1, 2, 3, 4):
            vals = np.array([r["finishes"].get(t, {}).get(pos, 0) / n_per_run for r in runs])
            out_per_team[t][f"finish_{pos}"] = {
                "mean": float(vals.mean()), "p05": float(np.percentile(vals, 5)),
                "p50": float(np.percentile(vals, 50)), "p95": float(np.percentile(vals, 95)),
            }
    return out_per_team


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="2k sims × 3 seeds (faster)")
    parser.add_argument("--no-travel", action="store_true", help="disable travel fatigue")
    parser.add_argument("--no-dispersion", action="store_true", help="use Poisson instead of NB")
    parser.add_argument("--no-adjustments", action="store_true", help="ignore injury/suspension layer")
    parser.add_argument("--live", action="store_true", help="enable live mode (uses results_2026.json)")
    parser.add_argument("--out", default="predictions.json", help="output filename")
    parser.add_argument("--seeds", type=int, default=5, help="number of MC seeds")
    parser.add_argument("--sims", type=int, default=5000,
                        help="sims per seed (production = 5000, total 25k with default 5 seeds)")
    args = parser.parse_args()

    n_seeds = args.seeds if not args.quick else 3
    n_sims = args.sims if not args.quick else 2000

    print(f"[1/5] Loading inputs… (seeds={n_seeds}, sims/seed={n_sims})")
    cfg_data = json.loads((RAW / "wc2026_config.json").read_text())
    bracket = json.loads((RAW / "knockout_bracket_2026.json").read_text())
    annex_c = json.loads((RAW / "annex_c_third_place_table_2026.json").read_text())
    squad_vals = json.loads((RAW / "squad_values_2026.json").read_text())["squad_values"]
    elo = json.loads((PROC / "elo_ratings.json").read_text())
    home_model = joblib.load(MODELS / "home_goals_model.joblib")
    away_model = joblib.load(MODELS / "away_goals_model.joblib")
    feature_cols = json.loads((MODELS / "feature_cols_v2.json").read_text())
    metrics = json.loads((MODELS / "metrics_v2.json").read_text())
    matches_df = pd.read_parquet(PROC / "matches_clean.parquet")

    cfg = dict(DEFAULTS)
    cfg["use_dispersion"] = not args.no_dispersion
    cfg["use_travel"] = not args.no_travel
    cfg["live_mode"] = args.live
    cfg["use_adjustments"] = not args.no_adjustments

    # Live-mode artifacts
    distance_matrix_path = RAW / "host_city_distance_matrix.json"
    distance_matrix = json.loads(distance_matrix_path.read_text()) if distance_matrix_path.exists() else None
    injury_adjustments = load_injury_adjustments(ROOT / "data" / "live" / "team_adjustments.json") \
        if cfg["use_adjustments"] else {}
    completed_matches = load_completed_matches(ROOT / "data" / "live" / "results_2026.json") \
        if cfg["live_mode"] else {}
    live_team_state = load_live_team_state(ROOT / "data" / "live" / "live_team_state.json") \
        if cfg["live_mode"] else {}

    print(f"      Travel: {'on' if cfg['use_travel'] else 'off'} · "
          f"Adjustments: {len(injury_adjustments)} team(s) · "
          f"Live: {'on' if cfg['live_mode'] else 'off'} ({len(completed_matches)} matches locked, "
          f"{len(live_team_state)} team-state deltas)")

    print("[2/5] Precomputing match matrices + knockout pair matrices…")
    ctx = precompute_context(cfg_data, bracket, annex_c, squad_vals, elo,
                             home_model, away_model, feature_cols, matches_df, cfg,
                             distance_matrix=distance_matrix,
                             injury_adjustments=injury_adjustments,
                             completed_matches=completed_matches,
                             live_team_state=live_team_state)

    # H1: stamp a stable hash of the inputs that drive the live simulation.
    # run_live_update reads this from the previous predictions_live.json to
    # decide whether to re-simulate. Live mode only — baseline runs don't
    # need it.
    if cfg["live_mode"]:
        _h = hashlib.sha256()
        try:
            res_p = ROOT / "data" / "live" / "results_2026.json"
            if res_p.exists():
                res = json.loads(res_p.read_text())
                cm = sorted(res.get("completed_matches", []), key=lambda r: r.get("m", 0))
                _h.update(json.dumps(cm, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            mi_p = ROOT / "dashboard" / "matchday_intelligence.json"
            if mi_p.exists():
                mi = json.loads(mi_p.read_text())
                _h.update(str(mi.get("generated_at", "")).encode("utf-8"))
                _h.update(str(len(mi.get("adjustments") or [])).encode("utf-8"))
            lts_p = ROOT / "data" / "live" / "live_team_state.json"
            if lts_p.exists():
                lts = json.loads(lts_p.read_text())
                _h.update(str(lts.get("last_updated", "")).encode("utf-8"))
                _h.update(json.dumps(lts.get("deltas", {}) or {},
                                     sort_keys=True, separators=(",", ":")).encode("utf-8"))
            ctx["input_hash"] = _h.hexdigest()[:16]
        except Exception as _e:
            print(f"[03_simulate] could not compute input_hash: {_e}")
            ctx["input_hash"] = ""

    print(f"[3/5] Running {n_seeds} seeds × {n_sims:,} sims…")
    all_teams = ctx["all_teams"]
    runs = []
    for seed in range(42, 42 + n_seeds):
        print(f"      Seed {seed} …", end="", flush=True)
        r = run_single_seed(seed, cfg, n_sims, ctx)
        runs.append(r)
        print(f" champion(top)={max(r['champion'].items(), key=lambda x: x[1])[0]} "
              f"misses={r['annex_c_misses']}")

    print(f"[4/5] Aggregating multi-seed statistics…")
    per_team = aggregate_runs(runs, all_teams, n_sims)

    team_summary = []
    for t in all_teams:
        d = per_team[t]
        team_summary.append({
            "team": t,
            "group": next(g for g, ts in cfg_data["groups"].items() if t in ts),
            "elo": float(elo.get(t, 1500)),
            "fifa_pts": cfg_data["fifa_rankings_june_2026"].get(t),
            "squad_value_eur_m": squad_vals.get(t),
            "p_advance_groups": d["qualified"]["mean"],
            "p_reach_r16": d["r16"]["mean"],
            "p_reach_qf": d["qf"]["mean"],
            "p_reach_sf": d["sf"]["mean"],
            "p_reach_final": d["final"]["mean"],
            "p_champion": d["champion"]["mean"],
            "p_third_place": d["third_place"]["mean"],
            "p_champion_p05": d["champion"]["p05"],
            "p_champion_p95": d["champion"]["p95"],
            "p_finish_1st_group": d["finish_1"]["mean"],
            "p_finish_2nd_group": d["finish_2"]["mean"],
            "p_finish_3rd_group": d["finish_3"]["mean"],
            "p_finish_4th_group": d["finish_4"]["mean"],
        })
    team_summary.sort(key=lambda r: -r["p_champion"])

    match_predictions = [{k: v for k, v in m.items() if k != "matrix"}
                         for m in ctx["group_matrices"]]
    for m in match_predictions:
        # numpy → python
        m["lam_home"] = float(m["lam_home"]); m["lam_away"] = float(m["lam_away"])
    # P1-D: append knockout placeholders so the dashboard's Matches view
    # has fixtures to render from 28 Jun onwards. These carry no per-team
    # probabilities pre-resolution; the dashboard already null-guards
    # those fields and falls back to slot labels for the team strings.
    match_predictions.extend(ctx.get("knockout_predictions", []))

    annex_c_misses = sum(r["annex_c_misses"] for r in runs)
    print(f"      Annex C misses across all sims: {annex_c_misses}")

    # Sanity diagnostics
    top1 = team_summary[0]["p_champion"]
    top2 = team_summary[0]["p_champion"] + team_summary[1]["p_champion"]
    top5 = sum(t["p_champion"] for t in team_summary[:5])
    print(f"\n  Concentration diagnostics:")
    print(f"  Top-1 champion P: {top1*100:.1f}%   {'⚠ HIGH' if top1 > 0.20 else 'OK'}")
    print(f"  Top-2 combined:   {top2*100:.1f}%   {'⚠ HIGH' if top2 > 0.40 else 'OK'}")
    print(f"  Top-5 combined:   {top5*100:.1f}%   {'⚠ HIGH' if top5 > 0.70 else 'OK'}")

    print("[5/5] Writing predictions.json…")
    out = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "n_simulations_per_seed": n_sims,
        "n_seeds": n_seeds,
        "n_simulations_total": n_sims * n_seeds,
        "config": cfg,
        "model_metrics": metrics,
        "tournament": cfg_data["tournament"],
        "groups": cfg_data["groups"],
        "team_predictions": team_summary,
        "match_predictions": match_predictions,
        "feature_importances_home": metrics["feature_importances_home"],
        "feature_importances_away": metrics["feature_importances_away"],
        "host_cities": cfg_data["host_cities"],
        "bracket": bracket,
        "annex_c_misses": annex_c_misses,
        "concentration": {
            "top1_champion_p": top1,
            "top2_combined": top2,
            "top5_combined": top5,
        },
        "model_caveats": [
            "Probabilities are Monte Carlo frequencies, not point forecasts.",
            "Goal model: Poisson regressors + Negative Binomial dispersion + Dixon-Coles τ correction.",
            "Knockout bracket follows the OFFICIAL FIFA-published R32-to-final structure.",
            "Third-place slot assignment uses FIFA Annex C exact lookup (495 combinations).",
            "Tiebreakers follow FIFA 2026 regulations.",
            "Simulation ranges (p05/p95) shown across 5 independent MC seeds — this is sampling noise from independent tournament rollouts, NOT a parameter confidence interval.",
            "Model does NOT account for: last-minute injuries, refereeing, in-tournament momentum.",
        ],
        "data_sources": {
            "match_history": "github.com/martj42/international_results (CC0)",
            "fifa_rankings": "FIFA Men's World Ranking (live tracker, June 2026)",
            "schedule": "FIFA.com official 2026 schedule + ESPN article 47108758",
            "bracket": "FIFA Annex C (Regulations) — official R32→Final mapping",
            "annex_c_third_place_lookup": "Wikipedia template + FWC2026 regulations PDF",
            "squad_values": "Transfermarkt aggregates (Sportingpedia / GiveMeSport, May-June 2026)",
            "tiebreakers": "FIFA 2026 regulations",
        },
    }
    out["live_mode"] = cfg["live_mode"]
    out["travel_enabled"] = cfg["use_travel"]
    out["injury_adjustments_active"] = injury_adjustments
    out["live_team_state_deltas"] = live_team_state
    out["completed_matches"] = list(completed_matches.keys())
    # H1: stamp the inputs hash so run_live_update can detect actual changes
    # (results, intel, team-state) rather than just the locked-match count.
    out["input_hash"] = ctx.get("input_hash", "")
    # C3: per-team grand-total-cap audit so the dashboard can badge any team
    # whose live form + matchday intel was clamped to ±45.
    out["grand_cap_applied"] = ctx.get("grand_cap_applied", {})
    # P2: per-fixture intel audit trail (which group match got how much
    # weather/lineup delta, whether the grand cap clamped that fixture).
    # Trimmed to non-zero entries to keep payload size honest — most
    # fixtures will have nothing live to report pre-tournament.
    _meta_all = ctx.get("per_match_intel_meta", []) or []
    out["per_match_intel_meta"] = [
        x for x in _meta_all
        if abs(x.get("home_intel_bonus_applied", 0.0)) > 1e-6
        or abs(x.get("away_intel_bonus_applied", 0.0)) > 1e-6
        or x.get("home_grand_cap_hit") or x.get("away_grand_cap_hit")
        or x.get("climate_static_h_zeroed_by_forecast")
        or x.get("climate_static_a_zeroed_by_forecast")
    ]
    # Atomic write: a SIGKILL/OOM mid-write would otherwise leave the
    # canonical predictions file partially written and trip the downstream
    # publish guard (or worse, get copied through to dashboard/). tempfile
    # in the same dir + os.replace gives same-filesystem atomic rename.
    _out_path = PROC / args.out
    _out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(_out_path.parent),
        prefix=_out_path.name + ".", suffix=".tmp", delete=False,
    ) as _tmp:
        json.dump(out, _tmp, indent=2, default=str)
        _tmp_path = Path(_tmp.name)
    os.replace(_tmp_path, _out_path)
    print(f"[OK] Wrote {_out_path}")

    print("\n=== TOP 15 (v3: NB + DC-τ + Annex C + 5-seed sim range) ===")
    print(f"{'Rank':<5}{'Team':<24}{'P(win)':>10}{'Sim 5-95':>14}{'P(SF)':>9}{'Elo':>7}")
    for i, t in enumerate(team_summary[:15], 1):
        ci = f"[{t['p_champion_p05']*100:4.1f}-{t['p_champion_p95']*100:4.1f}]"
        print(f"{i:<5}{t['team']:<24}{t['p_champion']*100:>7.1f}%   {ci:>14}  "
              f"{t['p_reach_sf']*100:>6.1f}%  {t['elo']:>6.0f}")

    print(f"\n  Σ champion = {sum(t['p_champion'] for t in team_summary):.4f}")
    print(f"  Σ finalists = {sum(t['p_reach_final'] for t in team_summary):.4f}")
    print(f"  Σ qualified = {sum(t['p_advance_groups'] for t in team_summary):.4f}")


if __name__ == "__main__":
    main()
