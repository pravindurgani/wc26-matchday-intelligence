"""R12 B3 regression — update_team_state.py MUST extend schedule_by_m
with KO fixtures so completed_matches with m>=73 actually contribute
Elo deltas.

Pre-R12 `schedule_by_m = {f["m"]: f for f in cfg["group_stage_schedule"]}`
was groups-only. Every completed KO match (m>=73) hit `if not fx: continue`
and was silently skipped. K=60 tournament-strength updates froze at end-
of-groups; MAX_PER_KNOCKOUT=15 cap was unreachable; auto_tier never saw
KO-elimination Elo movement.

R12 B3 extends schedule_by_m with load_knockout_fixtures() and resolves
KO home/away from the match record itself (KO bracket entries hold slot
codes "1A"/"W74" until results lock; the result record has resolved
team names per fetch_results.py:661-667).
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))


def _load_uts():
    spec = importlib.util.spec_from_file_location(
        "uts_r12_b3", ROOT / "scripts" / "live" / "update_team_state.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestR12B3UpdateTeamStateKO(unittest.TestCase):
    def setUp(self):
        self.uts = _load_uts()
        self.src = (ROOT / "scripts" / "live"
                    / "update_team_state.py").read_text()

    def test_load_knockout_fixtures_imported(self):
        """Static pin: load_knockout_fixtures must be imported."""
        self.assertIn("from _knockout import load_knockout_fixtures", self.src,
            "R12 B3: update_team_state must import load_knockout_fixtures")

    def test_schedule_extended_with_ko(self):
        """Static pin: schedule_by_m must merge groups + KO."""
        self.assertRegex(
            self.src,
            r'schedule_by_m\s*=\s*\{f\["m"\]:\s*f\s+for\s+f\s+in[^\}]*group_stage_schedule[^\}]*\+\s*load_knockout_fixtures\(\)',
            "R12 B3: schedule_by_m must extend with load_knockout_fixtures()"
        )

    def test_ko_match_resolves_home_away_from_record(self):
        """KO match (m>=73) must read home/away from the match record,
        not the fixture (which has slot codes pre-resolution)."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            live = tdp / "data" / "live"
            raw = tdp / "data" / "raw"
            proc = tdp / "data" / "processed"
            for p in (live, raw, proc):
                p.mkdir(parents=True)
            # Minimal results with one KO match.
            (live / "results_2026.json").write_text(json.dumps({
                "completed_matches": [
                    {"m": 73, "home": "Spain", "away": "Germany",
                     "home_score": 2, "away_score": 1, "status": "FT"},
                ]
            }))
            (proc / "elo_ratings.json").write_text(json.dumps({
                "Spain": 2200.0, "Germany": 2050.0,
            }))
            (raw / "wc2026_config.json").write_text(json.dumps({
                "group_stage_schedule": []
            }))
            with patch.object(self.uts, "LIVE", live), \
                 patch.object(self.uts, "RAW", raw), \
                 patch.object(self.uts, "PROC", proc), \
                 patch.object(self.uts, "load_knockout_fixtures",
                              return_value=[
                                  {"m": 73, "home": "1A", "away": "2B",
                                   "date": "2026-06-28", "venue": "X"}
                              ]):
                self.uts.main()
            blob = json.loads((live / "live_team_state.json").read_text())
            # Spain won → positive delta; Germany lost → negative.
            deltas = blob["deltas"]
            self.assertGreater(deltas.get("Spain", 0), 0,
                "R12 B3: KO winner Spain must have positive Elo delta")
            self.assertLess(deltas.get("Germany", 0), 0,
                "R12 B3: KO loser Germany must have negative Elo delta")
            self.assertEqual(blob["n_processed"], 1,
                "R12 B3: the KO match must be counted (pre-R12 it was "
                "silently skipped via `if not fx: continue`)")

    def test_unresolved_ko_slot_skipped_safely(self):
        """If a KO match record arrives without resolved home/away (pre-
        bracket-resolve), skip without crashing."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            live = tdp / "data" / "live"
            raw = tdp / "data" / "raw"
            proc = tdp / "data" / "processed"
            for p in (live, raw, proc):
                p.mkdir(parents=True)
            (live / "results_2026.json").write_text(json.dumps({
                "completed_matches": [
                    {"m": 73, "home_score": 2, "away_score": 1,
                     "status": "FT"},  # no home/away
                ]
            }))
            (proc / "elo_ratings.json").write_text(json.dumps({}))
            (raw / "wc2026_config.json").write_text(json.dumps({
                "group_stage_schedule": []
            }))
            with patch.object(self.uts, "LIVE", live), \
                 patch.object(self.uts, "RAW", raw), \
                 patch.object(self.uts, "PROC", proc), \
                 patch.object(self.uts, "load_knockout_fixtures",
                              return_value=[
                                  {"m": 73, "home": "1A", "away": "2B"}
                              ]):
                self.uts.main()  # must not raise
            blob = json.loads((live / "live_team_state.json").read_text())
            # Nothing processed (record had no home/away)
            self.assertEqual(blob["n_processed"], 0)

    def test_ko_match_grants_knockout_bonus_cap(self):
        """A team that played a KO match gets MAX_PER_TEAM_GROUP +
        MAX_PER_KNOCKOUT cap (30+15=45). Pre-R12 the `deltas_count <= 3`
        heuristic was a proxy for "still in group stage" — now we key on
        actual ko_matches_count."""
        # Read the source — verify the ko_matches_count + cap selection.
        self.assertIn("ko_matches_count", self.src,
            "R12 B3: must track ko_matches_count for accurate cap selection")
        self.assertIn("MAX_PER_TEAM_GROUP + MAX_PER_KNOCKOUT", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
