"""R14 regression tests for HIGH + MED fixes (R14 audit of R13).

HIGH:
- D2: dashboard/app.js renderContenders DOM leak (same pattern R13 D1
  fixed in renderCompare).
- C1: scripts/04_evaluate.py + scripts/06_ablation.py lambdas_to_wdl
  default max_g bumped 10 → 15 (sim alignment).
- C2: scripts/02_goal_model.py equivalent_wdl_logloss max_g bumped
  8 → 15 (sim alignment).

MED:
- Son Heung-min last_name_normalized fixed from "heung-min" → "son"
  in data/raw/key_players_2026.json (Korean naming convention: surname
  is the first whitespace token).
- triggering_match_id stripped from suspensions_2026.json output
  (suspension_tracker._attach_elo R14 MED).
- normalize_team defense-in-depth added at weather/lineup/stats loaders
  in apply_matchday_adjustments.py (R14 MED, mirroring R13 A2 hardening
  on injury/referee/suspension loaders).
- n_with == 1 partial-coverage test (R13 MED-1 check works for both
  extremes of partial coverage).
- Missing key_players_2026.json graceful fallback test.
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
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


class TestR14D2RenderContendersClearsOptions(unittest.TestCase):
    def test_render_contenders_clears_group_select(self):
        src = (ROOT / "dashboard" / "app.js").read_text()
        i = src.find("function renderContenders(")
        self.assertNotEqual(i, -1)
        j = src.find("\nfunction ", i + 1)
        body = src[i:j]
        # The fix preserves the default option (index 0) and removes
        # only the dynamically-appended group options via the same
        # `while (X.options.length > 1) X.remove(1)` pattern that
        # renderMatches uses.
        self.assertIn("groupSel.options.length > 1", body,
            "R14 D2: renderContenders must clear groupSel before "
            "appending (preserve default 'all' option at index 0).")
        self.assertIn("groupSel.remove(1)", body,
            "R14 D2: clearing must use .remove(1) loop pattern.")


class TestR14C1LambdasToWdlMaxG(unittest.TestCase):
    def test_04_evaluate_default_max_g_is_15(self):
        src = (ROOT / "scripts" / "04_evaluate.py").read_text()
        m = re.search(r"def lambdas_to_wdl\([^)]*max_g:\s*int\s*=\s*(\d+)\)", src)
        self.assertIsNotNone(m,
            "R14 C1: scripts/04_evaluate.py must declare lambdas_to_wdl "
            "with explicit max_g default")
        self.assertEqual(m.group(1), "15",
            f"R14 C1: lambdas_to_wdl default max_g must be 15; got {m.group(1)}")

    def test_06_ablation_wrapper_max_g_is_15(self):
        src = (ROOT / "scripts" / "06_ablation.py").read_text()
        m = re.search(r"def lambdas_to_wdl\([^)]*max_g\s*=\s*(\d+)\)", src)
        self.assertIsNotNone(m,
            "R14 C1: scripts/06_ablation.py must declare a wrapper "
            "lambdas_to_wdl with explicit max_g")
        self.assertEqual(m.group(1), "15",
            f"R14 C1: ablation wrapper max_g must be 15; got {m.group(1)}")


class TestR14C2GoalModelEquivalentWdlMaxG(unittest.TestCase):
    def test_equivalent_wdl_logloss_max_g_is_15(self):
        src = (ROOT / "scripts" / "02_goal_model.py").read_text()
        # Extract the function body.
        i = src.find("def equivalent_wdl_logloss(")
        self.assertNotEqual(i, -1)
        j = src.find("\ndef ", i + 1)
        body = src[i:j]
        m = re.search(r"^\s*max_g\s*=\s*(\d+)\b", body, re.M)
        self.assertIsNotNone(m,
            "R14 C2: equivalent_wdl_logloss must set max_g")
        self.assertEqual(m.group(1), "15",
            f"R14 C2: max_g must be 15 (was 8 pre-R14); got {m.group(1)}")


class TestR14MEDSonHeungMinLastName(unittest.TestCase):
    def test_son_last_name_normalized_is_son(self):
        d = json.loads((ROOT / "data" / "raw"
                        / "key_players_2026.json").read_text())
        son = next((p for p in d["players"]
                    if p.get("name") == "Son Heung-min"), None)
        self.assertIsNotNone(son, "Son Heung-min must exist")
        self.assertEqual(son.get("last_name_normalized"), "son",
            "R14 MED: Son Heung-min's last_name_normalized must be 'son' "
            "(the Korean surname), not 'heung-min' (the hyphenated given "
            "name). Korean names follow surname-first convention.")


class TestR14MEDTriggeringMatchIdStripped(unittest.TestCase):
    def test_attach_elo_drops_triggering_match_id(self):
        st = _load("st_r14",
                   ROOT / "scripts" / "live" / "suspension_tracker.py")
        rows_in = [{
            "match_id": 5, "team": "Mexico", "player": "Edson Álvarez",
            "player_norm": "edson alvarez", "reason": "red_card",
            "evidence_match_ids": [1], "triggering_match_id": 1,
        }]
        out = st._attach_elo(rows_in)
        self.assertEqual(len(out), 1)
        self.assertNotIn("triggering_match_id", out[0],
            "R14 MED: _attach_elo must strip triggering_match_id before "
            "write (schema docstring at top of file omits it)")
        # R13 MED-3 still works.
        self.assertNotIn("player_norm", out[0])
        # Display player + evidence_match_ids preserved.
        self.assertEqual(out[0]["player"], "Edson Álvarez")
        self.assertEqual(out[0]["evidence_match_ids"], [1])


class TestR14MEDLoaderTeamNormalization(unittest.TestCase):
    """Static-pin: weather/lineup/stats loaders re-normalize team for
    defense-in-depth against operator manual-edit overrides."""

    def setUp(self):
        self.src = (ROOT / "scripts" / "live"
                    / "apply_matchday_adjustments.py").read_text()

    def test_weather_loader_normalizes(self):
        # The weather loop reads w.get(f"{side}_team") and runs
        # normalize_team on it. Look for the raw_team / normalize pattern
        # in the weather-loader scope.
        i = self.src.find("def _load_weather_components")
        self.assertNotEqual(i, -1)
        j = self.src.find("\ndef ", i + 1)
        body = self.src[i:j]
        self.assertIn("raw_team = w.get(f\"{side}_team\")", body,
            "R14 MED: weather loader must extract raw_team first")
        self.assertIn("team = normalize_team(raw_team)", body,
            "R14 MED: weather loader must normalize_team(raw_team)")

    def test_lineup_loader_normalizes(self):
        i = self.src.find("def _load_lineup_components")
        self.assertNotEqual(i, -1)
        j = self.src.find("\ndef ", i + 1)
        body = self.src[i:j]
        self.assertIn("raw_team = ln.get(side)", body,
            "R14 MED: lineup loader must extract raw_team first")
        self.assertIn("team = normalize_team(raw_team)", body,
            "R14 MED: lineup loader must normalize_team(raw_team)")

    def test_stats_loader_normalizes(self):
        i = self.src.find("def _load_stats_components")
        self.assertNotEqual(i, -1)
        j = self.src.find("\ndef ", i + 1)
        body = self.src[i:j]
        self.assertIn("raw_team = s.get(side)", body,
            "R14 MED: stats loader must extract raw_team first")
        self.assertIn("team = normalize_team(raw_team)", body,
            "R14 MED: stats loader must normalize_team(raw_team)")


class TestR14MEDPartialCoverageNWith1(unittest.TestCase):
    """R13 MED-1 partial-coverage check must fire at BOTH extremes
    (n_with=1 AND n_with=47), not just one."""

    def setUp(self):
        self.ci = _load("ci_r14",
                        ROOT / "scripts" / "check_invariants.py")

    def test_n_with_1_raises(self):
        """Only 1 team has p_advance_groups, 47 don't → partial → raise."""
        tmp = Path(tempfile.mkdtemp(prefix="r14_n1_"))
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
                # Only team T0 has p_advance_groups — n_with == 1.
                if i == 0:
                    t["p_advance_groups"] = 1.0
                teams.append(t)
            blob = {"team_predictions": teams, "annex_c_misses": 0}
            f = tmp / "predictions_live.json"
            f.write_text(json.dumps(blob))
            with self.assertRaises(self.ci.MissingField) as ctx:
                self.ci.check_invariants(f)
            self.assertIn("p_advance_groups", str(ctx.exception))
            self.assertIn("1/48", str(ctx.exception),
                "R14 MED: partial-coverage error must report n_with/total")
        finally:
            for p in tmp.iterdir():
                p.unlink()
            tmp.rmdir()


class TestR14MEDMissingKeyPlayersFallback(unittest.TestCase):
    """player_join_key + classify_tier must degrade gracefully when
    key_players_2026.json is missing or empty — no crash, no false
    matches. Cross-feed dedup falls back to full normalized form."""

    def test_missing_file_returns_full_norm(self):
        ia = _load("ia_r14med",
                   ROOT / "scripts" / "live" / "injury_adjustments.py")
        # Temporarily rebind KEY_PLAYERS_PATH to a non-existent file,
        # reset the cache, verify graceful fallback, then restore.
        real_path = ia.KEY_PLAYERS_PATH
        fake_path = Path(tempfile.mkdtemp(prefix="r14_missing_")) / "no_such_file.json"
        try:
            ia.KEY_PLAYERS_PATH = fake_path
            ia.reset_key_players_index_for_tests()
            # With no index, every player_join_key call falls back to
            # the full normalized form (preserves forename so intra-team
            # collisions don't occur).
            self.assertEqual(
                ia.player_join_key("Lautaro Martínez", team="Argentina"),
                "lautaro martinez")
            self.assertEqual(
                ia.player_join_key("Emiliano Martínez", team="Argentina"),
                "emiliano martinez")
            # Cross-feed dedup degrades silently — accepted trade-off.
            self.assertNotEqual(
                ia.player_join_key("R. Jiménez", team="Mexico"),
                ia.player_join_key("Raúl Jiménez", team="Mexico"))
        finally:
            ia.KEY_PLAYERS_PATH = real_path
            ia.reset_key_players_index_for_tests()
            try:
                fake_path.parent.rmdir()
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
