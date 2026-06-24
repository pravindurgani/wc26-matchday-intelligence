"""R13 A1 regression — player_join_key must NOT silently collapse intra-team
same-surname pairs (Argentina Lautaro+Emiliano Martínez, Curaçao Leandro+
Juninho Bacuna, etc.) while still resolving cross-feed initial-form drift
("E. Álvarez" → "Edson Álvarez") for KEY players in key_players_2026.json.

Pre-R13 player_join_key collapsed any multi-token name to its surname:
  "Lautaro Martínez" → "martinez"
  "Emiliano Martínez" → "martinez"
A yellow card earned by either bumped the SAME (team, "martinez") counter
in suspension_tracker, so the YELLOW_THRESHOLD=2 trip falsely banned
whichever player triggered the second yellow — not necessarily the actual
accumulator. Real WC2026 cases:
  - Argentina: Lautaro Martínez (tier_1_star striker) + Emiliano Martínez
    (tier_1_keeper). Both probable cardists.
  - Curaçao: Leandro Bacuna (tier_1_star captain) + Juninho Bacuna
    (tier_1_star top scorer).

R13 A1 makes the helper team-aware: it consults the per-team key_players
index. by_full match → canonical (handles aliases like "Son" → "son
heung-min"). by_last with one match → canonicalize the initial-form
("E. Álvarez" → "edson alvarez"). by_last with multiple matches →
ambiguous → return None → caller falls back to full normalized form,
preserving the forename distinction.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestR13A1IntraTeamSurnameDistinct(unittest.TestCase):
    """Same-surname intra-team pairs MUST get distinct join keys."""

    def setUp(self):
        self.ia = _load("ia_r13",
                        ROOT / "scripts" / "live" / "injury_adjustments.py")
        self.ia.reset_key_players_index_for_tests()

    def test_argentina_martinez_pair_distinct(self):
        lautaro = self.ia.player_join_key("Lautaro Martínez", team="Argentina")
        emiliano = self.ia.player_join_key("Emiliano Martínez", team="Argentina")
        self.assertNotEqual(lautaro, emiliano,
            "R13 A1: Lautaro Martínez and Emiliano Martínez must have "
            "DIFFERENT join keys (both on Argentina; pre-R13 both "
            f"collapsed to 'martinez'). got both = {lautaro!r}")
        # Each must canonicalize to their full normalized form.
        self.assertEqual(lautaro, "lautaro martinez")
        self.assertEqual(emiliano, "emiliano martinez")

    def test_argentina_martinez_initial_form_distinct(self):
        """Even initial-form 'L. Martínez' vs 'E. Martínez' must stay
        distinct on Argentina (ambiguous by_last → caller falls back to
        full normalized form)."""
        l_initial = self.ia.player_join_key("L. Martínez", team="Argentina")
        e_initial = self.ia.player_join_key("E. Martínez", team="Argentina")
        self.assertNotEqual(l_initial, e_initial,
            f"R13 A1: 'L. Martínez' and 'E. Martínez' on Argentina must "
            f"stay distinct (by_last ambiguous → fallback to full norm). "
            f"got both = {l_initial!r}")

    def test_curacao_bacuna_pair_distinct(self):
        leandro = self.ia.player_join_key("Leandro Bacuna", team="Curacao")
        juninho = self.ia.player_join_key("Juninho Bacuna", team="Curacao")
        self.assertNotEqual(leandro, juninho,
            "R13 A1: Leandro and Juninho Bacuna on Curaçao must have "
            f"DIFFERENT join keys; got both = {leandro!r}")


class TestR13A1KeyPlayerCanonicalization(unittest.TestCase):
    """For players in key_players_2026.json, cross-feed initial-form
    drift MUST canonicalize to the same key."""

    def setUp(self):
        self.ia = _load("ia_r13b",
                        ROOT / "scripts" / "live" / "injury_adjustments.py")
        self.ia.reset_key_players_index_for_tests()

    def test_mexico_alvarez_initial_form_canonicalizes(self):
        """Edson Álvarez is the only Álvarez on Mexico's key_players, so
        'E. Álvarez' resolves unambiguously to 'edson alvarez'."""
        full = self.ia.player_join_key("Edson Álvarez", team="Mexico")
        initial = self.ia.player_join_key("E. Álvarez", team="Mexico")
        self.assertEqual(full, initial,
            "R13 A1: 'Edson Álvarez' and 'E. Álvarez' on Mexico must "
            "canonicalize to the same join key via by_last lookup")
        self.assertEqual(full, "edson alvarez")

    def test_korea_son_alias_canonicalizes(self):
        """'Son' (alias on Son Heung-min's key_players entry) must
        canonicalize to the entry's name_normalized."""
        canonical = self.ia.player_join_key("Son Heung-min", team="South Korea")
        alias = self.ia.player_join_key("Son", team="South Korea")
        self.assertEqual(canonical, alias,
            "R13 A1: 'Son' alias must canonicalize to the same join key "
            f"as 'Son Heung-min'; got canonical={canonical!r}, alias={alias!r}")


class TestR13A1FallbackBehavior(unittest.TestCase):
    """Players NOT in key_players index OR no team context → fallback
    to full normalized form (preserves forename component)."""

    def setUp(self):
        self.ia = _load("ia_r13c",
                        ROOT / "scripts" / "live" / "injury_adjustments.py")
        self.ia.reset_key_players_index_for_tests()

    def test_no_team_context_returns_full_norm(self):
        self.assertEqual(
            self.ia.player_join_key("Raúl Jiménez"),
            "raul jimenez")
        self.assertEqual(
            self.ia.player_join_key("R. Jiménez"),
            "r jimenez")

    def test_unknown_player_with_team_returns_full_norm(self):
        # An obscure player not in key_players for some team falls back.
        result = self.ia.player_join_key("Obscure Newkid", team="Mexico")
        self.assertEqual(result, "obscure newkid",
            "R13 A1: unknown player with team context should fall back "
            "to the full normalized form, not collapse to surname")

    def test_empty_input(self):
        self.assertEqual(self.ia.player_join_key(""), "")
        self.assertEqual(self.ia.player_join_key(None), "")


class TestR13A1CallsitesPassTeam(unittest.TestCase):
    """Static-pin: all production call sites of player_join_key MUST pass
    team=... so the canonical resolution is engaged."""

    def test_suspension_tracker_passes_team(self):
        src = (ROOT / "scripts" / "live"
               / "suspension_tracker.py").read_text()
        # Two call sites: in build_suspensions (yellow_counter join) and
        # in the final dedup tuple. Both must use the team= kwarg.
        import re
        calls = re.findall(r"player_join_key\([^)]*\)", src)
        # Filter out the imports.
        for call in calls:
            self.assertIn("team=", call,
                f"R13 A1: suspension_tracker player_join_key call must "
                f"pass team= for canonical resolution; got: {call!r}")
        self.assertGreaterEqual(len(calls), 1,
            "R13 A1: suspension_tracker must have ≥1 player_join_key call")

    def test_apply_matchday_passes_team(self):
        src = (ROOT / "scripts" / "live"
               / "apply_matchday_adjustments.py").read_text()
        import re
        calls = re.findall(r"player_join_key\([^)]*\)", src)
        for call in calls:
            self.assertIn("team=", call,
                f"R13 A1: apply_matchday player_join_key call must pass "
                f"team= for canonical resolution; got: {call!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
