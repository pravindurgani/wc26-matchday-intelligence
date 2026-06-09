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
    get_api_football_key, APIFOOTBALL_BASE,
)


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="api_football",
                    choices=["api_football", "sportmonks"])
    ap.add_argument("--league-id", default=None)
    ap.add_argument("--season", default="2026")
    ap.add_argument("--write", action="store_true",
                    help="Write the map. Without this flag, dry-run only.")
    args = ap.parse_args()

    cfg = json.loads((RAW / "wc2026_config.json").read_text())
    schedule = cfg["group_stage_schedule"]
    if len(schedule) != 72:
        print(f"[builder] FATAL: expected 72 fixtures in wc2026_config, got {len(schedule)}")
        return 1

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

    league_id = args.league_id or "1"

    if args.provider == "api_football":
        key = get_api_football_key()
        if not key:
            print("[builder] API_FOOTBALL_KEY not set. Export it and re-run.")
            return 2
        try:
            fixtures = fetch_apifootball_fixtures(key, league_id, args.season)
        except Exception as e:
            print(f"[builder] provider fetch failed: {type(e).__name__}: {e}")
            return 1
    else:
        print(f"[builder] {args.provider} adapter not yet implemented")
        return 2

    print(f"[builder] provider returned {len(fixtures)} fixtures")

    mapped: list[dict] = []
    unmapped_provider: list[dict] = []
    for f in fixtures:
        fx = f.get("fixture") or {}
        teams = f.get("teams") or {}
        provider_id = str(fx.get("id", ""))
        home = normalize_team((teams.get("home") or {}).get("name", ""))
        away = normalize_team((teams.get("away") or {}).get("name", ""))
        date = (fx.get("date") or "")[:10]

        m_id = lookup(home, away, date)
        if m_id is None:
            unmapped_provider.append({
                "provider_fixture_id": provider_id,
                "date": date, "home": home, "away": away,
                "raw_home": (teams.get("home") or {}).get("name"),
                "raw_away": (teams.get("away") or {}).get("name"),
            })
            continue
        mapped.append({
            "match_id": m_id,
            "provider_fixture_id": provider_id,
            "home": home, "away": away, "date": date,
        })

    # Which of our 72 are NOT covered?
    mapped_internal = {x["match_id"] for x in mapped}
    unmapped_internal = [s for s in schedule if s["m"] not in mapped_internal]

    print(f"[builder] mapped: {len(mapped)} / 72 group fixtures")
    print(f"[builder] provider fixtures we couldn't map: {len(unmapped_provider)}")
    print(f"[builder] internal fixtures still unmapped: {len(unmapped_internal)}")

    for u in unmapped_provider[:5]:
        print(f"  ? provider: {u['date']} {u['home']} vs {u['away']} "
              f"(raw: {u['raw_home']!r} vs {u['raw_away']!r})")
    for s in unmapped_internal[:5]:
        print(f"  ? internal: M{s['m']} {s['date']} {s['home']} vs {s['away']}")

    if len(mapped) < 72:
        print(f"[builder] WARN: only mapped {len(mapped)}/72 group fixtures. "
              "Check team aliases in fetch_results.TEAM_ALIAS.")
    else:
        print(f"[builder] ✓ all 72 group fixtures mapped")

    if not args.write:
        print("[builder] dry-run — no file written. Re-run with --write to commit.")
        return 0

    out = {
        "provider": args.provider,
        "league_id": league_id,
        "season": args.season,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fixtures": sorted(mapped, key=lambda x: x["match_id"]),
        "unmapped_internal_count": len(unmapped_internal),
        "unmapped_provider_count": len(unmapped_provider),
    }
    out_path = LIVE / "provider_fixture_map.json"
    atomic_write_json(out_path, out)
    print(f"[builder] wrote {out_path}")
    if len(mapped) < 72:
        print(f"[builder] EXIT 1: only {len(mapped)}/72 mapped — fix aliases before relying on this map")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
