"""
fetch_injuries.py — Stream B.3 injuries fetcher (API-Football).

Calls API-Football v3 /injuries?league={id}&season={year}, normalises each
player to (team, status, tier, elo), and writes data/live/injuries_2026.json.

Schema (consumed by apply_matchday_adjustments._load_injury_components):
  {
    "generated_at": ISO8601,
    "source":       "api_football",
    "league_id":    1,
    "season":       2026,
    "teams": {
        "<canonical_team_name>": {
            "total_elo_adjustment": -25.0,
            "players": [
              {"name": "...", "tier": "tier_2_starter", "status": "confirmed_out",
               "reason": "Injury - knee", "elo": -12.0, "fixture_id": 12345},
              ...
            ]
        },
        ...
    },
    "warnings": [...]
  }

CLI:
  python3 scripts/live/fetch_injuries.py            # live fetch
  python3 scripts/live/fetch_injuries.py --dry-run  # don't write
  python3 scripts/live/fetch_injuries.py --no-network --fixture FILE  # local replay

Designed to fail-closed: any API error writes an empty `teams: {}` snapshot
with a warning rather than crash the live-update orchestrator. The legacy
manual overlay in team_adjustments.json is consumed by apply_matchday_adjustments
on TOP of this file, so an empty API snapshot doesn't erase operator edits.

References:
  - API-Football /injuries docs: https://www.api-football.com/documentation-v3#tag/Injuries
  - Tier taxonomy: scripts/live/injury_adjustments.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIVE = ROOT / "data" / "live"
RAW = ROOT / "data" / "raw"
OUT_PATH = LIVE / "injuries_2026.json"

APIFOOTBALL_BASE = "https://v3.football.api-sports.io"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from injury_adjustments import (  # noqa: E402
    classify_api_type, discounted_elo, DEFAULT_TIER,
)

# Reuse the same canonical-name aliases as the results fetcher so a single
# team appears under one name across feeds. Imported lazily so this module
# stays importable even if fetch_results.py is refactored.
try:
    from fetch_results import normalize_team, TEAM_ALIAS  # noqa: F401
except Exception:
    def normalize_team(name: str) -> str:
        return (name or "").strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _wc_teams_set() -> set[str]:
    """Load the 48 canonical WC2026 team names from the config."""
    cfg_path = RAW / "wc2026_config.json"
    if not cfg_path.exists():
        return set()
    cfg = json.loads(cfg_path.read_text())
    teams: set[str] = set()
    for group_teams in cfg.get("groups", {}).values():
        teams.update(group_teams)
    return teams


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent),
        prefix=path.name + ".", suffix=".tmp", delete=False,
    ) as tmp:
        json.dump(payload, tmp, indent=2, ensure_ascii=False)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def _http_get_json(url: str, headers: dict, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _resolve_league_season() -> tuple[str, str]:
    """League + season come from provider_fixture_map.json (preferred) or env."""
    league_id = os.environ.get("API_FOOTBALL_LEAGUE_ID")
    season = os.environ.get("API_FOOTBALL_SEASON")
    fix_map = LIVE / "provider_fixture_map.json"
    if fix_map.exists():
        try:
            mf = json.loads(fix_map.read_text())
            league_id = league_id or mf.get("league_id")
            season = season or mf.get("season")
        except Exception:
            pass
    return str(league_id or "1"), str(season or "2026")


def fetch_apifootball_injuries(api_key: str) -> tuple[list[dict], list[dict]]:
    """Fetch the raw response list from API-Football. Returns (response, warnings)."""
    league_id, season = _resolve_league_season()
    url = f"{APIFOOTBALL_BASE}/injuries?league={league_id}&season={season}"
    headers = {"x-apisports-key": api_key, "Accept": "application/json"}
    print(f"[fetch_injuries] GET {url}")
    try:
        payload = _http_get_json(url, headers)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")[:200]
        except Exception:
            pass
        return [], [{"type": "http_error", "code": e.code, "body": body}]
    except Exception as e:
        return [], [{"type": "fetch_error", "message": f"{type(e).__name__}: {e}"}]
    if payload.get("errors"):
        errs = payload.get("errors") or {}
        if any(errs.values()):
            return [], [{"type": "api_error", "errors": errs}]
    response = payload.get("response") or []
    print(f"[fetch_injuries] {len(response)} injury records returned")
    return response, []


def normalise_records(records: list[dict], wc_teams: set[str]) -> tuple[dict, list[dict]]:
    """Group API records → {team: {total_elo_adjustment, players}}.

    Returns (teams_dict, warnings).
    """
    warnings: list[dict] = []
    teams: dict[str, dict] = {}
    skipped_non_wc = 0
    skipped_bad = 0
    for rec in records:
        team_raw = (rec.get("team") or {}).get("name", "")
        team = normalize_team(team_raw)
        if not team:
            skipped_bad += 1
            continue
        # Filter to WC2026 teams only — the league=1 endpoint sometimes returns
        # records for teams not in the active tournament (qualifier carry-over).
        if wc_teams and team not in wc_teams:
            skipped_non_wc += 1
            continue
        player_block = rec.get("player") or {}
        name = player_block.get("name") or "Unknown"
        ptype = player_block.get("type")
        reason = player_block.get("reason")
        status = classify_api_type(ptype)
        tier = DEFAULT_TIER  # v1: API doesn't expose importance; conservative default
        elo = discounted_elo(tier, status)
        fixture_id = (rec.get("fixture") or {}).get("id")
        teams.setdefault(team, {"total_elo_adjustment": 0.0, "players": []})
        teams[team]["players"].append({
            "name": name,
            "tier": tier,
            "status": status,
            "reason": reason,
            "elo": round(elo, 3),
            "fixture_id": fixture_id,
        })
        teams[team]["total_elo_adjustment"] = round(
            teams[team]["total_elo_adjustment"] + elo, 3
        )
    if skipped_non_wc:
        warnings.append({
            "type": "filter_non_wc",
            "count": skipped_non_wc,
            "message": f"Skipped {skipped_non_wc} records for teams not in WC2026",
        })
    if skipped_bad:
        warnings.append({
            "type": "skipped_bad_record",
            "count": skipped_bad,
            "message": f"Skipped {skipped_bad} records missing team name",
        })
    return teams, warnings


def _load_local_fixture(path: Path) -> list[dict]:
    """Replay a saved API response from disk (test/dev only)."""
    payload = json.loads(path.read_text())
    return payload.get("response") or []


def build_snapshot(records: list[dict], fetch_warnings: list[dict]) -> dict:
    league_id, season = _resolve_league_season()
    wc_teams = _wc_teams_set()
    teams, norm_warnings = normalise_records(records, wc_teams)
    return {
        "generated_at": _now_iso(),
        "schema_version": 1,
        "source": "api_football",
        "league_id": int(league_id) if str(league_id).isdigit() else league_id,
        "season": int(season) if str(season).isdigit() else season,
        "teams_with_injuries": len(teams),
        "teams": teams,
        "warnings": fetch_warnings + norm_warnings,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch WC2026 injuries from API-Football.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print summary; don't write injuries_2026.json.")
    ap.add_argument("--no-network", action="store_true",
                    help="Skip network call; combine with --fixture for local replay.")
    ap.add_argument("--fixture", type=Path, default=None,
                    help="Replay a saved API response JSON from disk.")
    args = ap.parse_args()

    if args.no_network:
        if not args.fixture or not args.fixture.exists():
            print("[fetch_injuries] --no-network requires --fixture FILE", file=sys.stderr)
            return 2
        records = _load_local_fixture(args.fixture)
        fetch_warnings: list[dict] = []
    else:
        api_key = (os.environ.get("API_FOOTBALL_KEY")
                   or os.environ.get("WC_APIFOOTBALL_KEY"))
        if not api_key:
            print("[fetch_injuries] WARN: API_FOOTBALL_KEY not set — writing empty snapshot")
            records = []
            fetch_warnings = [{"type": "missing_key",
                               "message": "API_FOOTBALL_KEY not in env"}]
        else:
            records, fetch_warnings = fetch_apifootball_injuries(api_key)

    snapshot = build_snapshot(records, fetch_warnings)
    print(f"[fetch_injuries] teams with injuries: {snapshot['teams_with_injuries']}")
    if snapshot["warnings"]:
        print(f"[fetch_injuries] warnings: {len(snapshot['warnings'])}")
        for w in snapshot["warnings"][:3]:
            print(f"  - {w}")
    if args.dry_run:
        print(f"[fetch_injuries] dry-run — would write {OUT_PATH.relative_to(ROOT)}")
        return 0
    _atomic_write_json(OUT_PATH, snapshot)
    print(f"[fetch_injuries] wrote {OUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
