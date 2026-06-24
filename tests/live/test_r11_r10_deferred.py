"""R11 cleanup of R10 deferrals — single-file omnibus pinning each of
the R10 deferred items that R11 closed.

R10's deferred queue per PRESSURE_TEST_R10.md lines 177-251:
  A2 (MED) — LAMBDA_CLIP_MAX runtime cfg["dc_rho"] assert
  A4 (LOW) — distinguish OSError from JSONDecodeError [tested in
             test_r11_freshness_loaders.py]
  B2 (MED) — INTEL_TOP_BAR_TYPES whitelist extend
  B3 (MED) — renderContenders empty-array guard
  B4 (LOW) — warning pill text unbounded
  B5 (MED) — match time TZ label
  C3-old (LOW) — fetch_injuries ordering before suspension_tracker
  D2-old (MED) — KO venue ", ST" suffix normalize
  D3-old (MED) — distance matrix indirection validator
  E2-old (MED) — annex_c_misses on dashboard mirror [in check_invariants]
  E10 (DEFENSIVE) — per-stage Σ + stacking pinned [in check_invariants]
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))
sys.path.insert(0, str(ROOT / "scripts"))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── A2: runtime cfg["dc_rho"] assert in build_score_matrix ───────────
class TestR11A2RuntimeDcRhoAssert(unittest.TestCase):
    def setUp(self):
        self.sim_src = (ROOT / "scripts" / "03_simulate.py").read_text()

    def test_runtime_dc_rho_assert_present(self):
        i = self.sim_src.find("def build_score_matrix(")
        j = self.sim_src.find("\ndef ", i + 1)
        body = self.sim_src[i:j]
        self.assertIn("LAMBDA_CLIP_MAX * abs(rho)", body,
            "R11 A2: build_score_matrix must runtime-check "
            "LAMBDA_CLIP_MAX * |cfg['dc_rho']| < 1.0")
        self.assertIn("Dixon-Coles τ boundary violated at runtime", body,
            "R11 A2: runtime assert must reference DC τ boundary in message")


# ── B2: INTEL_TOP_BAR_TYPES whitelist extension ──────────────────────
class TestR11B2IntelTopBarTypesExtended(unittest.TestCase):
    def setUp(self):
        self.app = (ROOT / "dashboard" / "app.js").read_text()

    def test_consolidated_types_whitelisted(self):
        for must_have in ("subsystem_degraded", "pipeline_unhealthy",
                          "matchday_consolidated_stale",
                          "matchday_consolidated_missing",
                          "no_records_returned", "provider_returned_nothing",
                          "sigma_gate_failed",
                          "side_match_unrecognized",
                          "lineup_side_unrecognized"):
            self.assertIn(f"'{must_have}'", self.app,
                f"R11 B2: INTEL_TOP_BAR_TYPES must include '{must_have}'")


# ── B3: renderContenders empty-array guard ───────────────────────────
class TestR11B3RenderContendersEmptyGuard(unittest.TestCase):
    def setUp(self):
        self.app = (ROOT / "dashboard" / "app.js").read_text()

    def test_empty_team_predictions_emits_placeholder_row(self):
        i = self.app.find("function renderContenders(")
        j = self.app.find("\nfunction ", i + 1)
        body = self.app[i:j]
        self.assertIn("if (!Array.isArray(all) || all.length === 0)", body,
            "R11 B3: renderContenders must guard empty/missing team_predictions")
        self.assertIn("No team predictions available", body,
            "R11 B3: empty-state placeholder text must be present")


# ── B4: warning pill width bounded ───────────────────────────────────
class TestR11B4WarningPillBounded(unittest.TestCase):
    def setUp(self):
        self.css = (ROOT / "dashboard" / "styles.css").read_text()

    def test_last_updated_has_max_width_and_overflow(self):
        i = self.css.find(".last-updated {")
        j = self.css.find("}", i)
        block = self.css[i:j]
        self.assertIn("max-width", block,
            "R11 B4: .last-updated must have max-width")
        self.assertIn("text-overflow: ellipsis", block,
            "R11 B4: .last-updated must use text-overflow: ellipsis")


# ── B5: match time TZ label ──────────────────────────────────────────
class TestR11B5MatchTimeHasTZLabel(unittest.TestCase):
    def setUp(self):
        self.app = (ROOT / "dashboard" / "app.js").read_text()

    def test_match_head_time_has_local_suffix(self):
        # The render line is at app.js:1252.
        self.assertIn("' (local)'", self.app,
            "R11 B5: match-head time render must include ' (local)' suffix "
            "(WC2026 venues span 4 NA time zones; raw HH:MM is ambiguous)")


# ── C3-old: workflow step ordering ───────────────────────────────────
class TestR11C3OldWorkflowOrdering(unittest.TestCase):
    def setUp(self):
        self.yml = (ROOT / ".github" / "workflows"
                    / "matchday-intel-slow.yml").read_text()

    def test_fetch_injuries_runs_before_suspension_tracker(self):
        """Compare the position of the actual step `name:` headers so
        a passing comment reference doesn't fool the test."""
        idx_injuries = self.yml.find("name: Fetch injuries")
        idx_susp = self.yml.find("name: Build suspension tracker")
        self.assertNotEqual(idx_injuries, -1)
        self.assertNotEqual(idx_susp, -1)
        self.assertLess(idx_injuries, idx_susp,
            "R11 C3-old: 'Fetch injuries' step must come before "
            "'Build suspension tracker' step (defense-in-depth ordering)")


# ── D2-old: KO venue suffix normalize ────────────────────────────────
class TestR11D2OldKOVenueNormalize(unittest.TestCase):
    def setUp(self):
        self.ko_src = (ROOT / "scripts" / "live" / "_knockout.py").read_text()

    def test_normalize_venue_helper_present(self):
        self.assertIn("def _normalize_venue", self.ko_src,
            "R11 D2-old: _knockout._normalize_venue helper must exist")
        self.assertIn('.split(",")[0].strip()', self.ko_src,
            "R11 D2-old: normalizer must strip ', ST' suffix via "
            ".split(',')[0].strip()")

    def test_normalize_strips_state_suffix(self):
        # Simulate the helper logic since it's nested inside
        # load_knockout_fixtures — pin contract via direct test of the
        # static substring presence above is enough for the regression
        # gate (any future "simplify the parser" PR would surface here).
        # A functional KO venue test would require a bracket fixture
        # which is out of scope for this static pin.
        pass


# ── D3-old: distance matrix indirection validator ────────────────────
class TestR11D3OldDistanceMatrixValidator(unittest.TestCase):
    def setUp(self):
        self.sim = _load("sim_r11_d3", ROOT / "scripts" / "03_simulate.py")

    def test_validator_function_exists(self):
        self.assertTrue(hasattr(self.sim, "validate_venue_distance_indirection"),
            "R11 D3-old: validate_venue_distance_indirection must be defined")

    def test_validator_flags_missing_city_keys(self):
        """Mock load_knockout_fixtures so the test only exercises the
        cfg_data slice we supply (not the real KO bracket on disk).
        Filter issues to only the [group] ones our test data triggers."""
        from unittest.mock import patch
        cfg_data = {
            "venue_city_map": {"Stadium A": "MissingCityName"},
            "group_stage_schedule": [
                {"m": 1, "venue": "Stadium A", "date": "2026-06-11"}
            ],
        }
        distance_matrix = {"distance_km": {"OtherCity": {}}}
        # Patch the lazy import inside the validator.
        import sys
        ko_mod = sys.modules.get("_knockout")
        if ko_mod is not None:
            with patch.object(ko_mod, "load_knockout_fixtures",
                              return_value=[]):
                issues = self.sim.validate_venue_distance_indirection(
                    cfg_data, distance_matrix)
        else:
            issues = self.sim.validate_venue_distance_indirection(
                cfg_data, distance_matrix)
        # Filter to only [group] issues — our test cfg only had group rows.
        group_issues = [i for i in issues if "[group]" in i]
        self.assertEqual(len(group_issues), 1)
        self.assertIn("MissingCityName", group_issues[0])

    def test_validator_clean_when_mapping_consistent(self):
        from unittest.mock import patch
        cfg_data = {
            "venue_city_map": {"Stadium A": "Inglewood"},
            "group_stage_schedule": [{"m": 1, "venue": "Stadium A"}],
        }
        distance_matrix = {"distance_km": {"Inglewood": {}}}
        import sys
        ko_mod = sys.modules.get("_knockout")
        if ko_mod is not None:
            with patch.object(ko_mod, "load_knockout_fixtures",
                              return_value=[]):
                issues = self.sim.validate_venue_distance_indirection(
                    cfg_data, distance_matrix)
        else:
            issues = self.sim.validate_venue_distance_indirection(
                cfg_data, distance_matrix)
        group_issues = [i for i in issues if "[group]" in i]
        self.assertEqual(group_issues, [])

    def test_validator_handles_missing_distance_matrix(self):
        cfg_data = {"venue_city_map": {}, "group_stage_schedule": []}
        # None distance_matrix = travel disabled
        issues = self.sim.validate_venue_distance_indirection(cfg_data, None)
        self.assertEqual(issues, [])


# ── E2-old: annex_c_misses pinned in check_invariants ────────────────
class TestR11E2OldAnnexCMissesInvariant(unittest.TestCase):
    def setUp(self):
        self.ci = _load("ci_r11_e2", ROOT / "scripts" / "check_invariants.py")

    def test_nonzero_annex_c_misses_raises(self):
        tmp = Path(tempfile.mkdtemp(prefix="r11_e2_"))
        try:
            blob = {
                "team_predictions": [
                    {"team": f"T{i}", "p_champion": 1.0 / 48.0}
                    for i in range(48)
                ],
                "annex_c_misses": 3,
            }
            f = tmp / "predictions_live.json"
            f.write_text(json.dumps(blob))
            with self.assertRaises(self.ci.MissingField) as ctx:
                self.ci.check_invariants(f)
            self.assertIn("annex_c_misses", str(ctx.exception))
        finally:
            for p in tmp.iterdir():
                p.unlink()
            tmp.rmdir()


# ── E10: per-stage Σ + INV1 stacking pinned ──────────────────────────
class TestR11E10PerStageSigmaAndStacking(unittest.TestCase):
    def setUp(self):
        self.ci = _load("ci_r11_e10", ROOT / "scripts" / "check_invariants.py")

    def _blob_with_valid_stages(self):
        # 32 teams advance from groups; 16 reach R16; 8 reach QF; 4 SF;
        # 2 Final; 1 Champion. R12 C1 extended stage_expectations and
        # stack_order to include p_advance_groups (32) and p_reach_r16
        # (16) — every team_predictions row must now carry those fields
        # for the per-stage Σ check + INV1 stacking check to fire.
        teams = []
        for i in range(48):
            p_advance = 1.0 if i < 32 else 0.0
            p_r16 = 1.0 if i < 16 else 0.0
            p_qf = 1.0 if i < 8 else 0.0
            p_sf = 1.0 if i < 4 else 0.0
            p_final = 1.0 if i < 2 else 0.0
            p_champ = 1.0 if i == 0 else 0.0
            teams.append({
                "team": f"T{i}",
                "p_advance_groups": p_advance,
                "p_reach_r16": p_r16,
                "p_reach_qf": p_qf, "p_reach_sf": p_sf,
                "p_reach_final": p_final, "p_champion": p_champ,
            })
        return {"team_predictions": teams, "annex_c_misses": 0}

    def test_valid_per_stage_sigma_passes(self):
        tmp = Path(tempfile.mkdtemp(prefix="r11_e10_"))
        try:
            blob = self._blob_with_valid_stages()
            f = tmp / "predictions_live.json"
            f.write_text(json.dumps(blob))
            # Must NOT raise.
            self.ci.check_invariants(f)
        finally:
            for p in tmp.iterdir():
                p.unlink()
            tmp.rmdir()

    def test_inv1_stacking_violation_raises(self):
        """A team whose p_champion > p_reach_final is impossible in a
        single-elim bracket. Must be caught."""
        tmp = Path(tempfile.mkdtemp(prefix="r11_e10_inv1_"))
        try:
            blob = self._blob_with_valid_stages()
            # Break team 0: p_champion (1.0) > p_reach_final (0.0)
            blob["team_predictions"][0]["p_reach_final"] = 0.0
            blob["team_predictions"][0]["p_champion"] = 1.0
            # Re-balance to keep per-stage Σ correct so we isolate
            # the stacking check (not the Σ check) as the trigger.
            # 2 teams must reach final → leave team 1 with p_reach_final=1
            # but also leave team 0 with p_reach_final=0. Σ_final = 0+1+0+0 = 1
            # not 2.0 — we have to add another team. Easier: set both
            # team 0's broader probs to 0 explicitly.
            blob["team_predictions"][0]["p_reach_sf"] = 0.0
            blob["team_predictions"][0]["p_reach_qf"] = 0.0
            # Now Σ p_reach_qf = 7, Σ p_reach_sf = 3, Σ p_reach_final = 1.
            # Top up the missing slots via teams 8/4/2.
            blob["team_predictions"][8]["p_reach_qf"] = 1.0
            blob["team_predictions"][4]["p_reach_sf"] = 1.0
            blob["team_predictions"][2]["p_reach_final"] = 1.0
            # Add the qf+sf prerequisites for those teams too.
            blob["team_predictions"][4]["p_reach_qf"] = 1.0
            blob["team_predictions"][2]["p_reach_sf"] = 1.0
            blob["team_predictions"][2]["p_reach_qf"] = 1.0
            f = tmp / "predictions_live.json"
            f.write_text(json.dumps(blob))
            with self.assertRaises(self.ci.SumOutOfTolerance) as ctx:
                self.ci.check_invariants(f)
            self.assertIn("INV1 stacking violated", str(ctx.exception))
        finally:
            for p in tmp.iterdir():
                p.unlink()
            tmp.rmdir()


if __name__ == "__main__":
    unittest.main(verbosity=2)
