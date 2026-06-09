"""
injury_adjustments.py — Stream B.3 pure helpers.

Tier table + classification helpers shared by fetch_injuries.py (writes the
canonical injuries_2026.json) and apply_matchday_adjustments.py (consumes it).

Separated from the fetcher so the math is unit-testable without hitting
API-Football.

Tiers come from the locked spec in data/live/team_adjustments.json's
tier_guide block (kept stable across the manual-overlay → API migration so
existing operator notes still apply):

  tier_1_star    — Mbappé / Bellingham / Vinícius / Haaland / Rodri-level   -30
  tier_1_keeper  — starting GK at a top-10 team                              -25
  tier_2_starter — regular outfield starter, not headline player             -12
  tier_3_squad   — rotation player                                            -4
  doubtful       — 0.5× the tier amount
  suspended      — full tier penalty (for that match only — caller handles)

API-Football's /injuries endpoint exposes `player.type`:
  "Missing Fixture"  → confirmed out      → status = "confirmed_out"
  "Questionable"     → doubtful           → status = "doubtful"

It does NOT expose per-player importance, so for v1 every API-sourced player
defaults to tier_2_starter. Manual overrides in team_adjustments.json may
upgrade individual players to tier_1_*. Conservative default keeps the layer
from over-reacting to depth-chart noise (a 3rd-string CB out shouldn't move
the model -30 Elo).
"""
from __future__ import annotations

TIER_TO_ELO = {
    "tier_1_star":    -30.0,
    "tier_1_keeper":  -25.0,
    "tier_2_starter": -12.0,
    "tier_3_squad":    -4.0,
}

# API-Football `player.type` → our status taxonomy
APIFOOTBALL_TYPE_MAP = {
    "Missing Fixture": "confirmed_out",
    "Questionable":    "doubtful",
    # Defensive fallthrough: anything else (e.g. "Suspended") treated as out.
    "Suspended":       "confirmed_out",
    "Coach Decision":  "doubtful",
}

DOUBTFUL_DISCOUNT = 0.5
DEFAULT_TIER = "tier_2_starter"  # conservative v1 default for API-sourced players


def classify_api_type(player_type: str | None) -> str:
    """Map API-Football `player.type` string → our `status` taxonomy."""
    if not player_type:
        return "confirmed_out"
    return APIFOOTBALL_TYPE_MAP.get(player_type.strip(), "confirmed_out")


def tier_elo(tier: str) -> float:
    """Look up tier penalty (signed; negative)."""
    return TIER_TO_ELO.get(tier, 0.0)


def discounted_elo(tier: str, status: str) -> float:
    """Apply status-based discount to the tier penalty.

    confirmed_out → full tier penalty
    doubtful      → 0.5× tier penalty
    anything else → 0 (defensive; unknown status shouldn't quietly leak Elo)
    """
    base = tier_elo(tier)
    if status == "confirmed_out":
        return base
    if status == "doubtful":
        return base * DOUBTFUL_DISCOUNT
    return 0.0
