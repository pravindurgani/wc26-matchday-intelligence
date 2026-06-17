"""
Coverage + invariant tests for data/raw/key_players_2026.json (S6 fix).

These tests pin three load-bearing properties of the hand-curated whitelist:

  1. Every WC2026 team in data/raw/squad_values_2026.json (48 nations) has
     at least one entry. Pre-S6 the file was 45/48 — Cape Verde, Curacao
     and Haiti were intentionally absent, so any injured player from those
     three squads silently fell through to DEFAULT_TIER (-12 Elo) instead
     of being classified by importance.

  2. The three newly added teams each carry at least one tier_1_star with a
     populated `replacement` block. Catches a future curator deletion that
     would re-open the S6 hole without surfacing.

  3. For every entry with a `replacement` block, the S5 invariant holds:
     replacement.elo_equiv ∈ [TIER_TO_ELO[player.tier], 0]. Duplicates the
     scripts/pre_flight.py:validate_key_players_replacements gate on
     purpose — redundancy here is cheap and the failure mode (net_injury_elo
     flipping positive) is bad enough to deserve two independent gates.

Run:
    python3 tests/live/test_key_players_coverage.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KEY_PLAYERS_PATH = ROOT / "data" / "raw" / "key_players_2026.json"
SQUAD_VALUES_PATH = ROOT / "data" / "raw" / "squad_values_2026.json"

# Mirror of scripts/live/injury_adjustments.py:TIER_TO_ELO. Hard-coded here
# so the test stays import-light and surfaces a failure if the canonical
# table ever drifts without the test being updated in lockstep.
TIER_TO_ELO = {
    "tier_1_star":    -30.0,
    "tier_1_keeper":  -25.0,
    "tier_2_starter": -12.0,
    "tier_3_squad":    -4.0,
}

# The three nations the S6 fix added. Listed explicitly so test_2 fails
# loudly if a future edit drops one of them rather than silently passing.
S6_NEWLY_COVERED_TEAMS = ("Cape Verde", "Curacao", "Haiti")


def _load_key_players() -> dict:
    return json.loads(KEY_PLAYERS_PATH.read_text())


def _load_squad_values() -> dict:
    return json.loads(SQUAD_VALUES_PATH.read_text())


class TestCoverageAllTeams(unittest.TestCase):
    """Test 1: every WC26 nation in squad_values_2026.json has an entry."""

    def test_all_48_teams_covered(self):
        key = _load_key_players()
        sv = _load_squad_values()
        sv_teams = set(sv["squad_values"].keys())
        key_teams = {p["team"] for p in key["players"]}

        # Hard count first — 48 expected per FIFA WC2026 expansion.
        self.assertEqual(
            len(sv_teams), 48,
            f"squad_values_2026.json should list 48 nations, got {len(sv_teams)}"
        )
        self.assertEqual(
            len(key_teams), 48,
            f"key_players_2026.json should cover 48 teams, got {len(key_teams)}: "
            f"missing {sorted(sv_teams - key_teams)}"
        )

        # Set-difference is the load-bearing check — name spellings must
        # match exactly (e.g. 'Curacao' vs 'Curaçao').
        missing_from_key = sv_teams - key_teams
        extra_in_key = key_teams - sv_teams
        self.assertEqual(
            missing_from_key, set(),
            f"WC26 teams missing from key_players_2026.json: "
            f"{sorted(missing_from_key)}"
        )
        self.assertEqual(
            extra_in_key, set(),
            f"key_players_2026.json carries non-WC26 teams: "
            f"{sorted(extra_in_key)} (spelling drift vs squad_values_2026.json?)"
        )


class TestS6NewlyAddedTeams(unittest.TestCase):
    """Test 2: the 3 teams added by S6 carry tier_1 entries with replacements."""

    def test_each_team_has_tier_1_with_replacement(self):
        key = _load_key_players()
        entries_by_team: dict[str, list[dict]] = {}
        for p in key["players"]:
            entries_by_team.setdefault(p["team"], []).append(p)

        for team in S6_NEWLY_COVERED_TEAMS:
            with self.subTest(team=team):
                entries = entries_by_team.get(team, [])
                self.assertGreater(
                    len(entries), 0,
                    f"{team}: no entries (S6 regression — team dropped)"
                )
                tier_1_with_repl = [
                    e for e in entries
                    if e.get("tier") in ("tier_1_star", "tier_1_keeper")
                    and isinstance(e.get("replacement"), dict)
                    and isinstance(
                        e["replacement"].get("elo_equiv"), (int, float)
                    )
                ]
                self.assertGreaterEqual(
                    len(tier_1_with_repl), 1,
                    f"{team}: no tier_1_* entry with a populated replacement "
                    f"block — S6 fix requires at least one so the team's "
                    f"highest-impact injury surfaces above DEFAULT_TIER. "
                    f"Got entries: {[(e['name'], e.get('tier')) for e in entries]}"
                )


class TestReplacementEloInvariant(unittest.TestCase):
    """Test 3: replacement.elo_equiv ∈ [TIER_TO_ELO[tier], 0] for every entry.

    This is the S5 invariant; duplicated here as a belt-and-braces gate so a
    Cape Verde / Curacao / Haiti curator edit that violates it would fail this
    file's run even without the pre_flight validator. The constraint is what
    keeps net_injury_elo = elo - replacement_elo from flipping positive (an
    injury that 'improves' the team).
    """

    def test_all_replacements_within_bounds(self):
        key = _load_key_players()
        violations: list[str] = []
        for entry in key["players"]:
            replacement = entry.get("replacement")
            if not isinstance(replacement, dict):
                continue
            repl_elo = replacement.get("elo_equiv")
            if not isinstance(repl_elo, (int, float)):
                # Non-numeric is a separate failure mode the schema gate owns.
                continue
            tier = entry.get("tier")
            floor = TIER_TO_ELO.get(tier)
            if floor is None:
                violations.append(
                    f"{entry.get('team')} / {entry.get('name')!r}: unknown "
                    f"tier {tier!r} — cannot validate replacement.elo_equiv"
                )
                continue
            # Invariant: floor <= repl_elo <= 0 (floor is negative).
            if not (floor <= float(repl_elo) <= 0.0):
                violations.append(
                    f"{entry.get('team')} / {entry.get('name')!r}: "
                    f"replacement.elo_equiv={repl_elo} violates [{floor}, 0] "
                    f"for tier {tier!r}"
                )
        self.assertEqual(
            violations, [],
            "replacement.elo_equiv invariant violations:\n  - "
            + "\n  - ".join(violations)
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
