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

# Schema-drift watchdog: compares fresh /injuries responses to the captured
# baseline under data/live/_provider_schemas/. Soft-mode by default — drift
# logs a WARNING, does NOT crash the tick.
from scripts.live._schema_watchdog import assert_shape  # noqa: E402
_SCHEMA_BASELINE_DIR = ROOT / "data" / "live" / "_provider_schemas"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from injury_adjustments import (  # noqa: E402
    AUTO_TIER_ACTIVE, classify_api_type, classify_tier,
    classify_tier_with_overrides, classify_tier_with_replacement,
    discounted_elo, net_injury_elo, normalize_player_name, DEFAULT_TIER,
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
    """Thin shim — delegates to `_http_client.http_get_json` so a 5xx /
    URLError / TimeoutError gets retried (3 attempts, exponential
    backoff) instead of escalating straight to subsystem_stale.
    Audit H3 (R2 round 3).
    """
    from _http_client import http_get_json  # noqa: PLC0415 — late import for
    # test isolation: monkeypatching `_http_client.http_get_json` from a test
    # is cleaner than monkeypatching the captured reference.
    return http_get_json(url, headers, timeout=timeout)


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
        # Schema-drift watchdog: soft mode — logs a WARNING on shape drift but
        # never raises. Lets the injuries feed keep flowing while flagging the
        # operator that the provider changed something.
        assert_shape(payload,
                     _SCHEMA_BASELINE_DIR / "apifootball_injuries.shape.json")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception as _body_err:
            body = f"<body unreadable: {type(_body_err).__name__}: {_body_err}>"
        return [], [{"type": "http_error", "code": e.code, "body": body}]
    except Exception as e:
        return [], [{"type": "fetch_error", "message": f"{type(e).__name__}: {e}"}]
    if payload.get("errors"):
        errs = payload.get("errors") or {}
        if any(errs.values()):
            return [], [{"type": "api_error", "errors": errs}]
    response = payload.get("response") or []
    print(f"[fetch_injuries] {len(response)} injury records returned")
    if not response:
        # The call succeeded — no missing_key, no http_error, no api_error —
        # but the API returned zero records. Two distinct realities live here:
        #   (a) genuinely quiet day (no team has a reportable injury), or
        #   (b) misconfigured league_id / season hitting an empty endpoint.
        # Without a sentinel both look identical on disk (teams: {},
        # warnings: []), and an operator looking at the dashboard can't
        # tell whether the feed is alive or wedged. Emit a low-severity
        # info warning that carries the endpoint coords so post-hoc
        # forensics has the league/season actually queried.
        return response, [{
            "type": "no_records_returned",
            "endpoint": "/injuries",
            "league": league_id,
            "season": season,
            "message": (
                f"API returned 0 injury records for league={league_id} "
                f"season={season} — could be a quiet day, or the league/"
                f"season pair is wrong (check provider_fixture_map.json)."
            ),
        }]
    return response, []


def _load_player_stats_snapshot() -> dict:
    """Best-effort load of data/live/player_stats_2026.json.

    Missing or malformed → empty dict (auto_tier degrades to auto_no_data
    per-player; the override layer keeps doing its job).
    """
    path = LIVE / "player_stats_2026.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def normalise_records(records: list[dict], wc_teams: set[str],
                       player_stats_snap: dict | None = None,
                       auto_tier_active: bool = AUTO_TIER_ACTIVE,
                       ) -> tuple[dict, list[dict]]:
    """Group API records → {team: {total_elo_adjustment, players}}.

    Returns (teams_dict, warnings).
    """
    # Fix #2 (Wave-B R4): empty/None wc_teams previously bypassed the
    # whitelist filter (`if wc_teams and team not in wc_teams`) and admitted
    # every team. A misconfigured _wc_teams_set() returning set() then let
    # qualifier carry-over records leak through with full Elo penalties.
    # Fail-closed: an empty whitelist is a configuration error, not a
    # signal to admit everyone.
    if not wc_teams:
        raise ValueError(
            "normalise_records requires a non-empty wc_teams set "
            "(empty/None would silently admit every team)"
        )
    warnings: list[dict] = []
    teams: dict[str, dict] = {}
    skipped_non_wc = 0
    skipped_bad = 0
    case_mismatch_cases: list[dict] = []
    duplicate_cases: list[dict] = []
    ambiguous_cases: list[dict] = []
    stats_teams = (player_stats_snap or {}).get("teams") or {}
    # Fix #3 (Wave-B R4): build a case-folded view of the canonical set so
    # we can detect provider casing drift ('france' vs 'France') and surface
    # a dedicated `case_mismatch` warning instead of silently dropping the
    # record as non-WC.
    wc_teams_cf = {t.casefold(): t for t in wc_teams}
    # Fix #4 (Wave-B R4): dedup duplicate (team, normalised-player) rows so
    # a provider double-emission (or fixture-scoped + season-scoped pair)
    # doesn't stack penalties (e.g. -30 + -15 = -45 for Mbappé). Keep the
    # FIRST occurrence — sufficient for the per-snapshot horizon — and
    # surface a `duplicate_record` warning for operator visibility.
    seen: set[tuple[str, str]] = set()
    for rec in records:
        team_raw = (rec.get("team") or {}).get("name", "")
        team = normalize_team(team_raw)
        if not team:
            skipped_bad += 1
            continue
        # Filter to WC2026 teams only — the league=1 endpoint sometimes returns
        # records for teams not in the active tournament (qualifier carry-over).
        if team not in wc_teams:
            # Fix #3: case-mismatch detection — if the case-folded form
            # matches a canonical team, surface a warning instead of
            # silently dropping. The record is still skipped (we don't
            # auto-canonicalise here; the operator should fix the upstream
            # alias map so the canonical form arrives intact).
            canonical_match = wc_teams_cf.get(team.casefold())
            if canonical_match:
                case_mismatch_cases.append({
                    "input": team,
                    "canonical": canonical_match,
                })
                continue
            skipped_non_wc += 1
            continue
        # Fix #1 (Wave-B R4): a record with NO `player` key previously
        # coerced into a fake 'Unknown' player at DEFAULT_TIER (-12 Elo).
        # Drop these records and surface a skipped_bad_record warning;
        # the canonical fields (name/type) are required to emit any
        # tier-driven penalty at all.
        player_block_raw = rec.get("player")
        if not isinstance(player_block_raw, dict) or not player_block_raw:
            skipped_bad += 1
            continue
        player_block = player_block_raw
        name = player_block.get("name") or "Unknown"
        ptype = player_block.get("type")
        reason = player_block.get("reason")
        # Fix #4: dedup on (team, normalised player name). Skip the rest
        # of this iteration if we've already booked a penalty for this
        # player in this snapshot.
        dedup_key = (team, normalize_player_name(name))
        if dedup_key in seen:
            duplicate_cases.append({
                "team": team,
                "input": name,
                "fixture_id": (rec.get("fixture") or {}).get("id"),
            })
            continue
        seen.add(dedup_key)
        status = classify_api_type(ptype)
        # v2: cross-reference the player + team against the hand-curated
        # whitelist at data/raw/key_players_2026.json. Headline names get
        # auto-upgraded to tier_1_star / tier_1_keeper; everyone else falls
        # through to DEFAULT_TIER (= tier_2_starter), matching v1 behaviour.
        # The `auto_tier_source` audit field records which path produced
        # the tier so post-hoc reviews can spot mismatches.
        tier, tier_source, replacement_elo = classify_tier_with_replacement(
            name, team
        )
        # Phase 6 (shadow): also compute the priority-chain answer to
        # surface the auto-tier suggestion + components per player. When
        # auto_tier_active is False the override / DEFAULT_TIER still
        # drives `tier` above; the auto suggestion is informational only.
        team_stats_payload = stats_teams.get(team)
        chain_tier, chain_source, chain_components = classify_tier_with_overrides(
            name, team,
            player_stats_payload=team_stats_payload,
            auto_tier_active=auto_tier_active,
        )
        if auto_tier_active:
            tier = chain_tier
            tier_source = chain_source
            # Override path keeps the replacement_elo computed above (the
            # whitelist entry carries an explicit replacement block).
            # Any auto_* source has NO replacement data, so net_injury_elo
            # must collapse to raw elo — force replacement_elo to None.
            if chain_source.startswith("auto_"):
                replacement_elo = None
        elo = discounted_elo(tier, status)
        net_elo = net_injury_elo(elo, replacement_elo)
        fixture_id = (rec.get("fixture") or {}).get("id")
        teams.setdefault(team, {
            "total_elo_adjustment": 0.0,
            "total_net_elo_adjustment": 0.0,
            "players": [],
        })
        teams[team]["players"].append({
            "name": name,
            "tier": tier,
            "auto_tier_source": tier_source,
            "status": status,
            "reason": reason,
            "elo": round(elo, 3),
            "replacement_elo": (round(replacement_elo, 3)
                                if replacement_elo is not None else None),
            "net_elo": round(net_elo, 3),
            "fixture_id": fixture_id,
            "auto_tier_suggestion": chain_components.get("auto_tier"),
            "auto_tier_components": chain_components.get("auto_components"),
        })
        teams[team]["total_elo_adjustment"] = round(
            teams[team]["total_elo_adjustment"] + elo, 3
        )
        teams[team]["total_net_elo_adjustment"] = round(
            teams[team]["total_net_elo_adjustment"] + net_elo, 3
        )
        # Surface every ambiguity — the operator can disambiguate via the
        # manual overlay (team_adjustments.json). Without this, an
        # Emiliano Martínez reported as "Dibu Martinez" silently routes
        # to DEFAULT_TIER (-12) instead of tier_1_keeper (-25) and the
        # operator has no signal to act on.
        if tier_source == "whitelist_ambiguous":
            ambiguous_cases.append({"team": team, "input": name,
                                    "fixture_id": fixture_id})
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
            "message": (
                f"Skipped {skipped_bad} records missing team name or "
                f"player block"
            ),
        })
    if case_mismatch_cases:
        # Fix #3 (Wave-B R4): a record arrived with the right team in the
        # wrong case (e.g. 'france' vs 'France'). Surface as a discrete
        # warning so the operator can fix the upstream alias map; the
        # record is dropped (not auto-canonicalised) because casing drift
        # frequently signals a separate provider feed with its own quirks
        # that warrant explicit triage.
        samples = ", ".join(
            f"{c['input']!r}→{c['canonical']!r}"
            for c in case_mismatch_cases[:3]
        )
        more = (f" (+{len(case_mismatch_cases) - 3} more)"
                if len(case_mismatch_cases) > 3 else "")
        warnings.append({
            "type": "team_case_mismatch",
            "count": len(case_mismatch_cases),
            "cases": case_mismatch_cases,
            "message": (
                f"{len(case_mismatch_cases)} record(s) had a case-mismatched "
                f"team name and were dropped: {samples}{more}"
            ),
        })
    if duplicate_cases:
        # Fix #4 (Wave-B R4): provider double-emission stacked penalties
        # (-30 + -15 = -45 for Mbappé). Now the first occurrence wins and
        # we surface the dropped duplicates here.
        samples = ", ".join(
            f"{c['team']}/{c['input']!r}" for c in duplicate_cases[:3]
        )
        more = (f" (+{len(duplicate_cases) - 3} more)"
                if len(duplicate_cases) > 3 else "")
        warnings.append({
            "type": "duplicate_record",
            "count": len(duplicate_cases),
            "cases": duplicate_cases,
            "message": (
                f"{len(duplicate_cases)} duplicate (team, player) record(s) "
                f"dropped to avoid double-counting: {samples}{more}"
            ),
        })
    if ambiguous_cases:
        # One aggregate warning carries every case so the dashboard
        # surfaces a single actionable item per snapshot (not N separate
        # alerts). `cases` is preserved in full for the operator audit log.
        samples = ", ".join(
            f"{c['team']}/{c['input']!r}" for c in ambiguous_cases[:3]
        )
        more = f" (+{len(ambiguous_cases) - 3} more)" if len(ambiguous_cases) > 3 else ""
        warnings.append({
            "type": "ambiguous_classification",
            "count": len(ambiguous_cases),
            "cases": ambiguous_cases,
            "message": (
                f"{len(ambiguous_cases)} ambiguous classification(s) "
                f"defaulted to tier_2_starter — disambiguate in "
                f"team_adjustments.json: {samples}{more}"
            ),
        })
    return teams, warnings


def _load_local_fixture(path: Path) -> list[dict]:
    """Replay a saved API response from disk (test/dev only)."""
    payload = json.loads(path.read_text())
    return payload.get("response") or []


def build_snapshot(records: list[dict], fetch_warnings: list[dict]) -> dict:
    league_id, season = _resolve_league_season()
    wc_teams = _wc_teams_set()
    stats_snap = _load_player_stats_snapshot()
    # Fix #2 (Wave-B R4): normalise_records now raises on an empty wc_teams
    # set. If the config file is missing/unreadable we surface a dedicated
    # warning and return an empty snapshot rather than crash the live-update
    # orchestrator (the legacy manual overlay still applies on top).
    if not wc_teams:
        return {
            "generated_at": _now_iso(),
            "schema_version": 1,
            "source": "api_football",
            "league_id": int(league_id) if str(league_id).isdigit() else league_id,
            "season": int(season) if str(season).isdigit() else season,
            "net_elo_active": True,
            "auto_tier_active": bool(AUTO_TIER_ACTIVE),
            "teams_with_injuries": 0,
            "teams": {},
            "warnings": fetch_warnings + [{
                "type": "missing_wc_teams_config",
                "message": (
                    "wc2026_config.json missing/empty — cannot filter "
                    "injury records; emitted empty snapshot"
                ),
            }],
        }
    teams, norm_warnings = normalise_records(
        records, wc_teams,
        player_stats_snap=stats_snap,
        auto_tier_active=AUTO_TIER_ACTIVE,
    )
    return {
        "generated_at": _now_iso(),
        "schema_version": 1,
        "source": "api_football",
        "league_id": int(league_id) if str(league_id).isdigit() else league_id,
        "season": int(season) if str(season).isdigit() else season,
        # Phase 1B (CORRECTIONS.md §1): replacement-aware net Elo is now
        # carried per-player as `net_elo` and per-team as
        # `total_net_elo_adjustment`. The raw `elo` / `total_elo_adjustment`
        # fields are preserved for backward compatibility with existing
        # readers; downstream consumers branch on this flag to decide which
        # value to apply.
        "net_elo_active": True,
        # Phase 6 (CORRECTIONS.md §7): rollout flag — when False, the
        # override / DEFAULT_TIER answer drives `tier` and only auto-tier
        # SUGGESTIONS are attached per-player for the disagreement-diff
        # CLI to consume.
        "auto_tier_active": bool(AUTO_TIER_ACTIVE),
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
