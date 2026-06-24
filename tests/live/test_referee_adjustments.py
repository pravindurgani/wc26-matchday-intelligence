"""Phase 2 — referee_adjustments unit tests.

Covers:
  - baseline load (missing file → {}, malformed → {})
  - _confidence_for thresholds (none / medium / high)
  - compute_referee_entry: honesty floor, cap clamping, missing-baseline,
    no-referee-assigned, full pass-through
  - build_referee_2026: results-missing warning, FT match rows
  - apply_matchday_adjustments._load_referee_components integration:
    one-sided shape, cap-double-enforced, zero-bonus skipped
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

import referee_adjustments as ra  # noqa: E402
import apply_matchday_adjustments as ama  # noqa: E402


_BASELINE = {
    "Szymon Marciniak": {
        "nationality": "Poland",
        "referee_id": "FIFA-POL-MAR-001",
        "home_elo_bonus": -3.96,
        "n_matches": 180,
        "notes": "neg home win rate vs 0.58 baseline",
    },
    "Clement Turpin": {
        "nationality": "France",
        "referee_id": "FIFA-FRA-TUR-001",
        "home_elo_bonus": -6.55,
        "n_matches": 196,
        "notes": "negative bias",
    },
    # exceeds cap on the negative side
    "Extreme Negative": {
        "home_elo_bonus": -15.0,
        "n_matches": 100,
        "notes": "should clamp to -8.0",
    },
    # exceeds cap on the positive side
    "Extreme Positive": {
        "home_elo_bonus": 12.5,
        "n_matches": 110,
        "notes": "should clamp to +8.0",
    },
    # below honesty floor — must zero out even though raw is non-zero
    "Anthony Taylor": {
        "home_elo_bonus": 0.0,
        "n_matches": 0,
        "notes": "FIFA appointment, no splits",
    },
    "Below Floor Nonzero": {
        "home_elo_bonus": -4.0,
        "n_matches": 5,
        "notes": "raw exists but below honesty floor",
    },
}


class TestLoadRefereeBaseline(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        self.assertEqual(ra.load_referee_baseline(Path("/nonexistent.json")), {})

    def test_malformed_json_returns_empty(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False,
        ) as f:
            f.write("not valid json {{{")
            p = Path(f.name)
        try:
            self.assertEqual(ra.load_referee_baseline(p), {})
        finally:
            p.unlink()

    def test_valid_baseline_returns_refs_map(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False,
        ) as f:
            json.dump({"refs": _BASELINE}, f)
            p = Path(f.name)
        try:
            out = ra.load_referee_baseline(p)
            self.assertIn("Szymon Marciniak", out)
            self.assertEqual(out["Clement Turpin"]["n_matches"], 196)
        finally:
            p.unlink()


class TestConfidenceFor(unittest.TestCase):
    def test_high_at_100(self):
        self.assertEqual(ra._confidence_for(100), "high")
        self.assertEqual(ra._confidence_for(180), "high")

    def test_medium_at_20(self):
        self.assertEqual(ra._confidence_for(20), "medium")
        self.assertEqual(ra._confidence_for(99), "medium")

    def test_none_below_20(self):
        self.assertEqual(ra._confidence_for(0), "none")
        self.assertEqual(ra._confidence_for(19), "none")


class TestComputeRefereeEntry(unittest.TestCase):
    def test_no_referee_assigned(self):
        e = ra.compute_referee_entry(1, "Mexico", "Canada", None, _BASELINE)
        self.assertEqual(e["home_team_adjustment_elo"], 0.0)
        self.assertEqual(e["away_team_adjustment_elo"], 0.0)
        self.assertEqual(e["reason"], "no_referee_assigned")
        self.assertEqual(e["confidence"], "none")
        self.assertEqual(e["n_matches"], 0)

    def test_ref_not_in_baseline(self):
        e = ra.compute_referee_entry(2, "Mexico", "Canada", "Unknown Ref", _BASELINE)
        self.assertEqual(e["home_team_adjustment_elo"], 0.0)
        self.assertEqual(e["reason"], "ref_not_in_baseline")

    def test_honesty_floor_zeros_bonus(self):
        e = ra.compute_referee_entry(3, "A", "B", "Below Floor Nonzero", _BASELINE)
        # Raw exists but n_matches < 20 → zero out and tag with reason.
        self.assertEqual(e["home_team_adjustment_elo"], 0.0)
        self.assertEqual(e["reason"], "honesty_floor_n_matches")
        self.assertEqual(e["confidence"], "none")
        self.assertEqual(e["n_matches"], 5)

    def test_normal_negative_bonus_passes_through(self):
        e = ra.compute_referee_entry(4, "A", "B", "Szymon Marciniak", _BASELINE)
        self.assertEqual(e["home_team_adjustment_elo"], -3.96)
        self.assertEqual(e["away_team_adjustment_elo"], 0.0)
        self.assertEqual(e["confidence"], "high")
        self.assertEqual(e["reason"], "ref_home_bias")
        self.assertEqual(e["cap_used"], ra.REFEREE_CAP)

    def test_cap_clamp_negative(self):
        e = ra.compute_referee_entry(5, "A", "B", "Extreme Negative", _BASELINE)
        self.assertEqual(e["home_team_adjustment_elo"], -ra.REFEREE_CAP)
        self.assertEqual(e["raw_home_elo"], -15.0)

    def test_cap_clamp_positive(self):
        e = ra.compute_referee_entry(6, "A", "B", "Extreme Positive", _BASELINE)
        self.assertEqual(e["home_team_adjustment_elo"], ra.REFEREE_CAP)
        self.assertEqual(e["raw_home_elo"], 12.5)

    def test_zero_raw_with_high_n_still_returns_row(self):
        # Anthony Taylor: n=0, raw=0 — surfaces as confidence=none (honesty floor),
        # zero bonus, but row is present so dashboard sees the assignment.
        e = ra.compute_referee_entry(7, "A", "B", "Anthony Taylor", _BASELINE)
        self.assertEqual(e["home_team_adjustment_elo"], 0.0)
        self.assertEqual(e["confidence"], "none")
        self.assertEqual(e["reason"], "honesty_floor_n_matches")


class TestBuildReferee2026(unittest.TestCase):
    def test_results_missing_warns(self):
        payload = ra.build_referee_2026(
            results_path=Path("/nonexistent_results.json"),
            baseline_path=Path("/nonexistent_baseline.json"),
        )
        self.assertEqual(payload["referee"], [])
        self.assertTrue(any(w["type"] == "results_missing" for w in payload["warnings"]))
        self.assertEqual(payload["cap_used"], ra.REFEREE_CAP)
        self.assertEqual(payload["min_matches_for_bonus"], ra.MIN_MATCHES_FOR_BONUS)

    def test_builds_rows_from_completed_matches(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            results_path = tdp / "results.json"
            baseline_path = tdp / "baseline.json"
            results_path.write_text(json.dumps({
                "completed_matches": [
                    {"m": 1, "home": "Mexico", "away": "Canada",
                     "referee": "Szymon Marciniak"},
                    {"m": 2, "home": "USA", "away": "Brazil",
                     "referee": "Below Floor Nonzero"},
                    {"m": 3, "home": "Spain", "away": "Italy",
                     "referee": None},
                    # missing m key — skipped silently
                    {"home": "X", "away": "Y", "referee": "Clement Turpin"},
                ]
            }))
            baseline_path.write_text(json.dumps({"refs": _BASELINE}))
            payload = ra.build_referee_2026(
                results_path=results_path,
                baseline_path=baseline_path,
            )
        rows = payload["referee"]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["home_team_adjustment_elo"], -3.96)
        self.assertEqual(rows[1]["home_team_adjustment_elo"], 0.0)
        self.assertEqual(rows[1]["reason"], "honesty_floor_n_matches")
        self.assertEqual(rows[2]["reason"], "no_referee_assigned")


class TestLoadRefereeComponentsIntegration(unittest.TestCase):
    """apply_matchday_adjustments._load_referee_components shape contract."""

    def _with_live(self, payload):
        return tempfile.TemporaryDirectory(), payload

    def test_one_sided_only_home_emitted(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "referee_2026.json").write_text(json.dumps({
                "referee": [
                    {"match_id": 1, "home_team": "Mexico", "away_team": "Canada",
                     "referee_name": "Szymon Marciniak",
                     "home_team_adjustment_elo": -3.96,
                     "away_team_adjustment_elo": 0.0,
                     "n_matches": 180, "confidence": "high",
                     "reason": "ref_home_bias"},
                ]
            }))
            orig = ama.LIVE
            ama.LIVE = tdp
            try:
                out = ama._load_referee_components()
            finally:
                ama.LIVE = orig
        # Only the home (Mexico, 1) key — away side is not emitted.
        self.assertIn(("Mexico", 1), out)
        self.assertNotIn(("Canada", 1), out)
        comp = out[("Mexico", 1)][0]
        self.assertEqual(comp["type"], "referee")
        self.assertEqual(comp["capped_elo"], -3.96)
        self.assertEqual(comp["cap_used"], ama.REFEREE_CAP)

    def test_cap_double_enforced_at_load_time(self):
        # If on-disk payload somehow exceeds cap (defense in depth), the
        # loader still clamps. Models cap at write time too, but two layers
        # keep a bad upstream from leaking through.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "referee_2026.json").write_text(json.dumps({
                "referee": [
                    {"match_id": 9, "home_team": "X", "away_team": "Y",
                     "referee_name": "Tampered",
                     "home_team_adjustment_elo": -42.0,
                     "away_team_adjustment_elo": 0.0,
                     "reason": "ref_home_bias"},
                ]
            }))
            orig = ama.LIVE
            ama.LIVE = tdp
            try:
                out = ama._load_referee_components()
            finally:
                ama.LIVE = orig
        comp = out[("X", 9)][0]
        self.assertEqual(comp["capped_elo"], -ama.REFEREE_CAP)

    def test_zero_bonus_row_emits_no_component(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "referee_2026.json").write_text(json.dumps({
                "referee": [
                    {"match_id": 1, "home_team": "A", "away_team": "B",
                     "referee_name": "Anthony Taylor",
                     "home_team_adjustment_elo": 0.0,
                     "away_team_adjustment_elo": 0.0,
                     "reason": "honesty_floor_n_matches"},
                ]
            }))
            orig = ama.LIVE
            ama.LIVE = tdp
            try:
                out = ama._load_referee_components()
            finally:
                ama.LIVE = orig
        # Zero contributions should not surface as components (matches
        # weather/lineup loader semantics).
        self.assertEqual(out, {})

    def test_missing_file_yields_empty(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            orig = ama.LIVE
            ama.LIVE = tdp
            try:
                out = ama._load_referee_components()
            finally:
                ama.LIVE = orig
        self.assertEqual(out, {})


if __name__ == "__main__":
    unittest.main()
