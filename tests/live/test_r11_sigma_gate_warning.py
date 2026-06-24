"""R11 D3 regression — run_live_update.py MUST capture
`scripts/09_validate.py`'s return code and surface a sigma_gate_failed
warning on non-zero exit.

Pre-R11 line 737 was `run([sys.executable, "scripts/09_validate.py"])`
with the return value DISCARDED. If the R10 Q3 strict 1e-6 Σ-gate
failed on a LIVE tick, the corrupt predictions_live.json was already
published in Step 7 (lines 720-734). The dashboard rendered invariant-
violating data with ZERO operator signal.

R11 D3 captures rc and re-writes live_state.json with a
sigma_gate_failed warning appended so the top pill surfaces the
failure even though the bad publish already shipped (next tick re-
runs validate against the next sim and converges).
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class TestR11D3SigmaGateWarning(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / "scripts" / "live" / "run_live_update.py").read_text()

    def test_validate_rc_captured(self):
        """Static pin: rc must be captured from the 09_validate.py run."""
        self.assertRegex(
            self.src,
            r"validate_rc\s*=\s*run\(\[sys\.executable,\s*\"scripts/09_validate\.py\"\]\)",
            "R11 D3: scripts/09_validate.py rc must be captured into "
            "validate_rc, not discarded"
        )

    def test_sigma_gate_failed_warning_emitted(self):
        """The non-zero branch must append a sigma_gate_failed warning
        and re-write live_state.json so the operator sees the failure."""
        self.assertIn("sigma_gate_failed", self.src,
            "R11 D3: must emit a sigma_gate_failed warning type")
        self.assertRegex(
            self.src,
            r"if validate_rc != 0:",
            "R11 D3: must branch on validate_rc != 0"
        )

    def test_live_state_rewritten_with_warning(self):
        """write_live_state must be called again with the warning
        appended (warns_with_gate pattern)."""
        # The re-write happens inside the validate_rc != 0 branch.
        block_m = re.search(
            r"if validate_rc != 0:.*?print\(f?\"\[run_live_update\] DONE",
            self.src, re.DOTALL,
        )
        self.assertIsNotNone(block_m,
            "R11 D3: the validate_rc != 0 block must precede the DONE log")
        block = block_m.group(0)
        self.assertIn("write_live_state", block,
            "R11 D3: must call write_live_state with the warning appended")


if __name__ == "__main__":
    unittest.main(verbosity=2)
