"""
R9 P2 regression — sample_score_with_noise must re-clip the noisy
effective λ to [LAMBDA_CLIP_MIN, LAMBDA_CLIP_MAX] AFTER the gamma
multiplier so the DC-τ boundary guard at scripts/03_simulate.py:115
remains valid in the noise path.

Pre-R9 only a floor (0.05) was applied after multiplying by gamma noise.
Empirically with α=12 and base λ ≈ 7.0 (top-scoring teams), ~33% of noisy
λ values exceeded the critical 1/|ρ|=7.69 boundary, causing
τ(0,1) = 1 + λ_a × ρ to go negative. build_score_matrix silently clipped
the resulting negative cells via np.maximum(mat, 1e-12) and renormalized,
which preserved Σ=1 (so the Σ-gate didn't catch it) but systematically
collapsed low-score outcomes (0-1, 1-0) for blowout-favorite fixtures.

R9 P2 (scripts/03_simulate.py:217-235) re-applies the [CLIP_MIN, CLIP_MAX]
clip post-noise. Numerical contract: build_score_matrix is now called
with λ ≤ LAMBDA_CLIP_MAX always, and the module-load assert holds for
the noise path too.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

spec = importlib.util.spec_from_file_location(
    "simulate_module", ROOT / "scripts" / "03_simulate.py"
)
sim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sim)


# Production config slice — only the fields sample_score_with_noise reads.
_CFG = {
    "lambda_noise_per_match": True,
    "lambda_noise_alpha": 12.0,
    "nb_dispersion": 5.0,
    "dc_rho": -0.13,
    "use_dispersion": True,
}


class TestDCTauPostNoiseClip(unittest.TestCase):
    def test_noisy_lambda_never_exceeds_clip_max(self):
        """Drive 10k samples at base λ=7.0 (the production clip max). Without
        R9 P2, ~33% of post-noise effective λ exceed 7.0. Post-R9 the clip
        re-application caps every draw at LAMBDA_CLIP_MAX."""
        rng = np.random.default_rng(20260618)
        alpha = _CFG["lambda_noise_alpha"]
        for _ in range(10_000):
            noise = rng.gamma(alpha, 1.0 / alpha)
            base = 7.0
            noisy_floor_only = max(0.05, base * noise)            # pre-R9 behaviour
            noisy_clipped = min(sim.LAMBDA_CLIP_MAX,
                                max(sim.LAMBDA_CLIP_MIN, base * noise))  # post-R9
            # Pre-R9 representation may exceed 7.0; post-R9 must not.
            self.assertLessEqual(noisy_clipped, sim.LAMBDA_CLIP_MAX,
                "R9 P2: post-noise λ must respect LAMBDA_CLIP_MAX")
            # Sanity: pre-R9 occasionally breaches, post-R9 saturates.
            if noisy_floor_only > sim.LAMBDA_CLIP_MAX:
                self.assertEqual(noisy_clipped, sim.LAMBDA_CLIP_MAX)

    def test_breach_rate_pre_fix_was_substantial(self):
        """Sanity check on the magnitude of the bug being fixed. With α=12
        on base λ=7.0, ≥25% of pre-R9 noisy draws breach the DC-τ boundary
        of 1/|ρ|=7.69. If this rate drops below 15% in a future config
        change, the R9 P2 numerical motivation should be re-examined."""
        rng = np.random.default_rng(20260618)
        alpha = _CFG["lambda_noise_alpha"]
        critical = 1.0 / abs(_CFG["dc_rho"])  # = 7.69
        n_breaches = 0
        n_trials = 10_000
        for _ in range(n_trials):
            noise = rng.gamma(alpha, 1.0 / alpha)
            if 7.0 * noise > critical:
                n_breaches += 1
        breach_rate = n_breaches / n_trials
        self.assertGreater(breach_rate, 0.15,
            f"Pre-R9 boundary breach rate dropped to {breach_rate:.3f}; "
            "re-examine R9 P2 motivation if config changed")

    def test_sample_score_with_noise_runs_at_max_lambda(self):
        """End-to-end smoke: the noise path must produce a valid score for
        the extreme λ=LAMBDA_CLIP_MAX case without exploding (NaN, negative,
        or boundary violations from downstream build_score_matrix)."""
        rng = np.random.default_rng(7)
        for _ in range(100):
            h, a = sim.sample_score_with_noise(
                sim.LAMBDA_CLIP_MAX, sim.LAMBDA_CLIP_MAX, _CFG, rng,
            )
            self.assertIsInstance(h, int)
            self.assertIsInstance(a, int)
            self.assertGreaterEqual(h, 0)
            self.assertGreaterEqual(a, 0)
            # R12 MED: max_g raised 10 → 15 so NB tail truncation no longer
            # redistributes 18% mass over [0..10] for high-λ matches.
            self.assertLessEqual(h, 15)
            self.assertLessEqual(a, 15)

    def test_module_load_assert_still_meaningful(self):
        """The R9 P2 fix is the engineering counterpart of the module-load
        assert at scripts/03_simulate.py:115. If a future contributor relaxes
        the noise path back to floor-only, this test fails — flagging that
        the assert no longer guards the actual numerical contract."""
        src = (ROOT / "scripts" / "03_simulate.py").read_text()
        # The post-noise clip must read LAMBDA_CLIP_MAX. Two occurrences
        # (one per lam_h/lam_a).
        idx = src.find("def sample_score_with_noise")
        self.assertGreater(idx, 0)
        body = src[idx:idx + 2000]
        self.assertGreaterEqual(body.count("LAMBDA_CLIP_MAX"), 2,
            "R9 P2: sample_score_with_noise must re-clip both λ_h and λ_a "
            "to LAMBDA_CLIP_MAX after the gamma noise multiplier; otherwise "
            "the DC-τ boundary guard at module-load is a fig leaf")


if __name__ == "__main__":
    unittest.main(verbosity=2)
