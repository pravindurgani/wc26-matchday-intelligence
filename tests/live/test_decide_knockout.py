"""
Unit tests for A.3 + R17 P1 — decide_knockout() in scripts/03_simulate.py.

Verifies that:
  - Locked knockouts (FT/AET/PEN) short-circuit to the real winner.
  - PEN matches return the winner from the `winner` field, NOT score
    comparison (a 0-0 (3-0 pens) win would otherwise be miscounted).
  - R17 P1: locked records carrying the provider's home/away team names
    resolve the winner BY NAME — a name-order-swapped record (provider
    home/away opposite to the sim's slot order) still advances the right
    team, where the old positional decode advanced the WRONG one.
  - R17 P1: a locked pairing that isn't the same two teams the sim
    resolved raises RuntimeError (fail-loud; a warning would let a
    corrupted bracket publish).
  - Legacy locked records WITHOUT names fall back to the positional
    decode (backward compat with pre-R17 results files).
  - Unlocked matches fall through to the existing Monte Carlo sampler.
  - Missing-winner records fall back to score comparison (defensive).

These tests use a tiny fake matrix + fake rng so we don't depend on the
full simulator pipeline. Pure-function testing of the new decider.

Run:
    python3 tests/live/test_decide_knockout.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

# We need to import decide_knockout. The simulator imports pandas/numpy at
# module-load time; that's fine for the test environment.
import importlib.util
spec = importlib.util.spec_from_file_location(
    "simulate_module", ROOT / "scripts" / "03_simulate.py"
)
sim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sim)


# A 1×1 matrix returning [0, 0] every sample — so unlocked paths produce
# deterministic 0-0 → ET → pens output, but ALL locked paths return real
# scores untouched.
_DUMMY_MATRIX = np.array([[1.0]])
_DUMMY_LAMS = (0.5, 0.5)
_DUMMY_ELOS = (1500, 1500)
_DUMMY_CFG = {"lambda_noise_per_match": False, "pen_elo_slope": 200.0}


class TestDecideKnockoutLocked(unittest.TestCase):
    """When a match is in the locked dict, decide_knockout MUST short-circuit."""

    def test_locked_pen_home_wins_via_winner_field(self):
        """Portugal 0-0 (3-0 pens) Slovenia — decoder must read winner field, not score.

        R17 P1: record carries provider home/away names (the shape
        fetch_results writes for every KO match) — winner resolves by name."""
        locked = {
            81: {
                "home": "Portugal", "away": "Slovenia",
                "home_score": 0, "away_score": 0,
                "home_pens": 3, "away_pens": 0,
                "winner": "home", "status": "PEN",
            }
        }
        rng = np.random.default_rng(42)
        h, a, w = sim.decide_knockout(
            "Portugal", "Slovenia", 81, locked,
            _DUMMY_MATRIX, *_DUMMY_LAMS, *_DUMMY_ELOS, _DUMMY_CFG, rng,
        )
        self.assertEqual(h, 0)
        self.assertEqual(a, 0)
        self.assertEqual(w, "Portugal",
                         "PEN winner MUST come from `winner` field — "
                         "score comparison would tie and return None")

    def test_locked_pen_away_wins(self):
        """Japan 1-1 (1-3 pens) Croatia — away wins (by-name, R17 P1)."""
        locked = {
            82: {
                "home": "Japan", "away": "Croatia",
                "home_score": 1, "away_score": 1,
                "home_pens": 1, "away_pens": 3,
                "winner": "away", "status": "PEN",
            }
        }
        rng = np.random.default_rng(42)
        h, a, w = sim.decide_knockout(
            "Japan", "Croatia", 82, locked,
            _DUMMY_MATRIX, *_DUMMY_LAMS, *_DUMMY_ELOS, _DUMMY_CFG, rng,
        )
        self.assertEqual(w, "Croatia")

    def test_locked_aet_winner_from_field(self):
        """England 2-1 AET Slovakia — winner field decides (by-name)."""
        locked = {
            73: {
                "home": "England", "away": "Slovakia",
                "home_score": 2, "away_score": 1,
                "home_pens": None, "away_pens": None,
                "winner": "home", "status": "AET",
            }
        }
        rng = np.random.default_rng(42)
        h, a, w = sim.decide_knockout(
            "England", "Slovakia", 73, locked,
            _DUMMY_MATRIX, *_DUMMY_LAMS, *_DUMMY_ELOS, _DUMMY_CFG, rng,
        )
        self.assertEqual((h, a, w), (2, 1, "England"))

    def test_locked_ft_winner_from_field(self):
        """Regulation win: Spain 2-1 Brazil → winner field (by-name)."""
        locked = {
            89: {
                "home": "Spain", "away": "Brazil",
                "home_score": 2, "away_score": 1,
                "home_pens": None, "away_pens": None,
                "winner": "home", "status": "FT",
            }
        }
        rng = np.random.default_rng(42)
        h, a, w = sim.decide_knockout(
            "Spain", "Brazil", 89, locked,
            _DUMMY_MATRIX, *_DUMMY_LAMS, *_DUMMY_ELOS, _DUMMY_CFG, rng,
        )
        self.assertEqual(w, "Spain")

    def test_locked_missing_winner_falls_back_to_score(self):
        """Defensive: locked record without `winner` falls back to score
        comparison. Legacy shape (no names) → positional decode."""
        locked = {
            73: {
                "home_score": 3, "away_score": 0,
                "home_pens": None, "away_pens": None,
                "winner": None, "status": "FT",
            }
        }
        rng = np.random.default_rng(42)
        h, a, w = sim.decide_knockout(
            "Germany", "Slovakia", 73, locked,
            _DUMMY_MATRIX, *_DUMMY_LAMS, *_DUMMY_ELOS, _DUMMY_CFG, rng,
        )
        self.assertEqual(w, "Germany", "score comparison must pick higher score")


class TestDecideKnockoutNamedRecords(unittest.TestCase):
    """R17 P1 — locked records with provider home/away names resolve BY
    NAME, never by position. The records below mirror what fetch_results
    writes for KO matches (canonical names via normalize_team)."""

    def test_swapped_home_away_resolves_by_name(self):
        """Provider lists Brazil (home) 2-1 Spain (away); the sim resolved
        the slot pairing the other way round (Spain slot_a, Brazil slot_b).
        The pre-R17 positional decode returned team_a=Spain for
        winner=='home' — the WRONG team. By-name resolution must return
        Brazil, with scores re-oriented to the (team_a, team_b) convention."""
        locked = {
            89: {
                "home": "Brazil", "away": "Spain",
                "home_score": 2, "away_score": 1,
                "home_pens": None, "away_pens": None,
                "winner": "home", "status": "FT",
            }
        }
        rng = np.random.default_rng(42)
        h, a, w = sim.decide_knockout(
            "Spain", "Brazil", 89, locked,
            _DUMMY_MATRIX, *_DUMMY_LAMS, *_DUMMY_ELOS, _DUMMY_CFG, rng,
        )
        self.assertEqual(w, "Brazil",
                         "winner=='home' must map to the provider's home "
                         "TEAM NAME (Brazil), not slot position team_a")
        # Scores oriented to (team_a=Spain, team_b=Brazil): Spain scored 1.
        self.assertEqual((h, a), (1, 2))

    def test_swapped_pen_record_resolves_by_name(self):
        """0-0 (pens) with provider order swapped vs slot order: the pens
        winner field must still map through the names."""
        locked = {
            81: {
                "home": "Portugal", "away": "Slovenia",
                "home_score": 0, "away_score": 0,
                "home_pens": 1, "away_pens": 3,
                "winner": "away", "status": "PEN",
            }
        }
        rng = np.random.default_rng(42)
        h, a, w = sim.decide_knockout(
            "Slovenia", "Portugal", 81, locked,
            _DUMMY_MATRIX, *_DUMMY_LAMS, *_DUMMY_ELOS, _DUMMY_CFG, rng,
        )
        # Provider away side (Slovenia) won the shootout; positional decode
        # would have returned team_b=Portugal.
        self.assertEqual((h, a, w), (0, 0, "Slovenia"))

    def test_matching_order_resolves_by_name(self):
        """Sanity: names present and aligned with slot order behaves as
        before."""
        locked = {
            73: {
                "home": "England", "away": "Slovakia",
                "home_score": 2, "away_score": 1,
                "home_pens": None, "away_pens": None,
                "winner": "home", "status": "AET",
            }
        }
        rng = np.random.default_rng(42)
        h, a, w = sim.decide_knockout(
            "England", "Slovakia", 73, locked,
            _DUMMY_MATRIX, *_DUMMY_LAMS, *_DUMMY_ELOS, _DUMMY_CFG, rng,
        )
        self.assertEqual((h, a, w), (2, 1, "England"))

    def test_mismatched_pairing_raises(self):
        """Locked pairing != sim's slot resolution → RuntimeError. Silently
        advancing either team would corrupt every later simulated round."""
        locked = {
            81: {
                "home": "Portugal", "away": "Slovenia",
                "home_score": 0, "away_score": 0,
                "home_pens": 3, "away_pens": 0,
                "winner": "home", "status": "PEN",
            }
        }
        rng = np.random.default_rng(42)
        with self.assertRaises(RuntimeError) as ctx:
            sim.decide_knockout(
                "Portugal", "Denmark", 81, locked,
                _DUMMY_MATRIX, *_DUMMY_LAMS, *_DUMMY_ELOS, _DUMMY_CFG, rng,
            )
        msg = str(ctx.exception)
        for token in ("m=81", "Slovenia", "Denmark"):
            self.assertIn(token, msg,
                          f"mismatch error must name the conflict ({token})")

    def test_named_no_winner_score_comparison_by_name(self):
        """Missing `winner` + names present + order swapped: the score-
        comparison fallback must also resolve by name (provider home
        scored 3 → provider home team wins, even though it's team_b)."""
        locked = {
            73: {
                "home": "Germany", "away": "Slovakia",
                "home_score": 3, "away_score": 0,
                "home_pens": None, "away_pens": None,
                "winner": None, "status": "FT",
            }
        }
        rng = np.random.default_rng(42)
        h, a, w = sim.decide_knockout(
            "Slovakia", "Germany", 73, locked,
            _DUMMY_MATRIX, *_DUMMY_LAMS, *_DUMMY_ELOS, _DUMMY_CFG, rng,
        )
        self.assertEqual(w, "Germany")
        self.assertEqual((h, a), (0, 3),
                         "scores must re-orient to (team_a, team_b)")

    def test_named_tied_no_winner_still_raises(self):
        """R12 MED behavior preserved on the named path: tied + no winner
        is unrecoverable."""
        locked = {
            90: {
                "home": "Japan", "away": "Croatia",
                "home_score": 1, "away_score": 1,
                "home_pens": None, "away_pens": None,
                "winner": None, "status": "PEN",
            }
        }
        rng = np.random.default_rng(42)
        with self.assertRaises(RuntimeError) as ctx:
            sim.decide_knockout(
                "Japan", "Croatia", 90, locked,
                _DUMMY_MATRIX, *_DUMMY_LAMS, *_DUMMY_ELOS, _DUMMY_CFG, rng,
            )
        self.assertIn("tied", str(ctx.exception))
        self.assertIn("winner", str(ctx.exception))

    def test_alias_in_hand_edited_record_normalizes(self):
        """Defense-in-depth: an operator hand-edit carrying a provider
        alias ('USA') must reconcile against the canonical sim name via
        fetch_results.normalize_team — not raise a false mismatch."""
        try:
            from fetch_results import normalize_team
        except Exception:  # pragma: no cover - minimal env
            self.skipTest("fetch_results not importable in this environment")
        if normalize_team("USA") != "United States":
            self.skipTest("alias table no longer maps USA")
        locked = {
            76: {
                "home": "USA", "away": "Mexico",
                "home_score": 2, "away_score": 0,
                "home_pens": None, "away_pens": None,
                "winner": "home", "status": "FT",
            }
        }
        rng = np.random.default_rng(42)
        h, a, w = sim.decide_knockout(
            "United States", "Mexico", 76, locked,
            _DUMMY_MATRIX, *_DUMMY_LAMS, *_DUMMY_ELOS, _DUMMY_CFG, rng,
        )
        self.assertEqual((h, a, w), (2, 0, "United States"),
                         "winner must be the sim's canonical name so "
                         "downstream count dicts / matrix lookups key "
                         "correctly")

    def test_legacy_record_without_names_stays_positional(self):
        """Backward compat: a pre-R17 locked record with no home/away names
        can only be decoded positionally — winner=='home' → team_a."""
        locked = {
            89: {
                "home_score": 0, "away_score": 0,
                "home_pens": 4, "away_pens": 2,
                "winner": "home", "status": "PEN",
            }
        }
        rng = np.random.default_rng(42)
        h, a, w = sim.decide_knockout(
            "Spain", "Brazil", 89, locked,
            _DUMMY_MATRIX, *_DUMMY_LAMS, *_DUMMY_ELOS, _DUMMY_CFG, rng,
        )
        self.assertEqual((h, a, w), (0, 0, "Spain"))


class TestDecideKnockoutUnlocked(unittest.TestCase):
    """When a match is NOT in the locked dict, fall through to resolve_knockout."""

    def test_unlocked_falls_through_to_sampler(self):
        """An empty locked dict means every match samples normally."""
        rng = np.random.default_rng(42)
        # Use a real matrix so we get a real sample. Tiny 2x2 favouring 1-0.
        mat = np.array([
            [0.0, 0.0],
            [1.0, 0.0],  # P(home=1, away=0) = 1.0
        ])
        h, a, w = sim.decide_knockout(
            "Spain", "France", 73, {},  # empty locked
            mat, 1.0, 0.5, 1700, 1650, _DUMMY_CFG, rng,
        )
        # Matrix forces home=1 away=0 deterministically → Spain wins.
        self.assertEqual((h, a, w), (1, 0, "Spain"))

    def test_unlocked_match_not_in_dict(self):
        """Locked dict with OTHER matches doesn't affect this one."""
        locked = {
            73: {"home_score": 5, "away_score": 0, "winner": "home", "status": "FT"},
            # M89 NOT locked
        }
        rng = np.random.default_rng(42)
        mat = np.array([[0.0, 1.0], [0.0, 0.0]])  # forces home=0 away=1 → away wins
        h, a, w = sim.decide_knockout(
            "Argentina", "Netherlands", 89, locked,
            mat, 1.0, 1.0, 1700, 1650, _DUMMY_CFG, rng,
        )
        # M89 not in locked → samples → Netherlands (away) per the matrix.
        self.assertEqual(w, "Netherlands")


class TestLoadCompletedMatchesSchema(unittest.TestCase):
    """load_completed_matches captures all the new A.2 fields."""

    def test_captures_pens_and_winner(self):
        """Round-trip: a JSON record with all fields ends up in the dict."""
        import json
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
            json.dump({
                "completed_matches": [
                    {
                        "m": 81, "home": "Portugal", "away": "Slovenia",
                        "home_score": 0, "away_score": 0,
                        "home_pens": 3, "away_pens": 0,
                        "winner": "home", "status": "PEN",
                    },
                    {
                        "m": 1, "home": "Mexico", "away": "South Africa",
                        "home_score": 2, "away_score": 1,
                        "home_pens": None, "away_pens": None,
                        "winner": None, "status": "FT",
                    },
                ]
            }, tmp)
            path = Path(tmp.name)
        try:
            result = sim.load_completed_matches(path)
            self.assertEqual(result[81]["winner"], "home")
            self.assertEqual(result[81]["home_pens"], 3)
            self.assertEqual(result[81]["status"], "PEN")
            # R17 P1: provider team names MUST survive the load — without
            # them decide_knockout can only decode `winner` positionally
            # (the exact bug this fix removes).
            self.assertEqual(result[81]["home"], "Portugal")
            self.assertEqual(result[81]["away"], "Slovenia")
            self.assertEqual(result[1]["home"], "Mexico")
            self.assertIsNone(result[1]["winner"])
            self.assertIsNone(result[1]["home_pens"])
        finally:
            path.unlink()

    def test_legacy_records_without_new_fields(self):
        """Old results_2026.json (pre-A.2) without pen/winner fields still loads."""
        import json
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
            json.dump({
                "completed_matches": [
                    {"m": 1, "home_score": 2, "away_score": 1}  # only legacy fields
                ]
            }, tmp)
            path = Path(tmp.name)
        try:
            result = sim.load_completed_matches(path)
            self.assertEqual(result[1]["home_score"], 2)
            self.assertIsNone(result[1].get("winner"),
                              "missing winner field must surface as None, not raise")
            # R17 P1: legacy records without names load as None — signals
            # decide_knockout to take the positional backward-compat path.
            self.assertIsNone(result[1].get("home"))
            self.assertIsNone(result[1].get("away"))
        finally:
            path.unlink()


def _summary(result):
    print()
    print(f"  Ran {result.testsRun} tests")
    if result.wasSuccessful():
        print(f"  ✓ all passed")
    else:
        print(f"  ✗ {len(result.failures)} failures, {len(result.errors)} errors")


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    _summary(result)
    sys.exit(0 if result.wasSuccessful() else 1)
