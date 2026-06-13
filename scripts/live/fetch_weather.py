"""
fetch_weather.py — Stream B.2 weather fetcher.

Pulls per-match weather from Open-Meteo (no API key required, fair-use
limits ~10000 req/day). For matches within Open-Meteo's 16-day forecast
horizon: fetches hourly forecast and picks the kickoff-hour values.
For matches beyond 16 days: falls back to the venue's static climate
bucket from wc2026_config.json — the dashboard correctly labels these
as `confidence: "static_fallback"`.

Output: data/live/weather_2026.json
  - One entry per upcoming match (skipped if match already locked)
  - Each entry includes raw observations, derived heat-index + wet-bulb,
    classified bucket, per-team Elo adjustments, and confidence label.

The downstream apply_matchday_adjustments.py reads this file via its
weather loader and applies caps + aggregates with other layers.

Behaviour:
  - Fails GRACEFULLY: if Open-Meteo is unreachable, every match gets a
    static_fallback entry (rather than dropping the whole file).
  - Idempotent: re-running overwrites with the latest forecast.
  - No external secrets — Open-Meteo is keyless.
  - HTTP retries on 5xx with the same backoff pattern as fetch_results.

Run:
    python3 scripts/live/fetch_weather.py
    python3 scripts/live/fetch_weather.py --dry-run     # no write
    python3 scripts/live/fetch_weather.py --only-match 12
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
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[2]
LIVE = ROOT / "data" / "live"
RAW = ROOT / "data" / "raw"
OUT_PATH = LIVE / "weather_2026.json"

OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
FORECAST_HORIZON_DAYS = 16  # Open-Meteo free tier maximum

# Reuse the math helpers (testable, no network)
sys.path.insert(0, str(Path(__file__).parent))
from weather_adjustments import (  # noqa: E402
    heat_index_c, wet_bulb_proxy_c, classify_weather_bucket, team_elo_adjustment,
)


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent),
        prefix=path.name + ".", suffix=".tmp", delete=False,
    ) as tmp:
        json.dump(payload, tmp, indent=2, ensure_ascii=False)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def _http_get_json(url: str, retries: int = 3, timeout: int = 15) -> dict:
    """Mirrors fetch_results.http_get_json semantics. 5xx retried, 4xx raised."""
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                raise
            last_err = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
        time.sleep(2 ** attempt)
    raise last_err if last_err else RuntimeError(f"_http_get_json failed: {url}")


def _load_config() -> tuple[list[dict], dict[str, dict], dict[str, str]]:
    """Return (schedule, venue_by_city, venue_city_map).
       schedule = group + knockout matches.
       venue_city_map translates stadium-suburb names ("Inglewood", "Foxborough")
       into host-city anchors ("Los Angeles", "Boston") that the host_cities
       table is indexed by.
    """
    cfg = json.loads((RAW / "wc2026_config.json").read_text())
    schedule = list(cfg.get("group_stage_schedule") or [])
    bracket_path = RAW / "knockout_bracket_2026.json"
    if bracket_path.exists():
        bracket = json.loads(bracket_path.read_text())
        # Knockout matches don't have home/away assigned until the bracket
        # resolves — we still need them in the schedule so we can fetch
        # their weather using the venue. Mark them with phase='knockout'.
        for section_key in ("r32_slots", "r16_bracket", "qf_bracket", "sf_bracket"):
            for s in bracket.get(section_key, []):
                schedule.append({
                    "m": s["match_num"], "date": s["date"], "time": "20:00",
                    "venue": s["venue"], "home": None, "away": None,
                    "phase": "knockout",
                })
        ft = bracket.get("final_and_third_place") or {}
        for k in ("third_place", "final"):
            if k in ft:
                schedule.append({
                    "m": ft[k]["match_num"], "date": ft[k]["date"], "time": "20:00",
                    "venue": ft[k]["venue"], "home": None, "away": None,
                    "phase": "knockout",
                })
    # Venue-city index
    host_cities = cfg.get("host_cities") or []
    venue_by_city = {hc["city"]: hc for hc in host_cities}
    venue_city_map = cfg.get("venue_city_map") or {}
    return schedule, venue_by_city, venue_city_map


def _venue_to_city(venue_str: str, venue_city_map: dict[str, str]) -> str:
    """The schedule's `venue` field is "Inglewood, CA" / "Mexico City" / etc.
    First strip the state suffix, then run through venue_city_map to translate
    stadium suburbs ("Inglewood", "Foxborough") to host-city anchors
    ("Los Angeles", "Boston") that host_cities[] is indexed under.
    """
    suburb = venue_str.split(",")[0].strip()
    return venue_city_map.get(suburb, suburb)


def _within_forecast_horizon(match_date: str, today: _date) -> bool:
    """Open-Meteo's free `forecast` endpoint allows `start_date` up to but
    NOT including (today + 16 days). Strict less-than guards against the
    HTTP 400 'start_date out of allowed range' error we'd otherwise hit
    on the exact boundary."""
    try:
        md = _date.fromisoformat(match_date)
        return md >= today and (md - today).days < FORECAST_HORIZON_DAYS
    except Exception:
        return False


# H1 (Round 16): tz-aware kickoff_utc derivation.
# Pre-H1, kickoff_utc was f"{local_date}T{local_time}:00Z" — concatenating
# the schedule's LOCAL clock time with the Z suffix. _pick_hour then matched
# the (mis-labelled) local hour against Open-Meteo's UTC-indexed hourly array,
# yielding a sample 4-7h off the true kickoff (e.g., M5 Santa Clara "noon"
# showed 14°C / 96% humidity — the actual 05:00 PDT dawn fog reading).
# Also, 31/72 group-stage matches kick off late enough locally that the UTC
# date wraps into the next day — so even requesting the right hour requires
# requesting the right UTC date too. Mexico abolished DST in 2023 (UTC-6
# year-round); US/Canada cities still observe DST per IANA tzdb.
def _venue_tz(venue: dict | None) -> ZoneInfo | None:
    """Resolve venue's IANA tz from host_cities[].tz; None if absent."""
    if not venue:
        return None
    tz_name = venue.get("tz")
    if not tz_name:
        return None
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return None


def _kickoff_utc_dt(match: dict, venue: dict | None) -> datetime | None:
    """Compute true UTC kickoff datetime from local clock + venue tz.
    Returns None if the venue lacks a tz field — caller falls back to
    legacy behavior so config migration can be incremental."""
    tz = _venue_tz(venue)
    if tz is None:
        return None
    try:
        local_dt = datetime.fromisoformat(
            f"{match['date']}T{match.get('time','20:00')}:00"
        ).replace(tzinfo=tz)
        return local_dt.astimezone(timezone.utc)
    except (ValueError, KeyError):
        return None


def _kickoff_utc_iso(match: dict, venue: dict | None) -> str:
    """ISO 8601 'Z'-suffixed UTC kickoff string. Falls back to the
    legacy local-as-Z format when venue has no tz — keeps the artifact
    parseable while config rollout is staged."""
    dt = _kickoff_utc_dt(match, venue)
    if dt is None:
        return f"{match['date']}T{match.get('time','20:00')}:00Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# Per-fetch warnings are sampled (cap = 3) so a wedged Open-Meteo on a
# 104-match day doesn't bloat the snapshot. The aggregate sentinel emitted
# in main() carries the full failure count regardless.
_WARNING_SAMPLE_CAP = 3


def _fetch_open_meteo(lat: float, lon: float, day: str,
                      match_id: int | None = None,
                      warnings_acc: list[dict] | None = None) -> dict | None:
    """Hit the Open-Meteo forecast endpoint for a single day.

    Returns the parsed JSON or None on any failure (network error, 4xx,
    bogus payload). Caller handles fallback.

    Failures are RECORDED to warnings_acc (sampled at _WARNING_SAMPLE_CAP
    per type) so apply_matchday_adjustments can lift them to the
    consolidated state. Prior to Round 12, this function silently
    print()'d failures — operators had no on-dashboard signal when
    Open-Meteo went dark and every match silently fell back to climate
    bucket. The sentinel chain (Round 11B) now wires through here too.
    """
    params = (
        f"latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m,relative_humidity_2m,apparent_temperature,"
        f"precipitation,rain,weather_code,wind_speed_10m,wind_gusts_10m,cloud_cover"
        f"&start_date={day}&end_date={day}&timezone=UTC"
    )
    url = f"{OPEN_METEO_FORECAST}?{params}"
    try:
        return _http_get_json(url)
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception as _body_err: body = f"<body unreadable: {type(_body_err).__name__}: {_body_err}>"
        print(f"[weather] HTTP {e.code} for {lat},{lon} {day}: {body}")
        if warnings_acc is not None:
            existing = sum(1 for w in warnings_acc if w.get("type") == "http_error")
            if existing < _WARNING_SAMPLE_CAP:
                warnings_acc.append({
                    "type": "http_error",
                    "code": e.code,
                    "match_id": match_id,
                    "body": body[:200],
                })
        return None
    except Exception as e:
        print(f"[weather] fetch error for {lat},{lon} {day}: {type(e).__name__}: {e}")
        if warnings_acc is not None:
            existing = sum(1 for w in warnings_acc if w.get("type") == "fetch_error")
            if existing < _WARNING_SAMPLE_CAP:
                warnings_acc.append({
                    "type": "fetch_error",
                    "match_id": match_id,
                    "message": f"{type(e).__name__}: {e}",
                })
        return None


def _pick_hour(hourly: dict, kickoff_iso: str) -> dict:
    """Open-Meteo returns hour-aligned arrays for the day. Pick the index
    matching the match's kickoff hour. Returns dict of single-value samples.
    """
    times = hourly.get("time") or []
    target = kickoff_iso[:13]  # "YYYY-MM-DDTHH"
    idx = next((i for i, t in enumerate(times) if (t or "").startswith(target)), None)
    if idx is None:
        # Fallback: use noon UTC for that day if exact hour missing
        idx = next((i for i, t in enumerate(times) if "T12" in (t or "")), 0)
    out = {}
    for k, vs in hourly.items():
        if k == "time" or not isinstance(vs, list):
            continue
        out[k] = vs[idx] if idx < len(vs) else None
    return out


# ── Match → weather entry ───────────────────────────────────────────────
def _build_entry_forecast(match: dict, venue: dict, hour_data: dict) -> dict:
    temp = hour_data.get("temperature_2m")
    rh = hour_data.get("relative_humidity_2m")
    apparent = hour_data.get("apparent_temperature")
    precip = hour_data.get("precipitation")
    wind = hour_data.get("wind_speed_10m")
    gust = hour_data.get("wind_gusts_10m")
    weather_code = hour_data.get("weather_code")
    cloud = hour_data.get("cloud_cover")

    hi = heat_index_c(temp, rh) if (temp is not None and rh is not None) else None
    wb = wet_bulb_proxy_c(temp, rh) if (temp is not None and rh is not None) else None
    bucket = classify_weather_bucket(apparent, rh, precip, gust, temp, wb)

    home, away = match.get("home"), match.get("away")
    # Knockout fixtures (no teams assigned yet) get an empty adjustment block
    # but still surface the weather metadata for the dashboard.
    # Pass the computed WBGT so the hydration-break dampener fires when
    # FIFA cooling-break threshold is reached (wet_bulb_c >= 32 °C).
    home_adj = team_elo_adjustment(home, bucket, wet_bulb_c=wb) if home else 0.0
    away_adj = team_elo_adjustment(away, bucket, wet_bulb_c=wb) if away else 0.0

    return {
        "match_id": match["m"],
        "phase": match.get("phase", "group"),
        "home": home, "away": away,
        "home_team": home, "away_team": away,
        "venue": match.get("venue"),
        "city": match.get("_city", _venue_to_city(match.get("venue", ""), {})),
        "kickoff_utc": _kickoff_utc_iso(match, venue),
        "temperature_c": temp,
        "humidity_pct": rh,
        "apparent_temperature_c": apparent,
        "precipitation_mm": precip,
        "wind_kph": wind,
        "gust_kph": gust,
        "weather_code": weather_code,
        "cloud_cover_pct": cloud,
        "heat_index_c": hi,
        "wet_bulb_proxy_c": wb,
        "weather_bucket": bucket,
        "home_team_adjustment_elo": round(home_adj, 2),
        "away_team_adjustment_elo": round(away_adj, 2),
        "confidence": "forecast",
        "source": "open_meteo",
    }


def _build_entry_static_fallback(match: dict, venue: dict) -> dict:
    """When a match is outside the 16-day forecast horizon OR the API is
    down, fall back to the venue's static climate bucket. Adjustments
    are computed from that climate label so the layer still contributes
    something defensible — labelled `static_fallback` for transparency."""
    climate = (venue or {}).get("climate", "") if venue else ""
    # Map static climate labels to our runtime buckets.
    _STATIC_TO_BUCKET = {
        "very_hot": "hot",
        "hot_humid": "hot_humid",
        "warm_humid": "hot",
        "high_altitude_mild": "normal",
        "temperate": "normal",
        "cold": "cold",
        "mild": "normal",
    }
    bucket = _STATIC_TO_BUCKET.get(climate, "normal")
    home, away = match.get("home"), match.get("away")
    home_adj = team_elo_adjustment(home, bucket) if home else 0.0
    away_adj = team_elo_adjustment(away, bucket) if away else 0.0
    return {
        "match_id": match["m"],
        "phase": match.get("phase", "group"),
        "home": home, "away": away,
        "home_team": home, "away_team": away,
        "venue": match.get("venue"),
        "city": match.get("_city", _venue_to_city(match.get("venue", ""), {})),
        "kickoff_utc": _kickoff_utc_iso(match, venue),
        "weather_bucket": bucket,
        "static_climate_label": climate,
        "home_team_adjustment_elo": round(home_adj, 2),
        "away_team_adjustment_elo": round(away_adj, 2),
        "confidence": "static_fallback",
        "source": "wc2026_config.host_cities[].climate",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only-match", type=int, default=None,
                    help="Fetch only this match number (for testing).")
    args = ap.parse_args()

    schedule, venue_by_city, venue_city_map = _load_config()
    today = datetime.now(timezone.utc).date()
    entries: list[dict] = []
    fetch_count = 0
    fallback_count = 0
    in_horizon_attempted = 0
    # warnings_acc collects per-match fetch failures (sampled) and an
    # aggregate sentinel if Open-Meteo wedged across the whole run.
    # apply_matchday_adjustments.py:_PROPAGATE_WARNING_TYPES already
    # includes http_error / fetch_error / no_records_returned, so any
    # entry we append here will lift to the dashboard's matchday-intel
    # detail block automatically.
    warnings_acc: list[dict] = []
    for m in schedule:
        if args.only_match and m["m"] != args.only_match:
            continue
        city = _venue_to_city(m.get("venue", ""), venue_city_map)
        m["_city"] = city  # stash so build_entry_* doesn't re-derive
        venue = venue_by_city.get(city)
        if not venue or venue.get("lat") is None or venue.get("lon") is None:
            print(f"[weather] M{m['m']}: no coords for {city!r} — skip")
            continue
        if _within_forecast_horizon(m["date"], today):
            in_horizon_attempted += 1
            # H1: request the UTC date covering the kickoff hour, not the
            # local date. 31/72 group matches kick off late enough locally
            # that the UTC kickoff is the next day (e.g., M28 Mexico City
            # 23:00 local = 05:00 UTC next day). Asking Open-Meteo for the
            # local date would return a hourly array that doesn't even
            # include the kickoff hour for those 31 matches.
            kickoff_dt = _kickoff_utc_dt(m, venue)
            utc_day = kickoff_dt.strftime("%Y-%m-%d") if kickoff_dt else m["date"]
            payload = _fetch_open_meteo(
                venue["lat"], venue["lon"], utc_day,
                match_id=m.get("m"), warnings_acc=warnings_acc)
            if payload and (payload.get("hourly") or {}).get("time"):
                # _pick_hour now receives a true UTC ISO string (or the
                # legacy local-as-Z string if venue lacks tz — same as before).
                kickoff_iso = _kickoff_utc_iso(m, venue)
                hour_data = _pick_hour(payload["hourly"], kickoff_iso)
                entries.append(_build_entry_forecast(m, venue, hour_data))
                fetch_count += 1
                continue
        # Fall through to static fallback (out of horizon or API failure)
        entries.append(_build_entry_static_fallback(m, venue))
        fallback_count += 1

    # Aggregate sentinel: if we ATTEMPTED any in-horizon fetches and ALL
    # of them failed, emit a single high-signal `no_records_returned`
    # alongside the per-match samples. This is the operator's "is
    # Open-Meteo wedged today?" signal — without it, a 100% fallback day
    # is indistinguishable from a 0% in-horizon day (off-window matches).
    if in_horizon_attempted > 0 and fetch_count == 0:
        warnings_acc.append({
            "type": "no_records_returned",
            "endpoint": "/v1/forecast (open-meteo)",
            "in_horizon_attempted": in_horizon_attempted,
            "message": (
                f"All {in_horizon_attempted} in-horizon Open-Meteo fetches "
                f"failed — every match fell back to climate bucket. "
                f"Check Open-Meteo status or rate limits."
            ),
        })

    out = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "open_meteo + wc2026_config_fallback",
        "forecast_horizon_days": FORECAST_HORIZON_DAYS,
        "fetched_count": fetch_count,
        "fallback_count": fallback_count,
        "weather": entries,
        "warnings": warnings_acc,
    }

    if args.dry_run:
        print(f"[weather] dry-run: would write {len(entries)} entries "
              f"({fetch_count} forecast, {fallback_count} fallback)")
        return 0

    _atomic_write_json(OUT_PATH, out)
    print(f"[weather] wrote {OUT_PATH.relative_to(ROOT)}: "
          f"{len(entries)} entries ({fetch_count} forecast, {fallback_count} fallback)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
