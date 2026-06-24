"""
Unit tests for B.5 — stats_proxy_adjustments + fetch_match_stats normalisation.

No network; synthetic /fixtures/statistics responses.

Run:
    python3 tests/live/test_stats_proxy.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

from stats_proxy_adjustments import (  # noqa: E402
    stats_to_dict, compute_form_delta, both_form_deltas,
    compute_xg_form_delta,
    STATS_PROXY_RAW_CAP, POSSESSION_DEADZONE_PP, XG_EDGE_WEIGHT,
)
import fetch_match_stats  # noqa: E402


class TestStatsToDict(unittest.TestCase):
    def test_parses_ints_and_percent_strings(self):
        d = stats_to_dict([
            {"type": "Shots on Goal", "value": 6},
            {"type": "Ball Possession", "value": "57%"},
            {"type": "Corner Kicks", "value": "4"},
            {"type": "Empty Field", "value": None},
        ])
        self.assertEqual(d["Shots on Goal"], 6)
        self.assertEqual(d["Ball Possession"], 57)
        self.assertEqual(d["Corner Kicks"], 4)
        self.assertIsNone(d["Empty Field"])

    def test_empty_list(self):
        self.assertEqual(stats_to_dict([]), {})

    def test_none_safe(self):
        self.assertEqual(stats_to_dict(None), {})


class TestComputeFormDelta(unittest.TestCase):
    def test_dominant_team_positive_delta(self):
        own = {"Shots on Goal": 8, "Ball Possession": 65, "Corner Kicks": 7}
        opp = {"Shots on Goal": 2, "Ball Possession": 35, "Corner Kicks": 2}
        delta = compute_form_delta(own, opp)
        self.assertGreater(delta, 0)
        # Should be sizeable but capped
        self.assertLessEqual(delta, STATS_PROXY_RAW_CAP)

    def test_dominated_team_negative_delta(self):
        own = {"Shots on Goal": 2, "Ball Possession": 35, "Corner Kicks": 2}
        opp = {"Shots on Goal": 8, "Ball Possession": 65, "Corner Kicks": 7}
        delta = compute_form_delta(own, opp)
        self.assertLess(delta, 0)
        self.assertGreaterEqual(delta, -STATS_PROXY_RAW_CAP)

    def test_balanced_match_near_zero(self):
        own = {"Shots on Goal": 4, "Ball Possession": 50, "Corner Kicks": 5}
        opp = {"Shots on Goal": 4, "Ball Possession": 50, "Corner Kicks": 5}
        self.assertEqual(compute_form_delta(own, opp), 0.0)

    def test_missing_possession_safe(self):
        own = {"Shots on Goal": 5, "Corner Kicks": 3}
        opp = {"Shots on Goal": 3, "Corner Kicks": 2}
        delta = compute_form_delta(own, opp)
        self.assertGreater(delta, 0)  # shot edge still scores

    def test_raw_cap_enforced(self):
        own = {"Shots on Goal": 100, "Ball Possession": 99, "Corner Kicks": 50}
        opp = {"Shots on Goal": 0, "Ball Possession": 1, "Corner Kicks": 0}
        delta = compute_form_delta(own, opp)
        self.assertEqual(delta, STATS_PROXY_RAW_CAP)

    def test_both_form_deltas_are_signed_pair(self):
        home = [{"type": "Shots on Goal", "value": 7},
                {"type": "Ball Possession", "value": "60%"},
                {"type": "Corner Kicks", "value": 6}]
        away = [{"type": "Shots on Goal", "value": 3},
                {"type": "Ball Possession", "value": "40%"},
                {"type": "Corner Kicks", "value": 3}]
        h_d, a_d = both_form_deltas(home, away)
        self.assertGreater(h_d, 0)
        self.assertLess(a_d, 0)
        # By symmetry they should be exact negatives of each other
        self.assertAlmostEqual(h_d, -a_d, places=4)


class TestBuildMatchEntry(unittest.TestCase):
    def test_schema_and_xg_flag_locked_false(self):
        match = {"m": 1, "home": "Mexico", "away": "South Africa", "status": "FT"}
        response = [
            {"team": {"name": "Mexico"},
             "statistics": [{"type": "Shots on Goal", "value": 6},
                            {"type": "Ball Possession", "value": "55%"},
                            {"type": "Corner Kicks", "value": 5}]},
            {"team": {"name": "South Africa"},
             "statistics": [{"type": "Shots on Goal", "value": 2},
                            {"type": "Ball Possession", "value": "45%"},
                            {"type": "Corner Kicks", "value": 2}]},
        ]
        entry = fetch_match_stats.build_match_entry(match, response, "1489369")
        self.assertEqual(entry["match_id"], 1)
        self.assertEqual(entry["status"], "FT")
        self.assertFalse(entry["true_xg_available"],
                         "true_xg_available must be False — spec lock")
        # Honesty flags: we always check for xG; provider didn't return it.
        self.assertTrue(entry["xg_attempted"])
        self.assertFalse(entry["xg_found"])
        self.assertGreater(entry["home_form_adjustment_elo"], 0)
        self.assertLess(entry["away_form_adjustment_elo"], 0)
        self.assertEqual(entry["fixture_id"], "1489369")
        self.assertIn("Shots on Goal", entry["home_stats"])

    def test_handles_missing_team_block(self):
        """If the stats endpoint returns one side only (rare), don't crash."""
        match = {"m": 1, "home": "A", "away": "B", "status": "FT"}
        response = [{"team": {"name": "A"},
                     "statistics": [{"type": "Shots on Goal", "value": 5}]}]
        entry = fetch_match_stats.build_match_entry(match, response, "1")
        self.assertIsNotNone(entry["home_form_adjustment_elo"])
        self.assertIsNotNone(entry["away_form_adjustment_elo"])


class TestPossessionDeadzone(unittest.TestCase):
    """Phase 3 (b): possession edges within ±5pp are noise."""

    def test_possession_within_deadzone_contributes_zero(self):
        # 53/47 → within ±5pp; shots & corners balanced ⇒ delta == 0.
        own = {"Shots on Goal": 4, "Ball Possession": 53, "Corner Kicks": 5}
        opp = {"Shots on Goal": 4, "Ball Possession": 47, "Corner Kicks": 5}
        self.assertEqual(compute_form_delta(own, opp), 0.0)

    def test_possession_just_beyond_deadzone_credits_only_excess(self):
        # 56/44 → 6pp above 50 → only 1pp credited after deadzone.
        own = {"Shots on Goal": 0, "Ball Possession": 56, "Corner Kicks": 0}
        opp = {"Shots on Goal": 0, "Ball Possession": 44, "Corner Kicks": 0}
        delta = compute_form_delta(own, opp)
        # (56 - 50 - 5) * 0.06 = 0.06
        self.assertAlmostEqual(delta, 0.06, places=4)

    def test_deadzone_constant_is_5pp(self):
        self.assertEqual(POSSESSION_DEADZONE_PP, 5.0)


class TestComputeXgFormDelta(unittest.TestCase):
    """Phase 3 (c): real-xG branch helper. Dead by default upstream."""

    def test_positive_xg_edge(self):
        # 1.5 xG advantage ⇒ 9 Elo
        self.assertAlmostEqual(compute_xg_form_delta(2.5, 1.0), 1.5 * XG_EDGE_WEIGHT)

    def test_xg_branch_is_capped(self):
        self.assertEqual(compute_xg_form_delta(10.0, 0.0), STATS_PROXY_RAW_CAP)
        self.assertEqual(compute_xg_form_delta(0.0, 10.0), -STATS_PROXY_RAW_CAP)

    def test_xg_branch_symmetric(self):
        self.assertAlmostEqual(
            compute_xg_form_delta(1.8, 0.6),
            -compute_xg_form_delta(0.6, 1.8),
        )


class TestXgFlagGating(unittest.TestCase):
    """Phase 3 (c): build_match_entry uses xG only when both flag + data align."""

    _RESP_WITH_XG = [
        {"team": {"name": "Mexico"},
         "statistics": [{"type": "Shots on Goal", "value": 4},
                        {"type": "Ball Possession", "value": "52%"},
                        {"type": "Corner Kicks", "value": 4},
                        {"type": "Expected Goals", "value": "2.4"}]},
        {"team": {"name": "South Africa"},
         "statistics": [{"type": "Shots on Goal", "value": 3},
                        {"type": "Ball Possession", "value": "48%"},
                        {"type": "Corner Kicks", "value": 3},
                        {"type": "Expected Goals", "value": "0.6"}]},
    ]
    _MATCH = {"m": 1, "home": "Mexico", "away": "South Africa", "status": "FT"}

    def test_xg_found_but_flag_off_keeps_proxy(self):
        # Default: XG_ENABLED is False ⇒ flag-gated branch dormant.
        entry = fetch_match_stats.build_match_entry(
            self._MATCH, self._RESP_WITH_XG, "1489369",
        )
        self.assertTrue(entry["xg_attempted"])
        self.assertTrue(entry["xg_found"])
        self.assertFalse(entry["true_xg_available"])
        # Proxy formula's home delta with these stats is small (poss in deadzone).
        # 1 SoT edge * 1.2 + 1 corner edge * 0.3 = 1.5
        self.assertAlmostEqual(entry["home_form_adjustment_elo"], 1.5, places=3)

    def test_flag_on_with_data_uses_xg_branch(self):
        orig = fetch_match_stats.XG_ENABLED
        fetch_match_stats.XG_ENABLED = True
        try:
            entry = fetch_match_stats.build_match_entry(
                self._MATCH, self._RESP_WITH_XG, "1489369",
            )
        finally:
            fetch_match_stats.XG_ENABLED = orig
        self.assertTrue(entry["true_xg_available"])
        # 1.8 xG edge * 6.0 = 10.8, capped at 12 ⇒ 10.8
        self.assertAlmostEqual(entry["home_form_adjustment_elo"], 10.8, places=3)
        self.assertAlmostEqual(entry["away_form_adjustment_elo"], -10.8, places=3)

    def test_flag_on_without_data_falls_back_to_proxy(self):
        resp_no_xg = [
            {"team": {"name": "Mexico"},
             "statistics": [{"type": "Shots on Goal", "value": 4},
                            {"type": "Corner Kicks", "value": 4}]},
            {"team": {"name": "South Africa"},
             "statistics": [{"type": "Shots on Goal", "value": 3},
                            {"type": "Corner Kicks", "value": 3}]},
        ]
        orig = fetch_match_stats.XG_ENABLED
        fetch_match_stats.XG_ENABLED = True
        try:
            entry = fetch_match_stats.build_match_entry(
                self._MATCH, resp_no_xg, "1489369",
            )
        finally:
            fetch_match_stats.XG_ENABLED = orig
        # No Expected Goals returned ⇒ xg_found False, true_xg_available False.
        self.assertTrue(entry["xg_attempted"])
        self.assertFalse(entry["xg_found"])
        self.assertFalse(entry["true_xg_available"])

    def test_xg_enabled_module_constant_defaults_false(self):
        # Defense: the module ships with the gate closed.
        self.assertFalse(fetch_match_stats.XG_ENABLED)


class TestBuildSnapshot(unittest.TestCase):
    def test_snapshot_schema_and_notes(self):
        snap = fetch_match_stats.build_snapshot([], [])
        self.assertEqual(snap["schema_version"], 1)
        self.assertEqual(snap["source"], "api_football")
        self.assertIn("PROXY", snap["notes"])
        self.assertIn("NOT xG", snap["notes"])
        self.assertEqual(snap["n_completed"], 0)


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
