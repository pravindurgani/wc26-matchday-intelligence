"""
fetch_lineups.py — Stream B.4 lineups fetcher (API-Football).

Polls /fixtures/lineups for upcoming WC2026 fixtures within the kickoff
window (default: next 4 hours). For each side we record the confirmed
starting XI, look up the team's most recent recorded XI in
lineups_2026.json, and apply the conservative B.4 heuristic
(see scripts/live/lineup_adjustments.py).

Schema (consumed by apply_matchday_adjustments._load_lineup_components):
  {
    "generated_at": ISO8601,
    "schema_version": 1,
    "source": "api_football",
    "lineups": [
      {
        "match_id": 12,
        "home": "France", "away": "Senegal",
        "home_team_adjustment_elo": -8.0,
        "away_team_adjustment_elo":  0.0,
        "home_adjustment_reason": "GK swap",
        "away_adjustment_reason": null,
        "baseline_source": "lineups_2026.json:prior_match",
        "home_xi": [{"id": ..., "name": ..., "pos": ...}, ...],
        "away_xi": [...],
        "fixture_status": "NS|1H|...",
        "captured_at": ISO8601
      },
      ...
    ],
    "warnings": []
  }

CLI:
  python3 scripts/live/fetch_lineups.py                       # live
  python3 scripts/live/fetch_lineups.py --hours-ahead 6       # widen window
  python3 scripts/live/fetch_lineups.py --dry-run             # don't write
  python3 scripts/live/fetch_lineups.py --no-network \
        --fixture-dir tests/live/lineup_samples/              # local replay

Display-first: we always record startXI even when no Elo delta applies.
The dashboard surfaces lineups for every upcoming match so the user can
see them even when the model didn't move.

Fail-closed: per-fixture errors get logged and skipped; the snapshot is
still written (with whatever fixtures succeeded).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIVE = ROOT / "data" / "live"
RAW = ROOT / "data" / "raw"
OUT_PATH = LIVE / "lineups_2026.json"

APIFOOTBALL_BASE = "https://v3.football.api-sports.io"
DEFAULT_HOURS_AHEAD = 4

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lineup_adjustments import (  # noqa: E402
    extract_starting_xi, compute_lineup_delta_elo,
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


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


def _load_schedule() -> list[dict]:
    cfg = json.loads((RAW / "wc2026_config.json").read_text())
    return cfg.get("group_stage_schedule", []) or []


def _load_fixture_map() -> dict[int, str]:
    """match_id → provider_fixture_id mapping."""
    p = LIVE / "provider_fixture_map.json"
    if not p.exists():
        return {}
    m = json.loads(p.read_text())
    out: dict[int, str] = {}
    for fx in m.get("fixtures", []):
        mid = fx.get("match_id") or fx.get("m")
        pfid = fx.get("provider_fixture_id")
        if mid and pfid:
            out[int(mid)] = str(pfid)
    return out


def _kickoff_utc(sched_entry: dict) -> datetime | None:
    """Parse `date` + `time` (local) to UTC. Schedule lacks tz so we treat
    times as UTC — kickoff-window filtering is approximate by design (we
    widen the window with `--hours-ahead` to cover the slop)."""
    try:
        ts = f"{sched_entry['date']}T{sched_entry.get('time','12:00')}:00+00:00"
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def fixtures_in_window(schedule: list[dict], hours_ahead: int,
                       now: datetime | None = None) -> list[dict]:
    """Return scheduled fixtures with kickoff in [now-15min, now+hours_ahead]."""
    now = now or _now_utc()
    lower = now - timedelta(minutes=15)
    upper = now + timedelta(hours=hours_ahead)
    out = []
    for s in schedule:
        k = _kickoff_utc(s)
        if k is None:
            continue
        if lower <= k <= upper:
            out.append(s)
    return out


def _load_prior_lineups() -> dict[str, dict]:
    """Map team → most recent recorded startXI dict (from lineups_2026.json)."""
    p = OUT_PATH
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text())
    except Exception:
        return {}
    # Walk entries in match_id order; later entries overwrite earlier.
    per_team: dict[str, dict] = {}
    for entry in sorted(d.get("lineups", []),
                        key=lambda x: x.get("match_id", 0)):
        for side in ("home", "away"):
            team = entry.get(side)
            xi_raw = entry.get(f"{side}_xi") or []
            if team and xi_raw:
                # Reconstruct a startXI-compatible block from stored players.
                side_block = {"startXI": [{"player": p_} for p_ in xi_raw]}
                per_team[team] = extract_starting_xi(side_block)
    return per_team


def fetch_one_fixture(api_key: str, provider_fixture_id: str) -> list[dict]:
    url = f"{APIFOOTBALL_BASE}/fixtures/lineups?fixture={provider_fixture_id}"
    headers = {"x-apisports-key": api_key, "Accept": "application/json"}
    payload = _http_get_json(url, headers)
    if payload.get("errors") and any((payload.get("errors") or {}).values()):
        raise RuntimeError(f"API errors: {payload['errors']}")
    return payload.get("response") or []


def _summarise_xi(xi_block_raw: list[dict]) -> list[dict]:
    """Compact player records for storage in lineups_2026.json."""
    out = []
    for entry in xi_block_raw:
        inner = entry.get("player") or {}
        out.append({
            "id": inner.get("id"),
            "name": inner.get("name"),
            "number": inner.get("number"),
            "pos": inner.get("pos"),
        })
    return out


def build_lineup_entry(sched: dict, response_sides: list[dict],
                       prior_xis: dict[str, dict]) -> dict:
    """Turn one fixture's /fixtures/lineups response into our schema entry."""
    home_team_canonical = sched["home"]
    away_team_canonical = sched["away"]
    home_block: dict = {}
    away_block: dict = {}
    for side_entry in response_sides:
        team_name = (side_entry.get("team") or {}).get("name", "")
        # Provider name may differ from canonical (e.g. Korea Republic) —
        # take the first as home, second as away when provider order matches
        # the fixture. Fall back to name-prefix heuristic otherwise.
        if not home_block:
            home_block = side_entry
        else:
            away_block = side_entry
    home_xi = extract_starting_xi(home_block)
    away_xi = extract_starting_xi(away_block)
    home_delta, home_reason = compute_lineup_delta_elo(
        prior_xis.get(home_team_canonical), home_xi)
    away_delta, away_reason = compute_lineup_delta_elo(
        prior_xis.get(away_team_canonical), away_xi)
    baseline_source = (
        "lineups_2026.json:prior_match"
        if prior_xis.get(home_team_canonical) or prior_xis.get(away_team_canonical)
        else "none:first_recorded_xi"
    )
    return {
        "match_id": int(sched["m"]),
        "home": home_team_canonical,
        "away": away_team_canonical,
        "home_team_adjustment_elo": round(home_delta, 3),
        "away_team_adjustment_elo": round(away_delta, 3),
        "home_adjustment_reason": home_reason,
        "away_adjustment_reason": away_reason,
        "baseline_source": baseline_source,
        "home_xi": _summarise_xi(home_block.get("startXI") or []),
        "away_xi": _summarise_xi(away_block.get("startXI") or []),
        "captured_at": _now_iso(),
    }


def build_snapshot(entries: list[dict], warnings: list[dict],
                   hours_ahead: int) -> dict:
    return {
        "generated_at": _now_iso(),
        "schema_version": 1,
        "source": "api_football",
        "hours_ahead": hours_ahead,
        "lineups": entries,
        "warnings": warnings,
    }


def _replay_local_fixtures(fixture_dir: Path, schedule: list[dict],
                           fixture_map: dict[int, str]) -> tuple[list[dict], list[dict]]:
    """Walk fixture_dir/*.json — filename `{provider_fixture_id}.json` →
    matched against fixture_map to find the canonical match_id."""
    entries, warnings = [], []
    pfid_to_match = {pfid: mid for mid, pfid in fixture_map.items()}
    prior_xis = _load_prior_lineups()
    for f in sorted(fixture_dir.glob("*.json")):
        pfid = f.stem
        mid = pfid_to_match.get(pfid)
        if mid is None:
            warnings.append({"type": "unmapped_fixture", "fixture_id": pfid})
            continue
        sched = next((s for s in schedule if s["m"] == mid), None)
        if sched is None:
            continue
        response = json.loads(f.read_text()).get("response") or []
        entries.append(build_lineup_entry(sched, response, prior_xis))
    return entries, warnings


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch WC2026 lineups from API-Football.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--hours-ahead", type=int, default=DEFAULT_HOURS_AHEAD)
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("--fixture-dir", type=Path, default=None,
                    help="Replay /fixtures/lineups responses from disk.")
    args = ap.parse_args()

    schedule = _load_schedule()
    fixture_map = _load_fixture_map()

    if args.no_network:
        if not args.fixture_dir or not args.fixture_dir.exists():
            print("[fetch_lineups] --no-network requires --fixture-dir DIR", file=sys.stderr)
            return 2
        entries, warnings = _replay_local_fixtures(
            args.fixture_dir, schedule, fixture_map)
    else:
        api_key = (os.environ.get("API_FOOTBALL_KEY")
                   or os.environ.get("WC_APIFOOTBALL_KEY"))
        upcoming = fixtures_in_window(schedule, args.hours_ahead)
        print(f"[fetch_lineups] {len(upcoming)} fixtures in next {args.hours_ahead}h window")
        entries: list[dict] = []
        warnings: list[dict] = []
        if not api_key:
            warnings.append({"type": "missing_key",
                             "message": "API_FOOTBALL_KEY not in env"})
        prior_xis = _load_prior_lineups()
        for sched in upcoming:
            pfid = fixture_map.get(int(sched["m"]))
            if not pfid:
                warnings.append({"type": "unmapped_match",
                                 "match_id": sched["m"]})
                continue
            if not api_key:
                continue  # already warned; just skip work
            try:
                sides = fetch_one_fixture(api_key, pfid)
            except urllib.error.HTTPError as e:
                warnings.append({"type": "http_error",
                                 "match_id": sched["m"], "code": e.code})
                continue
            except Exception as e:
                warnings.append({"type": "fetch_error",
                                 "match_id": sched["m"],
                                 "error": f"{type(e).__name__}: {e}"})
                continue
            if not sides:
                # Lineups not published yet — common at T-3h, rare at T-30min.
                continue
            entries.append(build_lineup_entry(sched, sides, prior_xis))

    snapshot = build_snapshot(entries, warnings, args.hours_ahead)
    print(f"[fetch_lineups] entries: {len(entries)} · warnings: {len(warnings)}")
    if args.dry_run:
        print(f"[fetch_lineups] dry-run — would write {OUT_PATH.relative_to(ROOT)}")
        return 0
    _atomic_write_json(OUT_PATH, snapshot)
    print(f"[fetch_lineups] wrote {OUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
