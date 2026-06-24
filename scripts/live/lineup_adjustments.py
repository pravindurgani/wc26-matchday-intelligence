"""
lineup_adjustments.py — Stream B.4 pure helpers.

Compute conservative Elo deltas from confirmed starting XIs.

v1 philosophy (locked by Stream B sequencing): display first, adjust
sparingly. The dashboard SHOWS every starting XI as soon as the provider
publishes it, but the model only moves when the change is high-confidence.

Heuristic per team-match:
  - Confirmed GK swap vs the team's most recent recorded XI  →  -8 Elo
  - 3+ outfield changes from the prior XI                     →  -3 Elo
  - Both apply (different GK + heavy rotation)                →  sum, capped
  - No baseline (first recorded XI for this team)             →   0 Elo

Cap: LINEUP_CAP (±20) is enforced downstream in apply_matchday_adjustments.
The heuristics above intentionally stay well within that ceiling so the
operator has headroom to add manual deltas without instantly hitting cap.

References:
  - /fixtures/lineups schema:
      https://www.api-football.com/documentation-v3#tag/Fixtures/operation/get-fixtures-lineups
"""
from __future__ import annotations

import warnings

GK_SWAP_ELO = -8.0
HEAVY_ROTATION_ELO = -3.0
HEAVY_ROTATION_THRESHOLD = 3   # ≥ this many outfield differences = "rotation"

GK_POSITION_CODES = {"G", "GK", "Goalkeeper"}

# Position-aware weighting (Phase 4). A 3-defender shuffle is closer to
# tactical noise; a forward dropped from the XI moves the xG needle. We
# weight the OUTGOING starters (prior - current) by position and multiply
# by PENALTY_PER_WEIGHTED_SWAP. Calibration: weight(M)=1.0 + penalty=-1.0
# preserves the legacy "3 swaps = -3 Elo" behaviour when positions are
# unknown — see test_heavy_rotation.
POSITION_WEIGHTS = {"D": 0.7, "M": 1.0, "F": 1.5}
DEFAULT_POSITION_WEIGHT = 1.0
PENALTY_PER_WEIGHTED_SWAP = -1.0


def _is_gk(player: dict) -> bool:
    """Identify the goalkeeper in an API-Football lineup player dict.

    API-Football's startXI entries look like:
      {"player": {"id": 1, "name": "...", "number": 1, "pos": "G", "grid": "1:1"}}
    """
    pos = (player.get("player", {}) or {}).get("pos") or player.get("pos")
    return pos in GK_POSITION_CODES


def _player_id(player: dict) -> int | None:
    """Pull the API-Football player id (preferred over name for stability)."""
    inner = player.get("player") or player
    pid = inner.get("id")
    return int(pid) if pid is not None else None


def _player_pos(player: dict) -> str | None:
    """Pull the position code (G/D/M/F) from an XI entry."""
    inner = player.get("player") or player
    return inner.get("pos")


def extract_starting_xi(side_block: dict) -> dict:
    """Return {"gk_id": int|None, "outfield_ids": set[int], "raw_players": list,
    "position_by_id": dict[int, str]}.

    `side_block` is one entry from the /fixtures/lineups response array
    (one per team). We tolerate missing/malformed fields — the consumer
    treats no-baseline as 0 adjustment, which is safer than guessing.

    Loud-failure invariants (hardened against silent provider bugs):
      - >1 player with pos in {G, GK, Goalkeeper} in startXI raises
        ValueError("multiple_starting_gks") rather than silently letting
        the LAST keeper win and dropping the earlier one into the outfield
        set (which would poison the next-match GK-swap baseline).
      - Duplicate player ids inside the same startXI emit a
        UserWarning("duplicate_player_id") and the repeat is dropped —
        otherwise an id can end up in BOTH gk_id and outfield_ids.
      - startXI lengths outside {0, 11} emit a UserWarning(
        "malformed_xi_size") — a 10- or 12-player roster signals either a
        pre-finalised lineup or a garbled provider payload, and silently
        accepting it skews the downstream rotation delta.
    """
    start = side_block.get("startXI") or []

    # Bug #1: count GKs up-front so we fail loudly instead of letting the
    # last G-pos entry silently overwrite an earlier one.
    gk_entries = [e for e in start if e is not None and _is_gk(e)]
    if len(gk_entries) > 1:
        raise ValueError(
            f"multiple_starting_gks: {len(gk_entries)} players "
            f"flagged as goalkeeper in startXI"
        )

    # Bug #2/#3: warn (but still parse) when startXI is not 0 or 11 entries.
    # Empty is fine (lineup not yet published); other sizes are malformed.
    if len(start) not in (0, 11):
        warnings.warn(
            f"malformed_xi_size: startXI has {len(start)} entries "
            f"(expected 0 or 11)",
            UserWarning,
            stacklevel=2,
        )

    gk_id = None
    outfield: set[int] = set()
    raw: list[dict] = []
    pos_by_id: dict[int, str] = {}
    seen_ids: set[int] = set()  # Bug #4: detect duplicate ids inside one XI.
    for entry in start:
        pid = _player_id(entry)
        raw.append(entry)
        if pid is None:
            continue
        # Bug #4: same id listed twice → warn and drop the repeat so the id
        # can't end up in both gk_id and outfield_ids.
        if pid in seen_ids:
            warnings.warn(
                f"duplicate_player_id: id={pid} appears more than once "
                f"in startXI",
                UserWarning,
                stacklevel=2,
            )
            continue
        seen_ids.add(pid)
        pos = _player_pos(entry)
        if pos:
            pos_by_id[pid] = pos
        if _is_gk(entry):
            gk_id = pid
        else:
            outfield.add(pid)
    return {
        "gk_id": gk_id,
        "outfield_ids": outfield,
        "raw_players": raw,
        "position_by_id": pos_by_id,
    }


def _coerce_id(x: object) -> int | None:
    """Bug #6: prior XIs loaded from a JSON snapshot have string ids;
    extract_starting_xi-produced XIs have int ids. Without coercion at the
    comparison point, the set diff treats identical ids as a full rotation
    and silently scores -10 Elo. Coerce both sides at the boundary."""
    if x is None:
        return None
    return int(x)


def compute_lineup_delta_elo(
    prior_xi: dict | None,
    current_xi: dict,
) -> tuple[float, str | None]:
    """Apply the v1 heuristic. Returns (elo_delta, reason_str_or_None).

    `prior_xi` is None when this is the first recorded XI for the team —
    in that case we have no baseline, so the Elo delta is 0 (display only).
    """
    if not prior_xi:
        return 0.0, None
    if not current_xi.get("outfield_ids"):
        return 0.0, None

    reasons: list[str] = []
    delta = 0.0

    # Bug #5: `if prior_gk and curr_gk` treats gk_id==0 as 'no prior keeper'
    # and silently skips the GK-swap check. A provider id of 0 is falsy but
    # valid — use `is not None` so a 0→999 swap is still detected.
    # Bug #6: coerce both sides to int so a string-id prior (loaded from a
    # JSON snapshot) matches an int-id current XI.
    prior_gk = _coerce_id(prior_xi.get("gk_id"))
    curr_gk = _coerce_id(current_xi.get("gk_id"))
    if prior_gk is not None and curr_gk is not None and prior_gk != curr_gk:
        delta += GK_SWAP_ELO
        reasons.append("GK swap")

    # Bug #6: same coercion for outfield sets — otherwise {"2","3",...} vs
    # {2, 3, ...} silently scores a full rotation despite identical players.
    prior_outfield = {_coerce_id(p) for p in (prior_xi.get("outfield_ids") or set())}
    curr_outfield = {_coerce_id(p) for p in (current_xi.get("outfield_ids") or set())}
    if prior_outfield:
        dropped = prior_outfield - curr_outfield
        # `dropped` = players who started before but not now. Equivalent to
        # the legacy `symmetric_difference // 2` count because every "out"
        # is paired with an "in" within an 11-player XI.
        diff = len(dropped)
        if diff >= HEAVY_ROTATION_THRESHOLD:
            prior_pos = prior_xi.get("position_by_id") or {}
            # `prior_pos` may have str OR int keys depending on whether the
            # caller went through extract_starting_xi or hand-built the dict.
            # Look up under both forms so the position weight still applies.
            def _pos_for(pid: int | None) -> str:
                if pid is None:
                    return ""
                return prior_pos.get(pid) or prior_pos.get(str(pid)) or ""
            weighted = sum(
                POSITION_WEIGHTS.get(_pos_for(pid), DEFAULT_POSITION_WEIGHT)
                for pid in dropped
            )
            delta += weighted * PENALTY_PER_WEIGHTED_SWAP
            reasons.append(f"{diff} outfield changes")

    return delta, ("; ".join(reasons) if reasons else None)
