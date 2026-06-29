"""R32 dashboard regression — resolved KO rows must replace placeholders."""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_JS = ROOT / "dashboard" / "app.js"


def _extract_function(src: str, name: str) -> str:
    start = src.find(f"function {name}(")
    if start == -1:
        raise AssertionError(f"{name} not found in dashboard/app.js")
    end = src.find("\nfunction ", start + 1)
    if end == -1:
        end = len(src)
    return src[start:end]


class TestR32DashboardKoMerge(unittest.TestCase):
    def setUp(self):
        self.src = APP_JS.read_text()

    def test_render_matches_uses_resolved_match_predictions(self):
        body = _extract_function(self.src, "renderMatches")
        self.assertIn("const matches = resolvedMatchPredictions(data);", body)
        self.assertNotIn("data.match_predictions", body,
            "renderMatches must render the KO-overlayed matches array, not "
            "the raw placeholder-only match_predictions feed")

    def test_resolved_match_predictions_overlays_ko_export(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not installed")

        helper = _extract_function(self.src, "resolvedMatchPredictions")
        data = {
            "team_predictions": [
                {"team": "South Africa", "elo": 1656},
                {"team": "Canada", "elo": 1892},
            ],
            "match_predictions": [{
                "m": 73,
                "stage": "r32",
                "date": "2026-06-28",
                "venue": "Inglewood, CA",
                "slot_a": "2A",
                "slot_b": "2B",
                "home": "2A",
                "away": "2B",
            }],
            "match_predictions_ko": [{
                "m": 73,
                "stage": "r32",
                "home": "South Africa",
                "away": "Canada",
                "lambda_home": 0.66,
                "lambda_away": 1.63,
                "p_home_win": 0.16,
                "p_draw": 0.29,
                "p_away_win": 0.55,
                "p_advance_match": 0.305,
            }],
        }
        script = textwrap.dedent(f"""
            {helper}
            const data = {json.dumps(data)};
            const merged = resolvedMatchPredictions(data)[0];
            console.log(JSON.stringify(merged));
        """)
        res = subprocess.run([node, "-e", script], text=True,
                             capture_output=True, check=True)
        merged = json.loads(res.stdout)

        self.assertEqual(merged["home"], "South Africa")
        self.assertEqual(merged["away"], "Canada")
        self.assertEqual(merged["slot_a"], "2A")
        self.assertEqual(merged["slot_b"], "2B")
        self.assertEqual(merged["lam_home"], 0.66)
        self.assertEqual(merged["lam_away"], 1.63)
        self.assertEqual(merged["p_away_win"], 0.55)
        self.assertEqual(merged["elo_home"], 1656)
        self.assertEqual(merged["elo_away"], 1892)


if __name__ == "__main__":
    unittest.main(verbosity=2)
