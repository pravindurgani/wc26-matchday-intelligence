"""Adversarial audit for scripts/live/referee_adjustments.py.

Each test feeds a subtly broken referee baseline / results pair through
the entry points and classifies the observed behavior as:

  LOUD  — raises, surfaces a warning, or refuses to emit a confident row.
  SILENT — silently emits a confident-looking row with wrong / nonsense
           numbers; codified as xfail(strict=True) so a future fix
           flips the test green naturally.

These tests intentionally do NOT modify production thresholds, caps,
or call sites. AUTO_TIER_ACTIVE stays False.
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

import referee_adjustments as ra  # noqa: E402


# ── LOUD cases ──────────────────────────────────────────────────────────

class TestRefereeLoudFailures(unittest.TestCase):
    """Behaviors that already raise / sanitize — codified to lock them in."""

    def test_missing_referee_record_falls_through_to_zero(self):
        # Ref name present in results but not in baseline → row still emitted
        # but with zero bonus and reason='ref_not_in_baseline'. LOUD via the
        # reason tag (consumer can detect silent-hole).
        e = ra.compute_referee_entry(1, "Mex", "Can", "Unknown Ref", {})
        self.assertEqual(e["home_team_adjustment_elo"], 0.0)
        self.assertEqual(e["reason"], "ref_not_in_baseline")
        self.assertEqual(e["confidence"], "none")

    def test_no_referee_assigned_emits_explicit_reason(self):
        # Empty / None ref → reason='no_referee_assigned'. LOUD via reason.
        e_none = ra.compute_referee_entry(1, "A", "B", None, {})
        e_empty = ra.compute_referee_entry(1, "A", "B", "", {})
        self.assertEqual(e_none["reason"], "no_referee_assigned")
        self.assertEqual(e_empty["reason"], "no_referee_assigned")
        self.assertEqual(e_none["home_team_adjustment_elo"], 0.0)
        self.assertEqual(e_empty["home_team_adjustment_elo"], 0.0)

    def test_zero_historical_matches_zeroes_bonus(self):
        # Ref with n_matches=0 — honesty floor catches it.
        baseline = {"NoHistory": {"home_elo_bonus": -7.0, "n_matches": 0}}
        e = ra.compute_referee_entry(1, "A", "B", "NoHistory", baseline)
        self.assertEqual(e["home_team_adjustment_elo"], 0.0)
        self.assertEqual(e["reason"], "honesty_floor_n_matches")

    def test_negative_n_matches_zeroes_via_honesty_floor(self):
        # n_matches=-50 is nonsense but < threshold → caught by honesty floor.
        # The negative number passes through verbatim into the output row,
        # which is itself a soft-silent issue — but the bonus is zeroed so
        # nothing leaks into the Elo math. LOUD via bonus=0.
        baseline = {"Neg": {"home_elo_bonus": -5.0, "n_matches": -50}}
        e = ra.compute_referee_entry(1, "A", "B", "Neg", baseline)
        self.assertEqual(e["home_team_adjustment_elo"], 0.0)
        self.assertEqual(e["reason"], "honesty_floor_n_matches")

    def test_nan_n_matches_raises_value_error(self):
        # int(NaN) → ValueError. LOUD.
        baseline = {"BadN": {"home_elo_bonus": -5.0, "n_matches": float("nan")}}
        with self.assertRaises(ValueError):
            ra.compute_referee_entry(1, "A", "B", "BadN", baseline)

    def test_inf_n_matches_raises_overflow(self):
        # int(inf) → OverflowError. LOUD.
        baseline = {"BadN": {"home_elo_bonus": -5.0, "n_matches": float("inf")}}
        with self.assertRaises(OverflowError):
            ra.compute_referee_entry(1, "A", "B", "BadN", baseline)

    def test_extreme_positive_bonus_clamps_to_cap(self):
        baseline = {"Crazy": {"home_elo_bonus": 99.0, "n_matches": 200}}
        e = ra.compute_referee_entry(1, "A", "B", "Crazy", baseline)
        self.assertEqual(e["home_team_adjustment_elo"], ra.REFEREE_CAP)

    def test_extreme_negative_bonus_clamps_to_negative_cap(self):
        baseline = {"Crazy": {"home_elo_bonus": -99.0, "n_matches": 200}}
        e = ra.compute_referee_entry(1, "A", "B", "Crazy", baseline)
        self.assertEqual(e["home_team_adjustment_elo"], -ra.REFEREE_CAP)

    def test_string_n_matches_coerces_and_passes(self):
        # int("100") works → 100 → honesty floor passes. Silent type juggling
        # in this case is acceptable because Python int() rejects non-numeric
        # strings loudly.
        baseline = {"Stringy": {"home_elo_bonus": -3.0, "n_matches": "100"}}
        e = ra.compute_referee_entry(1, "A", "B", "Stringy", baseline)
        self.assertEqual(e["home_team_adjustment_elo"], -3.0)

    def test_garbage_string_n_matches_raises(self):
        baseline = {"Stringy": {"home_elo_bonus": -3.0, "n_matches": "many"}}
        with self.assertRaises(ValueError):
            ra.compute_referee_entry(1, "A", "B", "Stringy", baseline)

    def test_baseline_lookup_is_case_sensitive_silent_miss_tagged(self):
        # Real names in the baseline are mixed case. If results sends a
        # lowercased copy, it misses → ref_not_in_baseline. LOUD via reason.
        baseline = {"Szymon Marciniak": {"home_elo_bonus": -3.96, "n_matches": 180}}
        e = ra.compute_referee_entry(1, "A", "B", "szymon marciniak", baseline)
        self.assertEqual(e["reason"], "ref_not_in_baseline")
        self.assertEqual(e["home_team_adjustment_elo"], 0.0)

    def test_results_missing_emits_warning(self):
        payload = ra.build_referee_2026(
            results_path=Path("/nonexistent_results.json"),
            baseline_path=Path("/nonexistent_baseline.json"),
        )
        types = {w["type"] for w in payload["warnings"]}
        self.assertIn("results_missing", types)

    def test_baseline_malformed_json_returns_empty(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{not json")
            p = Path(f.name)
        try:
            self.assertEqual(ra.load_referee_baseline(p), {})
        finally:
            p.unlink()


# ── SILENT cases (xfail strict) ─────────────────────────────────────────

def test_nan_home_elo_bonus_is_neutralised_not_clamped():
    """NaN home_elo_bonus is now neutralised to 0.0 with a labelled reason
    instead of silently clamping to ±REFEREE_CAP with a 'high' confidence
    'ref_home_bias' tag. Fix lives at referee_adjustments.py — math.isfinite
    guard immediately after the float() coercion."""
    baseline = {"NaNRef": {"home_elo_bonus": float("nan"), "n_matches": 100}}
    e = ra.compute_referee_entry(1, "Home", "Away", "NaNRef", baseline)
    assert e["home_team_adjustment_elo"] == 0.0, (
        f"NaN bonus silently clamped to {e['home_team_adjustment_elo']}"
    )
    assert e["reason"] == "nonfinite_in_baseline", (
        f"Expected nonfinite_in_baseline label, got {e['reason']!r}"
    )
    assert e["confidence"] == "none", (
        f"Confidence should be 'none' for nonfinite input, got {e['confidence']!r}"
    )


def test_inf_home_elo_bonus_is_neutralised_not_clamped():
    """Inf home_elo_bonus is now neutralised to 0.0 with a labelled reason
    instead of silently clamping to ±REFEREE_CAP. Same math.isfinite guard
    covers both NaN and Inf cases."""
    baseline = {"InfRef": {"home_elo_bonus": float("inf"), "n_matches": 100}}
    e = ra.compute_referee_entry(1, "Home", "Away", "InfRef", baseline)
    assert e["home_team_adjustment_elo"] == 0.0, (
        f"Inf bonus silently clamped to {e['home_team_adjustment_elo']}"
    )
    assert e["reason"] == "nonfinite_in_baseline", (
        f"Expected nonfinite_in_baseline label, got {e['reason']!r}"
    )
    assert e["confidence"] == "none"


def test_conflicting_referee_names_downgrade_reason():
    """Name-keyed baseline cannot disambiguate two refs sharing a name. When
    a baseline record carries `nationality` but lacks a stable `referee_id`,
    compute_referee_entry now downgrades the reason from 'ref_home_bias' to
    'ref_home_bias_name_keyed_only' so downstream consumers can flag the
    assignment as needing manual disambiguation. Real entries gain identity
    by adding `referee_id` to the baseline JSON."""
    baseline = {
        "Anthony Taylor": {
            "nationality": "England",
            "home_elo_bonus": -2.0,
            "n_matches": 120,
        },
    }
    # Simulate a second arrival (e.g. merge step) — would overwrite silently.
    baseline["Anthony Taylor"] = {
        "nationality": "USA",
        "home_elo_bonus": +6.0,
        "n_matches": 100,
    }
    e = ra.compute_referee_entry(1, "Home", "Away", "Anthony Taylor", baseline)
    assert e["reason"] == "ref_home_bias_name_keyed_only", (
        f"Name collision should downgrade reason, got {e['reason']!r}"
    )
    assert e["reason"] != "ref_home_bias", (
        "Name collision silently resolved without warning"
    )


if __name__ == "__main__":
    unittest.main()
