"""
R10 Q5 regression — `.github/workflows/daily-baseline.yml` must surface
git-push failures with `::error::` + non-zero exit, mirroring the R4 G1
hardening already applied to live-matchday.yml and matchday-intel-slow.yml.

Pre-R10 daily-baseline.yml used the pre-R4 silent-failure pattern:
    git push origin "HEAD:$BRANCH" || echo "Push failed — next daily run will retry."
    exit 0
Result: a token expiry, branch-protection rule, or force-push race would
mark the daily run GREEN while the freshly-retrained model artifacts
(home_goals_model.joblib, away_goals_model.joblib, feature_cols_v2.json,
metrics_v2.json, walk_forward.json, ablation.json, sensitivity.json,
evaluation.json + dashboard/predictions.json + processed/predictions.json)
silently failed to land. Next-day fast-workflow tick then ran the OLD
committed model with no operator signal — exactly the silent-model-drift
class R4 G1 was created to prevent.

R10 Q5 swaps in the `if ! git push ...; then echo "::error::..."; exit 1; fi`
pattern. Static pin so a future "simplify the workflow" PR can't revert.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WF = ROOT / ".github" / "workflows"


class TestDailyBaselinePushSurfacing(unittest.TestCase):
    def setUp(self):
        self.daily = (WF / "daily-baseline.yml").read_text()

    def test_daily_baseline_no_longer_swallows_push_failure(self):
        """The pre-R4-G1 silent-failure pattern must be gone."""
        self.assertNotIn(
            'git push origin "HEAD:$BRANCH" || echo "Push failed — next daily run will retry."',
            self.daily,
            "R10 Q5 (C4): daily-baseline.yml must NOT use the pre-R4-G1 "
            "swallowed-push pattern; mirror the live-matchday.yml fix"
        )

    def test_daily_baseline_uses_error_annotation_and_nonzero_exit(self):
        """The R4 G1 pattern: if-block on git push, ::error:: annotation,
        exit 1 on failure."""
        self.assertRegex(
            self.daily,
            r"if\s+!\s+git\s+push\s+origin",
            "R10 Q5 (C4): daily-baseline.yml must check `if ! git push`",
        )
        self.assertIn("::error::", self.daily,
            "R10 Q5 (C4): daily-baseline.yml must surface push failure "
            "with GHA ::error:: annotation so Actions UI lights up red")
        # Look for `exit 1` near the error annotation.
        m = re.search(r"::error::[^\n]*\n[^\n]*exit\s+1", self.daily)
        self.assertIsNotNone(m,
            "R10 Q5 (C4): the ::error:: line must be followed by `exit 1` "
            "so the job goes red, not the legacy `exit 0`")

    def test_daily_baseline_parity_with_live_matchday(self):
        """Both workflows should share the same push-failure structure.
        live-matchday.yml is the R4 G1 reference."""
        live = (WF / "live-matchday.yml").read_text()
        # Both must have the if-block + error pattern.
        for label, src in (("daily-baseline.yml", self.daily),
                           ("live-matchday.yml", live)):
            self.assertRegex(src, r"if\s+!\s+git\s+push\s+origin",
                f"{label}: missing if-block push-fail handler")
            self.assertIn("::error::git push", src,
                f"{label}: missing ::error::git push surfacing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
