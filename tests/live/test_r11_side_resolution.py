"""R11 E3 + E4 regression — fetcher side-resolution MUST match by
canonical name, not by iteration order.

Pre-R11 the `len(response_sides) == 2` fallback in
`fetch_match_stats.build_match_entry` (E3) and the bare positional
"first side = home, second = away" assignment in
`fetch_lineups.build_lineup_entry` (E4) both silently swapped home/away
data whenever the API-Football provider returned the sides in
[away, home] order (no documented ordering guarantee).

Consequences pre-R11:
  - E3: home_form_adjustment_elo / away_form_adjustment_elo SIGN-FLIPPED
        for every mis-ordered fixture; the trailing team gets a positive
        Elo boost in a match it dominated against.
  - E4: GK swap fires (-8 Elo each), 11 outfield diffs fire → cap-hit
        LINEUP_CAP=20 penalty per team on every mis-ordered fixture.

R11 fixes use `normalize_team(side.team.name)` against canonical
home/away from the fixture and emit a `side_match_warnings` list when
a side doesn't match either canonical name.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestR11E3MatchStatsSideResolution(unittest.TestCase):
    def setUp(self):
        self.fms = _load("fms", ROOT / "scripts" / "live" / "fetch_match_stats.py")

    def test_away_first_response_does_not_swap_sides(self):
        """Provider returns [away, home] order. Pre-R11 the positional
        fallback assigned home_stats_raw from the away side; R11 fix
        matches by canonical name."""
        match = {"m": 1, "home": "Spain", "away": "Portugal"}
        response_sides = [
            {"team": {"name": "Portugal"},
             "statistics": [{"type": "Shots on Goal", "value": 7}]},  # away first
            {"team": {"name": "Spain"},
             "statistics": [{"type": "Shots on Goal", "value": 2}]},
        ]
        entry = self.fms.build_match_entry(match, response_sides, "fx-1")
        # stats_to_dict preserves the provider's raw type key.
        self.assertEqual(entry["home_stats"].get("Shots on Goal"), 2,
            "R11 E3: home_stats must reflect Spain (the canonical home), "
            "not Portugal (the side the provider returned first)")
        self.assertEqual(entry["away_stats"].get("Shots on Goal"), 7,
            "R11 E3: away_stats must reflect Portugal")
        self.assertEqual(entry.get("side_match_warnings", []), [])

    def test_unrecognized_side_yields_warning_not_silent_swap(self):
        """A side whose team name doesn't match either canonical must
        emit a side_match_warnings entry — pre-R11 this silently
        cascaded the wrong assignment."""
        match = {"m": 5, "home": "Brazil", "away": "Mexico"}
        response_sides = [
            {"team": {"name": "Brazil"}, "statistics": []},
            {"team": {"name": "Atletico Madrid"},  # wrong team
             "statistics": [{"type": "Shots on Goal", "value": 99}]},
        ]
        entry = self.fms.build_match_entry(match, response_sides, "fx-5")
        warnings = entry.get("side_match_warnings", [])
        self.assertEqual(len(warnings), 1,
            "R11 E3: unrecognized side must emit one warning entry")
        self.assertIn("Atletico Madrid", warnings[0])

    def test_normalize_team_is_imported(self):
        """Static pin: normalize_team must be imported from fetch_results
        so canonical team aliases ('USA' → 'United States' etc.) match."""
        src = (ROOT / "scripts" / "live" / "fetch_match_stats.py").read_text()
        self.assertIn("from fetch_results import normalize_team", src,
            "R11 E3: fetch_match_stats must import normalize_team")

    def test_positional_fallback_removed(self):
        """Static pin: the `or len(response_sides) == 2` positional
        fallback must NOT appear inside build_match_entry's body
        (mention in the docstring / module comment is acceptable —
        we want to document the historical bug)."""
        src = (ROOT / "scripts" / "live" / "fetch_match_stats.py").read_text()
        i = src.find("def build_match_entry(")
        j = src.find("\ndef ", i + 1)
        body = src[i:j]
        # Strip the docstring (between first and second triple-quotes).
        first = body.find('"""')
        second = body.find('"""', first + 3) if first >= 0 else -1
        body_no_doc = body[second + 3:] if second >= 0 else body
        self.assertNotIn("len(response_sides) == 2", body_no_doc,
            "R11 E3: positional fallback must not appear in executable "
            "code of build_match_entry — name-based match only")


class TestR11E4LineupsSideResolution(unittest.TestCase):
    def setUp(self):
        self.fl = _load("fl", ROOT / "scripts" / "live" / "fetch_lineups.py")

    def test_away_first_response_does_not_swap_lineups(self):
        """Provider returns [away, home] for /fixtures/lineups. Pre-R11
        bare positional assignment swapped home_block/away_block — R11
        fix matches by canonical team name."""
        sched = {"m": 10, "home": "Germany", "away": "France",
                 "date": "2026-06-15", "time": "20:00"}
        response_sides = [
            {"team": {"name": "France"},
             "startXI": [{"player": {"id": 1, "name": "Mbappé", "pos": "F"}}]},
            {"team": {"name": "Germany"},
             "startXI": [{"player": {"id": 2, "name": "Müller", "pos": "F"}}]},
        ]
        prior_xis = {}  # no priors — neutral baseline
        entry = self.fl.build_lineup_entry(sched, response_sides, prior_xis)
        # Home XI should be Germany's (Müller), not France's (Mbappé).
        self.assertEqual(entry["home_xi"][0]["name"], "Müller",
            "R11 E4: home_xi must be Germany's (canonical home), not "
            "France's (the side the provider returned first)")
        self.assertEqual(entry["away_xi"][0]["name"], "Mbappé")
        self.assertEqual(entry.get("side_match_warnings", []), [])

    def test_unrecognized_lineup_side_yields_warning(self):
        sched = {"m": 11, "home": "Italy", "away": "Croatia",
                 "date": "2026-06-16", "time": "20:00"}
        response_sides = [
            {"team": {"name": "Italy"}, "startXI": []},
            {"team": {"name": "Juventus"},  # wrong team
             "startXI": [{"player": {"id": 9, "name": "X", "pos": "F"}}]},
        ]
        entry = self.fl.build_lineup_entry(sched, response_sides, {})
        warnings = entry.get("side_match_warnings", [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("Juventus", warnings[0])

    def test_normalize_team_is_imported_in_fetch_lineups(self):
        src = (ROOT / "scripts" / "live" / "fetch_lineups.py").read_text()
        self.assertIn("from fetch_results import normalize_team", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
