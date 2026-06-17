"""Adversarial audit for scripts/live/suspension_tracker.py.

Each test feeds malformed / contradictory card-event data through
build_suspensions and classifies the observed behavior as:

  LOUD  — drops the bad event, returns no suspension, or raises.
  SILENT (FIXED) — formerly codified as xfail(strict=True). The four
                   silent-failure modes (duplicate red, duplicate yellow,
                   missing match-id, cross-feed dedup) have been closed
                   in suspension_tracker.py via per-match + final
                   idempotency keys. Assertions now run green.

These tests do NOT modify production thresholds (PER_SUSPENSION_ELO,
SUSPENSION_CAP, YELLOW_THRESHOLD). AUTO_TIER_ACTIVE stays False.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

import suspension_tracker as st  # noqa: E402


_SCHEDULE = [
    {"m": 1, "date": "2026-06-11", "home": "Mexico", "away": "South Africa"},
    {"m": 2, "date": "2026-06-12", "home": "Canada", "away": "Brazil"},
    {"m": 17, "date": "2026-06-17", "home": "Mexico", "away": "Canada"},
    {"m": 33, "date": "2026-06-23", "home": "South Africa", "away": "Brazil"},
    {"m": 50, "date": "2026-06-25", "home": "Mexico", "away": "Brazil"},
]


def _ev(kind: str, team: str, player: str, minute: int = 45) -> dict:
    subtype = {
        "yellow": "yellow_card",
        "red": "red_card",
        "second_yellow": "second_yellow_card",
    }[kind]
    return {
        "type": "card",
        "subtype": subtype,
        "team": team,
        "player": player,
        "minute": minute,
        "extra_minute": None,
        "assist": None,
        "comments": None,
    }


# ── LOUD cases ──────────────────────────────────────────────────────────

class TestSuspensionLoudFailures(unittest.TestCase):
    """Inputs where the current code correctly drops / refuses / surfaces."""

    def test_card_with_no_player_is_dropped(self):
        completed = [{
            "m": 1, "home": "Mexico", "away": "South Africa",
            "events": [{"type": "card", "subtype": "red_card",
                        "team": "Mexico", "player": None, "minute": 10}],
        }]
        sus, summary = st.build_suspensions(completed, _SCHEDULE)
        self.assertEqual(sus, [])
        self.assertEqual(summary["n_suspensions"], 0)

    def test_card_with_no_team_is_dropped(self):
        completed = [{
            "m": 1, "home": "Mexico", "away": "South Africa",
            "events": [{"type": "card", "subtype": "red_card",
                        "team": None, "player": "Carlos", "minute": 10}],
        }]
        sus, _ = st.build_suspensions(completed, _SCHEDULE)
        self.assertEqual(sus, [])

    def test_card_with_no_minute_still_processes(self):
        # minute is irrelevant to the suspension model — accepting it as a
        # valid card is LOUD-OK because the model does not depend on it.
        completed = [{
            "m": 1, "home": "Mexico", "away": "South Africa",
            "events": [{"type": "card", "subtype": "red_card",
                        "team": "Mexico", "player": "Carlos", "minute": None}],
        }]
        sus, _ = st.build_suspensions(completed, _SCHEDULE)
        self.assertEqual(len(sus), 1)
        self.assertEqual(sus[0]["reason"], "red_card")

    def test_minute_over_120_still_processes(self):
        # Extra-time / penalty-shootout minute > 120 is a real edge case.
        # The model ignores minute → suspension still emits. LOUD-OK.
        completed = [{
            "m": 1, "home": "Mexico", "away": "South Africa",
            "events": [_ev("red", "Mexico", "Carlos", minute=999)],
        }]
        sus, _ = st.build_suspensions(completed, _SCHEDULE)
        self.assertEqual(len(sus), 1)
        self.assertEqual(sus[0]["match_id"], 17)

    def test_yellow_plus_red_same_player_same_match_red_supersedes(self):
        # Both events processed but the yellow counter goes to 1 (no ban),
        # and the red issues a single red_card suspension. Net = 1 ban,
        # reason='red_card'. Correct.
        completed = [{
            "m": 1, "home": "Mexico", "away": "South Africa",
            "events": [
                _ev("yellow", "Mexico", "Carlos", 10),
                _ev("red", "Mexico", "Carlos", 50),
            ],
        }]
        sus, _ = st.build_suspensions(completed, _SCHEDULE)
        reasons = [s["reason"] for s in sus]
        self.assertEqual(reasons, ["red_card"])
        self.assertEqual(sus[0]["match_id"], 17)

    def test_no_next_match_for_red_in_last_fixture(self):
        # Red issued in team's final scheduled fixture → no next match.
        # Suspension is silently NOT emitted. This is LOUD-OK at the
        # group-stage level (knockout extends elsewhere); the empty
        # suspensions list is the honest answer for the group surface.
        completed = [{
            "m": 50, "home": "Mexico", "away": "Brazil",
            "events": [_ev("red", "Mexico", "Carlos")],
        }]
        sus, _ = st.build_suspensions(completed, _SCHEDULE)
        self.assertEqual(sus, [])

    def test_yellow_across_unrelated_team_yellows_do_not_cross_contaminate(self):
        # Two yellows on the SAME player but DIFFERENT teams — should never
        # accumulate. (Player swapped teams between tournaments — wildly
        # adversarial but verifies key isolation.)
        completed = [
            {"m": 1, "home": "Mexico", "away": "South Africa",
             "events": [_ev("yellow", "Mexico", "Carlos")]},
            {"m": 17, "home": "Mexico", "away": "Canada",
             "events": [_ev("yellow", "Canada", "Carlos")]},
        ]
        sus, _ = st.build_suspensions(completed, _SCHEDULE)
        self.assertEqual(sus, [])

    def test_player_with_two_yellows_in_same_tournament_accumulates(self):
        # Canonical yellow accumulation across the threshold boundary.
        completed = [
            {"m": 1, "home": "Mexico", "away": "South Africa",
             "events": [_ev("yellow", "Mexico", "Carlos")]},
            {"m": 17, "home": "Mexico", "away": "Canada",
             "events": [_ev("yellow", "Mexico", "Carlos")]},
        ]
        sus, _ = st.build_suspensions(completed, _SCHEDULE)
        accumulated = [s for s in sus if s["reason"] == "accumulated_yellows"]
        self.assertEqual(len(accumulated), 1)
        self.assertEqual(accumulated[0]["match_id"], 50)
        self.assertEqual(accumulated[0]["evidence_match_ids"], [1, 17])

    def test_yellow_counter_resets_after_ban_issued(self):
        # Two yellows trigger a ban; a fresh yellow in a later match should
        # NOT re-trigger immediately.
        completed = [
            {"m": 1, "home": "Mexico", "away": "South Africa",
             "events": [_ev("yellow", "South Africa", "Sipho")]},
            {"m": 17, "home": "Mexico", "away": "Canada",
             "events": [_ev("yellow", "South Africa", "Sipho")]},
            {"m": 33, "home": "South Africa", "away": "Brazil",
             "events": [_ev("yellow", "South Africa", "Sipho")]},
        ]
        sus, _ = st.build_suspensions(completed, _SCHEDULE)
        accumulated = [s for s in sus if s["reason"] == "accumulated_yellows"]
        self.assertEqual(len(accumulated), 1)
        self.assertEqual(accumulated[0]["evidence_match_ids"], [1, 17])

    def test_yellow_in_one_tournament_does_not_accumulate_across_tournaments(self):
        # Schedule has only group-stage rows; second yellow in a hypothetical
        # post-tournament match wouldn't appear in completed_matches under
        # normal use. The script has no cross-tournament concept so this is
        # primarily a documentation test: only matches passed in are tracked.
        completed = [
            {"m": 1, "home": "Mexico", "away": "South Africa",
             "events": [_ev("yellow", "Mexico", "Carlos")]},
        ]
        sus, _ = st.build_suspensions(completed, _SCHEDULE)
        self.assertEqual(sus, [])


# ── SILENT cases (xfail strict) ─────────────────────────────────────────

def test_duplicate_red_same_incident_is_deduped():
    """Same red card emitted twice → ONE suspension row.

    Fixed at suspension_tracker.py: per-match `seen` set keyed by
    (team, player, kind) drops the duplicate before it can produce a
    second suspension row.
    """
    completed = [{
        "m": 1, "home": "Mexico", "away": "South Africa",
        "events": [
            _ev("red", "Mexico", "Carlos", 50),
            _ev("red", "Mexico", "Carlos", 50),  # provider dupe
        ],
    }]
    sus, summary = st.build_suspensions(completed, _SCHEDULE)
    # Honest answer: ONE ban for Carlos against match 17.
    assert len(sus) == 1, (
        f"Duplicate red emitted {len(sus)} suspension rows; expected 1"
    )
    assert summary["n_suspensions"] == 1
    assert sus[0]["team"] == "Mexico"
    assert sus[0]["player"] == "Carlos"
    assert sus[0]["match_id"] == 17
    assert sus[0]["reason"] == "red_card"


def test_duplicate_yellow_same_match_does_not_trigger_accumulation_ban():
    """Two yellow events for one player in one match → counter increments
    by ONE, no false ban.

    Fixed at suspension_tracker.py: the same per-match `seen` set that
    guards red cards also guards the yellow path, so a duplicate yellow
    within a single match cannot push the accumulation counter past the
    threshold. A real second yellow arrives with subtype 'second_yellow'
    via the normalizer, not a repeat 'yellow' event.
    """
    completed = [{
        "m": 1, "home": "Mexico", "away": "South Africa",
        "events": [
            _ev("yellow", "Mexico", "Carlos", 10),
            _ev("yellow", "Mexico", "Carlos", 60),  # provider dupe
        ],
    }]
    sus, _ = st.build_suspensions(completed, _SCHEDULE)
    # Honest answer: ONE yellow counted → no ban yet.
    assert sus == [], (
        f"Duplicate yellow within match silently produced a ban: {sus}"
    )


def test_missing_match_id_drops_the_row():
    """A match with no 'm' field is dropped — no phantom ban.

    Fixed at suspension_tracker.py: `if match.get('m') is None: continue`
    before the int cast. Without this guard, a missing 'm' silently
    became mid=0, and next_match_for_team would resolve to the FIRST
    scheduled fixture for the team — confident suspension, wrong target.
    """
    completed = [{
        "home": "Mexico", "away": "South Africa",
        "events": [_ev("red", "Mexico", "Carlos")],
    }]
    sus, _ = st.build_suspensions(completed, _SCHEDULE)
    # Honest answer: cannot resolve next match without knowing the source
    # match → no row should be emitted.
    assert sus == [], (
        f"Missing match-id silently produced suspension: {sus}"
    )


def test_two_feed_sources_for_same_red_dedupe_at_final_list():
    """Conflicting feed sources both reporting the same red → ONE row.

    Fixed at suspension_tracker.py with TWO layers:
      1. Per-match `seen` set keyed by (team, player, kind) catches the
         intra-match case (both reds in the same events list, even with
         different minute fields — minute is not part of the dedup key).
      2. Final `(team, player, match_id, reason)` dedup before
         `_attach_elo` catches the cross-match case (same banned player
         emerging from two different source matches).

    The minute-51 vs minute-50 variant here is fully closed by layer 1
    because minute is intentionally not part of the per-match key — a
    real second yellow arrives with a distinct subtype, not a re-emit of
    the same kind. Layer 2 still runs as belt-and-suspenders.
    """
    completed = [{
        "m": 1, "home": "Mexico", "away": "South Africa",
        "events": [
            _ev("red", "Mexico", "Carlos", 50),
            _ev("red", "Mexico", "Carlos", 51),  # other source, off by 1min
        ],
    }]
    sus, _ = st.build_suspensions(completed, _SCHEDULE)
    # Dedupe-by-key expectation:
    unique_keys = {(s["team"], s["player"], s["match_id"], s["reason"])
                   for s in sus}
    assert len(sus) == len(unique_keys), (
        f"Duplicate suspension keys present: rows={len(sus)} "
        f"unique={len(unique_keys)}"
    )
    # And exactly ONE ban for Carlos.
    assert len(sus) == 1, (
        f"Two-feed dupe produced {len(sus)} rows; expected 1"
    )


if __name__ == "__main__":
    unittest.main()
