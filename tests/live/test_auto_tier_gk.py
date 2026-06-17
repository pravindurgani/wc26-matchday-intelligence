"""Item #4b — goalkeeper classification path.

API-Football's /players?team=&season= endpoint does not populate per-
keeper clean_sheets in our feed (audited 2026-06-16: all 250 GKs in
data/live/player_stats_2026.json have clean_sheets == 0; the field is
hard-zero'd in fetch_player_stats.normalise_team_payload with a
provenance comment — previous expression `goals.get("conceded") and 0`
was misleading but only ever produced 0 in practice because no fallback
key exists in the response either). The auto_tier GK branch is
therefore minutes-share-only, with a higher minutes-share bar for the
"defining-player keeper" tier_1_star case (Courtois/Martínez who play
every minute).

These tests pin:
  - GK above tier_1_keeper minutes-share threshold → tier_1_keeper.
  - GK below tier_2_starter threshold → not tier_1.
  - A structurally-zero clean_sheets value does NOT penalise the GK
    into a worse tier than minutes-share alone would have given.
  - GK at/above the defining-player minutes_share bar → tier_1_star.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

from auto_tier import (  # noqa: E402
    THRESHOLDS, PlayerStats, auto_classify,
)


class TestGoalkeeperBranches(unittest.TestCase):
    def test_gk_above_keeper_threshold_is_tier_1_keeper(self):
        # 800/1000 = 0.80 share > tier_1_keeper threshold (0.75).
        s = PlayerStats(minutes=800, team_top_minutes=1000,
                        appearances=10, clean_sheets=0, position="G")
        tier, src, _ = auto_classify(s)
        self.assertEqual(tier, "tier_1_keeper")
        self.assertEqual(src, "auto_keeper")

    def test_gk_below_starter_threshold_is_squad(self):
        # 200/1000 = 0.20 share < tier_2_starter threshold (0.50).
        s = PlayerStats(minutes=200, team_top_minutes=1000,
                        appearances=3, clean_sheets=0, position="G")
        tier, src, _ = auto_classify(s)
        self.assertEqual(tier, "tier_3_squad")
        self.assertEqual(src, "auto_minutes_low")
        self.assertNotEqual(tier, "tier_1_keeper")

    def test_gk_between_starter_and_keeper_is_starter(self):
        # 600/1000 = 0.60 share — clears tier_2 but not tier_1_keeper.
        s = PlayerStats(minutes=600, team_top_minutes=1000,
                        appearances=7, clean_sheets=0, position="G")
        tier, src, _ = auto_classify(s)
        self.assertEqual(tier, "tier_2_starter")
        self.assertEqual(src, "auto_starter")

    def test_gk_zero_clean_sheets_does_not_penalise(self):
        # Real-world feed: GK has played every minute (defining-player
        # case) but feed reports clean_sheets=0. Pre-fix, the star
        # branch checked `cs_share >= 0.80` and so a defining-player
        # keeper was structurally locked out of tier_1_star. Post-fix,
        # minutes-share-only at 0.90 promotes them correctly.
        s = PlayerStats(minutes=950, team_top_minutes=1000,
                        appearances=11, clean_sheets=0, position="G")
        tier, src, _ = auto_classify(s)
        self.assertEqual(tier, "tier_1_star")
        self.assertEqual(src, "auto_star")
        # And the resulting tier is strictly >= what minutes-share-only
        # would have given anyway (tier_1_keeper at 0.95 share).
        self.assertIn(tier, ("tier_1_star", "tier_1_keeper"))

    def test_gk_defining_player_threshold_is_minutes_share_only(self):
        # Bar is THRESHOLDS["tier_1_star_minutes_share_gk"] — pin it.
        bar = THRESHOLDS["tier_1_star_minutes_share_gk"]
        # Just below the bar → tier_1_keeper (not star).
        s_below = PlayerStats(
            minutes=int(bar * 1000) - 5, team_top_minutes=1000,
            appearances=11, clean_sheets=0, position="G")
        tier_below, src_below, _ = auto_classify(s_below)
        self.assertEqual(tier_below, "tier_1_keeper")
        self.assertEqual(src_below, "auto_keeper")
        # At the bar → tier_1_star.
        s_at = PlayerStats(
            minutes=int(bar * 1000), team_top_minutes=1000,
            appearances=11, clean_sheets=0, position="G")
        tier_at, src_at, _ = auto_classify(s_at)
        self.assertEqual(tier_at, "tier_1_star")
        self.assertEqual(src_at, "auto_star")

    def test_gk_star_does_not_require_clean_sheets(self):
        # Explicit anti-regression: a GK with clean_sheets=0 AND high
        # minutes share must NOT be capped at tier_1_keeper by virtue
        # of the (now-removed) cs_share gate.
        s = PlayerStats(minutes=990, team_top_minutes=1000,
                        appearances=11, clean_sheets=0, position="G")
        tier, _, comp = auto_classify(s)
        self.assertEqual(tier, "tier_1_star")
        # cs_share is still surfaced in components for audit, but it
        # must not have driven the decision.
        self.assertEqual(comp["cs_share"], 0.0)
        self.assertTrue(comp["is_gk"])

    def test_outfield_star_path_unchanged(self):
        # Regression guard: the outfield tier_1_star path still requires
        # both minutes_share AND ga90, regardless of the GK-branch rework.
        # Outfielder at 0.95 share but 0 g+a → tier_2_starter, not star.
        s = PlayerStats(minutes=950, team_top_minutes=1000,
                        goals=0, assists=0, position="M")
        tier, _, _ = auto_classify(s)
        self.assertEqual(tier, "tier_2_starter")


if __name__ == "__main__":
    unittest.main()
