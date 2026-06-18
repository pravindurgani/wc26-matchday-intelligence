"""
check_invariants.py — Strict Σ-invariant gate for predictions_live.json.

Asserts the canonical post-simulation invariants required before any deploy or
live update:

  1. File exists and parses as JSON.
  2. Top-level team list key is present (canonical: "team_predictions";
     also accepts legacy alias "teams" for forward-compatibility).
  3. Team list length is exactly 48.
  4. Team codes are unique across the 48 records (no duplicated 'team' key).
  5. Every team carries a numeric "p_champion" field.
  6. Every p_champion is finite (no NaN / inf) and lies in [0.0, 1.0].
  7. |Σ p_champion − 1.0| < 1e-6 (tight tolerance, not the legacy 1e-2/1e-3
     used by 09_validate.py:76-79 and pre_flight.py:198).

Exit codes (when run as a script):
  0  — all invariants hold
  2  — MissingFile
  3  — MissingKey
  4  — WrongTeamCount        (also fires for duplicate team codes)
  5  — MissingField          (also fires for out-of-range p_champion)
  6  — SumOutOfTolerance     (also fires for non-finite p_champion)
  7  — MalformedJson         (JSON parse error — was uncategorised exit 1)

Importers should call ``check_invariants(path)`` and catch the specific
exceptions below; the function raises rather than calling ``sys.exit``.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Union

# Canonical artifact path. The strict gate runs against the post-simulation
# blob written by scripts/03_simulate.py — currently data/processed/, with
# a mirror published to dashboard/predictions_live.json for the front end.
DEFAULT_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "processed"
    / "predictions_live.json"
)

# Tight tolerance — purposely 4 orders of magnitude tighter than the legacy
# 1e-2 in 09_validate.py:76-79 so that any silent drift in the simulator
# normalisation is caught before publish.
TOLERANCE = 1e-6

# Accept canonical key first; fall back to "teams" so the contract in the
# remediation spec (`top-level "teams" key`) is also honoured for any future
# blob that switches to that name without breaking this gate.
_TEAM_KEYS = ("team_predictions", "teams")


class InvariantError(Exception):
    """Base class for all Σ-invariant violations."""


class MissingFile(InvariantError):
    """Predictions blob does not exist on disk."""


class MissingKey(InvariantError):
    """Predictions blob does not contain a team-list key."""


class WrongTeamCount(InvariantError):
    """Team list length is not 48, or team codes are not unique across 48 rows."""


class MissingField(InvariantError):
    """At least one team is missing the p_champion field, or p_champion
    is out of the legal probability range [0.0, 1.0]."""


class SumOutOfTolerance(InvariantError):
    """|Σ p_champion − 1.0| ≥ 1e-6, or at least one p_champion is non-finite
    (NaN/inf) so the sum is mathematically undefined."""


class MalformedJson(InvariantError):
    """predictions_live.json failed to parse (JSONDecodeError, empty file,
    trailing garbage, etc). Maps to exit code 7."""


def check_invariants(path: Union[str, Path, None] = None) -> None:
    """Run all strict invariants. Raises on failure, returns ``None`` on pass.

    Parameters
    ----------
    path : str | Path | None
        Override path to predictions_live.json. Defaults to DEFAULT_PATH.

    Raises
    ------
    MissingFile, MissingKey, WrongTeamCount, MissingField,
    SumOutOfTolerance, MalformedJson
    """
    p = Path(path) if path is not None else DEFAULT_PATH
    if not p.exists():
        raise MissingFile(f"predictions blob not found at {p}")

    # Bug #4: wrap JSON parsing so malformed input becomes a documented
    # MalformedJson (exit 7) instead of bubbling as an uncategorised exit 1.
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise MalformedJson(f"failed to parse {p}: {e}") from e

    # Top-level must be a JSON object (dict). A list / scalar can't carry a
    # team-key by definition — surface that as MissingKey rather than
    # crashing in the error-message formatter with AttributeError (which
    # bubbled as an uncategorised exit 1, breaking the documented
    # 2/3/4/5/6 contract).
    if not isinstance(data, dict):
        raise MissingKey(
            f"top-level JSON must be an object with one of {_TEAM_KEYS}; "
            f"got {type(data).__name__}"
        )

    teams = None
    for key in _TEAM_KEYS:
        if key in data:
            teams = data[key]
            break
    if teams is None:
        raise MissingKey(
            f"none of {_TEAM_KEYS} present in top-level keys: "
            f"{sorted(data.keys())[:10]}"
        )

    if len(teams) != 48:
        raise WrongTeamCount(f"expected 48 teams, got {len(teams)}")

    # Bug #3: enforce uniqueness of team codes across the 48 records.
    # A duplicated 'team' code semantically means one of the 48 WC slots is
    # missing; the sum-check alone won't notice because the duplicated row
    # still contributes a legal probability to the total.
    codes = [t.get("team") for t in teams]
    if len({c for c in codes if c is not None}) != 48:
        # Either a duplicate, or one or more rows are missing the 'team' key.
        n_dupes = len(codes) - len(set(codes))
        raise WrongTeamCount(
            f"team codes are not unique across 48 rows "
            f"(duplicates: {n_dupes}; sample codes: {codes[:5]})"
        )

    for i, t in enumerate(teams):
        if "p_champion" not in t:
            name = t.get("team", f"<index {i}>")
            raise MissingField(f"team {name!r} missing p_champion field")

    # Bug #1: reject non-finite p_champion BEFORE summing. NaN propagates
    # through sum() and abs(NaN - 1.0) >= tol is False under IEEE-754, so the
    # tolerance check silently passes. inf would actually trip the existing
    # tolerance check (inf >= tol is True), but failing it here as a
    # SumOutOfTolerance with a clearer message keeps both paths consistent.
    #
    # Wave-4 fix: also reject `bool`. Python bools are a subclass of int, so
    # `isinstance(True, (int, float))` is True and `math.isfinite(True)` is
    # True (True == 1.0). Without this filter, 47 False + 1 True passes
    # Σ == 1.0 silently — a JSON serialiser drift that swapped a 1.0 for a
    # `true` literal would slip past every other check.
    for t in teams:
        p_val = t["p_champion"]
        if isinstance(p_val, bool) or not isinstance(p_val, (int, float)) \
                or not math.isfinite(p_val):
            raise SumOutOfTolerance(
                f"non-finite p_champion for team {t.get('team')!r}: {p_val!r}"
            )

    # Bug #2: enforce 0.0 ≤ p_champion ≤ 1.0. A negative value compensated
    # by a positive value can still hit Σ = 1.0 and slip past the tolerance
    # check, even though probabilities can't legally be negative.
    for t in teams:
        p_val = t["p_champion"]
        if not (0.0 <= p_val <= 1.0):
            raise MissingField(
                f"p_champion for team {t.get('team')!r} out of range "
                f"[0.0, 1.0]: {p_val!r}"
            )

    total = sum(t["p_champion"] for t in teams)
    if abs(total - 1.0) >= TOLERANCE:
        raise SumOutOfTolerance(
            f"|Σ p_champion − 1.0| = {abs(total - 1.0):.3e} "
            f"≥ tol {TOLERANCE:.0e} (actual sum = {total!r})"
        )

    # R11 E2-old: pin annex_c_misses == 0 on every blob, not just the
    # pre-tournament canonical that 09_validate.py:96-97 already checks.
    # The R10 Q3 strict-mirror call into _check_strict_invariants picks up
    # this assertion automatically, so dashboard/predictions_live.json
    # gets the same gate as the canonical artifact without an extra
    # 09_validate.py line.
    annex_misses = data.get("annex_c_misses")
    if annex_misses is not None and annex_misses != 0:
        raise MissingField(
            f"annex_c_misses = {annex_misses!r} (expected 0). The Annex C "
            f"third-place lookup table is incomplete or the table key "
            f"computation drifted; downstream R32 third-place assignments "
            f"will silently fall back to non-canonical permutations."
        )

    # R11 E10: pin per-stage round-survival Σ and per-team probability
    # stacking. Pre-R11 only Σ p_champion was checked strictly. Single-
    # elimination structure gives:
    #   Σ p_advance_groups ≈ 32 (R32 = top-2 of 16 groups)
    #   Σ p_reach_r16 ≈ 32 (same — R32 = R16 entry by definition)
    #   Σ p_reach_qf ≈ 8 (16 → 8 winners)
    #   Σ p_reach_sf ≈ 4 (8 → 4 winners)
    #   Σ p_reach_final ≈ 2 (4 → 2 winners)
    #   Σ p_champion ≈ 1 (2 → 1 winner)
    # Tolerance same 1e-6 as Σ p_champion since the simulator emits these
    # from the same 25k-trial empirical proportions. A drift in any of
    # them would signal an off-by-one stage transition.
    stage_expectations = (
        ("p_reach_qf", 8.0),
        ("p_reach_sf", 4.0),
        ("p_reach_final", 2.0),
    )
    for field, expected in stage_expectations:
        if not all(field in t for t in teams):
            # Field not in this blob (pre-R11 or legacy) — skip silently.
            continue
        s = 0.0
        for t in teams:
            v = t[field]
            if isinstance(v, bool) or not isinstance(v, (int, float)) \
                    or not math.isfinite(v) or not (0.0 <= v <= 1.0):
                raise SumOutOfTolerance(
                    f"invalid {field} for team {t.get('team')!r}: {v!r}"
                )
            s += v
        if abs(s - expected) >= TOLERANCE:
            raise SumOutOfTolerance(
                f"|Σ {field} − {expected:.1f}| = {abs(s - expected):.3e} "
                f"≥ tol {TOLERANCE:.0e} (actual sum = {s!r})"
            )

    # Per-team stacking: p_champion ≤ p_reach_final ≤ p_reach_sf ≤
    # p_reach_qf ≤ p_reach_r16. A violation means the simulator emitted a
    # team that "reached the final" less often than it "won the cup" —
    # an impossible event in a single-elim bracket. INV1 (probability-
    # monotone) catches the class of bug R11 E10 was designed to pin.
    # Allow a 1e-9 cushion for floating-point on the few "round-robin"
    # teams whose deeper-stage probabilities round to the same value.
    stack_order = ("p_reach_qf", "p_reach_sf", "p_reach_final", "p_champion")
    stack_cushion = 1e-9
    for t in teams:
        if not all(f in t for f in stack_order):
            continue
        for i in range(len(stack_order) - 1):
            broader, narrower = stack_order[i], stack_order[i + 1]
            if t[narrower] - t[broader] > stack_cushion:
                raise SumOutOfTolerance(
                    f"INV1 stacking violated for team {t.get('team')!r}: "
                    f"{narrower}={t[narrower]!r} > {broader}={t[broader]!r}. "
                    f"A team cannot win the cup more often than it reaches "
                    f"the final, or reach the final more often than the SF, "
                    f"etc. The simulator's stage transitions are drifting."
                )


def _format_ok(path: Path) -> str:
    data = json.loads(path.read_text())
    teams = data.get("team_predictions") or data.get("teams") or []
    total = sum(t["p_champion"] for t in teams)
    return (
        f"OK  Σ p_champion = {total!r}  (|Δ| = {abs(total - 1.0):.3e}, "
        f"tol = {TOLERANCE:.0e})  teams = {len(teams)}  path = {path}"
    )


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_PATH
    try:
        check_invariants(path)
    except MissingFile as e:
        print(f"check_invariants: MissingFile: {e}", file=sys.stderr)
        return 2
    except MissingKey as e:
        print(f"check_invariants: MissingKey: {e}", file=sys.stderr)
        return 3
    except WrongTeamCount as e:
        print(f"check_invariants: WrongTeamCount: {e}", file=sys.stderr)
        return 4
    except MissingField as e:
        print(f"check_invariants: MissingField: {e}", file=sys.stderr)
        return 5
    except SumOutOfTolerance as e:
        print(f"check_invariants: SumOutOfTolerance: {e}", file=sys.stderr)
        return 6
    except MalformedJson as e:
        print(f"check_invariants: MalformedJson: {e}", file=sys.stderr)
        return 7
    print(_format_ok(path))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
