"""Finding S4 — schema-watchdog wiring across the remaining 5 fetch_* modules.

Pins:
  - assert_shape(payload, <baseline>) is invoked in each of the 5 production
    fetch_* sites after the HTTP call.
  - Each of the 5 synthetic baselines round-trips against the payload that
    was used to capture it (hash matches re-computed hash of the same shape).
  - Soft mode: a synthetic SHAPE DRIFT logs a WARNING but the fetch returns
    cleanly. The tick MUST NOT crash on drift.

No real network calls: `_http_get_json` / `http_get_json` is mocked per module.

Counterpart of tests/live/test_fetch_results_schema.py (which already covers
/fixtures and /fixtures/events). Together these two files pin all 7 wired
sites against soft-mode regressions.
"""
from __future__ import annotations

import copy
import json
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "live"))

from scripts.live._schema_watchdog import (  # noqa: E402
    compute_shape_hash,
    load_baseline,
)

import fetch_injuries  # noqa: E402
import fetch_lineups  # noqa: E402
import fetch_match_stats  # noqa: E402
import fetch_player_stats  # noqa: E402

BASELINES = ROOT / "data" / "live" / "_provider_schemas"


# ---------------------------------------------------------------------------
# Representative payloads — same shapes used to capture the 5 baselines.
# Re-defined here (not imported from a script) so the round-trip test is
# self-contained: any future tweak to the baseline-generation script can't
# silently drift the contract.
# ---------------------------------------------------------------------------

PAYLOAD_INJURIES = {
    "_comment": "synthetic",
    "get": "injuries",
    "parameters": {"league": "1", "season": "2026"},
    "errors": [],
    "results": 1,
    "paging": {"current": 1, "total": 1},
    "response": [
        {
            "player": {"id": 1001, "name": "K. Mbappé",
                       "type": "Missing Fixture", "reason": "Injury - knee"},
            "team": {"id": 2, "name": "France",
                     "logo": "https://media.api-sports.io/football/teams/2.svg"},
            "fixture": {"id": 1489369, "timezone": "UTC",
                        "date": "2026-06-12T20:00:00+00:00",
                        "timestamp": 1781200800},
            "league": {"id": 1, "season": 2026, "name": "World Cup",
                       "logo": "https://x.svg", "country": "World",
                       "flag": None, "round": "Group Stage"},
            "type": "Missing Fixture",
            "reason": "Injury",
        }
    ],
}

PAYLOAD_LINEUPS = {
    "_comment": "synthetic",
    "get": "fixtures/lineups",
    "parameters": {"fixture": "1489369"},
    "errors": [],
    "results": 2,
    "paging": {"current": 1, "total": 1},
    "response": [
        {
            "team": {
                "id": 30, "name": "Mexico",
                "logo": "https://media.api-sports.io/football/teams/30.svg",
                "colors": {
                    "player": {"primary": "006847", "number": "ffffff",
                               "border": "006847"},
                    "goalkeeper": {"primary": "ffd700", "number": "000000",
                                   "border": "ffd700"},
                },
            },
            "formation": "4-3-3",
            "startXI": [
                {"player": {"id": 1, "name": "G. Ochoa", "number": 1,
                            "pos": "G", "grid": "1:1"}},
            ],
            "substitutes": [
                {"player": {"id": 12, "name": "A. Talavera", "number": 12,
                            "pos": "G", "grid": None}},
            ],
            "coach": {"id": 99, "name": "Jaime Lozano",
                      "photo": "https://media.api-sports.io/football/coachs/99.png"},
        }
    ],
}

PAYLOAD_FIXTURES_STATS = {
    "_comment": "synthetic",
    "get": "fixtures/statistics",
    "parameters": {"fixture": "1489369"},
    "errors": [],
    "results": 2,
    "paging": {"current": 1, "total": 1},
    "response": [
        {
            "team": {"id": 30, "name": "Mexico",
                     "logo": "https://media.api-sports.io/football/teams/30.svg"},
            "statistics": [
                {"type": "Shots on Goal", "value": 6},
            ],
        }
    ],
}

PAYLOAD_TEAMS = {
    "_comment": "synthetic",
    "get": "teams",
    "parameters": {"league": "1", "season": "2026"},
    "errors": [],
    "results": 1,
    "paging": {"current": 1, "total": 1},
    "response": [
        {
            "team": {
                "id": 30, "name": "Mexico", "code": "MEX", "country": "Mexico",
                "founded": 1927, "national": True,
                "logo": "https://media.api-sports.io/football/teams/30.svg",
            },
            "venue": {
                "id": 100, "name": "Estadio Azteca",
                "address": "Calz. de Tlalpan 3465",
                "city": "Mexico City", "capacity": 87000,
                "surface": "grass",
                "image": "https://media.api-sports.io/football/venues/100.png",
            },
        }
    ],
}

PAYLOAD_PLAYERS = {
    "_comment": "synthetic",
    "get": "players",
    "parameters": {"team": "30", "season": "2026", "page": "1"},
    "errors": [],
    "results": 1,
    "paging": {"current": 1, "total": 1},
    "response": [
        {
            "player": {
                "id": 1001, "name": "R. Jiménez",
                "firstname": "Raúl", "lastname": "Jiménez",
                "age": 33,
                "birth": {"date": "1991-05-05",
                          "place": "Tepeji del Río",
                          "country": "Mexico"},
                "nationality": "Mexico",
                "height": "190 cm", "weight": "76 kg", "injured": False,
                "photo": "https://media.api-sports.io/football/players/1001.png",
            },
            "statistics": [
                {
                    "team": {"id": 30, "name": "Mexico",
                             "logo": "https://media.api-sports.io/football/teams/30.svg"},
                    "league": {"id": 1, "name": "World Cup", "country": "World",
                               "logo": "https://x.svg", "flag": None, "season": 2026},
                    "games": {"appearences": 13, "lineups": 12,
                              "minutes": 1100, "number": 9,
                              "position": "Attacker", "rating": "7.4",
                              "captain": False},
                    "substitutes": {"in": 1, "out": 8, "bench": 1},
                    "shots": {"total": 40, "on": 22},
                    "goals": {"total": 9, "conceded": 0, "assists": 4,
                              "saves": None},
                    "passes": {"total": 320, "key": 24, "accuracy": 80},
                    "tackles": {"total": 5, "blocks": None,
                                "interceptions": 4},
                    "duels": {"total": 180, "won": 95},
                    "dribbles": {"attempts": 40, "success": 18, "past": None},
                    "fouls": {"drawn": 30, "committed": 12},
                    "cards": {"yellow": 1, "yellowred": 0, "red": 0},
                    "penalty": {"won": 1, "commited": 0, "scored": 1,
                                "missed": 0, "saved": None},
                },
            ],
        }
    ],
}


# ---------------------------------------------------------------------------
# 1. Round-trip — each baseline hash matches the re-hashed source payload.
# ---------------------------------------------------------------------------

class TestBaselinesRoundTrip(unittest.TestCase):
    """The 5 captured baselines must round-trip against the payload used to
    generate them. If any of these fail, the baseline was either mis-written
    or its source payload was edited without re-capturing."""

    cases = [
        ("apifootball_injuries.shape.json", PAYLOAD_INJURIES),
        ("apifootball_fixtures_lineups.shape.json", PAYLOAD_LINEUPS),
        ("apifootball_fixtures_statistics.shape.json", PAYLOAD_FIXTURES_STATS),
        ("apifootball_teams.shape.json", PAYLOAD_TEAMS),
        ("apifootball_players.shape.json", PAYLOAD_PLAYERS),
    ]

    def test_each_baseline_matches_its_synthetic_fixture(self):
        for fname, payload in self.cases:
            with self.subTest(baseline=fname):
                bp = BASELINES / fname
                self.assertTrue(bp.exists(), f"missing baseline: {fname}")
                baseline = load_baseline(bp)
                self.assertEqual(
                    compute_shape_hash(payload), baseline["hash"],
                    f"baseline {fname} no longer matches its source payload — "
                    f"re-capture via _schema_watchdog.write_baseline",
                )


# ---------------------------------------------------------------------------
# 2. fetch_injuries — assert_shape is called with the /injuries baseline.
# ---------------------------------------------------------------------------

class TestFetchInjuriesWiring(unittest.TestCase):
    def test_assert_shape_called_with_injuries_baseline(self):
        with mock.patch.object(fetch_injuries, "_http_get_json",
                               return_value=PAYLOAD_INJURIES):
            with mock.patch.object(fetch_injuries, "assert_shape") as spy:
                records, warnings_ = fetch_injuries.fetch_apifootball_injuries(
                    "x" * 32,
                )
        self.assertTrue(spy.called, "assert_shape was never invoked")
        # Inspect every call: at least one must reference the /injuries baseline.
        called_with_baseline = any(
            Path(call.args[1]).name == "apifootball_injuries.shape.json"
            or "apifootball_injuries.shape.json" in str(call)
            for call in spy.call_args_list
        )
        self.assertTrue(
            called_with_baseline,
            f"assert_shape must be called with the /injuries baseline; "
            f"got calls: {spy.call_args_list}",
        )
        # The fetch still returns a list of records (soft mode, no crash).
        self.assertIsInstance(records, list)

    def test_drifted_payload_does_not_crash_fetch(self):
        drifted = copy.deepcopy(PAYLOAD_INJURIES)
        # Rename a top-level key inside response[0] to force shape drift.
        drifted["response"][0]["athlete"] = drifted["response"][0].pop("player")
        with mock.patch.object(fetch_injuries, "_http_get_json",
                               return_value=drifted):
            with self.assertLogs("schema_watchdog", level="WARNING") as cm:
                records, warnings_ = fetch_injuries.fetch_apifootball_injuries(
                    "x" * 32,
                )
        joined = "\n".join(cm.output)
        self.assertIn("SHAPE DRIFT", joined)
        self.assertIn("apifootball_injuries.shape.json", joined)
        # Fetch returned cleanly (soft mode).
        self.assertIsInstance(records, list)


# ---------------------------------------------------------------------------
# 3. fetch_lineups — assert_shape called with /fixtures/lineups baseline.
# ---------------------------------------------------------------------------

class TestFetchLineupsWiring(unittest.TestCase):
    def test_assert_shape_called_with_lineups_baseline(self):
        with mock.patch.object(fetch_lineups, "_http_get_json",
                               return_value=PAYLOAD_LINEUPS):
            with mock.patch.object(fetch_lineups, "assert_shape") as spy:
                sides = fetch_lineups.fetch_one_fixture("x" * 32, "1489369")
        self.assertTrue(spy.called)
        called_with_baseline = any(
            "apifootball_fixtures_lineups.shape.json" in str(call)
            for call in spy.call_args_list
        )
        self.assertTrue(
            called_with_baseline,
            f"assert_shape must reference the /fixtures/lineups baseline; "
            f"got: {spy.call_args_list}",
        )
        self.assertIsInstance(sides, list)

    def test_drifted_payload_does_not_crash_fetch(self):
        drifted = copy.deepcopy(PAYLOAD_LINEUPS)
        drifted["response"][0]["club"] = drifted["response"][0].pop("team")
        with mock.patch.object(fetch_lineups, "_http_get_json",
                               return_value=drifted):
            with self.assertLogs("schema_watchdog", level="WARNING") as cm:
                sides = fetch_lineups.fetch_one_fixture("x" * 32, "1489369")
        joined = "\n".join(cm.output)
        self.assertIn("SHAPE DRIFT", joined)
        self.assertIn("apifootball_fixtures_lineups.shape.json", joined)
        self.assertIsInstance(sides, list)


# ---------------------------------------------------------------------------
# 4. fetch_match_stats — assert_shape called with /fixtures/statistics baseline.
# ---------------------------------------------------------------------------

class TestFetchMatchStatsWiring(unittest.TestCase):
    def test_assert_shape_called_with_stats_baseline(self):
        with mock.patch.object(fetch_match_stats, "_http_get_json",
                               return_value=PAYLOAD_FIXTURES_STATS):
            with mock.patch.object(fetch_match_stats, "assert_shape") as spy:
                sides = fetch_match_stats.fetch_one_fixture(
                    "x" * 32, "1489369",
                )
        self.assertTrue(spy.called)
        called_with_baseline = any(
            "apifootball_fixtures_statistics.shape.json" in str(call)
            for call in spy.call_args_list
        )
        self.assertTrue(
            called_with_baseline,
            f"assert_shape must reference the /fixtures/statistics baseline; "
            f"got: {spy.call_args_list}",
        )
        self.assertIsInstance(sides, list)

    def test_drifted_payload_does_not_crash_fetch(self):
        drifted = copy.deepcopy(PAYLOAD_FIXTURES_STATS)
        drifted["response"][0]["stats"] = drifted["response"][0].pop("statistics")
        with mock.patch.object(fetch_match_stats, "_http_get_json",
                               return_value=drifted):
            with self.assertLogs("schema_watchdog", level="WARNING") as cm:
                sides = fetch_match_stats.fetch_one_fixture(
                    "x" * 32, "1489369",
                )
        joined = "\n".join(cm.output)
        self.assertIn("SHAPE DRIFT", joined)
        self.assertIn("apifootball_fixtures_statistics.shape.json", joined)
        self.assertIsInstance(sides, list)


# ---------------------------------------------------------------------------
# 5. fetch_player_stats — TWO sites: /teams + /players.
# ---------------------------------------------------------------------------

class TestFetchPlayerStatsTeamsWiring(unittest.TestCase):
    def test_assert_shape_called_with_teams_baseline(self):
        with mock.patch.object(fetch_player_stats, "_http_get_json",
                               return_value=PAYLOAD_TEAMS):
            with mock.patch.object(fetch_player_stats, "assert_shape") as spy:
                team_ids, warns = fetch_player_stats.fetch_team_ids(
                    "x" * 32, "1", "2026",
                )
        self.assertTrue(spy.called)
        called_with_baseline = any(
            "apifootball_teams.shape.json" in str(call)
            for call in spy.call_args_list
        )
        self.assertTrue(
            called_with_baseline,
            f"assert_shape must reference the /teams baseline; "
            f"got: {spy.call_args_list}",
        )
        self.assertIsInstance(team_ids, dict)

    def test_drifted_payload_does_not_crash_fetch(self):
        drifted = copy.deepcopy(PAYLOAD_TEAMS)
        drifted["response"][0]["club"] = drifted["response"][0].pop("team")
        with mock.patch.object(fetch_player_stats, "_http_get_json",
                               return_value=drifted):
            with self.assertLogs("schema_watchdog", level="WARNING") as cm:
                team_ids, warns = fetch_player_stats.fetch_team_ids(
                    "x" * 32, "1", "2026",
                )
        joined = "\n".join(cm.output)
        self.assertIn("SHAPE DRIFT", joined)
        self.assertIn("apifootball_teams.shape.json", joined)
        # Even though shape drifted, fetch returned (soft mode).
        self.assertIsInstance(team_ids, dict)


class TestFetchPlayerStatsPlayersWiring(unittest.TestCase):
    def test_assert_shape_called_with_players_baseline(self):
        # Cap paging at 1 so the loop exits after one page.
        page1 = copy.deepcopy(PAYLOAD_PLAYERS)
        page1["paging"] = {"current": 1, "total": 1}
        with mock.patch.object(fetch_player_stats, "_http_get_json",
                               return_value=page1):
            with mock.patch.object(fetch_player_stats, "assert_shape") as spy:
                records, warns = fetch_player_stats.fetch_team_players(
                    "x" * 32, 30, "2026",
                )
        self.assertTrue(spy.called)
        called_with_baseline = any(
            "apifootball_players.shape.json" in str(call)
            for call in spy.call_args_list
        )
        self.assertTrue(
            called_with_baseline,
            f"assert_shape must reference the /players baseline; "
            f"got: {spy.call_args_list}",
        )
        self.assertIsInstance(records, list)

    def test_drifted_payload_does_not_crash_fetch(self):
        drifted = copy.deepcopy(PAYLOAD_PLAYERS)
        drifted["paging"] = {"current": 1, "total": 1}
        drifted["response"][0]["athlete"] = drifted["response"][0].pop("player")
        with mock.patch.object(fetch_player_stats, "_http_get_json",
                               return_value=drifted):
            with self.assertLogs("schema_watchdog", level="WARNING") as cm:
                records, warns = fetch_player_stats.fetch_team_players(
                    "x" * 32, 30, "2026",
                )
        joined = "\n".join(cm.output)
        self.assertIn("SHAPE DRIFT", joined)
        self.assertIn("apifootball_players.shape.json", joined)
        self.assertIsInstance(records, list)


# ---------------------------------------------------------------------------
# 6. Module-level wiring sanity — guard against accidental un-wiring.
# ---------------------------------------------------------------------------

class TestModulesImportAssertShape(unittest.TestCase):
    """All 5 fetch_* modules must import assert_shape and define
    _SCHEMA_BASELINE_DIR. Mirrors the contract pinned in
    test_fetch_results_schema.TestWiringIsPresent for the other 2 sites."""

    modules = [
        fetch_injuries,
        fetch_lineups,
        fetch_match_stats,
        fetch_player_stats,
    ]

    def test_modules_expose_assert_shape(self):
        for m in self.modules:
            with self.subTest(module=m.__name__):
                self.assertTrue(
                    hasattr(m, "assert_shape"),
                    f"{m.__name__} must import assert_shape at module top",
                )

    def test_modules_expose_schema_baseline_dir(self):
        for m in self.modules:
            with self.subTest(module=m.__name__):
                self.assertTrue(
                    hasattr(m, "_SCHEMA_BASELINE_DIR"),
                    f"{m.__name__} must define _SCHEMA_BASELINE_DIR",
                )
                self.assertTrue(
                    m._SCHEMA_BASELINE_DIR.is_dir(),
                    f"{m.__name__}._SCHEMA_BASELINE_DIR must exist on disk",
                )

    def test_all_five_baselines_present_on_disk(self):
        required = [
            "apifootball_injuries.shape.json",
            "apifootball_fixtures_lineups.shape.json",
            "apifootball_fixtures_statistics.shape.json",
            "apifootball_teams.shape.json",
            "apifootball_players.shape.json",
        ]
        for fn in required:
            with self.subTest(baseline=fn):
                self.assertTrue(
                    (BASELINES / fn).exists(),
                    f"required baseline missing: {fn}",
                )


if __name__ == "__main__":
    unittest.main()
