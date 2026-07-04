"""
build_provider_fixture_map.py — One-shot fixture-id mapper.

Pulls World Cup 2026 fixtures from the configured provider, matches each to
your internal match id (wc2026_config.group_stage_schedule) by date + home +
away (with the same TEAM_ALIAS normalisation used at fetch time), and writes
data/live/provider_fixture_map.json.

After running this once, fetch_results.py uses the map for O(1) provider-id →
internal-id lookups — no more fuzzy matching on every live tick.

Usage:
    # Dry-run (no file written, prints what would map):
    python3 scripts/live/build_provider_fixture_map.py --provider api_football

    # Write the map (requires API_FOOTBALL_KEY in env):
    python3 scripts/live/build_provider_fixture_map.py --provider api_football --write

    # Override league/season (default: league=1, season=2026):
    python3 scripts/live/build_provider_fixture_map.py --provider api_football \\
        --league-id 1 --season 2026 --write
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIVE = ROOT / "data" / "live"
RAW = ROOT / "data" / "raw"

# Reuse the adapter's normalisation + HTTP helper
sys.path.insert(0, str(Path(__file__).parent))
from fetch_results import (  # noqa: E402
    normalize_team, http_get_json, atomic_write_json,
    get_api_football_key, get_football_data_token,
    APIFOOTBALL_BASE, FOOTBALLDATA_BASE,
    _PHASE_TO_STAGE, _dates_within_one_day, _known_team_set,
    _resolved_bracket_rows,
)

# ── Round-label classifier ────────────────────────────────────────────────
# A.6 (2026-07-03): canonical definition moved to scripts/live/_knockout.py
# so fetch_results.py's knockout map auto-extension can classify rounds
# without importing this builder (which imports fetch_results — circular).
# Re-exported here for back-compat: tests/live/test_knockout_fixture_map.py
# and any operator tooling import it from this module.
from _knockout import classify_round  # noqa: E402, F401


# ── A.7 (2026-07-03, 16:31Z incident): NAME-FIRST knockout assignment ──────
# The original knockout strategy paired provider fixtures to internal ids
# POSITIONALLY (round + chronological order). That assumption is FALSE
# within a date: FIFA match numbering is not venue-local-kickoff order and
# provider dates/kickoffs are UTC, so on 2026-06-29 the builder re-keyed
# the already-name-correct R32 entries wrong (m74/75/76 rotated; m77/78,
# m81/82, m83/84 swapped) and real scores were ingested under the wrong
# internal ids. Knockout assignment is now:
#
#   1. NAME-FIRST — a provider fixture whose team names are real (both in
#      the WC26 team set after TEAM_ALIAS normalisation) is assigned by the
#      SAME criteria fetch_results' A.6 auto-extension uses: stage (via
#      classify_round) + unordered normalized team pair + date±1 against
#      the internal bracket with slots resolved from on-disk results
#      (export_ko_advance resolver via fetch_results._resolved_bracket_rows).
#      A real-named fixture that cannot be name-placed is REFUSED (left
#      unmapped + warned) — never guessed positionally.
#   2. MERGE — existing map entries are preserved unless fresh name
#      evidence supersedes or refutes them. A positional guess can never
#      blind-overwrite a previously name-matched entry.
#   3. POSITIONAL is reserved for genuinely-TBD fixtures (placeholder names
#      like "Winner Group A", empty, or not in the team set), and only when
#      unambiguous: the fixture must have exactly ONE unclaimed internal
#      candidate in its (stage, date±1) bucket AND be that candidate's only
#      suitor. Ambiguous buckets are refused (unmapped + warned) — mapping
#      a TBD fixture is a convenience (no result can exist for it yet), so
#      refusal costs nothing while a wrong guess re-keys real scores.
_MATCHED_BY_NAME = "name"
_MATCHED_BY_POSITION = "position"
_MATCHED_BY_PRESERVED = "preserved"


def assign_knockout_ids(
    provider_fixtures: list[dict],
    bracket_rows: list[dict],
    known_teams: set[str],
    existing_entries: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Assign internal match ids to knockout provider fixtures, name-first.

    `provider_fixtures`: [{"id", "home_raw", "away_raw", "date", "round"}]
    (raw provider names; group-stage rows are ignored via classify_round).
    `bracket_rows`: fetch_results._resolved_bracket_rows() rows — bracket
    schedule annotated with `resolved_home`/`resolved_away` (None while the
    feeding results are unknown).
    `existing_entries`: current map records for THIS provider (m>=73 only)
    to merge — never blind-overwritten by positional guesses.

    Returns (mapped_records, unmapped_provider). Each mapped record carries
    `matched_by` ∈ {"name", "position", "preserved"} for auditability.
    Deterministic: iteration order is (date, provider id) on the provider
    side and match_id on the merge side, independent of PYTHONHASHSEED.
    """
    from collections import Counter

    rows = [r for r in bracket_rows
            if isinstance(r, dict) and r.get("m") is not None]
    rows_by_m = {int(r["m"]): r for r in rows}
    by_stage_pair: dict[tuple[str, tuple[str, str]], dict] = {}
    for r in rows:
        h, a = r.get("resolved_home"), r.get("resolved_away")
        if h and a:
            by_stage_pair[(r.get("stage"), tuple(sorted((h, a))))] = r

    mapped: list[dict] = []
    unmapped: list[dict] = []
    claimed: set[int] = set()
    assigned_pfids: set[str] = set()

    def _unmapped_entry(f: dict, phase: str, reason: str) -> dict:
        # `f` is an internal `ko` dict (built below): names already
        # normalized under "home"/"away", originals under "raw_home"/…
        return {
            "provider_fixture_id": str(f["id"]), "phase": phase,
            "date": f.get("date"),
            "home": f.get("home") or "",
            "away": f.get("away") or "",
            "raw_home": f.get("raw_home"), "raw_away": f.get("raw_away"),
            "reason": reason,
        }

    # Classify + normalise once; deterministic provider-side order.
    ko: list[dict] = []
    for f in provider_fixtures:
        phase = classify_round(f.get("round"))
        if phase is None:
            continue
        ko.append({
            "id": str(f["id"]),
            "phase": phase,
            "stage": _PHASE_TO_STAGE.get(phase, phase),
            "home": normalize_team(f.get("home_raw") or ""),
            "away": normalize_team(f.get("away_raw") or ""),
            "raw_home": f.get("home_raw"), "raw_away": f.get("away_raw"),
            "date": f.get("date"),
        })
    ko.sort(key=lambda f: ((f.get("date") or ""), f["id"]))

    real_named = [f for f in ko
                  if bool(known_teams) and f["home"] and f["away"]
                  and f["home"] != f["away"]
                  and f["home"] in known_teams and f["away"] in known_teams]
    _real_ids = {f["id"] for f in real_named}
    tbd = [f for f in ko if f["id"] not in _real_ids]

    # ── Pass 1: name matches (authoritative) ────────────────────────────
    for f in real_named:
        pair = tuple(sorted((f["home"], f["away"])))
        row = by_stage_pair.get((f["stage"], pair))
        if (row is not None
                and _dates_within_one_day(f.get("date"), row.get("date"))
                and int(row["m"]) not in claimed):
            m = int(row["m"])
            claimed.add(m)
            assigned_pfids.add(f["id"])
            mapped.append({
                "match_id": m, "provider_fixture_id": f["id"],
                "home": f["home"], "away": f["away"],
                "date": row.get("date"), "phase": f["phase"],
                "matched_by": _MATCHED_BY_NAME,
            })
        else:
            # Real names that cannot be name-placed: our bracket resolution
            # is incomplete or disagrees with the provider. REFUSE — a
            # positional guess here is exactly the 16:31Z incident.
            unmapped.append(_unmapped_entry(
                f, f["phase"],
                "name_unplaceable (bracket slot unresolved, pairing "
                "disagreement, or duplicate) — refusing positional guess"))

    unplaceable_pfids = {u["provider_fixture_id"] for u in unmapped}

    # ── Pass 2: merge existing entries (never blind-overwrite) ──────────
    for e in sorted(existing_entries or [],
                    key=lambda x: (int(x.get("match_id") or x.get("m") or 0),
                                   str(x.get("provider_fixture_id") or ""))):
        if not isinstance(e, dict):
            continue
        pfid = str(e.get("provider_fixture_id") or "")
        try:
            m = int(e.get("match_id") or e.get("m") or 0)
        except (TypeError, ValueError):
            continue
        if not pfid or m < 73:
            continue
        if pfid in assigned_pfids:
            continue  # fresh name evidence supersedes the old entry
        if pfid in unplaceable_pfids:
            continue  # payload has real names that refute/can't confirm it
        if m in claimed or m not in rows_by_m:
            continue  # id claimed by name evidence (or bogus) — entry loses
        claimed.add(m)
        assigned_pfids.add(pfid)
        mapped.append({
            "match_id": m, "provider_fixture_id": pfid,
            "home": e.get("home") or "TBD", "away": e.get("away") or "TBD",
            "date": e.get("date") or rows_by_m[m].get("date"),
            "phase": e.get("phase") or "unknown",
            "matched_by": e.get("matched_by") or _MATCHED_BY_PRESERVED,
        })

    # ── Pass 3: positional — genuinely-TBD fixtures, unambiguous only ───
    pending = [f for f in tbd if f["id"] not in assigned_pfids]
    cands: dict[str, list[int]] = {}
    for f in pending:
        exact: list[int] = []
        near: list[int] = []
        fd = (f.get("date") or "")[:10]
        for r in rows:
            if r.get("stage") != f["stage"] or int(r["m"]) in claimed:
                continue
            rd = (r.get("date") or "")[:10]
            if fd and rd and fd == rd:
                exact.append(int(r["m"]))
            elif _dates_within_one_day(fd, rd):
                near.append(int(r["m"]))
        # Exact-date candidates outrank ±1 rollover neighbours.
        cands[f["id"]] = sorted(exact) if exact else sorted(near)
    wanted = Counter(m for lst in cands.values() for m in lst)
    for f in pending:
        lst = cands[f["id"]]
        if len(lst) == 1 and wanted[lst[0]] == 1 and lst[0] not in claimed:
            m = lst[0]
            row = rows_by_m[m]
            claimed.add(m)
            assigned_pfids.add(f["id"])
            mapped.append({
                "match_id": m, "provider_fixture_id": f["id"],
                "home": f["home"] or f.get("raw_home") or "TBD",
                "away": f["away"] or f.get("raw_away") or "TBD",
                "date": row.get("date"), "phase": f["phase"],
                "matched_by": _MATCHED_BY_POSITION,
            })
        else:
            unmapped.append(_unmapped_entry(
                f, f["phase"],
                ("ambiguous_positional_bucket "
                 f"({len(lst)} candidate(s), contested={bool(lst) and wanted[lst[0]] > 1}) "
                 "— refusing to guess; will name-match once teams resolve")))

    mapped.sort(key=lambda x: x["match_id"])
    return mapped, unmapped


def fetch_apifootball_fixtures(api_key: str, league_id: str, season: str) -> list[dict]:
    headers = {"x-apisports-key": api_key, "Accept": "application/json"}
    url = f"{APIFOOTBALL_BASE}/fixtures?league={league_id}&season={season}"
    print(f"[builder] GET {url}")
    payload = http_get_json(url, headers)
    if payload.get("errors"):
        print(f"[builder] provider returned errors: {payload['errors']}")
        if any(payload["errors"].values()):
            return []
    return payload.get("response", []) or []


def fetch_football_data_fixtures(token: str, competition: str = "WC") -> list[dict]:
    """Returns football-data.org matches in their native shape (caller maps to internal)."""
    import urllib.error
    headers = {"X-Auth-Token": token, "Accept": "application/json"}
    url = f"{FOOTBALLDATA_BASE}/competitions/{competition}/matches"
    print(f"[builder] GET {url}")
    try:
        payload = http_get_json(url, headers)
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception as _body_err: body = f"<body unreadable: {type(_body_err).__name__}: {_body_err}>"
        print(f"[builder] football-data.org HTTP {e.code}: {body}")
        if e.code == 400:
            print(f"[builder] HTTP 400 usually means: invalid token format, or the WC2026")
            print(f"[builder] competition isn't yet available in football-data.org's index.")
            print(f"[builder] Verify the token at https://api.football-data.org/v4/competitions")
            print(f"[builder] (curl -s -H 'X-Auth-Token: <YOUR_TOKEN>' that URL).")
        elif e.code in (401, 403):
            print(f"[builder] HTTP {e.code}: token is wrong/unauthorised. Double-check you")
            print(f"[builder] got the token from https://www.football-data.org/client/register")
            print(f"[builder] (NOT the API-Football key — they're different services).")
        elif e.code == 429:
            print(f"[builder] HTTP 429: rate-limited. Free tier is 10 req/min. Wait 60s.")
        raise
    return payload.get("matches", []) or []


def check_football_data_token(token: str) -> bool:
    """Hit /v4/competitions with the token. Returns True if 200 OK + WC is listed."""
    import urllib.error
    headers = {"X-Auth-Token": token, "Accept": "application/json"}
    url = f"{FOOTBALLDATA_BASE}/competitions"
    print(f"[builder] Verifying token via GET {url}")
    try:
        payload = http_get_json(url, headers)
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception as _body_err: body = f"<body unreadable: {type(_body_err).__name__}: {_body_err}>"
        print(f"[builder] token check failed: HTTP {e.code} {body}")
        return False
    comps = payload.get("competitions") or []
    print(f"[builder] token is valid — {len(comps)} competitions visible")
    wc = [c for c in comps if c.get("code") == "WC" or (c.get("name") or "").startswith("FIFA World Cup")]
    if wc:
        print(f"[builder] ✓ FIFA World Cup IS available to this token "
              f"(code={wc[0].get('code')}, name={wc[0].get('name')}, "
              f"plan-tier={wc[0].get('plan', '?')})")
        return True
    else:
        print(f"[builder] ✗ FIFA World Cup is NOT in the competitions list for this token.")
        print(f"[builder]   Free tier should include it. Check your plan at "
              f"https://www.football-data.org/account.")
        sample = [c.get("code") for c in comps][:10]
        print(f"[builder]   Sample of available competitions: {sample}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="api_football",
                    choices=["api_football", "football_data", "sportmonks"])
    ap.add_argument("--league-id", default=None)
    ap.add_argument("--season", default="2026")
    ap.add_argument("--write", action="store_true",
                    help="Write the map. Without this flag, dry-run only.")
    ap.add_argument("--allow-partial", action="store_true",
                    help="Force-write even if fewer than 72 group fixtures mapped. "
                         "Use only if you know what you're doing.")
    ap.add_argument("--min-mapped", type=int, default=72,
                    help="Minimum mapped fixtures required to write (default: 72).")
    ap.add_argument("--check-token", action="store_true",
                    help="Only verify the provider token works (no map write). "
                         "Use this first when troubleshooting auth.")
    args = ap.parse_args()

    # --check-token shortcut
    if args.check_token:
        if args.provider == "football_data":
            token = get_football_data_token()
            if not token:
                print("[builder] FOOTBALL_DATA_TOKEN not set in env. Export it and re-run.")
                return 2
            return 0 if check_football_data_token(token) else 1
        elif args.provider == "api_football":
            key = get_api_football_key()
            if not key:
                print("[builder] API_FOOTBALL_KEY not set in env. Export it and re-run.")
                return 2
            try:
                payload = http_get_json(
                    f"{APIFOOTBALL_BASE}/status",
                    {"x-apisports-key": key, "Accept": "application/json"},
                )
                resp = payload.get("response") or {}
                acct = resp.get("account") or {}
                sub = resp.get("subscription") or {}
                print(f"[builder] API-Football token OK — account: "
                      f"{acct.get('firstname','?')} {acct.get('lastname','?')}, "
                      f"plan: {sub.get('plan','?')}, "
                      f"requests today: {(resp.get('requests') or {}).get('current','?')}/"
                      f"{(resp.get('requests') or {}).get('limit_day','?')}")
                if sub.get("plan", "").lower() == "free":
                    print(f"[builder] ⚠ FREE plan — 2026 WC fixtures will return empty. Upgrade to Pro/Ultra.")
                return 0
            except Exception as e:
                print(f"[builder] API-Football token check failed: {type(e).__name__}: {e}")
                return 1
        else:
            print(f"[builder] --check-token not implemented for {args.provider}")
            return 2

    cfg = json.loads((RAW / "wc2026_config.json").read_text())
    schedule = cfg["group_stage_schedule"]
    if len(schedule) != 72:
        print(f"[builder] FATAL: expected 72 group fixtures in wc2026_config, got {len(schedule)}")
        return 1

    # Knockout bracket — empirically confirmed schema (probe A.0): API-Football
    # exposes all 32 knockout fixtures with `league.round` labels like
    # "Round of 16" / "Quarter-finals" / "Semi-finals" / "3rd Place Final" /
    # "Final". Pre-draw, the team-name fields hold placeholder strings
    # ("Winner Group A", "Runner Up Group B", etc.).
    # A.7 NOTE: "position-in-round is a stable invariant" was the pre-incident
    # assumption and it is FALSE — FIFA match numbering within a date is not
    # UTC-kickoff order (2026-07-03 16:31Z incident). Assignment is now
    # name-first via assign_knockout_ids; this schedule list only feeds the
    # coverage accounting below.
    bracket_path = RAW / "knockout_bracket_2026.json"
    knockout_schedule: list[dict] = []
    if bracket_path.exists():
        bracket = json.loads(bracket_path.read_text())
        # WC2026 phase → list of internal matches in canonical order
        for s in bracket.get("r32_slots", []):
            knockout_schedule.append({"m": s["match_num"], "date": s["date"], "phase": "r32"})
        for s in bracket.get("r16_bracket", []):
            knockout_schedule.append({"m": s["match_num"], "date": s["date"], "phase": "r16"})
        for s in bracket.get("qf_bracket", []):
            knockout_schedule.append({"m": s["match_num"], "date": s["date"], "phase": "qf"})
        for s in bracket.get("sf_bracket", []):
            knockout_schedule.append({"m": s["match_num"], "date": s["date"], "phase": "sf"})
        ft = bracket.get("final_and_third_place") or {}
        if "third_place" in ft:
            tp = ft["third_place"]
            knockout_schedule.append({"m": tp["match_num"], "date": tp["date"], "phase": "third_place"})
        if "final" in ft:
            fn = ft["final"]
            knockout_schedule.append({"m": fn["match_num"], "date": fn["date"], "phase": "final"})
        print(f"[builder] knockout bracket loaded: {len(knockout_schedule)} fixtures (M73-M{72 + len(knockout_schedule)})")
    else:
        print(f"[builder] no knockout bracket at {bracket_path} — group-stage only")

    # Build a (home, away) -> [(date, match_id), ...] index so we can match
    # tolerantly across the UTC↔local-date boundary (NA evening matches roll
    # over to the next UTC day).
    from datetime import date as _date, timedelta as _td
    by_teams: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for s in schedule:
        by_teams.setdefault((s["home"], s["away"]), []).append((s["date"], s["m"]))

    def lookup(home: str, away: str, provider_date: str) -> int | None:
        candidates = by_teams.get((home, away), [])
        if not candidates:
            return None
        # Prefer the candidate whose date is closest to the provider date (within 1 day)
        try:
            target = _date.fromisoformat(provider_date)
        except Exception:
            return candidates[0][1]  # fall back to first
        best = None
        best_gap = 999
        for sched_date, m_id in candidates:
            try:
                sd = _date.fromisoformat(sched_date)
                gap = abs((sd - target).days)
                if gap <= 1 and gap < best_gap:
                    best, best_gap = m_id, gap
            except Exception:
                continue
        return best


    # classify_round is at module scope (above) for testability.

    league_id = args.league_id or "1"

    if args.provider == "api_football":
        key = get_api_football_key()
        if not key:
            print("[builder] API_FOOTBALL_KEY not set. Export it and re-run.")
            return 2
        try:
            fixtures_raw = fetch_apifootball_fixtures(key, league_id, args.season)
        except Exception as e:
            print(f"[builder] provider fetch failed: {type(e).__name__}: {e}")
            return 1
        # Normalise to a common shape: {id, home, away, date, round, kickoff}
        # We keep `round` + `kickoff` for knockout pairing — group fixtures
        # ignore them, knockout fixtures need them to find their slot.
        fixtures = [{
            "id": str((f.get("fixture") or {}).get("id", "")),
            "home_raw": ((f.get("teams") or {}).get("home") or {}).get("name", ""),
            "away_raw": ((f.get("teams") or {}).get("away") or {}).get("name", ""),
            "date": ((f.get("fixture") or {}).get("date") or "")[:10],
            "kickoff": (f.get("fixture") or {}).get("date") or "",
            "round": (f.get("league") or {}).get("round", ""),
        } for f in fixtures_raw]
    elif args.provider == "football_data":
        token = get_football_data_token()
        if not token:
            print("[builder] FOOTBALL_DATA_TOKEN not set. Export it and re-run.")
            return 2
        competition = os.environ.get("FOOTBALL_DATA_COMPETITION") or "WC"
        try:
            fixtures_raw = fetch_football_data_fixtures(token, competition)
        except Exception as e:
            print(f"[builder] provider fetch failed: {type(e).__name__}: {e}")
            return 1
        # football-data.org exposes `stage` (LAST_16, QUARTER_FINALS, etc.) —
        # translate to the same round-label shape API-Football uses so the
        # classifier handles both providers uniformly.
        FD_STAGE_TO_ROUND = {
            "GROUP_STAGE": "Group Stage",
            "LAST_32": "Round of 32",
            "LAST_16": "Round of 16",
            "QUARTER_FINALS": "Quarter-finals",
            "SEMI_FINALS": "Semi-finals",
            "THIRD_PLACE": "3rd Place Final",
            "FINAL": "Final",
        }
        fixtures = [{
            "id": str(m.get("id", "")),
            "home_raw": (m.get("homeTeam") or {}).get("name", ""),
            "away_raw": (m.get("awayTeam") or {}).get("name", ""),
            "date": (m.get("utcDate") or "")[:10],
            "kickoff": m.get("utcDate") or "",
            "round": FD_STAGE_TO_ROUND.get(m.get("stage") or "", m.get("stage") or ""),
        } for m in fixtures_raw]
    else:
        print(f"[builder] {args.provider} adapter not yet implemented")
        return 2

    print(f"[builder] provider returned {len(fixtures)} fixtures")

    # Split provider fixtures by phase so we can use different matching
    # strategies for group (name-based) vs knockout (round + ordered pairing).
    group_fixtures: list[dict] = []
    knockout_fixtures_by_phase: dict[str, list[dict]] = {}
    for f in fixtures:
        phase = classify_round(f.get("round"))
        if phase is None:
            group_fixtures.append(f)
        else:
            knockout_fixtures_by_phase.setdefault(phase, []).append(f)

    print(f"[builder] provider fixtures split: "
          f"{len(group_fixtures)} group-stage, "
          f"{sum(len(v) for v in knockout_fixtures_by_phase.values())} knockout "
          f"(across phases: {sorted(knockout_fixtures_by_phase.keys())})")

    mapped: list[dict] = []
    unmapped_provider: list[dict] = []

    # ── Group stage: name-based lookup (unchanged from v1) ───────────────
    for f in group_fixtures:
        provider_id = f["id"]
        home = normalize_team(f["home_raw"])
        away = normalize_team(f["away_raw"])
        date = f["date"]
        m_id = lookup(home, away, date)
        if m_id is None:
            unmapped_provider.append({
                "provider_fixture_id": provider_id, "phase": "group",
                "date": date, "home": home, "away": away,
                "raw_home": f["home_raw"], "raw_away": f["away_raw"],
            })
            continue
        mapped.append({
            "match_id": m_id, "provider_fixture_id": provider_id,
            "home": home, "away": away, "date": date, "phase": "group",
        })

    # ── Knockout: NAME-FIRST assignment (A.7 — replaces positional) ──────
    # The pre-A.7 strategy paired provider fixtures to internal ids by
    # round + chronological order. FIFA match numbering within a date is
    # NOT UTC-kickoff order, so on 2026-07-03 a bootstrap rebuild re-keyed
    # the already-name-correct R32 map wrong (m74/75/76 rotated; m77/78,
    # m81/82, m83/84 swapped) and real scores landed under wrong internal
    # ids. See assign_knockout_ids for the full strategy: name-first,
    # merge-preserving, positional ONLY for unambiguous TBD fixtures.
    ko_provider_fixtures = [f for lst in knockout_fixtures_by_phase.values()
                            for f in lst]
    existing_ko_entries: list[dict] = []
    map_path = LIVE / "provider_fixture_map.json"
    if map_path.exists():
        try:
            _doc = json.loads(map_path.read_text())
            if (_doc.get("provider") or args.provider) == args.provider:
                existing_ko_entries = [
                    e for e in _doc.get("fixtures", [])
                    if isinstance(e, dict)
                    and int(e.get("match_id") or e.get("m") or 0) >= 73
                ]
            else:
                print(f"[builder] existing map is for provider "
                      f"{_doc.get('provider')!r} — not merging its KO entries")
        except Exception as e:
            print(f"[builder] WARN: existing map unreadable ({e}) — "
                  f"no KO entries to merge")
    bracket_rows = _resolved_bracket_rows()
    ko_mapped, ko_unmapped = assign_knockout_ids(
        ko_provider_fixtures, bracket_rows, _known_team_set(),
        existing_ko_entries)
    mapped.extend(ko_mapped)
    unmapped_provider.extend(ko_unmapped)

    by_method: dict[str, int] = {}
    for x in ko_mapped:
        by_method[x.get("matched_by", "?")] = \
            by_method.get(x.get("matched_by", "?"), 0) + 1
    print(f"[builder] knockout assignment: {len(ko_mapped)} mapped "
          f"({', '.join(f'{k}={v}' for k, v in sorted(by_method.items())) or 'none'}), "
          f"{len(ko_unmapped)} refused/unmapped")
    for u in ko_unmapped[:6]:
        print(f"  ✋ {u.get('phase', '?')}: {u.get('date')} "
              f"{u.get('raw_home')!r} vs {u.get('raw_away')!r} — "
              f"{u.get('reason', '?')}")

    # ── Coverage reporting ───────────────────────────────────────────────
    mapped_internal = {x["match_id"] for x in mapped}
    expected_match_ids = {s["m"] for s in schedule} | {ks["m"] for ks in knockout_schedule}
    unmapped_internal = [
        {"m": m, "date": "?"} for m in sorted(expected_match_ids - mapped_internal)
    ]

    mapped_group = sum(1 for x in mapped if x.get("phase") == "group")
    mapped_knockout = sum(1 for x in mapped if x.get("phase") != "group")
    expected_total = 72 + len(knockout_schedule)
    print(f"[builder] mapped: {len(mapped)} / {expected_total}  "
          f"(group: {mapped_group}/72, knockout: {mapped_knockout}/{len(knockout_schedule)})")
    print(f"[builder] provider fixtures we couldn't map: {len(unmapped_provider)}")
    print(f"[builder] internal fixtures still unmapped: {len(unmapped_internal)}")

    for u in unmapped_provider[:5]:
        print(f"  ? provider {u.get('phase', '?')}: {u['date']} "
              f"{u.get('home', '?')} vs {u.get('away', '?')} (raw: {u['raw_home']!r} vs {u['raw_away']!r})")
    for s in unmapped_internal[:5]:
        print(f"  ? internal: M{s['m']}")

    if mapped_group < 72:
        print(f"[builder] WARN: only {mapped_group}/72 group fixtures mapped. "
              "Check team aliases in fetch_results.TEAM_ALIAS.")
    else:
        print(f"[builder] ✓ all 72 group fixtures mapped")
    if knockout_schedule:
        if mapped_knockout == len(knockout_schedule):
            print(f"[builder] ✓ all {len(knockout_schedule)} knockout fixtures mapped")
        elif mapped_knockout > 0:
            print(f"[builder] ⏳ {mapped_knockout}/{len(knockout_schedule)} knockouts mapped "
                  f"(provider populates the rest as the bracket resolves)")

    if not args.write:
        print("[builder] dry-run — no file written. Re-run with --write to commit.")
        return 0

    # The `--min-mapped` floor protects against writing a useless map.
    # Default 72 = group-stage-only writes are fine (pre-tournament + first
    # half of tournament). Once the knockout bracket resolves, the next
    # rebuild will pick up M73-M104 and the file grows automatically.
    # If group-stage mapping fails for some reason (alias drift, provider
    # outage), refuse to overwrite — partial maps cause silent drops.
    if mapped_group < args.min_mapped and not args.allow_partial:
        print(f"\n[builder] REFUSING TO WRITE: only {mapped_group}/{args.min_mapped} group fixtures mapped.")
        print("[builder] A partial map is worse than no map — the fetcher's fuzzy fallback")
        print("[builder]   handles missing IDs gracefully, but a stub map with 0 entries")
        print("[builder]   will be treated as authoritative and cause every fixture to be unmapped.")
        print("[builder]")
        print("[builder] Likely causes:")
        print("[builder]   • API-Football FREE plan blocks current/future seasons (2022–2024 only).")
        print("[builder]     → Upgrade to Pro/Ultra, OR switch to provider=football_data (free WC coverage).")
        print("[builder]   • Wrong league_id — pass --league-id <N> after checking your provider's WC league.")
        print("[builder]   • Wrong season — pass --season 2026 (default).")
        print("[builder]   • Provider uses a team name not in TEAM_ALIAS — add it to scripts/live/fetch_results.py.")
        print("[builder]")
        print(f"[builder] To force-write the partial map anyway: --allow-partial")
        return 1

    out = {
        "provider": args.provider,
        "league_id": league_id,
        "season": args.season,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fixtures": sorted(mapped, key=lambda x: x["match_id"]),
        # Audit fields — useful for the workflow's "should I rebuild?" check.
        "coverage": {
            "group_mapped": mapped_group,
            "group_total": 72,
            "knockout_mapped": mapped_knockout,
            "knockout_total": len(knockout_schedule),
            "total_mapped": len(mapped),
            "total_expected": 72 + len(knockout_schedule),
        },
        "unmapped_internal_count": len(unmapped_internal),
        "unmapped_provider_count": len(unmapped_provider),
    }
    out_path = LIVE / "provider_fixture_map.json"
    atomic_write_json(out_path, out)
    print(f"[builder] wrote {out_path}")
    if mapped_group < 72:
        print(f"[builder] WARN: only {mapped_group}/72 group mapped — fetcher will fuzzy-match the rest")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
