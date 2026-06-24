"""
Adversarial tests for scripts/check_invariants.py.

Each adversarial case feeds a deliberately-broken predictions blob and
asserts the specific exception type fires. The happy-path test confirms a
well-formed 48-team / Σ=1.0 blob passes without raising.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make scripts/ importable without polluting sys.path globally.
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from check_invariants import (  # noqa: E402
    MissingField,
    MissingFile,
    MissingKey,
    SumOutOfTolerance,
    WrongTeamCount,
    check_invariants,
)


def _write(tmp_path: Path, blob: dict) -> Path:
    p = tmp_path / "predictions_live.json"
    p.write_text(json.dumps(blob))
    return p


def _team(name: str, p: float) -> dict:
    return {"team": name, "p_champion": p}


def _well_formed(p_each: float | None = None) -> dict:
    p_each = (1.0 / 48) if p_each is None else p_each
    return {"team_predictions": [_team(f"T{i:02d}", p_each) for i in range(48)]}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_happy_path_48_teams_sum_one_passes(tmp_path: Path) -> None:
    """48 teams, p_champion=1/48 each → must not raise."""
    p = _write(tmp_path, _well_formed())
    check_invariants(p)  # no exception → pass


# ---------------------------------------------------------------------------
# Adversarial: missing file
# ---------------------------------------------------------------------------
def test_missing_file_raises(tmp_path: Path) -> None:
    p = tmp_path / "does_not_exist.json"
    with pytest.raises(MissingFile):
        check_invariants(p)


# ---------------------------------------------------------------------------
# Adversarial (a): sum = 0.5 across 48 teams
# ---------------------------------------------------------------------------
def test_sum_half_raises_sum_out_of_tolerance(tmp_path: Path) -> None:
    blob = {"team_predictions": [_team(f"T{i:02d}", 0.5 / 48) for i in range(48)]}
    p = _write(tmp_path, blob)
    with pytest.raises(SumOutOfTolerance):
        check_invariants(p)


# ---------------------------------------------------------------------------
# Adversarial (b): 47 teams, correct sum
# ---------------------------------------------------------------------------
def test_47_teams_raises_wrong_team_count(tmp_path: Path) -> None:
    blob = {"team_predictions": [_team(f"T{i:02d}", 1.0 / 47) for i in range(47)]}
    p = _write(tmp_path, blob)
    with pytest.raises(WrongTeamCount):
        check_invariants(p)


# ---------------------------------------------------------------------------
# Adversarial (c): team-list key missing entirely
# ---------------------------------------------------------------------------
def test_missing_team_key_raises_missing_key(tmp_path: Path) -> None:
    blob = {"generated_at": "2026-06-16T00:00:00Z", "match_predictions": []}
    p = _write(tmp_path, blob)
    with pytest.raises(MissingKey):
        check_invariants(p)


# ---------------------------------------------------------------------------
# Adversarial (d): one team missing p_champion field
# ---------------------------------------------------------------------------
def test_team_without_p_champion_raises_missing_field(tmp_path: Path) -> None:
    teams = [_team(f"T{i:02d}", 1.0 / 48) for i in range(48)]
    teams[7].pop("p_champion")  # one bad apple
    p = _write(tmp_path, {"team_predictions": teams})
    with pytest.raises(MissingField):
        check_invariants(p)


# ---------------------------------------------------------------------------
# Adversarial: sum slightly off — just outside 1e-6 tolerance
# ---------------------------------------------------------------------------
def test_sum_just_outside_tolerance_raises(tmp_path: Path) -> None:
    """Σ = 1.0 + 2e-6 should fail; confirms the 1e-6 boundary is enforced."""
    teams = [_team(f"T{i:02d}", 1.0 / 48) for i in range(48)]
    # Bump one team to push the sum 2e-6 over 1.0
    teams[0]["p_champion"] += 2e-6
    p = _write(tmp_path, {"team_predictions": teams})
    with pytest.raises(SumOutOfTolerance):
        check_invariants(p)
