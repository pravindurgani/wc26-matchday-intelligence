"""S9 hygiene: the tournament-wide stats-proxy cap was renamed from
``STATS_CAP_GROUP_TOTAL`` to ``STATS_CAP_TOURNAMENT_TOTAL`` because it
applies across all group + knockout matches, not just the group stage.

This test pins the new name and asserts the old name is gone from
``apply_matchday_adjustments.py`` source so a future regression can't
silently re-introduce the misleading identifier.

The VALUE (20.0) is unchanged and is locked separately by
``scripts/pre_flight.py``; this test does not duplicate that check.
"""
from __future__ import annotations

from pathlib import Path


AMD_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts" / "live" / "apply_matchday_adjustments.py"
)


def test_renamed_constant_present_and_old_name_absent() -> None:
    src = AMD_PATH.read_text(encoding="utf-8")
    assert "STATS_CAP_TOURNAMENT_TOTAL" in src, (
        "expected renamed constant STATS_CAP_TOURNAMENT_TOTAL in "
        f"{AMD_PATH}; not found"
    )
    assert "STATS_CAP_GROUP_TOTAL" not in src, (
        "old constant name STATS_CAP_GROUP_TOTAL still present in "
        f"{AMD_PATH}; the rename is incomplete"
    )


def test_renamed_constant_importable_with_same_value() -> None:
    from scripts.live import apply_matchday_adjustments as amd

    assert hasattr(amd, "STATS_CAP_TOURNAMENT_TOTAL"), (
        "STATS_CAP_TOURNAMENT_TOTAL must be importable from "
        "scripts.live.apply_matchday_adjustments after the rename"
    )
    assert amd.STATS_CAP_TOURNAMENT_TOTAL == 20.0, (
        "rename must not change the cap value (20.0); got "
        f"{amd.STATS_CAP_TOURNAMENT_TOTAL!r}"
    )
    assert not hasattr(amd, "STATS_CAP_GROUP_TOTAL"), (
        "old name STATS_CAP_GROUP_TOTAL must not be re-exported as an "
        "alias — call sites should be migrated, not aliased"
    )
