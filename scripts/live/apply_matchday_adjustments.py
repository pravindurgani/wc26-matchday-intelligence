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

# Round 5: graceful per-record / per-subsystem degradation. Math layer
# raises loudly on bad inputs (ValueError on NaN xG, TypeError on
# non-str names, etc.) — the orchestrator catches so a single bad
# record / subsystem doesn't abort the whole tick.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _degrade import degrade_record, degrade_subsystem  # noqa: E402

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
REFEREE_CAP = 8.0
SUSPENSION_CAP = 8.0
STATS_CAP_PER_MATCH = 8.0
STATS_CAP_TOURNAMENT_TOTAL = 20.0  # Tournament-wide stats-proxy cap (formerly tournament_total under a group-only name; value unchanged).
AGGREGATE_MATCHDAY_CAP = 35.0   # injuries + lineups + weather + stats proxy
GRAND_TOTAL_CAP = 45.0          # + live_team_state delta


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Same atomic write pattern as fetch_results / run_live_update.

    R8 O2: allow_nan=False rejects NaN / Infinity at the producer side. The
    matchday_intelligence.json file feeds 03_simulate's `base_intel_plus_state`
    lookup at predict_lambdas; pre-R8 a corrupted upstream that produced an
    Infinity elo adjustment would silently round-trip through json (CPython
    accepts/emits Infinity by default) and propagate NaN into nbinom.pmf and
    onward to a NaN p_champion in the dashboard. Fail-loud on the WRITE
    instead of fail-silent-NaN on the READ. Clean runs are unaffected; this
    only fires when something upstream has already broken.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent),
        prefix=path.name + ".", suffix=".tmp", delete=False,
    ) as tmp:
        json.dump(payload, tmp, indent=2, ensure_ascii=False, allow_nan=False)
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


# ── Freshness guard (Wave-2 S1) ─────────────────────────────────────────
# Why 6h: matchday-intel-slow.yml runs every 3h; anything older than
# 2 ticks is genuinely stale (one missed tick is recoverable, two means
# the producer or upstream API is down). Reference clock = results_2026.json
# mtime — the fast workflow refreshes it every 10 min, so it's the freshest
# pipeline input. We compare against that rather than wall-clock so an
# offline replay against a frozen snapshot doesn't false-alarm.
#
# The guard is LOUD-DEGRADE-WARN, not crash-the-tick: missing/stale files
# still flow through `_read_json(default={})` to neutral zero-adjustment,
# but a `subsystem_stale` warning lands in `degradation_warnings` so the
# dashboard can render the pill and operators see WHY adjustments dropped
# to zero. Round 5's loud-degrade-warn precedent (see _degrade.py) applies.
STALENESS_MAX_AGE_HOURS = 6.0  # 2 slow-cron ticks (cron = every 3h)


# R9 P5 B1: content-timestamp source for freshness comparison.
# Pre-R9 `_check_freshness` used `path.stat().st_mtime` exclusively.
# In CI, `actions/checkout@v6` resets every checked-out file's mtime to
# checkout time (within microseconds), so age_delta_seconds ≈ 0 always
# and the freshness guard was a no-op there. A subsystem could be stale
# for DAYS without firing `subsystem_stale` — defeating the entire
# Wave-2 S1 freshness defense. Producers all write a `generated_at` (or
# `updated_at` for results_2026.json) field carrying their actual
# generation time; reading that gives us real freshness regardless of
# filesystem mtime semantics. Local dev / replays still work — they
# carry honest content timestamps too.
_FRESHNESS_TIMESTAMP_KEYS = (
    "generated_at", "updated_at", "last_updated_utc", "last_updated",
)


def _freshness_timestamp_seconds(path: Path) -> tuple[float | None, str]:
    """Return (epoch_seconds, source) where source ∈
    {'content', 'mtime', 'corrupt_fallback_mtime', 'error'}.

    R9 P5 B1: prefer the producer-written generated_at/updated_at over
    filesystem mtime. mtime is unreliable in CI (actions/checkout flattens
    it). Falls back to mtime if the JSON has no timestamp field — keeps
    backward compat for files predating the generated_at convention.

    R11 A4: distinguish OSError (file missing / unreadable) from
    JSONDecodeError (file present but corrupt). Pre-R11 both were caught
    by the same `except (OSError, json.JSONDecodeError): pass`, then the
    function fell back to `path.stat().st_mtime` — a corrupt JSON file
    silently masqueraded as fresh content via its mtime. The new return
    source `corrupt_fallback_mtime` makes the fallback visible so the
    caller can emit a structured `freshness_unreadable` warning
    (degradation visible in dashboard).
    """
    try:
        raw = path.read_text()
    except OSError:
        # File missing / unreadable — fall through to mtime probe and
        # let it return error if even stat() fails.
        pass
    else:
        try:
            d = json.loads(raw)
            for key in _FRESHNESS_TIMESTAMP_KEYS:
                ts = d.get(key) if isinstance(d, dict) else None
                if isinstance(ts, str) and ts:
                    try:
                        # Handle both 'Z' and explicit +HH:MM offsets.
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        return (dt.timestamp(), "content")
                    except ValueError:
                        continue
        except json.JSONDecodeError:
            # R11 A4: JSON corruption is a distinct failure mode — surface
            # as a different source label so the caller's warning text
            # can distinguish "stale" from "corrupt JSON masquerading as
            # fresh via mtime". The file's mtime is still useful as a
            # last-ditch freshness proxy (an actively-corrupting writer
            # would have a recent mtime).
            try:
                return (path.stat().st_mtime, "corrupt_fallback_mtime")
            except OSError:
                return (None, "error")
    try:
        return (path.stat().st_mtime, "mtime")
    except OSError:
        return (None, "error")


def _check_freshness(
    input_path: Path,
    reference_path: Path,
    max_age_hours: float,
    subsystem: str,
    warnings_acc: list,
) -> bool:
    """Return True if `input_path` is fresh, False if missing or stale.

    Stale = older than `reference_path` by MORE than `max_age_hours`.
    Missing = file does not exist. Either case appends a structured
    `subsystem_stale` warning (matches `_degrade._make_warning` shape:
    {subsystem, scope, record_id, exception_class, message, ts}) so the
    consolidated state surfaces the degradation alongside per-record /
    per-subsystem skips.

    Does NOT raise — the caller still reads the file with default={} so
    the subsystem degrades to neutral rather than crashing the tick.
    """
    now_iso = _now_iso()
    if not input_path.exists():
        warnings_acc.append({
            "subsystem": subsystem,
            "scope": "freshness",
            "record_id": f"file={input_path.name}",
            "exception_class": "Stale",
            "message": (
                f"{subsystem} input missing: {input_path.name} not present — "
                "producer never ran on this CI host or output is unpublished. "
                "Subsystem degrades to neutral zero adjustment this tick."
            ),
            "ts": now_iso,
        })
        return False
    if not reference_path.exists():
        # No reference clock — can't compute delta; treat as fresh so we
        # don't false-alarm on bootstrap tests / replays.
        return True
    # R9 P5 B1: use content-timestamp (generated_at) when available,
    # fall back to mtime. Pre-R9 the mtime-only path was a no-op in CI
    # (actions/checkout flattens mtimes), so a multi-day-stale producer
    # silently passed the freshness guard.
    input_ts, input_src = _freshness_timestamp_seconds(input_path)
    ref_ts, ref_src = _freshness_timestamp_seconds(reference_path)
    if input_ts is None or ref_ts is None:
        warnings_acc.append({
            "subsystem": subsystem,
            "scope": "freshness",
            "record_id": f"file={input_path.name}",
            "exception_class": "Stale",
            "message": (
                f"freshness timestamp unreadable on {input_path.name} "
                f"(input_src={input_src}, ref_src={ref_src})"
            ),
            "ts": now_iso,
        })
        return False
    # R11 A4: surface corrupt-JSON fallback as a distinct warning. Pre-R11
    # the OSError + json.JSONDecodeError were both caught silently and the
    # mtime fallback let a corrupt JSON pass freshness while downstream
    # _read_json(default={}) silently zeroed the subsystem.
    if input_src == "corrupt_fallback_mtime":
        warnings_acc.append({
            "subsystem": subsystem,
            "scope": "freshness",
            "record_id": f"file={input_path.name}",
            "exception_class": "CorruptJSON",
            "message": (
                f"{subsystem} input {input_path.name} is unparseable JSON "
                f"— using filesystem mtime as a fallback freshness proxy. "
                f"Downstream _read_json(default={{}}) will neutralize the "
                f"subsystem this tick. Investigate the producer."
            ),
            "ts": now_iso,
        })
    age_delta_seconds = ref_ts - input_ts
    # R10 Q4 (A1): future-dated content timestamp guard. R9 P5 B1's
    # content-preferring read trusted whatever the JSON's `generated_at`
    # said. If a producer's clock is skewed into the future (Docker host
    # with bad NTP, replay against a hard-coded future date, manual edit),
    # `age_delta_seconds` goes NEGATIVE and the `<= max_age_hours*3600`
    # check passes indefinitely — the subsystem could be stale forever
    # without ever firing the warning. Emit a distinct `future_timestamp`
    # signal when input is more than `max_age_hours` in the FUTURE
    # relative to the reference clock; small forward skew (within the
    # threshold band) tolerated to avoid false-positives from sub-second
    # clock drift between fetches.
    if -age_delta_seconds > max_age_hours * 3600.0:
        future_hours = -age_delta_seconds / 3600.0
        warnings_acc.append({
            "subsystem": subsystem,
            "scope": "freshness",
            "record_id": f"file={input_path.name}",
            "exception_class": "FutureTimestamp",
            "message": (
                f"{subsystem} input {input_path.name} has a content "
                f"timestamp {future_hours:.1f}h IN THE FUTURE relative to "
                f"{reference_path.name} — producer clock skew or replay "
                f"against a hard-coded future date. Treating as degraded "
                f"because the freshness check is meaningless on inverted "
                f"timestamps. (input_src={input_src}, ref_src={ref_src})"
            ),
            "ts": now_iso,
        })
        return False
    if age_delta_seconds <= max_age_hours * 3600.0:
        return True
    age_hours = age_delta_seconds / 3600.0
    warnings_acc.append({
        "subsystem": subsystem,
        "scope": "freshness",
        "record_id": f"file={input_path.name}",
        "exception_class": "Stale",
        "message": (
            f"{subsystem} input {input_path.name} is "
            f"{age_hours:.1f}h older than {reference_path.name} "
            f"(threshold {max_age_hours:.1f}h = 2 slow-cron ticks). "
            "Subsystem degrades to neutral zero adjustment this tick."
        ),
        "ts": now_iso,
    })
    return False


# ── Wave R2 P1c: fast-path freshness propagation ─────────────────────────
# Why this exists:
#   The fast path is run_live_update.py → 03_simulate.py → get_team_elo_
#   adjustment(). It reads matchday-adjusted Elo from cached subsystem
#   state but never surfaces freshness warnings — get_team_elo_adjustment
#   returns a float, no warning channel. If the slow workflow (every 3h)
#   stalls and the consolidated matchday_intelligence.json goes stale, the
#   fast tick silently applies stale matchday adjustments as if fresh.
#
#   This helper closes the gap: run_live_update calls it before
#   write_live_state and merges the returned warnings into the
#   live_state.json `warnings` array, so the dashboard's freshness pill
#   reflects matchday staleness too — not just results-feed staleness.
#
# Contract:
#   - Returns a list of {type, message} dicts ready to merge into
#     live_state.warnings — empty list when everything is fresh
#   - Never raises; an OSError/JSONDecodeError here would silently swallow
#     the freshness signal we're trying to surface, so we degrade to a
#     diagnostic warning of our own.
#   - The reference clock is results_2026.json mtime (matches the rest of
#     the freshness guard at _check_freshness above) — keeps the policy
#     consistent and avoids wall-clock false-alarms on replays.
def get_matchday_freshness_warnings() -> list[dict]:
    """Return live_state-shaped warnings describing matchday freshness.

    Surfaces three failure modes from the slow path to the fast path's
    live_state.json:

      1. `matchday_consolidated_missing` — dashboard/matchday_intelligence.json
         absent (slow workflow never ran on this host).
      2. `matchday_consolidated_stale` — consolidated file's mtime is more
         than STALENESS_MAX_AGE_HOURS older than results_2026.json's mtime
         (slow workflow stalled at least 2 ticks).
      3. `matchday_subsystem_stale` — consolidated file is fresh ITSELF
         but contains one or more `subsystem_stale` / freshness warnings
         from its producers (e.g. referee_2026.json missing → loud
         subsystem-stale warning embedded in the consolidated state).

    Type (3) is the most subtle: matchday_intelligence.json IS up-to-date
    (slow workflow ran on time) but ONE producer underneath it failed
    upstream. That signal previously stayed in the slow-path dashboard
    file and never reached live_state.json.
    """
    out: list[dict] = []
    # OUT_PATH is module-level (DASH / "matchday_intelligence.json").
    # results_2026.json (the reference clock) lives in LIVE — same dir
    # convention as the per-subsystem freshness reference at L115.
    results_path = LIVE / "results_2026.json"
    if not OUT_PATH.exists():
        out.append({
            "type": "matchday_consolidated_missing",
            "message": (
                f"Consolidated matchday state missing: "
                f"{OUT_PATH.name} not present. "
                "Slow workflow (matchday-intel-slow.yml) has not run on "
                "this host — fast-tick adjustments are zero."
            ),
        })
        return out

    # R9 P5 B1: content-timestamp (generated_at) preferred over mtime —
    # see comment on _freshness_timestamp_seconds above. Pre-R9 the
    # mtime path silently no-op'd in CI.
    out_ts, out_src = _freshness_timestamp_seconds(OUT_PATH)
    if out_ts is None:
        out.append({
            "type": "matchday_consolidated_unreadable",
            "message": (
                f"freshness timestamp unreadable on {OUT_PATH.name} "
                f"(source={out_src})"
            ),
        })
        return out

    if results_path.exists():
        ref_ts, _ = _freshness_timestamp_seconds(results_path)
        if ref_ts is None:
            ref_ts = out_ts  # no reference → don't false-alarm
        age_seconds = ref_ts - out_ts
        if age_seconds > STALENESS_MAX_AGE_HOURS * 3600.0:
            age_hours = age_seconds / 3600.0
            out.append({
                "type": "matchday_consolidated_stale",
                "message": (
                    f"{OUT_PATH.name} is {age_hours:.1f}h older than "
                    f"{results_path.name} (threshold "
                    f"{STALENESS_MAX_AGE_HOURS:.1f}h = 2 slow-cron ticks). "
                    "Slow workflow has stalled; fast-tick is applying "
                    "stale matchday adjustments."
                ),
            })

    # Type (3): consolidated file fresh, but one of its producers is stale
    # (subsystem_stale warning embedded by _check_freshness above).
    try:
        consolidated = json.loads(OUT_PATH.read_text())
    except (OSError, json.JSONDecodeError) as e:
        out.append({
            "type": "matchday_consolidated_unparseable",
            "message": (
                f"Could not parse {OUT_PATH.name}: "
                f"{type(e).__name__}: {e}"
            ),
        })
        return out

    deg = consolidated.get("degradation_warnings") or []
    stale_subs: list[str] = []
    # R5 C4: also track per-record degradations. The freshness/subsystem
    # filter below catches subsystem-wide collapse, but a sustained stream
    # of per-record failures (NaN xG on many players, malformed lineup
    # entries from a provider schema drift, etc.) stays embedded in
    # matchday_intelligence.json with no signal to the dashboard. The
    # subsystem still degrades to neutral per-record, but the operator
    # never knows the data quality dropped. Emit a single rollup warning
    # so the dashboard surfaces "N per-record degradations" without
    # spamming live_state.json with one entry per record.
    record_degradations: dict[str, int] = {}
    for w in deg:
        if not isinstance(w, dict):
            continue
        if w.get("scope") == "freshness" or w.get("exception_class") == "Stale":
            sub = w.get("subsystem")
            if sub and sub not in stale_subs:
                stale_subs.append(sub)
        elif w.get("scope") == "record":
            sub = w.get("subsystem") or "unknown"
            record_degradations[sub] = record_degradations.get(sub, 0) + 1
    if stale_subs:
        out.append({
            "type": "matchday_subsystem_stale",
            "message": (
                "Consolidated matchday state is current but the following "
                "subsystem inputs are stale and degraded to neutral: "
                + ", ".join(stale_subs)
                + ". Investigate the corresponding producer "
                "(referee_adjustments / suspension_tracker / "
                "fetch_player_stats) in matchday-intel-slow.yml."
            ),
            "subsystems": stale_subs,
        })
    if record_degradations:
        total = sum(record_degradations.values())
        breakdown = ", ".join(
            f"{sub}={n}" for sub, n in sorted(record_degradations.items())
        )
        out.append({
            "type": "matchday_record_degradation",
            "message": (
                f"Per-record degradations: {total} records skipped across "
                f"subsystems ({breakdown}). Records were skipped per the "
                f"_degrade.py allowlist; subsystem still produced neutral "
                f"output for them. See matchday_intelligence.json:"
                f"degradation_warnings for per-record details."
            ),
            "count": total,
            "by_subsystem": record_degradations,
        })

    return out


# ── Per-layer adjustment loaders ────────────────────────────────────────
# Each loader returns {(team, match_id_or_None): component_dict}. The
# match_id key is None for layers that apply tournament-wide (e.g. a
# team-level injury covering multiple matches).

def _load_injury_components(now_iso: str, warnings_acc: list | None = None) -> dict:
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
    warnings_acc = warnings_acc if warnings_acc is not None else []
    # R11 D4: freshness guard on the API injury feed. See
    # _load_weather_components for context. team_adjustments.json is the
    # operator-curated overlay (not provider-derived) so freshness checks
    # don't apply to it — operator edits expires_at directly.
    _check_freshness(
        LIVE / "injuries_2026.json", LIVE / "results_2026.json",
        STALENESS_MAX_AGE_HOURS, "injury", warnings_acc,
    )

    # Per-team API totals (tournament-wide, not match-scoped).
    api_path = LIVE / "injuries_2026.json"
    api_data = _read_json(api_path, default={}) or {}
    for team, blob in (api_data.get("teams") or {}).items():
        # Per-record degradation: a single bad team blob (NaN total,
        # missing field surfacing as KeyError) must not abort injury
        # loading for the other 47 WC2026 squads.
        def _build_api_record(team=team, blob=blob):
            raw = float(blob.get("total_elo_adjustment", 0.0) or 0.0)
            # API source uses the "normal" injury cap (manual overlay can push
            # toward "extreme" — see overlay block below).
            capped = max(-INJURY_CAP_NORMAL, min(INJURY_CAP_NORMAL, raw))
            if capped == 0.0:
                return None
            n_players = len(blob.get("players") or [])
            return {
                "type": "injury",
                "subtype": "api_aggregate",
                "raw_elo": raw,
                "capped_elo": capped,
                "cap_used": INJURY_CAP_NORMAL,
                "n_players": n_players,
                "source": "api_football",
            }
        record = degrade_record(
            "injury", f"team={team} src=api", _build_api_record, warnings_acc)
        if record is None:
            continue
        out.setdefault((team, None), []).append(record)

    # Manual overlay (operator-curated tier_1 / suspensions / notes).
    # Per-player tracking is required so the orchestrator can dedup against
    # suspensions (a player can't be both injured AND suspended for the same
    # fixture — suspension wins as it's the more-certain signal). See
    # build_adjustments_state for the dedup application.
    overlay_path = LIVE / "team_adjustments.json"
    overlay = _read_json(overlay_path, default={}) or {}
    overlay_by_team: dict[str, float] = {}
    overlay_reasons: dict[str, list[str]] = {}
    overlay_players_by_team: dict[str, list[dict]] = {}
    for adj_idx, adj in enumerate(overlay.get("adjustments", []) or []):
        def _accumulate_overlay(adj=adj, adj_idx=adj_idx):
            # Legacy semantics: approved defaults True, doubtful → 0.5x, expired skipped.
            if not adj.get("approved", True):
                return None
            exp = adj.get("expires_at")
            if exp:
                # Wave-4 fix: previously this block was `try: exp.replace(...);
                # if exp_iso < now_iso: return None; except Exception: pass`.
                # That swallowed every malformed-timestamp case (None, int,
                # dict, bad-format string) and silently kept the overlay
                # ACTIVE — an operator typo in expires_at could leave an
                # "expired" entry contributing Elo forever. Now we coerce to
                # str and surface a `bad_expires_at` warning so the
                # degradation log shows the bad row instead of hiding it.
                if not isinstance(exp, str):
                    warnings_acc.append({
                        "subsystem": "injury",
                        "scope": "overlay",
                        "record_id": f"overlay_idx={adj_idx} team={adj.get('team')}",
                        "exception_class": "TypeError",
                        "message": (
                            f"bad expires_at on overlay idx={adj_idx}: "
                            f"expected ISO-8601 str, got {type(exp).__name__} "
                            f"({exp!r}); treating as expired (entry skipped)"
                        ),
                        "ts": _now_iso(),
                    })
                    return None
                # Tolerate both "Z" and offset-suffixed timestamps. The
                # string comparison is correct ONLY for canonical ISO-8601
                # forms; validate by attempting to parse before relying on it.
                exp_iso = exp.replace("Z", "+00:00")
                try:
                    datetime.fromisoformat(exp_iso)
                except (TypeError, ValueError) as e:
                    warnings_acc.append({
                        "subsystem": "injury",
                        "scope": "overlay",
                        "record_id": f"overlay_idx={adj_idx} team={adj.get('team')}",
                        "exception_class": type(e).__name__,
                        "message": (
                            f"bad expires_at on overlay idx={adj_idx}: "
                            f"{exp!r} is not ISO-8601 ({e}); treating as "
                            "expired (entry skipped)"
                        ),
                        "ts": _now_iso(),
                    })
                    return None
                if exp_iso < now_iso:
                    return None
            amount = float(adj.get("adjustment_elo", 0) or 0)
            if adj.get("status") == "doubtful":
                amount *= 0.5
            team = adj.get("team")
            if not team:
                return None
            overlay_by_team[team] = overlay_by_team.get(team, 0.0) + amount
            overlay_reasons.setdefault(team, []).append(
                adj.get("reason") or adj.get("player") or adj.get("status") or "manual"
            )
            # Per-player breakdown for cross-subsystem dedup. Only entries
            # with an explicit `player` name participate in dedup; team-level
            # entries (no `player` set) flow through aggregate-only.
            player_name = adj.get("player")
            if player_name:
                overlay_players_by_team.setdefault(team, []).append({
                    "player": player_name,
                    "amount": amount,
                })
            return True
        degrade_record(
            "injury",
            f"overlay_idx={adj_idx} team={adj.get('team') if isinstance(adj, dict) else '?'}",
            _accumulate_overlay,
            warnings_acc,
        )
    for team, raw in overlay_by_team.items():
        def _build_overlay_record(team=team, raw=raw):
            # Overlay may exceed the "normal" cap if the operator flagged extreme
            # circumstances (multiple tier_1 players out) — use extreme cap here.
            capped = max(-INJURY_CAP_EXTREME, min(INJURY_CAP_EXTREME, raw))
            if capped == 0.0:
                return None
            return {
                "type": "injury",
                "subtype": "manual_overlay",
                "raw_elo": raw,
                "capped_elo": capped,
                "cap_used": INJURY_CAP_EXTREME,
                "reasons": overlay_reasons.get(team, []),
                "players": overlay_players_by_team.get(team, []),
                "source": "team_adjustments_manual",
            }
        record = degrade_record(
            "injury", f"team={team} src=overlay",
            _build_overlay_record, warnings_acc)
        if record is None:
            continue
        out.setdefault((team, None), []).append(record)

    return out


def _load_weather_components(warnings_acc: list | None = None) -> dict:
    """Read weather_2026.json. Schema written by B.2 fetch_weather.

    Each entry: {match_id, home_team_adjustment_elo, away_team_adjustment_elo,
                 lambda_adjustment, weather_bucket, ...}.
    """
    warnings_acc = warnings_acc if warnings_acc is not None else []
    # R11 D4: freshness guard. Pre-R11 only referee / suspension / player_stats
    # called _check_freshness — a multi-day-stale weather snapshot was
    # silently ingested with no `subsystem_stale` warning. Mirror the
    # existing pattern at line 683 / 736 / 1047.
    _check_freshness(
        LIVE / "weather_2026.json", LIVE / "results_2026.json",
        STALENESS_MAX_AGE_HOURS, "weather", warnings_acc,
    )
    data = _read_json(LIVE / "weather_2026.json", default={}) or {}
    entries = data.get("weather") or []
    out: dict[tuple[str, int | None], list[dict]] = {}
    for w in entries:
        m_id = w.get("match_id") if isinstance(w, dict) else None
        if m_id is None:
            continue
        bucket = w.get("weather_bucket")
        confidence = w.get("confidence")
        # Two sides — each may have an independent acclimatisation penalty.
        for side in ("home", "away"):
            team = w.get(f"{side}_team")
            if not team:
                continue
            def _build_side(w=w, side=side, team=team, m_id=m_id,
                            bucket=bucket, confidence=confidence):
                raw = float(w.get(f"{side}_team_adjustment_elo", 0.0) or 0.0)
                capped = max(-WEATHER_CAP, min(WEATHER_CAP, raw))
                if capped == 0.0:
                    return None
                return {
                    "type": "weather",
                    "weather_bucket": bucket,
                    "raw_elo": raw,
                    "capped_elo": capped,
                    "cap_used": WEATHER_CAP,
                    "confidence": confidence,
                    "source": "open_meteo",
                }
            record = degrade_record(
                "weather", f"m={m_id} side={side} team={team}",
                _build_side, warnings_acc)
            if record is None:
                continue
            out.setdefault((team, m_id), []).append(record)
    return out


def _load_referee_components(warnings_acc: list | None = None) -> dict:
    """Read referee_2026.json. Schema written by Phase 2 referee_adjustments.

    Each entry: {match_id, home_team, away_team, referee_name,
                 home_team_adjustment_elo, away_team_adjustment_elo, ...}.
    Wave-A model is one-sided: only the home side carries a bonus; away is
    always 0.0. Skipping the away side keeps the components list lean.
    """
    warnings_acc = warnings_acc if warnings_acc is not None else []
    # Wave-2 S1: freshness guard — emit `subsystem_stale` if the producer
    # never ran on this host or the snapshot is older than 2 slow-cron ticks.
    # Reads still proceed via _read_json(default={}) so the subsystem
    # degrades to neutral instead of crashing.
    _check_freshness(
        LIVE / "referee_2026.json", LIVE / "results_2026.json",
        STALENESS_MAX_AGE_HOURS, "referee", warnings_acc,
    )
    data = _read_json(LIVE / "referee_2026.json", default={}) or {}
    entries = data.get("referee") or []
    out: dict[tuple[str, int | None], list[dict]] = {}
    for r in entries:
        m_id = r.get("match_id") if isinstance(r, dict) else None
        if m_id is None:
            continue
        team = r.get("home_team")
        if not team:
            continue
        def _build_ref_record(r=r, m_id=m_id, team=team):
            raw = float(r.get("home_team_adjustment_elo", 0.0) or 0.0)
            capped = max(-REFEREE_CAP, min(REFEREE_CAP, raw))
            if capped == 0.0:
                return None
            return {
                "type": "referee",
                "raw_elo": raw,
                "capped_elo": capped,
                "cap_used": REFEREE_CAP,
                "referee_name": r.get("referee_name"),
                "n_matches": r.get("n_matches"),
                "confidence": r.get("confidence"),
                "reason": r.get("reason"),
                "source": "wave_a_proposal",
            }
        record = degrade_record(
            "referee", f"m={m_id} team={team}",
            _build_ref_record, warnings_acc)
        if record is None:
            continue
        out.setdefault((team, m_id), []).append(record)
    return out


def _load_suspension_components(warnings_acc: list | None = None) -> dict:
    """Read suspensions_2026.json. Schema written by Phase 4 suspension_tracker.

    Each entry: {match_id, team, player, reason, team_adjustment_elo, ...}.
    One-sided by construction — only the suspended player's team carries
    the penalty. Multiple suspensions for the same (team, match) stack and
    are re-clamped at the per-match cap (defense-in-depth alongside the
    tracker's per-row clamp).
    """
    warnings_acc = warnings_acc if warnings_acc is not None else []
    # Wave-2 S1: freshness guard — see _check_freshness docstring. Stale
    # suspensions are particularly dangerous: a player banned for tomorrow's
    # match silently drops to neutral if the tracker hasn't refreshed since
    # yesterday's red card.
    _check_freshness(
        LIVE / "suspensions_2026.json", LIVE / "results_2026.json",
        STALENESS_MAX_AGE_HOURS, "suspension", warnings_acc,
    )
    data = _read_json(LIVE / "suspensions_2026.json", default={}) or {}
    entries = data.get("suspensions") or []
    by_key: dict[tuple[str, int | None], list[dict]] = {}
    for s in entries:
        m_id = s.get("match_id") if isinstance(s, dict) else None
        team = s.get("team") if isinstance(s, dict) else None
        if m_id is None or not team:
            continue
        def _build_susp_record(s=s, m_id=m_id, team=team):
            raw = float(s.get("team_adjustment_elo", 0.0) or 0.0)
            if raw == 0.0:
                return None
            return {
                "type": "suspension",
                "raw_elo": raw,
                "per_player_capped_elo": raw,
                "cap_used": SUSPENSION_CAP,
                "player": s.get("player"),
                "reason": s.get("reason"),
                "evidence_match_ids": s.get("evidence_match_ids") or [],
                "confidence": s.get("confidence", "high"),
                "source": s.get("source", "fetch_results_events"),
            }
        record = degrade_record(
            "suspension",
            f"m={m_id} team={team} player={s.get('player') if isinstance(s, dict) else '?'}",
            _build_susp_record, warnings_acc)
        if record is None:
            continue
        by_key.setdefault((team, m_id), []).append(record)
    # Re-clamp the per-(team, match) sum at SUSPENSION_CAP so multiple
    # suspensions don't quietly bypass the cap before the aggregate layer.
    out: dict[tuple[str, int | None], list[dict]] = {}
    for key, comps in by_key.items():
        running = 0.0
        for c in comps:
            remaining = SUSPENSION_CAP - abs(running)
            sign = 1.0 if c["per_player_capped_elo"] >= 0 else -1.0
            allowed = sign * min(abs(c["per_player_capped_elo"]), max(0.0, remaining))
            c["capped_elo"] = allowed
            c["cap_reason"] = (
                "suspension_total"
                if abs(allowed) < abs(c["per_player_capped_elo"])
                else "per_player"
            )
            running += allowed
            if allowed == 0.0:
                continue
            out.setdefault(key, []).append(c)
    return out


def _load_lineup_components(warnings_acc: list | None = None) -> dict:
    """Read lineups_2026.json. Schema written by B.4 fetch_lineups.

    Each entry: {match_id, home_team_adjustment_elo, away_team_adjustment_elo,
                 reason, baseline_source, ...}. Display-only entries have
                 adjustment_elo == 0 — those still appear in the dashboard
                 JSON but contribute nothing to the Elo sum.
    """
    warnings_acc = warnings_acc if warnings_acc is not None else []
    # R11 D4: freshness guard. See _load_weather_components.
    _check_freshness(
        LIVE / "lineups_2026.json", LIVE / "results_2026.json",
        STALENESS_MAX_AGE_HOURS, "lineup", warnings_acc,
    )
    data = _read_json(LIVE / "lineups_2026.json", default={}) or {}
    entries = data.get("lineups") or []
    out: dict[tuple[str, int | None], list[dict]] = {}
    for ln in entries:
        m_id = ln.get("match_id") if isinstance(ln, dict) else None
        if m_id is None:
            continue
        for side in ("home", "away"):
            team = ln.get(side)
            if not team:
                continue
            def _build_lineup_side(ln=ln, side=side, team=team, m_id=m_id):
                raw = float(ln.get(f"{side}_team_adjustment_elo", 0.0) or 0.0)
                capped = max(-LINEUP_CAP, min(LINEUP_CAP, raw))
                if capped == 0.0:
                    return None
                return {
                    "type": "lineup",
                    "raw_elo": raw,
                    "capped_elo": capped,
                    "cap_used": LINEUP_CAP,
                    "reason": ln.get(f"{side}_adjustment_reason"),
                    "baseline_source": ln.get("baseline_source", "unknown"),
                    "source": "api_football",
                }
            record = degrade_record(
                "lineup", f"m={m_id} side={side} team={team}",
                _build_lineup_side, warnings_acc)
            if record is None:
                continue
            out.setdefault((team, m_id), []).append(record)
    return out


def _load_stats_components(warnings_acc: list | None = None) -> dict:
    """Read match_stats_2026.json. Schema written by B.5 fetch_match_stats.

    Post-match only. Each entry adjusts the TEAM's live form (not a specific
    upcoming match), so match_id key is None. Group-stage total cap applied
    by aggregating per-team across all completed matches.

    H7: down-weight the proxy by 0.5× when a team already has a non-zero
    live_team_state delta. Both layers encode post-match form (the stats
    proxy via shots/possession/corners, live_team_state via the Elo K-factor
    on the result itself). Without this they stack with no joint cap until
    GRAND_TOTAL_CAP clamps the sum — and the goal model ALSO carries its
    own per-team form features. Halving stats_proxy when live_team_state is
    active keeps the two signals from doubling up on the same information.
    """
    warnings_acc = warnings_acc if warnings_acc is not None else []
    # R11 D4: freshness guard on the post-match stats feed. See
    # _load_weather_components.
    _check_freshness(
        LIVE / "match_stats_2026.json", LIVE / "results_2026.json",
        STALENESS_MAX_AGE_HOURS, "stats_proxy", warnings_acc,
    )
    data = _read_json(LIVE / "match_stats_2026.json", default={}) or {}
    entries = data.get("matches") or []
    # Read live team state to detect "form already accounted for" teams.
    lts_path = LIVE / "live_team_state.json"
    lts_data = _read_json(lts_path, default={}) or {}
    # Schema written by update_team_state.py is `{"deltas": {team: float}}`.
    # `team_state` was an older draft key; check both for forward-compat.
    if isinstance(lts_data, dict):
        lts_by_team = lts_data.get("deltas") or lts_data.get("team_state") or {}
    else:
        lts_by_team = {}
    def _live_state_active(team: str) -> bool:
        v = lts_by_team.get(team, 0.0)
        try:
            return abs(float(v)) > 0.01
        except (TypeError, ValueError):
            return False
    # Aggregate per team across all matches, applying both per-match and
    # group-stage caps.
    per_team_total: dict[str, list[dict]] = {}
    for s in entries:
        if not isinstance(s, dict) or s.get("status") != "FT":
            continue
        for side in ("home", "away"):
            team = s.get(side)
            if not team:
                continue
            def _build_stats_side(s=s, side=side, team=team):
                raw = float(s.get(f"{side}_form_adjustment_elo", 0.0) or 0.0)
                # H7: 0.5× discount when live_team_state already reflects form.
                downweighted = False
                if _live_state_active(team):
                    raw = raw * 0.5
                    downweighted = True
                capped = max(-STATS_CAP_PER_MATCH, min(STATS_CAP_PER_MATCH, raw))
                if capped == 0.0:
                    return None
                return {
                    "type": "stats_proxy",
                    "match_id": s.get("match_id"),
                    "raw_elo": raw,
                    "capped_elo_per_match": capped,
                    "cap_per_match": STATS_CAP_PER_MATCH,
                    "true_xg_available": s.get("true_xg_available", False),
                    "xg_attempted": s.get("xg_attempted", False),
                    "xg_found": s.get("xg_found", False),
                    "downweighted_for_live_team_state": downweighted,
                    "source": "api_football",
                }
            record = degrade_record(
                "stats_proxy",
                f"m={s.get('match_id')} side={side} team={team}",
                _build_stats_side, warnings_acc)
            if record is None:
                continue
            per_team_total.setdefault(team, []).append(record)
    # Apply group-stage total cap per team.
    out: dict[tuple[str, int | None], list[dict]] = {}
    for team, components in per_team_total.items():
        running_sum = 0.0
        for c in components:
            # If adding this component would exceed the group total cap,
            # truncate this component's contribution.
            remaining_budget = STATS_CAP_TOURNAMENT_TOTAL - abs(running_sum)
            sign = 1.0 if c["capped_elo_per_match"] >= 0 else -1.0
            allowed = sign * min(abs(c["capped_elo_per_match"]), max(0.0, remaining_budget))
            c["capped_elo"] = allowed
            c["cap_used"] = STATS_CAP_TOURNAMENT_TOTAL
            c["cap_reason"] = (
                "group_total" if abs(allowed) < abs(c["capped_elo_per_match"]) else "per_match"
            )
            running_sum += allowed
            if allowed == 0.0:
                continue
            out.setdefault((team, None), []).append(c)
    return out


# ── Cross-subsystem dedup helper ────────────────────────────────────────
def _apply_injury_suspension_dedup(
    inj: dict[tuple[str, int | None], list[dict]],
    sus: dict[tuple[str, int | None], list[dict]],
    warnings_acc: list,
) -> None:
    """Mutates `inj` in place to add per-match positive credits that
    cancel the injury overlay contribution of any player who is ALSO
    suspended for that match.

    Suspension wins (hard rule — player can't play; injury is a
    probability the player can't play). Dedup is per-
    (team, match_id, player_name): a player suspended for m=73 and
    injured tournament-wide keeps the injury penalty for m=74 etc. —
    only m=73 is deduplicated.

    Mechanism: the injury overlay is bucketed at (team, None) as a single
    aggregated record with a `players` list of {player, amount} entries.
    For each suspended player whose name appears in their team's overlay
    `players` list, we emit a credit record at (team, match_id) with
    capped_elo = -player_overlay_amount (positive when amount is negative)
    so the sum cancels for that one match without disturbing other matches.

    Every dedup decision is appended to `warnings_acc` as a structured
    `injury_suspension_dedup` warning so the dashboard / audit log can
    surface why a team's match total differs from naive injury+suspension.
    """
    # Build per-team player→overlay-amount index from the injury overlay
    # records (tournament-wide bucket only — API aggregates have no
    # per-player breakdown to dedup against).
    overlay_player_amount: dict[tuple[str, str], float] = {}
    for (team, m_id), comps in inj.items():
        if m_id is not None:
            continue
        for c in comps:
            if c.get("subtype") != "manual_overlay":
                continue
            for p in c.get("players") or []:
                name = p.get("player")
                amt = p.get("amount")
                if not name or amt is None:
                    continue
                # Sum across multiple overlay entries for the same player
                # (e.g. two operator notes for one tier-1 star).
                overlay_player_amount[(team, name)] = (
                    overlay_player_amount.get((team, name), 0.0) + float(amt)
                )

    if not overlay_player_amount:
        return

    # For each suspension record, check if the player has an overlay
    # contribution to credit back at THAT match_id.
    for (team, m_id), comps in list(sus.items()):
        if m_id is None:
            continue
        for c in comps:
            player_name = c.get("player")
            if not player_name:
                continue
            key = (team, player_name)
            overlay_amt = overlay_player_amount.get(key)
            if overlay_amt is None or overlay_amt == 0.0:
                continue
            # Credit = -overlay_amt (cancels the tournament-wide injury
            # contribution for THIS match only). If overlay_amt is -25
            # (player injured, -25 Elo), credit is +25 here so the sum at
            # m=m_id collapses to just the suspension.
            credit = -float(overlay_amt)
            inj.setdefault((team, m_id), []).append({
                "type": "injury",
                "subtype": "suspension_dedup_credit",
                "raw_elo": credit,
                "capped_elo": credit,
                "cap_used": INJURY_CAP_EXTREME,
                "player": player_name,
                "reasons": [
                    f"dedup: {player_name} also suspended for m={m_id}; "
                    f"injury overlay deferred to suspension (suspension wins)"
                ],
                "source": "cross_subsystem_dedup",
            })
            warnings_acc.append({
                "subsystem": "injury",
                "scope": "dedup",
                "record_id": f"team={team} m={m_id} player={player_name}",
                "type": "injury_suspension_dedup",
                "message": (
                    f"{player_name} appears in both injury overlay and "
                    f"suspension for {team} m={m_id}; injury contribution "
                    f"({overlay_amt:+.1f}) credited back — suspension wins"
                ),
                "ts": _now_iso(),
            })


# ── Core aggregation ────────────────────────────────────────────────────
def build_adjustments_state(now_iso: str | None = None) -> dict:
    """Read all layers, apply caps, return the consolidated state dict.

    The returned dict is what gets written to matchday_intelligence.json
    AND appended as one record to the audit log. Pure function aside from
    file reads — no writes here, the caller decides.
    """
    now_iso = now_iso or _now_iso()
    # Round 5: each subsystem call is wrapped both at the per-record level
    # (inside _load_*_components — see degrade_record) AND at the
    # per-subsystem level here. Per-record catches bad data inside an
    # otherwise-healthy feed; per-subsystem catches unexpected failures
    # in the loader itself (e.g. a corrupted top-level key) so other
    # subsystems still produce output.
    degradation_warnings: list[dict] = []
    # Wave-2 S1: player_stats freshness — this file is consumed by
    # auto_tier (today in shadow mode, AUTO_TIER_ACTIVE=False) via
    # injury_adjustments. The orchestrator doesn't load it directly, but
    # we still surface staleness here so the dashboard sees the producer
    # gap before the auto-tier flip lands. Referee + suspension freshness
    # checks live inside their respective loaders.
    _check_freshness(
        LIVE / "player_stats_2026.json", LIVE / "results_2026.json",
        STALENESS_MAX_AGE_HOURS, "player_stats", degradation_warnings,
    )
    inj = degrade_subsystem(
        "injury", lambda: _load_injury_components(now_iso, degradation_warnings),
        degradation_warnings)
    wx = degrade_subsystem(
        "weather", lambda: _load_weather_components(degradation_warnings),
        degradation_warnings)
    ln = degrade_subsystem(
        "lineup", lambda: _load_lineup_components(degradation_warnings),
        degradation_warnings)
    stats = degrade_subsystem(
        "stats_proxy", lambda: _load_stats_components(degradation_warnings),
        degradation_warnings)
    ref = degrade_subsystem(
        "referee", lambda: _load_referee_components(degradation_warnings),
        degradation_warnings)
    sus = degrade_subsystem(
        "suspension", lambda: _load_suspension_components(degradation_warnings),
        degradation_warnings)

    # ── Cross-subsystem dedup: injury + suspension same player ──────────
    # Rule (see ARCHITECTURE / Round 6 §dedup): a suspended player is
    # already unavailable for the affected match — the team's roster
    # contribution is the unavailability, not the cause. Stacking injury
    # AND suspension for the same player is double-counting. Convention:
    # SUSPENSION WINS (hard rule — player cannot play; injury is a
    # probabilistic signal).
    #
    # Dedup-key set schema: {(team: str, match_id: int, player_name: str)}.
    # The injury overlay is tournament-wide (bucketed at (team, None)), so
    # for each suspended player we emit a per-match POSITIVE credit at
    # (team, match_id) that cancels the suspended player's overlay
    # contribution for THAT match only. Tournament-wide injury still
    # applies for every OTHER match (per-(team,match) dedup, not
    # per-(team,player)).
    _apply_injury_suspension_dedup(inj, sus, degradation_warnings)

    # Merge component lists by (team, match_id).
    components_by_key: dict[tuple[str, int | None], list[dict]] = {}
    for src in (inj, wx, ln, stats, ref, sus):
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
            "referee": REFEREE_CAP,
            "suspension": SUSPENSION_CAP,
            "stats_per_match": STATS_CAP_PER_MATCH,
            "stats_group_total": STATS_CAP_TOURNAMENT_TOTAL,
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
            "referee": (LIVE / "referee_2026.json").exists(),
            "suspensions": (LIVE / "suspensions_2026.json").exists(),
            "lineups": (LIVE / "lineups_2026.json").exists(),
            "stats_proxy": (LIVE / "match_stats_2026.json").exists(),
        },
        "warnings": [],
        # Round 5: every per-record skip + per-subsystem fallover is
        # surfaced here so the dashboard can render an operator alert
        # and downstream consumers can see WHY a tick produced fewer
        # adjustments than expected. Empty list = clean tick.
        "degradation_warnings": degradation_warnings,
    }

    # Surface a top-level `subsystem_degraded` warning for any subsystem
    # whose entire loader raised — operators need a single pill not
    # buried record-level entries.
    _degraded_subsystems = sorted({
        w["subsystem"] for w in degradation_warnings
        if w.get("scope") == "subsystem"
    })
    for subsys in _degraded_subsystems:
        state["warnings"].append({
            "type": "subsystem_degraded",
            "subsystem": subsys,
            "message": (
                f"{subsys} subsystem returned no output this tick — see "
                f"degradation_warnings for the underlying exception"
            ),
        })

    # Catastrophic fail: ALL six subsystems degraded => no layer
    # contributed any signal. The orchestrator still writes the snapshot
    # (so the dashboard sees the warning) but the CLI exits non-zero.
    _ALL_SUBSYSTEMS = {"injury", "weather", "lineup", "stats_proxy", "referee", "suspension"}
    if set(_degraded_subsystems) >= _ALL_SUBSYSTEMS:
        state["warnings"].append({
            "type": "pipeline_unhealthy",
            "message": (
                "all matchday subsystems degraded this tick — no live "
                "adjustments applied; investigate upstream feeds"
            ),
        })
        state["pipeline_unhealthy"] = True
    else:
        state["pipeline_unhealthy"] = False

    # Surface a friendly warning when a feed THIS MODULE consumes is absent.
    # As of B.3, injuries are owned here (API-Football primary + manual overlay).
    _FEEDS_THIS_MODULE_CONSUMES = {"injuries", "weather", "referee", "suspensions", "lineups", "stats_proxy"}
    for feed, present in state["feeds_available"].items():
        if feed not in _FEEDS_THIS_MODULE_CONSUMES:
            continue
        if not present:
            state["warnings"].append({
                "type": "feed_missing",
                "feed": feed,
                "message": f"{feed} feed not present — adjustments from this layer are skipped",
            })

    # Lift upstream feed warnings into the consolidated state. Today this
    # surfaces `ambiguous_classification` items from fetch_injuries (an
    # operator can disambiguate via team_adjustments.json) plus any
    # fetch-error warnings from past API failures. Tagging with `feed:`
    # lets the dashboard scope the alert. We deliberately don't lift
    # benign info-only items like `filter_non_wc` (those are expected
    # every cycle and would be noise).
    _UPSTREAM_FEEDS_WITH_WARNINGS = [
        ("injuries", LIVE / "injuries_2026.json"),
        ("weather",  LIVE / "weather_2026.json"),
        ("referee",  LIVE / "referee_2026.json"),
        ("suspensions", LIVE / "suspensions_2026.json"),
        ("lineups",  LIVE / "lineups_2026.json"),
        ("stats_proxy", LIVE / "match_stats_2026.json"),
    ]
    _PROPAGATE_WARNING_TYPES = {
        "ambiguous_classification", "fetch_error", "http_error",
        "api_error", "missing_key",
        # `no_records_returned` is INFO-level: emitted when an upstream API
        # call succeeded but returned an empty payload (could be a quiet day,
        # could be a misconfigured endpoint). The dashboard's
        # INTEL_TOP_BAR_TYPES allowlist deliberately omits it — surface it
        # only in the matchday-intel detail block, not the alert pill, so a
        # genuinely quiet feed doesn't trigger a false alarm.
        "no_records_returned",
        # Phase 4 §4 fallback: suspension_tracker surfaces this when the
        # snapshot has completed matches but no enriched `events` lists.
        # Propagating prevents the silent "zero suspensions = confirmed"
        # failure mode the §4 spec exists to prevent.
        "no_events_in_snapshot",
    }
    # Types we KNOW are benign info-only and deliberately don't propagate
    # to the consolidated state. Listed explicitly so the
    # dropped-unknown-type observability below doesn't log them as
    # "unknown" every cron tick. To add a new benign filter: add the
    # type string here. To make a new type alert-grade: add it to
    # _PROPAGATE_WARNING_TYPES above AND consider adding to
    # dashboard/app.js:INTEL_TOP_BAR_TYPES for top-pill surface.
    _BENIGN_DROPPED_WARNING_TYPES = {
        "filter_non_wc",       # qualifier carry-over records — expected
        "skipped_bad_record",  # malformed API record — expected occasionally
        "feed_missing",        # generated locally by this module already;
                               # re-lifting would double-count
        "unmapped_fixture",    # local-replay artifact, not production
        "unmapped_match",      # schedule drift; surfaces in fetch_lineups log
    }
    # Track warning types we saw but DIDN'T propagate — surfaces the
    # "added a new warning type upstream but forgot to extend the
    # allowlist" maintenance gap. Without this, a future fetch_lineups
    # patch that emits `provider_quota_exhausted` (say) would silently
    # disappear here with no trace in logs. Sample once per (feed, type)
    # pair so a thousand identical warnings don't bloat stderr.
    dropped_types_seen: set[tuple[str, str]] = set()
    for feed, path in _UPSTREAM_FEEDS_WITH_WARNINGS:
        upstream = _read_json(path, default={}) or {}
        raw_warnings = upstream.get("warnings")
        if not isinstance(raw_warnings, list):
            # Malformed upstream payload (warnings field corrupted or
            # truncated mid-write). Skip silently rather than crash —
            # the matchday cron must continue producing a snapshot.
            continue
        for w in raw_warnings:
            # Guard against non-dict elements in the warnings list — a
            # truncated or hand-edited JSON could produce strings/null
            # entries. Without this, .get() raises AttributeError and
            # aborts the entire build_adjustments_state call.
            if not isinstance(w, dict):
                continue
            w_type = w.get("type")
            if w_type in _PROPAGATE_WARNING_TYPES:
                # Shallow copy so the dashboard label doesn't mutate the
                # original on-disk record between runs.
                lifted = dict(w)
                lifted["feed"] = feed
                state["warnings"].append(lifted)
            elif w_type and w_type not in _BENIGN_DROPPED_WARNING_TYPES:
                # New / unknown warning type seen — log once per
                # (feed, type) pair so a future maintainer notices and
                # decides whether to add it to the allowlist or the
                # benign-dropped set.
                key = (feed, w_type)
                if key not in dropped_types_seen:
                    dropped_types_seen.add(key)
                    print(
                        f"[apply_matchday] WARN: dropping unknown warning "
                        f"type={w_type!r} from feed={feed!r} — extend "
                        f"_PROPAGATE_WARNING_TYPES or _BENIGN_DROPPED_"
                        f"WARNING_TYPES in apply_matchday_adjustments.py",
                        file=sys.stderr,
                    )

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
        # Round 5: pin per-tick degradation count + types in the replay
        # log so an operator scanning matchday_intelligence_log.jsonl can
        # spot a creeping bad-feed pattern without diff'ing snapshots.
        "degradation_warnings": state.get("degradation_warnings", []),
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
    # R9 P3: allow_nan=False on the audit log writer. Pre-R9 R8 O2 hardened
    # the canonical matchday_intelligence.json writer at :101 but the
    # adjustments_log.jsonl audit trail used at :1246 still emitted NaN
    # silently — operators inspecting the log to triage degradation would
    # see literal "NaN" tokens with no fail-loud signal.
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")


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

    Round 5: a failure to write the dashboard JSON (disk full, perms)
    is itself a catastrophic event — flagged via `pipeline_unhealthy`
    so the CLI can exit non-zero. The audit log append is best-effort.
    """
    state = build_adjustments_state()
    if dry_run:
        # `relative_to` raises ValueError if OUT_PATH is outside ROOT
        # (e.g. when tests monkeypatch OUT_PATH into a tempfs); fall back
        # to the absolute string in that case so dry-run never crashes.
        try:
            out_rel = OUT_PATH.relative_to(ROOT)
            log_rel = LOG_PATH.relative_to(ROOT)
        except ValueError:
            out_rel = OUT_PATH
            log_rel = LOG_PATH
        print(
            f"[matchday] dry-run — would write to {out_rel} and append to "
            f"{log_rel}"
        )
        return state
    try:
        _atomic_write_json(OUT_PATH, state)
    except (OSError, ValueError, TypeError) as e:
        # The dashboard JSON couldn't land — escalate to catastrophic so
        # the CLI exits non-zero, but DON'T re-raise so the caller still
        # gets a state dict back (callers like run_live_update.py shouldn't
        # crash mid-tick over a transient FS error).
        state.setdefault("warnings", []).append({
            "type": "pipeline_unhealthy",
            "message": f"failed to write dashboard JSON: "
                       f"{type(e).__name__}: {e}",
        })
        state["pipeline_unhealthy"] = True
        print(
            f"[matchday] ERROR: failed to write {OUT_PATH}: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
    try:
        append_audit_log(state)
    except OSError as e:
        # Audit log failure is loud but non-fatal — the snapshot still
        # made it to disk above, so the dashboard recovers next tick.
        print(
            f"[matchday] WARN: failed to append audit log: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
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
            print(f"  - {w.get('type', 'unknown')}: {w.get('message', '')}")
    deg = state.get("degradation_warnings") or []
    if deg:
        print(f"[matchday] degradation warnings: {len(deg)}")
        for w in deg[:3]:
            print(
                f"  - {w.get('subsystem')}/{w.get('scope')} "
                f"{w.get('record_id')}: {w.get('exception_class')}"
            )
    if not args.dry_run:
        try:
            out_rel = OUT_PATH.relative_to(ROOT)
            log_rel = LOG_PATH.relative_to(ROOT)
        except ValueError:
            out_rel = OUT_PATH
            log_rel = LOG_PATH
        print(f"[matchday] wrote {out_rel}")
        print(f"[matchday] appended audit log {log_rel}")
    # Round 5 exit-code policy:
    #   per-record skip       → exit 0, warnings non-empty
    #   per-subsystem degraded → exit 0 if at least one other subsystem
    #                            produced output (the per-subsystem
    #                            `subsystem_degraded` warning is loud)
    #   catastrophic (all subsystems failed OR snapshot write failed)
    #                          → exit 1, pipeline_unhealthy=True
    if state.get("pipeline_unhealthy"):
        print(
            "[matchday] ERROR: pipeline_unhealthy — exiting non-zero",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
