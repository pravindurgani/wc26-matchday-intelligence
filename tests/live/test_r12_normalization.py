"""R12 A1 + A2 regression — feature normalization on join keys.

A1: suspension_tracker yellow_counter and apply_matchday_adjustments
    cross-subsystem dedup MUST normalize player names before keying.
    Pre-R12 raw "R. Jiménez" and "Raúl Jiménez" produced different keys,
    splitting the count and silently zeroing suspensions.

A2: apply_matchday_adjustments overlay reader MUST normalize team names
    before bucketing. Pre-R12 operator-entered "USA" / "Korea Republic"
    silently failed because get_team_elo_adjustment uses strict equality.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestR12A1SuspensionPlayerNameNormalization(unittest.TestCase):
    """Yellow accumulation across matches must dedupe player-name variants."""

    def setUp(self):
        self.st = _load("st_r12_a1",
                        ROOT / "scripts" / "live" / "suspension_tracker.py")
        self.src = (ROOT / "scripts" / "live"
                    / "suspension_tracker.py").read_text()

    def test_normalize_player_name_imported(self):
        self.assertIn("normalize_player_name", self.src,
            "R12 A1: suspension_tracker must import normalize_player_name")
        self.assertIn("player_join_key", self.src,
            "R12 A1: suspension_tracker must also import player_join_key "
            "(stronger join form for cross-feed initial-form dedup)")

    def test_player_key_uses_normalized_form(self):
        """Static pin: player_key derived via the stronger join form."""
        self.assertIn("player_key = player_join_key", self.src,
            "R12 A1: player join key must be the player_join_key form "
            "(drops single-letter initials, falls back to surname)")

    def test_yellow_accumulation_across_name_variants(self):
        """Two yellow cards for the same KEY player (in key_players_2026.
        json) emitted as different provider name forms ('E. Álvarez' /
        'Edson Álvarez') must trigger the YELLOW_THRESHOLD=2 suspension
        via the R13 A1 team-aware canonical resolution.

        R12 used surname-only collapse, which over-merged intra-team
        same-surname pairs. R13 resolves via key_players index: known
        players canonicalize ("E. Álvarez" → "edson alvarez"); unknown
        players or ambiguous surnames preserve the forename component.
        """
        schedule = [
            {"m": 1, "home": "Mexico", "away": "Poland",
             "date": "2026-06-11", "stage": "group", "group": "A"},
            {"m": 17, "home": "Mexico", "away": "Saudi Arabia",
             "date": "2026-06-18", "stage": "group", "group": "A"},
            {"m": 33, "home": "Mexico", "away": "Argentina",
             "date": "2026-06-25", "stage": "group", "group": "A"},
        ]
        # Edson Álvarez IS in key_players_2026.json (Mexico tier_1_star).
        # The by_last index resolves "E. Álvarez" → "edson alvarez"
        # because there's only one Álvarez on Mexico's key_players.
        completed = [
            {"m": 1, "home": "Mexico", "away": "Poland",
             "home_score": 1, "away_score": 0, "status": "FT",
             "events": [
                 {"type": "card", "subtype": "yellow_card",
                  "team": "Mexico", "player": "E. Álvarez", "minute": 30},
             ]},
            {"m": 17, "home": "Mexico", "away": "Saudi Arabia",
             "home_score": 2, "away_score": 0, "status": "FT",
             "events": [
                 {"type": "card", "subtype": "yellow_card",
                  "team": "Mexico", "player": "Edson Álvarez", "minute": 60},
             ]},
        ]
        suspensions, summary = self.st.build_suspensions(
            completed, schedule)
        # Expected: 1 accumulation suspension for m=33.
        accum = [s for s in suspensions if s["reason"] == "accumulated_yellows"]
        self.assertEqual(len(accum), 1,
            f"R13 A1: yellow accumulation across 'E. Álvarez' / 'Edson Álvarez' "
            f"must trigger 1 suspension via key_players canonicalization; "
            f"got {len(accum)}; suspensions={suspensions}")
        self.assertEqual(accum[0]["match_id"], 33,
            "R13 A1: suspension must be for the NEXT match (m=33)")

    def test_red_card_dedup_across_provider_variants(self):
        """Two providers emitting the same red card with name drift must
        not result in two suspension rows. Uses a player WITH key_players
        coverage (Vinícius Júnior on Brazil) so R13 A1 canonical resolution
        applies."""
        schedule = [
            {"m": 1, "home": "Brazil", "away": "Switzerland",
             "date": "2026-06-12", "stage": "group", "group": "E"},
            {"m": 17, "home": "Brazil", "away": "Cameroon",
             "date": "2026-06-19", "stage": "group", "group": "E"},
        ]
        completed = [
            {"m": 1, "home": "Brazil", "away": "Switzerland",
             "home_score": 1, "away_score": 0, "status": "FT",
             "events": [
                 {"type": "card", "subtype": "red_card",
                  "team": "Brazil", "player": "Vinícius Júnior",
                  "minute": 88},
                 # Provider duplicate (different name form, same incident).
                 {"type": "card", "subtype": "red_card",
                  "team": "Brazil", "player": "Vinicius Junior",
                  "minute": 88},
             ]},
        ]
        suspensions, _ = self.st.build_suspensions(completed, schedule)
        # Expected: 1 red-card suspension, not 2.
        reds = [s for s in suspensions if s["reason"] == "red_card"]
        self.assertEqual(len(reds), 1,
            f"R12 A1 / R13 A1: red-card dedup across name variants must "
            f"collapse to 1; got {len(reds)}; suspensions={suspensions}")


class TestR12A2OverlayTeamNormalization(unittest.TestCase):
    """Operator overlay team field must normalize to canonical name."""

    def setUp(self):
        self.amd_src = (ROOT / "scripts" / "live"
                        / "apply_matchday_adjustments.py").read_text()

    def test_normalize_team_imported(self):
        self.assertIn("from fetch_results import normalize_team",
                      self.amd_src,
            "R12 A2: apply_matchday must import normalize_team")

    def test_overlay_team_normalized_before_bucketing(self):
        """Static pin: raw_team → normalize_team before key construction."""
        # The fix renamed local `team` → `raw_team` then computed
        # `team = normalize_team(raw_team)`. Verify that pattern.
        self.assertIn("raw_team = adj.get(\"team\")", self.amd_src)
        self.assertIn("team = normalize_team(raw_team)", self.amd_src)

    def test_pre_flight_gate_added(self):
        """Static pin: pre_flight has a team_adjustments.json gate."""
        pf = (ROOT / "scripts" / "pre_flight.py").read_text()
        self.assertIn("team_adjustments.json team field resolves", pf,
            "R12 A2: pre_flight must validate overlay team field resolves "
            "to canonical WC2026 team name")


class TestR12MEDTeamAliasExtensions(unittest.TestCase):
    """R12 MED: TEAM_ALIAS gaps for football-data.org variants."""

    def setUp(self):
        self.fr = _load("fr_r12_med",
                        ROOT / "scripts" / "live" / "fetch_results.py")

    def test_korea_republic_of_normalizes(self):
        self.assertEqual(self.fr.normalize_team("Korea, Republic of"),
                         "South Korea",
            "R12 MED: 'Korea, Republic of' (football-data.org format) "
            "must resolve to South Korea")

    def test_turkiye_parenthesized_normalizes(self):
        self.assertEqual(self.fr.normalize_team("Türkiye (Turkey)"),
                         "Turkey",
            "R12 MED: 'Türkiye (Turkey)' must resolve to Turkey")


if __name__ == "__main__":
    unittest.main(verbosity=2)
