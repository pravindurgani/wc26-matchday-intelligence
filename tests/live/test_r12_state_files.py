"""R12 E1 + E2 regression — state files seeded on disk + tracked in git.

E1: data/live/circuit_breaker_state.json must exist on disk with seeded
    {consecutive_failures: 0, last_updated: ..., threshold: 3} so the
    R11 D1 commit allow-list actually has a file to add. Pre-R12 the
    file never existed; `git add ... 2>/dev/null` silently swallowed the
    missing-file error every tick.

E2: data/live/live_team_state.json must include `last_updated` field so
    compute_input_hash sees a real timestamp instead of empty string.
"""
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class TestR12E1CircuitBreakerStateSeeded(unittest.TestCase):
    def test_file_exists_on_disk(self):
        cb = ROOT / "data" / "live" / "circuit_breaker_state.json"
        self.assertTrue(cb.exists(),
            "R12 E1: data/live/circuit_breaker_state.json must exist so "
            "the R11 D1 commit allow-list has a file to add (pre-R12 the "
            "git add 2>/dev/null silently swallowed missing-file errors)")

    def test_file_schema_correct(self):
        cb = ROOT / "data" / "live" / "circuit_breaker_state.json"
        blob = json.loads(cb.read_text())
        self.assertIn("consecutive_failures", blob,
            "R12 E1: CB state must include consecutive_failures")
        self.assertIn("last_updated", blob,
            "R12 E1: CB state must include last_updated")
        self.assertIn("threshold", blob,
            "R12 E1: CB state must include threshold")
        # Reasonable seed values.
        self.assertEqual(blob["consecutive_failures"], 0)
        self.assertEqual(blob["threshold"], 3)

    def test_file_is_trackable_by_git(self):
        """The .gitignore !data/live/circuit_breaker_state.json un-ignore
        line means git check-ignore must NOT mark this file as ignored."""
        cb_rel = "data/live/circuit_breaker_state.json"
        rc = subprocess.run(
            ["git", "check-ignore", cb_rel],
            cwd=ROOT, capture_output=True
        ).returncode
        # check-ignore: 0 = ignored, 1 = NOT ignored. We want 1.
        self.assertEqual(rc, 1,
            "R12 E1: CB file must NOT be gitignored "
            "(.gitignore needs !data/live/circuit_breaker_state.json)")


class TestR12E2LiveTeamStateHasLastUpdated(unittest.TestCase):
    def test_live_team_state_has_last_updated(self):
        lts = ROOT / "data" / "live" / "live_team_state.json"
        if not lts.exists():
            self.skipTest("live_team_state.json not present")
        blob = json.loads(lts.read_text())
        self.assertIn("last_updated", blob,
            "R12 E2: live_team_state.json must include last_updated so "
            "compute_input_hash sees a real timestamp instead of '' default")

    def test_last_updated_parseable_iso(self):
        lts = ROOT / "data" / "live" / "live_team_state.json"
        if not lts.exists():
            self.skipTest("live_team_state.json not present")
        blob = json.loads(lts.read_text())
        from datetime import datetime
        # Must parse as ISO-8601.
        ts = blob.get("last_updated")
        if ts:
            datetime.fromisoformat(ts.replace("Z", "+00:00"))  # raises if bad


if __name__ == "__main__":
    unittest.main(verbosity=2)
