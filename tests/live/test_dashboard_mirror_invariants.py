"""
R10 Q3 regression — `scripts/09_validate.py` must run the strict Σ-gate
on BOTH the canonical `data/processed/predictions_live.json` AND the
deployed `dashboard/predictions_live.json` mirror.

Pre-R10 only the canonical was gated. The workflow at
.github/workflows/live-matchday.yml:247-253 commits the DASHBOARD copy
to git (canonical stays in working tree per the "load-bearing
--autostash" comment at lines 270-279), so the actually-shipped artifact
served via Vercel was never explicitly checked. A copy-path corruption,
filesystem race, or accidental hand-edit of the dashboard mirror would
have published invariant-violating p_champion to users without any
operator signal.

R10 Q3 (`scripts/09_validate.py` section 2c) adds a second
`_check_strict_invariants(DASH / "predictions_live.json")` call. The
canonical and dashboard mirrors SHOULD agree byte-for-byte after each
run_live_update.py tick (parse-check + raw-byte copy), so a divergence
or invariant failure on the mirror is itself a signal — either the copy
path is broken, or something post-publish has corrupted the file.

Also verifies the dashboard mirror passes today's invariants on real data.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

spec = importlib.util.spec_from_file_location(
    "check_invariants_module", ROOT / "scripts" / "check_invariants.py"
)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


class TestDashboardMirrorInvariants(unittest.TestCase):
    def test_validate_script_gates_dashboard_mirror(self):
        """Static pin: 09_validate.py must invoke check_invariants on the
        DASH / predictions_live.json path. Pre-R10 only the canonical was
        gated; R10 Q3 added the dashboard mirror as section 2c."""
        src = (ROOT / "scripts" / "09_validate.py").read_text()
        # The R10 Q3 closure introduces a call against the dashboard path.
        self.assertIn('DASH / "predictions_live.json"', src,
            "R10 Q3: 09_validate.py must validate the dashboard mirror")
        self.assertIn("dashboard mirror", src,
            "R10 Q3: 09_validate.py must label the dashboard-mirror gate "
            "for operator visibility")
        # Two _check_strict_invariants calls: one for canonical, one for mirror.
        self.assertGreaterEqual(src.count("_check_strict_invariants("), 2,
            "R10 Q3: 09_validate.py must invoke check_invariants TWICE — "
            "once on canonical, once on dashboard mirror")

    def test_dashboard_mirror_passes_today_invariants(self):
        """Real-data check: the shipped dashboard/predictions_live.json
        currently in the repo must pass the strict Σ-gate. If this fails,
        a future tick has published invariant-violating numbers and
        operators need to know."""
        dash_live = ROOT / "dashboard" / "predictions_live.json"
        if not dash_live.exists():
            self.skipTest("dashboard/predictions_live.json not present in repo")
        # Must not raise.
        try:
            ci.check_invariants(dash_live)
        except ci.InvariantError as e:
            self.fail(
                f"R10 Q3: dashboard/predictions_live.json fails strict "
                f"Σ-gate today: {type(e).__name__}: {e}. The shipped "
                f"artifact has drifted from invariants — investigate "
                f"the publish path."
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
