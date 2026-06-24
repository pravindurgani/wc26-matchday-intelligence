"""
Adversarial unit tests for scripts/live/injury_adjustments.py +
scripts/live/fetch_injuries.normalise_records.

Goal: each test feeds a subtly malformed input and asserts either
  - LOUD:   pytest.raises / asserts a warning was emitted / sentinel returned, OR
  - SILENT: xfail(strict=True) marking a real silent-failure bug with a one-line
            fix proposal referenced in the marker reason (file:line).

Run:
    python3 -m pytest tests/live/test_injury_adversarial.py -v
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

import injury_adjustments  # noqa: E402
import fetch_injuries  # noqa: E402
from injury_adjustments import (  # noqa: E402
    DEFAULT_TIER,
    classify_api_type,
    classify_tier,
    discounted_elo,
    net_injury_elo,
    normalize_player_name,
    tier_elo,
)


# ── Factory helpers ──────────────────────────────────────────────────────

def _record(team: str = "France", name: str = "Player A",
            type_: str | None = "Missing Fixture",
            reason: str = "Knee", fixture_id: int = 1001) -> dict:
    """Well-formed baseline API-Football injury record."""
    return {
        "team": {"name": team},
        "player": {"name": name, "type": type_, "reason": reason},
        "fixture": {"id": fixture_id},
    }


# ============================================================
# LOUD cases — code raises / warns / returns sentinel
# ============================================================

class TestInjuryLoudFailures:
    """Inputs that DO surface — exceptions, warnings, or explicit sentinels."""

    def test_record_with_player_as_list_emits_warning(self):
        """Wrong type for player block (list, not dict). After Wave-B R4 the
        fix that drops records with a missing player block also catches
        wrong-typed player blocks (anything that is not a dict). The record
        is dropped + a skipped_bad_record warning is emitted instead of
        crashing the orchestrator with an AttributeError."""
        bad = [{"team": {"name": "France"},
                "player": ["Mbappe"],  # wrong type
                "fixture": {"id": 1}}]
        teams, warnings = fetch_injuries.normalise_records(
            bad, wc_teams={"France"})
        assert teams == {}
        assert any(w["type"] == "skipped_bad_record" for w in warnings), (
            f"Expected skipped_bad_record warning; got {warnings}")

    def test_record_with_team_as_string_raises(self):
        """team should be {'name': ...}; passing a bare string breaks .get()."""
        bad = [{"team": "France",
                "player": {"name": "X", "type": "Missing Fixture"},
                "fixture": {"id": 1}}]
        with pytest.raises(AttributeError):
            fetch_injuries.normalise_records(bad, wc_teams={"France"})

    def test_record_with_numeric_type_raises(self):
        """player.type as int. Pre-Wave-B-R4 this raised AttributeError
        because `(-1).strip()` blew up; the Bug #8 type guard now raises
        TypeError on any non-str/non-None input. Still loud, just a more
        descriptive exception class."""
        bad = [{"team": {"name": "France"},
                "player": {"name": "X", "type": -1},
                "fixture": {"id": 1}}]
        with pytest.raises(TypeError):
            fetch_injuries.normalise_records(bad, wc_teams={"France"})

    def test_records_top_level_none_raises(self):
        """records=None — must blow up rather than silently emit empty teams."""
        with pytest.raises(TypeError):
            fetch_injuries.normalise_records(None, wc_teams={"France"})

    def test_record_entry_is_none_raises(self):
        """A None entry inside the records list — .get() fails fast."""
        with pytest.raises(AttributeError):
            fetch_injuries.normalise_records([None], wc_teams={"France"})

    def test_record_entry_is_string_raises(self):
        """Record is a bare string — .get() fails fast."""
        with pytest.raises(AttributeError):
            fetch_injuries.normalise_records(["junk"], wc_teams={"France"})

    def test_unknown_team_emits_filter_warning(self):
        """A team not in wc_teams set is dropped AND a warning is logged."""
        bad = [_record(team="UNKNOWN")]
        teams, warnings = fetch_injuries.normalise_records(
            bad, wc_teams={"France"})
        assert teams == {}
        assert any(w["type"] == "filter_non_wc" for w in warnings), (
            "Non-WC team must surface a filter_non_wc warning, not silently vanish")

    def test_missing_team_name_emits_skipped_warning(self):
        """team.name missing → record dropped AND skipped_bad_record warning."""
        bad = [{"team": {},
                "player": {"name": "X", "type": "Missing Fixture"},
                "fixture": {"id": 1}}]
        teams, warnings = fetch_injuries.normalise_records(
            bad, wc_teams={"France"})
        assert teams == {}
        assert any(w["type"] == "skipped_bad_record" for w in warnings)

    def test_classify_api_type_with_unknown_string_returns_safe_sentinel(self):
        """Unknown status fails closed to confirmed_out (the sentinel)."""
        assert classify_api_type("ThisIsNotARealStatus") == "confirmed_out"

    def test_tier_elo_with_unknown_tier_returns_zero_sentinel(self):
        """Unknown tier → 0.0 (defensive: don't penalise on garbage)."""
        assert tier_elo("tier_made_up") == 0.0

    def test_discounted_elo_with_unknown_status_returns_zero_sentinel(self):
        """Unknown status earns 0 — pinned by the existing contract."""
        assert discounted_elo("tier_1_star", "maybe_playing") == 0.0


# ============================================================
# SILENT cases — code returns a confident-looking but wrong answer.
# Each is xfail(strict=True): if the code is later fixed to fail loudly
# the test flips to passing and gets noticed.
# ============================================================

class TestInjurySilentFailures:
    """Previously xfail-tracked silent bugs — now hardened by Wave-B Round 4.

    Each test below asserts the NEW loud behaviour (raises / warns / sentinel)
    instead of merely flagging the silent path. Production fixes live in
    scripts/live/fetch_injuries.py and scripts/live/injury_adjustments.py.
    """

    def test_record_missing_player_block_should_be_loud(self):
        """Bug #1 (Wave-B R4) — a record with NO `player` key used to coerce
        into a fake 'Unknown' player at DEFAULT_TIER (-12 Elo). Fix in
        fetch_injuries.normalise_records: drop the record and emit a
        skipped_bad_record warning."""
        bad = [{"team": {"name": "France"}, "fixture": {"id": 1}}]
        teams, warnings = fetch_injuries.normalise_records(
            bad, wc_teams={"France"})
        assert teams == {}, (
            f"Expected empty teams; got phantom 'Unknown' player: {teams}")
        assert any(w["type"] == "skipped_bad_record" for w in warnings), (
            f"Expected skipped_bad_record warning; got {warnings}")

    def test_empty_wc_teams_should_not_admit_everything(self):
        """Bug #2 (Wave-B R4) — empty/None wc_teams used to bypass the
        WC filter and admit every team. Fix in
        fetch_injuries.normalise_records: raise ValueError on empty set."""
        bad = [_record(team="Andorra")]
        with pytest.raises(ValueError):
            fetch_injuries.normalise_records(bad, wc_teams=set())

    def test_lowercase_team_should_match_or_warn(self):
        """Bug #3 (Wave-B R4) — 'france' used to silently drop as non-WC
        because the canonical set is case-sensitive. Fix in
        fetch_injuries.normalise_records: case-fold both sides for a
        membership probe and emit a team_case_mismatch warning."""
        bad = [_record(team="france")]
        teams, warnings = fetch_injuries.normalise_records(
            bad, wc_teams={"France"})
        # The record is dropped (we don't auto-canonicalise), but the
        # operator now gets a dedicated case-mismatch signal instead of
        # a vanilla filter_non_wc.
        assert teams == {}
        assert any(w.get("type") == "team_case_mismatch" for w in warnings), (
            f"Expected team_case_mismatch warning; got {warnings}")

    def test_duplicate_player_records_should_not_double_count(self):
        """Bug #4 (Wave-B R4) — duplicate (team, player) records used to
        stack penalties (-30 + -15 = -45 for Mbappé). Fix in
        fetch_injuries.normalise_records: dedup on (team, normalised name);
        keep the first occurrence and emit a duplicate_record warning."""
        dupe = [
            _record(team="France", name="Kylian Mbappé",
                    type_="Missing Fixture", fixture_id=1),
            _record(team="France", name="Kylian Mbappé",
                    type_="Questionable", fixture_id=2),
        ]
        teams, warnings = fetch_injuries.normalise_records(
            dupe, wc_teams={"France"})
        total = teams["France"]["total_elo_adjustment"]
        # Worst-case bound: a single confirmed_out at the highest tier we
        # support (tier_1_star = -30). Stacked penalties would be ≤ -45.
        assert total >= -30.0, (
            f"Double-counted to {total}; expected ≥ -30 after dedup")
        assert any(w.get("type") == "duplicate_record" for w in warnings), (
            f"Expected duplicate_record warning; got {warnings}")

    def test_normalize_player_name_with_list_should_reject(self):
        """Bug #5 (Wave-B R4) — normalize_player_name used to str()-coerce
        a list into '[a b]' silently. Fix: TypeError on non-str input."""
        with pytest.raises(TypeError):
            normalize_player_name(["Kylian", "Mbappe"])

    def test_net_injury_elo_with_nan_should_be_loud(self):
        """Bug #6 (Wave-B R4) — NaN elo used to propagate through unchanged
        and silently poison the team total. Fix: math.isfinite guard
        raising ValueError."""
        with pytest.raises(ValueError):
            net_injury_elo(float("nan"), None)

    def test_net_injury_elo_with_inf_should_be_loud(self):
        """Bug #7 (Wave-B R4) — same guard covers ±Inf."""
        with pytest.raises(ValueError):
            net_injury_elo(float("inf"), None)

    def test_classify_api_type_with_bytes_should_reject(self):
        """Bug #8 (Wave-B R4) — bytes payloads previously fell through
        APIFOOTBALL_TYPE_MAP (bytes-key never matches) and silently
        returned 'confirmed_out'. Fix: TypeError on non-str/non-None."""
        with pytest.raises(TypeError):
            classify_api_type(b"Questionable")

    def test_classify_tier_with_integer_should_reject(self):
        """Bug #9 (Wave-B R4) — integer player_name was truthy and flowed
        to normalize_player_name, silently returning DEFAULT_TIER. Fix:
        TypeError when player_name is not str/None."""
        with pytest.raises(TypeError):
            classify_tier(12345, "France")


# ============================================================
# Boundary cases that DO behave correctly — assert the contract.
# ============================================================

class TestInjuryBoundaryContract:
    """Inputs that are weird but the code handles them deliberately."""

    def test_empty_records_returns_empty_teams_no_warnings(self):
        teams, warnings = fetch_injuries.normalise_records(
            [], wc_teams={"France"})
        assert teams == {}
        assert warnings == []

    def test_single_record_yields_single_team_entry(self):
        teams, _ = fetch_injuries.normalise_records(
            [_record(team="France", name="Some Backup")],
            wc_teams={"France"})
        assert list(teams.keys()) == ["France"]
        assert len(teams["France"]["players"]) == 1

    def test_classify_api_type_strips_whitespace_then_resolves(self):
        """Whitespace-only is non-empty → flows to .strip() → empty str
        → fallback to confirmed_out. Documented defensive default."""
        assert classify_api_type("   ") == "confirmed_out"
        assert classify_api_type(" Missing Fixture ") == "confirmed_out"

    def test_classify_tier_with_missing_team_defaults_quietly(self):
        """Documented contract: None team → DEFAULT_TIER / 'default'."""
        assert classify_tier("Mbappe", None) == (DEFAULT_TIER, "default")
        assert classify_tier("Mbappe", "") == (DEFAULT_TIER, "default")

    def test_classify_tier_with_missing_name_defaults_quietly(self):
        assert classify_tier(None, "France") == (DEFAULT_TIER, "default")
        assert classify_tier("", "France") == (DEFAULT_TIER, "default")
