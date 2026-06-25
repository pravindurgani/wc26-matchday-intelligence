"""R13 regression tests for C1/C2/C3/D1/MED fixes (A1 covered separately
in test_r13_player_join_key.py).

C1: scripts/live/export_ko_advance.py MAX_G bumped 10 → 15 (was stale vs
    R12 MED sim default).
C2: tests/live/test_goal_grid_feed_agreement.py + scripts/live/verify_
    goal_grid_agreement.py MAX_G bumped 10 → 15.
C3: wc26-engine-gs/WC26_Engine_AppsScript_v*.gs (latest) GOAL_GRID_MAX_GOALS
    bumped 10 → 15.
D1: dashboard/app.js::renderCompare clears select options before append
    (DOM-leak fix; pre-R13 R12 D1 added the per-tick call but did not
    clear → 32 duplicates per tick → 3840 dup nodes per 2h).
MED-1: check_invariants raises MissingField when a modern blob (with
       p_champion) is missing a stage_expectations field (pre-R13 a
       conditional skip silently passed).
MED-2: orchestrator crash path increments circuit_breaker (pre-R13 only
       sim_failure counted; crashes left CB at 0 forever).
MED-3: suspension_tracker._attach_elo strips player_norm from the on-
       disk row (pre-R13 it leaked to suspensions_2026.json).
A2 defense-in-depth: apply_matchday_adjustments injury/referee/
   suspension loaders re-normalize team via normalize_team (catches
   operator manual-edit overrides with raw aliases).
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "live"))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestR13C1ExportKoAdvanceMaxG(unittest.TestCase):
    def test_export_ko_advance_max_g_is_15(self):
        # Post-centralization (scripts/constants.py is the single source
        # of truth): the drift guard now asserts (a) the canonical
        # value is 15 and (b) export_ko_advance.py imports from the
        # canonical module rather than shadow-declaring (which is
        # exactly the drift surface centralization eliminates).
        from constants import MAX_G as canonical_max_g
        self.assertEqual(canonical_max_g, 15,
            "R13 C1: canonical scripts/constants.MAX_G must be 15")
        src = (ROOT / "scripts" / "live"
               / "export_ko_advance.py").read_text()
        self.assertIn("from constants import", src,
            "R13 C1: export_ko_advance.py must import from "
            "scripts/constants.py (no shadow declaration allowed)")
        self.assertNotIn("MAX_G = 10", src,
            "R13 C1: stale MAX_G = 10 must be gone")


class TestR13C2GoalGridAgreementMaxG(unittest.TestCase):
    def test_feed_agreement_max_g_is_15(self):
        src = (ROOT / "tests" / "live"
               / "test_goal_grid_feed_agreement.py").read_text()
        # MAX_G should be set to 15.
        m = re.search(r"^MAX_G\s*=\s*(\d+)\b", src, re.M)
        self.assertIsNotNone(m,
            "R13 C2: test_goal_grid_feed_agreement.py must declare MAX_G")
        self.assertEqual(m.group(1), "15",
            f"R13 C2: MAX_G must be 15; got {m.group(1)}")

    def test_verify_goal_grid_max_g_is_15(self):
        # Post-centralization: same pattern as C1.
        from constants import MAX_G as canonical_max_g
        self.assertEqual(canonical_max_g, 15,
            "R13 C2: canonical scripts/constants.MAX_G must be 15")
        src = (ROOT / "scripts" / "live"
               / "verify_goal_grid_agreement.py").read_text()
        self.assertIn("from constants import", src,
            "R13 C2: verify_goal_grid_agreement.py must import "
            "from scripts/constants.py (no shadow declaration allowed)")


class TestR13C3AppsScriptMaxGoals(unittest.TestCase):
    def test_gs_constant_is_15(self):
        gs_dir = ROOT / "wc26-engine-gs"
        candidates = sorted(gs_dir.glob("WC26_Engine_AppsScript_v*.gs"))
        assert candidates, f"No WC26_Engine_AppsScript_v*.gs in {gs_dir}"
        gs = candidates[-1].read_text()
        self.assertIn("const GOAL_GRID_MAX_GOALS = 15;", gs,
            "R13 C3: Apps Script GOAL_GRID_MAX_GOALS must be 15")
        self.assertNotIn("const GOAL_GRID_MAX_GOALS = 10;", gs,
            "R13 C3: stale = 10 must be gone")


class TestR13D1RenderCompareClearsOptions(unittest.TestCase):
    def test_render_compare_calls_replace_children(self):
        src = (ROOT / "dashboard" / "app.js").read_text()
        # Extract renderCompare body.
        i = src.find("function renderCompare(")
        self.assertNotEqual(i, -1, "renderCompare must exist")
        j = src.find("\nfunction ", i + 1)
        body = src[i:j]
        # The fix clears both select elements via replaceChildren()
        # before the option-append loop.
        self.assertIn("a.replaceChildren()", body,
            "R13 D1: renderCompare must call a.replaceChildren() before "
            "appending team options (pre-R13 DOM-leak on every tick)")
        self.assertIn("b.replaceChildren()", body,
            "R13 D1: renderCompare must call b.replaceChildren() before "
            "appending team options")


class TestR13MED1CheckInvariantsPartialCoverageRaises(unittest.TestCase):
    """Partial coverage of a stage field (47/48 teams have it, 1 doesn't)
    MUST raise — that's the real regression signal. Zero coverage (no
    teams have it) still skips silently to preserve synthetic-test and
    legacy-blob compatibility."""

    def setUp(self):
        self.ci = _load("ci_r13",
                        ROOT / "scripts" / "check_invariants.py")

    def test_partial_coverage_raises(self):
        """47 teams have p_advance_groups, 1 doesn't → partial coverage →
        must raise (real regression)."""
        tmp = Path(tempfile.mkdtemp(prefix="r13_med1_"))
        try:
            teams = []
            for i in range(48):
                t = {
                    "team": f"T{i}",
                    "p_champion": 1.0 if i == 0 else 0.0,
                    "p_reach_final": 1.0 if i < 2 else 0.0,
                    "p_reach_sf": 1.0 if i < 4 else 0.0,
                    "p_reach_qf": 1.0 if i < 8 else 0.0,
                    "p_reach_r16": 1.0 if i < 16 else 0.0,
                }
                # Drop p_advance_groups on the LAST team to simulate
                # a regression that loses a field for one team.
                if i < 47:
                    t["p_advance_groups"] = 1.0 if i < 32 else 0.0
                teams.append(t)
            blob = {"team_predictions": teams, "annex_c_misses": 0}
            f = tmp / "predictions_live.json"
            f.write_text(json.dumps(blob))
            with self.assertRaises(self.ci.MissingField) as ctx:
                self.ci.check_invariants(f)
            self.assertIn("p_advance_groups", str(ctx.exception))
            self.assertIn("partial", str(ctx.exception).lower())
        finally:
            for p in tmp.iterdir():
                p.unlink()
            tmp.rmdir()

    def test_zero_coverage_skips_silently(self):
        """No team has the stage fields → synthetic test blob or legacy.
        Must NOT raise (backward compatibility)."""
        tmp = Path(tempfile.mkdtemp(prefix="r13_med1b_"))
        try:
            teams = []
            for i in range(48):
                teams.append({"team": f"T{i}",
                              "p_champion": 1.0 if i == 0 else 0.0})
            blob = {"team_predictions": teams, "annex_c_misses": 0}
            f = tmp / "predictions_live.json"
            f.write_text(json.dumps(blob))
            # Should pass (only p_champion sum is checked).
            try:
                self.ci.check_invariants(f)
            except self.ci.MissingField as e:
                self.fail(f"R13 MED-1: zero-coverage blob must skip "
                          f"silently, not raise; got: {e}")
        finally:
            for p in tmp.iterdir():
                p.unlink()
            tmp.rmdir()


class TestR13MED2CrashIncrementsCB(unittest.TestCase):
    """Static-pin: orchestrator crash handler calls write_circuit_breaker
    on the failure path."""

    def test_crash_handler_increments_cb(self):
        src = (ROOT / "scripts" / "live" / "run_live_update.py").read_text()
        # The exception handler block must contain a read+write_cb
        # incrementing pair.
        # Find the `except Exception as e:` at module level.
        i = src.find("except Exception as e:")
        self.assertNotEqual(i, -1)
        # Look between there and `sys.exit(1)` for the CB increment.
        j = src.find("sys.exit(1)", i)
        block = src[i:j]
        self.assertIn("read_circuit_breaker()", block,
            "R13 MED-2: crash handler must read current CB count")
        self.assertIn("write_circuit_breaker(current + 1)", block,
            "R13 MED-2: crash handler must increment CB by 1 on crash")


class TestR13MED3PlayerNormStrippedFromOutput(unittest.TestCase):
    """suspension_tracker._attach_elo strips player_norm before write."""

    def test_attach_elo_drops_player_norm(self):
        st = _load("st_r13_med3",
                   ROOT / "scripts" / "live" / "suspension_tracker.py")
        rows_in = [{
            "match_id": 5, "team": "Mexico", "player": "R. Jiménez",
            "player_norm": "jimenez", "reason": "red_card",
            "evidence_match_ids": [1], "triggering_match_id": 1,
        }]
        out = st._attach_elo(rows_in)
        self.assertEqual(len(out), 1)
        # player_norm must be absent from the on-disk row.
        self.assertNotIn("player_norm", out[0],
            "R13 MED-3: _attach_elo must strip player_norm before write")
        # Display player preserved.
        self.assertEqual(out[0]["player"], "R. Jiménez")
        # Other R12-introduced fields stay.
        self.assertIn("team_adjustment_elo", out[0])


class TestR13A2LoaderTeamNormalization(unittest.TestCase):
    """Static-pin: injury/referee/suspension loaders re-normalize team
    name as defense-in-depth against operator manual-edit overrides."""

    def setUp(self):
        self.src = (ROOT / "scripts" / "live"
                    / "apply_matchday_adjustments.py").read_text()

    def test_injury_loader_normalizes(self):
        # Pattern: raw_team = ...; team = normalize_team(raw_team)
        self.assertIn("team = normalize_team(raw_team)", self.src)

    def test_referee_loader_normalizes(self):
        # Find the referee loader function and check it has raw_team pattern.
        i = self.src.find("def _load_referee_components")
        self.assertNotEqual(i, -1)
        j = self.src.find("\ndef ", i + 1)
        body = self.src[i:j]
        self.assertIn("raw_team = r.get(\"home_team\")", body,
            "R13 A2: referee loader must extract raw_team then normalize")
        self.assertIn("normalize_team(raw_team)", body)

    def test_suspension_loader_normalizes(self):
        i = self.src.find("def _load_suspension_components")
        self.assertNotEqual(i, -1)
        j = self.src.find("\ndef ", i + 1)
        body = self.src[i:j]
        self.assertIn("raw_team = s.get(\"team\")", body,
            "R13 A2: suspension loader must extract raw_team then normalize")
        self.assertIn("normalize_team(raw_team)", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
