"""R11 E1 regression — `fetch_results.main()` MUST extend the schedule
with `load_knockout_fixtures()` and `validate_match` MUST skip the
home/away cross-reference for KO match IDs (m>=73).

Pre-R11 `schedule = cfg.get("group_stage_schedule", [])` only loaded
72 group rows. Every R32/R16/QF/SF/3rd/Final result (m=73..104) hit
`validate_match` → `next((f for f in schedule if f["m"] == m["m"]), None)`
→ None → `(False, "match {m} not in WC2026 schedule")` → rejected.

From 2026-06-28 (R32 kickoff, T-10 days from R11 commit) the
dashboard would freeze at end-of-groups; suspensions never resolve
from KO events; sim re-samples completed KOs 25,000× per tick.

This test guards against a future "simplify the validator" PR
silently reverting either half of the fix.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))


def _load_fetch_results():
    spec = importlib.util.spec_from_file_location(
        "fr_module", ROOT / "scripts" / "live" / "fetch_results.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestR11E1ValidateMatchKO(unittest.TestCase):
    def setUp(self):
        self.fr = _load_fetch_results()

    def test_validate_match_accepts_ko_with_resolved_team_names(self):
        """KO matches (m=73) with resolved team names ("Canada"/"Mexico")
        must pass validate_match even when the schedule fixture still
        carries slot codes ("1A"/"2B"). The R11 E1 fix is the m>=73
        early-return that skips the home/away string check."""
        schedule = [
            {"m": 73, "date": "2026-06-28", "home": "1A", "away": "2B",
             "venue": "Estadio Azteca", "stage": "r32"},
        ]
        m = {"m": 73, "home": "Canada", "away": "Mexico",
             "home_score": 2, "away_score": 1, "status": "FT"}
        ok, why = self.fr.validate_match(m, schedule)
        self.assertTrue(ok,
            f"R11 E1: KO m=73 with resolved names must pass; got why={why!r}")

    def test_validate_match_still_enforces_home_away_for_group(self):
        """Group matches (m<73) MUST still cross-reference home/away to
        catch provider-side fixture mismatches. The R11 fix is KO-only."""
        schedule = [
            {"m": 1, "date": "2026-06-11", "home": "Mexico", "away": "Poland"},
        ]
        m = {"m": 1, "home": "Argentina", "away": "Poland",
             "home_score": 1, "away_score": 0, "status": "FT"}
        ok, why = self.fr.validate_match(m, schedule)
        self.assertFalse(ok,
            "R11 E1: home mismatch on a GROUP match must still reject")
        self.assertIn("home mismatch", why)

    def test_main_schedule_includes_knockout_fixtures(self):
        """Static pin: main() must build schedule from
        group_stage_schedule + load_knockout_fixtures() so KO matches are
        discoverable by validate_match."""
        src = (ROOT / "scripts" / "live" / "fetch_results.py").read_text()
        self.assertIn(
            'cfg.get("group_stage_schedule", []) + load_knockout_fixtures()',
            src,
            "R11 E1: main() must extend schedule with load_knockout_fixtures()"
        )

    def test_validate_match_has_ko_skip_branch(self):
        """Static pin: the m>=73 KO-aware early-return must be present."""
        src = (ROOT / "scripts" / "live" / "fetch_results.py").read_text()
        # The KO skip must reference m["m"] >= 73 explicitly.
        self.assertRegex(
            src,
            r'if m\["m"\] >= 73:\s*\n\s*return True, "ok"',
            "R11 E1: validate_match must early-return ok for KO matches"
        )

    def test_validate_match_rejects_when_fixture_id_missing(self):
        """An unknown match id (not in groups or KO) must still reject —
        the fix didn't make validation permissive for arbitrary ids."""
        schedule = []  # empty — nothing to lookup
        m = {"m": 999, "home": "X", "away": "Y",
             "home_score": 0, "away_score": 0, "status": "FT"}
        ok, why = self.fr.validate_match(m, schedule)
        self.assertFalse(ok)
        self.assertIn("not in WC2026 schedule", why)


if __name__ == "__main__":
    unittest.main(verbosity=2)
