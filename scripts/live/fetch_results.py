"""
fetch_results.py — Pluggable live-score fetcher.

Sources (selected via env var FOOTBALL_PROVIDER, with WC_RESULTS_SOURCE as a
backward-compatible alias):
  - mock (default)  — reads data/live/results_2026.json as-is (manual mode)
  - api_football    — live adapter for API-Football (requires API_FOOTBALL_KEY,
                      or WC_APIFOOTBALL_KEY for backward compatibility).
                      NOTE: API-Football FREE tier blocks 2026 — must be Pro+
  - football_data   — live adapter for football-data.org (requires
                      FOOTBALL_DATA_TOKEN). FREE TIER covers FIFA World Cup.
  - sportmonks      — placeholder adapter for Sportmonks (requires SPORTMONKS_TOKEN
                      or WC_SPORTMONKS_TOKEN)

Each adapter returns a list of normalised records:
  {
    "m": internal_match_id,
    "provider_fixture_id": str | None,
    "home": str, "away": str, "date": "YYYY-MM-DD",
    "home_score": int | None, "away_score": int | None,
    "status": "FT|AET|PEN|...",
    "status_long": "Match Finished|...",
    "source": "api_football|sportmonks|mock",
    "updated_at": ISO-8601,
  }

LOCK only when status ∈ {FT, AET, PEN}. POSTPONED/ABANDONED/CANCELED/SUSPENDED
are tracked as warnings — they never overwrite a locked result.

Hardening:
  - All HTTP calls wrapped in try/except + retries on transient 5xx.
  - Match-level validation wrapped per-record.
  - results_2026.json is written atomically.
  - If we'd be replacing N locked results with 0 (provider returned nothing),
    we refuse and preserve the existing file.

CLI:
  python scripts/live/fetch_results.py
  python scripts/live/fetch_results.py --provider api_football --dry-run
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIVE = ROOT / "data" / "live"
RAW = ROOT / "data" / "raw"

# Schema-drift watchdog (Round 5/6): compares fresh provider responses to
# captured baselines under data/live/_provider_schemas/. Soft-mode by default —
# drift logs a WARNING, does NOT crash the tick.
# R15: ROOT on sys.path so absolute `scripts.live.*` imports resolve under
# script-mode test invocations (CI runs `python tests/live/test_*.py`
# directly — script-mode does NOT add CWD to sys.path).
sys.path.insert(0, str(ROOT))
from scripts.live._schema_watchdog import assert_shape  # noqa: E402
from scripts.live._knockout import load_knockout_fixtures  # noqa: E402  # R9 P4
from scripts.live._knockout import classify_round  # noqa: E402  # A.6
_SCHEMA_BASELINE_DIR = ROOT / "data" / "live" / "_provider_schemas"

LOCKED_STATUSES = {"FT", "AET", "PEN"}
WARN_STATUSES = {"POSTPONED", "ABANDONED", "CANCELED", "CANCELLED",
                 "SUSPENDED", "INTERRUPTED", "WALKOVER", "WALKOVERAWARD"}

# API-Football short codes → our internal canonical status
APIFOOTBALL_STATUS_MAP = {
    "FT":   "FT",          # Match Finished (regulation time)
    "AET":  "AET",         # After Extra Time
    "PEN":  "PEN",         # Penalty Shootout
    "PST":  "POSTPONED",
    "CANC": "CANCELED",
    "ABD":  "ABANDONED",
    "SUSP": "SUSPENDED",
    "INT":  "INTERRUPTED",
    "AWD":  "WALKOVERAWARD",
    "WO":   "WALKOVER",
    # In-progress / not-started — never lock these
    "TBD":  "SCHEDULED", "NS": "SCHEDULED",
    "1H":   "LIVE", "HT": "LIVE", "2H": "LIVE",
    "ET":   "LIVE", "BT": "LIVE", "P":  "LIVE", "LIVE": "LIVE",
}

# football-data.org status strings → our internal canonical status
# https://docs.football-data.org/general/v4/lookup_tables.html
FOOTBALLDATA_STATUS_MAP = {
    "SCHEDULED":   "SCHEDULED",
    "TIMED":       "SCHEDULED",
    "IN_PLAY":     "LIVE",
    "PAUSED":      "LIVE",
    "EXTRA_TIME":  "LIVE",
    "PENALTY_SHOOTOUT": "LIVE",
    "FINISHED":    "FT",         # generic full-time; AET/PEN inferred from score blocks
    "AWARDED":     "WALKOVERAWARD",
    "POSTPONED":   "POSTPONED",
    "SUSPENDED":   "SUSPENDED",
    "CANCELLED":   "CANCELED",
    "CANCELED":    "CANCELED",
}

# Team-name normalisation: provider name → our canonical name.
# R12 MED: extend with football-data.org and operator-overlay variants.
TEAM_ALIAS = {
    "USA":                "United States",
    "U.S.A.":             "United States",
    "United States of America": "United States",
    "Korea Republic":     "South Korea",
    "Republic of Korea":  "South Korea",
    "Korea, Republic of": "South Korea",  # R12: football-data.org tournament format
    "South Korea (Korea Republic)": "South Korea",
    "Türkiye":            "Turkey",
    "Turkiye":            "Turkey",
    "Türkiye (Turkey)":   "Turkey",       # R12
    "Czech Republic":     "Czechia",
    "Cabo Verde":         "Cape Verde",
    "Cape Verde Islands": "Cape Verde",
    "Côte d'Ivoire":      "Ivory Coast",
    "Cote d'Ivoire":      "Ivory Coast",
    "Ivory Coast (Côte d'Ivoire)": "Ivory Coast",
    "IR Iran":            "Iran",
    "Iran Islamic Republic": "Iran",
    "Congo DR":           "DR Congo",
    "DR Congo":           "DR Congo",
    "Congo Democratic Republic": "DR Congo",
    "Bosnia-Herzegovina": "Bosnia and Herzegovina",
    "Bosnia & Herzegovina": "Bosnia and Herzegovina",
    "Curaçao":            "Curacao",
    "Saudi Arabia":       "Saudi Arabia",
    "New Zealand":        "New Zealand",
}


def normalize_team(name: str) -> str:
    """Map provider team name → our canonical name (used in wc2026_config)."""
    if not name:
        return name
    return TEAM_ALIAS.get(name.strip(), name.strip())


def get_provider_name() -> str:
    """Resolve provider from CLI/env. Order: FOOTBALL_PROVIDER, WC_RESULTS_SOURCE, 'mock'."""
    return (os.environ.get("FOOTBALL_PROVIDER")
            or os.environ.get("WC_RESULTS_SOURCE")
            or "mock").strip().lower().replace("-", "_")


def get_api_football_key() -> str | None:
    return (os.environ.get("API_FOOTBALL_KEY")
            or os.environ.get("WC_APIFOOTBALL_KEY"))


def get_sportmonks_token() -> str | None:
    return (os.environ.get("SPORTMONKS_TOKEN")
            or os.environ.get("WC_SPORTMONKS_TOKEN"))


def get_football_data_token() -> str | None:
    return (os.environ.get("FOOTBALL_DATA_TOKEN")
            or os.environ.get("WC_FOOTBALL_DATA_TOKEN"))


# ─── ATOMIC IO ─────────────────────────────────────────────────────────────
def atomic_write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent),
        prefix=path.name + ".", suffix=".tmp", delete=False,
    ) as tmp:
        # R9 P3: allow_nan=False — reject NaN/Infinity at producer boundary
        # so downstream consumers (apply_matchday_adjustments aggregator,
        # 03_simulate live re-run) never read poisoned floats. Pre-R9 R8 O2
        # only hardened the matchday writer; results_2026.json is read into
        # locked_score / events tallies. A NaN home_score would silently
        # propagate into the sim and emerge as NaN p_champion at the very end.
        json.dump(payload, tmp, indent=2, allow_nan=False)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


# R12 B2: route through the shared _http_client.http_get_json so the
# R11 C1 Retry-After honoring and 429-retry behavior actually applies to
# fetch_results' three call sites (events fetch + both adapter list calls).
# Pre-R12 fetch_results.py shipped its own local http_get_json that:
#   (a) ignored Retry-After (R32 burst risk on /fixtures/events)
#   (b) raised on 429 without retry
#   (c) sleeps on EVERY attempt including the final (1s wasted per fail)
# All three issues are fixed by the shared client. The local fn was a
# pre-R11-C1 artifact; the comment at fetch_apifootball_events_for_fixture
# already claimed R11 C1 benefit but L386 actually called the LOCAL fn.
from scripts.live._http_client import http_get_json  # noqa: E402


# ─── KNOCKOUT DECODER (A.2) ─────────────────────────────────────────────────
# Shared helper used by both API-Football and football-data.org adapters to
# extract penalty-shootout sub-scores and the winning team. Empirically grounded
# in the A.0 probe (tests/live/provider_samples/apifootball_*.json):
#
#   For PEN matches, API-Football returns:
#     score.penalty.{home, away}  → int sub-scores ("3", "0")
#     teams.{home, away}.winner   → true / false (one side always true)
#     goals.{home, away}          → regulation+ET total (e.g. 0-0)
#
#   For AET matches:
#     score.penalty.{home, away}  → null, null
#     score.extratime.{home,away} → ET-only goals (e.g. 1-0)
#     goals.{home, away}          → regulation+ET total (e.g. 2-1)
#     teams.{home, away}.winner   → true / false
#
#   For FT matches, score.penalty is {null, null} and winner reflects the
#   group-stage outcome (or null on draws — irrelevant for group stage).
#
# football-data.org uses parallel field names (score.penalties.{home, away},
# score.winner = "HOME_TEAM"|"AWAY_TEAM"|"DRAW"). The helper accepts either
# shape via the `winner_source` argument.
def extract_pens_and_winner(
    score_block: dict,
    teams_block: dict,
    canon_status: str,
    winner_source: str = "api_football",
) -> tuple[int | None, int | None, str | None]:
    """Returns (home_pens, away_pens, winner).

    `winner` is one of "home" | "away" | None. None is correct for:
      - group-stage draws
      - any FT match (no need to single out a winner)
      - PEN matches where the provider hasn't populated the winner field yet
        (caller should WARN + skip — never fabricate)

    `home_pens` / `away_pens` are int sub-scores for PEN matches, None
    otherwise. Stored alongside the regulation+ET goals so downstream
    consumers can render "Spain 1-1 France (4-3 pens)" without ambiguity.
    """
    # Pen sub-scores — only present for PEN status, always None otherwise.
    pen_block = (score_block or {}).get("penalty") or (score_block or {}).get("penalties") or {}
    home_pens = pen_block.get("home")
    away_pens = pen_block.get("away")
    # Coerce to int when present (some providers return strings)
    if home_pens is not None:
        try: home_pens = int(home_pens)
        except (TypeError, ValueError): home_pens = None
    if away_pens is not None:
        try: away_pens = int(away_pens)
        except (TypeError, ValueError): away_pens = None

    # Winner derivation depends on the source schema
    winner: str | None = None
    if winner_source == "api_football":
        home_w = (teams_block.get("home") or {}).get("winner")
        away_w = (teams_block.get("away") or {}).get("winner")
        if home_w is True:
            winner = "home"
        elif away_w is True:
            winner = "away"
        # Both null/false → no winner (draw, group stage, or pre-resolution)
    elif winner_source == "football_data":
        # football-data exposes a top-level winner enum in the score block
        fd_winner = (score_block or {}).get("winner")
        if fd_winner == "HOME_TEAM":
            winner = "home"
        elif fd_winner == "AWAY_TEAM":
            winner = "away"
    # Knockouts must always have a winner — if status is PEN/AET but winner
    # is None, the caller's responsibility is to log + skip.
    return home_pens, away_pens, winner


# ─── PROVIDER: MOCK ─────────────────────────────────────────────────────────
def fetch_mock() -> list[dict]:
    """Mock: return whatever's already in results_2026.json (manual entry mode)."""
    path = LIVE / "results_2026.json"
    if not path.exists():
        return []
    try:
        d = json.loads(path.read_text())
    except Exception as e:
        print(f"[fetch_results] mock read failed: {e}")
        return []
    return d.get("completed_matches", [])


# ─── PROVIDER: API-FOOTBALL ────────────────────────────────────────────────
APIFOOTBALL_BASE = "https://v3.football.api-sports.io"


# ─── EVENT NORMALISATION (Phase B3) ────────────────────────────────────────
# API-Football /fixtures/events shape:
#   {"time": {"elapsed": 25, "extra": null},
#    "team": {"id": 463, "name": "..."},
#    "player": {"id": 6126, "name": "..."},
#    "assist": {"id": null, "name": null},
#    "type": "Goal" | "Card" | "subst" | "Var",
#    "detail": "Normal Goal" | "Yellow Card" | "Red Card" | "Substitution 1" | ...,
#    "comments": null | str}
#
# Compact internal shape (consumed by suspension_tracker, scorer-rate, CLV):
#   {"type": "goal" | "card" | "subst" | "var" | "other",
#    "subtype": "normal_goal" | "yellow_card" | "red_card" | "second_yellow" | ...,
#    "team": "<canonical>",
#    "player": "<name>", "assist": "<name | None>",
#    "minute": int, "extra_minute": int | None,
#    "comments": str | None}
EVENT_TYPE_MAP = {
    "goal":  "goal",
    "card":  "card",
    "subst": "subst",
    "var":   "var",
}


def _slug_detail(detail: str | None) -> str:
    if not detail:
        return ""
    return "_".join(detail.strip().lower().split())


def normalize_event(e: dict) -> dict | None:
    """Map a raw API-Football event → compact dict. None on malformed input.

    Subtype slugging is deliberately verbatim ("normal_goal", "yellow_card",
    "substitution_1") so the suspension tracker can pattern-match without an
    enum table that lags the provider's vocabulary. Card detection keys off
    `type == "card"` + `subtype.startswith("yellow"/"red"/"second")`.
    """
    if not isinstance(e, dict):
        return None
    raw_type = str(e.get("type") or "").strip().lower()
    canon_type = EVENT_TYPE_MAP.get(raw_type, "other")
    detail = e.get("detail")
    subtype = _slug_detail(detail)
    time_block = e.get("time") or {}
    elapsed = time_block.get("elapsed")
    extra = time_block.get("extra")
    try:
        minute = int(elapsed) if elapsed is not None else None
    except (TypeError, ValueError):
        minute = None
    try:
        extra_minute = int(extra) if extra is not None else None
    except (TypeError, ValueError):
        extra_minute = None
    team_name_raw = (e.get("team") or {}).get("name", "")
    team_name = normalize_team(team_name_raw) if team_name_raw else None
    player = ((e.get("player") or {}).get("name") or None)
    assist = ((e.get("assist") or {}).get("name") or None)
    comments = e.get("comments")
    # API-Football encodes a 2nd-yellow as detail="Second Yellow card" — fold
    # it under the card type even if its provider type slug drifts.
    if canon_type == "other" and subtype.startswith(("yellow", "red", "second")):
        canon_type = "card"
    return {
        "type": canon_type,
        "subtype": subtype,
        "team": team_name,
        "player": player,
        "assist": assist,
        "minute": minute,
        "extra_minute": extra_minute,
        "comments": comments,
    }


def fetch_apifootball_events_for_fixture(
    api_key: str, fixture_id: str | int, timeout: int = 15,
) -> tuple[list[dict], dict | None]:
    """Fetch /fixtures/events?fixture={id}. Returns (events, warning_or_None).

    Returns ([], warning) on http/api errors so callers can attach the warning
    and continue — the events feed is supplemental to results_2026.json's
    locked scores. A failure here must never block the score-locking path.
    """
    headers = {"x-apisports-key": api_key, "Accept": "application/json"}
    url = f"{APIFOOTBALL_BASE}/fixtures/events?fixture={fixture_id}"
    try:
        # R11 C3: retries=3 (was 2). R32 burst of 8 KO matches in 24h hits
        # /fixtures/events back-to-back; a single 5xx with retries=2 means
        # one attempt + 1s sleep + one attempt → suspension data missed for
        # that match → no penalty for the player's next match. retries=3
        # adds a second retry (1+2=3s extra) and pairs with the R11 C1
        # Retry-After honoring in _http_client.http_get_json.
        payload = http_get_json(url, headers, timeout=timeout, retries=3)
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception: body = "<body unreadable>"
        return [], {"type": "events_http_error", "fixture_id": str(fixture_id),
                    "code": e.code, "body": body}
    except Exception as e:
        return [], {"type": "events_fetch_error", "fixture_id": str(fixture_id),
                    "message": f"{type(e).__name__}: {e}"}
    # Schema-drift watchdog: soft mode — logs a WARNING on shape drift but
    # never raises. Lets the events feed keep flowing while flagging the
    # operator that the provider changed something.
    assert_shape(payload,
                 _SCHEMA_BASELINE_DIR / "apifootball_fixtures_events.shape.json")
    if isinstance(payload, dict) and payload.get("errors"):
        errs = payload.get("errors") or {}
        if any(errs.values() if isinstance(errs, dict) else errs):
            return [], {"type": "events_api_error", "fixture_id": str(fixture_id),
                        "errors": errs}
    raw = payload.get("response") or []
    out: list[dict] = []
    for e in raw:
        norm = normalize_event(e)
        if norm is not None:
            out.append(norm)
    return out, None


def enrich_matches_with_events(
    matches: list[dict],
    api_key: str | None,
    existing_events_by_m: dict[int, list[dict]] | None = None,
    sleep_between: float = 0.15,
) -> tuple[list[dict], list[dict]]:
    """Attach `events: [...]` to each locked match record. Cache-aware.

    For any match `m` present in `existing_events_by_m`, we reuse the cached
    events (immutable once status is locked — see CORRECTIONS.md §4). For
    everything else we hit /fixtures/events once per fixture, with a small
    inter-request sleep to stay polite under API-Football's rate limit.

    A per-fixture failure attaches `events: []` + records a warning; the
    locked score itself is left untouched. Returns (matches, warnings).
    """
    cache = existing_events_by_m or {}
    warnings_out: list[dict] = []
    if not api_key:
        # No key — just thread cached events through and warn.
        for m in matches:
            mid = m.get("m")
            if mid in cache and "events" not in m:
                m["events"] = cache[mid]
        if any(m.get("status") in LOCKED_STATUSES for m in matches):
            warnings_out.append({"type": "events_missing_key",
                                 "message": "API_FOOTBALL_KEY not in env — events not fetched"})
        return matches, warnings_out
    fetched = 0
    reused = 0
    for m in matches:
        if (m.get("status") or "").upper() not in LOCKED_STATUSES:
            continue
        mid = m.get("m")
        if mid in cache:
            m["events"] = cache[mid]
            reused += 1
            continue
        fixture_id = m.get("provider_fixture_id")
        if not fixture_id:
            m["events"] = []
            warnings_out.append({"type": "events_no_fixture_id", "m": mid,
                                 "message": "no provider_fixture_id — cannot fetch events"})
            continue
        events, warn = fetch_apifootball_events_for_fixture(api_key, fixture_id)
        if warn:
            warn["m"] = mid
            warnings_out.append(warn)
            m["events"] = []
        else:
            m["events"] = events
            fetched += 1
        if sleep_between:
            time.sleep(sleep_between)
    if fetched or reused:
        print(f"[fetch_results] events: fetched={fetched} reused={reused} (cached)")
    return matches, warnings_out


def load_fixture_map() -> dict | None:
    """Returns {provider_fixture_id_str: internal_match_id} if map file exists."""
    p = LIVE / "provider_fixture_map.json"
    if not p.exists():
        return None
    try:
        m = json.loads(p.read_text())
        out = {}
        for fx in m.get("fixtures", []):
            pfid = fx.get("provider_fixture_id")
            mid = fx.get("match_id") or fx.get("m")
            if pfid and mid:
                out[str(pfid)] = int(mid)
        return out
    except Exception as e:
        print(f"[fetch_results] WARN: provider_fixture_map.json unreadable: {e}")
        return None


# ─── A.6 — KNOCKOUT FIXTURE-MAP AUTO-EXTENSION (2026-07-03) ─────────────────
# Root cause of the R32 freeze (2026-06-28 → 2026-07-03), a four-link chain:
#   1. data/live/provider_fixture_map.json was generated 2026-06-09 by the
#      pre-A.1 builder — 72 group ids only, no KO ids, no `coverage` block.
#   2. live-matchday.yml's rebuild Trigger 2 read `.coverage.knockout_total
#      // 0` from that legacy map → `0 -lt 0` never true → never rebuilt.
#   3. The per-fixture fallback below (date±1 + home + away vs the schedule)
#      can never match a KO fixture: bracket rows carry slot codes ("1A",
#      "W74", "3A/B/C/D/F"), not team names, so every provider R32 fixture
#      fell into `unmapped` and was dropped.
#   4. The drop was silent operationally: the KO-window CRITICAL message went
#      to stderr only — never into results_2026.json warnings — so
#      live_state.json showed zero warnings while completed_matches froze
#      at 72.
#
# Fix: extend the map in-process from the fixtures payload the adapter has
# ALREADY fetched this tick (zero extra API calls, no manual steps).
# Internal bracket slots are resolved to team names from locked results via
# export_ko_advance's resolver (group ranks + Annex C third-place routing +
# W/L codes — the same single source of truth the KO advance-prob export
# uses). Provider KO fixtures are then matched by stage + unordered team
# pair + date±1, with the same TEAM_ALIAS normalisation used at fetch time.
# Matched ids are persisted to provider_fixture_map.json (already in the
# workflow's commit allow-list) so later ticks take the O(1) map path.
# Rounds bootstrap sequentially: R32 resolves from the group results already
# on disk; R16 resolves once R32 results lock; and so on through the final.
#
# Unmapped fixtures inside the tournament window now emit a structured
# warning via build_unmapped_warnings — surfaced in results_2026.json →
# live_state.json → dashboard. Never stderr-only again.

# _knockout.load_knockout_fixtures stage tags ("3rd") vs builder phase codes
# ("third_place") — bridge the two vocabularies.
_PHASE_TO_STAGE = {
    "r32": "r32", "r16": "r16", "qf": "qf", "sf": "sf",
    "third_place": "3rd", "final": "final",
}


def _dates_within_one_day(a: str | None, b: str | None) -> bool:
    """±1 day tolerance — same UTC↔local rollover window the fuzzy fallback
    uses (NA evening matches roll past midnight UTC)."""
    if not a or not b:
        return False
    if a[:10] == b[:10]:
        return True
    from datetime import date as _date
    try:
        da, db = _date.fromisoformat(a[:10]), _date.fromisoformat(b[:10])
    except ValueError:
        return False
    return abs((da - db).days) <= 1


def _resolved_bracket_rows() -> list[dict]:
    """load_knockout_fixtures() rows annotated with `resolved_home` /
    `resolved_away` team names (None while the feeding results are still
    unknown). Never raises: a resolver failure degrades to unresolved rows,
    the extension no-ops, and the loud unmapped warning downstream fires."""
    rows = load_knockout_fixtures()
    if not rows:
        return []
    for row in rows:
        row["resolved_home"] = None
        row["resolved_away"] = None
    try:
        from scripts.live.export_ko_advance import (  # noqa: PLC0415
            _build_completed_index, _resolve_group_slots, _resolve_slot,
        )
        cfg = json.loads((RAW / "wc2026_config.json").read_text())
        annex_path = RAW / "annex_c_third_place_table_2026.json"
        annex_c = json.loads(annex_path.read_text()) if annex_path.exists() else {}
        completed_idx = _build_completed_index(LIVE / "results_2026.json")
        group_slots = _resolve_group_slots(completed_idx, cfg, annex_c)
    except Exception as e:
        print(f"[fetch_results] WARN: KO bracket resolution unavailable — "
              f"{type(e).__name__}: {e}")
        return rows
    for row in rows:
        ctx = {"completed_idx": completed_idx, "group_slots": group_slots,
               "r32_match_num": row["m"]}
        try:
            row["resolved_home"] = _resolve_slot(row.get("home"), ctx)
            row["resolved_away"] = _resolve_slot(row.get("away"), ctx)
        except Exception as e:
            print(f"[fetch_results] WARN: KO slot resolution failed for "
                  f"m={row.get('m')} — {type(e).__name__}: {e}")
    return rows


def extend_fixture_map_with_knockouts(
    provider_rows: list[dict],
    provider: str,
    write: bool = True,
) -> dict[str, int]:
    """Return {provider_fixture_id: internal_m} for newly-mapped KO fixtures.

    `provider_rows` is the minimal normalised view of the fixtures payload
    the adapter already holds: [{"id", "home", "away", "date", "round"}],
    with home/away already normalize_team()'d. A provider fixture maps to a
    bracket row iff its classified stage matches, its unordered team pair
    equals the row's resolved pair, and the dates agree within ±1 day.
    Single-elimination guarantees a team appears at most once per round, so
    (stage, pair) is unique on both sides — no positional guessing, no
    cross-wired ids (a mis-mapped id would write a real score into the
    WRONG match: validate_match skips the team cross-check for m>=73).

    Persists the extended map atomically when `write` is True, recomputing
    the builder-shaped `coverage` block so the workflow's bootstrap-rebuild
    trigger sees truthful numbers. Never raises: any failure returns {} and
    the caller's unmapped path emits the loud warning instead."""
    try:
        candidates = []
        for r in provider_rows:
            pfid = str(r.get("id") or "")
            phase = classify_round(r.get("round"))
            if not pfid or phase is None:
                continue
            candidates.append((pfid, phase, r))
        if not candidates:
            return {}

        map_path = LIVE / "provider_fixture_map.json"
        doc: dict = {}
        if map_path.exists():
            try:
                doc = json.loads(map_path.read_text())
            except Exception as e:
                print(f"[fetch_results] WARN: fixture map unreadable for KO "
                      f"extension ({e}) — starting a fresh map skeleton")
                doc = {}
        if doc.get("provider") and doc.get("provider") != provider:
            # Never graft one provider's fixture ids onto another provider's
            # map — the workflow's provider-mismatch trigger rebuilds instead.
            print(f"[fetch_results] KO map extension skipped: map provider="
                  f"{doc.get('provider')!r} != active provider {provider!r}")
            return {}
        fixtures = [f for f in doc.get("fixtures", []) if isinstance(f, dict)]
        known_ids = {str(f.get("provider_fixture_id")) for f in fixtures}
        used_ms = set()
        for f in fixtures:
            mid = f.get("match_id") or f.get("m")
            if mid is not None:
                used_ms.add(int(mid))

        pending = [(pfid, phase, r) for (pfid, phase, r) in candidates
                   if pfid not in known_ids]
        if not pending:
            return {}

        by_stage_pair: dict[tuple[str, tuple[str, str]], dict] = {}
        for row in _resolved_bracket_rows():
            h, a = row.get("resolved_home"), row.get("resolved_away")
            if not h or not a or int(row["m"]) in used_ms:
                continue
            by_stage_pair[(row["stage"], tuple(sorted((h, a))))] = row

        added: dict[str, int] = {}
        new_records: list[dict] = []
        # Deterministic claim order (PYTHONHASHSEED-independent): sort by
        # (date, provider id) so re-runs on the same payload map identically.
        for pfid, phase, r in sorted(pending,
                                     key=lambda t: (t[2].get("date") or "", t[0])):
            stage = _PHASE_TO_STAGE.get(phase, phase)
            pair = tuple(sorted((r.get("home") or "", r.get("away") or "")))
            row = by_stage_pair.get((stage, pair))
            if row is None or not _dates_within_one_day(r.get("date"), row.get("date")):
                continue
            if int(row["m"]) in used_ms:
                continue  # first claim wins — a dup provider row can't fork
            used_ms.add(int(row["m"]))
            added[pfid] = int(row["m"])
            new_records.append({
                "match_id": int(row["m"]),
                "provider_fixture_id": pfid,
                "home": r.get("home"),
                "away": r.get("away"),
                "date": row.get("date"),
                "phase": phase,
            })
        if not added:
            return {}

        ms = sorted(added.values())
        print(f"[fetch_results] KO map auto-extension: +{len(added)} fixtures "
              f"(m={ms}) write={write}")
        if write:
            fixtures.extend(new_records)
            fixtures.sort(key=lambda x: int(x.get("match_id") or x.get("m") or 0))
            doc["fixtures"] = fixtures
            doc.setdefault("provider", provider)
            doc["extended_at"] = datetime.now(timezone.utc).isoformat()
            doc["extended_by"] = "fetch_results.knockout_auto_extension"
            ko_total = len(load_knockout_fixtures()) or 32
            n_group = sum(1 for f in fixtures
                          if int(f.get("match_id") or f.get("m") or 0) <= 72)
            n_ko = len(fixtures) - n_group
            doc["coverage"] = {
                "group_mapped": n_group,
                "group_total": 72,
                "knockout_mapped": n_ko,
                "knockout_total": ko_total,
                "total_mapped": len(fixtures),
                "total_expected": 72 + ko_total,
            }
            atomic_write_json(map_path, doc)
        return added
    except Exception as e:
        print(f"[fetch_results] WARN: KO map auto-extension failed — "
              f"{type(e).__name__}: {e}")
        return {}


def _known_team_set() -> set[str]:
    """All 48 canonical WC2026 team names (from config groups)."""
    try:
        cfg = json.loads((RAW / "wc2026_config.json").read_text())
        teams: set[str] = set()
        for g_teams in (cfg.get("groups") or {}).values():
            teams.update(g_teams)
        return teams
    except Exception:
        return set()


def _tournament_window() -> tuple[str, str]:
    """(first, last) fixture date of WC2026, derived from config + bracket so
    a schedule change never silently narrows the warning window. Falls back
    to the published 2026-06-11 → 2026-07-19 window."""
    lo, hi = "2026-06-11", "2026-07-19"
    try:
        cfg = json.loads((RAW / "wc2026_config.json").read_text())
        dates = [s.get("date") for s in cfg.get("group_stage_schedule", [])
                 if s.get("date")]
        dates += [r.get("date") for r in load_knockout_fixtures() if r.get("date")]
        if dates:
            lo, hi = min(dates), max(dates)
    except Exception:
        pass
    return lo, hi


def build_unmapped_warnings(unmapped: list[dict], provider: str) -> list[dict]:
    """A.6: structured warning for unmapped provider fixtures inside the
    tournament window — lands in results_2026.json → live_state.json →
    dashboard. Pre-fix the only signal was a stderr print (link 4 of the
    R32-freeze chain).

    A fixture is warn-worthy when its date falls inside the window AND it is
    either already meaningful (locked/live status — a real result is being
    dropped RIGHT NOW) or both team names resolve to known WC2026 teams
    (the pairing exists but we failed to place it — alias drift or bracket-
    resolution disagreement). Pre-draw KO fixtures carrying provider
    placeholder names ("Winner Group A") stay quiet: they are expected to be
    unmappable until the bracket resolves and would spam every group-stage
    tick otherwise."""
    lo, hi = _tournament_window()
    known = _known_team_set()
    noisy: list[dict] = []
    for u in unmapped:
        d = (u.get("date") or "")[:10]
        if not (lo <= d <= hi):
            continue
        raw_status = (u.get("status") or "").upper()
        canon = (APIFOOTBALL_STATUS_MAP.get(raw_status)
                 or FOOTBALLDATA_STATUS_MAP.get(raw_status)
                 or raw_status)
        live_or_locked = canon in LOCKED_STATUSES or canon == "LIVE"
        home = normalize_team(u.get("home") or "")
        away = normalize_team(u.get("away") or "")
        teams_known = bool(known) and home in known and away in known
        if live_or_locked or teams_known:
            noisy.append(dict(u))
    if not noisy:
        return []
    return [{
        "type": "unmapped_provider_fixture",
        "severity": "critical",
        "provider": provider,
        "count": len(noisy),
        "message": (
            f"{len(noisy)} provider fixture(s) inside the tournament window "
            f"could not be mapped to internal match ids — their results are "
            f"NOT being ingested. KO auto-extension could not place them: "
            f"check provider_fixture_map.json coverage and TEAM_ALIAS for "
            f"the names below, or rebuild via scripts/live/"
            f"build_provider_fixture_map.py --provider {provider} --write."
        ),
        "fixtures": noisy[:8],
    }]


def fetch_api_football(api_key: str, dry_run: bool = False,
                       warnings_sink: list | None = None) -> list[dict]:
    """Fetch WC2026 fixtures from API-Football v3, normalise to our schema.

    A.6: `warnings_sink` (optional, append-only) receives structured
    warnings — currently unmapped-tournament-fixture alerts — that main()
    folds into results_2026.json's warnings array."""
    headers = {"x-apisports-key": api_key, "Accept": "application/json"}

    # League + season come from the fixture map (preferred) or env (override)
    fix_map_file = LIVE / "provider_fixture_map.json"
    league_id = os.environ.get("API_FOOTBALL_LEAGUE_ID")
    season = os.environ.get("API_FOOTBALL_SEASON")
    if fix_map_file.exists():
        try:
            mf = json.loads(fix_map_file.read_text())
            league_id = league_id or mf.get("league_id")
            season = season or mf.get("season")
        except Exception:
            pass
    league_id = league_id or "1"   # API-Football's FIFA World Cup default
    season = season or "2026"

    url = f"{APIFOOTBALL_BASE}/fixtures?league={league_id}&season={season}"
    print(f"[fetch_results] GET {url}")

    try:
        payload = http_get_json(url, headers)
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception as _body_err: body = f"<body unreadable: {type(_body_err).__name__}: {_body_err}>"
        print(f"[fetch_results] API-Football HTTP {e.code}: {body}")
        return []
    except Exception as e:
        print(f"[fetch_results] API-Football fetch failed: {type(e).__name__}: {e}")
        return []

    # Schema-drift watchdog: soft mode — logs a WARNING on shape drift but
    # never raises. The scoring + locking logic below stays intact even if
    # the provider added/removed/renamed a field; the warning gives the
    # operator a heads-up before a silent data loss bug appears.
    assert_shape(payload,
                 _SCHEMA_BASELINE_DIR / "apifootball_fixtures.shape.json")

    if payload.get("errors"):
        print(f"[fetch_results] API-Football returned errors: {payload['errors']}")
        # Don't return [] silently — surface upstream so we don't overwrite locked data
        if any(payload["errors"].values()):
            return []

    response = payload.get("response", []) or []
    print(f"[fetch_results] API-Football returned {len(response)} fixtures")

    if dry_run:
        # Print status distribution + next 5 + finals
        status_dist = {}
        finals, upcoming = [], []
        for f in response:
            s = (f.get("fixture", {}).get("status", {}) or {}).get("short", "?")
            status_dist[s] = status_dist.get(s, 0) + 1
            mapped = APIFOOTBALL_STATUS_MAP.get(s, s)
            if mapped in LOCKED_STATUSES: finals.append(f)
            elif mapped == "SCHEDULED": upcoming.append(f)
        print(f"[dry-run] status distribution: {status_dist}")
        print(f"[dry-run] finished: {len(finals)}, upcoming: {len(upcoming)}")
        for f in upcoming[:5]:
            home = (f.get("teams", {}).get("home", {}) or {}).get("name", "?")
            away = (f.get("teams", {}).get("away", {}) or {}).get("name", "?")
            date = (f.get("fixture", {}) or {}).get("date", "?")
            print(f"  upcoming: {date}  {home} vs {away}")
        for f in finals[:5]:
            home = (f.get("teams", {}).get("home", {}) or {}).get("name", "?")
            away = (f.get("teams", {}).get("away", {}) or {}).get("name", "?")
            gh = (f.get("goals", {}) or {}).get("home")
            ga = (f.get("goals", {}) or {}).get("away")
            print(f"  final: {home} {gh}-{ga} {away}")

    fixture_map = load_fixture_map() or {}
    if not fixture_map:
        print("[fetch_results] WARN: no provider_fixture_map.json — falling back to "
              "team+date matching. Run scripts/live/build_provider_fixture_map.py to "
              "create a deterministic map.")

    # A.6: auto-extend the map with knockout fixture ids present in THIS
    # payload — zero extra API calls, no manual rebuild step. See the block
    # comment above extend_fixture_map_with_knockouts for the root-cause
    # chain this closes (R32 freeze, 2026-06-28 → 2026-07-03).
    map_rows = [{
        "id": str((f.get("fixture") or {}).get("id", "")),
        "home": normalize_team((((f.get("teams") or {}).get("home")) or {}).get("name", "")),
        "away": normalize_team((((f.get("teams") or {}).get("away")) or {}).get("name", "")),
        "date": ((f.get("fixture") or {}).get("date") or "")[:10],
        "round": (f.get("league") or {}).get("round", ""),
    } for f in response]
    added = extend_fixture_map_with_knockouts(
        map_rows, provider="api_football", write=not dry_run)
    if added:
        fixture_map.update(added)

    # Load our schedule for fuzzy fallback + date validation
    cfg = json.loads((RAW / "wc2026_config.json").read_text())
    # R9 P4 A2: extend with KO bracket so KO match IDs (m=73..104) can be
    # mapped from provider_fixture_map.json. Pre-R9 only group_stage_schedule
    # was loaded — any KO fixture_id that mapped to m_id ∈ [73,104] hit the
    # `sched = schedule_by_id.get(m_id)` line at the previous loop iteration
    # and got silently dropped to `unmapped` with reason="m not in schedule".
    # Net effect pre-R9: entire knockout phase invisible to fetch_results;
    # results_2026.json.completed_matches frozen at 72; predictions_live
    # never updates for any KO outcome; dashboard locks at pre-R32 state.
    # KO entries have slot codes ("1A", "W74") in home/away — for KO output
    # rows we use the provider's normalised team names directly (handled at
    # the result-emission step below).
    ko_schedule = load_knockout_fixtures()
    schedule = cfg["group_stage_schedule"] + ko_schedule
    schedule_by_id = {f["m"]: f for f in schedule}

    out = []
    unmapped = []
    for f in response:
        fx = f.get("fixture") or {}
        teams = f.get("teams") or {}
        goals = f.get("goals") or {}
        status = (fx.get("status") or {})
        short = status.get("short", "")
        canon_status = APIFOOTBALL_STATUS_MAP.get(short, short)
        provider_fixture_id = str(fx.get("id", ""))
        home_raw = (teams.get("home") or {}).get("name", "")
        away_raw = (teams.get("away") or {}).get("name", "")
        home = normalize_team(home_raw)
        away = normalize_team(away_raw)
        date = (fx.get("date") or "")[:10]  # ISO → YYYY-MM-DD

        # Resolve to our match id: prefer fixture map, fall back to (date±1, home, away)
        # ±1 day handles UTC↔local boundary (NA evening matches roll past midnight UTC)
        m_id = fixture_map.get(provider_fixture_id)
        if m_id is None:
            from datetime import date as _date, timedelta as _td
            try:
                d0 = _date.fromisoformat(date)
                date_window = {d0.isoformat(), (d0 - _td(days=1)).isoformat(),
                               (d0 + _td(days=1)).isoformat()}
            except Exception:
                date_window = {date}
            cand = next((s for s in schedule
                         if s["date"] in date_window
                         and s["home"] == home and s["away"] == away), None)
            if cand:
                m_id = cand["m"]
        if m_id is None:
            unmapped.append({"fixture_id": provider_fixture_id, "home": home_raw,
                             "away": away_raw, "date": date, "status": short,
                             "round": (f.get("league") or {}).get("round", "")})
            continue

        sched = schedule_by_id.get(m_id)
        if not sched:
            unmapped.append({"fixture_id": provider_fixture_id, "m": m_id, "reason": "m not in schedule"})
            continue

        gh = goals.get("home")
        ga = goals.get("away")
        if canon_status in LOCKED_STATUSES and (gh is None or ga is None):
            print(f"[fetch_results] WARN: M{m_id} status={short} but goals missing — skipping")
            continue

        # A.2 — knockout decoding: extract PEN sub-scores + winner.
        # Verified via A.0 probe: API-Football populates score.penalty.{home,away}
        # for PEN matches and teams.{home,away}.winner (true/false) for any
        # completed knockout. Group stage matches leave penalty as {null, null}
        # and winner may be null on draws — those resolve to (None, None, None)
        # which is exactly what extract_pens_and_winner returns.
        score_block = f.get("score") or {}
        home_pens, away_pens, winner = extract_pens_and_winner(
            score_block, teams, canon_status,
        )
        if canon_status == "PEN" and winner is None:
            # The provider classified this as a shootout but didn't populate
            # a winner — defensive: skip rather than fabricate. Real PEN
            # matches always have a winner; missing means transient bad data.
            print(f"[fetch_results] WARN: M{m_id} status=PEN but no winner field — skipping")
            continue

        # Phase 2 — referee name from fixture.referee (probed in A.0 as present).
        # May be None for unassigned/early fixtures; downstream lookup handles
        # absence gracefully (zero contribution).
        referee_raw = fx.get("referee")
        referee = referee_raw.strip() if isinstance(referee_raw, str) else None

        # R9 P4 A2: for KO matches (m >= 73), sched["home"]/["away"] are
        # bracket slot codes ("1A", "W74") that are placeholders until
        # results resolve them — emitting those into results_2026.json
        # would be wrong (the simulator's locked_score path doesn't read
        # home/away strings, but dashboards and audit logs do). Use the
        # provider's normalised team names for KO; sched values for group
        # (where sched["home"] matches normalize_team(home_raw) by design).
        is_ko_match = int(m_id) >= 73
        out.append({
            "m": int(m_id),
            "provider_fixture_id": provider_fixture_id,
            "date": sched["date"],
            "home": home if is_ko_match else sched["home"],
            "away": away if is_ko_match else sched["away"],
            "home_score": int(gh) if isinstance(gh, int) else None,
            "away_score": int(ga) if isinstance(ga, int) else None,
            "home_pens": home_pens,
            "away_pens": away_pens,
            "winner": winner,
            "status": canon_status,
            "status_long": status.get("long", ""),
            "elapsed": status.get("elapsed"),
            "referee": referee,
            "source": "api_football",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "raw_status": short,
        })

    if unmapped:
        print(f"[fetch_results] {len(unmapped)} unmapped provider fixtures (likely friendlies)")
        for u in unmapped[:3]:
            print(f"  ? {u}")
        # R9 P4 A2: louder warning when an unmapped fixture has a date in the
        # KO window. Pre-R9 those silently dropped; if R32 kickoff (2026-06-28)
        # passes and a KO match still isn't in provider_fixture_map.json,
        # operator MUST rebuild via scripts/live/build_provider_fixture_map.py
        # or KO results never lock and the dashboard freezes at end-of-groups.
        ko_unmapped = [u for u in unmapped if (u.get("date") or "") >= "2026-06-28"]
        if ko_unmapped:
            print(f"[fetch_results] CRITICAL: {len(ko_unmapped)} unmapped fixtures "
                  f"in KO window (date>=2026-06-28). Rebuild provider_fixture_map.json "
                  f"or KO results will not lock. Sample:", file=sys.stderr)
            for u in ko_unmapped[:5]:
                print(f"  KO-unmapped: {u}", file=sys.stderr)
        # A.6: the stderr print above is invisible to the dashboard — ALSO
        # emit a structured warning that main() writes into results_2026.json
        # (→ live_state.json). This is what makes the drop non-silent.
        if warnings_sink is not None:
            warnings_sink.extend(
                build_unmapped_warnings(unmapped, provider="api_football"))

    return out


# ─── PROVIDER: FOOTBALL-DATA.ORG ───────────────────────────────────────────
FOOTBALLDATA_BASE = "https://api.football-data.org/v4"


def fetch_football_data(token: str, dry_run: bool = False,
                        warnings_sink: list | None = None) -> list[dict]:
    """Fetch WC2026 fixtures from football-data.org, normalise to our schema.

    Free tier:
      - 10 requests/minute
      - FIFA World Cup competition code: "WC"
      - Endpoint: GET /v4/competitions/WC/matches
      - Auth header: X-Auth-Token
    """
    headers = {"X-Auth-Token": token, "Accept": "application/json"}
    competition = os.environ.get("FOOTBALL_DATA_COMPETITION") or "WC"
    url = f"{FOOTBALLDATA_BASE}/competitions/{competition}/matches"
    print(f"[fetch_results] GET {url}")

    try:
        payload = http_get_json(url, headers)
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception as _body_err: body = f"<body unreadable: {type(_body_err).__name__}: {_body_err}>"
        print(f"[fetch_results] football-data.org HTTP {e.code}: {body}")
        return []
    except Exception as e:
        print(f"[fetch_results] football-data.org fetch failed: {type(e).__name__}: {e}")
        return []

    matches_raw = payload.get("matches", []) or []
    print(f"[fetch_results] football-data.org returned {len(matches_raw)} matches")

    if dry_run:
        status_dist: dict[str, int] = {}
        for m in matches_raw:
            s = m.get("status", "?")
            status_dist[s] = status_dist.get(s, 0) + 1
        print(f"[dry-run] status distribution: {status_dist}")
        for m in matches_raw[:5]:
            home = (m.get("homeTeam") or {}).get("name", "?")
            away = (m.get("awayTeam") or {}).get("name", "?")
            print(f"  fixture: {m.get('utcDate', '?')[:16]} {home} vs {away}  status={m.get('status')}")

    fixture_map = load_fixture_map() or {}

    # A.6: auto-extend the map with knockout fixture ids present in THIS
    # payload (see fetch_api_football — same fix, football-data flavour).
    # classify_round accepts football-data stage enums (LAST_32, …) directly.
    map_rows = [{
        "id": str(m.get("id", "")),
        "home": normalize_team(((m.get("homeTeam") or {}).get("name")) or ""),
        "away": normalize_team(((m.get("awayTeam") or {}).get("name")) or ""),
        "date": (m.get("utcDate") or "")[:10],
        "round": m.get("stage") or "",
    } for m in matches_raw]
    added = extend_fixture_map_with_knockouts(
        map_rows, provider="football_data", write=not dry_run)
    if added:
        fixture_map.update(added)

    cfg = json.loads((RAW / "wc2026_config.json").read_text())
    # R9 P4 A2: extend with KO bracket (same closure as fetch_apifootball above).
    ko_schedule = load_knockout_fixtures()
    schedule = cfg["group_stage_schedule"] + ko_schedule
    schedule_by_id = {f["m"]: f for f in schedule}

    out: list[dict] = []
    unmapped: list[dict] = []
    for m in matches_raw:
        provider_id = str(m.get("id", ""))
        home = normalize_team((m.get("homeTeam") or {}).get("name", ""))
        away = normalize_team((m.get("awayTeam") or {}).get("name", ""))
        date = (m.get("utcDate") or "")[:10]
        raw_status = m.get("status", "")
        canon_status = FOOTBALLDATA_STATUS_MAP.get(raw_status, raw_status)
        score = m.get("score") or {}
        full_time = score.get("fullTime") or {}
        extra_time = score.get("extraTime") or {}
        penalties = score.get("penalties") or {}

        # Resolve to our match id
        m_id = fixture_map.get(provider_id)
        if m_id is None:
            from datetime import date as _date, timedelta as _td
            try:
                d0 = _date.fromisoformat(date)
                date_window = {d0.isoformat(),
                               (d0 - _td(days=1)).isoformat(),
                               (d0 + _td(days=1)).isoformat()}
            except Exception:
                date_window = {date}
            cand = next((s for s in schedule
                         if s["date"] in date_window
                         and s["home"] == home and s["away"] == away), None)
            if cand:
                m_id = cand["m"]

        if m_id is None:
            unmapped.append({"id": provider_id, "home": home, "away": away,
                             "date": date, "status": raw_status,
                             "round": m.get("stage") or ""})
            continue
        sched = schedule_by_id.get(m_id)
        if not sched:
            continue

        # Choose the right "final" goals: if AET/PEN was reached, use that;
        # otherwise plain fullTime.
        gh, ga = full_time.get("home"), full_time.get("away")
        # Distinguish AET / PEN by which sub-score is populated
        eff_status = canon_status
        if canon_status == "FT":
            if penalties.get("home") is not None or penalties.get("away") is not None:
                eff_status = "PEN"
            elif extra_time.get("home") is not None or extra_time.get("away") is not None:
                eff_status = "AET"
                # AET goals are commonly the cumulative score at end of ET → fullTime already holds it
        if eff_status in LOCKED_STATUSES and (gh is None or ga is None):
            print(f"[fetch_results] WARN: M{m_id} status={raw_status} but score missing — skipping")
            continue

        # A.2 — knockout decoding (football-data.org variant).
        # Previously this adapter inferred AET/PEN from sub-score presence but
        # discarded the penalty sub-scores themselves and never captured the
        # winner. That was a bug: a 1-1 (4-3 pens) match would store as
        # home_score=1, away_score=1 with no way for downstream consumers to
        # tell who advanced. Now using the shared extract_pens_and_winner.
        # football-data wraps the winner enum on the score block, not teams.
        home_pens, away_pens, winner = extract_pens_and_winner(
            score, teams_block={}, canon_status=eff_status,
            winner_source="football_data",
        )
        if eff_status == "PEN" and winner is None:
            print(f"[fetch_results] WARN: M{m_id} status=PEN but no winner field — skipping")
            continue

        # R9 P4 A2: KO matches use provider team names (sched has slot codes).
        is_ko_match = int(m_id) >= 73
        out.append({
            "m": int(m_id),
            "provider_fixture_id": provider_id,
            "date": sched["date"],
            "home": home if is_ko_match else sched["home"],
            "away": away if is_ko_match else sched["away"],
            "home_score": int(gh) if isinstance(gh, int) else None,
            "away_score": int(ga) if isinstance(ga, int) else None,
            "home_pens": home_pens,
            "away_pens": away_pens,
            "winner": winner,
            "status": eff_status,
            "status_long": raw_status.replace("_", " ").title(),
            "elapsed": (m.get("minute") if isinstance(m.get("minute"), int) else None),
            "source": "football_data",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "raw_status": raw_status,
        })

    if unmapped:
        print(f"[fetch_results] {len(unmapped)} unmapped football-data fixtures (likely friendlies)")
        for u in unmapped[:3]:
            print(f"  ? {u}")
        # R9 P4 A2: same KO-window critical warning as fetch_apifootball.
        ko_unmapped = [u for u in unmapped if (u.get("date") or "") >= "2026-06-28"]
        if ko_unmapped:
            print(f"[fetch_results] CRITICAL: {len(ko_unmapped)} unmapped fixtures "
                  f"in KO window (date>=2026-06-28). Rebuild provider_fixture_map.json "
                  f"or KO results will not lock.", file=sys.stderr)
            for u in ko_unmapped[:5]:
                print(f"  KO-unmapped: {u}", file=sys.stderr)
        # A.6: structured warning → results_2026.json → live_state.json.
        if warnings_sink is not None:
            warnings_sink.extend(
                build_unmapped_warnings(unmapped, provider="football_data"))

    return out


# ─── PROVIDER: SPORTMONKS (stub) ───────────────────────────────────────────
def fetch_sportmonks(token: str, dry_run: bool = False) -> list[dict]:
    """Placeholder for Sportmonks. See fetch_api_football for the pattern."""
    print("[fetch_results] sportmonks adapter not yet wired — returning empty list.")
    return []


# ─── VALIDATION ────────────────────────────────────────────────────────────
def validate_match(m: dict, schedule: list) -> tuple[bool, str]:
    """Schema + cross-reference validation.

    R11 E1: schedule must include BOTH group_stage_schedule and the KO
    bracket rows from load_knockout_fixtures() — pre-R11 main() loaded
    only groups, so every KO result (m>=73) was silently rejected from
    2026-06-28 onward. KO bracket rows carry slot codes ("1A", "W74")
    in home/away until results resolve them, but the API-Football
    adapter substitutes resolved team names for m>=73 at fetch time
    (fetch_results.py:661-667). The home/away string cross-check is
    therefore skipped for KO matches — comparing resolved team names
    to bracket slot codes would reject every KO result forever.
    """
    required = ["m", "home_score", "away_score"]
    for k in required:
        if k not in m:
            return False, f"missing {k}"
    if not isinstance(m["m"], int):
        return False, "invalid match id (not int)"
    if not isinstance(m["home_score"], int) or m["home_score"] < 0:
        return False, "invalid home_score"
    if not isinstance(m["away_score"], int) or m["away_score"] < 0:
        return False, "invalid away_score"
    if m["home_score"] > 30 or m["away_score"] > 30:
        return False, "implausible scoreline (>30 goals)"
    fixture = next((f for f in schedule if f["m"] == m["m"]), None)
    if not fixture:
        return False, f"match {m['m']} not in WC2026 schedule"
    # KO matches: skip home/away string check (see docstring).
    if m["m"] >= 73:
        return True, "ok"
    if m.get("home") and m["home"] != fixture["home"]:
        return False, f"home mismatch: expected {fixture['home']}, got {m['home']}"
    if m.get("away") and m["away"] != fixture["away"]:
        return False, f"away mismatch: expected {fixture['away']}, got {m['away']}"
    return True, "ok"


# ─── MAIN ──────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=None,
                    help="mock | api_football | sportmonks (default: env)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Fetch and print plan, but do not write results_2026.json")
    ap.add_argument("--with-events", action="store_true",
                    default=(os.environ.get("WC_FETCH_EVENTS", "").lower()
                             in ("1", "true", "yes", "on")),
                    help="Enrich locked matches with /fixtures/events. "
                         "Cached events on existing results_2026.json are reused.")
    args = ap.parse_args()

    src = (args.provider or get_provider_name()).lower().replace("-", "_")
    print(f"[fetch_results] provider={src}{' (dry-run)' if args.dry_run else ''}")

    try:
        cfg = json.loads((RAW / "wc2026_config.json").read_text())
    except Exception as e:
        print(f"[fetch_results] FATAL: cannot read wc2026_config.json — {e}")
        return 1
    # R11 E1 (R32-blocker): extend schedule with KO bracket so validate_match
    # accepts m>=73 records. Pre-R11 main() loaded only group_stage_schedule;
    # from 2026-06-28 every R32/R16/QF/SF/3rd/Final result emitted by both
    # adapters was rejected at the validation gate with reason
    # "match N not in WC2026 schedule" — dashboard would freeze at end of
    # groups and suspensions would never resolve from KO events.
    schedule = cfg.get("group_stage_schedule", []) + load_knockout_fixtures()
    if not schedule:
        print("[fetch_results] FATAL: empty group_stage_schedule in config")
        return 1

    matches: list[dict] = []
    # A.6: adapter-level structured warnings (unmapped tournament-window
    # fixtures). Folded into warnings_list below so they land in
    # results_2026.json and flow to live_state.json via the orchestrator's
    # get_results_warnings() — the pre-fix path was stderr-only.
    adapter_warnings: list[dict] = []
    try:
        if src == "mock":
            matches = fetch_mock()
        elif src in ("api_football", "apifootball"):
            key = get_api_football_key()
            if not key:
                print("[fetch_results] API_FOOTBALL_KEY missing; falling back to mock")
                matches = fetch_mock()
                src = "mock"
            else:
                matches = fetch_api_football(key, dry_run=args.dry_run,
                                             warnings_sink=adapter_warnings)
        elif src in ("football_data", "footballdata"):
            token = get_football_data_token()
            if not token:
                print("[fetch_results] FOOTBALL_DATA_TOKEN missing; falling back to mock")
                matches = fetch_mock()
                src = "mock"
            else:
                matches = fetch_football_data(token, dry_run=args.dry_run,
                                              warnings_sink=adapter_warnings)
                src = "football_data"
        elif src == "sportmonks":
            token = get_sportmonks_token()
            if not token:
                print("[fetch_results] SPORTMONKS_TOKEN missing; falling back to mock")
                matches = fetch_mock()
                src = "mock"
            else:
                matches = fetch_sportmonks(token, dry_run=args.dry_run)
        else:
            print(f"[fetch_results] unknown provider {src!r}; falling back to mock")
            matches = fetch_mock()
            src = "mock"
    except Exception as e:
        print(f"[fetch_results] adapter raised {type(e).__name__}: {e} — keeping existing file")
        return 0  # graceful no-op

    if not isinstance(matches, list):
        print(f"[fetch_results] adapter returned non-list ({type(matches).__name__}); keeping existing file")
        return 0

    # Validate + dedupe + categorize
    seen_m = set()
    valid: list[dict] = []
    rejected: list[tuple[dict, str]] = []
    warnings_list: list[dict] = []
    # P1-G: capture in-play matches (status mapped to "LIVE") so the
    # orchestrator can surface them as a live-game strip on the dashboard
    # without waiting for FT. Score may be partial but is the truth-of-the-
    # moment; elapsed minutes are useful UI context.
    in_play: list[dict] = []
    for m in matches:
        if not isinstance(m, dict):
            rejected.append(({"m": "?"}, f"non-dict record ({type(m).__name__})"))
            continue
        status = (m.get("status") or "").upper()
        if status in WARN_STATUSES:
            warnings_list.append({"m": m.get("m", "?"), "status": status, "note": m.get("note", "")})
            continue
        if status == "LIVE":
            # Minimal payload — score may be None very early in the match.
            in_play.append({
                "m": m.get("m"),
                "home": m.get("home"),
                "away": m.get("away"),
                "home_score": m.get("home_score"),
                "away_score": m.get("away_score"),
                "elapsed": m.get("elapsed"),
                "status": status,
                "status_long": m.get("status_long", ""),
            })
            continue
        if status and status not in LOCKED_STATUSES:
            continue  # SCHEDULED — skip silently
        if m.get("m") in seen_m:
            rejected.append((m, "duplicate match id"))
            continue
        try:
            ok, why = validate_match(m, schedule)
        except Exception as e:
            rejected.append((m, f"validator crashed: {type(e).__name__}: {e}"))
            continue
        if not ok:
            rejected.append((m, why))
            continue
        seen_m.add(m["m"])
        valid.append(m)

    # A.6: adapter warnings join the per-match status warnings. They ride
    # every write path below — the happy write, the shrink-refusal preserve
    # (existing["warnings"] = warnings_list), and they disarm the
    # nothing-useful preserve guard (`not warnings_list`), so an unmapped
    # alert can never be swallowed by a preservation branch.
    warnings_list.extend(adapter_warnings)

    print(f"[fetch_results] valid={len(valid)} rejected={len(rejected)} warnings={len(warnings_list)}")
    for m, why in rejected[:5]:
        print(f"  ✗ M{m.get('m', '?')}: {why}")
    for w in warnings_list[:5]:
        # A.6: warnings_list now mixes per-match status warnings ({m, status,
        # note}) with adapter-level typed warnings ({type, message, ...}) —
        # print defensively for both shapes.
        if "type" in w:
            print(f"  ⚠ {w['type']}: {str(w.get('message', ''))[:140]}")
        else:
            print(f"  ⚠ M{w.get('m', '?')}: {w.get('status', '?')} "
                  f"{('· ' + w['note']) if w.get('note') else ''}")

    out_path = LIVE / "results_2026.json"

    # Phase B3: optional events enrichment. Cache already-fetched events from
    # the existing results_2026.json so we only hit /fixtures/events for
    # matches that newly entered a LOCKED status this run.
    if args.with_events and src in ("api_football", "apifootball"):
        existing_events_by_m: dict[int, list[dict]] = {}
        if out_path.exists():
            try:
                existing = json.loads(out_path.read_text())
                for em in existing.get("completed_matches", []) or []:
                    if isinstance(em.get("events"), list) and em.get("m") is not None:
                        existing_events_by_m[int(em["m"])] = em["events"]
            except Exception as e:
                print(f"[fetch_results] events cache read failed: {e}")
        api_key = get_api_football_key()
        valid, ev_warnings = enrich_matches_with_events(
            valid, api_key, existing_events_by_m=existing_events_by_m,
        )
        warnings_list.extend(ev_warnings)

    if args.dry_run:
        print("[fetch_results] dry-run — no file written")
        return 0
    # Preserve existing locked data if provider returned nothing useful.
    # R5 C1: emit an explicit `provider_returned_nothing` warning into the
    # preserved file so the orchestrator's get_results_warnings() surfaces it
    # to live_state.json. Without this the silent-failure mode (provider
    # returns HTTP 200 + empty body with no parser-side warning — e.g.
    # auth token silently expired, or API serving cached empty response)
    # leaves results_2026.json untouched AND the dashboard unaware.
    if not valid and not warnings_list and out_path.exists():
        try:
            existing = json.loads(out_path.read_text())
            if existing.get("completed_matches"):
                print("[fetch_results] adapter returned nothing useful; preserving existing locked matches")
                # R6 M3: dedup the provider_returned_nothing warning across
                # consecutive preservation ticks. A 3h sustained provider
                # outage = ~18 fast ticks, each previously appending a
                # duplicate entry → warnings[] grew linearly and
                # results_2026.json bloated. Now: if the warning already
                # exists, bump a count + last_seen_utc instead of appending;
                # if not, append once with count=1 + first_seen_utc.
                now_iso = datetime.now(timezone.utc).isoformat()
                warnings = existing.setdefault("warnings", [])
                existing_warning = next(
                    (w for w in warnings
                     if isinstance(w, dict) and w.get("type") == "provider_returned_nothing"),
                    None,
                )
                if existing_warning is not None:
                    existing_warning["count"] = int(existing_warning.get("count", 1)) + 1
                    existing_warning["last_seen_utc"] = now_iso
                    # R7 N3: backfill first_seen_utc on any pre-R6 warning entry
                    # that was written before the dedup fields existed. Without
                    # this the duration of an in-progress outage that started on
                    # an old build would appear to start at the first post-deploy
                    # tick rather than at the actual onset.
                    existing_warning.setdefault("first_seen_utc", now_iso)
                else:
                    warnings.append({
                        "type": "provider_returned_nothing",
                        "message": (
                            f"Provider '{src}' returned 0 matches with no warnings; "
                            f"existing locked matches preserved. Investigate the "
                            f"adapter / provider token if this persists across ticks."
                        ),
                        "count": 1,
                        "first_seen_utc": now_iso,
                        "last_seen_utc": now_iso,
                    })
                existing["updated_at"] = now_iso
                existing["source"] = src
                atomic_write_json(out_path, existing)
                return 0
        except Exception:
            pass
    # Also: if provider returned fewer locked matches than we already have, refuse —
    # likely a partial fetch or auth issue, not an actual rollback.
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text())
            existing_n = len(existing.get("completed_matches", []))
            if src != "mock" and len(valid) < existing_n:
                print(f"[fetch_results] provider returned {len(valid)} locked matches but "
                      f"existing has {existing_n}; refusing to shrink (preserving existing)")
                # Still update warnings + updated_at so the orchestrator sees freshness
                existing["warnings"] = warnings_list
                existing["updated_at"] = datetime.now(timezone.utc).isoformat()
                existing["source"] = src
                atomic_write_json(out_path, existing)
                return 0
        except Exception:
            pass

    out = {
        "schema": "Completed WC 2026 matches — locked. Future matches are simulated.",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": src,
        "completed_matches": valid,
        "in_play": in_play,             # P1-G: surfaced via live_state.json
        "warnings": warnings_list,
    }
    try:
        atomic_write_json(out_path, out)
    except Exception as e:
        print(f"[fetch_results] FATAL: could not write {out_path} — {e}")
        return 1
    print(f"[fetch_results] wrote {out_path} ({len(valid)} matches locked, "
          f"{len(warnings_list)} warnings, source={src})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
