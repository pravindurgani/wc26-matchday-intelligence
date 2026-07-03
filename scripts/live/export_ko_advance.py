"""
S7 — KO advance-prob export post-processor.

Background
----------
For each KO match (m ≥ 73) with both teams resolved, derive
    p_advance_match = p_home_win + p_draw * P(home wins ET+pens | 90' draw)
where the 90-min WDL comes from the same NB(α)-marginal × Dixon-Coles-τ
joint matrix the production sim uses (build_score_matrix at
scripts/03_simulate.py:186-208), and the draw mass is split with the SAME
tie-break model the sim actually plays in `resolve_knockout`
(scripts/03_simulate.py:334-355):

    1. 30-min extra time: independent Poisson goals at λ/ET_LAMBDA_DIVISOR
       (λ/3) per side — this favors the stronger side, NOT 50/50;
    2. if still level, penalties via the Elo logistic
       P(home) = 1 / (1 + 10 ** ((elo_a - elo_h) / PEN_ELO_SLOPE)),
       using the sim-emitted effective Elos from `knock_lambdas_table`.

R17 P2 history: this field used to be `p_home_win + 0.5 * p_draw` under a
docstring claiming the 50/50 split was "already implicit" in
resolve_knockout. That claim was false — the sim's ET-at-λ/3 + Elo-pens
model tilts the draw mass toward the favorite, so the published number
underpriced favorites by ~2-5pp on the dashboard. The closed form below
now matches the sim's model exactly (constants imported from
scripts/constants.py — no magic numbers); for two equal-strength sides it
degenerates to the old p_home + 0.5*p_draw.

Why a post-processor (not a sim-core change)
-------------------------------------------
The sim already computes the symmetric λ table for every team pair
(scripts/03_simulate.py:956-980 — `knock_lambdas`); this script reuses
that work via a tiny additive export (`knock_lambdas_table`, written at
scripts/03_simulate.py:1267-1283) and combines it with the bracket +
completed_matches to resolve which KO matches actually have both teams.
No retraining, no MC re-run, no threshold/cap changes — purely a
serialisation + per-match closed-form WDL evaluation.

Σ-gate invariant
----------------
p_advance_match is per-match, not part of Σ p_champion = 1.0. Σ-gate
(scripts/check_invariants.py) is re-run at the end of every export as a
sanity check; non-zero exit fails the post-processor loudly.

Idempotency
-----------
The `match_predictions_ko` block is rebuilt from scratch on every run
(any pre-existing entries are dropped before re-computing). Running the
script N times produces identical output — no duplication, no
compounding floating-point drift.

CLI
---
    python3 -m scripts.live.export_ko_advance
    python3 -m scripts.live.export_ko_advance \
        --in  data/processed/predictions_live.json \
        --out data/processed/predictions_live.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"
PROC = ROOT / "data" / "processed"
LIVE = ROOT / "data" / "live"

# Make sibling packages importable when invoked as a module OR a script.
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "live"))

# Reuse the canonical FIFA tiebreaker cascade — single source of truth.
from tiebreakers import rank_group, rank_third_placed  # noqa: E402

# `is_placeholder_slot` is the shared definition of "this bracket slot has
# not yet resolved to a real team" — keep one definition, don't fork.
from _knockout import is_placeholder_slot  # noqa: E402

# Σ-gate import — re-run at the end of every export to confirm the
# post-processor hasn't accidentally broken the invariant. The KO-advance
# fields are per-match (not in any sum-to-1 channel), so a regression here
# would be a bug we *want* to catch loudly.
from check_invariants import (  # noqa: E402
    check_invariants,
    InvariantError,
)

# Closed-form joint constants — canonical declarations live in
# scripts/constants.py (single source of truth). The .gs betting engine
# mirrors them as GOAL_GRID_TAU / GOAL_GRID_MAX_GOALS, and
# scripts/live/verify_goal_grid_agreement.py asserts numerical
# agreement against the production sim at 1e-9.
# R13 C1 history: bumped 10 → 15 to follow R12 MED (sim default
# max_g 10 → 15). Pre-R13 the export silently truncated the NB tail at
# 10, publishing KO advance probabilities that diverged from the
# production sim's 16×16 by up to 2.2pp at the high-λ tail.
from constants import (  # noqa: E402
    DC_RHO,
    ET_LAMBDA_DIVISOR,
    MAX_G,
    NB_ALPHA,
    PEN_ELO_SLOPE,
)
FLOOR = 1e-12

DEFAULT_IN = PROC / "predictions_live.json"
DEFAULT_OUT = PROC / "predictions_live.json"
DEFAULT_BRACKET = RAW / "knockout_bracket_2026.json"
DEFAULT_RESULTS = LIVE / "results_2026.json"
DEFAULT_CONFIG = RAW / "wc2026_config.json"
DEFAULT_ANNEX_C = RAW / "annex_c_third_place_table_2026.json"


# --------------------------------------------------------------------------
# NB + DC matrix (verbatim port of scripts/03_simulate.py:186-208 with the
# `use_dispersion=True` branch — the production default). Implemented from
# scratch here so the post-processor does not import the sim module
# (avoids dragging in joblib / pandas just to evaluate a tiny closed form).
# --------------------------------------------------------------------------
def _log_gamma(x: float) -> float:
    return math.lgamma(x)


def _nb_pmf(k: int, n: float, p: float) -> float:
    """scipy.stats.nbinom convention: pmf(k, n, p) = C(k+n-1, k) p^n (1-p)^k.
    Implemented via lgamma for numerical stability — matches
    scipy.stats.nbinom.pmf to ~1e-15 on all our λ ranges.
    """
    if p <= 0.0 or p > 1.0:
        return 0.0
    if k < 0:
        return 0.0
    log_coeff = _log_gamma(k + n) - _log_gamma(k + 1) - _log_gamma(n)
    log_val = log_coeff + n * math.log(p) + k * math.log(1.0 - p)
    return math.exp(log_val)


def _dc_tau(h: int, a: int, lam_h: float, lam_a: float, rho: float) -> float:
    """Verbatim from scripts/03_simulate.py:178-183."""
    if h == 0 and a == 0:
        return 1.0 - lam_h * lam_a * rho
    if h == 0 and a == 1:
        return 1.0 + lam_a * rho
    if h == 1 and a == 0:
        return 1.0 + lam_h * rho
    if h == 1 and a == 1:
        return 1.0 - rho
    return 1.0


def _build_nb_dc_matrix(lam_h: float, lam_a: float,
                        alpha: float = NB_ALPHA, rho: float = DC_RHO,
                        max_g: int = MAX_G) -> list[list[float]]:
    """Joint score distribution P[h, a]. NB marginals + DC τ correction.

    Mirrors scripts/03_simulate.py:188-208 with use_dispersion=True. Floor
    + renorm step also mirrored so the resulting matrix is identical to
    the production sim's matrix at the same (lam_h, lam_a) within floating
    -point precision.
    """
    p_h = alpha / (alpha + lam_h)
    p_a = alpha / (alpha + lam_a)
    ph = [_nb_pmf(k, alpha, p_h) for k in range(max_g + 1)]
    pa = [_nb_pmf(k, alpha, p_a) for k in range(max_g + 1)]
    s_ph, s_pa = sum(ph), sum(pa)
    ph = [x / s_ph for x in ph]
    pa = [x / s_pa for x in pa]
    M = [[ph[h] * pa[a] for a in range(max_g + 1)] for h in range(max_g + 1)]
    for h in (0, 1):
        for a in (0, 1):
            M[h][a] *= _dc_tau(h, a, lam_h, lam_a, rho)
    # Floor + renorm — mirrors scripts/03_simulate.py:205-206.
    M = [[max(M[h][a], FLOOR) for a in range(max_g + 1)] for h in range(max_g + 1)]
    total = sum(M[h][a] for h in range(max_g + 1) for a in range(max_g + 1))
    M = [[M[h][a] / total for a in range(max_g + 1)] for h in range(max_g + 1)]
    return M


def _wdl_from_matrix(M: list[list[float]]) -> tuple[float, float, float]:
    """Mirrors scripts/03_simulate.py:wdl_from_matrix at :231-235."""
    n = len(M)
    p_home = sum(M[h][a] for h in range(n) for a in range(n) if h > a)
    p_draw = sum(M[h][a] for h in range(n) for a in range(n) if h == a)
    p_away = sum(M[h][a] for h in range(n) for a in range(n) if h < a)
    return p_home, p_draw, p_away


# --------------------------------------------------------------------------
# R17 P2: closed-form tie-break model — the exact model resolve_knockout
# (scripts/03_simulate.py:334-355) plays when 90 minutes end level:
# 30-min ET at independent Poisson(λ/ET_LAMBDA_DIVISOR) per side, then a
# penalty shootout via the PEN_ELO_SLOPE Elo logistic. Constants imported
# from scripts/constants.py — the same symbols the sim consumes.
# --------------------------------------------------------------------------
def _poisson_pmf(k: int, lam: float) -> float:
    """Plain Poisson pmf via lgamma (numerically stable). The sim's ET uses
    rng.poisson — plain Poisson, NOT the NB/DC 90-min joint — so the closed
    form must too."""
    if k < 0:
        return 0.0
    if lam <= 0.0:
        return 1.0 if k == 0 else 0.0
    return math.exp(k * math.log(lam) - lam - _log_gamma(k + 1))


def _et_pens_home_advance_prob(lam_h: float, lam_a: float,
                               eff_elo_h: float | None,
                               eff_elo_a: float | None,
                               pen_elo_slope: float = PEN_ELO_SLOPE,
                               max_g: int = MAX_G) -> float:
    """P(home advances | 90-min draw) under the sim's tie-break model.

    Mirrors resolve_knockout (scripts/03_simulate.py:334-355): since the
    90-min score is level, the ET winner is decided purely by the two
    independent Poisson(λ/3) ET goal counts; a level ET goes to penalties,
    where P(home) = 1 / (1 + 10 ** ((elo_a - elo_h) / PEN_ELO_SLOPE)) on
    the same effective Elos the sim passes into the shootout.

    `lam_h`/`lam_a` are the FULL-match λs (the division by
    ET_LAMBDA_DIVISOR happens here, exactly as in the sim). Truncation at
    max_g leaves < 1e-12 tail mass at λ/3 ≤ 2.4; marginals are renormalised
    so the three ET outcomes sum to exactly 1 (same convention as the
    90-min matrix floor+renorm).

    If either effective Elo is missing (legacy knock_lambdas_table rows),
    the shootout falls back to 50/50 — the ET tilt still applies.
    For λ_h == λ_a and equal Elos this returns exactly 0.5, so
    p_advance_match degenerates to the historical p_home + 0.5 * p_draw.
    """
    lh = lam_h / ET_LAMBDA_DIVISOR
    la = lam_a / ET_LAMBDA_DIVISOR
    ph = [_poisson_pmf(k, lh) for k in range(max_g + 1)]
    pa = [_poisson_pmf(k, la) for k in range(max_g + 1)]
    s_ph, s_pa = sum(ph), sum(pa)
    ph = [x / s_ph for x in ph]
    pa = [x / s_pa for x in pa]
    p_et_home = sum(ph[h] * pa[a]
                    for h in range(max_g + 1)
                    for a in range(max_g + 1) if h > a)
    p_et_draw = sum(ph[k] * pa[k] for k in range(max_g + 1))
    if eff_elo_h is None or eff_elo_a is None:
        p_pens_home = 0.5
    else:
        # Verbatim logistic from scripts/03_simulate.py:350.
        p_pens_home = 1.0 / (1.0 + 10.0 ** ((eff_elo_a - eff_elo_h)
                                            / pen_elo_slope))
    return p_et_home + p_et_draw * p_pens_home


# --------------------------------------------------------------------------
# Bracket-slot resolution. A KO slot is "resolved" iff it points to a
# concrete team name. Three resolution paths:
#
#   `W<n>` / `L<n>`  →  winner / loser of match n (from completed_matches)
#   `1<G>` / `2<G>`  →  group <G>'s 1st / 2nd place (need all 6 group
#                       matches completed; uses tiebreakers.rank_group)
#   `3<G>` (Annex C) →  third-placed of <G>, mapped via Annex C lookup
#                       once all 12 thirds are known
#
# If any required input is missing, the slot stays unresolved and the KO
# match is silently skipped in this run — by design, since we only price
# matches with both teams known.
# --------------------------------------------------------------------------
def _build_completed_index(results_path: Path) -> dict[int, dict]:
    """Return {match_num: completed_record}. Empty if results file absent."""
    if not results_path.exists():
        return {}
    try:
        d = json.loads(results_path.read_text())
    except json.JSONDecodeError:
        return {}
    out = {}
    for m in d.get("completed_matches", []) or []:
        mn = m.get("m")
        if mn is None:
            continue
        out[int(mn)] = m
    return out


def _winner_of(rec: dict) -> str | None:
    """Real team name of the winner of a completed match, or None."""
    if not rec:
        return None
    w = rec.get("winner")
    if w == "home":
        return rec.get("home")
    if w == "away":
        return rec.get("away")
    return None


def _loser_of(rec: dict) -> str | None:
    if not rec:
        return None
    w = rec.get("winner")
    if w == "home":
        return rec.get("away")
    if w == "away":
        return rec.get("home")
    return None


def _group_match_dicts(group_letter: str, group_teams: list[str],
                       completed_idx: dict[int, dict],
                       schedule: list[dict]) -> list[dict] | None:
    """Return all 6 completed match dicts for a group in the shape
    tiebreakers._stats_from_matches expects, OR None if the group is not
    yet fully completed."""
    out = []
    for m in schedule:
        if m.get("group") != group_letter:
            continue
        rec = completed_idx.get(m["m"])
        if not rec or rec.get("home_score") is None or rec.get("away_score") is None:
            return None
        out.append({
            "home": rec["home"], "away": rec["away"],
            "home_score": rec["home_score"], "away_score": rec["away_score"],
        })
    if len(out) != 6:
        return None
    return out


def _resolve_group_slots(completed_idx: dict[int, dict],
                         cfg_data: dict,
                         annex_c: dict) -> dict[str, str]:
    """Return mapping {slot_code: team_name} for every group slot whose
    group is fully completed. Slot codes covered:
        '1A'..'1L'   group winners
        '2A'..'2L'   group runners-up
        '3A'..'3L'   third-placed (only filled for the 8 qualifying thirds,
                     mapped to their R32 slot codes 'M74', 'M77', etc.)
        'M74' (etc.) → third-placed assigned to that R32 match by Annex C.
    """
    schedule = cfg_data.get("group_stage_schedule", [])
    fifa_pts = cfg_data.get("fifa_rankings_june_2026", {})
    groups = cfg_data.get("groups", {})
    out: dict[str, str] = {}
    third_buckets: list[dict] = []  # bucket per group (or None if group incomplete)
    third_complete = True
    for g, teams in groups.items():
        gms = _group_match_dicts(g, teams, completed_idx, schedule)
        if gms is None:
            third_complete = False
            continue
        ranked = rank_group(teams, gms, fifa_pts)
        # ranked is a list of 4 dicts with name, pts, gd, gf, pos.
        out[f"1{g}"] = ranked[0]["name"]
        out[f"2{g}"] = ranked[1]["name"]
        # Stash the third entry for Annex C — annotate group letter so
        # rank_third_placed can produce the qualifying set later.
        third_dict = dict(ranked[2])
        third_dict["group"] = g
        third_buckets.append(third_dict)

    # Annex C: resolve `3<G>` for the 8 qualifiers iff every group is
    # complete (otherwise we don't know the qualifying-thirds set).
    if third_complete and len(third_buckets) == len(groups):
        ranked_thirds = rank_third_placed(third_buckets, fifa_pts)
        qualifying = ranked_thirds[:8]
        groups_present = sorted(q["group"] for q in qualifying)
        key = "".join(groups_present)
        mapping = annex_c.get("table", {}).get(key)
        if mapping is not None:
            # mapping is {"3A": "M82", ...} — invert to {"M82": team_name}.
            for q in qualifying:
                slot_key = f"3{q['group']}"
                m_target = mapping.get(slot_key)
                if m_target:
                    out[m_target] = q["name"]
                # Also expose `3<G>` → team directly for any consumer that
                # walks raw group-feeder codes.
                out[slot_key] = q["name"]
    return out


def _resolve_slot(slot: str | None, slot_resolver_ctx: dict) -> str | None:
    """Return the resolved team name for a slot code, or None if not yet
    resolvable. Returns None for placeholder fan-outs like '3A/B/C/D/F'."""
    if slot is None:
        return None
    s = str(slot).strip()
    if not s:
        return None
    if is_placeholder_slot(s) and not (s.startswith("W") or s.startswith("L")
                                       or s.startswith(("1", "2", "3"))):
        # is_placeholder_slot catches `1A`, `W74`, etc. — but those are the
        # codes we DO know how to resolve. The remaining placeholder type
        # this branch catches is the third-place fan-out like
        # '3A/B/C/D/F' which is NOT directly resolvable until Annex C
        # collapses it via _resolve_group_slots(). For those we fall
        # through to the "/" check below.
        return None
    if "/" in s:
        # Third-place fan-out — resolved via R32 slot's match number,
        # which the caller passes as 'r32_match_num' in the ctx.
        m_target = slot_resolver_ctx.get("r32_match_num")
        if m_target is None:
            return None
        return slot_resolver_ctx.get("group_slots", {}).get(f"M{m_target}")
    if s.startswith("W") and s[1:].isdigit():
        return _winner_of(slot_resolver_ctx.get("completed_idx", {}).get(int(s[1:]), {}))
    if s.startswith("L") and s[1:].isdigit():
        return _loser_of(slot_resolver_ctx.get("completed_idx", {}).get(int(s[1:]), {}))
    if s[0] in ("1", "2", "3") and len(s) >= 2 and s[1:].isalpha():
        # Direct group slot code: '1A', '2B', '3F', etc.
        return slot_resolver_ctx.get("group_slots", {}).get(s)
    # Concrete team name (not a slot code) — return as-is.
    if not is_placeholder_slot(s):
        return s
    return None


# --------------------------------------------------------------------------
# KO-advance computation. Iterate every KO fixture, attempt to resolve
# both slots, look up λ from `knock_lambdas_table` (sim-emitted), build
# the matrix, write the per-match block. The pre-existing
# `match_predictions_ko` is dropped before this loop runs — full idempotency.
# --------------------------------------------------------------------------
def _iter_bracket_rows(bracket: dict) -> Iterable[tuple[str, dict]]:
    """Yield (stage_tag, slot_record) pairs for every KO fixture."""
    sections = (
        ("r32", "r32_slots"),
        ("r16", "r16_bracket"),
        ("qf", "qf_bracket"),
        ("sf", "sf_bracket"),
    )
    for stage, key in sections:
        for row in bracket.get(key, []) or []:
            yield stage, row
    ft = bracket.get("final_and_third_place", {}) or {}
    if "third_place" in ft:
        yield "3rd", ft["third_place"]
    if "final" in ft:
        yield "final", ft["final"]


def _lambda_lookup(
    table: list[dict],
) -> dict[tuple[str, str], tuple[float, float, float | None, float | None]]:
    """Index `knock_lambdas_table` by (home, away) for O(1) lookup.

    Each value is (lambda_home, lambda_away, effective_elo_home,
    effective_elo_away). The λs feed the 90-min matrix AND the ET-at-λ/3
    leg of the draw-split; the effective Elos feed the penalty logistic —
    they are the SAME symmetrized Elos the sim passes into
    resolve_knockout, so the closed form prices the shootout identically.
    Elos are None-tolerant for legacy tables (pre-S7 rows without the
    effective_elo_* fields) — the pens leg then falls back to 50/50.

    The sim emits the FULL directed table — entries for both (A, B) and
    (B, A). The advance-prob computation is order-sensitive (we want
    P(home advances | home is slot_a)), so this index does NOT collapse
    directions. If a future regression drops half the entries, the lookup
    will miss and the KO match will be silently skipped — caught by
    test_ko_advance_export.test_knock_lambdas_table_full_coverage.
    """
    out: dict[tuple[str, str], tuple[float, float, float | None, float | None]] = {}
    for row in table or []:
        h = row.get("home")
        a = row.get("away")
        lh = row.get("lambda_home")
        la = row.get("lambda_away")
        if h is None or a is None or lh is None or la is None:
            continue
        eh = row.get("effective_elo_home")
        ea = row.get("effective_elo_away")
        out[(h, a)] = (
            float(lh), float(la),
            float(eh) if eh is not None else None,
            float(ea) if ea is not None else None,
        )
    return out


def build_ko_advance_entries(predictions: dict, bracket: dict,
                             completed_idx: dict[int, dict],
                             cfg_data: dict, annex_c: dict) -> list[dict]:
    """Return the `match_predictions_ko` list. Empty when no KO match has
    both teams resolved (e.g. during group stage)."""
    lam_table = _lambda_lookup(predictions.get("knock_lambdas_table") or [])
    group_slots = _resolve_group_slots(completed_idx, cfg_data, annex_c)
    # R17 P2: pen slope — prefer the config block the sim serialises into
    # the payload (the value the run actually used), fall back to the
    # canonical constant. Both resolve to scripts/constants.PEN_ELO_SLOPE
    # today; the config path guards against a future tuning of DEFAULTS
    # reaching the feed before this module is redeployed.
    pen_slope = float((predictions.get("config") or {})
                      .get("pen_elo_slope", PEN_ELO_SLOPE))
    base_ctx = {
        "completed_idx": completed_idx,
        "group_slots": group_slots,
    }
    out: list[dict] = []
    for stage, row in _iter_bracket_rows(bracket):
        m_num = row.get("match_num")
        slot_a = row.get("slot_a")
        slot_b = row.get("slot_b")
        # R32 third-place fan-outs need the match number to disambiguate
        # which group's third gets routed to which R32 slot (Annex C lookup).
        ctx = dict(base_ctx, r32_match_num=m_num)
        home = _resolve_slot(slot_a, ctx)
        away = _resolve_slot(slot_b, ctx)
        if home is None or away is None:
            continue  # at least one slot unresolved — skip silently
        # Look up λ from the sim-emitted table. If absent (sim hasn't been
        # re-run after the export hook landed), skip — better to emit
        # nothing than to emit something derived from a different λ
        # source the agreement test won't accept.
        lams = lam_table.get((home, away))
        if lams is None:
            continue
        lam_h, lam_a, eff_elo_h, eff_elo_a = lams
        M = _build_nb_dc_matrix(lam_h, lam_a)
        p_home, p_draw, p_away = _wdl_from_matrix(M)
        # R17 P2: split the draw mass with the sim's ACTUAL tie-break model
        # (ET at Poisson λ/3 → Elo-logistic pens; resolve_knockout at
        # scripts/03_simulate.py:334-355) instead of the old 50/50 prior,
        # which underpriced favorites by ~2-5pp. For equal λs + equal Elos
        # p_tiebreak_home is exactly 0.5 and this reduces to the old form.
        p_tiebreak_home = _et_pens_home_advance_prob(
            lam_h, lam_a, eff_elo_h, eff_elo_a, pen_slope)
        p_advance = p_home + p_draw * p_tiebreak_home
        out.append({
            "m": m_num,
            "stage": stage,
            "home": home,
            "away": away,
            "lambda_home": lam_h,
            "lambda_away": lam_a,
            # Effective Elos (sim-symmetrized, from knock_lambdas_table) so
            # downstream consumers/tests can re-derive the tie-break split.
            "effective_elo_home": eff_elo_h,
            "effective_elo_away": eff_elo_a,
            "p_home_win": p_home,
            "p_draw": p_draw,
            "p_away_win": p_away,
            # P(home advances | 90-min draw) — the ET+pens draw-split.
            "p_tiebreak_home_win": p_tiebreak_home,
            "p_advance_match": p_advance,
        })
    return out


def merge_ko_entries_into_match_predictions(predictions: dict,
                                            entries: list[dict]) -> None:
    """Overlay resolved KO rows onto the dashboard's canonical match list.

    The dashboard renders `match_predictions` for the fixture grid. The
    post-processor also exposes the raw KO export under `match_predictions_ko`
    for downstream consumers, but resolved teams must be mirrored into
    `match_predictions` so the public card grid does not keep showing bracket
    placeholders like "2A vs 2B" after the group stage is complete.
    """
    if not entries:
        return

    matches = predictions.get("match_predictions")
    if not isinstance(matches, list):
        return

    entries_by_m = {
        entry.get("m"): entry
        for entry in entries
        if entry.get("m") is not None
    }
    if not entries_by_m:
        return

    team_elo = {
        row.get("team"): row.get("elo")
        for row in predictions.get("team_predictions", []) or []
        if row.get("team") is not None
    }

    seen: set[int] = set()
    merged_matches: list[dict] = []
    for row in matches:
        entry = entries_by_m.get(row.get("m"))
        if entry is None:
            merged_matches.append(row)
            continue

        seen.add(entry["m"])
        home = entry["home"]
        away = entry["away"]
        merged = dict(row)
        merged.update({
            "stage": entry.get("stage", row.get("stage")),
            "home": home,
            "away": away,
            "lambda_home": entry.get("lambda_home"),
            "lambda_away": entry.get("lambda_away"),
            "lam_home": entry.get("lambda_home"),
            "lam_away": entry.get("lambda_away"),
            "p_home_win": entry.get("p_home_win"),
            "p_draw": entry.get("p_draw"),
            "p_away_win": entry.get("p_away_win"),
            "p_advance_match": entry.get("p_advance_match"),
            "elo_home": team_elo.get(home, row.get("elo_home")),
            "elo_away": team_elo.get(away, row.get("elo_away")),
        })
        merged_matches.append(merged)

    for entry in entries:
        if entry.get("m") in seen:
            continue
        home = entry.get("home")
        away = entry.get("away")
        merged_matches.append({
            "m": entry.get("m"),
            "stage": entry.get("stage"),
            "group": str(entry.get("stage", "KO")).upper(),
            "home": home,
            "away": away,
            "lambda_home": entry.get("lambda_home"),
            "lambda_away": entry.get("lambda_away"),
            "lam_home": entry.get("lambda_home"),
            "lam_away": entry.get("lambda_away"),
            "p_home_win": entry.get("p_home_win"),
            "p_draw": entry.get("p_draw"),
            "p_away_win": entry.get("p_away_win"),
            "p_advance_match": entry.get("p_advance_match"),
            "elo_home": team_elo.get(home),
            "elo_away": team_elo.get(away),
        })

    predictions["match_predictions"] = merged_matches


# --------------------------------------------------------------------------
# CLI plumbing. Atomic write via tempfile + os.replace (same pattern as
# scripts/03_simulate.py:1274-1280) so a kill mid-write doesn't leave a
# half-serialised JSON in place of the canonical feed.
# --------------------------------------------------------------------------
def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent),
        prefix=path.name + ".", suffix=".tmp", delete=False,
    ) as tmp:
        # R9 P3: allow_nan=False at producer boundary — the KO advance export
        # carries p_advance probabilities directly; a NaN here would silently
        # publish to dashboard.
        json.dump(payload, tmp, indent=2, default=str, allow_nan=False)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def export(in_path: Path, out_path: Path,
           bracket_path: Path = DEFAULT_BRACKET,
           results_path: Path = DEFAULT_RESULTS,
           cfg_path: Path = DEFAULT_CONFIG,
           annex_c_path: Path = DEFAULT_ANNEX_C,
           run_sigma_gate: bool = True) -> dict:
    """Run the post-processor end-to-end. Returns the updated payload.

    Idempotent: the `match_predictions_ko` block is rebuilt from scratch
    on every invocation. Re-running on the same input produces byte-
    identical output (modulo dict iteration order, which json.dump
    stabilises via sort_keys=False but our build order is deterministic).
    """
    if not in_path.exists():
        raise FileNotFoundError(f"input predictions feed not found: {in_path}")
    predictions = json.loads(in_path.read_text())

    # Inputs from raw / live — tolerate missing files (output empty block).
    bracket = json.loads(bracket_path.read_text()) if bracket_path.exists() else {}
    cfg_data = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    annex_c = json.loads(annex_c_path.read_text()) if annex_c_path.exists() else {}
    completed_idx = _build_completed_index(results_path)

    entries = build_ko_advance_entries(predictions, bracket, completed_idx,
                                       cfg_data, annex_c)
    predictions["match_predictions_ko"] = entries
    merge_ko_entries_into_match_predictions(predictions, entries)

    _atomic_write_json(out_path, predictions)

    if run_sigma_gate:
        # Re-validate the Σ-invariant. p_advance_match is per-match and
        # not part of any sum-to-1 channel, so this should always pass
        # unless a separate regression sneaks in alongside this fix.
        check_invariants(out_path)

    return predictions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export p_advance_match for resolved KO fixtures.",
    )
    parser.add_argument("--in", dest="in_path", default=str(DEFAULT_IN),
                        help="Input predictions_live.json")
    parser.add_argument("--out", dest="out_path", default=str(DEFAULT_OUT),
                        help="Output predictions_live.json (atomic write)")
    parser.add_argument("--bracket", default=str(DEFAULT_BRACKET),
                        help="knockout_bracket_2026.json")
    parser.add_argument("--results", default=str(DEFAULT_RESULTS),
                        help="results_2026.json (completed_matches index)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="wc2026_config.json (groups + schedule)")
    parser.add_argument("--annex-c", default=str(DEFAULT_ANNEX_C),
                        help="annex_c_third_place_table_2026.json")
    parser.add_argument("--no-sigma-gate", action="store_true",
                        help="Skip Σ-invariant gate (for testing only)")
    args = parser.parse_args(argv)
    try:
        payload = export(
            in_path=Path(args.in_path),
            out_path=Path(args.out_path),
            bracket_path=Path(args.bracket),
            results_path=Path(args.results),
            cfg_path=Path(args.config),
            annex_c_path=Path(args.annex_c),
            run_sigma_gate=not args.no_sigma_gate,
        )
    except FileNotFoundError as e:
        print(f"export_ko_advance: {e}", file=sys.stderr)
        return 2
    except InvariantError as e:
        print(f"export_ko_advance: Σ-gate failed after export: {e}",
              file=sys.stderr)
        return 6
    n_ko = len(payload.get("match_predictions_ko", []))
    print(f"export_ko_advance: wrote {n_ko} resolved KO entries → "
          f"{args.out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
