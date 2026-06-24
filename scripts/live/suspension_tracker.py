"""
suspension_tracker.py — Stream B / Phase 4 suspension surface.

Reads `data/live/results_2026.json` completed_matches and, for each match
that carries an `events` list (B3 enrichment), walks card events to:

  1. Identify players who picked up a Red Card or Second Yellow card in
     any completed match → banned for that team's NEXT scheduled match.
  2. Identify players who accumulated >= YELLOW_THRESHOLD (FIFA rule: 2)
     yellow cards across the group stage → banned for that team's NEXT
     scheduled match after the second yellow.

Writes `data/live/suspensions_2026.json`. Consumed by
`apply_matchday_adjustments._load_suspension_components`.

§4 fallback (CORRECTIONS.md): if the live snapshot has NO events on
completed matches (data source dry), this script ships an empty
`suspensions` list AND attaches a `no_events_in_snapshot` warning at the
top level so consumers see the source is dry — never present "zero
suspensions" as a confirmed fact when the truth is "we have no data".

Schema (consumed by apply_matchday_adjustments._load_suspension_components):
  {
    "generated_at": ISO8601,
    "schema_version": 1,
    "source": "fetch_results_events",
    "cap_used": 8.0,
    "per_suspension_elo": -3.0,
    "yellow_threshold": 2,
    "suspensions": [
      {
        "match_id": <int next match for the team>,
        "team": str,
        "player": str,
        "reason": "red_card" | "second_yellow_card" | "accumulated_yellows",
        "team_adjustment_elo": float,   # already capped per-player
        "raw_elo": float,
        "cap_used": 8.0,
        "evidence_match_ids": [int, ...],
        "confidence": "high",
        "source": "fetch_results_events"
      }, ...
    ],
    "warnings": [],
    "summary": {
      "n_completed_matches": int,
      "n_with_events": int,
      "n_suspensions": int
    }
  }

CLI:
  python3 scripts/live/suspension_tracker.py
  python3 scripts/live/suspension_tracker.py --dry-run
  python3 scripts/live/suspension_tracker.py \
        --results tests/live/suspension_fixtures/results_with_events.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIVE = ROOT / "data" / "live"
RAW = ROOT / "data" / "raw"
OUT_PATH = LIVE / "suspensions_2026.json"
SCHEDULE_PATH = RAW / "wc2026_config.json"
BRACKET_PATH = RAW / "knockout_bracket_2026.json"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _knockout import (  # noqa: E402
    is_placeholder_slot, load_knockout_fixtures,
)
# R12 A1: normalize player names on yellow-accumulation join keys. Pre-R12
# `yellow_counter[(team, player)]` used the raw provider event string. When
# API-Football emits the same player as "R. Jiménez" in one match and
# "Raúl Jiménez" in another (provider-side initial-form drift on accented
# names — verified in tests/live/provider_samples/apifootball_events_sample.json),
# the counter splits across keys and never reaches YELLOW_THRESHOLD=2 →
# silent zero suspension rows. The display field on the suspension row
# preserves the original raw name from the triggering event so the dashboard
# row still reads the way the operator sees it.
from injury_adjustments import normalize_player_name, player_join_key  # noqa: E402

# ── Tunables ────────────────────────────────────────────────────────────
# Per-player Elo penalty for a single suspension. Conservative — without
# per-player importance ranking at this stage, this stays well under the
# cap so multiple suspensions can stack toward SUSPENSION_CAP. Lineup
# layer would have caught the player if available; suspensions account
# for the part of the absence that lineups DON'T see (banned players
# never appear in the XI to begin with).
PER_SUSPENSION_ELO = -3.0
# Per-team-per-match cap. Matches the referee cap (Phase 2) precedent for
# single-match binary events and the SUSPENSION_CAP wired into
# apply_matchday_adjustments. Two layers of clamping (here + loader).
SUSPENSION_CAP = 8.0
# FIFA WC accumulation rule: two yellows over the group stage = one-match
# suspension. Yellows are wiped after the quarter-finals.
YELLOW_THRESHOLD = 2


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent),
        prefix=path.name + ".", suffix=".tmp", delete=False,
    ) as tmp:
        # R9 P3: allow_nan=False at producer boundary — apply_matchday
        # reads this file; pre-R9 only the matchday writer rejected NaN.
        json.dump(payload, tmp, indent=2, ensure_ascii=False, allow_nan=False)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def _read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def load_schedule(path: Path = SCHEDULE_PATH,
                  bracket_path: Path = BRACKET_PATH) -> list[dict]:
    """Return group + knockout schedule rows sorted by match_id.

    Round 6 R32-critical fix: prior to this version `load_schedule` read
    ONLY `group_stage_schedule`, so `next_match_for_team` returned None
    for every knockout fixture (m=73..104). A red card in R32 emitted
    zero suspension rows; yellow accumulation across knockout rounds
    silently capped at the group → R32 boundary.

    Group rows keep their original shape but pick up `stage="group"` so
    callers can detect stage transitions uniformly. Knockout rows are
    loaded via `_knockout.load_knockout_fixtures` with `stage` ∈ {"r32",
    "r16", "qf", "sf", "3rd", "final"}; `home`/`away` may be unresolved
    slot codes (e.g. "1A", "W74") until results lock in — those rows are
    still present so the schedule is contiguous, and
    `next_match_for_team` skips them via `is_placeholder_slot`.
    """
    cfg = _read_json(path, default={}) or {}
    group = cfg.get("group_stage_schedule") or []
    if not isinstance(group, list):
        group = []
    merged: list[dict] = []
    for row in group:
        # Backward-compat: leave the original row intact, just tag stage.
        # Group-stage callers that pre-date this field continue to work.
        if isinstance(row, dict):
            tagged = dict(row)
            tagged.setdefault("stage", "group")
            merged.append(tagged)
    merged.extend(load_knockout_fixtures(bracket_path))
    return sorted(merged, key=lambda r: int(r.get("m", 0)))


def next_match_for_team(team: str, after_m: int,
                        schedule: list[dict]) -> int | None:
    """Return the smallest match_id > after_m where `team` plays. None if
    no such fixture (team already finished, wrong name, or all remaining
    knockout slots for the team are unresolved placeholders).

    Round 6: placeholder-slot guard. Knockout rows arrive with `home`/
    `away` set to slot codes ("1A", "W74", "3A/B/C/D/F") until the
    bracket resolves. Matching a real team name against those codes is
    impossible AND we must not emit a ban targeting an unresolved slot
    (it would attach to whichever real team eventually fills that slot).
    `is_placeholder_slot` filters them out.
    """
    for row in schedule:
        try:
            mid = int(row.get("m", 0))
        except (TypeError, ValueError):
            continue
        if mid <= after_m:
            continue
        home = row.get("home")
        away = row.get("away")
        # Skip rows where the slot we'd need to match is a placeholder.
        # If a real team has been written into one side already (after
        # earlier KO results land), we still match on that side.
        home_ok = not is_placeholder_slot(home)
        away_ok = not is_placeholder_slot(away)
        if home_ok and home == team:
            return mid
        if away_ok and away == team:
            return mid
    return None


def _card_kind(ev: dict) -> str | None:
    """Map a normalized event to a card kind or None if not a card.

    Returns one of "yellow", "red", "second_yellow". The fetch_results
    normalizer (normalize_event) sets type='card' and a subtype slugged
    from `detail`. We pattern-match on the slug rather than enumerate
    every provider variant.
    """
    if (ev.get("type") or "").lower() != "card":
        return None
    sub = (ev.get("subtype") or "").lower()
    if sub.startswith("second"):
        return "second_yellow"
    if sub.startswith("red"):
        return "red"
    if sub.startswith("yellow"):
        return "yellow"
    return None


def _stage_from_match_id(mid: int) -> str:
    """FIFA WC 2026 stage layout, indexed by match number:
      1..72   group, 73..88 r32, 89..96 r16, 97..100 qf,
      101..102 sf, 103 3rd, 104 final.
    Used as a fallback for callers that pass a stage-less schedule
    (legacy tests built before Round 6 added the `stage` field)."""
    if mid <= 72:
        return "group"
    if mid <= 88:
        return "r32"
    if mid <= 96:
        return "r16"
    if mid <= 100:
        return "qf"
    if mid <= 102:
        return "sf"
    if mid == 103:
        return "3rd"
    if mid == 104:
        return "final"
    return "group"


def _stage_for_match(mid: int, schedule: list[dict]) -> str:
    """Return the stage tag for `mid` ("group"/"r32"/.../"final").
    Prefers an explicit `stage` field on the matching schedule row;
    falls back to `_stage_from_match_id(mid)` when the row lacks it
    (or the row is absent entirely — e.g. legacy unit tests that pass
    a stage-less schedule). This keeps the QF-flush logic working even
    when the test fixture's schedule predates the stage-tagging."""
    for row in schedule:
        try:
            if int(row.get("m", -1)) == mid:
                stage = row.get("stage")
                if stage:
                    return str(stage)
                break
        except (TypeError, ValueError):
            continue
    return _stage_from_match_id(mid)


def build_suspensions(completed_matches: list[dict],
                      schedule: list[dict]) -> tuple[list[dict], dict]:
    """Walk completed_matches in order; emit one suspension row per
    (player, triggering_match) and resolve the upcoming match each
    suspension applies to. Returns (suspensions, summary).

    Yellow accumulation is tracked per (team, player) across the group
    stage. The moment the count hits YELLOW_THRESHOLD, we emit ONE
    suspension row for that player and reset their counter (post-ban
    yellows accumulate fresh).

    Round 6 FIFA WC QF-flush rule: per FIFA regulations (consistent with
    WC 2018 / WC 2022 precedent), single yellow cards are wiped at the
    end of the quarter-finals so a player on one yellow going into the
    semi-finals starts on a clean slate. We detect the QF→SF (or
    QF→anything-later) transition between match iterations and zero the
    yellow_counter for every (team, player) key whose accumulated count
    sits at 1. Reds and accumulation bans already emitted by that point
    are unaffected — they target a concrete next match and the ban
    stands. The flush only erases the unconverted carry-over.
    """
    yellow_counter: dict[tuple[str, str], int] = {}
    # Track the matches where each yellow was earned so evidence_match_ids
    # captures the triggering pair, not just the most recent match.
    yellow_evidence: dict[tuple[str, str], list[int]] = {}
    suspensions: list[dict] = []
    n_with_events = 0
    n_completed = 0
    matches_sorted = sorted(
        completed_matches, key=lambda m: int(m.get("m", 0))
    )
    # Track the stage of the previous processed match so we can detect a
    # transition out of QF and flush the yellow tally before the next
    # match's events are processed. last_stage starts as None so the very
    # first iteration never flushes.
    last_stage: str | None = None
    for match in matches_sorted:
        # Bug #3 fix: a completed match missing its 'm' field cannot resolve
        # to a real fixture row. Without this guard, int(match.get('m', 0))
        # silently became 0, then next_match_for_team would return the first
        # scheduled fixture for the team, producing a phantom ban targeting
        # an arbitrary match. Drop the row loudly (skip — caller's results
        # file is malformed and the suspension surface refuses to guess).
        if match.get("m") is None:
            continue
        try:
            mid = int(match["m"])
        except (TypeError, ValueError):
            continue
        n_completed += 1
        # QF-flush detection: resolve the current match's stage from the
        # schedule, and if we're transitioning AWAY from QF into a later
        # round (SF / 3rd / final), zero every yellow_counter sitting at
        # exactly 1. Counts of 0 are already empty and counts of >=2
        # would already have emitted bans + reset, so the "==1" branch
        # is the only carry-over the flush erases.
        current_stage = _stage_for_match(mid, schedule)
        if last_stage == "qf" and current_stage in ("sf", "3rd", "final"):
            for k in list(yellow_counter.keys()):
                if yellow_counter[k] == 1:
                    yellow_counter[k] = 0
                    yellow_evidence[k] = []
        last_stage = current_stage
        events = match.get("events")
        if not isinstance(events, list) or not events:
            continue
        n_with_events += 1
        # Bug #1 / #2 fix: per-match dedup. Provider duplication or two
        # concatenated feed sources can land the same card incident in
        # `events` twice. Without this set, a duplicate red emits two
        # suspension rows and a duplicate yellow within ONE match double-
        # counts toward the accumulation threshold (false ban). Key by
        # (team, player, kind) — the first occurrence wins, repeats drop.
        seen: set[tuple[str, str, str]] = set()
        for ev in events:
            if not isinstance(ev, dict):
                continue
            kind = _card_kind(ev)
            if kind is None:
                continue
            team = ev.get("team")
            player = ev.get("player")
            if not team or not player:
                continue
            # R12 A1: collapse cross-feed initial-form drift on the join
            # keys (per-match dedup, yellow_counter, yellow_evidence). The
            # display string `player` is preserved unchanged so dashboard
            # rows show the original provider name; player_join_key (the
            # stronger normalization that drops single-letter initials and
            # falls back to the surname token) is used only for joining
            # across matches where the provider may emit "R. Jiménez" in
            # one match and "Raúl Jiménez" in another for the same player.
            player_key = player_join_key(player) or player
            seen_key = (team, player_key, kind)
            if seen_key in seen:
                continue
            seen.add(seen_key)
            key = (team, player_key)
            if kind == "yellow":
                yellow_counter[key] = yellow_counter.get(key, 0) + 1
                yellow_evidence.setdefault(key, []).append(mid)
                if yellow_counter[key] >= YELLOW_THRESHOLD:
                    next_m = next_match_for_team(team, mid, schedule)
                    if next_m is not None:
                        suspensions.append({
                            "match_id": next_m,
                            "team": team,
                            "player": player,
                            "player_norm": player_key,
                            "reason": "accumulated_yellows",
                            "evidence_match_ids": list(yellow_evidence[key]),
                            "triggering_match_id": mid,
                        })
                    yellow_counter[key] = 0
                    yellow_evidence[key] = []
            elif kind in ("red", "second_yellow"):
                next_m = next_match_for_team(team, mid, schedule)
                if next_m is not None:
                    suspensions.append({
                        "match_id": next_m,
                        "team": team,
                        "player": player,
                        "player_norm": player_key,
                        "reason": ("red_card" if kind == "red"
                                   else "second_yellow_card"),
                        "evidence_match_ids": [mid],
                        "triggering_match_id": mid,
                    })
    # Bug #4 fix: final idempotency key. Even with per-match dedup, two
    # independent providers feeding into the same pipeline could surface the
    # same (team, player, match_id, reason) tuple from different source
    # matches. Collapse the final list on that tuple before _attach_elo so
    # the dashboard never lists the same banned player twice. First write
    # wins — preserves evidence_match_ids from the earliest emission.
    deduped: list[dict] = []
    final_seen: set[tuple[str, str, int, str]] = set()
    for row in suspensions:
        # R12 A1: key the cross-provider idempotency tuple on the
        # stronger join form so providers that emit "R. Jiménez" and
        # "Raúl Jiménez" for the same suspension don't both land.
        # player_norm was attached above; fall back to player_join_key
        # on the raw player for any pre-R12 callsite that bypasses the
        # writer.
        norm = (row.get("player_norm")
                or player_join_key(row["player"])
                or row["player"])
        final_key = (row["team"], norm, row["match_id"], row["reason"])
        if final_key in final_seen:
            continue
        final_seen.add(final_key)
        deduped.append(row)
    suspensions = deduped
    summary = {
        "n_completed_matches": n_completed,
        "n_with_events": n_with_events,
        "n_suspensions": len(suspensions),
    }
    return suspensions, summary


def _attach_elo(suspensions: list[dict]) -> list[dict]:
    """Stamp every suspension with raw_elo / team_adjustment_elo /
    cap_used / confidence. Per-player penalty stacks naturally at the
    apply_matchday_adjustments cap — we still write the per-player
    capped value here so the dashboard view never displays a row whose
    `team_adjustment_elo` exceeds the cap."""
    out: list[dict] = []
    for s in suspensions:
        raw = PER_SUSPENSION_ELO
        capped = max(-SUSPENSION_CAP, min(SUSPENSION_CAP, raw))
        row = dict(s)
        row["raw_elo"] = raw
        row["team_adjustment_elo"] = capped
        row["cap_used"] = SUSPENSION_CAP
        row["confidence"] = "high"
        row["source"] = "fetch_results_events"
        out.append(row)
    return out


def build_payload(results_path: Path = LIVE / "results_2026.json",
                  schedule_path: Path = SCHEDULE_PATH,
                  now_iso: str | None = None) -> dict:
    now = now_iso or _now_iso()
    results = _read_json(results_path, default=None)
    warnings: list[dict] = []
    if not isinstance(results, dict):
        return {
            "generated_at": now,
            "schema_version": 1,
            "source": "fetch_results_events",
            "cap_used": SUSPENSION_CAP,
            "per_suspension_elo": PER_SUSPENSION_ELO,
            "yellow_threshold": YELLOW_THRESHOLD,
            "suspensions": [],
            "warnings": [{
                "type": "results_missing",
                "message": f"results file not found at {results_path}",
            }],
            "summary": {
                "n_completed_matches": 0,
                "n_with_events": 0,
                "n_suspensions": 0,
            },
        }
    completed = results.get("completed_matches") or []
    schedule = load_schedule(schedule_path)
    # The group-stage config is the load-bearing input; the knockout
    # bracket is a Round 6 add-on. If the group config is missing OR
    # gives us no rows, fire the existing schedule_missing warning even
    # if the KO bracket loaded — pre-R32 the KO rows alone are not
    # enough to resolve group-stage bans.
    cfg_probe = _read_json(schedule_path, default=None)
    group_rows = (cfg_probe or {}).get("group_stage_schedule") if cfg_probe else None
    if not group_rows:
        warnings.append({
            "type": "schedule_missing",
            "message": (
                f"schedule not found or empty at {schedule_path} — "
                "suspensions cannot resolve to upcoming matches"
            ),
        })
    suspensions, summary = build_suspensions(completed, schedule)
    suspensions = _attach_elo(suspensions)

    # §4 fallback: dry-source warning is the entire point of this tracker
    # existing on day one. Without this surface, a card-eventless snapshot
    # would render as "zero players suspended" — the silent-hole failure
    # mode we explicitly refuse.
    if summary["n_completed_matches"] > 0 and summary["n_with_events"] == 0:
        warnings.append({
            "type": "no_events_in_snapshot",
            "n_completed": summary["n_completed_matches"],
            "n_with_events": 0,
            "message": (
                "Live events not enriched on any completed match. "
                "Suspension tracker returns empty — this is NOT a "
                "confirmation that no players are suspended. Run "
                "`fetch_results.py --with-events` to populate."
            ),
        })

    return {
        "generated_at": now,
        "schema_version": 1,
        "source": "fetch_results_events",
        "cap_used": SUSPENSION_CAP,
        "per_suspension_elo": PER_SUSPENSION_ELO,
        "yellow_threshold": YELLOW_THRESHOLD,
        "suspensions": suspensions,
        "warnings": warnings,
        "summary": summary,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Build WC26 suspension tracker.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--results", type=Path, default=LIVE / "results_2026.json",
                    help="Path to results_2026.json (or fixture).")
    ap.add_argument("--schedule", type=Path, default=SCHEDULE_PATH,
                    help="Path to wc2026_config.json with group_stage_schedule.")
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    args = ap.parse_args()
    payload = build_payload(results_path=args.results,
                            schedule_path=args.schedule)
    print(f"[suspension_tracker] completed={payload['summary']['n_completed_matches']} "
          f"with_events={payload['summary']['n_with_events']} "
          f"suspensions={payload['summary']['n_suspensions']} "
          f"warnings={len(payload['warnings'])}")
    if args.dry_run:
        print(f"[suspension_tracker] dry-run — would write {args.out.relative_to(ROOT)}")
        return 0
    _atomic_write_json(args.out, payload)
    print(f"[suspension_tracker] wrote {args.out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
