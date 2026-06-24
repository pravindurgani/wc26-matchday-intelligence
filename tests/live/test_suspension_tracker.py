"""Phase 4 — suspension_tracker + apply_matchday_adjustments wiring tests.

Covers:
  - _card_kind detection (yellow / red / second_yellow / unknown)
  - next_match_for_team schedule lookup
  - build_suspensions: yellow accumulation = 2, red, second_yellow,
    no-events branch, evidence_match_ids tracks the triggering pair
  - _attach_elo: cap enforcement
  - build_payload §4 fallback: emits `no_events_in_snapshot` when
    completed > 0 and with_events == 0
  - build_payload: emits `results_missing` and `schedule_missing` warnings
  - apply_matchday_adjustments._load_suspension_components shape +
    cap-double-enforced + zero-skipped
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

import suspension_tracker as st  # noqa: E402
import apply_matchday_adjustments as ama  # noqa: E402


_SCHEDULE = [
    {"m": 1, "date": "2026-06-11", "home": "Mexico", "away": "South Africa"},
    {"m": 2, "date": "2026-06-12", "home": "Canada", "away": "Brazil"},
    {"m": 17, "date": "2026-06-17", "home": "Mexico", "away": "Canada"},
    {"m": 33, "date": "2026-06-23", "home": "South Africa", "away": "Brazil"},
    {"m": 50, "date": "2026-06-25", "home": "Mexico", "away": "Brazil"},
]


def _ev(kind: str, team: str, player: str, minute: int = 45) -> dict:
    """Build a normalized card event matching fetch_results.normalize_event."""
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


class TestCardKind(unittest.TestCase):
    def test_yellow(self):
        self.assertEqual(st._card_kind({"type": "card", "subtype": "yellow_card"}), "yellow")

    def test_red(self):
        self.assertEqual(st._card_kind({"type": "card", "subtype": "red_card"}), "red")

    def test_second_yellow(self):
        self.assertEqual(
            st._card_kind({"type": "card", "subtype": "second_yellow_card"}),
            "second_yellow",
        )

    def test_non_card_returns_none(self):
        self.assertIsNone(st._card_kind({"type": "goal", "subtype": "normal_goal"}))

    def test_unknown_card_subtype(self):
        self.assertIsNone(st._card_kind({"type": "card", "subtype": "mystery"}))

    def test_case_insensitive(self):
        self.assertEqual(st._card_kind({"type": "CARD", "subtype": "Yellow Card"}), "yellow")


class TestNextMatchForTeam(unittest.TestCase):
    def test_returns_next_strictly_greater(self):
        self.assertEqual(st.next_match_for_team("Mexico", 1, _SCHEDULE), 17)
        self.assertEqual(st.next_match_for_team("Mexico", 17, _SCHEDULE), 50)

    def test_returns_none_when_no_remaining(self):
        self.assertIsNone(st.next_match_for_team("Mexico", 50, _SCHEDULE))

    def test_unknown_team_returns_none(self):
        self.assertIsNone(st.next_match_for_team("Atlantis", 1, _SCHEDULE))


class TestBuildSuspensions(unittest.TestCase):
    def test_red_card_emits_immediate_ban(self):
        completed = [{
            "m": 1, "home": "Mexico", "away": "South Africa",
            "events": [_ev("red", "Mexico", "Carlos")],
        }]
        sus, summary = st.build_suspensions(completed, _SCHEDULE)
        self.assertEqual(len(sus), 1)
        self.assertEqual(sus[0]["reason"], "red_card")
        self.assertEqual(sus[0]["match_id"], 17)
        self.assertEqual(sus[0]["evidence_match_ids"], [1])
        self.assertEqual(summary["n_with_events"], 1)
        self.assertEqual(summary["n_suspensions"], 1)

    def test_second_yellow_emits_immediate_ban(self):
        completed = [{
            "m": 1, "home": "Mexico", "away": "South Africa",
            "events": [
                _ev("yellow", "Mexico", "Carlos", 22),
                _ev("second_yellow", "Mexico", "Carlos", 67),
            ],
        }]
        sus, _ = st.build_suspensions(completed, _SCHEDULE)
        # The first yellow seeds counter[Carlos] = 1; the second_yellow
        # path bans regardless and does NOT roll the counter.
        reasons = sorted(s["reason"] for s in sus)
        self.assertIn("second_yellow_card", reasons)
        self.assertTrue(any(s["match_id"] == 17 for s in sus))

    def test_yellow_accumulation_threshold_two(self):
        completed = [
            {"m": 1, "home": "Mexico", "away": "South Africa",
             "events": [_ev("yellow", "Mexico", "Carlos")]},
            {"m": 17, "home": "Mexico", "away": "Canada",
             "events": [_ev("yellow", "Mexico", "Carlos")]},
        ]
        sus, _ = st.build_suspensions(completed, _SCHEDULE)
        # Should produce ONE accumulated_yellows row for Mexico's next match (50).
        accumulated = [s for s in sus if s["reason"] == "accumulated_yellows"]
        self.assertEqual(len(accumulated), 1)
        self.assertEqual(accumulated[0]["match_id"], 50)
        self.assertEqual(accumulated[0]["evidence_match_ids"], [1, 17])

    def test_single_yellow_no_ban(self):
        completed = [{
            "m": 1, "home": "Mexico", "away": "South Africa",
            "events": [_ev("yellow", "Mexico", "Carlos")],
        }]
        sus, summary = st.build_suspensions(completed, _SCHEDULE)
        self.assertEqual(sus, [])
        self.assertEqual(summary["n_suspensions"], 0)

    def test_no_events_branch_skipped(self):
        completed = [{"m": 1, "home": "Mexico", "away": "South Africa"}]
        sus, summary = st.build_suspensions(completed, _SCHEDULE)
        self.assertEqual(sus, [])
        self.assertEqual(summary["n_completed_matches"], 1)
        self.assertEqual(summary["n_with_events"], 0)

    def test_no_next_match_skips_emission(self):
        # Red card in the LAST scheduled match for the team — no next match
        # exists, so no suspension row is emitted (knockout-only fixture).
        completed = [{
            "m": 50, "home": "Mexico", "away": "Brazil",
            "events": [_ev("red", "Mexico", "Carlos")],
        }]
        sus, _ = st.build_suspensions(completed, _SCHEDULE)
        self.assertEqual(sus, [])

    def test_yellow_counter_resets_after_ban(self):
        # Three yellows in three matches: ban after match 17 (counter hits 2),
        # then a fresh yellow at match 33 leaves counter at 1 (no second ban).
        completed = [
            {"m": 1, "home": "Mexico", "away": "South Africa",
             "events": [_ev("yellow", "South Africa", "Sipho")]},
            {"m": 17, "home": "South Africa", "away": "Brazil",
             "events": [_ev("yellow", "South Africa", "Sipho")]},
            {"m": 33, "home": "South Africa", "away": "Brazil",
             "events": [_ev("yellow", "South Africa", "Sipho")]},
        ]
        # South Africa plays m=1 and m=33 in this fixture. After m=17 emits
        # the ban (next match is m=33), the counter resets — m=33's yellow
        # is a fresh count of 1, no further ban.
        sus, _ = st.build_suspensions(completed, _SCHEDULE)
        accumulated = [s for s in sus if s["reason"] == "accumulated_yellows"]
        self.assertEqual(len(accumulated), 1)
        self.assertEqual(accumulated[0]["evidence_match_ids"], [1, 17])


class TestAttachElo(unittest.TestCase):
    def test_attaches_capped_elo_to_each_row(self):
        rows = st._attach_elo([
            {"match_id": 17, "team": "Mexico", "player": "Carlos",
             "reason": "red_card", "evidence_match_ids": [1]},
        ])
        self.assertEqual(rows[0]["raw_elo"], st.PER_SUSPENSION_ELO)
        self.assertEqual(rows[0]["team_adjustment_elo"], st.PER_SUSPENSION_ELO)
        self.assertEqual(rows[0]["cap_used"], st.SUSPENSION_CAP)
        self.assertEqual(rows[0]["confidence"], "high")
        self.assertEqual(rows[0]["source"], "fetch_results_events")


class TestBuildPayloadFallbacks(unittest.TestCase):
    def test_results_missing_emits_warning(self):
        payload = st.build_payload(
            results_path=Path("/nonexistent_results.json"),
            schedule_path=Path("/nonexistent_schedule.json"),
        )
        self.assertEqual(payload["suspensions"], [])
        self.assertTrue(any(w["type"] == "results_missing" for w in payload["warnings"]))
        self.assertEqual(payload["cap_used"], st.SUSPENSION_CAP)
        self.assertEqual(payload["yellow_threshold"], st.YELLOW_THRESHOLD)

    def test_schedule_missing_emits_warning(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            results = tdp / "results.json"
            results.write_text(json.dumps({"completed_matches": []}))
            payload = st.build_payload(
                results_path=results,
                schedule_path=tdp / "missing_schedule.json",
            )
        self.assertTrue(any(w["type"] == "schedule_missing" for w in payload["warnings"]))

    def test_no_events_in_snapshot_warning(self):
        """§4 fallback: dry source must surface loudly, never silently zero."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            results = tdp / "results.json"
            schedule = tdp / "schedule.json"
            # 3 completed matches, NONE with events key — mirrors the
            # current live state where B3 hasn't been run yet.
            results.write_text(json.dumps({
                "completed_matches": [
                    {"m": 1, "home": "Mexico", "away": "South Africa"},
                    {"m": 2, "home": "Canada", "away": "Brazil"},
                    {"m": 3, "home": "USA", "away": "Italy"},
                ],
            }))
            schedule.write_text(json.dumps({"group_stage_schedule": _SCHEDULE}))
            payload = st.build_payload(results_path=results, schedule_path=schedule)
        warns = [w for w in payload["warnings"] if w["type"] == "no_events_in_snapshot"]
        self.assertEqual(len(warns), 1)
        self.assertEqual(warns[0]["n_completed"], 3)
        self.assertEqual(warns[0]["n_with_events"], 0)
        self.assertEqual(payload["suspensions"], [])
        self.assertEqual(payload["summary"]["n_completed_matches"], 3)
        self.assertEqual(payload["summary"]["n_with_events"], 0)

    def test_payload_with_events_omits_dry_warning(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            results = tdp / "results.json"
            schedule = tdp / "schedule.json"
            results.write_text(json.dumps({
                "completed_matches": [
                    {"m": 1, "home": "Mexico", "away": "South Africa",
                     "events": [_ev("red", "Mexico", "Carlos")]},
                ],
            }))
            schedule.write_text(json.dumps({"group_stage_schedule": _SCHEDULE}))
            payload = st.build_payload(results_path=results, schedule_path=schedule)
        self.assertFalse(any(w["type"] == "no_events_in_snapshot" for w in payload["warnings"]))
        self.assertEqual(len(payload["suspensions"]), 1)
        self.assertEqual(payload["suspensions"][0]["team_adjustment_elo"], st.PER_SUSPENSION_ELO)


class TestLoadSuspensionComponents(unittest.TestCase):
    """apply_matchday_adjustments._load_suspension_components shape contract."""

    def test_one_sided_emission(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "suspensions_2026.json").write_text(json.dumps({
                "suspensions": [
                    {"match_id": 17, "team": "Mexico", "player": "Carlos",
                     "reason": "red_card",
                     "team_adjustment_elo": -3.0,
                     "raw_elo": -3.0,
                     "cap_used": 8.0,
                     "evidence_match_ids": [1],
                     "confidence": "high",
                     "source": "fetch_results_events"},
                ],
            }))
            orig = ama.LIVE
            ama.LIVE = tdp
            try:
                out = ama._load_suspension_components()
            finally:
                ama.LIVE = orig
        self.assertIn(("Mexico", 17), out)
        comp = out[("Mexico", 17)][0]
        self.assertEqual(comp["type"], "suspension")
        self.assertEqual(comp["capped_elo"], -3.0)
        self.assertEqual(comp["cap_used"], ama.SUSPENSION_CAP)

    def test_zero_elo_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "suspensions_2026.json").write_text(json.dumps({
                "suspensions": [
                    {"match_id": 17, "team": "Mexico", "player": "Phantom",
                     "team_adjustment_elo": 0.0, "reason": "red_card"},
                ],
            }))
            orig = ama.LIVE
            ama.LIVE = tdp
            try:
                out = ama._load_suspension_components()
            finally:
                ama.LIVE = orig
        self.assertEqual(out, {})

    def test_multiple_suspensions_re_clamp_at_cap(self):
        # Three -3.0 suspensions = -9.0 raw → clamped to SUSPENSION_CAP (-8.0).
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "suspensions_2026.json").write_text(json.dumps({
                "suspensions": [
                    {"match_id": 17, "team": "Mexico", "player": "A",
                     "team_adjustment_elo": -3.0, "reason": "red_card"},
                    {"match_id": 17, "team": "Mexico", "player": "B",
                     "team_adjustment_elo": -3.0, "reason": "red_card"},
                    {"match_id": 17, "team": "Mexico", "player": "C",
                     "team_adjustment_elo": -3.0, "reason": "red_card"},
                ],
            }))
            orig = ama.LIVE
            ama.LIVE = tdp
            try:
                out = ama._load_suspension_components()
            finally:
                ama.LIVE = orig
        comps = out[("Mexico", 17)]
        total = sum(c["capped_elo"] for c in comps)
        self.assertAlmostEqual(total, -ama.SUSPENSION_CAP, places=3)
        # Last component should be truncated to fit remaining budget.
        cap_reasons = [c["cap_reason"] for c in comps]
        self.assertIn("suspension_total", cap_reasons)

    def test_missing_file_yields_empty(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            orig = ama.LIVE
            ama.LIVE = tdp
            try:
                out = ama._load_suspension_components()
            finally:
                ama.LIVE = orig
        self.assertEqual(out, {})


if __name__ == "__main__":
    unittest.main()
