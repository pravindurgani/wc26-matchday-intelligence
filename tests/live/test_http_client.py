"""
H3 + H4 — shared HTTP client (retries + rate-limit) tests.

Audit context
-------------
Pre-R2-round-3, only `scripts/live/fetch_results.py:171-189` had HTTP
retries. The four remaining producers (`fetch_injuries`, `fetch_lineups`,
`fetch_match_stats`, `fetch_player_stats`) used a bare `urllib.urlopen`
that turned a single transient 5xx / URLError into a degraded subsystem
on the matchday tick. `fetch_player_stats` additionally lacked any
inter-request throttle and fired 48+ /players?team=… calls per tick,
which on API-Football's free tier (10/min) cascades to 429s.

This file pins:

  H3a — `http_get_json` retries 5xx with exponential backoff and
        eventually succeeds when the upstream recovers
  H3b — `http_get_json` does NOT retry 4xx — auth / usage failures
        surface fast
  H3c — `http_get_json` retries URLError / TimeoutError / ConnectionError
        and re-raises the last one if every retry fails
  H3d — every fetcher's local `_http_get_json` delegates to the shared
        helper (catches a refactor that re-introduces a bare urlopen)

  H4a — `RateLimiter(min_interval=0)` is a no-op
  H4b — `RateLimiter` enforces the minimum gap between consecutive
        `acquire()` calls
  H4c — `RateLimiter` rejects negative intervals at construction
  H4d — `fetch_player_stats.fetch_team_players` accepts a `rate_limiter`
        kwarg and calls `acquire()` before EACH paginated HTTP request
  H4e — `fetch_player_stats.fetch_apifootball_player_stats` instantiates
        a single shared `RateLimiter` and passes it through
"""
from __future__ import annotations

import sys
import time
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT / "scripts" / "live"),):
    if p not in sys.path:
        sys.path.insert(0, p)

from _http_client import RateLimiter, http_get_json  # noqa: E402


# ─────────────────────────────────────────────────── H3a: 5xx + recover
def _make_http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://test", code=code, msg="x", hdrs=None, fp=None,
    )


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *a: object) -> None:
        return None


def test_h3a_5xx_then_recovery_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """5xx on attempts 0-1, 200 on attempt 2 → returns parsed JSON.
    Total sleep budget ≤ 1 + 2 = 3 seconds; we use a 0-sleep monkeypatch
    so the assertion is about RESPONSE not timing.
    """
    calls = {"n": 0}

    def fake_urlopen(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _make_http_error(503)
        return _FakeResponse(b'{"ok": true, "n": 3}')

    monkeypatch.setattr("_http_client.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("_http_client.time.sleep", lambda _x: None)

    result = http_get_json("http://test", {}, retries=3)
    assert result == {"ok": True, "n": 3}
    assert calls["n"] == 3, (
        f"expected 3 attempts, observed {calls['n']}"
    )


# ─────────────────────────────────────────────────── H3b: no 4xx retry
# R11 C1: 429 is now retried (with Retry-After honoring) — separated out
# from the non-retried 4xx codes so the retry-on-429 path is exercised
# explicitly. 400/401/403/404/422 still raise immediately.
@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_h3b_4xx_does_not_retry(code: int,
                                monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-429 4xx raises HTTPError immediately — no sleep, no retry.
    Auth / not-found / unprocessable errors don't go away on retry."""
    calls = {"n": 0}

    def fake_urlopen(*_args, **_kwargs):
        calls["n"] += 1
        raise _make_http_error(code)

    monkeypatch.setattr("_http_client.urllib.request.urlopen", fake_urlopen)
    sleep_calls = []
    monkeypatch.setattr("_http_client.time.sleep",
                        lambda x: sleep_calls.append(x))

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        http_get_json("http://test", {}, retries=3)

    assert exc_info.value.code == code
    assert calls["n"] == 1, f"expected 1 call (no retry on 4xx), observed {calls['n']}"
    assert sleep_calls == [], f"expected no sleeps on 4xx, observed {sleep_calls}"


# R11 C1: 429 (rate-limit) DOES retry with backoff, honoring Retry-After
# when present. Pre-R11 the test bundled 429 with 4xx non-retry — that
# behavior was the bug: a 429 with Retry-After:1 should sleep 1s and
# retry, not raise immediately and force the next slow-cron tick to do
# the same retry.
def test_h3b_429_does_retry_with_backoff(
        monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_urlopen(*_args, **_kwargs):
        calls["n"] += 1
        raise _make_http_error(429)

    monkeypatch.setattr("_http_client.urllib.request.urlopen", fake_urlopen)
    sleep_calls = []
    monkeypatch.setattr("_http_client.time.sleep",
                        lambda x: sleep_calls.append(x))

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        http_get_json("http://test", {}, retries=3)
    assert exc_info.value.code == 429
    assert calls["n"] == 3, f"expected 3 attempts on 429, observed {calls['n']}"
    # Two sleeps between three attempts (no sleep after the final attempt).
    assert len(sleep_calls) == 2


# ─────────────────────────────────────────────────── H3c: URLError exhausted
def test_h3c_persistent_urlerror_exhausts_retries(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """All `retries` attempts raise URLError → re-raises the last one."""
    calls = {"n": 0}

    def fake_urlopen(*_args, **_kwargs):
        calls["n"] += 1
        raise urllib.error.URLError("network down")

    monkeypatch.setattr("_http_client.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("_http_client.time.sleep", lambda _x: None)

    with pytest.raises(urllib.error.URLError):
        http_get_json("http://test", {}, retries=3)
    assert calls["n"] == 3, (
        f"expected 3 attempts, observed {calls['n']}"
    )


def test_h3c_timeout_error_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """TimeoutError gets the same retry treatment as URLError."""
    calls = {"n": 0}

    def fake_urlopen(*_args, **_kwargs):
        calls["n"] += 1
        raise TimeoutError("slow upstream")

    monkeypatch.setattr("_http_client.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("_http_client.time.sleep", lambda _x: None)

    with pytest.raises(TimeoutError):
        http_get_json("http://test", {}, retries=2)
    assert calls["n"] == 2


# ─────────────────────────────────────────────────── H3d: shim wiring
@pytest.mark.parametrize("module_name", [
    "fetch_injuries", "fetch_lineups",
    "fetch_match_stats", "fetch_player_stats",
])
def test_h3d_fetcher_delegates_to_shared_http_get_json(
        module_name: str,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Each of the 4 audit-flagged fetchers must route through
    `_http_client.http_get_json`. Importing the module + calling its
    `_http_get_json` with a monkeypatched shared helper proves the
    delegation is intact and not a copy of the old bare-urlopen body.
    """
    # Reload via a fresh import so we get the latest module bytecode.
    import importlib
    mod = importlib.import_module(module_name)

    captured = {"called": False, "args": None}

    def fake_http_get_json(url, headers, timeout=15, retries=3):
        captured["called"] = True
        captured["args"] = (url, headers, timeout)
        return {"sentinel": True}

    # Patch the shared module so the LATE-import inside the shim picks
    # up the fake on next call. This is exactly why the fetchers use a
    # late import — late binding makes monkeypatching trivial.
    import _http_client
    monkeypatch.setattr(_http_client, "http_get_json", fake_http_get_json)

    out = mod._http_get_json("http://example.test/x", {"k": "v"})
    assert out == {"sentinel": True}
    assert captured["called"] is True, (
        f"{module_name}._http_get_json did NOT delegate to the shared helper"
    )
    assert captured["args"][0] == "http://example.test/x"
    assert captured["args"][1] == {"k": "v"}


# ─────────────────────────────────────────────────── H4a-c: RateLimiter
def test_h4a_zero_interval_is_noop() -> None:
    """RateLimiter(0).acquire() returns immediately even on tight loops."""
    rl = RateLimiter(0.0)
    t0 = time.monotonic()
    for _ in range(50):
        rl.acquire()
    elapsed = time.monotonic() - t0
    assert elapsed < 0.05, f"expected near-zero, observed {elapsed:.3f}s"


def test_h4b_enforces_minimum_gap() -> None:
    """Two consecutive acquires are at least `min_interval` apart.
    Use a small interval to keep the test fast but big enough to be
    measurable above timer noise."""
    rl = RateLimiter(0.05)
    rl.acquire()  # first call returns immediately
    t0 = time.monotonic()
    rl.acquire()
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.045, (  # tiny slack for sleep precision
        f"second acquire returned in {elapsed:.4f}s (expected >= 0.045s)"
    )


def test_h4c_rejects_negative_interval() -> None:
    with pytest.raises(ValueError, match="must be >= 0"):
        RateLimiter(-0.1)


def test_h4_first_acquire_is_immediate() -> None:
    """The very first acquire() never sleeps (no previous call to space from)."""
    rl = RateLimiter(0.5)
    t0 = time.monotonic()
    rl.acquire()
    elapsed = time.monotonic() - t0
    assert elapsed < 0.05, (
        f"first acquire blocked for {elapsed:.4f}s (should be immediate)"
    )


# ───────────────────────────────────── H4d: fetch_team_players takes limiter
def test_h4d_fetch_team_players_calls_acquire_before_each_request() -> None:
    """The paginated loop in fetch_team_players acquires the rate_limiter
    BEFORE each /players page request — not just before the first."""
    import fetch_player_stats as fps

    # Track acquire() calls vs HTTP requests; they should be 1:1.
    acquire_count = {"n": 0}
    http_count = {"n": 0}

    class _CountingLimiter:
        def acquire(self) -> None:
            acquire_count["n"] += 1

    # Fake _http_get_json that drives 3 pages of pagination.
    pages: list[dict] = [
        {"paging": {"current": 1, "total": 3}, "response": [{"k": "p1"}]},
        {"paging": {"current": 2, "total": 3}, "response": [{"k": "p2"}]},
        {"paging": {"current": 3, "total": 3}, "response": [{"k": "p3"}]},
    ]

    def fake_get(_url: str, _headers: dict, timeout: int = 20) -> dict:
        idx = http_count["n"]
        http_count["n"] += 1
        return pages[idx]

    with patch.object(fps, "_http_get_json", fake_get), \
         patch.object(fps, "assert_shape", lambda *a, **kw: None):
        records, warns = fps.fetch_team_players(
            api_key="k", team_id=1, season="2026",
            rate_limiter=_CountingLimiter(),
        )

    assert http_count["n"] == 3, f"expected 3 HTTP calls, got {http_count['n']}"
    assert acquire_count["n"] == 3, (
        f"expected 3 acquire() calls (1:1 with HTTP), got {acquire_count['n']}"
    )
    assert len(records) == 3
    assert warns == []


# ────────────── H4e: fetch_apifootball_player_stats wires the limiter through
def test_h4e_fetch_apifootball_player_stats_creates_shared_limiter() -> None:
    """The high-level fan-out must create a SINGLE RateLimiter and pass
    it through to each fetch_team_players call (so the rate ceiling
    applies across teams, not just within one team)."""
    import fetch_player_stats as fps

    limiters_seen: list = []
    call_count = {"n": 0}

    def fake_team_players(_api_key, team_id, _season, rate_limiter=None):
        call_count["n"] += 1
        limiters_seen.append(id(rate_limiter) if rate_limiter else None)
        return [{"player": {"id": team_id, "name": "X"}}], []

    def fake_team_ids(_k, _l, _s):
        return ({"Argentina": 1, "Brazil": 2, "France": 3}, [])

    with patch.object(fps, "fetch_team_players", fake_team_players), \
         patch.object(fps, "fetch_team_ids", fake_team_ids):
        out, warns = fps.fetch_apifootball_player_stats(
            api_key="k",
            wc_teams={"Argentina", "Brazil", "France"},
            sleep_between=0.0,  # disable actual throttle for test speed
        )

    assert call_count["n"] == 3
    # sleep_between=0 → limiter is None (no throttling needed).
    assert all(l is None for l in limiters_seen), (
        "sleep_between=0 should produce a None limiter (skip throttle entirely)"
    )


def test_h4e_nonzero_interval_creates_shared_limiter() -> None:
    """With sleep_between > 0, the SAME limiter object is passed to every
    team — that's what makes the rate-ceiling apply across the full
    sweep, not per-team."""
    import fetch_player_stats as fps

    limiters_seen: list = []

    def fake_team_players(_api_key, team_id, _season, rate_limiter=None):
        limiters_seen.append(id(rate_limiter))
        return [], []

    def fake_team_ids(_k, _l, _s):
        return ({"Argentina": 1, "Brazil": 2}, [])

    with patch.object(fps, "fetch_team_players", fake_team_players), \
         patch.object(fps, "fetch_team_ids", fake_team_ids):
        fps.fetch_apifootball_player_stats(
            api_key="k",
            wc_teams={"Argentina", "Brazil"},
            sleep_between=0.01,
        )

    assert len(limiters_seen) == 2
    assert limiters_seen[0] is not None
    assert limiters_seen[0] == limiters_seen[1], (
        "expected same RateLimiter instance shared across teams; got "
        f"distinct ids {limiters_seen}"
    )
