"""R11 D2 regression — daily-baseline.yml Vercel deploy MUST surface
non-zero exit codes with `::error::` + `exit 1`.

Pre-R11 the deploy step used the pre-R4-G1 silent-failure pattern:
    set +e
    if [ -z "${VERCEL_TOKEN:-}" ]; then ... exit 0; fi
    npx ... vercel pull ...
    npx ... vercel deploy --prod ...
    exit 0

A revoked token, build error, network blip, or project-moved error
returned non-zero from the npx command, but the trailing `exit 0`
overrode it — the daily-baseline job went GREEN while the dashboard
silently stopped getting redeployed. Mirrors the silent-push-failure
class fixed by R4 G1 and R10 Q5 for the git push step.

R11 D2 captures rc with `|| rc=$?` style after each npx call and
surfaces failure with `::error::` + `exit 1`, preserving the
intentional missing-token skip clause.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WF = ROOT / ".github" / "workflows" / "daily-baseline.yml"


class TestR11D2VercelDeploySurfacing(unittest.TestCase):
    def setUp(self):
        self.src = WF.read_text()

    def test_silent_pattern_removed(self):
        """The pre-R11 pattern `npx ... vercel deploy ... && exit 0`
        without rc-capture MUST be gone. Specifically, no `exit 0` line
        may appear immediately after `vercel deploy` with no `if` /
        `||` / rc handling between them."""
        # Extract just the deploy-step block.
        m = re.search(
            r"name:\s*Deploy refreshed baseline to Vercel.*?(?=\n      - name:|\Z)",
            self.src, re.DOTALL,
        )
        self.assertIsNotNone(m, "deploy step block must exist")
        block = m.group(0)
        # The block must reference rc capture or `if ! vercel deploy`.
        self.assertTrue(
            "deploy_rc=$?" in block or "if ! npx" in block
            or "if [ \"$deploy_rc\"" in block,
            "R11 D2: deploy step must capture rc (deploy_rc=$?) or use "
            "if-block on the npx vercel deploy call"
        )

    def test_error_annotation_present(self):
        """`::error::` annotation must appear in the deploy step so a
        failed deploy lights up the Actions UI in red."""
        self.assertIn("::error::vercel deploy failed", self.src,
            "R11 D2: deploy step must emit ::error::vercel deploy failed")

    def test_exit_1_on_deploy_failure(self):
        """The error annotation MUST be paired with exit 1 (so the step
        actually fails), not just printed-then-exit-0."""
        # Look for ::error::vercel deploy ... followed by exit 1.
        self.assertRegex(
            self.src,
            r"::error::vercel deploy[^\n]*\n[\s\S]*?exit 1",
            "R11 D2: ::error:: must be followed by exit 1 to fail the step"
        )

    def test_pull_failure_also_surfaced(self):
        """vercel pull is the precondition for deploy — its failure
        deserves the same red signal."""
        self.assertIn("::error::vercel pull failed", self.src,
            "R11 D2: vercel pull failure must also surface as ::error::")

    def test_missing_token_skip_preserved(self):
        """The intentional missing-VERCEL_TOKEN skip clause must stay —
        production already runs in token-less local dev paths and going
        red on missing-token would be a false positive."""
        self.assertIn('if [ -z "${VERCEL_TOKEN:-}" ]', self.src)
        self.assertIn("VERCEL_TOKEN not configured", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
