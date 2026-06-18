"""R11 D1 regression — circuit breaker MUST persist across GHA ticks.

Pre-R11 `data/live/circuit_breaker_state.json` was:
  1. Gitignored at .gitignore:41
  2. NOT in the commit allow-list at .github/workflows/live-matchday.yml

Net effect: every GHA tick boots from `fetch-depth: 1` with no CB file
on disk. `read_circuit_breaker()` returns 0. CB_THRESHOLD=3 escalation
NEVER crosses a tick boundary — 6 consecutive sim_failures over 6
ticks produce 6 separate "1/3" warnings with no human-required halt.

R11 D1 closes both halves:
  - .gitignore: explicit `!data/live/circuit_breaker_state.json` un-ignore
  - live-matchday.yml commit allow-list: adds the CB path
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class TestR11D1CircuitBreakerPersistence(unittest.TestCase):
    def test_gitignore_explicitly_un_ignores_cb_file(self):
        gitignore = (ROOT / ".gitignore").read_text()
        # The bare "circuit_breaker_state.json" line stays so any other
        # location is still ignored, but the canonical data/live/ path
        # MUST have a "!" exception.
        self.assertIn(
            "!data/live/circuit_breaker_state.json",
            gitignore,
            "R11 D1: data/live/circuit_breaker_state.json must be "
            "explicitly UN-ignored so it persists across GHA ticks"
        )

    def test_live_matchday_commits_cb_file(self):
        yml = (ROOT / ".github" / "workflows" / "live-matchday.yml").read_text()
        self.assertIn(
            "data/live/circuit_breaker_state.json",
            yml,
            "R11 D1: live-matchday.yml commit allow-list must include "
            "circuit_breaker_state.json so CB_THRESHOLD escalates across "
            "ticks (pre-R11 every tick read 0 from a missing file)"
        )

    def test_legacy_bare_pattern_still_blocks_root_level(self):
        """The bare `circuit_breaker_state.json` line MUST stay so any
        root-level stray CB file (e.g. from old debug runs) doesn't get
        committed. Only the data/live/ path is allow-listed."""
        gitignore = (ROOT / ".gitignore").read_text()
        # Look for the bare line (allowing leading-non-bang character).
        lines = [l.strip() for l in gitignore.splitlines()]
        self.assertIn("circuit_breaker_state.json", lines,
            "R11 D1: root-level CB stray-files must still be ignored")


if __name__ == "__main__":
    unittest.main(verbosity=2)
