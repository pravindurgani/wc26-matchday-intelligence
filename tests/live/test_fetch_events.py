"""Phase B3 — /fixtures/events integration tests.

Covers:
  - normalize_event() shape + subtype slugs for goal/card/subst/var
  - enrich_matches_with_events() cache reuse vs fresh fetch
  - failure path: per-fixture error attaches events: [] + warning,
    locked score is preserved.

Synthetic events sample lives at
tests/live/provider_samples/apifootball_events_sample.json.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

import fetch_results  # noqa: E402
from fetch_results import (  # noqa: E402
    enrich_matches_with_events,
    fetch_apifootball_events_for_fixture,
    normalize_event,
)

SAMPLES = ROOT / "tests" / "live" / "provider_samples"


class TestNormalizeEvent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sample = SAMPLES / "apifootball_events_sample.json"
        cls.events = json.loads(sample.read_text())["response"]

    def test_normal_goal_with_assist(self):
        ev = normalize_event(self.events[0])
        self.assertEqual(ev["type"], "goal")
        self.assertEqual(ev["subtype"], "normal_goal")
        self.assertEqual(ev["team"], "Mexico")
        self.assertEqual(ev["player"], "R. Jiménez")
        self.assertEqual(ev["assist"], "H. Lozano")
        self.assertEqual(ev["minute"], 12)
        self.assertIsNone(ev["extra_minute"])

    def test_yellow_card(self):
        ev = normalize_event(self.events[1])
        self.assertEqual(ev["type"], "card")
        self.assertEqual(ev["subtype"], "yellow_card")
        self.assertEqual(ev["player"], "T. Mokoena")
        self.assertEqual(ev["comments"], "Foul")

    def test_penalty_with_extra_minute(self):
        ev = normalize_event(self.events[2])
        self.assertEqual(ev["type"], "goal")
        self.assertEqual(ev["subtype"], "penalty")
        self.assertEqual(ev["minute"], 45)
        self.assertEqual(ev["extra_minute"], 2)

    def test_second_yellow_card_recognised(self):
        ev = normalize_event(self.events[3])
        self.assertEqual(ev["type"], "card")
        # subtype is the verbatim slug — suspension tracker pattern-matches
        # on prefix, so "second_yellow_card" must start with "second".
        self.assertTrue(ev["subtype"].startswith("second"))

    def test_substitution_carries_player_in_and_out(self):
        ev = normalize_event(self.events[4])
        self.assertEqual(ev["type"], "subst")
        # API-Football quirk: `player` is the one going OFF, `assist` is
        # the one coming ON. We surface both verbatim.
        self.assertEqual(ev["player"], "R. Jiménez")
        self.assertEqual(ev["assist"], "S. Giménez")

    def test_red_card(self):
        ev = normalize_event(self.events[5])
        self.assertEqual(ev["type"], "card")
        self.assertEqual(ev["subtype"], "red_card")

    def test_own_goal(self):
        ev = normalize_event(self.events[6])
        self.assertEqual(ev["type"], "goal")
        self.assertEqual(ev["subtype"], "own_goal")

    def test_var_event(self):
        ev = normalize_event(self.events[7])
        self.assertEqual(ev["type"], "var")
        self.assertEqual(ev["subtype"], "goal_cancelled")
        self.assertIsNone(ev["player"])

    def test_malformed_returns_none(self):
        self.assertIsNone(normalize_event(None))
        self.assertIsNone(normalize_event("not a dict"))
        self.assertIsNone(normalize_event(42))

    def test_missing_fields_degrade_gracefully(self):
        ev = normalize_event({})
        self.assertEqual(ev["type"], "other")
        self.assertIsNone(ev["minute"])
        self.assertIsNone(ev["team"])


class TestEnrichWithEvents(unittest.TestCase):
    """enrich_matches_with_events: cache reuse, fresh fetch, failure path."""

    def _matches(self):
        return [
            {"m": 1, "provider_fixture_id": "1489369",
             "home": "Mexico", "away": "South Africa", "status": "FT"},
            {"m": 2, "provider_fixture_id": "1538999",
             "home": "South Korea", "away": "Czechia", "status": "FT"},
            {"m": 3, "provider_fixture_id": "1539000",
             "home": "Canada", "away": "Bosnia and Herzegovina",
             "status": "FT"},
        ]

    def test_cache_reused_no_fetch(self):
        # All three matches in cache → zero HTTP calls.
        matches = self._matches()
        cache = {1: [{"type": "goal"}], 2: [], 3: [{"type": "card"}]}
        with mock.patch.object(fetch_results, "fetch_apifootball_events_for_fixture") as m_fx:
            out, warns = enrich_matches_with_events(
                matches, api_key="dummy",
                existing_events_by_m=cache, sleep_between=0.0,
            )
        m_fx.assert_not_called()
        self.assertEqual(out[0]["events"], cache[1])
        self.assertEqual(out[1]["events"], cache[2])
        self.assertEqual(out[2]["events"], cache[3])
        self.assertEqual(warns, [])

    def test_fresh_fetch_for_uncached_matches(self):
        matches = self._matches()
        cache = {1: [{"type": "goal", "subtype": "normal_goal"}]}  # only m=1 cached
        with mock.patch.object(
            fetch_results, "fetch_apifootball_events_for_fixture",
            side_effect=lambda key, fx: ([{"type": "card", "fixture": fx}], None),
        ) as m_fx:
            out, warns = enrich_matches_with_events(
                matches, api_key="dummy",
                existing_events_by_m=cache, sleep_between=0.0,
            )
        # m=1 cache hit, m=2 + m=3 fresh
        self.assertEqual(m_fx.call_count, 2)
        self.assertEqual(out[0]["events"], cache[1])
        self.assertEqual(out[1]["events"][0]["fixture"], "1538999")
        self.assertEqual(out[2]["events"][0]["fixture"], "1539000")
        self.assertEqual(warns, [])

    def test_per_fixture_failure_attaches_warning_keeps_score(self):
        matches = self._matches()
        # m=2 fails, m=1 + m=3 succeed.
        def _side(key, fx):
            if fx == "1538999":
                return [], {"type": "events_http_error", "fixture_id": fx,
                            "code": 429, "body": "rate limit"}
            return [{"ok": True}], None
        with mock.patch.object(
            fetch_results, "fetch_apifootball_events_for_fixture",
            side_effect=_side,
        ):
            out, warns = enrich_matches_with_events(
                matches, api_key="dummy",
                existing_events_by_m=None, sleep_between=0.0,
            )
        # All three locked-score records survive.
        self.assertEqual(len(out), 3)
        self.assertEqual(out[1]["events"], [])  # failed → empty events
        self.assertEqual(out[1]["home_score"] if "home_score" in out[1] else None,
                         None)  # didn't fabricate
        self.assertEqual(len(warns), 1)
        self.assertEqual(warns[0]["type"], "events_http_error")
        self.assertEqual(warns[0]["m"], 2)

    def test_no_api_key_warns_but_threads_cache(self):
        matches = self._matches()
        cache = {1: [{"type": "card"}]}
        with mock.patch.object(fetch_results, "fetch_apifootball_events_for_fixture") as m_fx:
            out, warns = enrich_matches_with_events(
                matches, api_key=None,
                existing_events_by_m=cache, sleep_between=0.0,
            )
        m_fx.assert_not_called()
        self.assertEqual(out[0]["events"], cache[1])
        self.assertTrue(any(w["type"] == "events_missing_key" for w in warns))

    def test_skips_non_locked_status(self):
        matches = [
            {"m": 10, "provider_fixture_id": "999", "status": "LIVE"},
            {"m": 11, "provider_fixture_id": "1000", "status": "SCHEDULED"},
            {"m": 12, "provider_fixture_id": "1001", "status": "FT"},
        ]
        with mock.patch.object(
            fetch_results, "fetch_apifootball_events_for_fixture",
            return_value=([{"type": "goal"}], None),
        ) as m_fx:
            out, _ = enrich_matches_with_events(
                matches, api_key="dummy",
                existing_events_by_m={}, sleep_between=0.0,
            )
        # Only the FT fixture fetched.
        self.assertEqual(m_fx.call_count, 1)
        m_fx.assert_called_with("dummy", "1001")
        self.assertEqual(out[2]["events"], [{"type": "goal"}])
        # Non-locked statuses get no events attached.
        self.assertNotIn("events", out[0])
        self.assertNotIn("events", out[1])

    def test_missing_provider_fixture_id_warns(self):
        matches = [
            {"m": 99, "provider_fixture_id": "", "status": "FT"},
        ]
        with mock.patch.object(
            fetch_results, "fetch_apifootball_events_for_fixture",
        ) as m_fx:
            out, warns = enrich_matches_with_events(
                matches, api_key="dummy",
                existing_events_by_m={}, sleep_between=0.0,
            )
        m_fx.assert_not_called()
        self.assertEqual(out[0]["events"], [])
        self.assertTrue(any(w["type"] == "events_no_fixture_id" for w in warns))


class TestFetchApifootballEventsForFixture(unittest.TestCase):
    """HTTP path of fetch_apifootball_events_for_fixture — mocked transport."""

    def test_success_path_normalises(self):
        payload = json.loads(
            (SAMPLES / "apifootball_events_sample.json").read_text()
        )
        with mock.patch.object(
            fetch_results, "http_get_json", return_value=payload,
        ):
            events, warn = fetch_apifootball_events_for_fixture(
                "dummy", "1489369",
            )
        self.assertIsNone(warn)
        self.assertEqual(len(events), 8)
        self.assertEqual(events[0]["type"], "goal")
        self.assertEqual(events[5]["subtype"], "red_card")

    def test_api_errors_returns_warning(self):
        with mock.patch.object(
            fetch_results, "http_get_json",
            return_value={"errors": {"plan": "Pro tier required"}, "response": []},
        ):
            events, warn = fetch_apifootball_events_for_fixture(
                "dummy", "1489369",
            )
        self.assertEqual(events, [])
        self.assertIsNotNone(warn)
        self.assertEqual(warn["type"], "events_api_error")

    def test_transport_exception_returns_warning(self):
        with mock.patch.object(
            fetch_results, "http_get_json",
            side_effect=RuntimeError("boom"),
        ):
            events, warn = fetch_apifootball_events_for_fixture(
                "dummy", "1489369",
            )
        self.assertEqual(events, [])
        self.assertIsNotNone(warn)
        self.assertEqual(warn["type"], "events_fetch_error")
        self.assertIn("boom", warn["message"])


if __name__ == "__main__":
    unittest.main()
