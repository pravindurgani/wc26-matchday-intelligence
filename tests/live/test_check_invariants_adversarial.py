"""
Extended adversarial tests for scripts/check_invariants.py.

Companion to tests/live/test_check_invariants.py (the original 6 cases) —
adds boundary, NaN/inf, malformed-JSON, duplicate-team and structural
adversarial cases. SILENT bugs are pinned with ``xfail(strict=True)`` so
they turn green automatically the moment the gate hardens.

Adversarial matrix recap (per audit task):
  * p_champ = 1.0 + 1e-7      (just inside  1e-6 tol → must PASS)        LOUD
  * p_champ = 1.0 + 1.0001e-6 (just outside 1e-6 tol → must FAIL)        LOUD
  * 48 teams all p = 1/48     (uniform but valid → must PASS)            LOUD
  * one team p = NaN          (SILENT — NaN comparisons swallow check)
  * one team p = inf          (LOUD — inf >= 1e-6 fires)
  * one team p = −0.001       (SILENT — no range check)
  * duplicate team key        (SILENT — no uniqueness check)
  * trailing garbage in JSON  (LOUD-ish — uncaught JSONDecodeError → exit 1)
  * empty JSON file           (LOUD-ish — same JSONDecodeError path)
  * top-level list (not dict) (LOUD — falls into MissingKey)
  * team_predictions = []     (LOUD — WrongTeamCount)
  * 48 records, 1 missing field (LOUD — MissingField — already covered
    in test_check_invariants.py; not duplicated here)
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from check_invariants import (  # noqa: E402
    MalformedJson,
    MissingField,
    MissingKey,
    SumOutOfTolerance,
    WrongTeamCount,
    check_invariants,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _team(name: str, p: float) -> dict:
    return {"team": name, "p_champion": p}


def _write(tmp_path: Path, blob) -> Path:
    p = tmp_path / "predictions_live.json"
    if isinstance(blob, (dict, list)):
        p.write_text(json.dumps(blob))
    else:
        # Raw bytes / string — write verbatim (used for malformed JSON cases).
        p.write_text(str(blob))
    return p


def _48_teams_uniform() -> list[dict]:
    return [_team(f"T{i:02d}", 1.0 / 48) for i in range(48)]


# ---------------------------------------------------------------------------
# Tolerance boundary — must PASS just inside, FAIL just outside
# ---------------------------------------------------------------------------
def test_sum_just_inside_tolerance_passes(tmp_path: Path) -> None:
    """Σ = 1.0 + 1e-7 → |Δ| = 1e-7 < 1e-6 tol → must PASS (LOUD-correct)."""
    teams = _48_teams_uniform()
    teams[0]["p_champion"] += 1e-7
    p = _write(tmp_path, {"team_predictions": teams})
    check_invariants(p)  # no raise


def test_sum_just_outside_tolerance_fails(tmp_path: Path) -> None:
    """Σ = 1.0 + 1.0001e-6 → |Δ| ≥ 1e-6 → must FAIL (LOUD-correct).

    Note: the existing test_check_invariants.py covers 2e-6; this nails the
    boundary at 1.0001e-6 to make the tolerance edge explicit.
    """
    teams = _48_teams_uniform()
    teams[0]["p_champion"] += 1.0001e-6
    p = _write(tmp_path, {"team_predictions": teams})
    with pytest.raises(SumOutOfTolerance):
        check_invariants(p)


# ---------------------------------------------------------------------------
# Uniform distribution — valid but pathological for downstream rendering
# ---------------------------------------------------------------------------
def test_uniform_distribution_48_equal_passes(tmp_path: Path) -> None:
    """48 teams all at p=1/48 → sum = 1.0 to float precision → PASS.

    Confirms the gate doesn't accidentally reject uniform priors (which
    is the legitimate starting state before any data lands)."""
    p = _write(tmp_path, {"team_predictions": _48_teams_uniform()})
    check_invariants(p)


# ---------------------------------------------------------------------------
# NaN — SILENT BUG (NaN comparison semantics)
# ---------------------------------------------------------------------------
def test_one_team_p_champion_nan_is_rejected(tmp_path: Path) -> None:
    """HARDENED: one NaN in p_champion must raise SumOutOfTolerance.

    Pre-fix this silently passed because NaN propagates through sum() and
    ``abs(NaN - 1.0) >= 1e-6`` evaluates to False under IEEE-754. The gate
    now pre-validates every p_champion with math.isfinite before summing.

    JSON doesn't natively support NaN, so we hand-craft the blob via
    ``allow_nan=True`` (which is the json default — it emits the literal
    `NaN` which json.loads then accepts back due to parse_constant).
    """
    teams = _48_teams_uniform()
    teams[5]["p_champion"] = float("nan")
    p = tmp_path / "predictions_live.json"
    # json.dumps emits 'NaN' (non-standard but Python's default), and
    # json.loads accepts it back.
    p.write_text(json.dumps({"team_predictions": teams}))
    # Confirms the BLOB itself round-trips NaN — sanity check.
    loaded = json.loads(p.read_text())
    assert math.isnan(loaded["team_predictions"][5]["p_champion"])
    # HARDENED: non-finite guard now fires at check_invariants.py before
    # the sum is even computed.
    with pytest.raises(SumOutOfTolerance):
        check_invariants(p)


# ---------------------------------------------------------------------------
# inf — LOUD (inf - 1.0 = inf, inf >= 1e-6 is True)
# ---------------------------------------------------------------------------
def test_one_team_p_champion_inf_raises_sum_out_of_tolerance(
    tmp_path: Path,
) -> None:
    """LOUD: one inf p_champion → sum = inf → abs(inf - 1.0) = inf ≥ tol
    → SumOutOfTolerance fires. Correct behavior, pin it."""
    teams = _48_teams_uniform()
    teams[3]["p_champion"] = float("inf")
    p = tmp_path / "predictions_live.json"
    p.write_text(json.dumps({"team_predictions": teams}))
    with pytest.raises(SumOutOfTolerance):
        check_invariants(p)


# ---------------------------------------------------------------------------
# Negative probability — SILENT if sum still hits 1.0 by chance
# ---------------------------------------------------------------------------
def test_negative_p_champion_is_rejected(tmp_path: Path) -> None:
    """HARDENED: negative p_champion must raise MissingField even when
    the negative value is exactly compensated by a positive overshoot
    elsewhere so Σ still hits 1.0.

    Pre-fix the gate only enforced Σ ≈ 1.0 + presence of the field;
    it now enforces 0.0 ≤ p_champion ≤ 1.0 per-row (mapped to
    MissingField / exit 5 to keep the contract surface compact)."""
    teams = _48_teams_uniform()
    teams[10]["p_champion"] = -0.001
    teams[11]["p_champion"] = (1.0 / 48) + 0.001  # compensate
    p = _write(tmp_path, {"team_predictions": teams})
    with pytest.raises(MissingField):
        check_invariants(p)


# ---------------------------------------------------------------------------
# Duplicate team key — SILENT
# ---------------------------------------------------------------------------
def test_duplicate_team_code_is_rejected(tmp_path: Path) -> None:
    """HARDENED: 48 records but two share the same 'team' code must raise
    WrongTeamCount, even though the sum is still 1.0.

    Pre-fix the gate only counted len==48; one of the 48 WC slots could
    be silently missing as long as another row duplicated some code with
    a compensating probability. The gate now checks set(team_codes)==48
    immediately after the length check."""
    teams = _48_teams_uniform()
    # Replace team 47 with a duplicate of team 0 (same code, same p).
    # Sum is still 1.0 → gate would have passed before the uniqueness check.
    teams[47] = _team("T00", 1.0 / 48)
    p = _write(tmp_path, {"team_predictions": teams})
    with pytest.raises(WrongTeamCount):
        check_invariants(p)


# ---------------------------------------------------------------------------
# Malformed JSON — trailing garbage / empty file
# ---------------------------------------------------------------------------
def test_trailing_garbage_after_valid_json_raises(tmp_path: Path) -> None:
    """HARDENED: trailing junk → json.loads raises JSONDecodeError, which
    check_invariants now catches and re-raises as MalformedJson (exit 7).

    Previously this bubbled as an uncategorised exception that mapped to
    CLI exit 1 (Python traceback), breaking the documented exit-code
    contract."""
    p = tmp_path / "predictions_live.json"
    p.write_text(json.dumps({"team_predictions": _48_teams_uniform()}) + " GARBAGE")
    with pytest.raises(MalformedJson):
        check_invariants(p)


def test_empty_json_file_raises(tmp_path: Path) -> None:
    """HARDENED: empty file → JSONDecodeError → MalformedJson (exit 7)."""
    p = tmp_path / "predictions_live.json"
    p.write_text("")
    with pytest.raises(MalformedJson):
        check_invariants(p)


# ---------------------------------------------------------------------------
# Wrong top-level type — list instead of dict
# ---------------------------------------------------------------------------
def test_top_level_list_raises_missing_key(tmp_path: Path) -> None:
    """LOUD: top-level list → `for key in _TEAM_KEYS: if key in data` →
    string-in-list returns False → teams is None → MissingKey."""
    p = _write(tmp_path, _48_teams_uniform())  # raw list, no wrapper dict
    with pytest.raises(MissingKey):
        check_invariants(p)


# ---------------------------------------------------------------------------
# Empty team_predictions list
# ---------------------------------------------------------------------------
def test_team_predictions_empty_list_raises_wrong_team_count(
    tmp_path: Path,
) -> None:
    """LOUD: empty list → len != 48 → WrongTeamCount."""
    p = _write(tmp_path, {"team_predictions": []})
    with pytest.raises(WrongTeamCount):
        check_invariants(p)


# ---------------------------------------------------------------------------
# 48 records, one missing p_champion field (sanity — already covered in
# test_check_invariants.py but kept here as a parametric scaffold for the
# "missing field" edge family).
# ---------------------------------------------------------------------------
def test_48_records_one_missing_p_champion_raises_missing_field(
    tmp_path: Path,
) -> None:
    teams = _48_teams_uniform()
    teams[42].pop("p_champion")
    p = _write(tmp_path, {"team_predictions": teams})
    with pytest.raises(MissingField):
        check_invariants(p)


# ---------------------------------------------------------------------------
# CLI exit-code contract — confirm the documented 0/2/3/4/5/6 mapping
# holds under adversarial input.
# ---------------------------------------------------------------------------
_INVARIANTS_CLI = _ROOT / "scripts" / "check_invariants.py"


def _run_cli(path: Path) -> int:
    return subprocess.run(
        [sys.executable, str(_INVARIANTS_CLI), str(path)],
        capture_output=True,
    ).returncode


def test_cli_exit_0_on_happy_path(tmp_path: Path) -> None:
    p = _write(tmp_path, {"team_predictions": _48_teams_uniform()})
    assert _run_cli(p) == 0


def test_cli_exit_2_on_missing_file(tmp_path: Path) -> None:
    assert _run_cli(tmp_path / "does_not_exist.json") == 2


def test_cli_exit_3_on_missing_key(tmp_path: Path) -> None:
    p = _write(tmp_path, {"generated_at": "now"})
    assert _run_cli(p) == 3


def test_cli_exit_4_on_wrong_team_count(tmp_path: Path) -> None:
    p = _write(tmp_path, {"team_predictions": _48_teams_uniform()[:47]})
    assert _run_cli(p) == 4


def test_cli_exit_5_on_missing_field(tmp_path: Path) -> None:
    teams = _48_teams_uniform()
    teams[0].pop("p_champion")
    p = _write(tmp_path, {"team_predictions": teams})
    assert _run_cli(p) == 5


def test_cli_exit_6_on_sum_out_of_tolerance(tmp_path: Path) -> None:
    teams = _48_teams_uniform()
    teams[0]["p_champion"] += 0.1  # huge violation
    p = _write(tmp_path, {"team_predictions": teams})
    assert _run_cli(p) == 6


def test_cli_exit_7_on_malformed_json(tmp_path: Path) -> None:
    """HARDENED: malformed JSON now maps to documented exit code 7
    (MalformedJson) instead of bubbling as an uncategorised exit 1.

    check_invariants.py wraps json.loads in try/except json.JSONDecodeError
    and raises MalformedJson, which main() catches and returns 7."""
    p = tmp_path / "predictions_live.json"
    p.write_text("{ not valid json")
    rc = _run_cli(p)
    assert rc == 7, (
        f"Malformed JSON should exit 7 (MalformedJson); got {rc}. "
        "See check_invariants.py module docstring for the full exit-code "
        "contract (0/2/3/4/5/6/7)."
    )
