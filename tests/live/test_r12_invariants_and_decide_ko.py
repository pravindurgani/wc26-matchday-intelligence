"""R12 C1 + C2 + MED regression — check_invariants extensions +
decide_knockout safety on tied locked KO.

C1: stage_expectations + stack_order extended with p_advance_groups (32)
    and p_reach_r16 (16). Pre-R12 only p_reach_qf/p_reach_sf/p_reach_final
    were pinned; an off-by-one in groups→R32 or R32→R16 transition would
    slide Σ silently.

C2: comment fix — "Σ p_reach_r16 ≈ 32" was wrong; real value is 16.

MED: decide_knockout on a tied locked KO (h==a) with no `winner` field
     RAISES instead of silently defaulting to team_b.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


def _load_ci():
    spec = importlib.util.spec_from_file_location(
        "ci_r12", ROOT / "scripts" / "check_invariants.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestR12C1StageExpectationsExtended(unittest.TestCase):
    def setUp(self):
        self.ci = _load_ci()
        self.src = (ROOT / "scripts" / "check_invariants.py").read_text()

    def test_p_advance_groups_pinned_at_32(self):
        self.assertIn('("p_advance_groups", 32.0)', self.src,
            "R12 C1: stage_expectations must pin p_advance_groups at 32.0")

    def test_p_reach_r16_pinned_at_16(self):
        self.assertIn('("p_reach_r16", 16.0)', self.src,
            "R12 C1: stage_expectations must pin p_reach_r16 at 16.0")

    def test_stack_order_includes_new_fields(self):
        self.assertIn('"p_advance_groups", "p_reach_r16"', self.src,
            "R12 C1: stack_order must extend with p_advance_groups + p_reach_r16")

    def test_off_by_one_groups_transition_caught(self):
        """A blob where p_advance_groups sums to 31 (off-by-one in
        groups→R32) must raise SumOutOfTolerance."""
        tmp = Path(tempfile.mkdtemp(prefix="r12_c1_"))
        try:
            teams = []
            for i in range(48):
                teams.append({
                    "team": f"T{i}",
                    "p_advance_groups": 1.0 if i < 31 else 0.0,  # ← 31 not 32
                    "p_reach_r16": 1.0 if i < 16 else 0.0,
                    "p_reach_qf": 1.0 if i < 8 else 0.0,
                    "p_reach_sf": 1.0 if i < 4 else 0.0,
                    "p_reach_final": 1.0 if i < 2 else 0.0,
                    "p_champion": 1.0 if i == 0 else 0.0,
                })
            blob = {"team_predictions": teams, "annex_c_misses": 0}
            f = tmp / "predictions_live.json"
            f.write_text(json.dumps(blob))
            with self.assertRaises(self.ci.SumOutOfTolerance) as ctx:
                self.ci.check_invariants(f)
            self.assertIn("p_advance_groups", str(ctx.exception))
        finally:
            for p in tmp.iterdir():
                p.unlink()
            tmp.rmdir()


class TestR12C2CommentFix(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / "scripts" / "check_invariants.py").read_text()

    def test_no_wrong_p_reach_r16_eq_32_claim(self):
        """The pre-R12 comment claimed "Σ p_reach_r16 ≈ 32". That's wrong
        (real value is 16). Make sure the wrong text is gone."""
        # Allow `≈ 16` mention; forbid the literal `≈ 32` for p_reach_r16.
        # Be permissive about spacing.
        import re
        wrong_pattern = re.search(
            r"p_reach_r16\s*[≈~=]\s*32", self.src)
        self.assertIsNone(wrong_pattern,
            "R12 C2: the wrong 'Σ p_reach_r16 ≈ 32' comment must be gone")

    def test_correct_value_16_documented(self):
        """The correct value (16) should appear in the comment."""
        # Look for an explanation near p_reach_r16.
        self.assertIn("16 R32 winners reach R16", self.src,
            "R12 C2: comment should explain the correct math (16, not 32)")


class TestR12MEDDecideKnockoutSafety(unittest.TestCase):
    """Tied locked KO with no winner must RAISE, not silently guess."""

    def test_decide_knockout_raises_on_tied_no_winner(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "sim_r12_med", ROOT / "scripts" / "03_simulate.py")
        sim = importlib.util.module_from_spec(spec)
        # Avoid running the full module if heavy (heading the cost is OK
        # for the boundary check)
        spec.loader.exec_module(sim)
        # Build a locked record with h == a and no winner.
        locked = {73: {"home_score": 1, "away_score": 1,
                       # NO winner field
                       }}
        import numpy as np
        rng = np.random.default_rng(0)
        mat = np.ones((11, 11)) / 121
        # decide_knockout signature:
        # (team_a, team_b, m_num, locked, mat, lam_h, lam_a, e_h, e_a, cfg, rng)
        with self.assertRaises(RuntimeError) as ctx:
            sim.decide_knockout(
                "Spain", "Germany", 73, locked,
                mat, 1.5, 1.5, 2000, 2000, sim.DEFAULTS, rng,
            )
        self.assertIn("tied", str(ctx.exception))
        self.assertIn("winner", str(ctx.exception))


class TestR12MEDMaxGRaised(unittest.TestCase):
    """build_score_matrix default max_g must be 15 (was 10 pre-R12)."""

    def test_default_max_g_is_15(self):
        src = (ROOT / "scripts" / "03_simulate.py").read_text()
        # Both signatures: build_score_matrix and sample_score_with_noise.
        import re
        bsm = re.search(r"def build_score_matrix\([^)]*max_g=(\d+)", src)
        self.assertIsNotNone(bsm)
        self.assertEqual(bsm.group(1), "15",
            "R12 MED: build_score_matrix max_g must be 15")
        ssw = re.search(r"def sample_score_with_noise\([^)]*max_g=(\d+)", src)
        self.assertIsNotNone(ssw)
        self.assertEqual(ssw.group(1), "15",
            "R12 MED: sample_score_with_noise max_g must be 15")


if __name__ == "__main__":
    unittest.main(verbosity=2)
