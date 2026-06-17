"""
fetch_match_stats.py — Stream B.5 post-match stats fetcher.

For every FT match in data/live/results_2026.json, calls
API-Football /fixtures/statistics?fixture={id}, computes the v1 stats
PROXY form delta (NOT xG — see scripts/live/stats_proxy_adjustments.py)
for each side, and writes data/live/match_stats_2026.json.

Schema (consumed by apply_matchday_adjustments._load_stats_components):
  {
    "generated_at": ISO8601,
    "schema_version": 1,
    "source": "api_football",
    "matches": [
      {
        "match_id": 1,
        "status": "FT",
        "home": "Mexico", "away": "South Africa",
        "home_form_adjustment_elo":  4.6,
        "away_form_adjustment_elo": -4.6,
        "true_xg_available": false,         <-- always false, by spec
        "home_stats": {"Shots on Goal": 6, "Ball Possession": 58, ...},
        "away_stats": {...},
        "fixture_id": "1489369",
        "captured_at": ISO8601
      },
      ...
    ],
    "warnings": []
  }

CLI:
  python3 scripts/live/fetch_match_stats.py                       # live
  python3 scripts/live/fetch_match_stats.py --dry-run             # no write
  python3 scripts/live/fetch_match_stats.py --no-network \
        --fixture-dir tests/live/match_stat_samples/              # replay
  python3 scripts/live/fetch_match_stats.py --only-match 1

Idempotent: re-running re-fetches all FT matches; cheap because the
endpoint returns immediately for a completed fixture. The slow workflow
(B.8) runs this on a 3h cadence — no need for finer granularity since
the stats never change after FT.
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
OUT_PATH = LIVE / "match_stats_2026.json"

APIFOOTBALL_BASE = "https://v3.football.api-sports.io"

# Schema-drift watchdog: compares fresh /fixtures/statistics responses to the
# captured baseline under data/live/_provider_schemas/. Soft-mode by default —
# drift logs a WARNING, does NOT crash the tick.
from scripts.live._schema_watchdog import assert_shape  # noqa: E402
_SCHEMA_BASELINE_DIR = ROOT / "data" / "live" / "_provider_schemas"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stats_proxy_adjustments import (  # noqa: E402
    stats_to_dict, compute_form_delta, compute_xg_form_delta,
)

# Real-xG path is dead by default. Flipping to True ALSO requires updating
# the pre_flight.py:628-629 assertion that enforces true_xg_available=False.
XG_ENABLED = False


def _xg_value(side_stats_raw: list[dict]):
    """API-Football exposes 'Expected Goals' only on Pro-tier plans. Pulled
    from the raw stats array because stats_to_dict() coerces to int."""
    for entry in side_stats_raw or []:
        if (entry.get("type") or "").strip() == "Expected Goals":
            v = entry.get("value")
            if v is None:
                return None
            try:
                return float(str(v).rstrip("%"))
            except (TypeError, ValueError):
                return None
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    from _http_client import http_get_json  # noqa: PLC0415
    return http_get_json(url, headers, timeout=timeout)


def load_ft_matches(only: int | None = None) -> list[dict]:
    """Return completed matches from results_2026.json (status == FT/AET/PEN)."""
    p = LIVE / "results_2026.json"
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text())
    except Exception:
        return []
    out = []
    for m in d.get("completed_matches", []) or []:
        if (m.get("status") or "").upper() not in {"FT", "AET", "PEN"}:
            continue
        if only is not None and int(m.get("m", 0)) != only:
            continue
        out.append(m)
    return out


def _load_fixture_map() -> dict[int, str]:
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


def fetch_one_fixture(api_key: str, provider_fixture_id: str) -> list[dict]:
    """/fixtures/statistics returns one entry per team for the fixture."""
    url = f"{APIFOOTBALL_BASE}/fixtures/statistics?fixture={provider_fixture_id}"
    headers = {"x-apisports-key": api_key, "Accept": "application/json"}
    payload = _http_get_json(url, headers)
    # Schema-drift watchdog: soft mode — logs a WARNING on shape drift but
    # never raises. Lets the stats feed keep flowing while flagging the
    # operator that the provider changed something.
    assert_shape(payload,
                 _SCHEMA_BASELINE_DIR / "apifootball_fixtures_statistics.shape.json")
    if payload.get("errors") and any((payload.get("errors") or {}).values()):
        raise RuntimeError(f"API errors: {payload['errors']}")
    return payload.get("response") or []


def build_match_entry(match: dict, response_sides: list[dict],
                      fixture_id: str | None) -> dict:
    """Compose one entry. `response_sides` is the /fixtures/statistics array
    (one item per team). Order isn't guaranteed — match by team name."""
    home_team = match["home"]
    away_team = match["away"]
    home_stats_raw: list[dict] = []
    away_stats_raw: list[dict] = []
    for side in response_sides:
        team_name = (side.get("team") or {}).get("name", "")
        # Defensive: provider may use slightly different alias. Falling back
        # to position (first/second) avoids dropping data if names diverge.
        if not home_stats_raw and (team_name == home_team or len(response_sides) == 2):
            home_stats_raw = side.get("statistics") or []
        elif not away_stats_raw:
            away_stats_raw = side.get("statistics") or []
    home_dict = stats_to_dict(home_stats_raw)
    away_dict = stats_to_dict(away_stats_raw)

    # Honesty flags: surface whether we even tried to read xG, and whether
    # the provider returned it. true_xg_available is the gated combination —
    # the proxy stays in charge until both XG_ENABLED is flipped here AND
    # pre_flight.py:628-629 is updated.
    home_xg = _xg_value(home_stats_raw)
    away_xg = _xg_value(away_stats_raw)
    xg_attempted = True
    xg_found = home_xg is not None and away_xg is not None
    true_xg_available = bool(XG_ENABLED and xg_found)

    if true_xg_available:
        home_delta = compute_xg_form_delta(home_xg, away_xg)
        away_delta = compute_xg_form_delta(away_xg, home_xg)
    else:
        home_delta = compute_form_delta(home_dict, away_dict)
        away_delta = compute_form_delta(away_dict, home_dict)

    return {
        "match_id": int(match["m"]),
        "status": match.get("status", "FT"),
        "home": home_team,
        "away": away_team,
        "home_form_adjustment_elo": round(home_delta, 3),
        "away_form_adjustment_elo": round(away_delta, 3),
        "true_xg_available": true_xg_available,
        "xg_attempted": xg_attempted,
        "xg_found": xg_found,
        "home_stats": home_dict,
        "away_stats": away_dict,
        "fixture_id": fixture_id,
        "captured_at": _now_iso(),
    }


def _replay_local(fixture_dir: Path, matches: list[dict],
                  fixture_map: dict[int, str]) -> tuple[list[dict], list[dict]]:
    entries, warnings = [], []
    for m in matches:
        pfid = fixture_map.get(int(m["m"]))
        if not pfid:
            warnings.append({"type": "unmapped_match", "match_id": m["m"]})
            continue
        f = fixture_dir / f"{pfid}.json"
        if not f.exists():
            warnings.append({"type": "fixture_file_missing",
                             "match_id": m["m"], "fixture_id": pfid})
            continue
        response = json.loads(f.read_text()).get("response") or []
        entries.append(build_match_entry(m, response, pfid))
    return entries, warnings


def build_snapshot(entries: list[dict], warnings: list[dict]) -> dict:
    return {
        "generated_at": _now_iso(),
        "schema_version": 1,
        "source": "api_football",
        "n_completed": len(entries),
        "matches": entries,
        "warnings": warnings,
        "notes": (
            "Form-delta is a stats PROXY (shot-on-target + possession + corners), "
            "deliberately NOT xG. See scripts/live/stats_proxy_adjustments.py."
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch WC2026 post-match stats proxy.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("--fixture-dir", type=Path, default=None)
    ap.add_argument("--only-match", type=int, default=None,
                    help="Restrict to one match_id (debug).")
    args = ap.parse_args()

    matches = load_ft_matches(only=args.only_match)
    fixture_map = _load_fixture_map()
    print(f"[fetch_match_stats] {len(matches)} FT matches to process")

    if args.no_network:
        if not args.fixture_dir or not args.fixture_dir.exists():
            print("[fetch_match_stats] --no-network requires --fixture-dir DIR", file=sys.stderr)
            return 2
        entries, warnings = _replay_local(args.fixture_dir, matches, fixture_map)
    else:
        api_key = (os.environ.get("API_FOOTBALL_KEY")
                   or os.environ.get("WC_APIFOOTBALL_KEY"))
        entries: list[dict] = []
        warnings: list[dict] = []
        if not api_key:
            warnings.append({"type": "missing_key",
                             "message": "API_FOOTBALL_KEY not in env"})
        for m in matches:
            pfid = fixture_map.get(int(m["m"]))
            if not pfid:
                warnings.append({"type": "unmapped_match", "match_id": m["m"]})
                continue
            if not api_key:
                continue
            try:
                response = fetch_one_fixture(api_key, pfid)
            except urllib.error.HTTPError as e:
                warnings.append({"type": "http_error",
                                 "match_id": m["m"], "code": e.code})
                continue
            except Exception as e:
                warnings.append({"type": "fetch_error",
                                 "match_id": m["m"],
                                 "error": f"{type(e).__name__}: {e}"})
                continue
            if not response:
                continue
            entries.append(build_match_entry(m, response, pfid))

    snap = build_snapshot(entries, warnings)
    print(f"[fetch_match_stats] entries: {len(entries)} · warnings: {len(warnings)}")
    if args.dry_run:
        print(f"[fetch_match_stats] dry-run — would write {OUT_PATH.relative_to(ROOT)}")
        return 0
    _atomic_write_json(OUT_PATH, snap)
    print(f"[fetch_match_stats] wrote {OUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
