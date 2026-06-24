"""
Adversarial audit of scripts/live/stats_proxy_adjustments.py.

Convention:
  * pytest.raises(...) — LOUD: code raises a useful exception.
  * @pytest.mark.xfail(strict=True, reason=...) — SILENT BUG: code returns
    a plausible-looking number instead of signalling. xfail(strict) means
    the test PASSES today only because the bug is present; if/when the
    bug is fixed, the test will start passing-as-expected and we flip
    the marker.
  * plain assert — documents acceptable / by-design behavior.

Read-only: does not change production thresholds, caps, or logic.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

from stats_proxy_adjustments import (  # noqa: E402
    STATS_PROXY_RAW_CAP,
    _possession_signal,
    _to_int,
    both_form_deltas,
    compute_form_delta,
    compute_xg_form_delta,
    stats_to_dict,
)


def _sd(sot=0, poss=None, corners=0) -> dict:
    d: dict = {"Shots on Goal": sot, "Corner Kicks": corners}
    if poss is not None:
        d["Ball Possession"] = poss
    return d


# ======================================================================
# A. EMPTY / SPARSE — should be zero, not crash
# ======================================================================
class TestEmptyOrSparseInputs:
    def test_both_dicts_empty_returns_zero(self):
        """LOUD-by-spec: missing stats ⇒ 0 delta (correct conservative)."""
        assert compute_form_delta({}, {}) == 0.0

    def test_one_side_empty_other_dominant(self):
        """Half-empty (rare provider truncation): own gets full credit
        if opp dict is blank. Documents current behavior."""
        own = _sd(sot=8, poss=60, corners=6)
        opp = {}
        # 8*1.2 + (60-50-5)*0.06 + 6*0.3 = 9.6 + 0.3 + 1.8 = 11.7
        assert math.isclose(compute_form_delta(own, opp), 11.7, abs_tol=1e-6)

    def test_unknown_types_only_yield_zero_delta(self):
        d = stats_to_dict([
            {"type": "Yellow Cards", "value": 3},
            {"type": "Goalkeeper Saves", "value": 5},
        ])
        # R12 MED: stats_to_dict now ALSO populates lowercase-collapsed
        # alias keys for case-tolerance against provider-side drift.
        # Original keys still present; aliases added.
        assert d["Yellow Cards"] == 3
        assert d["Goalkeeper Saves"] == 5
        assert d["yellow cards"] == 3
        assert d["goalkeeper saves"] == 5
        assert compute_form_delta(d, d) == 0.0

    def test_stats_to_dict_handles_none_payload(self):
        """LOUD-by-spec via `or []` guard at line 69."""
        assert stats_to_dict(None) == {}
        assert stats_to_dict([]) == {}


# ======================================================================
# B. EXTREME NUMERIC INPUTS — LOUD on inf/nan in raw payload
# ======================================================================
class TestExtremeNumericInputs:
    def test_to_int_nan_raises_value_error(self):
        """LOUD: int(float('nan')) → ValueError, propagates."""
        with pytest.raises(ValueError):
            _to_int(float("nan"))

    def test_to_int_inf_raises_overflow_error(self):
        """LOUD: int(float('inf')) → OverflowError, propagates."""
        with pytest.raises(OverflowError):
            _to_int(float("inf"))

    def test_to_int_nan_string_returns_none(self):
        """LOUD-enough: 'nan' string fails int conversion via the
        explicit `except ValueError: return None` branch. Downstream
        treats None as missing ⇒ zero delta. Safe."""
        assert _to_int("nan") is None
        assert _to_int("trash") is None
        assert _to_int("") is None

    def test_to_int_inf_string_returns_none(self):
        """LOUD-enough: 'inf' parses through float() but the explicit
        except is only ValueError — int(float('inf')) raises Overflow
        which would PROPAGATE. This is a documented quirk."""
        with pytest.raises(OverflowError):
            _to_int("inf")

    def test_xg_nan_should_not_silently_clamp(self):
        """FIXED: compute_xg_form_delta now raises ValueError on NaN
        inputs (stats_proxy_adjustments.py:107 guard)."""
        with pytest.raises(ValueError):
            compute_xg_form_delta(float("nan"), 1.0)

    def test_xg_inf_should_not_silently_clamp(self):
        """FIXED: compute_xg_form_delta now raises ValueError on +inf
        inputs (stats_proxy_adjustments.py:107 guard)."""
        with pytest.raises(ValueError):
            compute_xg_form_delta(float("inf"), 0.0)

    def test_xg_negative_input_silently_accepted(self):
        """SILENT-by-design: xG cannot physically be < 0, but no domain
        check. Documents current behavior; acceptable for an internal
        helper where upstream is responsible for sanitising."""
        assert compute_xg_form_delta(-1.0, 0.0) == -6.0

    def test_possession_nan_should_not_silently_clamp(self):
        """FIXED: _possession_signal now early-returns 0.0 when NaN is
        passed (stats_proxy_adjustments.py:78 guard). NaN possession
        therefore contributes 0 to the form delta instead of getting
        silently clamped to +12.0."""
        own = _sd(sot=0, poss=float("nan"), corners=0)
        opp = _sd(sot=0, poss=50, corners=0)
        out = compute_form_delta(own, opp)
        assert out == 0.0

    def test_possession_out_of_range_high_silently_accepted(self):
        """SILENT-by-design: 200% poss is impossible but accepted.
        (200-50-5)*0.06 = 8.7, well within cap. Caller sanitises."""
        own = _sd(sot=0, poss=200, corners=0)
        opp = _sd(sot=0, poss=-100, corners=0)
        assert compute_form_delta(own, opp) == 8.7

    def test_negative_shots_on_target_silently_scored(self):
        """SILENT-by-design: SoT can't be < 0 physically. A future
        patch that subtracts SoT (sign flip) hits negative cap silently.
        Documents the gap."""
        own = _sd(sot=-5, poss=None, corners=0)
        opp = _sd(sot=5, poss=None, corners=0)
        assert compute_form_delta(own, opp) == -STATS_PROXY_RAW_CAP

    def test_xg_zero_both_sides_returns_zero(self):
        assert compute_xg_form_delta(0.0, 0.0) == 0.0

    def test_xg_equal_high_values_return_zero(self):
        """Equal xG ⇒ zero edge, regardless of magnitude."""
        assert compute_xg_form_delta(5.0, 5.0) == 0.0


# ======================================================================
# C. RAW PROVIDER ARRAY QUIRKS
# ======================================================================
class TestProviderArrayQuirks:
    def test_duplicate_entries_last_wins(self):
        """SILENT-by-design: API-Football has been observed double-
        sending stats on retried fixtures. stats_to_dict overwrites
        without warning; last entry wins."""
        d = stats_to_dict([
            {"type": "Shots on Goal", "value": 3},
            {"type": "Shots on Goal", "value": 10},
        ])
        assert d["Shots on Goal"] == 10

    def test_empty_type_dropped(self):
        """LOUD-enough: empty/None type silently dropped ⇒ empty dict
        ⇒ downstream delta = 0. Safe by accident."""
        d = stats_to_dict([
            {"type": "", "value": 5},
            {"type": None, "value": 7},
        ])
        assert d == {}

    def test_percent_with_decimal_truncated(self):
        """SILENT-by-design: '57.6%' → 57 (int truncation, ~0.6pp lost
        ≈ ±0.06 Elo error within deadzone). Document."""
        assert _to_int("57.6%") == 57

    def test_both_form_deltas_symmetry_property(self):
        """LOUD-by-spec: equal-and-opposite deltas under swap. If a
        refactor breaks the sign convention this test fails loudly."""
        home = [{"type": "Shots on Goal", "value": 7}]
        away = [{"type": "Shots on Goal", "value": 3}]
        h, a = both_form_deltas(home, away)
        assert math.isclose(h + a, 0.0, abs_tol=1e-9)
        h2, a2 = both_form_deltas(away, home)
        assert math.isclose(h2, a, abs_tol=1e-9)
        assert math.isclose(a2, h, abs_tol=1e-9)


# ======================================================================
# D. POSSESSION DEADZONE EDGE CASES
# ======================================================================
class TestPossessionDeadzoneEdges:
    def test_exact_deadzone_boundary_returns_zero(self):
        assert _possession_signal(55) == 0.0
        assert _possession_signal(45) == 0.0

    def test_just_above_deadzone_credits_only_excess(self):
        out = _possession_signal(55.5)
        # edge=5.5, adjusted=0.5, credit=0.03
        assert math.isclose(out, 0.5 * 0.06, abs_tol=1e-12)

    def test_none_possession_returns_zero(self):
        assert _possession_signal(None) == 0.0

    def test_zero_possession_strong_negative(self):
        # edge=-50, adjusted=-45, credit=-2.7
        assert math.isclose(_possession_signal(0), -2.7, abs_tol=1e-12)

    def test_full_possession_strong_positive(self):
        assert math.isclose(_possession_signal(100), 2.7, abs_tol=1e-12)


# ======================================================================
# E. CAP ENFORCEMENT — LOUD-by-construction
# ======================================================================
class TestRawCapEnforcement:
    def test_raw_cap_positive(self):
        own = _sd(sot=100, poss=99, corners=50)
        opp = _sd(sot=0, poss=1, corners=0)
        assert compute_form_delta(own, opp) == STATS_PROXY_RAW_CAP

    def test_raw_cap_negative(self):
        own = _sd(sot=0, poss=1, corners=0)
        opp = _sd(sot=100, poss=99, corners=50)
        assert compute_form_delta(own, opp) == -STATS_PROXY_RAW_CAP

    def test_raw_cap_unchanged_constant(self):
        """LOUD-by-spec guard: the raw cap drifting silently to a new
        value would change matchday Elo by up to 4pts per match."""
        assert STATS_PROXY_RAW_CAP == 12.0
