"""
Adversarial unit tests for scripts/live/lineup_adjustments.py +
scripts/live/fetch_lineups.build_lineup_entry.

Goal: each test feeds a subtly malformed input and asserts either
  - LOUD:   pytest.raises / asserts a warning was emitted / sentinel returned, OR
  - SILENT: xfail(strict=True) marking a real silent-failure bug with a one-line
            fix proposal referenced in the marker reason (file:line).

Run:
    python3 -m pytest tests/live/test_lineup_adversarial.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

import lineup_adjustments  # noqa: E402
import fetch_lineups  # noqa: E402
from lineup_adjustments import (  # noqa: E402
    GK_SWAP_ELO,
    compute_lineup_delta_elo,
    extract_starting_xi,
)


# ── Factory helpers ──────────────────────────────────────────────────────

def _xi(gk_id: int | None, outfield_ids: list[int],
        position_by_id: dict[int, str] | None = None) -> dict:
    """Build a well-formed extract_starting_xi-style dict directly."""
    return {
        "gk_id": gk_id,
        "outfield_ids": set(outfield_ids),
        "raw_players": [],
        "position_by_id": position_by_id or {},
    }


def _side(start: list[dict], team_name: str = "France") -> dict:
    """Build a well-formed /fixtures/lineups side block."""
    return {"team": {"name": team_name}, "startXI": start}


def _player(pid: int | str | None, pos: str | None = "M",
            name: str = "P") -> dict:
    """Build a well-formed startXI entry."""
    return {"player": {"id": pid, "name": name, "pos": pos}}


# ============================================================
# LOUD cases — code raises / returns the documented no-baseline sentinel.
# ============================================================

class TestLineupLoudFailures:
    """Inputs that DO surface — exceptions or explicit zero-delta sentinels."""

    def test_extract_starting_xi_with_none_block_raises(self):
        """side_block=None — must raise rather than silently emit empty xi."""
        with pytest.raises(AttributeError):
            extract_starting_xi(None)

    def test_extract_starting_xi_with_string_startxi_raises(self):
        """startXI passed as a dict-of-players (not a list) breaks iteration."""
        with pytest.raises(AttributeError):
            extract_starting_xi({"startXI": "not-a-list"})

    def test_extract_starting_xi_with_none_entry_raises(self):
        """startXI containing a None entry — must blow up."""
        with pytest.raises(AttributeError):
            extract_starting_xi({"startXI": [None,
                                              _player(1, pos="G")]})

    def test_extract_starting_xi_with_nan_id_raises(self):
        """NaN id → int(NaN) raises ValueError. Good — wouldn't want NaN
        sneaking into the set."""
        with pytest.raises(ValueError):
            extract_starting_xi({"startXI": [
                _player(float("nan"), pos="G")]})

    def test_extract_starting_xi_with_garbage_string_id_raises(self):
        """id='abc' → int('abc') raises ValueError."""
        with pytest.raises(ValueError):
            extract_starting_xi({"startXI": [_player("abc", pos="G")]})

    def test_no_baseline_returns_zero_sentinel(self):
        """First recording for a team → 0 Elo, no reason — documented."""
        delta, reason = compute_lineup_delta_elo(None, _xi(1, [2, 3, 4]))
        assert delta == 0.0
        assert reason is None

    def test_compute_with_prior_as_list_raises(self):
        """prior_xi as wrong type (list) — must blow up, not silently 0."""
        with pytest.raises(AttributeError):
            compute_lineup_delta_elo([1, 2, 3], _xi(1, [2, 3]))

    def test_build_lineup_entry_missing_match_id_raises(self):
        """sched.m is required for downstream join — KeyError is correct."""
        with pytest.raises(KeyError):
            fetch_lineups.build_lineup_entry(
                {"home": "A", "away": "B"}, [], {})

    def test_build_lineup_entry_garbage_match_id_raises(self):
        """match_id must be coercible to int — non-numeric must fail."""
        with pytest.raises(ValueError):
            fetch_lineups.build_lineup_entry(
                {"m": "garbage", "home": "A", "away": "B"}, [], {})


# ============================================================
# SILENT cases — xfail(strict=True), each names file:line for the fix.
# ============================================================

class TestLineupSilentFailures:

    def test_two_starting_gks_should_be_loud(self):
        """Two players with pos='G' in startXI must raise rather than
        silently last-win (which would poison the next-match GK baseline).
        Fixed in lineup_adjustments.py:91-98 — count GK entries before
        the parse loop and raise ValueError('multiple_starting_gks')."""
        side = {"startXI": [
            _player(1, pos="G"),
            _player(2, pos="G"),
            _player(3, pos="D"),
        ]}
        with pytest.raises(ValueError, match="multiple_starting_gks"):
            extract_starting_xi(side)

    def test_ten_player_xi_should_be_loud(self):
        """A 10-player startXI (provider bug / pre-finalised lineup) must
        emit a 'malformed_xi_size' UserWarning rather than silently
        producing a 10-player XI that distorts the rotation delta.
        Fixed in lineup_adjustments.py:100-108."""
        with pytest.warns(UserWarning, match="malformed_xi_size"):
            extract_starting_xi({"startXI": [
                _player(i, pos="G" if i == 1 else "M") for i in range(1, 11)
            ]})

    def test_twelve_player_xi_should_be_loud(self):
        """Symmetric 12-player case — same warning."""
        with pytest.warns(UserWarning, match="malformed_xi_size"):
            extract_starting_xi({"startXI": [
                _player(i, pos="G" if i == 1 else "M") for i in range(1, 13)
            ]})

    def test_duplicate_player_ids_should_be_loud(self):
        """Same id listed twice in startXI must emit a 'duplicate_player_id'
        UserWarning AND not appear in both gk_id and outfield_ids. Fixed in
        lineup_adjustments.py:114-129 — tracked in `seen_ids` per parse."""
        with pytest.warns(UserWarning, match="duplicate_player_id"):
            xi = extract_starting_xi({"startXI": [
                _player(5, pos="G"),
                _player(5, pos="D"),
                _player(6, pos="M"),
            ]})
        # First entry wins the GK slot; the dropped repeat must NOT slide
        # into the outfield set (which is the silent-failure being fixed).
        assert 5 not in xi["outfield_ids"], (
            f"id=5 silently appears as both GK and outfield: {xi}")
        assert xi["gk_id"] == 5
        assert xi["outfield_ids"] == {6}

    def test_prior_gk_id_zero_should_still_detect_swap(self):
        """gk_id=0 is falsy but a valid integer — must still detect a swap.
        Fixed in lineup_adjustments.py:178-182 — guard switched from
        truthiness to `is not None`."""
        prior = _xi(0, [2, 3, 4, 5, 6, 7, 8, 9, 10, 11])
        curr = _xi(999, [2, 3, 4, 5, 6, 7, 8, 9, 10, 11])
        delta, reason = compute_lineup_delta_elo(prior, curr)
        assert delta == GK_SWAP_ELO, (
            f"GK swap missed for prior_gk=0 (delta={delta}, reason={reason})")
        assert reason is not None and "GK swap" in reason

    def test_string_vs_int_prior_outfield_ids_silent_full_rotation(self):
        """A prior_xi loaded from JSON with string IDs vs a current XI with
        int IDs must compare as identical (delta=0), not silently score
        -10 (full rotation). Fixed in lineup_adjustments.py:178-187 —
        both gk_id and outfield_ids are coerced via _coerce_id at the
        comparison point."""
        prior = {
            "gk_id": 1,
            "outfield_ids": {"2", "3", "4", "5", "6", "7", "8", "9", "10", "11"},
            "position_by_id": {},
        }
        curr = _xi(1, [2, 3, 4, 5, 6, 7, 8, 9, 10, 11])
        delta, reason = compute_lineup_delta_elo(prior, curr)
        assert delta == 0.0, (
            f"String/int mismatch silently scored {delta} with reason "
            f"{reason!r} (set diff treated identical IDs as full rotation)")
        assert reason is None


# ============================================================
# Boundary cases that ARE handled correctly — assert the contract.
# ============================================================

class TestLineupBoundaryContract:
    """Inputs that are weird but the code handles them deliberately."""

    def test_empty_side_block_returns_empty_xi(self):
        assert extract_starting_xi({}) == {
            "gk_id": None, "outfield_ids": set(),
            "raw_players": [], "position_by_id": {},
        }

    def test_empty_startxi_returns_empty_xi(self):
        assert extract_starting_xi({"startXI": []}) == {
            "gk_id": None, "outfield_ids": set(),
            "raw_players": [], "position_by_id": {},
        }

    def test_single_outfield_only_no_gk(self):
        """Single starter with no GK — handled gracefully."""
        xi = extract_starting_xi({"startXI": [_player(5, pos="M")]})
        assert xi["gk_id"] is None
        assert xi["outfield_ids"] == {5}

    def test_player_block_inner_none_skipped(self):
        """player=None entries still iterate but contribute nothing."""
        xi = extract_starting_xi({"startXI": [{"player": None}]})
        assert xi["gk_id"] is None
        assert xi["outfield_ids"] == set()

    def test_player_missing_pos_field_treated_as_outfield(self):
        """Documented contract: no position → not GK, falls into outfield."""
        xi = extract_starting_xi({"startXI": [_player(1, pos=None)]})
        assert xi["gk_id"] is None
        assert 1 in xi["outfield_ids"]

    def test_string_match_id_coerced_to_int(self):
        """build_lineup_entry casts sched['m'] via int() — string digits OK."""
        entry = fetch_lineups.build_lineup_entry(
            {"m": "12", "home": "A", "away": "B"}, [], {})
        assert entry["match_id"] == 12
        assert isinstance(entry["match_id"], int)

    def test_empty_outfield_in_current_returns_zero(self):
        """Empty current XI → 0 (no panic; first recording or feed gap)."""
        prior = _xi(1, [2, 3])
        curr = _xi(None, [])
        delta, reason = compute_lineup_delta_elo(prior, curr)
        assert delta == 0.0
        assert reason is None

    def test_below_rotation_threshold_zero(self):
        """1 outfield change is sub-threshold and stays at 0."""
        prior = _xi(1, list(range(2, 12)))
        curr = _xi(1, [20] + list(range(3, 12)))  # one swap
        delta, _ = compute_lineup_delta_elo(prior, curr)
        assert delta == 0.0
