"""
stats_proxy_adjustments.py — Stream B.5 pure helpers.

Compute a POST-MATCH form delta from raw box-score stats. Deliberately NOT
labelled or modelled as xG — the spec lockdown is "stats_proxy never xG".
True xG (per-shot quality) requires shot-location data API-Football's
/fixtures/statistics does not expose, and trying to back into it from
totals is fake precision.

What this CAN show: which team dominated possession + shot volume vs which
team scraped a result against the run of play. That's a useful signal for
"this team's form is better than the result suggests" without pretending
to know shot quality.

v1 formula (signed, in Elo points):
  shot_dominance = (own_shots_on_target - opp_shots_on_target) * 1.2
  possession_edge = (own_possession_pct - 50.0) * 0.06
  corner_edge = (own_corners - opp_corners) * 0.3

  form_delta = clamp(shot_dominance + possession_edge + corner_edge,
                     -STATS_PROXY_RAW_CAP, +STATS_PROXY_RAW_CAP)

Then the consumer (apply_matchday_adjustments) re-clamps at the locked
caps: per-match ±8 (STATS_CAP_PER_MATCH) and group-stage total ±20
(STATS_CAP_TOURNAMENT_TOTAL). Our raw cap is intentionally a bit looser so
the simulator-side caps remain the load-bearing ceiling.

Sign convention: positive means "deserved more than the scoreboard shows"
(if your shots-on-target > opp's, you "earned" form credit). Goals are
NOT in the formula because they're already in the scoreline used by the
Monte Carlo locked-result short-circuit.

Reference fields in /fixtures/statistics response items:
  - "Shots on Goal" → integer
  - "Ball Possession" → "57%" string
  - "Corner Kicks" → integer
"""
from __future__ import annotations

import math

STATS_PROXY_RAW_CAP = 12.0  # downstream re-caps at ±8 per match

SHOT_DOMINANCE_WEIGHT = 1.2
POSSESSION_WEIGHT = 0.06
CORNER_WEIGHT = 0.3
# Possession within ±5pp of 50/50 is noise — score it as zero.
POSSESSION_DEADZONE_PP = 5.0
# Real-xG branch (dead by default; flag-gated upstream in fetch_match_stats).
XG_EDGE_WEIGHT = 6.0  # 1.0 xG edge ≈ 6 Elo of "deserved" credit


def _to_int(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        s = v.strip().rstrip("%")
        try:
            return int(float(s))
        except ValueError:
            return None
    return None


def stats_to_dict(side_stats: list[dict]) -> dict:
    """Convert an API-Football statistics array
       [{"type": "Shots on Goal", "value": 5}, ...] → flat dict."""
    out: dict[str, int | None] = {}
    for entry in side_stats or []:
        t = (entry.get("type") or "").strip()
        v = _to_int(entry.get("value"))
        if t:
            out[t] = v
    return out


def _possession_signal(own_poss) -> float:
    if own_poss is None or (isinstance(own_poss, float) and math.isnan(own_poss)):
        return 0.0
    edge = own_poss - 50.0
    if abs(edge) <= POSSESSION_DEADZONE_PP:
        return 0.0
    adjusted = edge - (POSSESSION_DEADZONE_PP if edge > 0 else -POSSESSION_DEADZONE_PP)
    return adjusted * POSSESSION_WEIGHT


def compute_form_delta(own: dict, opp: dict) -> float:
    """Apply v1 weighted-sum heuristic. Returns signed Elo points, clamped
    at ±STATS_PROXY_RAW_CAP."""
    own_sot = own.get("Shots on Goal") or 0
    opp_sot = opp.get("Shots on Goal") or 0
    own_corn = own.get("Corner Kicks") or 0
    opp_corn = opp.get("Corner Kicks") or 0

    shot_dominance = (own_sot - opp_sot) * SHOT_DOMINANCE_WEIGHT
    possession_edge = _possession_signal(own.get("Ball Possession"))
    corner_edge = (own_corn - opp_corn) * CORNER_WEIGHT

    raw = shot_dominance + possession_edge + corner_edge
    return max(-STATS_PROXY_RAW_CAP, min(STATS_PROXY_RAW_CAP, raw))


def compute_xg_form_delta(own_xg: float, opp_xg: float) -> float:
    """Real-xG form delta. Dead by default — gated upstream by
    fetch_match_stats.XG_ENABLED + per-row xg_found honesty flags. Same
    cap as the proxy so downstream re-clamps stay load-bearing."""
    if not (math.isfinite(own_xg) and math.isfinite(opp_xg)):
        raise ValueError("xg must be finite")
    raw = (float(own_xg) - float(opp_xg)) * XG_EDGE_WEIGHT
    return max(-STATS_PROXY_RAW_CAP, min(STATS_PROXY_RAW_CAP, raw))


def both_form_deltas(home_stats: list[dict],
                     away_stats: list[dict]) -> tuple[float, float]:
    """Convenience: compute both sides at once from raw API arrays."""
    h = stats_to_dict(home_stats)
    a = stats_to_dict(away_stats)
    return compute_form_delta(h, a), compute_form_delta(a, h)
