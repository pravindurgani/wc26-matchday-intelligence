"""
R9 P4 A2 regression — fetch_results.py must include the knockout bracket
in its schedule_by_id so KO match IDs (m=73..104) flow through the
provider_fixture_map.json lookup path.

Pre-R9 only `cfg["group_stage_schedule"]` was loaded (72 rows). Any KO
fixture_id that the provider returned and that DID map to m_id ∈ [73,104]
in provider_fixture_map.json then hit `sched = schedule_by_id.get(m_id)`
which returned None, and the fixture was dropped into `unmapped` with
reason="m not in schedule". Net result: entire knockout phase invisible
to fetch_results — `completed_matches` frozen at 72, dashboard locked
at end-of-groups state through R32 → Final.

Also pins the KO-window critical warning: any unmapped fixture with
date >= 2026-06-28 must produce a "rebuild provider_fixture_map.json"
stderr alert so operators know to act.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))


def _load_fetch_results():
    """Import fetch_results without triggering the package-relative chain."""
    spec = importlib.util.spec_from_file_location(
        "fetch_results_module", ROOT / "scripts" / "live" / "fetch_results.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestKnockoutScheduleExtension(unittest.TestCase):
    def test_load_knockout_fixtures_returns_thirty_two(self):
        """The KO bracket loader must surface all 32 KO entries
        (16 R32 + 8 R16 + 4 QF + 2 SF + 1 3rd + 1 final). Anything else
        means the bracket file or loader is broken — fetch_results KO
        path inherits the breakage."""
        import _knockout
        rows = _knockout.load_knockout_fixtures()
        self.assertEqual(len(rows), 32,
            f"Expected 32 KO fixtures, got {len(rows)}; "
            f"fetch_results A2 closure will leak silently")
        ms = sorted(r["m"] for r in rows)
        self.assertEqual(ms[0], 73)
        self.assertEqual(ms[-1], 104)

    def test_fetch_results_imports_knockout_loader(self):
        """Static pin: the R9 P4 import must exist so the extension lines
        further down can call load_knockout_fixtures(). A revert would
        silently break the KO mapping path again."""
        src = (ROOT / "scripts" / "live" / "fetch_results.py").read_text()
        self.assertIn("from scripts.live._knockout import load_knockout_fixtures", src,
            "R9 P4 A2: fetch_results must import load_knockout_fixtures so "
            "the schedule_by_id extension at the apifootball + football_data "
            "adapters has the KO bracket available")

    def test_fetch_results_extends_schedule_with_ko_in_both_adapters(self):
        """Both fetch_apifootball() and fetch_football_data() must extend
        schedule_by_id to include KO entries. A revert in either path
        produces silent KO-result drops for that provider."""
        src = (ROOT / "scripts" / "live" / "fetch_results.py").read_text()
        # The R9 P4 A2 closure adds these two lines per adapter — at least
        # 2 calls to load_knockout_fixtures() in the file.
        self.assertGreaterEqual(src.count("load_knockout_fixtures()"), 2,
            "R9 P4 A2: load_knockout_fixtures() must be invoked in BOTH "
            "fetch_apifootball and fetch_football_data; otherwise the "
            "fallback adapter silently drops KO results")

    def test_fetch_results_ko_window_warning_pinned(self):
        """The CRITICAL warning for unmapped KO-window fixtures must be
        present in both adapters. Without this, an unmapped KO fixture
        gets lumped with friendlies in the routine info-level message."""
        src = (ROOT / "scripts" / "live" / "fetch_results.py").read_text()
        self.assertGreaterEqual(src.count("KO-unmapped"), 2,
            "R9 P4 A2: both adapters must surface the CRITICAL KO-window "
            "warning so unmapped post-2026-06-27 fixtures don't silently "
            "drop")
        self.assertIn("Rebuild provider_fixture_map.json", src,
            "R9 P4 A2: KO-window unmapped warning must name the operator "
            "remediation (rebuild provider_fixture_map.json)")

    def test_ko_match_uses_provider_team_names_not_slot_codes(self):
        """Static pin: the result-emission block for KO matches must use
        the local `home`/`away` variables (provider-normalised team names),
        NOT sched["home"]/sched["away"] which carry bracket slot codes
        ("1A", "W74") for KO fixtures."""
        src = (ROOT / "scripts" / "live" / "fetch_results.py").read_text()
        # Look for the `is_ko_match` branch — at least 2 occurrences
        # (apifootball + football_data adapters).
        self.assertGreaterEqual(src.count("is_ko_match"), 2,
            "R9 P4 A2: both adapters must distinguish KO from group "
            "matches at the result-emission step so KO rows don't get "
            "slot codes ('1A','W74') written as home/away team names")
        # And the conditional must appear in a result-build context.
        self.assertIn('"home": home if is_ko_match else sched["home"]', src)
        self.assertIn('"away": away if is_ko_match else sched["away"]', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
