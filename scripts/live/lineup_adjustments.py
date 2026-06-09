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

GK_SWAP_ELO = -8.0
HEAVY_ROTATION_ELO = -3.0
HEAVY_ROTATION_THRESHOLD = 3   # ≥ this many outfield differences = "rotation"

GK_POSITION_CODES = {"G", "GK", "Goalkeeper"}


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


def extract_starting_xi(side_block: dict) -> dict:
    """Return {"gk_id": int|None, "outfield_ids": set[int], "raw_players": list}.

    `side_block` is one entry from the /fixtures/lineups response array
    (one per team). We tolerate missing/malformed fields — the consumer
    treats no-baseline as 0 adjustment, which is safer than guessing.
    """
    start = side_block.get("startXI") or []
    gk_id = None
    outfield: set[int] = set()
    raw: list[dict] = []
    for entry in start:
        pid = _player_id(entry)
        raw.append(entry)
        if pid is None:
            continue
        if _is_gk(entry):
            gk_id = pid
        else:
            outfield.add(pid)
    return {"gk_id": gk_id, "outfield_ids": outfield, "raw_players": raw}


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

    prior_gk = prior_xi.get("gk_id")
    curr_gk = current_xi.get("gk_id")
    if prior_gk and curr_gk and prior_gk != curr_gk:
        delta += GK_SWAP_ELO
        reasons.append("GK swap")

    prior_outfield = prior_xi.get("outfield_ids") or set()
    curr_outfield = current_xi.get("outfield_ids") or set()
    if prior_outfield:
        diff = len(curr_outfield.symmetric_difference(prior_outfield)) // 2
        # symmetric_difference counts both "added" and "removed" — //2 collapses
        # to the number of swaps (a 1-for-1 substitution shows as 2 in sym-diff).
        if diff >= HEAVY_ROTATION_THRESHOLD:
            delta += HEAVY_ROTATION_ELO
            reasons.append(f"{diff} outfield changes")

    return delta, ("; ".join(reasons) if reasons else None)
