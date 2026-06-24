"""
auto_tier.py — Phase 6 (CORRECTIONS.md §7, deferred Wave-B).

Derive a baseline tier for any squad-listed player from API-Football
statistics, without ever hand-typing a name. The output feeds a priority
chain in injury_adjustments.py:

    override (data/raw/key_players_2026.json)  >  auto_tier  >  DEFAULT_TIER

The override file is conceptually the *thin* manual layer for cases the
automated signals provably miss: returning legends mid-comeback, talismen
with low recent minutes, position-specific judgement calls. The auto-tier
layer is the durable engine that scales to every WC26 squad without
requiring a human edit per player.

Why blend two signals instead of one (minutes-share alone)
==========================================================
Minutes-share captures *manager trust* — does the coach pick this player?
But it collapses "rotation regular" and "generational star" into the same
bucket whenever both clock high minutes (e.g. a Saudi CB regular and
Bellingham both ≥85% of squad minutes). A second axis is required to
separate trust from quality for outfield players:

  S1 — minutes_share          : trailing-12mo national-team minutes /
                                top earner's minutes in same window
  S2 — g+a per 90  (outfield) : (goals + assists) / (minutes / 90)

For GKs the second axis is intentionally NOT clean-sheets. API-Football's
/players?team=&season= response does not populate a per-keeper
clean_sheets field at any nesting level we can read — neither
`statistics[i].clean_sheets` nor `statistics[i].goals.<anything>` exposes
the count. `goals.conceded` is populated for keepers but the payload has
no `appearances - games_with_conceded` aggregate, so we cannot derive
clean_sheets locally without per-fixture data the /players endpoint
doesn't return. Audited 2026-06-16 against the full WC2026 snapshot:
all 250 GK records in data/live/player_stats_2026.json have
clean_sheets == 0, including obvious #1 keepers (E. Martínez 1459 mins,
Z. Suzuki 903, S. Rochet 1287). See fetch_player_stats.py
normalise_team_payload — the field is hard-zero'd in the parse with a
provenance comment. We therefore use minutes-share-only for keepers,
with a higher minutes-share bar for the "defining-player keeper" case
(Courtois/Martínez who play every minute of every match) — no
structurally-zero signal is ever weighted into the score.

Both axes are deterministically derivable from API-Football
/players?team={nat}&season endpoints — no hand-typing.

Tier thresholds (documented, calibratable)
==========================================
The numbers below are first-cut defaults pinned by the unit tests. They
will move once the disagreement diff against the existing 108-entry hand-
curated file lands and we calibrate against it. Any change must be:
  (a) reflected in this docstring
  (b) reflected in the unit-test table
  (c) reflected in CORRECTIONS.md §7

  tier_1_star    : (outfield) minutes_share ≥ 0.85 AND g+a_per_90 ≥ 0.45
                   (GK)       minutes_share ≥ 0.90  — defining-player
                              keeper, minutes-share-only (cs_share is
                              structurally zero on our feed)
  tier_1_keeper  : position == GK AND minutes_share ≥ 0.75
  tier_2_starter : minutes_share ≥ 0.50
  tier_3_squad   : otherwise

GK star threshold (0.90) justification
======================================
Anchored to the actual GK minutes_share distribution in
data/live/player_stats_2026.json. Across 96 GKs whose team has
team_top_minutes ≥ MIN_TEAM_TOP_MINUTES (the small-sample floor that
would otherwise admit them):
  p10=0.062  p25=0.110  p50=0.337  p75=0.574  p90=0.879  max=1.000
0.90 sits at ~p92 of this distribution — it picks up 8 GKs, every one
a recognised undisputed #1: Martínez (ARG, 0.99), Suzuki (JPN, 1.00),
Maignan (FRA, 1.00), Beiranvand (IRN, 1.00), Rochet (URU, 0.98),
Jo Hyeon-Woo (KOR, 0.96), Yazid Abu Layla (JOR, 0.97), Room (CUW, 1.00).
The borderline cohort at 0.85-0.89 (Pickford ENG 0.87, Bounou MAR 0.87,
Vargas COL 0.87, Placide HAI 0.85) are first-choice keepers who do see
backup rotation in friendlies; tier_1_keeper (0.75 bar) is the correct
ceiling for them, not tier_1_star. Threshold change requires re-running
the distribution audit; do not nudge by intuition.

Small-sample floor
==================
MIN_TEAM_TOP_MINUTES guards against noise-dominated minutes_share when a
team's top earner has played only a handful of friendlies. Below the
floor the classifier returns (None, "auto_insufficient_sample", ...) so
the priority chain falls through to override/DEFAULT_TIER rather than
emitting a misclassification. Floor chosen 2026-06-16 from the actual
team_top_minutes distribution across 48 WC26 squads:
  p10=163  p15=171  p25=184  p50=289  p75=1010  p90=1388  max=1560
200 sits at ~p20 and corresponds to "the leader has played ≥ ~2.2 full
matches' worth of minutes" — the smallest pool where minutes_share is
stable enough to trust.

A player with no stats record at all (rookie call-up, untracked league)
falls to tier_3_squad with source="auto_no_data".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Tier = Literal["tier_1_star", "tier_1_keeper", "tier_2_starter", "tier_3_squad"]

# Smallest team_top_minutes pool we trust as a denominator for
# minutes_share. Below this, the numerator/denominator ratio is
# noise-dominated (friendlies-only / qualifier-only teams) — see the
# module docstring "Small-sample floor" section for the percentile
# justification.
# Calibrated against p20 of team_top_minutes pre-tournament; re-validate after R32 ticks land fresh minutes data.
MIN_TEAM_TOP_MINUTES = 200

THRESHOLDS = {
    "tier_1_star_minutes_share": 0.85,
    "tier_1_star_ga90_outfield": 0.45,
    # GK "defining player" bar — minutes-share-only, set deliberately
    # higher than tier_1_keeper because cs_share is not available on
    # our feed (see module docstring).
    "tier_1_star_minutes_share_gk": 0.90,
    "tier_1_keeper_minutes_share": 0.75,
    "tier_2_starter_minutes_share": 0.50,
}


@dataclass(frozen=True)
class PlayerStats:
    """Minimal contract from fetch_player_stats — keep this stable.

    minutes              : trailing-12mo national-team minutes
    team_top_minutes     : max minutes by any player on the same squad
                           (denominator for minutes_share)
    goals                : trailing-12mo international goals
    assists              : trailing-12mo international assists
    appearances          : trailing-12mo international appearances
    clean_sheets         : trailing-12mo international clean sheets (GK)
    position             : "G" | "D" | "M" | "F" | None
    """
    minutes: int
    team_top_minutes: int
    goals: int = 0
    assists: int = 0
    appearances: int = 0
    clean_sheets: int = 0
    position: str | None = None


def minutes_share(stats: PlayerStats) -> float:
    if stats.team_top_minutes <= 0:
        return 0.0
    return min(1.0, stats.minutes / stats.team_top_minutes)


def goals_plus_assists_per_90(stats: PlayerStats) -> float:
    if stats.minutes <= 0:
        return 0.0
    return (stats.goals + stats.assists) / (stats.minutes / 90.0)


def clean_sheet_share(stats: PlayerStats) -> float:
    if stats.appearances <= 0:
        return 0.0
    return stats.clean_sheets / stats.appearances


def is_goalkeeper(stats: PlayerStats) -> bool:
    return (stats.position or "").upper().startswith("G")


def auto_classify(stats: PlayerStats | None,
                  ) -> tuple[Tier | None, str, dict]:
    """Return (tier, source, components).

    `tier` is `None` only when the source is `auto_insufficient_sample`
    — the small-sample guard. In every other case `tier` is a valid
    Tier literal. The priority chain in injury_adjustments treats
    `tier is None` as "no auto signal" and falls through to the next
    layer (override > auto_tier > DEFAULT_TIER).

    `source` taxonomy (mirrors classify_tier's audit vocabulary):
      auto_no_data              — no stats record / zero minutes
      auto_insufficient_sample  — team_top_minutes < MIN_TEAM_TOP_MINUTES
                                  (noise-dominated; tier is None)
      auto_minutes_low          — has data, fails tier_2 threshold
      auto_starter              — meets tier_2_starter threshold only
      auto_keeper               — GK meeting tier_1_keeper threshold
      auto_star                 — meets tier_1_star (outfield or GK
                                  defining-player, both minutes-share-
                                  based — see module docstring on why
                                  GKs do NOT use cs_share)

    `components` carries the numeric signals so the disagreement-diff CLI
    can show *why* each tier was assigned.
    """
    if stats is None:
        return "tier_3_squad", "auto_no_data", {"reason": "no_stats_record"}
    if stats.minutes <= 0 or stats.team_top_minutes <= 0:
        return "tier_3_squad", "auto_no_data", {"reason": "zero_minutes"}

    # Small-sample guard — see module docstring.
    if stats.team_top_minutes < MIN_TEAM_TOP_MINUTES:
        return None, "auto_insufficient_sample", {
            "reason": "team_top_minutes_below_floor",
            "team_top_minutes": stats.team_top_minutes,
            "floor": MIN_TEAM_TOP_MINUTES,
            "minutes": stats.minutes,
        }

    share = minutes_share(stats)
    ga90 = goals_plus_assists_per_90(stats)
    cs_share = clean_sheet_share(stats)
    gk = is_goalkeeper(stats)
    components = {
        "minutes_share": round(share, 3),
        "ga90": round(ga90, 3),
        # Preserved purely for audit transparency — never feeds the GK
        # classification decision (see module docstring).
        "cs_share": round(cs_share, 3),
        "is_gk": gk,
        "minutes": stats.minutes,
        "team_top_minutes": stats.team_top_minutes,
    }

    # tier_1_star (rare, defining-player tier)
    if (not gk) and share >= THRESHOLDS["tier_1_star_minutes_share"]:
        if ga90 >= THRESHOLDS["tier_1_star_ga90_outfield"]:
            return "tier_1_star", "auto_star", components

    # tier_1_star GK — defining-player keeper. Minutes-share-only because
    # the feed does not populate per-keeper clean_sheets (always zero).
    # The threshold is set higher than tier_1_keeper so we only promote
    # keepers who play essentially every minute.
    if gk and share >= THRESHOLDS["tier_1_star_minutes_share_gk"]:
        return "tier_1_star", "auto_star", components

    # tier_1_keeper — GK with first-choice minutes
    if gk and share >= THRESHOLDS["tier_1_keeper_minutes_share"]:
        return "tier_1_keeper", "auto_keeper", components

    # tier_2_starter — anyone clearing the trust bar
    if share >= THRESHOLDS["tier_2_starter_minutes_share"]:
        return "tier_2_starter", "auto_starter", components

    return "tier_3_squad", "auto_minutes_low", components
