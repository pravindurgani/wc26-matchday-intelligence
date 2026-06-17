"""
test_cross_subsystem_invariants.py — Invariants that hold across the
combined effect of every matchday subsystem.

Why this exists
===============
Each subsystem (injury, lineup, referee, suspension, stats_proxy) has
adversarial coverage in isolation. But the orchestrator at
scripts/live/apply_matchday_adjustments.py simply SUMS their outputs
into per-(team, match_id) buckets, then re-clamps at
AGGREGATE_MATCHDAY_CAP (35). That sum has its own invariants that no
single-subsystem test can catch:

  1. No double-counting injury + suspension for the same player.
  2. Bounded total adjustment per team — no compounding above the
     documented sum-of-caps.
  3. Best-case team can reach the positive bound (not silently floored).
  4. A team with NO entries in ANY snapshot has adjustment = 0 exactly.
  5. Symmetric referee bonus — +5 home is -5 away (or whatever the
     documented mirror rule is).
  6. Σ p_champion stays at 1.0 after the matchday layer runs.

Constraints (mirrored from issue):
  - No prod code changes; tests only.
  - AUTO_TIER_ACTIVE stays False (asserted in test_pipeline_e2e.py).
  - Synthetic fixtures only — no network.

Run:
    python3 -m pytest tests/live/test_cross_subsystem_invariants.py -v
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

import apply_matchday_adjustments as amd  # noqa: E402
import lineup_adjustments as lna  # noqa: E402


# Documented caps (read straight from the live module so a tightening / loosening
# is caught here, not silently absorbed).
REFEREE_CAP = amd.REFEREE_CAP
SUSPENSION_CAP = amd.SUSPENSION_CAP
INJURY_CAP_NORMAL = amd.INJURY_CAP_NORMAL
INJURY_CAP_EXTREME = amd.INJURY_CAP_EXTREME
LINEUP_CAP = amd.LINEUP_CAP
FORM_CAP = amd.STATS_CAP_TOURNAMENT_TOTAL
AGGREGATE_CAP = amd.AGGREGATE_MATCHDAY_CAP
GK_SWAP_ELO = lna.GK_SWAP_ELO  # -8.0

# Sum-of-all-subsystem-caps — the loosest bound BEFORE AGGREGATE_MATCHDAY_CAP.
# Used in the bounded-total-adjustment invariant. We deliberately use the
# extreme injury cap (overlay path can stack toward 35) so the bound is
# loose enough to not false-positive on a realistic worst-case.
SUM_OF_CAPS = (REFEREE_CAP + SUSPENSION_CAP + INJURY_CAP_EXTREME
               + LINEUP_CAP + FORM_CAP)


# ── Shared tempdir / patch helper ───────────────────────────────────────
class _PatchedFeeds:
    """Context manager: writes feeds into a tempdir and monkeypatches the
    apply_matchday_adjustments module's LIVE / DASH / LOG_PATH / OUT_PATH so
    the orchestrator reads from the tempdir.

    Pattern copied from tests/live/test_apply_matchday_adjustments.py:_TempFeeds
    so the contract here matches the existing harness."""

    def __init__(self, feeds: dict):
        self.feeds = feeds
        self.tmp = None
        self._patches = []

    def __enter__(self) -> Path:
        self.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmp.name)
        for name, payload in self.feeds.items():
            (tmp_path / name).write_text(json.dumps(payload))
        self._patches = [
            patch.object(amd, "LIVE", tmp_path),
            patch.object(amd, "DASH", tmp_path),
            patch.object(amd, "LOG_PATH", tmp_path / "matchday_intelligence_log.jsonl"),
            patch.object(amd, "OUT_PATH", tmp_path / "matchday_intelligence.json"),
        ]
        for p in self._patches:
            p.start()
        amd._STATE_CACHE = None
        return tmp_path

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        amd._STATE_CACHE = None
        self.tmp.cleanup()


def _team_total(state: dict, team: str, match_id=None) -> float:
    """Sum `total_elo_adjustment` for `team` across one match_id (or
    tournament-wide if `match_id is None`)."""
    total = 0.0
    for entry in state["active_adjustments"]:
        if entry["team"] != team:
            continue
        # Tournament-wide entries (match_id is None) apply universally.
        if entry["match_id"] is None or entry["match_id"] == match_id:
            total += entry["total_elo_adjustment"]
    return total


def _components_by_type(state: dict, team: str, match_id=None) -> dict:
    """Return {component_type: [components...]} for the (team, match_id) bucket."""
    out: dict[str, list] = {}
    for entry in state["active_adjustments"]:
        if entry["team"] != team:
            continue
        if entry["match_id"] != match_id:
            continue
        for c in entry["components"]:
            out.setdefault(c["type"], []).append(c)
    return out


# ── Invariant 1: no double-counting injury+suspension for same player ──
class TestNoDoubleCountInjuryAndSuspension:
    """If the same player appears in BOTH the injury snapshot AND the
    suspension snapshot, the orchestrator must NOT add both penalties
    to the team's adjustment. SUSPENSION WINS (hard rule — the player
    CANNOT play; injury is a probability the player can't play). The
    injury layer's contribution for that one match is credited back via
    a per-match `suspension_dedup_credit` component, leaving tournament-
    wide injury intact for matches where the player is NOT suspended.

    Dedup is keyed by (team, match_id, player_name) — see
    apply_matchday_adjustments._apply_injury_suspension_dedup."""

    def test_same_player_injury_and_suspension_does_not_double_count(self):
        """France: Mbappé in both injury overlay (-30 → -30 within
        INJURY_CAP_EXTREME 35) AND suspension (-3 for m=73). Expected:
        France's adjustment for m=73 should equal the suspension
        contribution alone (suspension wins), NOT injury + suspension
        stacked. Tournament-wide (no match_id) still carries the full
        injury overlay so other matches are unaffected."""
        feeds = {
            "team_adjustments.json": {"adjustments": [
                {"team": "France", "adjustment_elo": -30.0,
                 "status": "confirmed_out", "player": "Kylian Mbappé",
                 "expires_at": "2099-01-01T00:00:00+00:00"},
            ]},
            "suspensions_2026.json": {"suspensions": [
                {"match_id": 73, "team": "France",
                 "player": "Kylian Mbappé", "reason": "red_card",
                 "team_adjustment_elo": -3.0, "raw_elo": -3.0,
                 "cap_used": SUSPENSION_CAP,
                 "evidence_match_ids": [10],
                 "confidence": "high"},
            ]},
        }
        with _PatchedFeeds(feeds):
            france_m73 = amd.get_team_elo_adjustment("France", match_id=73)
            state = amd.build_adjustments_state()
        # Expected: only the suspension fires for this match (Mbappé can't
        # be both injured AND suspended at once for the same fixture; the
        # injury layer defers to the more-certain suspension signal).
        assert france_m73 == pytest.approx(-3.0, abs=0.01), (
            f"Expected France @ m=73 = -3.0 (suspension only, no double "
            f"count with injury overlay), got {france_m73}."
        )
        # The dedup decision must surface in degradation_warnings so the
        # dashboard / audit log can show WHY the m=73 total differs from
        # naive injury+suspension.
        dedup_warnings = [
            w for w in state.get("degradation_warnings", [])
            if w.get("type") == "injury_suspension_dedup"
        ]
        assert len(dedup_warnings) == 1, (
            f"expected exactly one injury_suspension_dedup warning, got "
            f"{dedup_warnings}"
        )
        w = dedup_warnings[0]
        assert "Kylian Mbappé" in w["message"]
        assert "France" in w["message"]
        assert "m=73" in w["message"]
        # And the dedup credit must appear as a positive injury component
        # on the m=73 bucket so the audit trail shows the cancellation.
        m73_buckets = [
            x for x in state["active_adjustments"]
            if x["team"] == "France" and x["match_id"] == 73
        ]
        assert len(m73_buckets) == 1
        credit_components = [
            c for c in m73_buckets[0]["components"]
            if c.get("subtype") == "suspension_dedup_credit"
        ]
        assert len(credit_components) == 1
        assert credit_components[0]["capped_elo"] == pytest.approx(30.0, abs=0.01)
        assert credit_components[0]["player"] == "Kylian Mbappé"

    def test_dedup_only_fires_when_player_overlaps(self):
        """Sanity check: 3 different injured players + 1 different suspended
        player (no overlap) → all 4 contributions present, no dedup
        suppression. Dedup must NOT fire when the suspension player is not
        in the injury overlay roster."""
        feeds = {
            "team_adjustments.json": {"adjustments": [
                {"team": "Brazil", "adjustment_elo": -8.0,
                 "status": "confirmed_out", "player": "Neymar",
                 "expires_at": "2099-01-01T00:00:00+00:00"},
                {"team": "Brazil", "adjustment_elo": -5.0,
                 "status": "confirmed_out", "player": "Vinicius Jr",
                 "expires_at": "2099-01-01T00:00:00+00:00"},
                {"team": "Brazil", "adjustment_elo": -4.0,
                 "status": "confirmed_out", "player": "Raphinha",
                 "expires_at": "2099-01-01T00:00:00+00:00"},
            ]},
            "suspensions_2026.json": {"suspensions": [
                {"match_id": 42, "team": "Brazil",
                 "player": "Casemiro", "reason": "red_card",
                 "team_adjustment_elo": -3.0, "raw_elo": -3.0,
                 "cap_used": SUSPENSION_CAP,
                 "evidence_match_ids": [10],
                 "confidence": "high"},
            ]},
        }
        with _PatchedFeeds(feeds):
            brazil_m42 = amd.get_team_elo_adjustment("Brazil", match_id=42)
            state = amd.build_adjustments_state()
        # Tournament-wide injury (-8 + -5 + -4 = -17) + m=42 suspension (-3)
        # → total at m=42 is -20. NO dedup credit (Casemiro not in overlay).
        assert brazil_m42 == pytest.approx(-20.0, abs=0.01), (
            f"Expected Brazil @ m=42 = -20.0 (full injury -17 + suspension -3, "
            f"no overlap), got {brazil_m42}"
        )
        # Crucially: no dedup warning should fire.
        dedup_warnings = [
            w for w in state.get("degradation_warnings", [])
            if w.get("type") == "injury_suspension_dedup"
        ]
        assert dedup_warnings == [], (
            f"dedup must NOT fire when player names don't overlap, got "
            f"{dedup_warnings}"
        )
        # And no `suspension_dedup_credit` component anywhere.
        for entry in state["active_adjustments"]:
            for c in entry["components"]:
                assert c.get("subtype") != "suspension_dedup_credit", (
                    f"unexpected dedup credit component: {entry} / {c}"
                )

    def test_dedup_is_per_match_not_per_team_player(self):
        """A player suspended in m=73 AND tournament-wide injured →
        suspension wins for m=73, injury still applies for m=74 (and every
        other match). Dedup is per-(team, match_id, player), NOT per-
        (team, player). If we dropped the injury entirely, m=74 would
        silently lose the player's contribution."""
        feeds = {
            "team_adjustments.json": {"adjustments": [
                {"team": "Spain", "adjustment_elo": -10.0,
                 "status": "confirmed_out", "player": "Rodri",
                 "expires_at": "2099-01-01T00:00:00+00:00"},
            ]},
            "suspensions_2026.json": {"suspensions": [
                {"match_id": 73, "team": "Spain",
                 "player": "Rodri", "reason": "red_card",
                 "team_adjustment_elo": -3.0, "raw_elo": -3.0,
                 "cap_used": SUSPENSION_CAP,
                 "evidence_match_ids": [10],
                 "confidence": "high"},
            ]},
        }
        with _PatchedFeeds(feeds):
            spain_m73 = amd.get_team_elo_adjustment("Spain", match_id=73)
            spain_m74 = amd.get_team_elo_adjustment("Spain", match_id=74)
            state = amd.build_adjustments_state()
        # m=73: suspension wins (-3 only) — injury credited back.
        assert spain_m73 == pytest.approx(-3.0, abs=0.01), (
            f"Expected Spain @ m=73 = -3.0 (suspension only), got {spain_m73}"
        )
        # m=74: Rodri NOT suspended for this match → tournament-wide injury
        # (-10) still applies in full. The dedup is per-match.
        assert spain_m74 == pytest.approx(-10.0, abs=0.01), (
            f"Expected Spain @ m=74 = -10.0 (full injury overlay, no "
            f"suspension at m=74), got {spain_m74}"
        )
        # Exactly ONE dedup warning (for m=73 only, NOT m=74).
        dedup_warnings = [
            w for w in state.get("degradation_warnings", [])
            if w.get("type") == "injury_suspension_dedup"
        ]
        assert len(dedup_warnings) == 1
        assert "m=73" in dedup_warnings[0]["message"]
        assert "Rodri" in dedup_warnings[0]["message"]


# ── Invariant 2: bounded total adjustment per team (worst case) ────────
class TestBoundedTotalAdjustment:
    """A synthetic worst-case team (max injuries, max suspensions, GK swap,
    hostile referee, terrible form) must not compound above SUM_OF_CAPS,
    and each per-(team, match) bucket must respect AGGREGATE_MATCHDAY_CAP."""

    def test_worst_case_team_bounded_by_sum_of_caps(self):
        """Push every subsystem to (or past) its own cap for one team.
        - Injury overlay: -100 (clamps to -35 extreme cap)
        - Suspension: 3 reds (per-player -3, capped at SUSPENSION_CAP=-8)
        - Lineup: -50 (clamps to -20)
        - Referee: -50 (clamps to -8)
        - Stats proxy: 5 matches × -30 (clamps to per-match -8 then
                       group-total -20)
        The orchestrator should keep the per-(team, match) bucket inside
        AGGREGATE_MATCHDAY_CAP and the tournament-wide bucket inside
        the bigger SUM_OF_CAPS bound."""
        feeds = {
            "team_adjustments.json": {"adjustments": [
                {"team": "Doom", "adjustment_elo": -100.0,
                 "status": "confirmed_out",
                 "expires_at": "2099-01-01T00:00:00+00:00"},
            ]},
            "suspensions_2026.json": {"suspensions": [
                {"match_id": 99, "team": "Doom", "player": f"P{i}",
                 "reason": "red_card", "team_adjustment_elo": -3.0,
                 "raw_elo": -3.0, "cap_used": SUSPENSION_CAP,
                 "evidence_match_ids": [i], "confidence": "high"}
                for i in range(3)
            ]},
            "lineups_2026.json": {"lineups": [
                {"match_id": 99, "home": "Doom", "away": "Other",
                 "home_team_adjustment_elo": -50.0,
                 "away_team_adjustment_elo": 0.0,
                 "home_adjustment_reason": "wholesale rotation"},
            ]},
            "referee_2026.json": {"referee": [
                {"match_id": 99, "home_team": "Doom", "away_team": "Other",
                 "referee_name": "Worst Ref",
                 "home_team_adjustment_elo": -50.0,
                 "away_team_adjustment_elo": 0.0,
                 "n_matches": 200, "confidence": "high"},
            ]},
            "match_stats_2026.json": {"matches": [
                {"match_id": i, "status": "FT",
                 "home": "Doom", "away": f"Opp{i}",
                 "home_form_adjustment_elo": -30.0,
                 "away_form_adjustment_elo": 0.0,
                 "true_xg_available": False}
                for i in range(5)
            ]},
        }
        with _PatchedFeeds(feeds):
            state = amd.build_adjustments_state()
            doom_total = amd.get_team_elo_adjustment("Doom", match_id=99)
        # Every per-(team, match) bucket inside AGGREGATE_MATCHDAY_CAP.
        for entry in state["active_adjustments"]:
            if entry["team"] != "Doom":
                continue
            assert -AGGREGATE_CAP <= entry["total_elo_adjustment"] <= AGGREGATE_CAP, (
                f"Doom m={entry['match_id']} total "
                f"{entry['total_elo_adjustment']} outside ±{AGGREGATE_CAP}"
            )
        # The combined (m=99 match bucket + tournament-wide bucket) must
        # be inside SUM_OF_CAPS — the documented loosest bound.
        assert -SUM_OF_CAPS <= doom_total <= SUM_OF_CAPS, (
            f"Doom total {doom_total} outside sum-of-caps ±{SUM_OF_CAPS}"
        )

    def test_no_individual_cap_exceeded(self):
        """No per-subsystem capped_elo value should exceed its layer's cap.
        Catches the silent-write-bypass case where a loader rounding bug or
        a refactor accidentally widens a cap without updating the contract."""
        feeds = {
            "team_adjustments.json": {"adjustments": [
                {"team": "Cap", "adjustment_elo": -200.0,
                 "status": "confirmed_out",
                 "expires_at": "2099-01-01T00:00:00+00:00"},
            ]},
            "lineups_2026.json": {"lineups": [
                {"match_id": 1, "home": "Cap", "away": "X",
                 "home_team_adjustment_elo": -200.0,
                 "away_team_adjustment_elo": 0.0},
            ]},
            "referee_2026.json": {"referee": [
                {"match_id": 1, "home_team": "Cap", "away_team": "X",
                 "referee_name": "R", "home_team_adjustment_elo": -200.0,
                 "away_team_adjustment_elo": 0.0,
                 "n_matches": 200, "confidence": "high"},
            ]},
            "suspensions_2026.json": {"suspensions": [
                {"match_id": 1, "team": "Cap", "player": "Q",
                 "reason": "red_card",
                 "team_adjustment_elo": -200.0,   # provider sent garbage
                 "raw_elo": -200.0, "cap_used": SUSPENSION_CAP,
                 "evidence_match_ids": [0], "confidence": "high"},
            ]},
        }
        cap_by_type = {
            "injury": INJURY_CAP_EXTREME,
            "lineup": LINEUP_CAP,
            "referee": REFEREE_CAP,
            "suspension": SUSPENSION_CAP,
            "stats_proxy": FORM_CAP,
            "weather": amd.WEATHER_CAP,
        }
        with _PatchedFeeds(feeds):
            state = amd.build_adjustments_state()
        for entry in state["active_adjustments"]:
            for c in entry["components"]:
                t = c["type"]
                if t not in cap_by_type:
                    continue
                assert abs(c.get("capped_elo", 0.0)) <= cap_by_type[t] + 1e-6, (
                    f"{entry['team']} m={entry['match_id']} type={t} "
                    f"capped_elo={c['capped_elo']} exceeds layer cap "
                    f"{cap_by_type[t]}"
                )


# ── Invariant 3: best-case team reaches positive bound, not floored ────
class TestBestCaseAdjustment:
    """A team with friendly referee + great form (no negatives) should
    surface a positive net adjustment, NOT be silently floored at 0.

    Subsystems that can produce positive deltas:
      - Stats proxy (good form: positive home_form_adjustment_elo)
      - Referee (positive home_elo_bonus when reffing home)
      - Lineup adjustments are typically <= 0 (GK swap / rotation are
        penalties) so lineup contributes 0 in best case.

    Combined bound: REFEREE_CAP + FORM_CAP = 8 + 20 = 28.
    """

    def test_best_case_team_reaches_positive_bound(self):
        """Team with REFEREE_CAP+ referee bonus + maxed-out positive form
        across enough matches should net out near REFEREE_CAP + FORM_CAP."""
        feeds = {
            "referee_2026.json": {"referee": [
                # +50 raw clamps to +REFEREE_CAP at write time.
                {"match_id": 1, "home_team": "Sunshine", "away_team": "X",
                 "referee_name": "Best Ref",
                 "home_team_adjustment_elo": 50.0,
                 "away_team_adjustment_elo": 0.0,
                 "n_matches": 200, "confidence": "high"},
            ]},
            "match_stats_2026.json": {"matches": [
                # 3 matches × +8 each → first two fully count, third truncated to
                # +4 by STATS_CAP_TOURNAMENT_TOTAL=20.
                {"match_id": i, "status": "FT",
                 "home": "Sunshine", "away": f"Opp{i}",
                 "home_form_adjustment_elo": 8.0,
                 "away_form_adjustment_elo": 0.0,
                 "true_xg_available": False}
                for i in range(3)
            ]},
        }
        with _PatchedFeeds(feeds):
            tournament_wide = amd.get_team_elo_adjustment("Sunshine")
            m1_total = amd.get_team_elo_adjustment("Sunshine", match_id=1)
        # Tournament-wide stats-proxy aggregate = FORM_CAP (20).
        assert tournament_wide == pytest.approx(FORM_CAP, abs=0.01), (
            f"Sunshine tournament-wide should be FORM_CAP={FORM_CAP}, "
            f"got {tournament_wide}"
        )
        # @ m=1: REFEREE_CAP (clamped from +50) + tournament-wide form.
        # But because referee is a match-specific bucket and stats_proxy
        # is tournament-wide, the public API sums them: REFEREE_CAP +
        # FORM_CAP = 8 + 20 = 28.
        assert m1_total == pytest.approx(REFEREE_CAP + FORM_CAP, abs=0.01), (
            f"Sunshine @ m=1 should hit REFEREE_CAP+FORM_CAP "
            f"({REFEREE_CAP + FORM_CAP}), got {m1_total}"
        )
        # Crucially, NOT floored at 0.
        assert m1_total > 0
        assert tournament_wide > 0


# ── Invariant 4: no adjustment without input ──────────────────────────
class TestZeroAdjustmentWithoutInput:
    """A team absent from every snapshot must have adjustment = 0 exactly.
    Catches the bug class where a loader emits a phantom (team, None) row
    with capped_elo == 0 that still surfaces in the consolidated state
    and accidentally trips downstream consumers."""

    def test_unseen_team_has_zero_adjustment(self):
        """Ghana is not in any snapshot. get_team_elo_adjustment must
        return 0.0 for any match_id, tournament-wide or otherwise."""
        feeds = {
            "team_adjustments.json": {"adjustments": [
                {"team": "France", "adjustment_elo": -12.0,
                 "status": "confirmed_out",
                 "expires_at": "2099-01-01T00:00:00+00:00"},
            ]},
            "referee_2026.json": {"referee": [
                {"match_id": 1, "home_team": "Brazil", "away_team": "Italy",
                 "referee_name": "R", "home_team_adjustment_elo": 5.0,
                 "away_team_adjustment_elo": 0.0,
                 "n_matches": 50, "confidence": "medium"},
            ]},
        }
        with _PatchedFeeds(feeds):
            assert amd.get_team_elo_adjustment("Ghana") == 0.0
            assert amd.get_team_elo_adjustment("Ghana", match_id=1) == 0.0
            assert amd.get_team_elo_adjustment("Ghana", match_id=999) == 0.0
            # And Ghana must not appear in the consolidated state at all.
            state = amd.build_adjustments_state()
            ghana = [x for x in state["active_adjustments"] if x["team"] == "Ghana"]
            assert ghana == [], (
                f"Ghana surfaced in state with no input: {ghana}"
            )

    def test_empty_snapshots_produce_zero_for_all_teams(self):
        """With every snapshot empty, every probed team gets 0.0 — not None,
        not raise."""
        feeds = {
            "injuries_2026.json": {"teams": {}},
            "lineups_2026.json": {"lineups": []},
            "referee_2026.json": {"referee": []},
            "suspensions_2026.json": {"suspensions": []},
            "match_stats_2026.json": {"matches": []},
        }
        with _PatchedFeeds(feeds):
            for team in ("Spain", "England", "USA", "Mexico", "Argentina"):
                assert amd.get_team_elo_adjustment(team) == 0.0
                assert amd.get_team_elo_adjustment(team, match_id=1) == 0.0


# ── Invariant 5: referee bonus is one-sided (documented mirror rule) ───
class TestRefereeMirrorRule:
    """The referee model is documented as ONE-SIDED: only the home team
    receives a bonus; the away side is always 0.0. A symmetric bonus would
    double-count the bias (referee_adjustments.py:20-21).

    The original issue spec proposes a symmetric mirror (+5 home → -5 away),
    but the production model deliberately rejects that. We pin the actual
    one-sided contract here so a future "fix" toward symmetric isn't a
    silent behaviour change."""

    def test_referee_bonus_is_one_sided_home_only(self):
        """+5 home referee bonus surfaces as +5 for home, 0 for away.
        This matches referee_adjustments.compute_referee_entry where
        `away_team_adjustment_elo` is hardcoded to 0.0 (line 131)."""
        feeds = {"referee_2026.json": {"referee": [
            {"match_id": 1, "home_team": "Home", "away_team": "Away",
             "referee_name": "+5 Ref",
             "home_team_adjustment_elo": 5.0,
             "away_team_adjustment_elo": 0.0,  # production always writes 0
             "n_matches": 100, "confidence": "high"},
        ]}}
        with _PatchedFeeds(feeds):
            home = amd.get_team_elo_adjustment("Home", match_id=1)
            away = amd.get_team_elo_adjustment("Away", match_id=1)
        assert home == pytest.approx(5.0, abs=0.01)
        assert away == 0.0, (
            "Referee bonus must be one-sided (away always 0). "
            "A symmetric mirror would double-count the bias."
        )

    def test_negative_referee_bonus_still_one_sided(self):
        """-3 home doesn't become +3 away — the model stays asymmetric
        for negative bonuses too."""
        feeds = {"referee_2026.json": {"referee": [
            {"match_id": 1, "home_team": "Home", "away_team": "Away",
             "referee_name": "-3 Ref",
             "home_team_adjustment_elo": -3.0,
             "away_team_adjustment_elo": 0.0,
             "n_matches": 100, "confidence": "high"},
        ]}}
        with _PatchedFeeds(feeds):
            home = amd.get_team_elo_adjustment("Home", match_id=1)
            away = amd.get_team_elo_adjustment("Away", match_id=1)
        assert home == pytest.approx(-3.0, abs=0.01)
        assert away == 0.0


# ── Invariant 6: Σ p_champion stays at 1 after matchday adjustments ────
class TestSigmaPchampionStaysAtOne:
    """The matchday pipeline writes to dashboard/matchday_intelligence.json
    and data/live/matchday_intelligence_log.jsonl. It does NOT touch
    data/processed/predictions_live.json (that's owned by 03_simulate.py).

    So running matchday adjustments must NOT perturb Σ p_champion. We pin
    this with the light-weight test: assert that the orchestrator does not
    write to predictions_live.json AND that the predictions blob (if
    present) still passes the strict Σ-invariant after the synthetic
    pipeline runs.

    Why light-weight: running the full simulator (heavy, multi-minute) just
    to confirm Σ=1 would blow the <5s test budget. The integration contract
    we care about is that adjustment_elo only enters via the simulator's
    `elo_eff_base` read of `get_team_elo_adjustment` — and the simulator
    re-normalises p_champion in `_normalise_advancement_probs` after every
    Monte Carlo cell. So as long as the matchday layer doesn't write
    predictions_live.json directly, the invariant holds.
    """

    def test_matchday_pipeline_does_not_touch_predictions_blob(self):
        """The orchestrator writes to OUT_PATH (matchday_intelligence.json)
        and LOG_PATH (audit log). It does NOT write to predictions_live.json.
        Running the synthetic pipeline must leave the file's mtime alone."""
        predictions = ROOT / "data" / "processed" / "predictions_live.json"
        if not predictions.exists():
            pytest.skip("predictions_live.json not present — run 03_simulate first")
        mtime_before = predictions.stat().st_mtime
        feeds = {
            "team_adjustments.json": {"adjustments": [
                {"team": "France", "adjustment_elo": -12.0,
                 "status": "confirmed_out",
                 "expires_at": "2099-01-01T00:00:00+00:00"},
            ]},
        }
        with _PatchedFeeds(feeds):
            amd.write_state_and_log()
        mtime_after = predictions.stat().st_mtime
        assert mtime_before == mtime_after, (
            "matchday pipeline modified predictions_live.json — this would "
            "break the simulator's Σ p_champion invariant"
        )

    def test_invariants_gate_passes_post_pipeline(self):
        """Run check_invariants.py against the real predictions blob AFTER
        a synthetic matchday cycle. Exit 0 = Σ p_champion still at 1.0 ± 1e-6,
        all 48 teams present, all probabilities finite and in [0, 1]."""
        predictions = ROOT / "data" / "processed" / "predictions_live.json"
        if not predictions.exists():
            pytest.skip("predictions_live.json not present — run 03_simulate first")
        feeds = {
            "team_adjustments.json": {"adjustments": [
                {"team": "France", "adjustment_elo": -12.0,
                 "status": "confirmed_out",
                 "expires_at": "2099-01-01T00:00:00+00:00"},
            ]},
        }
        with _PatchedFeeds(feeds):
            amd.write_state_and_log()
            # Verify the in-memory state is well-formed (also: Σ over the
            # consolidated bucket totals is finite — every team's
            # contribution is a real number).
            state = amd.build_adjustments_state()
            running = 0.0
            for entry in state["active_adjustments"]:
                v = entry["total_elo_adjustment"]
                assert math.isfinite(v)
                running += v
            assert math.isfinite(running)
        # Now exercise the real Σ-invariants gate.
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_invariants.py"),
             str(predictions)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, (
            f"check_invariants failed post-pipeline: exit={result.returncode}, "
            f"stderr={result.stderr!r}"
        )

    def test_adjustment_elo_does_not_appear_in_p_champion_path(self):
        """Light-weight contract check: `adjustment_elo` is consumed by
        the simulator at `elo_eff_base` (single integration point per the
        module docstring). It does NOT enter the p_champion path directly.

        Asserted by inspecting the module API: `get_team_elo_adjustment`
        returns a float that the simulator adds to the Elo BEFORE the
        Monte Carlo — the p_champion field is computed AFTER the sim
        normalises advancement probabilities. So the matchday layer
        cannot push p_champion out of [0, 1] by construction."""
        # Public API contract: returns a float, never None, never NaN.
        with _PatchedFeeds({}):
            v = amd.get_team_elo_adjustment("Spain")
            assert isinstance(v, float)
            assert math.isfinite(v)
            assert v == 0.0  # empty feed → zero adjustment

        # The orchestrator output structure does NOT have a `p_champion`
        # field — it has `total_elo_adjustment`, which is consumed
        # separately by the simulator's Elo path.
        with _PatchedFeeds({}):
            state = amd.build_adjustments_state()
        assert "p_champion" not in state
        for entry in state["active_adjustments"]:
            assert "p_champion" not in entry, (
                f"adjustment entry leaked p_champion field: {entry}"
            )
