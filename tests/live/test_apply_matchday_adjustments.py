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
        """Six real feeds (injuries, weather, referee, lineups, stats_proxy,
        suspensions) → six warnings. The meta-flag
        injuries_handled_by_this_module must NOT produce a warning."""
        with _TempFeeds({}):
            state = amd.build_adjustments_state()
        warning_feeds = sorted([w["feed"] for w in state["warnings"]])
        self.assertEqual(warning_feeds,
                         ["injuries", "lineups", "referee", "stats_proxy",
                          "suspensions", "weather"],
                         "expected exactly 6 feed_missing warnings — not the "
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


class TestStatsProxyDownweight(unittest.TestCase):
    """H7: stats_proxy is halved for teams that already have a live_team_state
    delta — both layers encode post-match form, so stacking them at full
    weight double-counts."""

    def test_no_live_state_full_weight(self):
        feeds = {"match_stats_2026.json": {"matches": [{
            "match_id": 5, "status": "FT",
            "home": "France", "away": "Argentina",
            "home_form_adjustment_elo": 6.0,
            "away_form_adjustment_elo": -4.0,
            "true_xg_available": False,
        }]}}
        with _TempFeeds(feeds):
            # Both queries inside the context; reload=True on first to force
            # the state cache to read the patched LIVE path.
            fr = amd.get_team_elo_adjustment("France", reload=True)
            ar = amd.get_team_elo_adjustment("Argentina")
        # No live_team_state.json present → both teams get full weight.
        self.assertAlmostEqual(fr, 6.0, places=2)
        self.assertAlmostEqual(ar, -4.0, places=2)

    def test_live_state_halves_proxy(self):
        feeds = {
            "match_stats_2026.json": {"matches": [{
                "match_id": 5, "status": "FT",
                "home": "France", "away": "Argentina",
                "home_form_adjustment_elo": 6.0,
                "away_form_adjustment_elo": -4.0,
                "true_xg_available": False,
            }]},
            "live_team_state.json": {
                "last_updated": "2026-06-12T00:00:00Z",
                # Schema written by update_team_state.py uses "deltas".
                "deltas": {"France": 5.0},
            },
        }
        with _TempFeeds(feeds):
            fr = amd.get_team_elo_adjustment("France", reload=True)
            ar = amd.get_team_elo_adjustment("Argentina")
        # France halved (6.0 → 3.0); Argentina untouched (no live state).
        self.assertAlmostEqual(fr, 3.0, places=2)
        self.assertAlmostEqual(ar, -4.0, places=2)


class TestUpstreamWarningLift(unittest.TestCase):
    """The consolidated state must surface upstream feed warnings so the
    dashboard (which reads matchday_intelligence.json[`warnings`]) can
    alert operators about ambiguous classifications, fetch errors, etc.

    Without this, fetch_injuries records a warning that nobody reads —
    the operator never sees that an Emiliano Martínez injury reported
    as 'Dibu Martinez' silently routed to tier_2_starter (-12) instead
    of tier_1_keeper (-25). The dashboard renders intel.warnings via
    renderMatchdayIntelligence (dashboard/app.js:1664)."""

    def test_lifts_ambiguous_classification_from_injuries(self):
        feeds = {"injuries_2026.json": {
            "teams": {},
            "warnings": [{
                "type": "ambiguous_classification",
                "count": 1,
                "cases": [{"team": "Argentina", "input": "Dibu Martinez",
                           "fixture_id": 1}],
                "message": "1 ambiguous classification(s) defaulted ...",
            }],
        }}
        with _TempFeeds(feeds):
            state = amd.build_adjustments_state()
        amb = [w for w in state["warnings"]
               if w.get("type") == "ambiguous_classification"]
        self.assertEqual(len(amb), 1)
        # The propagator tags `feed:` so the dashboard can scope alerts.
        self.assertEqual(amb[0]["feed"], "injuries")
        # The original case payload survives the lift.
        self.assertEqual(amb[0]["cases"][0]["input"], "Dibu Martinez")

    def test_ignores_benign_filter_warnings(self):
        """`filter_non_wc` is expected every cycle (qualifier carry-over)
        and would be noise on the dashboard. The lifter must NOT
        propagate it."""
        feeds = {"injuries_2026.json": {
            "teams": {},
            "warnings": [
                {"type": "filter_non_wc", "count": 3,
                 "message": "Skipped 3 records for teams not in WC2026"},
            ],
        }}
        with _TempFeeds(feeds):
            state = amd.build_adjustments_state()
        filt = [w for w in state["warnings"]
                if w.get("type") == "filter_non_wc"]
        self.assertEqual(filt, [])

    def test_lifts_fetch_errors_from_injuries(self):
        """API failures (http_error, fetch_error) must propagate so the
        dashboard pill turns on — otherwise a silent 500 from
        API-Football would let the operator believe data is fresh."""
        feeds = {"injuries_2026.json": {
            "teams": {},
            "warnings": [{"type": "http_error", "code": 503,
                          "body": "service unavailable"}],
        }}
        with _TempFeeds(feeds):
            state = amd.build_adjustments_state()
        errs = [w for w in state["warnings"] if w.get("type") == "http_error"]
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0]["feed"], "injuries")

    def test_lifts_no_records_returned_from_injuries(self):
        """`no_records_returned` is the empty-feed sentinel emitted by
        fetch_injuries when the API call succeeded but returned 0 records.
        It must propagate to the consolidated state so an operator can
        distinguish a genuinely quiet day from a misconfigured endpoint.

        Note: this type is intentionally OMITTED from the dashboard's
        INTEL_TOP_BAR_TYPES allowlist — surface in the matchday-intel
        detail block only, not the alert pill. Pinning the propagation
        here ensures it reaches the consolidated state where the detail
        renderer can read it."""
        feeds = {"injuries_2026.json": {
            "teams": {},
            "warnings": [{
                "type": "no_records_returned",
                "endpoint": "/injuries",
                "league": "1",
                "season": "2026",
                "message": "API returned 0 injury records ...",
            }],
        }}
        with _TempFeeds(feeds):
            state = amd.build_adjustments_state()
        info = [w for w in state["warnings"]
                if w.get("type") == "no_records_returned"]
        self.assertEqual(len(info), 1)
        self.assertEqual(info[0]["feed"], "injuries")
        self.assertEqual(info[0]["endpoint"], "/injuries")

    def test_no_double_lift_of_feed_missing(self):
        """`feed_missing` is generated locally by build_adjustments_state
        for absent feeds. Without the type filter the lifter would also
        re-lift any old `feed_missing` saved in the on-disk snapshot,
        producing duplicates. Verify the filter holds."""
        feeds = {"injuries_2026.json": {
            "teams": {},
            "warnings": [{"type": "feed_missing", "feed": "weather"}],
        }}
        with _TempFeeds(feeds):
            state = amd.build_adjustments_state()
        # Should see exactly the locally-generated `feed_missing` items
        # (weather, lineups, stats_proxy, referee, suspensions) — NOT an
        # extra one re-lifted from the injuries file.
        feed_missing = [w for w in state["warnings"]
                        if w.get("type") == "feed_missing"]
        # injuries IS present here (we wrote it), so the remaining real
        # feeds should all generate their own feed_missing.
        feeds_alerting = sorted(w["feed"] for w in feed_missing)
        self.assertEqual(feeds_alerting,
                         ["lineups", "referee", "stats_proxy",
                          "suspensions", "weather"])


class TestUnknownWarningTypeObservability(unittest.TestCase):
    """If a future fetcher emits a warning type that's neither in
    _PROPAGATE_WARNING_TYPES nor _BENIGN_DROPPED_WARNING_TYPES, the
    matchday consolidator must surface a one-time-per-(feed,type) stderr
    log line so the gap is visible at the next cron run instead of
    silently disappearing. The log is sampled — a thousand identical
    warnings produce ONE log line, not a flood."""

    def test_unknown_type_logs_to_stderr_once(self):
        """A fetcher emits `provider_quota_exhausted` (hypothetical
        future type). The consolidator must drop it from `state.warnings`
        but log a single stderr WARN line."""
        import io
        from unittest.mock import patch
        feeds = {"injuries_2026.json": {
            "teams": {},
            "warnings": [{"type": "provider_quota_exhausted",
                          "remaining": 0, "message": "quota gone"}],
        }}
        captured = io.StringIO()
        with _TempFeeds(feeds), patch("sys.stderr", captured):
            state = amd.build_adjustments_state()
        # The unknown type is NOT in state.warnings
        unknown = [w for w in state["warnings"]
                   if w.get("type") == "provider_quota_exhausted"]
        self.assertEqual(unknown, [])
        # But it IS in stderr
        log = captured.getvalue()
        self.assertIn("provider_quota_exhausted", log)
        self.assertIn("injuries", log)
        self.assertIn("WARN", log)

    def test_benign_dropped_types_do_not_log(self):
        """filter_non_wc is in _BENIGN_DROPPED_WARNING_TYPES — expected
        every cycle, must not log as unknown."""
        import io
        from unittest.mock import patch
        feeds = {"injuries_2026.json": {
            "teams": {},
            "warnings": [{"type": "filter_non_wc", "count": 3,
                          "message": "skipped 3 records"}],
        }}
        captured = io.StringIO()
        with _TempFeeds(feeds), patch("sys.stderr", captured):
            amd.build_adjustments_state()
        log = captured.getvalue()
        self.assertNotIn("filter_non_wc", log)

    def test_sample_cap_one_per_feed_type_pair(self):
        """100 identical unknown warnings produce ONE stderr log line,
        not 100 — prevents log flood under wedged-fetcher scenarios."""
        import io
        from unittest.mock import patch
        feeds = {"injuries_2026.json": {
            "teams": {},
            "warnings": [{"type": "novel_warning",
                          "i": i, "message": f"row {i}"}
                         for i in range(100)],
        }}
        captured = io.StringIO()
        with _TempFeeds(feeds), patch("sys.stderr", captured):
            amd.build_adjustments_state()
        log = captured.getvalue()
        # Exactly one log line for this (feed, type) pair
        self.assertEqual(log.count("novel_warning"), 1)


class TestMalformedUpstreamGuards(unittest.TestCase):
    """REGRESSION: a truncated or hand-edited upstream JSON must NOT
    crash build_adjustments_state. A matchday cron that crashes on a
    malformed warnings field loses every other layer (weather, lineups,
    stats_proxy) until the next run — outage rather than degraded
    output. Two failure modes pinned:

      1. `warnings` is present but not a list (a string, int, dict).
      2. `warnings` is a list but contains non-dict elements
         (strings, None) mixed with valid warnings.
    """

    def test_non_list_warnings_field_survives(self):
        """`warnings: "oops a string"` must not crash the consolidator."""
        feeds = {"injuries_2026.json": {"teams": {}, "warnings": "oops"}}
        with _TempFeeds(feeds):
            state = amd.build_adjustments_state()
        # Only the locally-generated feed_missing items (for absent
        # weather/lineups/stats_proxy) should appear.
        amb = [w for w in state["warnings"]
               if w.get("type") == "ambiguous_classification"]
        self.assertEqual(amb, [])

    def test_mixed_type_warnings_list_skips_bad_items(self):
        """A warnings list with mixed strings + dicts must drop the
        strings silently and propagate the valid dicts."""
        feeds = {"injuries_2026.json": {
            "teams": {},
            "warnings": [
                "stray string",
                None,
                42,
                {"type": "ambiguous_classification", "count": 1,
                 "cases": [{"team": "X", "input": "Y"}],
                 "message": "ambiguous test"},
                {"type": "http_error", "code": 500},
            ],
        }}
        with _TempFeeds(feeds):
            state = amd.build_adjustments_state()
        amb = [w for w in state["warnings"]
               if w.get("type") == "ambiguous_classification"]
        self.assertEqual(len(amb), 1)
        self.assertEqual(amb[0]["feed"], "injuries")
        err = [w for w in state["warnings"]
               if w.get("type") == "http_error"]
        self.assertEqual(len(err), 1)
        self.assertEqual(err[0]["feed"], "injuries")

    def test_missing_warnings_field_survives(self):
        """No `warnings` field at all — common on a fresh snapshot — is
        handled by the existing `or []` path. Pin it explicitly."""
        feeds = {"injuries_2026.json": {"teams": {}}}
        with _TempFeeds(feeds):
            state = amd.build_adjustments_state()
        self.assertNotIn("AttributeError",
                         repr(state))  # smoke check; if it raised we never got here


class TestRound5DegradationPerRecord(unittest.TestCase):
    """Round 5: a single bad per-record value (NaN, non-finite Elo, type
    error inside a record builder) must be skipped, logged to
    degradation_warnings, and NOT abort the rest of that subsystem's
    loop. Verified via monkeypatch injection — the inner record builder
    is patched to raise on a chosen team so other teams still produce
    adjustments."""

    def test_nan_xg_record_skipped_others_adjusted(self):
        """Simulate a NaN xG / stats record by patching float() at the
        per-record call site so the FIRST stats record raises
        ValueError, the second sails through."""
        feeds = {"match_stats_2026.json": {"matches": [
            {"match_id": 1, "status": "FT", "home": "Spain", "away": "X",
             "home_form_adjustment_elo": 6.0, "away_form_adjustment_elo": 0.0},
            {"match_id": 2, "status": "FT", "home": "Italy", "away": "Y",
             "home_form_adjustment_elo": 4.0, "away_form_adjustment_elo": 0.0},
        ]}}
        # Patch _load_stats_components to inject a ValueError on the Spain
        # record only — mimics what compute_xg_form_delta does on NaN xg.
        orig_loader = amd._load_stats_components

        def patched_loader(warnings_acc=None):
            warnings_acc = warnings_acc if warnings_acc is not None else []
            # Re-implement the per-team loop but force a raise on Spain.
            from _degrade import degrade_record  # type: ignore
            out = {}
            for team, raw in [("Spain", 6.0), ("Italy", 4.0)]:
                def _build(team=team, raw=raw):
                    if team == "Spain":
                        raise ValueError("xg must be finite")
                    return {
                        "type": "stats_proxy",
                        "match_id": 0,
                        "raw_elo": raw,
                        "capped_elo_per_match": raw,
                        "cap_per_match": amd.STATS_CAP_PER_MATCH,
                        "capped_elo": raw,
                        "cap_used": amd.STATS_CAP_TOURNAMENT_TOTAL,
                        "cap_reason": "per_match",
                        "downweighted_for_live_team_state": False,
                        "source": "api_football",
                    }
                rec = degrade_record(
                    "stats_proxy", f"team={team}", _build, warnings_acc)
                if rec is None:
                    continue
                out.setdefault((team, None), []).append(rec)
            return out

        with _TempFeeds(feeds), patch.object(
                amd, "_load_stats_components", patched_loader):
            state = amd.build_adjustments_state()
            # Italy still got its adjustment (patch must still be active)
            italy_elo = amd.get_team_elo_adjustment("Italy")
            spain_elo = amd.get_team_elo_adjustment("Spain")
        self.assertEqual(italy_elo, 4.0)
        # Spain was skipped
        self.assertEqual(spain_elo, 0.0)
        # Warning recorded
        deg = state["degradation_warnings"]
        spain_warns = [w for w in deg if "Spain" in w.get("record_id", "")]
        self.assertEqual(len(spain_warns), 1)
        self.assertEqual(spain_warns[0]["subsystem"], "stats_proxy")
        self.assertEqual(spain_warns[0]["scope"], "record")
        self.assertEqual(spain_warns[0]["exception_class"], "ValueError")
        self.assertIn("xg must be finite", spain_warns[0]["message"])
        self.assertIn("ts", spain_warns[0])
        # Pipeline still healthy — only one record skipped
        self.assertFalse(state["pipeline_unhealthy"])

    def test_nonfinite_elo_in_injury_record_skipped(self):
        """Simulate injury_adjustments raising ValueError on a non-finite
        elo for ONE team blob — other teams' injury totals must still
        flow through."""
        feeds = {"injuries_2026.json": {"teams": {
            "France":  {"total_elo_adjustment": -18.0, "players": []},
            "Senegal": {"total_elo_adjustment": -12.0, "players": []},
        }}}
        # We swap float() inside the orchestrator so the France record
        # raises. Easier: monkeypatch the loader to inject a raise on
        # the per-record block.
        orig_loader = amd._load_injury_components

        def patched_loader(now_iso, warnings_acc=None):
            warnings_acc = warnings_acc if warnings_acc is not None else []
            from _degrade import degrade_record  # type: ignore
            out = {}
            for team, total in [("France", -18.0), ("Senegal", -12.0)]:
                def _build(team=team, total=total):
                    if team == "France":
                        raise ValueError("non-finite elo")
                    return {
                        "type": "injury",
                        "subtype": "api_aggregate",
                        "raw_elo": total,
                        "capped_elo": total,
                        "cap_used": amd.INJURY_CAP_NORMAL,
                        "n_players": 0,
                        "source": "api_football",
                    }
                rec = degrade_record(
                    "injury", f"team={team} src=api", _build, warnings_acc)
                if rec is None:
                    continue
                out.setdefault((team, None), []).append(rec)
            return out

        with _TempFeeds(feeds), patch.object(
                amd, "_load_injury_components", patched_loader):
            state = amd.build_adjustments_state()
            senegal = amd.get_team_elo_adjustment("Senegal")
            france = amd.get_team_elo_adjustment("France")
        self.assertEqual(senegal, -12.0)
        self.assertEqual(france, 0.0)
        deg = state["degradation_warnings"]
        france_warns = [w for w in deg if "France" in w.get("record_id", "")]
        self.assertEqual(len(france_warns), 1)
        self.assertEqual(france_warns[0]["subsystem"], "injury")
        self.assertEqual(france_warns[0]["exception_class"], "ValueError")
        self.assertFalse(state["pipeline_unhealthy"])


class TestRound5DegradationPerSubsystem(unittest.TestCase):
    """A whole subsystem loader raising must NOT abort the others.
    Surfaced as a top-level `subsystem_degraded` warning plus a
    structured entry in `degradation_warnings`."""

    def test_one_subsystem_total_fail_others_continue(self):
        """Stats_proxy loader explodes (e.g., compute_form_delta raises
        before the per-record try/except can engage). Lineup data still
        feeds through normally."""
        feeds = {
            "lineups_2026.json": {"lineups": [{
                "match_id": 5, "home": "Brazil", "away": "Croatia",
                "home_team_adjustment_elo": -15.0,
                "away_team_adjustment_elo": 0.0,
                "home_adjustment_reason": "second-choice GK",
            }]},
            "match_stats_2026.json": {"matches": [{
                "match_id": 1, "status": "FT", "home": "Spain", "away": "X",
                "home_form_adjustment_elo": 8.0,
            }]},
        }

        def broken_stats(warnings_acc=None):
            raise ValueError("compute_form_delta tripped on bad row")

        with _TempFeeds(feeds), patch.object(
                amd, "_load_stats_components", broken_stats):
            state = amd.build_adjustments_state()
            brazil = amd.get_team_elo_adjustment("Brazil", match_id=5)
            spain = amd.get_team_elo_adjustment("Spain")
        # Lineup adjustment still applied
        self.assertEqual(brazil, -15.0)
        # Spain got no stats adjustment (subsystem degraded)
        self.assertEqual(spain, 0.0)
        # subsystem_degraded warning is surfaced at top level
        sd = [w for w in state["warnings"]
              if w.get("type") == "subsystem_degraded"]
        self.assertEqual(len(sd), 1)
        self.assertEqual(sd[0]["subsystem"], "stats_proxy")
        # And the structured degradation_warnings entry exists
        deg = [w for w in state["degradation_warnings"]
               if w.get("scope") == "subsystem"]
        self.assertEqual(len(deg), 1)
        self.assertEqual(deg[0]["subsystem"], "stats_proxy")
        self.assertEqual(deg[0]["exception_class"], "ValueError")
        # Pipeline still healthy — one subsystem down, others up
        self.assertFalse(state["pipeline_unhealthy"])

    def test_subsystem_degraded_warning_skipped_for_clean_loaders(self):
        """No loader raises → zero subsystem_degraded warnings.

        Note (Wave-2 S1): the freshness guard may LEGITIMATELY append
        `scope=freshness` warnings when the Phase 2/4/6 producer outputs
        aren't present in the temp dir. Those are not subsystem degradations —
        scope this assertion to `scope in (record, subsystem)`."""
        with _TempFeeds({}):
            state = amd.build_adjustments_state()
        sd = [w for w in state["warnings"]
              if w.get("type") == "subsystem_degraded"]
        self.assertEqual(sd, [])
        non_freshness = [w for w in state["degradation_warnings"]
                         if w.get("scope") != "freshness"]
        self.assertEqual(non_freshness, [])


class TestRound5CatastrophicFailure(unittest.TestCase):
    """All six subsystems failing OR snapshot write failing → exit 1,
    pipeline_unhealthy=True, plus a top-level `pipeline_unhealthy`
    warning. The state dict is still produced so downstream consumers
    can see the warning."""

    def test_all_subsystems_degraded_marks_pipeline_unhealthy(self):
        def boom(*args, **kwargs):
            raise ValueError("upstream wedged")

        patches = [
            patch.object(amd, "_load_injury_components", boom),
            patch.object(amd, "_load_weather_components", boom),
            patch.object(amd, "_load_lineup_components", boom),
            patch.object(amd, "_load_stats_components", boom),
            patch.object(amd, "_load_referee_components", boom),
            patch.object(amd, "_load_suspension_components", boom),
        ]
        with _TempFeeds({}):
            for p in patches:
                p.start()
            try:
                state = amd.build_adjustments_state()
            finally:
                for p in patches:
                    p.stop()
        self.assertTrue(state["pipeline_unhealthy"])
        unh = [w for w in state["warnings"]
               if w.get("type") == "pipeline_unhealthy"]
        self.assertEqual(len(unh), 1)
        # All six subsystems flagged as degraded
        sd = sorted(w["subsystem"] for w in state["warnings"]
                    if w.get("type") == "subsystem_degraded")
        self.assertEqual(sd, ["injury", "lineup", "referee",
                              "stats_proxy", "suspension", "weather"])

    def test_main_returns_nonzero_when_pipeline_unhealthy(self):
        def boom(*args, **kwargs):
            raise ValueError("upstream wedged")

        patches = [
            patch.object(amd, "_load_injury_components", boom),
            patch.object(amd, "_load_weather_components", boom),
            patch.object(amd, "_load_lineup_components", boom),
            patch.object(amd, "_load_stats_components", boom),
            patch.object(amd, "_load_referee_components", boom),
            patch.object(amd, "_load_suspension_components", boom),
        ]
        with _TempFeeds({}):
            for p in patches:
                p.start()
            try:
                # Use dry-run so we don't actually write
                with patch.object(sys, "argv",
                                  ["apply_matchday_adjustments.py", "--dry-run"]):
                    rc = amd.main()
            finally:
                for p in patches:
                    p.stop()
        self.assertEqual(rc, 1)

    def test_main_returns_zero_on_partial_degradation(self):
        """ONE subsystem failing must NOT exit non-zero — other
        subsystems still produced output, dashboard surfaces the
        degraded pill, tick is still useful."""
        def boom(warnings_acc=None):
            raise ValueError("just stats wedged")
        with _TempFeeds({}), patch.object(
                amd, "_load_stats_components", boom):
            with patch.object(sys, "argv",
                              ["apply_matchday_adjustments.py", "--dry-run"]):
                rc = amd.main()
        self.assertEqual(rc, 0)

    def test_snapshot_write_failure_marks_pipeline_unhealthy(self):
        """OSError from _atomic_write_json must escalate to
        pipeline_unhealthy but NOT crash — the function still returns
        the state dict so callers (run_live_update.py) keep going."""
        def boom_write(path, payload):
            raise OSError(28, "No space left on device")
        with _TempFeeds({}), patch.object(
                amd, "_atomic_write_json", boom_write):
            state = amd.write_state_and_log(dry_run=False)
        self.assertTrue(state["pipeline_unhealthy"])
        unh = [w for w in state["warnings"]
               if w.get("type") == "pipeline_unhealthy"]
        self.assertEqual(len(unh), 1)
        self.assertIn("dashboard JSON", unh[0]["message"])


class TestRound5DegradationSchema(unittest.TestCase):
    """Pin the shape of degradation_warnings + audit-log enrichment so
    downstream surfacing (dashboard pill, audit replay) doesn't break."""

    def test_degradation_warnings_field_present_on_clean_tick(self):
        with _TempFeeds({}):
            state = amd.build_adjustments_state()
        self.assertIn("degradation_warnings", state)
        self.assertIsInstance(state["degradation_warnings"], list)

    def test_degradation_warning_record_shape(self):
        def boom(warnings_acc=None):
            raise ValueError("synthetic boom")
        with _TempFeeds({}), patch.object(
                amd, "_load_stats_components", boom):
            state = amd.build_adjustments_state()
        deg = state["degradation_warnings"]
        self.assertGreaterEqual(len(deg), 1)
        # Filter to the synthetic stats_proxy subsystem failure — freshness
        # warnings from missing producer outputs (Wave-2 S1) share the
        # degradation_warnings channel but use scope='freshness'.
        subsystem_entries = [
            w for w in deg
            if w.get("scope") == "subsystem"
            and w.get("subsystem") == "stats_proxy"
        ]
        self.assertEqual(len(subsystem_entries), 1)
        entry = subsystem_entries[0]
        for k in ("subsystem", "scope", "record_id",
                  "exception_class", "message", "ts"):
            self.assertIn(k, entry)
        self.assertEqual(entry["subsystem"], "stats_proxy")
        self.assertEqual(entry["scope"], "subsystem")

    def test_audit_log_includes_degradation_warnings(self):
        def boom(warnings_acc=None):
            raise ValueError("synthetic")
        with _TempFeeds({}) as tmp, patch.object(
                amd, "_load_stats_components", boom):
            log_path = tmp / "matchday_intelligence_log.jsonl"
            amd.write_state_and_log()
            line = log_path.read_text().strip().splitlines()[-1]
            rec = json.loads(line)
        self.assertIn("degradation_warnings", rec)
        self.assertGreaterEqual(len(rec["degradation_warnings"]), 1)


class TestFreshnessGuard(unittest.TestCase):
    """Wave-2 S1: producers for referee / suspension / player_stats are
    invoked by matchday-intel-slow.yml. Origin/main has never seen these
    files until the new workflow steps land. The orchestrator must:

      1. Emit a `subsystem_stale` warning when the file is MISSING.
      2. Emit one when the file is OLDER than results_2026.json by more
         than STALENESS_MAX_AGE_HOURS (6h = 2 slow-cron ticks).
      3. Stay silent when all three files are fresh.
      4. Never raise — the subsystem still degrades to neutral via the
         existing _read_json(default={}) path.

    Threshold rationale: the slow workflow runs every 3h, so anything
    older than 2 ticks is a missed-then-missed pattern (one transient
    failure is recoverable; two consecutive means an upstream gap).
    """

    def _make_results(self, tmp_path: Path, mtime_offset_hours: float = 0.0):
        """Write a minimal results_2026.json and set its mtime to
        `now - mtime_offset_hours` for the staleness comparison.
        Returns the file path."""
        import os
        path = tmp_path / "results_2026.json"
        path.write_text(json.dumps({"completed_matches": []}))
        if mtime_offset_hours:
            mt = path.stat().st_mtime - mtime_offset_hours * 3600
            os.utime(path, (mt, mt))
        return path

    def _set_mtime_hours_ago(self, path: Path, hours_ago: float) -> None:
        import os
        ref = path.stat().st_mtime - hours_ago * 3600
        os.utime(path, (ref, ref))

    def test_missing_referee_emits_subsystem_stale(self):
        """No referee_2026.json on disk → `subsystem_stale` warning with
        subsystem='referee' and exception_class='Stale'. Existing
        `feed_missing` warning is also emitted (independent channel)."""
        with _TempFeeds({}) as tmp:
            self._make_results(tmp)
            state = amd.build_adjustments_state()
        stale = [w for w in state["degradation_warnings"]
                 if w.get("subsystem") == "referee"
                 and w.get("scope") == "freshness"]
        self.assertEqual(len(stale), 1, msg=f"got: {state['degradation_warnings']}")
        self.assertEqual(stale[0]["exception_class"], "Stale")
        self.assertEqual(stale[0]["scope"], "freshness")
        self.assertIn("referee_2026.json", stale[0]["message"])
        self.assertIn("ts", stale[0])

    def test_missing_suspensions_emits_subsystem_stale(self):
        with _TempFeeds({}) as tmp:
            self._make_results(tmp)
            state = amd.build_adjustments_state()
        stale = [w for w in state["degradation_warnings"]
                 if w.get("subsystem") == "suspension"
                 and w.get("scope") == "freshness"]
        self.assertEqual(len(stale), 1)
        self.assertIn("suspensions_2026.json", stale[0]["message"])

    def test_missing_player_stats_emits_subsystem_stale(self):
        with _TempFeeds({}) as tmp:
            self._make_results(tmp)
            state = amd.build_adjustments_state()
        stale = [w for w in state["degradation_warnings"]
                 if w.get("subsystem") == "player_stats"
                 and w.get("scope") == "freshness"]
        self.assertEqual(len(stale), 1)
        self.assertIn("player_stats_2026.json", stale[0]["message"])

    def test_suspensions_seven_hours_older_than_results_emits_stale(self):
        """suspensions_2026.json exists but is 7h older than results
        (> 6h threshold) → `subsystem_stale` with the delta surfaced."""
        feeds = {"suspensions_2026.json": {"suspensions": []}}
        with _TempFeeds(feeds) as tmp:
            results = self._make_results(tmp)
            # Make suspensions file 7h older than results.
            self._set_mtime_hours_ago(tmp / "suspensions_2026.json", 7.0)
            # Ensure results mtime is "now" — set explicitly
            import os
            now = results.stat().st_mtime
            os.utime(results, (now, now))
            state = amd.build_adjustments_state()
        stale = [w for w in state["degradation_warnings"]
                 if w.get("subsystem") == "suspension"
                 and w.get("scope") == "freshness"]
        self.assertEqual(len(stale), 1, msg=f"got: {state['degradation_warnings']}")
        # Message should mention the age (>6h).
        self.assertRegex(stale[0]["message"], r"[67]\.[0-9]h older")
        self.assertIn("threshold 6.0h", stale[0]["message"])

    def test_all_three_fresh_emits_no_subsystem_stale(self):
        """When all three files exist and are within 6h of results, no
        `subsystem_stale` warning is emitted by the freshness guard."""
        feeds = {
            "referee_2026.json": {"referee": []},
            "suspensions_2026.json": {"suspensions": []},
            "player_stats_2026.json": {"teams": {}},
        }
        with _TempFeeds(feeds) as tmp:
            self._make_results(tmp)
            state = amd.build_adjustments_state()
        stale = [w for w in state["degradation_warnings"]
                 if w.get("scope") == "freshness"]
        self.assertEqual(
            stale, [],
            msg=f"unexpected freshness warnings: {stale}",
        )

    def test_freshness_check_does_not_raise(self):
        """The guard must NEVER raise — failure mode is loud-degrade-warn,
        not crash-the-tick. Even with all 3 inputs missing AND no
        reference file, build_adjustments_state must return a state."""
        with _TempFeeds({}):
            # No results_2026.json either — _check_freshness should treat
            # missing reference as "fresh" (can't compute delta) and just
            # surface the missing-file warning for each input.
            state = amd.build_adjustments_state()
        self.assertIsInstance(state, dict)
        self.assertIn("degradation_warnings", state)

    def test_freshness_threshold_constant_is_six_hours(self):
        """Pin the threshold so a future refactor doesn't silently widen
        it (e.g. to 24h) and start hiding real staleness. 6h = 2 slow-cron
        ticks (cron schedule = every 3h). Bumping it MUST be a deliberate
        documented decision, not a drive-by refactor."""
        self.assertEqual(amd.STALENESS_MAX_AGE_HOURS, 6.0)


# R8 O2: matchday_intelligence.json writer rejects NaN / Infinity at the
# producer side. Pre-R8, CPython json round-tripped Infinity silently
# (json.loads("Infinity") → inf), so an upstream numerical bug could write
# Inf and have it propagate into 03_simulate's base_intel_plus_state →
# predict_lambdas → nbinom.pmf → NaN p_champion. Fail-loud on the write
# now; clean runs unaffected.
class TestR8O2AllowNanFalse(unittest.TestCase):
    def test_atomic_write_rejects_infinity(self):
        """A producer that accidentally emits Infinity must raise at the
        write boundary, not silently round-trip through json and corrupt
        downstream model lookups."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "matchday_intelligence.json"
            payload = {
                "generated_at": "2026-06-18T00:00:00+00:00",
                "schema_version": 1,
                "active_adjustments": [
                    {"team": "MEX", "total_elo_adjustment": float("inf")},
                ],
            }
            with self.assertRaises(ValueError):
                amd._atomic_write_json(target, payload)
            # And no .tmp file should be lingering committed in target.
            self.assertFalse(target.exists(),
                             "no atomically-replaced file should exist on rejected write")

    def test_atomic_write_rejects_nan(self):
        """Same fail-loud semantics for NaN — the silent-NaN-in,
        silent-NaN-out pipeline is exactly what R8 O2 closes."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "matchday_intelligence.json"
            payload = {"summary": {"net": float("nan")}}
            with self.assertRaises(ValueError):
                amd._atomic_write_json(target, payload)

    def test_atomic_write_accepts_clean_finite_floats(self):
        """Negative case: a clean payload round-trips. R8 O2 changes
        nothing for any tick that doesn't carry NaN/Inf."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "matchday_intelligence.json"
            payload = {"summary": {"net": 1.5, "ratio": 0.0, "min": -3.14}}
            amd._atomic_write_json(target, payload)
            self.assertTrue(target.exists())
            self.assertEqual(json.loads(target.read_text())["summary"]["net"], 1.5)


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
