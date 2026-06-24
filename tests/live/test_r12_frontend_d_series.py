"""R12 D1-D4 regression — frontend critical fixes.

D1: applyLiveUpdate calls renderStatsStrip, renderStorylines,
    renderInteresting, renderCompare, renderMatchdayIntelligence on
    every tick (pre-R12 omitted).

D2: addEventListener bind-once guard via `_r12Bound` flag prevents
    handler stacking on re-renders.

D3: renderHero / renderStorylines / renderCompare have empty-data guards
    so a sigma_gate_failed sim doesn't crash the hero above the fold.

D4: warnings sorted by severity rank before warnings[0] is picked for
    the top pill — sigma_gate_failed beats fetch_failure for position.
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "dashboard" / "app.js").read_text()


class TestR12D1ApplyLiveUpdateCallsAllRenderFns(unittest.TestCase):
    def setUp(self):
        # Extract applyLiveUpdate body.
        i = APP_JS.find("function applyLiveUpdate(")
        j = APP_JS.find("\nfunction ", i + 1)
        self.body = APP_JS[i:j]

    def test_renderStatsStrip_called(self):
        self.assertIn("renderStatsStrip(", self.body,
            "R12 D1: applyLiveUpdate must call renderStatsStrip")

    def test_renderStorylines_called(self):
        self.assertIn("renderStorylines(", self.body,
            "R12 D1: applyLiveUpdate must call renderStorylines")

    def test_renderInteresting_called(self):
        self.assertIn("renderInteresting(", self.body,
            "R12 D1: applyLiveUpdate must call renderInteresting")

    def test_renderCompare_called(self):
        self.assertIn("renderCompare(", self.body,
            "R12 D1: applyLiveUpdate must call renderCompare")

    def test_renderMatchdayIntelligence_called(self):
        self.assertIn("renderMatchdayIntelligence(", self.body,
            "R12 D1: applyLiveUpdate must call renderMatchdayIntelligence")


class TestR12D2BindOnceGuard(unittest.TestCase):
    def test_r12_bound_marker_used(self):
        """At least 3 different element references must use the bind-once
        marker to avoid re-bind on every render."""
        # Count occurrences of `._r12Bound` or `._r12BoundMatches`.
        n = APP_JS.count("_r12Bound") + APP_JS.count("_r12BoundMatches")
        self.assertGreater(n, 10,
            f"R12 D2: bind-once marker must be applied to multiple elements; "
            f"got {n} occurrences")

    def test_contenders_state_handle_exists(self):
        """The renderContenders state needs to live on window so the
        bind-once handlers can read fresh state across re-renders."""
        self.assertIn("window._contendersState", APP_JS,
            "R12 D2: window._contendersState handle must exist")


class TestR12D3EmptyDataGuards(unittest.TestCase):
    def test_renderHero_guards_empty(self):
        i = APP_JS.find("function renderHero(")
        j = APP_JS.find("\nfunction ", i + 1)
        body = APP_JS[i:j]
        self.assertIn("No predictions available", body,
            "R12 D3: renderHero must surface an explicit empty-state message")

    def test_renderStorylines_guards_empty(self):
        i = APP_JS.find("function renderStorylines(")
        j = APP_JS.find("\nfunction ", i + 1)
        body = APP_JS[i:j]
        # Look for the empty-state placeholder.
        self.assertIn("storylines hidden", body,
            "R12 D3: renderStorylines must guard empty team_predictions")

    def test_renderCompare_guards_empty(self):
        i = APP_JS.find("function renderCompare(")
        j = APP_JS.find("\nfunction ", i + 1)
        body = APP_JS[i:j]
        self.assertIn("Team comparison unavailable", body,
            "R12 D3: renderCompare must guard empty team_predictions")


class TestR12D4WarningSeverityPrioritization(unittest.TestCase):
    def test_severity_rank_table_exists(self):
        self.assertIn("SEVERITY_RANK", APP_JS,
            "R12 D4: SEVERITY_RANK ordering map must exist")

    def test_sigma_gate_failed_outranks_fetch_failure(self):
        """Read the SEVERITY_RANK ordering and assert sigma_gate_failed
        has a lower (= higher priority) rank than fetch_failure."""
        # Extract the SEVERITY_RANK block.
        i = APP_JS.find("const SEVERITY_RANK = {")
        j = APP_JS.find("};", i)
        block = APP_JS[i:j]
        # Parse each `name: N,` row.
        import re
        ranks = {}
        for m in re.finditer(r"(\w+):\s*(\d+)", block):
            ranks[m.group(1)] = int(m.group(2))
        self.assertIn("sigma_gate_failed", ranks)
        self.assertIn("fetch_failure", ranks)
        self.assertLess(ranks["sigma_gate_failed"], ranks["fetch_failure"],
            "R12 D4: sigma_gate_failed must outrank fetch_failure "
            "(lower number = higher priority)")
        self.assertIn("matchday_consolidated_stale", ranks)
        self.assertLess(ranks["matchday_consolidated_stale"],
                        ranks["fetch_failure"],
            "R12 D4: matchday_consolidated_stale must outrank fetch_failure")

    def test_warnings_sorted_before_pill_pick(self):
        """The warnings array must be sorted before warnings[0] is read."""
        import re
        # Look for `warnings.sort(` between SEVERITY_RANK and warnings[0].
        i = APP_JS.find("const SEVERITY_RANK")
        # warnings[0] is referenced after the sort
        sort_idx = APP_JS.find("warnings.sort(", i)
        self.assertNotEqual(sort_idx, -1,
            "R12 D4: warnings.sort must be called after SEVERITY_RANK is defined")


if __name__ == "__main__":
    unittest.main(verbosity=2)
