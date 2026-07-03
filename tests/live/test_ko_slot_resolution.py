"""
KO-phase fix (2026-07-03) — slot resolution in the suspension + lineup
schedule loaders.

Pre-fix, `suspension_tracker.load_schedule()` and
`fetch_lineups._load_schedule()` returned knockout rows whose home/away
were bracket slot codes ("W74", "1A", "3A/B/C/D/F"). Both consumers
guard with `is_placeholder_slot` and silently skip, so for the whole
knockout phase a red card imposed no next-match ban and no KO lineup was
ever polled. The fix post-processes the merged schedule through
`_ko_slot_resolution.resolve_schedule_slots`, which delegates to
`export_ko_advance`'s resolver (W/L codes + group ranks + Annex C) and
rewrites slots into real team names wherever locked results allow.

Covered here (hermetic — every input is a tmp_path fixture):
  1. Red card in a RESOLVED KO match → ban lands on the team's next
     resolved fixture (R16 red → QF ban), end-to-end via build_payload.
  2. Red card for a team whose remaining slots are unresolved → no ban
     row, quietly (the pre-fix skip contract is preserved).
  3. Lineup fetcher: a resolved R16 fixture enters the poll window with
     real team names (the main()-loop placeholder guard would pass it).
  4. Unresolved slots keep their placeholder codes and are still
     skipped quietly (no exception, no bogus resolution).

Run:
    python3 -m pytest tests/live/test_ko_slot_resolution.py -q
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

import suspension_tracker as st  # noqa: E402
import fetch_lineups  # noqa: E402
from _knockout import is_placeholder_slot  # noqa: E402
from _ko_slot_resolution import resolve_schedule_slots  # noqa: E402


# ── Shared hermetic fixtures ────────────────────────────────────────────
def _red(team: str, player: str, minute: int = 60) -> dict:
    return {"type": "card", "subtype": "red_card",
            "team": team, "player": player, "minute": minute}


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, indent=1))
    return path


@pytest.fixture
def raw_dir(tmp_path: Path) -> Path:
    """Minimal data/raw twin: empty group schedule + a 3-row KO bracket.

    Bracket wiring: R16 m=89 (W74 vs W77) and m=90 (W75 vs W76) feed
    QF m=97 (W89 vs W90). Kickoff times present so the _knockout
    missing-time warning stays silent.
    """
    d = tmp_path / "raw"
    d.mkdir()
    _write(d / "wc2026_config.json", {
        "group_stage_schedule": [],
        "groups": {},
        "fifa_rankings_june_2026": {},
        "venue_city_map": {},
        "host_cities": [],
    })
    _write(d / "knockout_bracket_2026.json", {
        "r16_bracket": [
            {"match_num": 89, "date": "2026-07-04", "time": "20:00",
             "venue": "Houston, TX", "slot_a": "W74", "slot_b": "W77"},
            {"match_num": 90, "date": "2026-07-05", "time": "17:00",
             "venue": "Dallas, TX", "slot_a": "W75", "slot_b": "W76"},
        ],
        "qf_bracket": [
            {"match_num": 97, "date": "2026-07-09", "time": "18:00",
             "venue": "Boston, MA", "slot_a": "W89", "slot_b": "W90"},
        ],
    })
    return d


@pytest.fixture
def results_path(tmp_path: Path) -> Path:
    """Locked results: R32 winners Spain (m=74) + Mexico (m=77), then the
    R16 m=89 Spain 2-1 Mexico with a red card on EACH side. m=75/76/90
    have not been played, so W75/W76/W90 stay genuinely unknown."""
    return _write(tmp_path / "results_2026.json", {
        "completed_matches": [
            {"m": 74, "home": "Spain", "away": "Poland",
             "home_score": 2, "away_score": 0, "winner": "home",
             "status": "FT"},
            {"m": 77, "home": "Mexico", "away": "Chile",
             "home_score": 1, "away_score": 0, "winner": "home",
             "status": "FT"},
            {"m": 89, "home": "Spain", "away": "Mexico",
             "home_score": 2, "away_score": 1, "winner": "home",
             "status": "FT",
             "events": [_red("Spain", "Rodri"),
                        _red("Mexico", "Carlos Vela")]},
        ],
        "in_play": [],
        "warnings": [],
    })


# ── 1+2: suspension tracker, end-to-end via build_payload ──────────────
class TestSuspensionBansLandOnResolvedKOFixtures:
    def test_red_card_in_resolved_r16_bans_resolved_qf(
            self, raw_dir: Path, results_path: Path) -> None:
        """Spain's m=89 red must land a ban on m=97: the QF slot 'W89'
        resolves to Spain via the locked m=89 result. Pre-fix this
        emitted ZERO rows (every KO row was a placeholder)."""
        payload = st.build_payload(
            results_path=results_path,
            schedule_path=raw_dir / "wc2026_config.json",
            bracket_path=raw_dir / "knockout_bracket_2026.json",
        )
        reds = [s for s in payload["suspensions"]
                if s["reason"] == "red_card" and s["team"] == "Spain"]
        assert len(reds) == 1, (
            f"expected exactly one Spain red-card ban, got "
            f"{payload['suspensions']}"
        )
        assert reds[0]["match_id"] == 97, (
            "ban must land on the RESOLVED next fixture (QF m=97, "
            f"W89→Spain); got match_id={reds[0]['match_id']}"
        )
        assert reds[0]["player"] == "Rodri"
        assert reds[0]["evidence_match_ids"] == [89]

    def test_red_card_with_unresolved_next_slot_is_skipped_quietly(
            self, raw_dir: Path, results_path: Path) -> None:
        """Mexico lost m=89, so its only conceivable next slots (W90 etc.)
        are unresolved — the ban must NOT attach to a placeholder row
        (it would land on whichever team eventually fills the slot).
        No exception, no row: the pre-fix skip contract holds."""
        payload = st.build_payload(
            results_path=results_path,
            schedule_path=raw_dir / "wc2026_config.json",
            bracket_path=raw_dir / "knockout_bracket_2026.json",
        )
        mexico_rows = [s for s in payload["suspensions"]
                       if s["team"] == "Mexico"]
        assert mexico_rows == [], (
            f"no ban may target an unresolved slot; got {mexico_rows}"
        )

    def test_load_schedule_resolves_slots_and_preserves_unknowns(
            self, raw_dir: Path, results_path: Path) -> None:
        """Loader-level contract: resolved sides carry real team names
        (original codes preserved on slot_home/slot_away); unresolved
        sides keep their codes so is_placeholder_slot still fires."""
        sched = st.load_schedule(
            raw_dir / "wc2026_config.json",
            bracket_path=raw_dir / "knockout_bracket_2026.json",
            results_path=results_path,
        )
        rows = {r["m"]: r for r in sched}
        assert rows[89]["home"] == "Spain"
        assert rows[89]["away"] == "Mexico"
        assert rows[89]["slot_home"] == "W74"
        assert rows[89]["slot_away"] == "W77"
        # QF: home side resolved from the locked R16, away still unknown.
        assert rows[97]["home"] == "Spain"
        assert rows[97]["away"] == "W90"
        assert is_placeholder_slot(rows[97]["away"])
        # Fully-unplayed feeder pair stays fully placeholder.
        assert rows[90]["home"] == "W75"
        assert rows[90]["away"] == "W76"

    def test_next_match_for_team_still_refuses_placeholder_queries(
            self, raw_dir: Path, results_path: Path) -> None:
        """Slot codes queried AS team names must keep returning None —
        resolution must not weaken the Round 6 placeholder guard."""
        sched = st.load_schedule(
            raw_dir / "wc2026_config.json",
            bracket_path=raw_dir / "knockout_bracket_2026.json",
            results_path=results_path,
        )
        assert st.next_match_for_team("W90", 0, sched) is None
        assert st.next_match_for_team("W75", 0, sched) is None


# ── 3+4: lineup fetcher poll window ─────────────────────────────────────
class TestLineupPollWindowIncludesResolvedKO:
    def test_resolved_r16_fixture_enters_poll_window(
            self, monkeypatch, raw_dir: Path, results_path: Path) -> None:
        """With m=89 resolved (Spain vs Mexico) and kickoff 4h away, the
        fixture must be in the poll window with REAL team names — i.e.
        the main()-loop `is_placeholder_slot` guard no longer skips it.
        Pre-fix the row carried W74/W77 and was skipped every tick."""
        monkeypatch.setattr(fetch_lineups, "RAW", raw_dir)
        sched = fetch_lineups._load_schedule(results_path=results_path)
        now = datetime(2026, 7, 4, 17, 0, tzinfo=timezone.utc)
        window = fetch_lineups.fixtures_in_window(sched, hours_ahead=4,
                                                  now=now)
        assert [s["m"] for s in window] == [89], (
            f"expected exactly the resolved R16 m=89 in the 4h window, "
            f"got {[s['m'] for s in window]}"
        )
        r16 = window[0]
        assert (r16["home"], r16["away"]) == ("Spain", "Mexico")
        # This is the exact guard main() applies before polling.
        assert not (is_placeholder_slot(r16.get("home"))
                    or is_placeholder_slot(r16.get("away"))), (
            "resolved KO fixture must pass the placeholder guard so a "
            "lineup poll actually happens"
        )

    def test_unresolved_fixture_still_skipped_quietly(
            self, monkeypatch, raw_dir: Path, results_path: Path) -> None:
        """m=90 (W75 vs W76, feeders unplayed) must stay a placeholder
        row: in the window when its kickoff nears, but still failing the
        placeholder guard → main() records unresolved_slot and moves on,
        exactly the pre-fix behavior."""
        monkeypatch.setattr(fetch_lineups, "RAW", raw_dir)
        sched = fetch_lineups._load_schedule(results_path=results_path)
        now = datetime(2026, 7, 5, 14, 0, tzinfo=timezone.utc)
        window = fetch_lineups.fixtures_in_window(sched, hours_ahead=4,
                                                  now=now)
        assert [s["m"] for s in window] == [90]
        r90 = window[0]
        assert (r90["home"], r90["away"]) == ("W75", "W76")
        assert (is_placeholder_slot(r90.get("home"))
                or is_placeholder_slot(r90.get("away"))), (
            "unresolved KO fixture must still trip the placeholder guard"
        )


# ── Resolver-glue robustness ────────────────────────────────────────────
class TestResolverDegradesQuietly:
    def test_missing_results_file_leaves_all_slots_untouched(
            self, raw_dir: Path, tmp_path: Path) -> None:
        rows = [
            {"m": 89, "stage": "r16", "home": "W74", "away": "W77"},
            {"m": 1, "stage": "group", "home": "Spain", "away": "Chile"},
        ]
        out = resolve_schedule_slots(
            rows,
            results_path=tmp_path / "does_not_exist.json",
            config_path=raw_dir / "wc2026_config.json",
        )
        assert out[0]["home"] == "W74" and out[0]["away"] == "W77"
        assert "slot_home" not in out[0]
        assert out[1]["home"] == "Spain"  # group rows never touched

    def test_unreadable_config_degrades_to_unresolved(
            self, tmp_path: Path, results_path: Path) -> None:
        """A resolver-input failure must degrade to the pre-fix state
        (placeholders intact), never raise into the caller's tick."""
        rows = [{"m": 97, "stage": "qf", "home": "W89", "away": "W90"}]
        out = resolve_schedule_slots(
            rows,
            results_path=results_path,
            config_path=tmp_path / "missing_config.json",
        )
        assert out[0]["home"] == "W89" and out[0]["away"] == "W90"
