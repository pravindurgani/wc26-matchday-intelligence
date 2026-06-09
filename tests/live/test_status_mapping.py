"""
Tests for the API-Football status mapper and validator in fetch_results.py.

Run:
    python3 tests/live/test_status_mapping.py
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

from fetch_results import (  # noqa: E402
    APIFOOTBALL_STATUS_MAP, LOCKED_STATUSES, WARN_STATUSES,
    normalize_team, validate_match,
)

FIXTURE_FILE = Path(__file__).parent / "provider_samples" / "api_football_fixture_response.json"


def test_status_mapping():
    """Every API-Football short code maps to a known canonical bucket."""
    finals = {"FT", "AET", "PEN"}
    warns = {"POSTPONED", "ABANDONED", "CANCELED", "SUSPENDED",
             "INTERRUPTED", "WALKOVER", "WALKOVERAWARD"}
    live = {"LIVE", "SCHEDULED"}
    for short, canon in APIFOOTBALL_STATUS_MAP.items():
        assert canon in finals | warns | live, \
            f"{short} → {canon} is in an unknown bucket"
    # The three lockable statuses must map to themselves
    for s in finals:
        assert APIFOOTBALL_STATUS_MAP[s] == s, f"{s} should map to itself"


def test_team_alias_normalisation():
    cases = [
        ("USA", "United States"),
        ("Korea Republic", "South Korea"),
        ("Türkiye", "Turkey"),
        ("Czech Republic", "Czechia"),
        ("Côte d'Ivoire", "Ivory Coast"),
        ("Bosnia-Herzegovina", "Bosnia and Herzegovina"),
        ("Cabo Verde", "Cape Verde"),
        ("IR Iran", "Iran"),
        ("Spain", "Spain"),  # already canonical
    ]
    for raw, expected in cases:
        assert normalize_team(raw) == expected, f"normalize_team({raw!r}) → {normalize_team(raw)!r}, expected {expected!r}"


def test_sample_payload_categorisation():
    """Walk the sample payload, mirror fetch_results' filtering logic, assert each row's fate."""
    fixtures = json.loads(FIXTURE_FILE.read_text())["response"]

    # Build expected outcomes per fixture id
    expectations = {
        1000001: "lock",            # FT  Mexico 2-0 South Africa
        1000002: "lock",            # AET Korea Republic 2-2 Czech Republic
        1000003: "lock",            # PEN Canada 1-1 Bosnia-Herzegovina
        1000004: "skip_live",       # HT  — never lock
        1000005: "skip_live",       # 2H  — never lock
        1000006: "warn",            # PST
        1000007: "warn",            # ABD
        1000008: "warn",            # AWD
        1000009: "skip_invalid",    # FT but goals.home is null
        9999999: "skip_unknown",    # Unknown teams ("Atlantis")
    }
    cfg = json.loads((ROOT / "data" / "raw" / "wc2026_config.json").read_text())
    schedule = cfg["group_stage_schedule"]
    # Date-tolerant lookup: match (home, away) within ±1 day of provider date
    from datetime import date as _date, timedelta as _td
    by_teams = {}
    for s in schedule:
        by_teams.setdefault((s["home"], s["away"]), []).append((s["date"], s["m"]))

    def lookup(home, away, provider_date):
        cands = by_teams.get((home, away), [])
        if not cands: return None
        try:
            t = _date.fromisoformat(provider_date)
        except Exception:
            return cands[0][1]
        best, best_gap = None, 999
        for sd, mid in cands:
            try:
                gap = abs((_date.fromisoformat(sd) - t).days)
                if gap <= 1 and gap < best_gap:
                    best, best_gap = mid, gap
            except Exception:
                continue
        return best

    outcomes = {}
    for f in fixtures:
        fxid = f["fixture"]["id"]
        short = f["fixture"]["status"]["short"]
        canon = APIFOOTBALL_STATUS_MAP.get(short, short)
        home = normalize_team(f["teams"]["home"]["name"])
        away = normalize_team(f["teams"]["away"]["name"])
        date = f["fixture"]["date"][:10]
        gh = f["goals"]["home"]
        ga = f["goals"]["away"]

        if canon in WARN_STATUSES:
            outcomes[fxid] = "warn"
            continue
        if canon not in LOCKED_STATUSES:
            outcomes[fxid] = "skip_live"
            continue
        if gh is None or ga is None:
            outcomes[fxid] = "skip_invalid"
            continue
        m_id = lookup(home, away, date)
        if m_id is None:
            outcomes[fxid] = "skip_unknown"
            continue
        outcomes[fxid] = "lock"

    for fxid, expected in expectations.items():
        got = outcomes.get(fxid)
        assert got == expected, f"fixture {fxid}: expected {expected}, got {got}"


def test_validate_match():
    schedule = [{"m": 1, "date": "2026-06-11", "home": "Mexico", "away": "South Africa"}]
    ok, why = validate_match({"m": 1, "home_score": 2, "away_score": 0, "home": "Mexico", "away": "South Africa"}, schedule)
    assert ok, why
    # Wrong home team
    ok, why = validate_match({"m": 1, "home_score": 2, "away_score": 0, "home": "Spain", "away": "South Africa"}, schedule)
    assert not ok and "home mismatch" in why
    # Negative score
    ok, why = validate_match({"m": 1, "home_score": -1, "away_score": 0}, schedule)
    assert not ok and "invalid home_score" in why
    # Implausible
    ok, why = validate_match({"m": 1, "home_score": 99, "away_score": 0}, schedule)
    assert not ok and "implausible" in why
    # Unknown match id
    ok, why = validate_match({"m": 999, "home_score": 1, "away_score": 1}, schedule)
    assert not ok and "not in WC2026 schedule" in why
    # Missing field
    ok, why = validate_match({"m": 1, "home_score": 2}, schedule)
    assert not ok and "missing" in why


if __name__ == "__main__":
    tests = [
        ("status_mapping",          test_status_mapping),
        ("team_alias",              test_team_alias_normalisation),
        ("sample_categorisation",   test_sample_payload_categorisation),
        ("validate_match",          test_validate_match),
    ]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  [✓] {name}")
        except AssertionError as e:
            failures += 1
            print(f"  [✗] {name} — {e}")
        except Exception as e:
            failures += 1
            print(f"  [✗] {name} — {type(e).__name__}: {e}")
    print(f"\n  {len(tests) - failures}/{len(tests)} passed")
    sys.exit(0 if failures == 0 else 1)
