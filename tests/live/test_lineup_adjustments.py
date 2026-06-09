"""
Unit tests for B.4 — lineup_adjustments + fetch_lineups normalisation.

No network; uses synthetic /fixtures/lineups responses.

Run:
    python3 tests/live/test_lineup_adjustments.py
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

from lineup_adjustments import (  # noqa: E402
    extract_starting_xi, compute_lineup_delta_elo,
    GK_SWAP_ELO, HEAVY_ROTATION_ELO, HEAVY_ROTATION_THRESHOLD,
)
import fetch_lineups  # noqa: E402


def _xi(gk_id: int, outfield_ids: list[int]) -> dict:
    """Helper: build an extract_starting_xi-style dict directly."""
    return {
        "gk_id": gk_id,
        "outfield_ids": set(outfield_ids),
        "raw_players": [],
    }


class TestExtractStartingXi(unittest.TestCase):
    def test_parses_gk_and_outfield(self):
        side_block = {"startXI": [
            {"player": {"id": 1, "name": "Keeper", "pos": "G"}},
            {"player": {"id": 2, "name": "DF1", "pos": "D"}},
            {"player": {"id": 3, "name": "MF1", "pos": "M"}},
            {"player": {"id": 4, "name": "FW1", "pos": "F"}},
        ]}
        xi = extract_starting_xi(side_block)
        self.assertEqual(xi["gk_id"], 1)
        self.assertEqual(xi["outfield_ids"], {2, 3, 4})

    def test_handles_missing_ids(self):
        side_block = {"startXI": [
            {"player": {"name": "NoId", "pos": "G"}},
            {"player": {"id": 5, "pos": "D"}},
        ]}
        xi = extract_starting_xi(side_block)
        self.assertIsNone(xi["gk_id"])
        self.assertEqual(xi["outfield_ids"], {5})

    def test_empty_block(self):
        self.assertEqual(extract_starting_xi({}),
                         {"gk_id": None, "outfield_ids": set(), "raw_players": []})


class TestComputeLineupDeltaElo(unittest.TestCase):
    def test_no_baseline_zero(self):
        """First recorded XI for a team — no baseline → 0 Elo (display only)."""
        delta, reason = compute_lineup_delta_elo(
            prior_xi=None, current_xi=_xi(1, [2, 3, 4]))
        self.assertEqual(delta, 0.0)
        self.assertIsNone(reason)

    def test_identical_xi_zero(self):
        prior = _xi(1, [2, 3, 4, 5, 6, 7, 8, 9, 10, 11])
        curr = _xi(1, [2, 3, 4, 5, 6, 7, 8, 9, 10, 11])
        delta, reason = compute_lineup_delta_elo(prior, curr)
        self.assertEqual(delta, 0.0)
        self.assertIsNone(reason)

    def test_gk_swap_only(self):
        prior = _xi(1, [2, 3, 4])
        curr = _xi(99, [2, 3, 4])
        delta, reason = compute_lineup_delta_elo(prior, curr)
        self.assertEqual(delta, GK_SWAP_ELO)
        self.assertIn("GK swap", reason)

    def test_heavy_rotation(self):
        """3+ outfield changes triggers HEAVY_ROTATION_ELO."""
        prior = _xi(1, [2, 3, 4, 5, 6, 7, 8, 9, 10, 11])
        # Replace 3 outfield players
        curr = _xi(1, [20, 30, 40, 5, 6, 7, 8, 9, 10, 11])
        delta, reason = compute_lineup_delta_elo(prior, curr)
        self.assertEqual(delta, HEAVY_ROTATION_ELO)
        self.assertIn("outfield changes", reason)

    def test_below_rotation_threshold(self):
        """Only 1-2 outfield changes → 0 Elo (within tactical noise)."""
        prior = _xi(1, [2, 3, 4, 5, 6, 7, 8, 9, 10, 11])
        curr = _xi(1, [20, 3, 4, 5, 6, 7, 8, 9, 10, 11])  # one swap
        delta, reason = compute_lineup_delta_elo(prior, curr)
        self.assertEqual(delta, 0.0)
        self.assertIsNone(reason)

    def test_gk_swap_plus_rotation_stacks(self):
        prior = _xi(1, [2, 3, 4, 5, 6, 7, 8, 9, 10, 11])
        curr = _xi(99, [20, 30, 40, 5, 6, 7, 8, 9, 10, 11])
        delta, reason = compute_lineup_delta_elo(prior, curr)
        self.assertEqual(delta, GK_SWAP_ELO + HEAVY_ROTATION_ELO)
        self.assertIn("GK swap", reason)
        self.assertIn("outfield", reason)

    def test_empty_current_xi(self):
        """Lineups response empty → 0 Elo, don't crash."""
        prior = _xi(1, [2, 3])
        curr = _xi(None, [])
        delta, reason = compute_lineup_delta_elo(prior, curr)
        self.assertEqual(delta, 0.0)
        self.assertIsNone(reason)


class TestFixturesInWindow(unittest.TestCase):
    def test_only_within_window(self):
        now = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)
        schedule = [
            {"m": 1, "date": "2026-06-12", "time": "15:00", "home": "A", "away": "B", "venue": "X"},  # +1h in
            {"m": 2, "date": "2026-06-12", "time": "20:00", "home": "C", "away": "D", "venue": "Y"},  # +6h out
            {"m": 3, "date": "2026-06-11", "time": "13:00", "home": "E", "away": "F", "venue": "Z"},  # past
        ]
        upcoming = fetch_lineups.fixtures_in_window(schedule, hours_ahead=4, now=now)
        self.assertEqual([s["m"] for s in upcoming], [1])

    def test_just_after_kickoff_included(self):
        """Match in progress (started 10min ago) still in window — lineups
        are most useful just before AND just after kickoff."""
        now = datetime(2026, 6, 12, 15, 10, tzinfo=timezone.utc)
        schedule = [{"m": 1, "date": "2026-06-12", "time": "15:00",
                     "home": "A", "away": "B", "venue": "X"}]
        upcoming = fetch_lineups.fixtures_in_window(schedule, hours_ahead=4, now=now)
        self.assertEqual([s["m"] for s in upcoming], [1])


class TestBuildLineupEntry(unittest.TestCase):
    def test_first_recording_zero_elo(self):
        sched = {"m": 12, "home": "France", "away": "Senegal"}
        response_sides = [
            {"team": {"name": "France"},
             "startXI": [{"player": {"id": i, "name": f"F{i}",
                                     "pos": "G" if i == 1 else "M"}}
                         for i in range(1, 12)]},
            {"team": {"name": "Senegal"},
             "startXI": [{"player": {"id": i, "name": f"S{i}",
                                     "pos": "G" if i == 1 else "M"}}
                         for i in range(100, 111)]},
        ]
        entry = fetch_lineups.build_lineup_entry(sched, response_sides, prior_xis={})
        self.assertEqual(entry["match_id"], 12)
        self.assertEqual(entry["home_team_adjustment_elo"], 0.0)
        self.assertEqual(entry["away_team_adjustment_elo"], 0.0)
        self.assertEqual(entry["baseline_source"], "none:first_recorded_xi")
        self.assertEqual(len(entry["home_xi"]), 11)
        self.assertEqual(len(entry["away_xi"]), 11)

    def test_baseline_present_triggers_delta(self):
        prior = {
            "France": _xi(1, list(range(2, 12))),
        }
        sched = {"m": 12, "home": "France", "away": "Senegal"}
        # France: swap GK
        response = [
            {"team": {"name": "France"},
             "startXI": [{"player": {"id": 999 if i == 1 else i,
                                     "name": f"F{i}",
                                     "pos": "G" if i == 1 else "M"}}
                         for i in range(1, 12)]},
            {"team": {"name": "Senegal"}, "startXI": []},
        ]
        entry = fetch_lineups.build_lineup_entry(sched, response, prior)
        self.assertEqual(entry["home_team_adjustment_elo"], GK_SWAP_ELO)
        self.assertIn("GK swap", entry["home_adjustment_reason"])
        self.assertEqual(entry["baseline_source"], "lineups_2026.json:prior_match")


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
