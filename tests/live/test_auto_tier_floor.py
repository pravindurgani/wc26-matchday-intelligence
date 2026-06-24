"""Item #4a — small-sample floor for auto_tier.

When a team's tracked top-minutes pool falls below MIN_TEAM_TOP_MINUTES,
minutes_share is noise-dominated (e.g. friendlies-only squads). The
classifier must return (None, "auto_insufficient_sample", ...) so the
priority chain (override > auto_tier > DEFAULT_TIER) treats it as "no
auto signal" and falls through.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

from auto_tier import (  # noqa: E402
    MIN_TEAM_TOP_MINUTES, PlayerStats, auto_classify,
)
from fetch_player_stats import to_stats  # noqa: E402
from injury_adjustments import classify_tier_with_overrides  # noqa: E402


class TestFloorConstant(unittest.TestCase):
    def test_floor_is_a_positive_round_number(self):
        # Pinning the value documented in auto_tier.py's "Small-sample
        # floor" docstring — if you move the floor you must update the
        # docstring AND CORRECTIONS.md §7.
        self.assertEqual(MIN_TEAM_TOP_MINUTES, 200)


class TestAutoClassifyFloor(unittest.TestCase):
    def test_below_floor_returns_insufficient_sample(self):
        # Team leader has played only ~1 half. Even a player at 100% of
        # leader minutes would be a noise-dominated signal.
        s = PlayerStats(minutes=45, team_top_minutes=45,
                        goals=0, assists=0, position="M")
        tier, src, comp = auto_classify(s)
        self.assertIsNone(tier)
        self.assertEqual(src, "auto_insufficient_sample")
        self.assertEqual(comp.get("team_top_minutes"), 45)
        self.assertEqual(comp.get("floor"), MIN_TEAM_TOP_MINUTES)

    def test_exactly_at_floor_returns_a_real_tier(self):
        # >= floor must NOT trigger insufficient_sample.
        s = PlayerStats(minutes=MIN_TEAM_TOP_MINUTES,
                        team_top_minutes=MIN_TEAM_TOP_MINUTES,
                        goals=0, assists=0, position="M")
        tier, src, _ = auto_classify(s)
        self.assertIsNotNone(tier)
        self.assertNotEqual(src, "auto_insufficient_sample")

    def test_above_floor_returns_real_tier(self):
        # Control case: team with healthy minutes pool, high-minutes
        # player → tier_2_starter (no goals, so no star).
        s = PlayerStats(minutes=900, team_top_minutes=1000,
                        goals=0, assists=0, position="D")
        tier, src, _ = auto_classify(s)
        self.assertEqual(tier, "tier_2_starter")
        self.assertEqual(src, "auto_starter")

    def test_high_minutes_share_in_small_sample_still_blocked(self):
        # Even a 100% minutes_share is meaningless when the leader has
        # only 60 total minutes. Must defer.
        s = PlayerStats(minutes=60, team_top_minutes=60,
                        goals=2, assists=1, position="F")
        tier, src, _ = auto_classify(s)
        self.assertIsNone(tier)
        self.assertEqual(src, "auto_insufficient_sample")


class TestSyntheticTeamBlob(unittest.TestCase):
    """End-to-end: build a synthetic stats blob with one team below the
    floor and one above. Run via to_stats + auto_classify to confirm the
    real lookup path returns insufficient_sample for small-sample team
    players and a real tier for the control team."""

    def _blob(self):
        # Small_team's leader is below the floor (90 min total).
        # Big_team's leader is well above (1200 min total).
        return {
            "teams": {
                "Small_Team": {
                    "team_top_minutes": 90,
                    "players": [
                        {"name": "S. Mall", "minutes": 90, "goals": 1,
                         "assists": 0, "appearances": 1,
                         "clean_sheets": 0, "position": "F"},
                        {"name": "T. Backup", "minutes": 45, "goals": 0,
                         "assists": 0, "appearances": 1,
                         "clean_sheets": 0, "position": "M"},
                    ],
                },
                "Big_Team": {
                    "team_top_minutes": 1200,
                    "players": [
                        {"name": "B. Star", "minutes": 1100, "goals": 8,
                         "assists": 5, "appearances": 13,
                         "clean_sheets": 0, "position": "F"},
                        {"name": "B. Reserve", "minutes": 300, "goals": 0,
                         "assists": 0, "appearances": 6,
                         "clean_sheets": 0, "position": "M"},
                    ],
                },
            },
        }

    def test_small_team_player_returns_insufficient_sample(self):
        blob = self._blob()
        stats = to_stats(blob["teams"]["Small_Team"], "S. Mall")
        self.assertIsNotNone(stats)
        tier, src, _ = auto_classify(stats)
        self.assertIsNone(tier)
        self.assertEqual(src, "auto_insufficient_sample")

    def test_big_team_player_returns_real_tier(self):
        # Control: > floor, high minutes share, healthy g+a → tier_1_star.
        blob = self._blob()
        stats = to_stats(blob["teams"]["Big_Team"], "B. Star")
        self.assertIsNotNone(stats)
        tier, src, _ = auto_classify(stats)
        self.assertEqual(tier, "tier_1_star")
        self.assertEqual(src, "auto_star")

    def test_priority_chain_falls_to_default_when_insufficient_sample(self):
        # Shadow mode: insufficient_sample player with no whitelist
        # entry → DEFAULT_TIER (source="default"), exactly like
        # pre-Phase-6 behaviour. The auto layer correctly abstains.
        blob = self._blob()
        tier, source, comp = classify_tier_with_overrides(
            "S. Mall", "Small_Team",
            player_stats_payload=blob["teams"]["Small_Team"],
            auto_tier_active=False,
            index={},
        )
        self.assertEqual(source, "default")
        self.assertIsNone(comp["auto_tier"])
        self.assertEqual(comp["auto_source"], "auto_insufficient_sample")

    def test_active_mode_with_insufficient_sample_still_falls_through(self):
        # Even when auto_tier_active=True, an insufficient_sample
        # signal must NOT be used as a tier — the chain falls through
        # to DEFAULT_TIER. This is the load-bearing invariant for
        # Wave-B remediation §4a.
        blob = self._blob()
        tier, source, comp = classify_tier_with_overrides(
            "S. Mall", "Small_Team",
            player_stats_payload=blob["teams"]["Small_Team"],
            auto_tier_active=True,
            index={},
        )
        self.assertEqual(source, "default")
        self.assertIsNone(comp["auto_tier"])


if __name__ == "__main__":
    unittest.main()
