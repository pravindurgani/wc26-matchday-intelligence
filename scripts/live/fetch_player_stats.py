"""
fetch_player_stats.py — Phase 6 squad-stats fetcher (API-Football).

Pulls per-squad player rosters + trailing-window stats so auto_tier.py can
derive a baseline tier per player without any hand-typed list.

Output: data/live/player_stats_2026.json
Schema (auto_tier consumes via to_stats()):
  {
    "generated_at": ISO8601,
    "season":       2026,
    "ttl_seconds":  604800,           # 7-day weekly cache
    "teams": {
        "<canonical_team_name>": {
            "team_top_minutes": 1180,
            "players": [
                {"name": "Kylian Mbappé",
                 "minutes": 1180, "goals": 9, "assists": 4,
                 "appearances": 13, "clean_sheets": 0,
                 "position": "F"},
                ...
            ]
        },
        ...
    },
    "warnings": [...]
  }

CLI:
  python3 scripts/live/fetch_player_stats.py
  python3 scripts/live/fetch_player_stats.py --dry-run
  python3 scripts/live/fetch_player_stats.py --no-network --fixture FILE

Network behaviour matches fetch_injuries.py (fail-closed, empty teams on
any error). Live network wiring is intentionally minimal in this commit —
the auto-tier rollout runs in shadow mode (auto_tier_active: false) until
the disagreement diff against key_players_2026.json is reviewed.
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
OUT_PATH = LIVE / "player_stats_2026.json"

APIFOOTBALL_BASE = "https://v3.football.api-sports.io"

# Weekly TTL — stats only need refreshing as new internationals are played.
TTL_SECONDS = 7 * 24 * 3600

# Schema-drift watchdog: compares fresh /teams + /players responses to their
# captured baselines under data/live/_provider_schemas/. Soft-mode by default —
# drift logs a WARNING, does NOT crash the tick.
from scripts.live._schema_watchdog import assert_shape  # noqa: E402
_SCHEMA_BASELINE_DIR = ROOT / "data" / "live" / "_provider_schemas"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from auto_tier import PlayerStats  # noqa: E402
from injury_adjustments import normalize_player_name  # noqa: E402

try:
    from fetch_results import normalize_team  # noqa: E402
except Exception:
    def normalize_team(name: str) -> str:
        return (name or "").strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _wc_teams_set() -> set[str]:
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
        # R9 P3: allow_nan=False at producer boundary — apply_matchday
        # reads this file; pre-R9 only the matchday writer rejected NaN.
        json.dump(payload, tmp, indent=2, ensure_ascii=False, allow_nan=False)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def normalise_team_payload(team_name: str, raw_players: list[dict]) -> dict:
    """Reduce API-Football /players response to the auto_tier contract.

    Each `raw` entry is shaped: {"player": {...}, "statistics": [...]}.
    We sum across statistics (a player can have club + national records
    in the same year) — but ONLY the national-team line is what the auto
    tier system wants. The caller passes already-filtered records.
    """
    cleaned = []
    top_minutes = 0
    for rec in raw_players:
        p = (rec.get("player") or {})
        stats_block = (rec.get("statistics") or [{}])[0]
        games = stats_block.get("games") or {}
        goals = stats_block.get("goals") or {}
        minutes = int(games.get("minutes") or 0)
        # NOTE: API-Football v3 /players?team=&season= does NOT expose a
        # per-GK clean_sheets field in either `statistics[i]` or any
        # nested object. `goals.conceded` is populated for keepers but
        # there is no `appearances - games_with_conceded_goals` aggregate
        # in the payload. Audited 2026-06-16 against the full WC2026
        # snapshot: 250 GKs, every record clean_sheets == 0 regardless
        # of minutes played (E. Martínez 1459mins → 0; Suzuki 903mins → 0).
        # The previous expression `goals.get("conceded") and 0 or
        # stats_block.get("clean_sheets", 0)` looked like it might
        # silently suppress a real value but both branches resolved to 0
        # in practice (the fallback key isn't in the response either).
        # See auto_tier.py module docstring for the consumer-side note —
        # the GK tier branch is intentionally minutes-share-only.
        cleaned.append({
            "name": p.get("name") or "Unknown",
            "minutes": minutes,
            "goals": int(goals.get("total") or 0),
            "assists": int(goals.get("assists") or 0),
            "appearances": int(games.get("appearences") or
                               games.get("appearances") or 0),
            # Field preserved in schema for downstream contract stability
            # (auto_tier.PlayerStats has a clean_sheets slot). Always 0
            # for this provider — do NOT depend on it as a tier signal.
            "clean_sheets": 0,
            "position": (games.get("position") or "")[:1].upper() or None,
        })
        if minutes > top_minutes:
            top_minutes = minutes
    return {"team_top_minutes": top_minutes, "players": cleaned}


def _player_to_stats(p: dict, top_minutes: int) -> PlayerStats:
    return PlayerStats(
        minutes=int(p.get("minutes") or 0),
        team_top_minutes=top_minutes,
        goals=int(p.get("goals") or 0),
        assists=int(p.get("assists") or 0),
        appearances=int(p.get("appearances") or 0),
        clean_sheets=int(p.get("clean_sheets") or 0),
        position=p.get("position"),
    )


def to_stats(team_payload: dict, player_name: str) -> PlayerStats | None:
    """Lookup helper used by auto_tier integration.

    Whitelist queries arrive as the canonical full name ("Lionel Messi").
    API-Football emits the initial-dot-surname form ("L. Messi"). To
    avoid 80%+ false-negatives we resolve via the same normalisation
    layer used by classify_tier:

      1. Exact normalised full-name match.
      2. Unique surname match within the payload.
      3. Surname + first-initial match (handles "L. Messi" ↔ "Lionel
         Messi"). Ambiguous initial collisions (e.g. "L. Martínez" with
         both Lautaro and Lisandro present) return None — safer than
         picking the wrong player.
    """
    if not team_payload:
        return None
    top = int(team_payload.get("team_top_minutes") or 0)
    players = team_payload.get("players") or []
    if not players:
        return None
    target_norm = normalize_player_name(player_name)
    if not target_norm:
        return None
    target_tokens = target_norm.split()
    target_last = target_tokens[-1] if target_tokens else ""
    target_first_initial = target_tokens[0][0] if (
        len(target_tokens) >= 2 and target_tokens[0]) else ""

    by_full: dict[str, dict] = {}
    by_last: dict[str, list[dict]] = {}
    for p in players:
        norm = normalize_player_name(p.get("name"))
        if not norm:
            continue
        by_full.setdefault(norm, p)
        toks = norm.split()
        if toks:
            by_last.setdefault(toks[-1], []).append(p)

    # 1. Full-name match.
    if target_norm in by_full:
        return _player_to_stats(by_full[target_norm], top)

    # 2 & 3. Surname-based fallback.
    if target_last and target_last in by_last:
        candidates = by_last[target_last]
        if len(candidates) == 1:
            return _player_to_stats(candidates[0], top)
        if target_first_initial:
            initial_matches = []
            for cand in candidates:
                cand_norm = normalize_player_name(cand.get("name"))
                cand_tokens = cand_norm.split()
                if len(cand_tokens) < 2:
                    continue
                cand_first = cand_tokens[0]
                if cand_first and cand_first[0] == target_first_initial:
                    initial_matches.append(cand)
            if len(initial_matches) == 1:
                return _player_to_stats(initial_matches[0], top)
            # Ambiguous (≥2 matching the initial, e.g. Lautaro vs Lisandro
            # Martínez) — refuse to guess.
    return None


def _http_get_json(url: str, headers: dict, timeout: int = 20) -> dict:
    """Thin shim — delegates to `_http_client.http_get_json` so a 5xx /
    URLError / TimeoutError gets retried (3 attempts, exponential
    backoff) instead of escalating straight to subsystem_stale.
    Audit H3 (R2 round 3).
    """
    from _http_client import http_get_json  # noqa: PLC0415
    return http_get_json(url, headers, timeout=timeout)


def _resolve_league_season() -> tuple[str, str]:
    """League + season come from provider_fixture_map.json (preferred) or env.

    Mirrors fetch_injuries._resolve_league_season so both feeds query the same
    coordinates and any mismatch is operator-visible.
    """
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


def fetch_team_ids(api_key: str, league_id: str, season: str,
                   ) -> tuple[dict[str, int], list[dict]]:
    """One /teams call returns every WC2026 nation with its provider team_id.

    Cheaper than 48 /teams?search= calls and avoids ambiguous club vs. nation
    matches (the league filter pins us to international teams).
    """
    url = f"{APIFOOTBALL_BASE}/teams?league={league_id}&season={season}"
    headers = {"x-apisports-key": api_key, "Accept": "application/json"}
    print(f"[fetch_player_stats] GET {url}")
    try:
        payload = _http_get_json(url, headers)
        # Schema-drift watchdog: soft mode — logs a WARNING on shape drift but
        # never raises. Lets the /teams feed keep flowing while flagging the
        # operator that the provider changed something.
        assert_shape(payload,
                     _SCHEMA_BASELINE_DIR / "apifootball_teams.shape.json")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception as _body_err:
            body = f"<body unreadable: {type(_body_err).__name__}: {_body_err}>"
        return {}, [{"type": "http_error", "endpoint": "/teams",
                     "code": e.code, "body": body}]
    except Exception as e:
        return {}, [{"type": "fetch_error", "endpoint": "/teams",
                     "message": f"{type(e).__name__}: {e}"}]
    if payload.get("errors"):
        errs = payload.get("errors") or {}
        if any(errs.values()):
            return {}, [{"type": "api_error", "endpoint": "/teams",
                         "errors": errs}]
    out: dict[str, int] = {}
    for rec in payload.get("response") or []:
        team = (rec.get("team") or {})
        name = normalize_team(team.get("name") or "")
        tid = team.get("id")
        if name and isinstance(tid, int):
            out[name] = tid
    print(f"[fetch_player_stats] resolved {len(out)} team IDs")
    return out, []


def fetch_team_players(api_key: str, team_id: int, season: str,
                       rate_limiter=None,
                       ) -> tuple[list[dict], list[dict]]:
    """Paginated GET /players?team={id}&season={year}.

    API-Football returns `paging: {current, total}` — loop until exhausted.
    Empty `response` is NOT an error here: some smaller federations have no
    seasonal stats yet, especially mid-cycle.

    Audit H4 (R2 round 3): when `rate_limiter` is provided, `acquire()` is
    called BEFORE each HTTP request — both the first page AND every
    subsequent paginated request. With ~48 teams * 2-3 pages each, the
    producer was previously bursting at network speed; passing a shared
    `RateLimiter(0.15)` from `fetch_apifootball_player_stats` throttles the
    sweep to roughly 5-7 req/sec, well under API-Football's paid-tier
    300/min ceiling and recoverable on free-tier with degradation.
    """
    headers = {"x-apisports-key": api_key, "Accept": "application/json"}
    records: list[dict] = []
    warnings: list[dict] = []
    page = 1
    while True:
        url = (f"{APIFOOTBALL_BASE}/players?team={team_id}"
               f"&season={season}&page={page}")
        try:
            if rate_limiter is not None:
                rate_limiter.acquire()
            payload = _http_get_json(url, headers)
            # Schema-drift watchdog: soft mode — logs a WARNING on shape drift
            # but never raises. Lets the /players feed keep flowing while
            # flagging the operator that the provider changed something.
            assert_shape(payload,
                         _SCHEMA_BASELINE_DIR / "apifootball_players.shape.json")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                body = "<unreadable>"
            warnings.append({"type": "http_error", "endpoint": "/players",
                             "team_id": team_id, "code": e.code, "body": body})
            break
        except Exception as e:
            warnings.append({"type": "fetch_error", "endpoint": "/players",
                             "team_id": team_id,
                             "message": f"{type(e).__name__}: {e}"})
            break
        if payload.get("errors"):
            errs = payload.get("errors") or {}
            if any(errs.values()):
                warnings.append({"type": "api_error", "endpoint": "/players",
                                 "team_id": team_id, "errors": errs})
                break
        chunk = payload.get("response") or []
        records.extend(chunk)
        paging = payload.get("paging") or {}
        cur = int(paging.get("current") or page)
        tot = int(paging.get("total") or cur)
        if cur >= tot or not chunk:
            break
        page = cur + 1
    return records, warnings


def fetch_apifootball_player_stats(api_key: str, wc_teams: set[str],
                                   sleep_between: float = 0.15,
                                   ) -> tuple[dict[str, list[dict]], list[dict]]:
    """Fan-out across the 48 WC2026 squads. Returns ({team_name: raw_records}, warnings).

    Mirrors fetch_apifootball_injuries' contract — fail-closed, structured
    warnings, no exceptions escape.

    Audit H4 (R2 round 3): a single `RateLimiter(sleep_between)` is
    created here and shared across BOTH the per-team loop AND each team's
    pagination loop. Default `0.15s` ≈ 6.7 req/sec mirrors the polite
    spacer in `scripts/live/fetch_results.py:enrich_matches_with_events`
    (L406, L454-455). With 48 teams * 2-3 pages each the worst-case
    burst is now ~14-22 seconds of evenly-spaced calls instead of a
    100 req/sec spike that risks API-Football 429 cascades.

    Pass `sleep_between=0` to disable throttling (replay tests).
    """
    from _http_client import RateLimiter  # noqa: PLC0415
    league_id, season = _resolve_league_season()
    team_ids, warns = fetch_team_ids(api_key, league_id, season)
    out: dict[str, list[dict]] = {}
    if not team_ids:
        return out, warns
    missing = [t for t in wc_teams if t not in team_ids] if wc_teams else []
    if missing:
        warns.append({
            "type": "missing_team_ids",
            "count": len(missing),
            "teams": missing,
            "message": (f"{len(missing)} WC teams not resolved via "
                        f"/teams?league={league_id}&season={season}"),
        })
    targets = sorted((t for t in team_ids if not wc_teams or t in wc_teams))
    rate_limiter = RateLimiter(sleep_between) if sleep_between > 0 else None
    for team in targets:
        tid = team_ids[team]
        recs, team_warns = fetch_team_players(api_key, tid, season,
                                              rate_limiter=rate_limiter)
        out[team] = recs
        warns.extend(team_warns)
        print(f"[fetch_player_stats] {team:30s} team_id={tid} players={len(recs)}")
    return out, warns


def build_snapshot_from_local(fixture: dict) -> dict:
    """Replay path — `fixture` is a {team_name: [raw_records...]} dict
    matching the structure persisted by a future live fetch step.
    """
    teams = {}
    for team, raw in (fixture.get("teams") or {}).items():
        teams[team] = normalise_team_payload(team, raw)
    return {
        "generated_at": _now_iso(),
        "schema_version": 1,
        "source": fixture.get("source", "local_fixture"),
        "season": fixture.get("season", 2026),
        "ttl_seconds": TTL_SECONDS,
        "teams": teams,
        "warnings": fixture.get("warnings", []),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fetch WC2026 per-player stats from API-Football.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print summary; don't write player_stats_2026.json.")
    ap.add_argument("--no-network", action="store_true",
                    help="Skip network; use --fixture for local replay.")
    ap.add_argument("--fixture", type=Path, default=None,
                    help="Replay a saved per-team stats payload.")
    args = ap.parse_args()

    if args.fixture and args.fixture.exists():
        fixture = json.loads(args.fixture.read_text())
    elif args.no_network:
        # Empty fail-closed snapshot — auto_tier degrades to "no_data"
        # for every player, priority chain falls through to overrides
        # and then DEFAULT_TIER.
        fixture = {"teams": {}, "warnings": [
            {"type": "no_network_no_fixture",
             "message": "fetch_player_stats invoked with --no-network and "
                        "no --fixture; produced empty snapshot."},
        ]}
    else:
        api_key = (os.environ.get("API_FOOTBALL_KEY")
                   or os.environ.get("WC_APIFOOTBALL_KEY"))
        if not api_key:
            # Fail-closed mirror of fetch_injuries: no key → empty snapshot
            # with explicit warning. Auto_tier degrades to "no_data" for
            # every player; the override layer keeps doing its job.
            print("[fetch_player_stats] WARN: API_FOOTBALL_KEY not set — "
                  "writing empty snapshot")
            fixture = {"teams": {}, "warnings": [
                {"type": "missing_key",
                 "message": "API_FOOTBALL_KEY not in env"},
            ], "source": "api_football_no_key"}
        else:
            wc_teams = _wc_teams_set()
            raw_by_team, fetch_warns = fetch_apifootball_player_stats(
                api_key, wc_teams,
            )
            fixture = {
                "teams": raw_by_team,
                "warnings": fetch_warns,
                "source": "api_football",
            }

    snap = build_snapshot_from_local(fixture)
    print(f"[fetch_player_stats] {len(snap['teams'])} teams normalised")
    if args.dry_run:
        print(json.dumps(snap, indent=2, ensure_ascii=False)[:500] + "…")
        return 0
    _atomic_write_json(OUT_PATH, snap)
    print(f"[fetch_player_stats] wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
