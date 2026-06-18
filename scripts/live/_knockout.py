"""
_knockout.py — shared helpers for loading the FIFA WC 2026 knockout
bracket alongside the group-stage schedule.

Pre-Round 6 (R32-critical pass), `suspension_tracker.load_schedule()` and
`fetch_lineups._load_schedule()` each read ONLY `group_stage_schedule`
from wc2026_config.json — every fixture at m=73..104 fell off the
schedule list, so:

  * `next_match_for_team(team, mid, ...)` returned None for any KO
    fixture (m=73..104). A red card in R32 emitted ZERO suspension rows.
  * Yellow accumulation could not bridge R32 → R16 → QF.
  * Lineup polling never targeted any KO fixture (KO lineup intel dark).

`load_knockout_fixtures` is the one place that maps the bracket file's
section layout into schedule rows that `next_match_for_team` and the
lineup poller can consume. Each row carries a `stage` tag ∈ {"r32",
"r16", "qf", "sf", "3rd", "final"} so `build_suspensions` can detect
the QF → SF transition and apply the FIFA WC yellow-card flush rule.

Placeholder slot codes ("1A", "2B", "3A/B/C/D/F", "W74", "L101") stay
opaque until results land — `is_placeholder_slot` returns True for any
unresolved slot so `next_match_for_team` skips emitting bans/lineup
polls until concrete team names appear in the bracket.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"
BRACKET_PATH = RAW / "knockout_bracket_2026.json"

# R9 P4 A1: process-local set tracking which KO match numbers we've already
# warned about missing kickoff times. Without this guard a single tick that
# calls load_knockout_fixtures() repeatedly (or test runs that import the
# module N times) would emit 32×N WARN lines on every invocation.
_KO_DEFAULT_TIME_WARNED: set[int] = set()

# Slot codes are short, alphanumeric, and never contain spaces. We treat
# anything that looks like a slot code as a placeholder and refuse to
# match it to a team. Examples:
#   group-stage feeders   1A, 2B, 3F     (group position + group letter)
#   third-place fan-outs  3A/B/C/D/F     (one of several groups' 3rd)
#   winner/loser of m=N   W74, L101
_GROUP_FEEDER_RE = re.compile(r"^[1-3][A-L]$")
_WINNER_LOSER_RE = re.compile(r"^[WL]\d{2,3}$")


def is_placeholder_slot(value: str | None) -> bool:
    """Return True if `value` is an unresolved bracket slot code rather
    than a concrete team name. Used by `next_match_for_team` to skip
    placeholder home/away entries until results lock in.

    Concrete team names ("Spain", "Cape Verde", "United States") all fall
    through to False — they contain a space, hyphen, or are simply longer
    than two characters and don't match the slot patterns.
    """
    if not value:
        return True
    s = value.strip()
    if not s:
        return True
    if s == "TBD":
        return True
    if "/" in s:  # 3A/B/C/D/F third-place fan-out
        return True
    if _GROUP_FEEDER_RE.match(s):
        return True
    if _WINNER_LOSER_RE.match(s):
        return True
    return False


def load_knockout_fixtures(path: Path = BRACKET_PATH) -> list[dict]:
    """Read the knockout bracket file and return schedule-shaped rows.

    Each returned row has the same minimum shape as a group-stage entry —
    `m`, `date`, `home`, `away`, `venue` — plus a `stage` tag. Pre-result
    `home`/`away` are slot codes (e.g. "1A", "W74") — the caller is
    responsible for resolving these via `is_placeholder_slot` before
    emitting bans or lineup polls.

    Returns [] if the bracket file is missing or malformed — callers
    fall back to group-stage-only behavior in that case.
    """
    if not path.exists():
        return []
    try:
        bracket = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    out: list[dict] = []
    # R9 P4 A1: collect KO matches that are using the "20:00" default so we
    # can emit a single summary warning at the bottom (rather than 32 warns
    # per call). Operator-visible: the dashboard pre-match window (lineups
    # 4h pre-KO) and weather forecast hour both depend on `time`, so a wrong
    # time silently degrades both subsystems for that KO match. Sourcing
    # real FIFA kickoff times into data/raw/knockout_bracket_2026.json is
    # the proper fix — this warning surfaces the gap to operators meanwhile.
    missing_time: list[int] = []
    section_to_stage = (
        ("r32_slots", "r32"),
        ("r16_bracket", "r16"),
        ("qf_bracket", "qf"),
        ("sf_bracket", "sf"),
    )
    for section_key, stage in section_to_stage:
        for s in bracket.get(section_key, []) or []:
            if s.get("time") in (None, ""):
                missing_time.append(s["match_num"])
            out.append({
                "m": s["match_num"],
                "date": s.get("date"),
                "time": s.get("time") or "20:00",
                "venue": s.get("venue"),
                "home": s.get("slot_a"),
                "away": s.get("slot_b"),
                "stage": stage,
            })
    ft = bracket.get("final_and_third_place") or {}
    if "third_place" in ft:
        tp = ft["third_place"]
        if tp.get("time") in (None, ""):
            missing_time.append(tp["match_num"])
        out.append({
            "m": tp["match_num"],
            "date": tp.get("date"),
            "time": tp.get("time") or "20:00",
            "venue": tp.get("venue"),
            "home": tp.get("slot_a"),
            "away": tp.get("slot_b"),
            "stage": "3rd",
        })
    if "final" in ft:
        fn = ft["final"]
        if fn.get("time") in (None, ""):
            missing_time.append(fn["match_num"])
        out.append({
            "m": fn["match_num"],
            "date": fn.get("date"),
            "time": fn.get("time") or "20:00",
            "venue": fn.get("venue"),
            "home": fn.get("slot_a"),
            "away": fn.get("slot_b"),
            "stage": "final",
        })
    # R9 P4 A1: single summary warning (dedup across calls per process).
    new_missing = sorted(set(missing_time) - _KO_DEFAULT_TIME_WARNED)
    if new_missing:
        print(
            f"[_knockout] WARN: {len(new_missing)} KO matches lack `time` in "
            f"data/raw/knockout_bracket_2026.json and default to '20:00' local. "
            f"Affected match_nums: {new_missing}. "
            f"This silently shifts the dashboard's pre-KO lineup-fetch window "
            f"and Open-Meteo weather forecast hour. Source FIFA's official "
            f"kickoff times before R32 (2026-06-28) to close.",
            file=sys.stderr,
        )
        _KO_DEFAULT_TIME_WARNED.update(new_missing)
    return out
