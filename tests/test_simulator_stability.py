"""
test_simulator_stability.py — regression gate on Monte Carlo stability.

The audit baseline (5000 sims × 5 seeds, production config) shows the top-12
teams' champion-probability spread (p95-p05) maxes out at ~1.63pp (France).
With reduced CI params (2 seeds × 300 sims) the same spread scales up by
roughly √(5000/300) ≈ 4.1×, so a healthy upper bound is ~10pp. A spread above
that signals a determinism break (RNG mis-seeding, parallelism leak) or a
gross model change — not a precision calibration regression.

This is a smoke gate, not a calibration test:
  - sum-to-1 invariant must hold (catch arithmetic drift)
  - top-12 spread < 10pp (catch lost determinism)
  - simulator must succeed end-to-end (catch broken CLI / fixture path)

Runs the simulator as a subprocess so the test exercises the real entry
point (argparse + main()), not an internal API surface. The bracket is
fixed and the tracked artifacts (elo_ratings.json, *_goals_model.joblib,
matches_clean.parquet) are the same ones live-matchday consumes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "scripts" / "03_simulate.py"
OUT_NAME = "_stability_smoke.json"
OUT_PATH = ROOT / "data" / "processed" / OUT_NAME


class SimulatorStability(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # --no-travel keeps the run hermetic (skips travel_impact dep).
        # Low sims/seeds keep wall-clock under CI budget; thresholds below
        # are scaled accordingly.
        cmd = [
            sys.executable, str(SIM),
            "--no-travel",
            "--seeds", "2",
            "--sims", "300",
            "--out", OUT_NAME,
        ]
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = "0"  # belt-and-braces determinism
        result = subprocess.run(
            cmd, cwd=str(ROOT), env=env,
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"simulator failed (rc={result.returncode}):\n"
                f"STDOUT:\n{result.stdout[-2000:]}\n"
                f"STDERR:\n{result.stderr[-2000:]}"
            )
        cls.payload = json.loads(OUT_PATH.read_text())

    @classmethod
    def tearDownClass(cls):
        try:
            OUT_PATH.unlink()
        except FileNotFoundError:
            pass

    def test_p_champion_sum_to_one(self):
        teams = self.payload["team_predictions"]
        total = sum(t["p_champion"] for t in teams)
        self.assertAlmostEqual(
            total, 1.0, places=3,
            msg=f"Σp_champion = {total:.6f} (expected ~1.0); "
                "indicates aggregation bug or partial bracket run."
        )

    def test_top12_seed_spread_bounded(self):
        teams = sorted(self.payload["team_predictions"],
                       key=lambda t: -t["p_champion"])[:12]
        # Threshold: 10pp accommodates the √(5000/300)≈4.1× noise scaling
        # vs the production 1.63pp observation. A value above 10pp signals
        # the seeds are not actually independent OR the bracket/sim chain
        # has been corrupted; both are correctness failures, not noise.
        spreads = []
        for t in teams:
            spread = (t["p_champion_p95"] - t["p_champion_p05"]) * 100
            spreads.append((t["team"], spread))
            self.assertLess(
                spread, 10.0,
                msg=f"{t['team']} spread = {spread:.2f}pp exceeds 10pp guard "
                    "(production baseline ~1.63pp at 5000×5; if this fires, "
                    "check for determinism break or stale model artifacts)"
            )
        max_team, max_spread = max(spreads, key=lambda x: x[1])
        # Surface the actual max for log forensics — useful when the threshold
        # is approached but not breached.
        print(f"\n  [stability] top-12 max spread = {max_spread:.2f}pp ({max_team})")

    def test_top1_p_champion_in_bookmaker_band(self):
        # Independent sanity: the favorite's champion probability must be in
        # a realistic band. Spain at 25.6% in prod; with reduced sims expect
        # some variance, but a leader at >60% or <5% would indicate a
        # broken Elo/feature pipeline rather than seed noise.
        teams = sorted(self.payload["team_predictions"],
                       key=lambda t: -t["p_champion"])
        top1 = teams[0]["p_champion"]
        self.assertGreater(top1, 0.05,
                           f"top1 p_champion = {top1:.3f} < 5% — model collapse?")
        self.assertLess(top1, 0.60,
                        f"top1 p_champion = {top1:.3f} > 60% — overconfidence?")


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
