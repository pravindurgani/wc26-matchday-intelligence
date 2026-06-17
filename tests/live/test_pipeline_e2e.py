"""
test_pipeline_e2e.py — End-to-end smoke test for the matchday-adjustment
pipeline.

Why this exists
===============
Each subsystem (injury, lineup, referee, suspension, stats_proxy) has its
own adversarial + baseline tests. But no test runs the FULL orchestrator
end-to-end against a curated, mixed synthetic fixture set and asserts that
the consolidated state is sensible.

This file fills that gap. It builds a synthetic snapshot for every feed,
runs `build_adjustments_state` + `write_state_and_log`, and verifies the
per-team consolidated Elo adjustments are:

  - structurally valid (dict, every team present, no NaN / inf)
  - inside the documented sum-of-subsystem-caps bound
  - carrying the expected per-subsystem contribution (injury, lineup,
    referee) that we explicitly injected
  - paired with a downstream predictions blob that still passes
    `check_invariants` (Σ p_champion ≈ 1.0).

The orchestrator entry point is `apply_matchday_adjustments.build_adjustments_state`
(scripts/live/apply_matchday_adjustments.py:447), with `write_state_and_log`
as the side-effecting wrapper that emits the dashboard JSON + audit log.
There is no `apply_all` — the consolidation is a pure function that walks
the per-layer loaders.

ABSOLUTE CONSTRAINTS (also pinned in the issue description):
  - No prod code changes; tests live entirely under tests/live/.
  - AUTO_TIER_ACTIVE stays False (verified by an assertion below).
  - No network: synthetic fixtures only, written into a tempdir with the
    module's LIVE / DASH path constants monkeypatched.

Run:
    python3 -m pytest tests/live/test_pipeline_e2e.py -v
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

import apply_matchday_adjustments as amd  # noqa: E402
import injury_adjustments as inj  # noqa: E402
import lineup_adjustments as lna  # noqa: E402


# ── Documented caps and constants pulled from the live modules ──────────
# Recomputing them in the test would be cheating — these are the same
# names used in production, and a tightening / loosening of any cap is
# caught here.
REFEREE_CAP = amd.REFEREE_CAP                       # 8.0
SUSPENSION_CAP = amd.SUSPENSION_CAP                 # 8.0
INJURY_CAP = amd.INJURY_CAP_EXTREME                 # 35.0 (overlay path)
LINEUP_CAP = amd.LINEUP_CAP                         # 20.0
FORM_CAP = amd.STATS_CAP_TOURNAMENT_TOTAL           # 20.0
AGGREGATE_CAP = amd.AGGREGATE_MATCHDAY_CAP          # 35.0
GK_SWAP_ELO = lna.GK_SWAP_ELO                       # -8.0

# Sum-of-all-subsystem-caps — the loosest plausible bound BEFORE the
# aggregate cap clamps. We use it for `adjustment_elo` finite-bound checks.
MAX_BOUND = REFEREE_CAP + SUSPENSION_CAP + INJURY_CAP + LINEUP_CAP + FORM_CAP


# ── Synthetic fixture builders ──────────────────────────────────────────
@pytest.fixture
def schedule_two_fixtures() -> dict:
    """Two fixtures from the real WC schedule: m=73 and m=74 would be R32
    if/when they land. Until then we use the first two group games (m=1, m=2)
    so the snapshot is independent of the live schedule file."""
    return {
        "m73": {"match_id": 73, "home": "France", "away": "Senegal"},
        "m74": {"match_id": 74, "home": "Brazil", "away": "Croatia"},
    }


@pytest.fixture
def injury_snapshot() -> dict:
    """Two teams carry injuries; everyone else is clean.
      - France: Mbappé out (tier_1_star: -30 raw, capped at INJURY_CAP_NORMAL=-25).
      - Senegal: tier_2_starter (-12).
      The injury_2026.json schema is `{"teams": {team: {"total_elo_adjustment", "players": [...]}}}`
      (apply_matchday_adjustments.py:136-148)."""
    return {
        "teams": {
            "France": {
                "total_elo_adjustment": -30.0,
                "players": [{"name": "Kylian Mbappé", "elo": -30.0,
                             "tier": "tier_1_star", "status": "confirmed_out"}],
            },
            "Senegal": {
                "total_elo_adjustment": -12.0,
                "players": [{"name": "Generic Starter", "elo": -12.0,
                             "tier": "tier_2_starter", "status": "confirmed_out"}],
            },
        }
    }


@pytest.fixture
def lineup_snapshot() -> dict:
    """One fixture (m=73) with a GK swap (Brazil home swaps GK → home_adj=GK_SWAP_ELO),
    one fixture with normal XI (m=74, zero adj — display only)."""
    return {
        "lineups": [
            {
                "match_id": 73, "home": "France", "away": "Senegal",
                "home_team_adjustment_elo": GK_SWAP_ELO,   # -8.0 home GK swap
                "away_team_adjustment_elo": 0.0,
                "home_adjustment_reason": "GK swap",
            },
            {
                "match_id": 74, "home": "Brazil", "away": "Croatia",
                "home_team_adjustment_elo": 0.0,
                "away_team_adjustment_elo": 0.0,
            },
        ]
    }


@pytest.fixture
def referee_snapshot() -> dict:
    """One referee with home_elo_bonus=+5 (under REFEREE_CAP), one with -3.
    Schema mirrors weather_2026.json (apply_matchday_adjustments.py:237-269)."""
    return {
        "referee": [
            {
                "match_id": 73, "home_team": "France", "away_team": "Senegal",
                "referee_name": "Friendly Ref",
                "home_team_adjustment_elo": 5.0,
                "away_team_adjustment_elo": 0.0,
                "n_matches": 50, "confidence": "medium",
                "reason": "ref_home_bias",
            },
            {
                "match_id": 74, "home_team": "Brazil", "away_team": "Croatia",
                "referee_name": "Strict Ref",
                "home_team_adjustment_elo": -3.0,
                "away_team_adjustment_elo": 0.0,
                "n_matches": 50, "confidence": "medium",
                "reason": "ref_home_bias",
            },
        ]
    }


@pytest.fixture
def suspension_snapshot() -> dict:
    """One player with a red card → next-match ban (Croatia, m=74).
    `team_adjustment_elo` is the per-player CAPPED value (PER_SUSPENSION_ELO
    is -3.0 in suspension_tracker.py, well under SUSPENSION_CAP=8.0).

    The 'accumulated yellows (1 yellow)' branch is omitted on purpose —
    suspension_tracker only emits a row when YELLOW_THRESHOLD (2) is hit;
    a single yellow has zero downstream effect."""
    return {
        "suspensions": [
            {
                "match_id": 74, "team": "Croatia",
                "player": "Generic Defender",
                "reason": "red_card",
                "team_adjustment_elo": -3.0,
                "raw_elo": -3.0,
                "cap_used": SUSPENSION_CAP,
                "evidence_match_ids": [10],
                "confidence": "high",
                "source": "fetch_results_events",
            }
        ]
    }


@pytest.fixture
def stats_snapshot() -> dict:
    """Realistic FT box scores for both fixtures; per-team form deltas under
    STATS_CAP_PER_MATCH (8) so nothing is clamped at write-time.

    Note: stats_proxy adjustments are tournament-wide (match_id=None in the
    consolidated dict) and aggregate per team across all FT matches."""
    return {
        "matches": [
            {
                "match_id": 73, "status": "FT",
                "home": "France", "away": "Senegal",
                "home_form_adjustment_elo": 2.0,
                "away_form_adjustment_elo": -1.5,
                "true_xg_available": False,
            },
            {
                "match_id": 74, "status": "FT",
                "home": "Brazil", "away": "Croatia",
                "home_form_adjustment_elo": 4.0,
                "away_form_adjustment_elo": -2.0,
                "true_xg_available": False,
            },
        ]
    }


@pytest.fixture
def all_feeds(injury_snapshot, lineup_snapshot, referee_snapshot,
              suspension_snapshot, stats_snapshot) -> dict:
    """The complete feed bundle — one entry per JSON file the orchestrator reads."""
    return {
        "injuries_2026.json": injury_snapshot,
        "lineups_2026.json": lineup_snapshot,
        "referee_2026.json": referee_snapshot,
        "suspensions_2026.json": suspension_snapshot,
        "match_stats_2026.json": stats_snapshot,
    }


@pytest.fixture
def patched_pipeline(all_feeds):
    """Context manager that writes the synthetic feeds into a tempdir and
    monkeypatches the LIVE / DASH / LOG_PATH / OUT_PATH constants on the
    apply_matchday_adjustments module so the orchestrator reads from the
    tempdir rather than the real repo state.

    Yields the tempdir path so tests can read the dashboard JSON / audit log."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_path = Path(tmp_str)
        for name, payload in all_feeds.items():
            (tmp_path / name).write_text(json.dumps(payload))

        patches = [
            patch.object(amd, "LIVE", tmp_path),
            patch.object(amd, "DASH", tmp_path),
            patch.object(amd, "LOG_PATH", tmp_path / "matchday_intelligence_log.jsonl"),
            patch.object(amd, "OUT_PATH", tmp_path / "matchday_intelligence.json"),
        ]
        for p in patches:
            p.start()
        amd._STATE_CACHE = None
        try:
            yield tmp_path
        finally:
            for p in patches:
                p.stop()
            amd._STATE_CACHE = None


# ── Part A: end-to-end smoke ────────────────────────────────────────────
class TestPipelineEndToEnd:
    """Run the full orchestrator pipeline on synthetic inputs and assert
    every plausibility check holds."""

    def test_auto_tier_active_stays_false(self):
        """Hard guard: AUTO_TIER_ACTIVE must remain False — flipping it
        live without the audit on calibrated player_stats would silently
        change every injury tier classification in the next cron run."""
        assert inj.AUTO_TIER_ACTIVE is False, (
            "AUTO_TIER_ACTIVE flipped to True — must remain False until the "
            "auto_tier_diff calibration audit signs off (CORRECTIONS.md §7)."
        )

    def test_orchestrator_returns_well_formed_state(self, patched_pipeline):
        """`build_adjustments_state` returns a dict with the documented shape —
        not None, no exception."""
        state = amd.build_adjustments_state()
        assert state is not None
        assert isinstance(state, dict)
        assert "active_adjustments" in state
        assert "summary" in state
        assert "caps" in state
        assert state["schema_version"] == 1

    def test_every_input_team_present_in_output(self, patched_pipeline):
        """Every team that appears in ANY input snapshot must surface in
        the consolidated state, even if its net Elo is zero."""
        state = amd.build_adjustments_state()
        teams_in_state = {x["team"] for x in state["active_adjustments"]}
        # Teams the synthetic snapshots inject SOME signal for:
        expected = {"France", "Senegal", "Brazil", "Croatia"}
        # Note: a team with all-zero contributions is dropped by the loaders
        # (they `continue` on capped_elo == 0). What we assert is the
        # weaker invariant: every team WITH a non-zero subsystem input
        # surfaces in the output.
        assert expected.issubset(teams_in_state), (
            f"Missing teams in output: {expected - teams_in_state}"
        )

    def test_every_team_adjustment_is_finite(self, patched_pipeline):
        """No NaN / inf escapes the orchestrator — all `total_elo_adjustment`
        values are finite numbers."""
        import math
        state = amd.build_adjustments_state()
        for entry in state["active_adjustments"]:
            val = entry["total_elo_adjustment"]
            assert isinstance(val, (int, float)), (
                f"non-numeric adjustment for {entry['team']}: {val!r}"
            )
            assert math.isfinite(val), (
                f"non-finite adjustment for {entry['team']}: {val!r}"
            )

    def test_adjustment_within_documented_sum_of_caps(self, patched_pipeline):
        """`total_elo_adjustment` must fall inside [-MAX_BOUND, +MAX_BOUND]
        where MAX_BOUND = REFEREE_CAP + SUSPENSION_CAP + INJURY_CAP +
        LINEUP_CAP + FORM_CAP. This is the loosest theoretical bound BEFORE
        the AGGREGATE_MATCHDAY_CAP per-match clamp kicks in."""
        state = amd.build_adjustments_state()
        for entry in state["active_adjustments"]:
            val = entry["total_elo_adjustment"]
            assert -MAX_BOUND <= val <= MAX_BOUND, (
                f"{entry['team']} m={entry['match_id']} adjustment "
                f"{val} outside ±{MAX_BOUND} (sum-of-caps bound)"
            )
            # Tighter per-(team, match) bound: AGGREGATE_MATCHDAY_CAP.
            assert -AGGREGATE_CAP <= val <= AGGREGATE_CAP, (
                f"{entry['team']} m={entry['match_id']} adjustment "
                f"{val} outside ±{AGGREGATE_CAP} (aggregate matchday cap)"
            )

    def test_mbappe_out_drives_negative_injury_contribution_for_france(
            self, patched_pipeline):
        """The synthetic snapshot puts Mbappé (-30 raw) on France. After the
        normal injury cap (25) the contribution should be -25 — strictly
        negative and non-zero — and routed to the (France, None) bucket
        (tournament-wide)."""
        france_total = amd.get_team_elo_adjustment("France")
        assert france_total < 0, f"France adjustment should be negative, got {france_total}"
        # The injury layer alone caps at -25 (INJURY_CAP_NORMAL); the only
        # other tournament-wide layer that touches France is stats_proxy
        # (+2.0 home form). So France's tournament-wide net should be
        # exactly -25 + 2.0 = -23.0 (per `match_id=None` aggregation).
        # We use `match_id=None` to isolate the tournament-wide buckets:
        france_tw = amd.get_team_elo_adjustment("France", match_id=None)
        # France gets injury (-25, capped from -30) and stats_proxy (+2.0) in
        # the tournament-wide bucket. Sum = -23.0.
        assert france_tw == pytest.approx(-23.0, abs=0.01), (
            f"France tournament-wide adjustment should be -23.0 "
            f"(injury -25 + stats_proxy +2.0), got {france_tw}"
        )

    def test_gk_swap_drives_lineup_contribution_on_m73(self, patched_pipeline):
        """France (home @ m=73) has a synthetic GK swap of GK_SWAP_ELO=-8.
        The lineup loader keys per (team, match_id), so France at m=73 must
        receive that -8 in its match-specific bucket on top of any
        tournament-wide layers."""
        state = amd.build_adjustments_state()
        # Pluck the (France, 73) bucket: that should contain ONE lineup
        # component AND one referee component (+5) for a sum of -3.
        france_m73 = [x for x in state["active_adjustments"]
                      if x["team"] == "France" and x["match_id"] == 73]
        assert len(france_m73) == 1
        comps = france_m73[0]["components"]
        lineup_comps = [c for c in comps if c["type"] == "lineup"]
        assert len(lineup_comps) == 1
        assert lineup_comps[0]["capped_elo"] == pytest.approx(GK_SWAP_ELO)

    def test_friendly_referee_drives_positive_contribution(self, patched_pipeline):
        """The synthetic +5 home_elo_bonus on the m=73 referee must show up
        as a +5 referee component on the France (home) bucket, under
        REFEREE_CAP=8."""
        state = amd.build_adjustments_state()
        france_m73 = [x for x in state["active_adjustments"]
                      if x["team"] == "France" and x["match_id"] == 73]
        assert len(france_m73) == 1
        ref_comps = [c for c in france_m73[0]["components"] if c["type"] == "referee"]
        assert len(ref_comps) == 1
        assert ref_comps[0]["capped_elo"] == pytest.approx(5.0)
        assert abs(ref_comps[0]["capped_elo"]) <= REFEREE_CAP

    def test_suspension_routes_to_correct_team_and_match(self, patched_pipeline):
        """The synthetic red-card suspension for Croatia at m=74 must
        surface as a single suspension component on the (Croatia, 74) bucket
        with team_adjustment_elo = -3.0 (PER_SUSPENSION_ELO)."""
        state = amd.build_adjustments_state()
        croatia_m74 = [x for x in state["active_adjustments"]
                       if x["team"] == "Croatia" and x["match_id"] == 74]
        assert len(croatia_m74) == 1
        sus_comps = [c for c in croatia_m74[0]["components"] if c["type"] == "suspension"]
        assert len(sus_comps) == 1
        # PER_SUSPENSION_ELO is -3.0 (under SUSPENSION_CAP).
        assert sus_comps[0]["capped_elo"] == pytest.approx(-3.0)

    def test_full_pipeline_writes_dashboard_and_audit_log(self, patched_pipeline):
        """write_state_and_log must atomically write the dashboard JSON AND
        append exactly one JSONL audit record."""
        tmp = patched_pipeline
        state = amd.write_state_and_log()
        out = tmp / "matchday_intelligence.json"
        log = tmp / "matchday_intelligence_log.jsonl"
        assert out.exists(), "dashboard JSON not written"
        assert log.exists(), "audit log not written"
        # The on-disk dashboard payload matches the returned state.
        on_disk = json.loads(out.read_text())
        assert on_disk["summary"] == state["summary"]
        # Exactly one audit record was appended.
        lines = log.read_text().strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert "ts" in rec and "active_adjustments" in rec

    def test_invariants_gate_passes_against_real_predictions_blob(
            self, patched_pipeline):
        """The Σ-invariants gate (scripts/check_invariants.py) must exit 0
        against the real on-disk predictions blob even AFTER we've run a
        synthetic matchday pipeline. The orchestrator does NOT write to
        predictions_live.json (that's owned by 03_simulate.py), so the
        invariants stay intact.

        This pins the integration contract: matchday adjustments live in
        their own JSON file and don't break the downstream simulator's
        Σ-invariant."""
        # Run the synthetic pipeline (writes to tempdir, not the real path).
        amd.write_state_and_log()
        # Now exercise the invariants gate against the real predictions
        # blob the simulator already produced. If this blob doesn't exist
        # (e.g. fresh checkout, no sim ever run), skip rather than fail —
        # the matchday pipeline can't fix a missing simulator output.
        predictions = ROOT / "data" / "processed" / "predictions_live.json"
        if not predictions.exists():
            pytest.skip("predictions_live.json not present — run 03_simulate first")
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_invariants.py"),
             str(predictions)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, (
            f"check_invariants exited {result.returncode}; "
            f"stderr={result.stderr!r}"
        )
