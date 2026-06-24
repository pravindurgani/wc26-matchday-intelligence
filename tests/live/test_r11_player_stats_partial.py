"""R11 C2 regression — fetch_player_stats MUST emit an aggregate
`player_stats_partial` warning when more than 5 of the 48 teams come
back with empty rosters.

Pre-R11 a per-team failure broke the pagination loop and recorded a
warning but the outer loop continued with `out[team] = []`. With no
aggregate threshold detector the operator had no top-level signal that
(say) 14 of 48 teams ended up with empty rosters — auto_tier silently
collapsed to auto_no_data for those teams without any pill lighting up.

R11 C2 counts empty teams and appends a single `player_stats_partial`
warning when count > 5 (~10% of WC squad).
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))


def _load_fps():
    spec = importlib.util.spec_from_file_location(
        "fps_r11_c2", ROOT / "scripts" / "live" / "fetch_player_stats.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestR11C2PlayerStatsPartialAggregate(unittest.TestCase):
    def setUp(self):
        self.fps = _load_fps()

    def test_aggregate_warning_fires_above_threshold(self):
        """Simulate 8 teams returning empty rosters — must emit the
        aggregate player_stats_partial warning."""
        # Build a small team_ids dict (mock fetch_team_ids) + force every
        # team to return [] from fetch_team_players.
        team_ids = {f"Team{i}": i for i in range(10)}
        with patch.object(self.fps, "fetch_team_ids",
                          return_value=(team_ids, [])), \
             patch.object(self.fps, "fetch_team_players",
                          # 8 of 10 return empty; 2 return one player
                          side_effect=lambda *a, **kw: (
                              [{"player": {"id": 1}}] if a[1] <= 1 else [],
                              [])):
            out, warns = self.fps.fetch_apifootball_player_stats(
                api_key="dummy", wc_teams=set(team_ids.keys()), sleep_between=0)
        partial = [w for w in warns if w.get("type") == "player_stats_partial"]
        self.assertEqual(len(partial), 1,
            f"R11 C2: 8 empty teams must trigger 1 player_stats_partial "
            f"aggregate warning; got warns={warns!r}")
        self.assertEqual(partial[0]["count"], 8)
        self.assertIn("teams", partial[0])

    def test_aggregate_warning_does_not_fire_below_threshold(self):
        """A small number of empty teams (≤5) is normal noise — must NOT
        cry-wolf with the aggregate warning."""
        team_ids = {f"Team{i}": i for i in range(48)}
        # Only 3 of 48 return empty.
        def _fetch(api_key, tid, season, rate_limiter=None):
            return ([] if tid in (0, 1, 2)
                    else [{"player": {"id": tid * 100}}]), []
        with patch.object(self.fps, "fetch_team_ids",
                          return_value=(team_ids, [])), \
             patch.object(self.fps, "fetch_team_players", side_effect=_fetch):
            out, warns = self.fps.fetch_apifootball_player_stats(
                api_key="dummy", wc_teams=set(team_ids.keys()), sleep_between=0)
        partial = [w for w in warns if w.get("type") == "player_stats_partial"]
        self.assertEqual(len(partial), 0,
            f"R11 C2: 3 empty teams (below threshold 5) must NOT trigger "
            f"the aggregate warning; got warns={warns!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
