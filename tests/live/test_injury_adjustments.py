"""
Unit tests for B.3 — injury_adjustments pure helpers + fetch_injuries
record normalisation. No network calls.

Run:
    python3 tests/live/test_injury_adjustments.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

from injury_adjustments import (  # noqa: E402
    TIER_TO_ELO, DEFAULT_TIER, DOUBTFUL_DISCOUNT,
    classify_api_type, tier_elo, discounted_elo,
)
import fetch_injuries  # noqa: E402


class TestTierTable(unittest.TestCase):
    def test_all_tiers_negative(self):
        for tier, val in TIER_TO_ELO.items():
            self.assertLess(val, 0.0, f"{tier} must be a penalty")

    def test_tier_ordering(self):
        # star ≥ keeper in magnitude > starter > squad
        self.assertLess(TIER_TO_ELO["tier_1_star"], TIER_TO_ELO["tier_1_keeper"])
        self.assertLess(TIER_TO_ELO["tier_1_keeper"], TIER_TO_ELO["tier_2_starter"])
        self.assertLess(TIER_TO_ELO["tier_2_starter"], TIER_TO_ELO["tier_3_squad"])

    def test_default_tier_is_starter(self):
        # Conservative default chosen so depth-chart noise doesn't move model.
        self.assertEqual(DEFAULT_TIER, "tier_2_starter")
        self.assertEqual(tier_elo(DEFAULT_TIER), -12.0)


class TestClassifyApiType(unittest.TestCase):
    def test_missing_fixture_is_out(self):
        self.assertEqual(classify_api_type("Missing Fixture"), "confirmed_out")

    def test_questionable_is_doubtful(self):
        self.assertEqual(classify_api_type("Questionable"), "doubtful")

    def test_suspended_treated_as_out(self):
        self.assertEqual(classify_api_type("Suspended"), "confirmed_out")

    def test_unknown_defaults_out(self):
        """Unknown types fail-closed to confirmed_out — safer than 0."""
        self.assertEqual(classify_api_type("Made-up-status"), "confirmed_out")
        self.assertEqual(classify_api_type(None), "confirmed_out")


class TestDiscountedElo(unittest.TestCase):
    def test_confirmed_full_penalty(self):
        self.assertEqual(discounted_elo("tier_2_starter", "confirmed_out"), -12.0)

    def test_doubtful_half_penalty(self):
        self.assertEqual(discounted_elo("tier_2_starter", "doubtful"),
                         -12.0 * DOUBTFUL_DISCOUNT)

    def test_unknown_status_zero(self):
        """Unknown statuses earn 0 — better to be quiet than leak Elo."""
        self.assertEqual(discounted_elo("tier_2_starter", "anything_else"), 0.0)


class TestNormaliseRecords(unittest.TestCase):
    """fetch_injuries.normalise_records turns raw API records into per-team totals."""

    def test_groups_players_per_team(self):
        records = [
            {"team": {"name": "France"},
             "player": {"name": "Player A", "type": "Missing Fixture",
                        "reason": "Knee"},
             "fixture": {"id": 1001}},
            {"team": {"name": "France"},
             "player": {"name": "Player B", "type": "Questionable",
                        "reason": "Ankle"},
             "fixture": {"id": 1001}},
            {"team": {"name": "Spain"},
             "player": {"name": "Player C", "type": "Missing Fixture",
                        "reason": "Hamstring"},
             "fixture": {"id": 1002}},
        ]
        teams, warnings = fetch_injuries.normalise_records(
            records, wc_teams={"France", "Spain"})
        self.assertEqual(set(teams.keys()), {"France", "Spain"})
        self.assertEqual(len(teams["France"]["players"]), 2)
        # France: -12 (confirmed_out) + -6 (doubtful) = -18
        self.assertAlmostEqual(teams["France"]["total_elo_adjustment"], -18.0)
        self.assertAlmostEqual(teams["Spain"]["total_elo_adjustment"], -12.0)
        self.assertEqual(warnings, [])

    def test_filters_non_wc_teams(self):
        records = [
            {"team": {"name": "France"},
             "player": {"name": "X", "type": "Missing Fixture"},
             "fixture": {"id": 1}},
            {"team": {"name": "Andorra"},  # not in WC
             "player": {"name": "Y", "type": "Missing Fixture"},
             "fixture": {"id": 2}},
        ]
        teams, warnings = fetch_injuries.normalise_records(
            records, wc_teams={"France"})
        self.assertEqual(set(teams.keys()), {"France"})
        # One warning about the filtered non-WC team
        types = {w["type"] for w in warnings}
        self.assertIn("filter_non_wc", types)

    def test_normalises_team_aliases(self):
        """API provider names map to canonical (e.g. Korea Republic → South Korea)."""
        records = [{
            "team": {"name": "Korea Republic"},
            "player": {"name": "X", "type": "Missing Fixture"},
            "fixture": {"id": 1},
        }]
        teams, _ = fetch_injuries.normalise_records(
            records, wc_teams={"South Korea"})
        self.assertIn("South Korea", teams)

    def test_skips_records_missing_team(self):
        records = [{"team": {}, "player": {"name": "X", "type": "Missing Fixture"}}]
        teams, warnings = fetch_injuries.normalise_records(
            records, wc_teams={"France"})
        self.assertEqual(teams, {})
        self.assertTrue(any(w["type"] == "skipped_bad_record" for w in warnings))

    def test_empty_records_clean_snapshot(self):
        teams, warnings = fetch_injuries.normalise_records([], wc_teams={"France"})
        self.assertEqual(teams, {})
        self.assertEqual(warnings, [])


class TestBuildSnapshot(unittest.TestCase):
    def test_snapshot_schema_keys(self):
        snap = fetch_injuries.build_snapshot([], [])
        for key in ("generated_at", "schema_version", "source",
                    "league_id", "season", "teams", "warnings",
                    "teams_with_injuries"):
            self.assertIn(key, snap)
        self.assertEqual(snap["source"], "api_football")
        self.assertEqual(snap["schema_version"], 1)


def _summary(result):
    print()
    print(f"  Ran {result.testsRun} tests")
    if result.wasSuccessful():
        print("  ✓ all passed")
    else:
        print(f"  ✗ {len(result.failures)} failures, {len(result.errors)} errors")


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    _summary(result)
    sys.exit(0 if result.wasSuccessful() else 1)
