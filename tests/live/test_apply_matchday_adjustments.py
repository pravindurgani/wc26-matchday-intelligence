"""
Unit tests for B.1 + B.3 — apply_matchday_adjustments.py.

Verifies:
  - Empty state path: all feeds missing → returns 0 components, no
    warnings about non-feed flags, get_team_elo_adjustment returns 0.0.
  - Weather components: per-side adjustments applied with ±15 cap.
  - Lineup components: per-side with ±20 cap.
  - Stats proxy: per-match ±8 + group-stage ±20 cap, truncates over-budget
    components.
  - Aggregate matchday cap (±35) clamps the sum across layers.
  - Audit log appends one JSONL record per call.
  - get_team_elo_adjustment public API returns the right per-team sum.
  - B.3: injuries loaded from API file + manual overlay, both capped,
    legacy semantics (approved default True, doubtful 0.5x, expires_at
    filter) preserved.

Tests use tempfile fixture files so we don't touch the real repo state.

Run:
    python3 tests/live/test_apply_matchday_adjustments.py
"""
from __future__ import annotations

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

import apply_matchday_adjustments as amd  # noqa: E402


class _TempFeeds:
    """Context manager: writes synthetic feed JSONs into a temp dir,
    monkeypatches the module's LIVE / DASH paths to point at it."""

    def __init__(self, feeds: dict[str, dict]):
        # feeds = {"weather_2026.json": {...}, "lineups_2026.json": {...}, ...}
        self.feeds = feeds
        self.tmp = None

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmp.name)
        for name, payload in self.feeds.items():
            (tmp_path / name).write_text(json.dumps(payload))
        # Monkeypatch the LIVE + DASH + LOG_PATH + OUT_PATH constants.
        self._patches = [
            patch.object(amd, "LIVE", tmp_path),
            patch.object(amd, "DASH", tmp_path),
            patch.object(amd, "LOG_PATH", tmp_path / "matchday_intelligence_log.jsonl"),
            patch.object(amd, "OUT_PATH", tmp_path / "matchday_intelligence.json"),
        ]
        for p in self._patches:
            p.start()
        # Force a state-cache reload so subsequent get_* calls see the new feeds.
        amd._STATE_CACHE = None
        return tmp_path

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        amd._STATE_CACHE = None
        self.tmp.cleanup()


class TestEmptyState(unittest.TestCase):
    """All feeds missing → clean zero state, no false warnings."""

    def test_no_feeds_returns_zero_components(self):
        with _TempFeeds({}):
            state = amd.build_adjustments_state()
        self.assertEqual(state["summary"]["total_active_components"], 0)
        self.assertEqual(state["summary"]["teams_affected"], 0)

    def test_no_feeds_emits_one_warning_per_real_feed(self):
        """Four real feeds (injuries, weather, lineups, stats_proxy) → four warnings.
        The meta-flag injuries_handled_by_this_module must NOT produce a warning."""
        with _TempFeeds({}):
            state = amd.build_adjustments_state()
        warning_feeds = sorted([w["feed"] for w in state["warnings"]])
        self.assertEqual(warning_feeds,
                         ["injuries", "lineups", "stats_proxy", "weather"],
                         "expected exactly 4 feed_missing warnings — not the "
                         "meta-flag")

    def test_get_team_elo_adjustment_zero_when_empty(self):
        with _TempFeeds({}):
            self.assertEqual(amd.get_team_elo_adjustment("Spain"), 0.0)
            self.assertEqual(amd.get_team_elo_adjustment("Spain", match_id=12), 0.0)


class TestWeatherLayer(unittest.TestCase):
    """Weather adjustments respect ±15 cap and aggregate properly."""

    def test_simple_weather_under_cap(self):
        feeds = {"weather_2026.json": {
            "weather": [{
                "match_id": 12, "home": "France", "away": "Senegal",
                "home_team": "France", "away_team": "Senegal",
                "home_team_adjustment_elo": -8.0,
                "away_team_adjustment_elo": 3.0,
                "weather_bucket": "hot_humid", "confidence": "forecast",
            }]
        }}
        with _TempFeeds(feeds):
            state = amd.build_adjustments_state()
            self.assertEqual(
                amd.get_team_elo_adjustment("France", match_id=12), -8.0)
            self.assertEqual(
                amd.get_team_elo_adjustment("Senegal", match_id=12), 3.0)
        comps = [c for entry in state["active_adjustments"]
                 for c in entry["components"]]
        self.assertTrue(any(c["type"] == "weather" for c in comps))

    def test_weather_exceeds_cap_clamps(self):
        feeds = {"weather_2026.json": {
            "weather": [{
                "match_id": 12, "home": "A", "away": "B",
                "home_team": "A", "away_team": "B",
                "home_team_adjustment_elo": -50.0,  # > ±15 cap
                "away_team_adjustment_elo": 100.0,
                "weather_bucket": "extreme_heat",
            }]
        }}
        with _TempFeeds(feeds):
            self.assertEqual(amd.get_team_elo_adjustment("A", match_id=12), -15.0)
            self.assertEqual(amd.get_team_elo_adjustment("B", match_id=12), 15.0)


class TestLineupLayer(unittest.TestCase):
    """Lineup adjustments respect ±20 cap."""

    def test_lineup_simple(self):
        feeds = {"lineups_2026.json": {
            "lineups": [{
                "match_id": 5, "home": "Brazil", "away": "Croatia",
                "home_team_adjustment_elo": -15.0,
                "away_team_adjustment_elo": 0.0,
                "home_adjustment_reason": "second-choice GK",
            }]
        }}
        with _TempFeeds(feeds):
            self.assertEqual(amd.get_team_elo_adjustment("Brazil", match_id=5), -15.0)
            self.assertEqual(amd.get_team_elo_adjustment("Croatia", match_id=5), 0.0)

    def test_lineup_clamps_at_cap(self):
        feeds = {"lineups_2026.json": {
            "lineups": [{
                "match_id": 5, "home": "Brazil", "away": "Croatia",
                "home_team_adjustment_elo": -50.0, "away_team_adjustment_elo": 0.0,
            }]
        }}
        with _TempFeeds(feeds):
            self.assertEqual(amd.get_team_elo_adjustment("Brazil", match_id=5), -20.0)


class TestStatsProxyLayer(unittest.TestCase):
    """Stats proxy: per-match ±8 + group ±20 caps, no fake xG label."""

    def test_under_caps(self):
        feeds = {"match_stats_2026.json": {
            "matches": [
                {"match_id": 1, "status": "FT", "home": "Spain", "away": "Italy",
                 "home_form_adjustment_elo": 5.0, "away_form_adjustment_elo": -3.0,
                 "true_xg_available": False},
            ]
        }}
        with _TempFeeds(feeds):
            self.assertEqual(amd.get_team_elo_adjustment("Spain"), 5.0)
            self.assertEqual(amd.get_team_elo_adjustment("Italy"), -3.0)

    def test_per_match_cap(self):
        feeds = {"match_stats_2026.json": {
            "matches": [
                {"match_id": 1, "status": "FT", "home": "Spain", "away": "X",
                 "home_form_adjustment_elo": 30.0, "away_form_adjustment_elo": 0.0},
            ]
        }}
        with _TempFeeds(feeds):
            # 30 → clamped to per-match cap +8
            self.assertEqual(amd.get_team_elo_adjustment("Spain"), 8.0)

    def test_group_total_cap_truncates_later_matches(self):
        """3 matches × +8 each = 24 but group cap = 20. 3rd match contributes
        only 4 of its 8."""
        feeds = {"match_stats_2026.json": {
            "matches": [
                {"match_id": 1, "status": "FT", "home": "Spain", "away": "X",
                 "home_form_adjustment_elo": 8.0, "away_form_adjustment_elo": 0.0},
                {"match_id": 2, "status": "FT", "home": "Spain", "away": "Y",
                 "home_form_adjustment_elo": 8.0, "away_form_adjustment_elo": 0.0},
                {"match_id": 3, "status": "FT", "home": "Spain", "away": "Z",
                 "home_form_adjustment_elo": 8.0, "away_form_adjustment_elo": 0.0},
            ]
        }}
        with _TempFeeds(feeds):
            # 8 + 8 + (20-16)=4 = 20 total
            self.assertAlmostEqual(amd.get_team_elo_adjustment("Spain"), 20.0)

    def test_non_ft_match_ignored(self):
        feeds = {"match_stats_2026.json": {
            "matches": [
                {"match_id": 1, "status": "2H", "home": "Spain", "away": "X",
                 "home_form_adjustment_elo": 5.0, "away_form_adjustment_elo": 0.0},
            ]
        }}
        with _TempFeeds(feeds):
            self.assertEqual(amd.get_team_elo_adjustment("Spain"), 0.0)


class TestAggregateCap(unittest.TestCase):
    """When weather + lineup combine to exceed ±35 per team-match, clamp."""

    def test_aggregate_matchday_cap_clamps(self):
        feeds = {
            "weather_2026.json": {"weather": [{
                "match_id": 12, "home": "A", "away": "B",
                "home_team": "A", "away_team": "B",
                "home_team_adjustment_elo": -15.0, "away_team_adjustment_elo": 0.0,
                "weather_bucket": "extreme_heat",
            }]},
            "lineups_2026.json": {"lineups": [{
                "match_id": 12, "home": "A", "away": "B",
                "home_team_adjustment_elo": -20.0, "away_team_adjustment_elo": 0.0,
            }]},
        }
        with _TempFeeds(feeds):
            # -15 + -20 = -35 (exactly at cap, no clamp)
            self.assertEqual(amd.get_team_elo_adjustment("A", match_id=12), -35.0)

    def test_aggregate_cap_clamps_when_above(self):
        # Push both layers to their individual caps then ensure sum stops at -35
        feeds = {
            "weather_2026.json": {"weather": [{
                "match_id": 12, "home": "A", "away": "B",
                "home_team": "A", "away_team": "B",
                "home_team_adjustment_elo": -50.0, "away_team_adjustment_elo": 0.0,
                "weather_bucket": "extreme_heat",
            }]},
            "lineups_2026.json": {"lineups": [{
                "match_id": 12, "home": "A", "away": "B",
                "home_team_adjustment_elo": -50.0, "away_team_adjustment_elo": 0.0,
            }]},
        }
        with _TempFeeds(feeds):
            # Individual caps: weather -15, lineup -20. Sum: -35 (at aggregate cap).
            self.assertEqual(amd.get_team_elo_adjustment("A", match_id=12), -35.0)


class TestInjuriesLayer(unittest.TestCase):
    """B.3: injuries from injuries_2026.json (API) + team_adjustments.json (manual)."""

    def test_api_only_under_cap(self):
        """API-sourced injuries aggregate per team, capped at INJURY_CAP_NORMAL (±25)."""
        feeds = {"injuries_2026.json": {
            "teams": {
                "France": {"total_elo_adjustment": -18.0,
                           "players": [{"name": "Player A", "elo": -12.0},
                                       {"name": "Player B", "elo": -6.0}]},
            }
        }}
        with _TempFeeds(feeds):
            self.assertEqual(amd.get_team_elo_adjustment("France"), -18.0)

    def test_api_clamps_at_normal_cap(self):
        feeds = {"injuries_2026.json": {
            "teams": {
                "France": {"total_elo_adjustment": -100.0, "players": []},
            }
        }}
        with _TempFeeds(feeds):
            self.assertEqual(amd.get_team_elo_adjustment("France"),
                             -amd.INJURY_CAP_NORMAL)

    def test_manual_overlay_only_legacy_semantics(self):
        """Manual overlay honours approved (default True), doubtful 0.5x,
        and expires_at filter."""
        feeds = {"team_adjustments.json": {
            "adjustments": [
                {"team": "Spain", "adjustment_elo": -12.0,
                 "status": "confirmed_out",
                 "expires_at": "2099-01-01T00:00:00+00:00"},
                {"team": "Spain", "adjustment_elo": -12.0,
                 "status": "doubtful",
                 "expires_at": "2099-01-01T00:00:00+00:00"},
                {"team": "Spain", "adjustment_elo": -30.0,
                 "status": "confirmed_out",
                 "expires_at": "2000-01-01T00:00:00+00:00"},  # expired → ignored
                {"team": "Spain", "adjustment_elo": -30.0,
                 "approved": False,
                 "status": "confirmed_out"},  # unapproved → ignored
            ]
        }}
        with _TempFeeds(feeds):
            # -12 (confirmed) + -12 * 0.5 (doubtful) = -18
            self.assertEqual(amd.get_team_elo_adjustment("Spain"), -18.0)

    def test_overlay_uses_extreme_cap(self):
        """Manual overlay can push toward INJURY_CAP_EXTREME (±35)."""
        feeds = {"team_adjustments.json": {
            "adjustments": [{
                "team": "Brazil", "adjustment_elo": -100.0,
                "status": "confirmed_out",
                "expires_at": "2099-01-01T00:00:00+00:00",
            }]
        }}
        with _TempFeeds(feeds):
            self.assertEqual(amd.get_team_elo_adjustment("Brazil"),
                             -amd.INJURY_CAP_EXTREME)

    def test_api_and_overlay_stack_within_aggregate_cap(self):
        """API + manual overlay stack per team — clamped by aggregate matchday
        cap (35) since both flow into the same (team, None) bucket."""
        feeds = {
            "injuries_2026.json": {"teams": {
                "Germany": {"total_elo_adjustment": -20.0, "players": []},
            }},
            "team_adjustments.json": {"adjustments": [{
                "team": "Germany", "adjustment_elo": -25.0,
                "status": "confirmed_out",
                "expires_at": "2099-01-01T00:00:00+00:00",
            }]},
        }
        with _TempFeeds(feeds):
            # API -20 (under normal cap 25) + overlay -25 (under extreme cap 35)
            # → raw sum -45 → aggregate matchday cap clamps to -35.
            self.assertEqual(amd.get_team_elo_adjustment("Germany"),
                             -amd.AGGREGATE_MATCHDAY_CAP)

    def test_feeds_available_flips_to_handled(self):
        with _TempFeeds({"injuries_2026.json": {"teams": {}}}):
            state = amd.build_adjustments_state()
            self.assertTrue(
                state["feeds_available"]["injuries_handled_by_this_module"])
            self.assertTrue(state["feeds_available"]["injuries"])


class TestAuditLog(unittest.TestCase):
    """Audit log appends one JSONL record per write_state_and_log call."""

    def test_appends_one_record_per_call(self):
        with _TempFeeds({}) as tmp:
            log_path = tmp / "matchday_intelligence_log.jsonl"
            amd.write_state_and_log()
            amd.write_state_and_log()
            amd.write_state_and_log()
            lines = log_path.read_text().strip().splitlines()
            self.assertEqual(len(lines), 3)
            for line in lines:
                rec = json.loads(line)
                self.assertIn("ts", rec)
                self.assertIn("summary", rec)
                self.assertIn("feeds_available", rec)

    def test_dashboard_json_written_atomically(self):
        with _TempFeeds({}) as tmp:
            out_path = tmp / "matchday_intelligence.json"
            amd.write_state_and_log()
            self.assertTrue(out_path.exists())
            data = json.loads(out_path.read_text())
            self.assertEqual(data["schema_version"], 1)
            self.assertIn("caps", data)
            self.assertIn("active_adjustments", data)


def _summary(result):
    print()
    print(f"  Ran {result.testsRun} tests")
    if result.wasSuccessful():
        print(f"  ✓ all passed")
    else:
        print(f"  ✗ {len(result.failures)} failures, {len(result.errors)} errors")


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    _summary(result)
    sys.exit(0 if result.wasSuccessful() else 1)
