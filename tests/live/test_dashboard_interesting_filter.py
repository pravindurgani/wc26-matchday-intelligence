"""
R10 Q1 regression — dashboard/app.js renderInteresting must filter out
past group matches so the "Closest match" / "Most likely draw" / "Biggest
upset potential" cards don't continue surfacing finished group games
through the KO phase.

Pre-R10 the filter only checked `(stage||'group')==='group' && typeof
p_home_win === 'number'`. Locked group matches retain their full
probability fields in predictions_live.json (verified: 72/72 group rows
carry p_home_win even when scored). After 2026-06-27, ALL 72 group rows
have `date < today`, so the section would freeze at pre-tournament picks
for the entire ~22-day knockout phase.

R10 Q1 (dashboard/app.js renderInteresting) adds `m.date >= todayIso`
to the filter AND a guard for empty result set so the cards array
builders (which dereference `closest.p_home_win` etc.) don't crash on
the post-group-stage state.

This is a JS source pin — we can't run dashboard JS in CI without a
headless browser, so we pin the load-bearing literals to catch a
revert.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_JS = ROOT / "dashboard" / "app.js"


class TestRenderInterestingDateFilter(unittest.TestCase):
    def setUp(self):
        self.src = APP_JS.read_text()

    def test_render_interesting_filter_includes_date_guard(self):
        """The pre-cards filter must reference todayIso AND m.date — both
        load-bearing for the post-group-stage no-stale-cards contract."""
        # Find the renderInteresting body.
        m = re.search(
            r"function renderInteresting\(data\)\s*\{(.*?)\n\}", self.src, re.DOTALL,
        )
        self.assertIsNotNone(m, "renderInteresting function not found")
        body = m.group(1)
        self.assertIn("todayIso", body,
            "R10 Q1: renderInteresting must define a todayIso constant "
            "for the date filter")
        self.assertIn("toISOString()", body,
            "R10 Q1: todayIso should come from a real Date() — using "
            "literal date strings would freeze at deploy time")
        self.assertIn("m.date", body,
            "R10 Q1: filter must reference m.date so past matches are "
            "excluded; otherwise stale group cards surface through the "
            "entire KO phase")

    def test_render_interesting_empty_guard_exists(self):
        """When ms is empty (post-group-stage), the cards-array builders
        below dereference closest.p_home_win etc. and would TypeError.
        R10 Q1 must guard with `if (ms.length === 0)` before that."""
        m = re.search(
            r"function renderInteresting\(data\)\s*\{(.*?)\n\}", self.src, re.DOTALL,
        )
        body = m.group(1)
        self.assertIn("ms.length === 0", body,
            "R10 Q1: renderInteresting must guard the empty-ms case so "
            "the cards array builders don't TypeError on closest.p_home_win "
            "when no upcoming group matches exist")

    def test_render_interesting_filter_uses_gte_today(self):
        """Use `>= todayIso` (not just `> todayIso`) so today's still-
        upcoming matches stay in the pool."""
        m = re.search(
            r"function renderInteresting\(data\)\s*\{(.*?)\n\}", self.src, re.DOTALL,
        )
        body = m.group(1)
        self.assertIn(">= todayIso", body,
            "R10 Q1: filter should use >= todayIso so today's matches "
            "stay visible (only strictly past matches are excluded)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
