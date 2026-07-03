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
)

# ── Round-label classifier ────────────────────────────────────────────────
# A.6 (2026-07-03): canonical definition moved to scripts/live/_knockout.py
# so fetch_results.py's knockout map auto-extension can classify rounds
# without importing this builder (which imports fetch_results — circular).
# Re-exported here for back-compat: tests/live/test_knockout_fixture_map.py
# and any operator tooling import it from this module.
from _knockout import classify_round  # noqa: E402, F401


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
    # ("Winner Group A", "Runner Up Group B", etc.) so we cannot map by
    # (home, away) name match the way we do for group fixtures. Instead we
    # pair by round + chronological order — both sides use the same FIFA
    # schedule, so position-in-round is a stable invariant.
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

    # ── Knockout: pair by phase + chronological order ────────────────────
    # Strategy: within each round, both internal schedule and provider feed
    # are ordered by FIFA's published calendar. Sort each side by (date,
    # tiebreaker) and pair index-for-index. This works pre-draw (placeholder
    # team names) because we ignore names entirely for knockouts.
    knockout_by_phase: dict[str, list[dict]] = {}
    for ks in knockout_schedule:
        knockout_by_phase.setdefault(ks["phase"], []).append(ks)

    for phase, internal_list in knockout_by_phase.items():
        provider_list = knockout_fixtures_by_phase.get(phase, [])
        if not provider_list:
            # No provider fixtures in this round yet — perfectly normal pre-draw.
            print(f"[builder] {phase}: no provider fixtures yet "
                  f"({len(internal_list)} internal awaiting upstream resolution)")
            continue
        # Stable sort on both sides. `m` is monotonic per FIFA's M-numbering;
        # provider `kickoff` ISO string sorts lexicographically.
        internal_sorted = sorted(internal_list, key=lambda x: (x["date"], x["m"]))
        provider_sorted = sorted(provider_list, key=lambda x: (x["date"], x["kickoff"], x["id"]))

        if len(provider_sorted) != len(internal_sorted):
            print(f"[builder] WARN {phase}: provider has {len(provider_sorted)} fixtures "
                  f"but bracket has {len(internal_sorted)}. Pairing the overlap.")

        for ix, internal in enumerate(internal_sorted):
            if ix >= len(provider_sorted):
                break  # provider hasn't published this slot yet
            f = provider_sorted[ix]
            mapped.append({
                "match_id": internal["m"],
                "provider_fixture_id": f["id"],
                # For knockouts pre-draw, home/away are placeholders — store
                # whatever the provider currently has so the fetcher's name
                # debug logs remain useful once teams resolve.
                "home": normalize_team(f["home_raw"]) or f["home_raw"] or "TBD",
                "away": normalize_team(f["away_raw"]) or f["away_raw"] or "TBD",
                "date": internal["date"],
                "phase": phase,
            })

        # Any leftover provider fixtures in this phase that didn't pair?
        for extra in provider_sorted[len(internal_sorted):]:
            unmapped_provider.append({
                "provider_fixture_id": extra["id"], "phase": phase,
                "date": extra["date"], "home": extra["home_raw"], "away": extra["away_raw"],
                "raw_home": extra["home_raw"], "raw_away": extra["away_raw"],
            })

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
