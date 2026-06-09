"""
apply_matchday_adjustments.py — Stream B.1 foundation.

Reads every matchday-intelligence layer (injuries, lineups, weather,
stats proxy), validates each adjustment against its per-layer cap, sums
them into a single per-team Elo adjustment respecting the aggregate cap,
emits a consolidated dashboard JSON, and APPENDS every decision to an
audit log so we can always answer "why did this probability move?".

This module ships first (before the actual fetchers) because:
  - Every fetcher needs a stable write target with a documented schema
  - The audit log must capture decisions from tick 1, not be added later
  - The simulator integration point (a single read at elo_eff_base) is
    a one-line change that gates everything else

Inputs (any may be missing — module degrades gracefully):
  data/live/team_adjustments.json       — injuries/suspensions (existing,
                                          updated by B.3 fetcher)
  data/live/weather_2026.json           — per-match weather (B.2)
  data/live/lineups_2026.json           — per-match lineup deltas (B.4)
  data/live/match_stats_2026.json       — post-match stats proxy (B.5)

Outputs:
  dashboard/matchday_intelligence.json  — the consolidated state the
                                          dashboard polls every 60s
  data/live/matchday_intelligence_log.jsonl
                                        — append-only audit log; one
                                          line per tick, retained for
                                          the tournament duration

Caps (locked per user decisions on Stream B sequencing):
  Injuries/suspensions: ±25 normal, ±35 extreme  (per-team-per-match)
  Lineups:              ±20                       (per-team-per-match)
  Weather:              ±15                       (per-team-per-match)
  Stats proxy:          ±8 per match, ±20 over group stage
  AGGREGATE matchday:   ±35 per team per match  (excludes live_team_state)
  GRAND TOTAL:          ±45 (matchday + live_team_state combined)

API used by 03_simulate.py (single integration point at elo_eff_base):
  get_team_elo_adjustment(team, match_id) -> float

Run as CLI for debugging / dry-run:
    python3 scripts/live/apply_matchday_adjustments.py
    python3 scripts/live/apply_matchday_adjustments.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LIVE = ROOT / "data" / "live"
RAW = ROOT / "data" / "raw"
DASH = ROOT / "dashboard"
LOG_PATH = LIVE / "matchday_intelligence_log.jsonl"
OUT_PATH = DASH / "matchday_intelligence.json"

# ── Caps (Elo, signed) ──────────────────────────────────────────────────
INJURY_CAP_NORMAL = 25.0
INJURY_CAP_EXTREME = 35.0
LINEUP_CAP = 20.0
WEATHER_CAP = 15.0
STATS_CAP_PER_MATCH = 8.0
STATS_CAP_GROUP_TOTAL = 20.0
AGGREGATE_MATCHDAY_CAP = 35.0   # injuries + lineups + weather + stats proxy
GRAND_TOTAL_CAP = 45.0          # + live_team_state delta


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Same atomic write pattern as fetch_results / run_live_update."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent),
        prefix=path.name + ".", suffix=".tmp", delete=False,
    ) as tmp:
        json.dump(payload, tmp, indent=2, ensure_ascii=False)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def _read_json(path: Path, default: Any = None) -> Any:
    """Load JSON if present; on any parse/read error, return default and
    let the caller treat it as 'feed unavailable'."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception as e:
        print(f"[matchday] WARN: failed to read {path.name}: {e}")
        return default


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Per-layer adjustment loaders ────────────────────────────────────────
# Each loader returns {(team, match_id_or_None): component_dict}. The
# match_id key is None for layers that apply tournament-wide (e.g. a
# team-level injury covering multiple matches).

def _load_injury_components(now_iso: str) -> dict:
    """B.3: read API-sourced injuries_2026.json and merge with the legacy
    manual overlay in team_adjustments.json.

    The legacy loader in 03_simulate.py used to own team_adjustments.json and
    feed `elo_eff_base` directly. In B.3 we centralise: API-Football is the
    primary source, team_adjustments.json is an OVERLAY for operator manual
    notes (the tournament team can still set tier_1_star for a Mbappé-tier
    out, since the API doesn't expose importance).

    Combination rule per team:
      sum = api_total_elo + manual_overlay_elo

    The aggregate matchday cap downstream still clamps the team-match
    contribution at ±35 even if both layers stack.

    Backwards compatible with the legacy team_adjustments.json schema:
      - Honours `expires_at` (filters expired entries)
      - Honours `approved` (default True, matches legacy)
      - Halves `adjustment_elo` for status == "doubtful" (legacy 0.5x)
    """
    out: dict[tuple[str, int | None], list[dict]] = {}

    # Per-team API totals (tournament-wide, not match-scoped).
    api_path = LIVE / "injuries_2026.json"
    api_data = _read_json(api_path, default={}) or {}
    for team, blob in (api_data.get("teams") or {}).items():
        raw = float(blob.get("total_elo_adjustment", 0.0) or 0.0)
        # API source uses the "normal" injury cap (manual overlay can push
        # toward "extreme" — see overlay block below).
        capped = max(-INJURY_CAP_NORMAL, min(INJURY_CAP_NORMAL, raw))
        if capped == 0.0:
            continue
        n_players = len(blob.get("players") or [])
        out.setdefault((team, None), []).append({
            "type": "injury",
            "subtype": "api_aggregate",
            "raw_elo": raw,
            "capped_elo": capped,
            "cap_used": INJURY_CAP_NORMAL,
            "n_players": n_players,
            "source": "api_football",
        })

    # Manual overlay (operator-curated tier_1 / suspensions / notes).
    overlay_path = LIVE / "team_adjustments.json"
    overlay = _read_json(overlay_path, default={}) or {}
    overlay_by_team: dict[str, float] = {}
    overlay_reasons: dict[str, list[str]] = {}
    for adj in overlay.get("adjustments", []) or []:
        # Legacy semantics: approved defaults True, doubtful → 0.5x, expired skipped.
        if not adj.get("approved", True):
            continue
        exp = adj.get("expires_at")
        if exp:
            try:
                # Tolerate both "Z" and offset-suffixed timestamps.
                exp_iso = exp.replace("Z", "+00:00")
                if exp_iso < now_iso:
                    continue
            except Exception:
                pass
        amount = float(adj.get("adjustment_elo", 0) or 0)
        if adj.get("status") == "doubtful":
            amount *= 0.5
        team = adj.get("team")
        if not team:
            continue
        overlay_by_team[team] = overlay_by_team.get(team, 0.0) + amount
        overlay_reasons.setdefault(team, []).append(
            adj.get("reason") or adj.get("player") or adj.get("status") or "manual"
        )
    for team, raw in overlay_by_team.items():
        # Overlay may exceed the "normal" cap if the operator flagged extreme
        # circumstances (multiple tier_1 players out) — use extreme cap here.
        capped = max(-INJURY_CAP_EXTREME, min(INJURY_CAP_EXTREME, raw))
        if capped == 0.0:
            continue
        out.setdefault((team, None), []).append({
            "type": "injury",
            "subtype": "manual_overlay",
            "raw_elo": raw,
            "capped_elo": capped,
            "cap_used": INJURY_CAP_EXTREME,
            "reasons": overlay_reasons.get(team, []),
            "source": "team_adjustments_manual",
        })

    return out


def _load_weather_components() -> dict:
    """Read weather_2026.json. Schema written by B.2 fetch_weather.

    Each entry: {match_id, home_team_adjustment_elo, away_team_adjustment_elo,
                 lambda_adjustment, weather_bucket, ...}.
    """
    data = _read_json(LIVE / "weather_2026.json", default={}) or {}
    entries = data.get("weather") or []
    out: dict[tuple[str, int | None], list[dict]] = {}
    for w in entries:
        m_id = w.get("match_id")
        if m_id is None:
            continue
        bucket = w.get("weather_bucket")
        confidence = w.get("confidence")
        # Two sides — each may have an independent acclimatisation penalty.
        for side in ("home", "away"):
            team = w.get(f"{side}_team")
            if not team:
                continue
            raw = float(w.get(f"{side}_team_adjustment_elo", 0.0) or 0.0)
            capped = max(-WEATHER_CAP, min(WEATHER_CAP, raw))
            if capped == 0.0:
                continue
            out.setdefault((team, m_id), []).append({
                "type": "weather",
                "weather_bucket": bucket,
                "raw_elo": raw,
                "capped_elo": capped,
                "cap_used": WEATHER_CAP,
                "confidence": confidence,
                "source": "open_meteo",
            })
    return out


def _load_lineup_components() -> dict:
    """Read lineups_2026.json. Schema written by B.4 fetch_lineups.

    Each entry: {match_id, home_team_adjustment_elo, away_team_adjustment_elo,
                 reason, baseline_source, ...}. Display-only entries have
                 adjustment_elo == 0 — those still appear in the dashboard
                 JSON but contribute nothing to the Elo sum.
    """
    data = _read_json(LIVE / "lineups_2026.json", default={}) or {}
    entries = data.get("lineups") or []
    out: dict[tuple[str, int | None], list[dict]] = {}
    for ln in entries:
        m_id = ln.get("match_id")
        if m_id is None:
            continue
        for side in ("home", "away"):
            team = ln.get(side)
            if not team:
                continue
            raw = float(ln.get(f"{side}_team_adjustment_elo", 0.0) or 0.0)
            capped = max(-LINEUP_CAP, min(LINEUP_CAP, raw))
            if capped == 0.0:
                continue
            out.setdefault((team, m_id), []).append({
                "type": "lineup",
                "raw_elo": raw,
                "capped_elo": capped,
                "cap_used": LINEUP_CAP,
                "reason": ln.get(f"{side}_adjustment_reason"),
                "baseline_source": ln.get("baseline_source", "unknown"),
                "source": "api_football",
            })
    return out


def _load_stats_components() -> dict:
    """Read match_stats_2026.json. Schema written by B.5 fetch_match_stats.

    Post-match only. Each entry adjusts the TEAM's live form (not a specific
    upcoming match), so match_id key is None. Group-stage total cap applied
    by aggregating per-team across all completed matches.
    """
    data = _read_json(LIVE / "match_stats_2026.json", default={}) or {}
    entries = data.get("matches") or []
    # Aggregate per team across all matches, applying both per-match and
    # group-stage caps.
    per_team_total: dict[str, list[dict]] = {}
    for s in entries:
        if s.get("status") != "FT":
            continue
        for side in ("home", "away"):
            team = s.get(side)
            if not team:
                continue
            raw = float(s.get(f"{side}_form_adjustment_elo", 0.0) or 0.0)
            capped = max(-STATS_CAP_PER_MATCH, min(STATS_CAP_PER_MATCH, raw))
            if capped == 0.0:
                continue
            per_team_total.setdefault(team, []).append({
                "type": "stats_proxy",
                "match_id": s.get("match_id"),
                "raw_elo": raw,
                "capped_elo_per_match": capped,
                "cap_per_match": STATS_CAP_PER_MATCH,
                "true_xg_available": s.get("true_xg_available", False),
                "source": "api_football",
            })
    # Apply group-stage total cap per team.
    out: dict[tuple[str, int | None], list[dict]] = {}
    for team, components in per_team_total.items():
        running_sum = 0.0
        for c in components:
            # If adding this component would exceed the group total cap,
            # truncate this component's contribution.
            remaining_budget = STATS_CAP_GROUP_TOTAL - abs(running_sum)
            sign = 1.0 if c["capped_elo_per_match"] >= 0 else -1.0
            allowed = sign * min(abs(c["capped_elo_per_match"]), max(0.0, remaining_budget))
            c["capped_elo"] = allowed
            c["cap_used"] = STATS_CAP_GROUP_TOTAL
            c["cap_reason"] = (
                "group_total" if abs(allowed) < abs(c["capped_elo_per_match"]) else "per_match"
            )
            running_sum += allowed
            if allowed == 0.0:
                continue
            out.setdefault((team, None), []).append(c)
    return out


# ── Core aggregation ────────────────────────────────────────────────────
def build_adjustments_state(now_iso: str | None = None) -> dict:
    """Read all layers, apply caps, return the consolidated state dict.

    The returned dict is what gets written to matchday_intelligence.json
    AND appended as one record to the audit log. Pure function aside from
    file reads — no writes here, the caller decides.
    """
    now_iso = now_iso or _now_iso()
    inj = _load_injury_components(now_iso)
    wx = _load_weather_components()
    ln = _load_lineup_components()
    stats = _load_stats_components()

    # Merge component lists by (team, match_id).
    components_by_key: dict[tuple[str, int | None], list[dict]] = {}
    for src in (inj, wx, ln, stats):
        for key, comps in src.items():
            components_by_key.setdefault(key, []).extend(comps)

    # Sum + apply aggregate matchday cap per (team, match_id).
    per_team_per_match: list[dict] = []
    for (team, m_id), comps in sorted(components_by_key.items(), key=lambda x: (x[0][0], x[0][1] or 0)):
        raw_sum = sum(c.get("capped_elo", 0.0) for c in comps)
        capped_sum = max(-AGGREGATE_MATCHDAY_CAP, min(AGGREGATE_MATCHDAY_CAP, raw_sum))
        aggregate_cap_applied = (capped_sum != raw_sum)
        per_team_per_match.append({
            "team": team,
            "match_id": m_id,
            "total_elo_adjustment": round(capped_sum, 3),
            "raw_sum_before_aggregate_cap": round(raw_sum, 3),
            "aggregate_cap_applied": aggregate_cap_applied,
            "aggregate_cap": AGGREGATE_MATCHDAY_CAP,
            "components": comps,
        })

    # Top-level dashboard payload.
    state = {
        "generated_at": now_iso,
        "schema_version": 1,
        "caps": {
            "injury_normal": INJURY_CAP_NORMAL,
            "injury_extreme": INJURY_CAP_EXTREME,
            "lineup": LINEUP_CAP,
            "weather": WEATHER_CAP,
            "stats_per_match": STATS_CAP_PER_MATCH,
            "stats_group_total": STATS_CAP_GROUP_TOTAL,
            "aggregate_matchday": AGGREGATE_MATCHDAY_CAP,
            "grand_total_with_live_form": GRAND_TOTAL_CAP,
        },
        "active_adjustments": per_team_per_match,
        "summary": {
            "total_active_components": sum(len(x["components"]) for x in per_team_per_match),
            "teams_affected": len({x["team"] for x in per_team_per_match if x["total_elo_adjustment"] != 0}),
            "matches_affected": len({x["match_id"] for x in per_team_per_match if x["match_id"] is not None and x["total_elo_adjustment"] != 0}),
            "aggregate_caps_hit": sum(1 for x in per_team_per_match if x["aggregate_cap_applied"]),
        },
        "feeds_available": {
            # B.3: this module now owns injuries. We accept EITHER the
            # API-sourced injuries_2026.json OR the legacy manual overlay
            # team_adjustments.json (or both — overlay stacks on API).
            "injuries": (
                (LIVE / "injuries_2026.json").exists()
                or (LIVE / "team_adjustments.json").exists()
            ),
            "injuries_handled_by_this_module": True,
            "weather": (LIVE / "weather_2026.json").exists(),
            "lineups": (LIVE / "lineups_2026.json").exists(),
            "stats_proxy": (LIVE / "match_stats_2026.json").exists(),
        },
        "warnings": [],
    }

    # Surface a friendly warning when a feed THIS MODULE consumes is absent.
    # As of B.3, injuries are owned here (API-Football primary + manual overlay).
    _FEEDS_THIS_MODULE_CONSUMES = {"injuries", "weather", "lineups", "stats_proxy"}
    for feed, present in state["feeds_available"].items():
        if feed not in _FEEDS_THIS_MODULE_CONSUMES:
            continue
        if not present:
            state["warnings"].append({
                "type": "feed_missing",
                "feed": feed,
                "message": f"{feed} feed not present — adjustments from this layer are skipped",
            })

    return state


# ── Audit log ───────────────────────────────────────────────────────────
def append_audit_log(state: dict, workflow_run_id: str | None = None) -> None:
    """Append one JSONL record. Append-only, never rotated mid-tournament."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": state["generated_at"],
        "workflow_run_id": workflow_run_id or os.environ.get("GITHUB_RUN_ID"),
        "summary": state["summary"],
        "feeds_available": state["feeds_available"],
        "warnings": state["warnings"],
        # Compact representation of every non-zero adjustment for replay.
        "active_adjustments": [
            {
                "team": x["team"],
                "match_id": x["match_id"],
                "total_elo": x["total_elo_adjustment"],
                "n_components": len(x["components"]),
                "types": sorted({c["type"] for c in x["components"]}),
            }
            for x in state["active_adjustments"]
            if x["total_elo_adjustment"] != 0
        ],
    }
    # Atomic append: open in 'a' mode is atomic for small writes on POSIX.
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ── Public API for 03_simulate.py ───────────────────────────────────────
_STATE_CACHE: dict | None = None


def _ensure_state(reload: bool = False) -> dict:
    global _STATE_CACHE
    if _STATE_CACHE is None or reload:
        _STATE_CACHE = build_adjustments_state()
    return _STATE_CACHE


def get_team_elo_adjustment(team: str, match_id: int | None = None,
                            reload: bool = False) -> float:
    """Single integration point for 03_simulate.py.

    Returns the total capped Elo adjustment for `team` at `match_id`. If
    `match_id` is None, only tournament-wide adjustments (post-match stats
    proxy, multi-match suspensions) are considered. The simulator calls
    this once per (team, group_match) and aggregates with existing
    live_team_state — the GRAND_TOTAL_CAP enforcement happens in the
    simulator (not here) because that's where live_team_state is known.

    Returns 0.0 if there's no adjustment, never None.
    """
    state = _ensure_state(reload=reload)
    total = 0.0
    for entry in state["active_adjustments"]:
        if entry["team"] != team:
            continue
        # Tournament-wide adjustments (match_id is None) apply to every match.
        # Match-specific adjustments apply only when match_id matches.
        if entry["match_id"] is None or entry["match_id"] == match_id:
            total += entry["total_elo_adjustment"]
    return round(total, 3)


def write_state_and_log(dry_run: bool = False) -> dict:
    """Run the full pipeline. Returns the state dict.

    Called by run_live_update.py on every tick (after fetchers run).
    Always safe to re-run: pure read on inputs, atomic write on output,
    append-only log.
    """
    state = build_adjustments_state()
    if dry_run:
        print("[matchday] dry-run — would write to "
              f"{OUT_PATH.relative_to(ROOT)} and append to "
              f"{LOG_PATH.relative_to(ROOT)}")
        return state
    _atomic_write_json(OUT_PATH, state)
    append_audit_log(state)
    return state


# ── CLI ─────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="Apply matchday intelligence adjustments.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Build state and print summary; do not write files.")
    args = ap.parse_args()

    state = write_state_and_log(dry_run=args.dry_run)
    s = state["summary"]
    print(f"[matchday] active components: {s['total_active_components']}")
    print(f"[matchday] teams affected: {s['teams_affected']}")
    print(f"[matchday] matches affected: {s['matches_affected']}")
    print(f"[matchday] aggregate caps hit: {s['aggregate_caps_hit']}")
    if state["warnings"]:
        print(f"[matchday] warnings: {len(state['warnings'])}")
        for w in state["warnings"][:3]:
            print(f"  - {w['type']}: {w['message']}")
    if not args.dry_run:
        print(f"[matchday] wrote {OUT_PATH.relative_to(ROOT)}")
        print(f"[matchday] appended audit log {LOG_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
