"""Unit tests for auto_tier — Phase 6 (CORRECTIONS.md §7).

Pin the documented thresholds and verify the source taxonomy is stable.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

from auto_tier import (  # noqa: E402
    PlayerStats, THRESHOLDS, auto_classify, minutes_share,
    goals_plus_assists_per_90, clean_sheet_share, is_goalkeeper,
)
from injury_adjustments import classify_tier_with_overrides  # noqa: E402


class TestComponentMath(unittest.TestCase):
    def test_minutes_share_zero_denominator(self):
        self.assertEqual(minutes_share(PlayerStats(minutes=100,
                                                  team_top_minutes=0)), 0.0)

    def test_minutes_share_capped_at_1(self):
        self.assertEqual(minutes_share(PlayerStats(minutes=2000,
                                                  team_top_minutes=1000)), 1.0)

    def test_ga90_zero_minutes(self):
        self.assertEqual(goals_plus_assists_per_90(
            PlayerStats(minutes=0, team_top_minutes=1000, goals=5)), 0.0)

    def test_ga90_typical(self):
        # 5 G + 3 A in 900 minutes = 8 / 10 * 90 = 0.8 per 90
        s = PlayerStats(minutes=900, team_top_minutes=900, goals=5, assists=3)
        self.assertAlmostEqual(goals_plus_assists_per_90(s), 0.8, places=3)

    def test_clean_sheet_share_zero_apps(self):
        self.assertEqual(clean_sheet_share(
            PlayerStats(minutes=0, team_top_minutes=0)), 0.0)

    def test_is_goalkeeper_recognises_G(self):
        self.assertTrue(is_goalkeeper(PlayerStats(0, 0, position="G")))
        self.assertTrue(is_goalkeeper(PlayerStats(0, 0, position="Goalkeeper")))
        self.assertFalse(is_goalkeeper(PlayerStats(0, 0, position="F")))
        self.assertFalse(is_goalkeeper(PlayerStats(0, 0, position=None)))


class TestAutoClassify(unittest.TestCase):
    """Threshold table is load-bearing — every change here must be
    reflected in auto_tier.py's docstring AND CORRECTIONS.md §7."""

    def test_none_stats_no_data(self):
        tier, src, _ = auto_classify(None)
        self.assertEqual(tier, "tier_3_squad")
        self.assertEqual(src, "auto_no_data")

    def test_zero_minutes_no_data(self):
        tier, src, _ = auto_classify(
            PlayerStats(minutes=0, team_top_minutes=1000))
        self.assertEqual(tier, "tier_3_squad")
        self.assertEqual(src, "auto_no_data")

    def test_star_outfield_meets_both_thresholds(self):
        # 950/1000 share = 0.95; 10 G + 4 A in 950 min = ~1.33 per 90.
        s = PlayerStats(minutes=950, team_top_minutes=1000,
                        goals=10, assists=4, position="F")
        tier, src, comp = auto_classify(s)
        self.assertEqual(tier, "tier_1_star")
        self.assertEqual(src, "auto_star")
        self.assertGreaterEqual(comp["minutes_share"],
                                THRESHOLDS["tier_1_star_minutes_share"])
        self.assertGreaterEqual(comp["ga90"],
                                THRESHOLDS["tier_1_star_ga90_outfield"])

    def test_high_minutes_low_ga90_drops_to_starter_not_star(self):
        # 0.95 minutes share but no goal contribution — defensive midfielder
        # pattern. Must NOT be tier_1_star.
        s = PlayerStats(minutes=950, team_top_minutes=1000,
                        goals=0, assists=1, position="M")
        tier, _, _ = auto_classify(s)
        self.assertEqual(tier, "tier_2_starter")

    def test_keeper_first_choice(self):
        s = PlayerStats(minutes=800, team_top_minutes=1000,
                        appearances=10, clean_sheets=4, position="G")
        tier, src, _ = auto_classify(s)
        self.assertEqual(tier, "tier_1_keeper")
        self.assertEqual(src, "auto_keeper")

    def test_keeper_defining_player_star(self):
        # GK with very high CS share AND high minutes — promoted to star.
        s = PlayerStats(minutes=950, team_top_minutes=1000,
                        appearances=10, clean_sheets=9, position="G")
        tier, src, _ = auto_classify(s)
        self.assertEqual(tier, "tier_1_star")
        self.assertEqual(src, "auto_star")

    def test_starter_threshold(self):
        s = PlayerStats(minutes=600, team_top_minutes=1000,
                        goals=1, assists=0, position="D")
        tier, src, _ = auto_classify(s)
        self.assertEqual(tier, "tier_2_starter")
        self.assertEqual(src, "auto_starter")

    def test_below_starter_threshold_is_squad(self):
        s = PlayerStats(minutes=300, team_top_minutes=1000,
                        goals=0, assists=0, position="M")
        tier, src, _ = auto_classify(s)
        self.assertEqual(tier, "tier_3_squad")
        self.assertEqual(src, "auto_minutes_low")

    def test_components_carry_signals_for_audit(self):
        s = PlayerStats(minutes=500, team_top_minutes=1000,
                        goals=2, assists=1, position="M")
        _, _, comp = auto_classify(s)
        # Disagreement-diff CLI relies on these keys — pinning the contract.
        for key in ("minutes_share", "ga90", "cs_share",
                    "is_gk", "minutes", "team_top_minutes"):
            self.assertIn(key, comp)


class TestPriorityChain(unittest.TestCase):
    """Phase 6 shadow-mode wiring — override > auto_tier > DEFAULT_TIER."""

    def _stub_index(self):
        # Hand-curated whitelist entry for Wakanda's T'Challa with a
        # replacement block (so classify_tier_internal returns the entry).
        return {
            "Wakanda": {
                "by_full": {
                    "tchalla": {
                        "team": "Wakanda", "name": "T'Challa",
                        "name_normalized": "tchalla",
                        "tier": "tier_1_star",
                        "replacement": {"name": "Shuri",
                                        "tier": "tier_2_starter",
                                        "elo_equiv": -9.6},
                    },
                },
                "by_last": {},
            },
        }

    def _stats_payload(self, **overrides):
        # Default payload mimics fetch_player_stats output for one team.
        base = {
            "team_top_minutes": 1000,
            "players": [
                {"name": "T'Challa", "minutes": 950, "goals": 12,
                 "assists": 3, "appearances": 12, "clean_sheets": 0,
                 "position": "F"},
                {"name": "M'Baku",   "minutes": 200, "goals": 0,
                 "assists": 0, "appearances": 5, "clean_sheets": 0,
                 "position": "D"},
                {"name": "Okoye",    "minutes": 900, "goals": 0,
                 "assists": 1, "appearances": 12, "clean_sheets": 0,
                 "position": "M"},
            ],
        }
        base.update(overrides)
        return base

    def test_override_wins_even_when_auto_disagrees(self):
        # Override says tier_1_star (the whitelist). Auto would also agree
        # here but we explicitly assert override path. Components must
        # carry the auto suggestion for diff visibility.
        idx = self._stub_index()
        tier, source, comp = classify_tier_with_overrides(
            "T'Challa", "Wakanda",
            player_stats_payload=self._stats_payload(),
            auto_tier_active=False,
            index=idx,
        )
        self.assertEqual(tier, "tier_1_star")
        self.assertEqual(source, "whitelist_full")
        self.assertEqual(comp["override_tier"], "tier_1_star")
        self.assertEqual(comp["auto_tier"], "tier_1_star")
        self.assertIn("auto_components", comp)

    def test_shadow_mode_falls_through_when_override_misses(self):
        # Okoye is NOT whitelisted. Auto would call her tier_2_starter
        # (high minutes share, no goals). In shadow mode (active=False)
        # the priority chain MUST still return DEFAULT_TIER — same as
        # pre-Phase-6 behaviour — so we don't silently change Elo.
        idx = self._stub_index()
        tier, source, comp = classify_tier_with_overrides(
            "Okoye", "Wakanda",
            player_stats_payload=self._stats_payload(),
            auto_tier_active=False,
            index=idx,
        )
        self.assertEqual(tier, "tier_2_starter")  # == DEFAULT_TIER
        self.assertEqual(source, "default")
        self.assertEqual(comp["override_tier"], None)
        # Auto suggestion is still carried for diff visibility.
        self.assertEqual(comp["auto_tier"], "tier_2_starter")
        self.assertEqual(comp["auto_source"], "auto_starter")
        self.assertFalse(comp["active"])

    def test_active_mode_uses_auto_tier_when_override_misses(self):
        # Same Okoye lookup but auto_tier_active=True now uses the auto
        # value as the answer. Source is the auto_* tag verbatim.
        idx = self._stub_index()
        tier, source, comp = classify_tier_with_overrides(
            "Okoye", "Wakanda",
            player_stats_payload=self._stats_payload(),
            auto_tier_active=True,
            index=idx,
        )
        self.assertEqual(tier, "tier_2_starter")
        self.assertEqual(source, "auto_starter")
        self.assertTrue(comp["active"])

    def test_active_mode_promotes_star_when_auto_says_so(self):
        # T'Challa stats meet tier_1_star threshold. If we pretend the
        # whitelist is empty (override misses) AND active=True, the auto
        # value drives the upgrade.
        idx = {}  # empty whitelist
        tier, source, comp = classify_tier_with_overrides(
            "T'Challa", "Wakanda",
            player_stats_payload=self._stats_payload(),
            auto_tier_active=True,
            index=idx,
        )
        self.assertEqual(tier, "tier_1_star")
        self.assertEqual(source, "auto_star")
        self.assertGreaterEqual(comp["auto_components"]["minutes_share"], 0.85)

    def test_missing_stats_payload_degrades_to_no_data(self):
        # No stats payload for the player → auto_tier returns
        # tier_3_squad / auto_no_data. In shadow mode the response is
        # still DEFAULT_TIER.
        idx = self._stub_index()
        tier, source, comp = classify_tier_with_overrides(
            "M'Baku", "Wakanda",
            player_stats_payload=None,
            auto_tier_active=False,
            index=idx,
        )
        self.assertEqual(tier, "tier_2_starter")
        self.assertEqual(source, "default")
        self.assertEqual(comp["auto_tier"], "tier_3_squad")
        self.assertEqual(comp["auto_source"], "auto_no_data")

    def test_active_mode_with_missing_stats_picks_no_data_squad(self):
        # auto_tier_active=True + no stats → still returns the auto value
        # (tier_3_squad). Operator sees the auto_no_data source.
        idx = {}  # empty whitelist
        tier, source, _ = classify_tier_with_overrides(
            "Random Reserve", "Wakanda",
            player_stats_payload={"team_top_minutes": 0, "players": []},
            auto_tier_active=True,
            index=idx,
        )
        self.assertEqual(tier, "tier_3_squad")
        self.assertEqual(source, "auto_no_data")


if __name__ == "__main__":
    unittest.main()
