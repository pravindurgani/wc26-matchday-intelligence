"""Unit tests for fetch_player_stats.to_stats name resolution.

API-Football returns initial-dot-surname forms ("L. Messi"); the whitelist
queries with canonical full names ("Lionel Messi"). Without normalisation
+ surname-initial fallback the auto_tier layer would silently degrade to
auto_no_data for ~85% of stars (real diff baseline before the fix).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

from fetch_player_stats import to_stats  # noqa: E402


def _payload(*players, top=1000):
    return {"team_top_minutes": top, "players": list(players)}


def _p(name, **kw):
    base = {"name": name, "minutes": 800, "goals": 5, "assists": 2,
            "appearances": 10, "clean_sheets": 0, "position": "F"}
    base.update(kw)
    return base


class TestToStatsNameResolution(unittest.TestCase):
    def test_initial_dot_surname_matches_canonical_full_name(self):
        payload = _payload(_p("L. Messi", minutes=900, goals=8))
        stats = to_stats(payload, "Lionel Messi")
        self.assertIsNotNone(stats)
        self.assertEqual(stats.minutes, 900)
        self.assertEqual(stats.goals, 8)

    def test_accented_surname_resolves(self):
        payload = _payload(_p("E. Martínez", minutes=850, position="G",
                              clean_sheets=6))
        stats = to_stats(payload, "Emiliano Martínez")
        self.assertIsNotNone(stats)
        self.assertEqual(stats.clean_sheets, 6)

    def test_ambiguous_surname_disambiguates_via_first_initial(self):
        payload = _payload(
            _p("L. Martínez", minutes=900, goals=9),   # Lautaro
            _p("Lisandro Martínez", minutes=700, position="D"),
        )
        # Canonical "Lautaro Martínez" — initial 'l' matches both, but
        # only Lautaro's payload name "L. Martínez" shares the full first
        # token 'l' with the target initial. Lisandro's first token is
        # 'lisandro'. Both start with 'l' → ambiguous → return None
        # (safer than guessing).
        stats = to_stats(payload, "Lautaro Martínez")
        self.assertIsNone(stats)

    def test_full_canonical_name_match_wins(self):
        payload = _payload(
            _p("Lautaro Martínez", minutes=900, goals=9),
            _p("Lisandro Martínez", minutes=700, position="D"),
        )
        stats = to_stats(payload, "Lautaro Martínez")
        self.assertIsNotNone(stats)
        self.assertEqual(stats.goals, 9)

    def test_unique_surname_match_when_no_collisions(self):
        payload = _payload(_p("V. Junior", minutes=950, goals=11))
        # Whitelist may store "Vinicius Junior" — unique surname → match.
        stats = to_stats(payload, "Vinicius Junior")
        self.assertIsNotNone(stats)
        self.assertEqual(stats.goals, 11)

    def test_empty_payload_returns_none(self):
        self.assertIsNone(to_stats({}, "Anyone"))
        self.assertIsNone(to_stats({"team_top_minutes": 100,
                                    "players": []}, "Anyone"))

    def test_missing_player_returns_none(self):
        payload = _payload(_p("R. Mahrez"))
        self.assertIsNone(to_stats(payload, "Someone Else"))

    def test_stroke_character_normalises(self):
        # Ødegaard / Łewandowski — NFKD won't decompose, the pre-translate
        # map in normalize_player_name should cover them.
        payload = _payload(_p("M. Ødegaard", minutes=900, goals=4))
        stats = to_stats(payload, "Martin Ødegaard")
        self.assertIsNotNone(stats)
        self.assertEqual(stats.goals, 4)


if __name__ == "__main__":
    unittest.main()
