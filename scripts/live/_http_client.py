"""
Shared HTTP client for live fetchers — exponential-backoff retries +
optional inter-request rate-limiting.

Why this exists
---------------
Pressure-test audit H3 (R2 round 3) surfaced that 4 of 5 fetchers
(`fetch_injuries.py`, `fetch_lineups.py`, `fetch_match_stats.py`,
`fetch_player_stats.py`) used a bare `urllib.request.urlopen` with no
retries. A single transient 5xx or `URLError` would fail the producer,
which the P1b degrade-don't-crash contract then turned into a
"subsystem_stale" warning. The freshness layer surfaces that warning
correctly, but a one-line retry is strictly less disruptive than a
matchday-degraded tick. Only `fetch_results.py:171` had retries before
this round.

Contract
--------
- `http_get_json(url, headers, timeout=15, retries=3)` GETs JSON with
  exponential backoff (1s, 2s, 4s) on 5xx / `URLError` / `TimeoutError`
  / `ConnectionError`.
- 4xx errors raise immediately — auth / usage problems do not benefit
  from retry and should surface fast.
- Final-attempt failure re-raises the last exception (same shape as the
  pre-existing `fetch_results.http_get_json` so all consumers'
  `except urllib.error.HTTPError / except Exception` clauses still fire).

Rate-limiting helper
--------------------
`RateLimiter(min_interval_seconds)` is a tiny token-bucket-of-one: every
`acquire()` call sleeps long enough to ensure two consecutive acquires
are at least `min_interval_seconds` apart. Designed for the per-team
fan-out pattern in `fetch_player_stats.py` where the producer issues
32+ requests per tick. API-Football's free tier is 10 req/min (≈6 s
between requests), paid tier is ~300 req/min (≈0.2 s between requests).
Default `0.15 s` matches the pattern already used by
`fetch_results.enrich_matches_with_events` at L406/L454-455 — the value
isn't a hard ceiling; it's a polite spacer that empirically keeps the
producer under burst limits on both tiers.

No new dependencies. No global state. Stateless functions + one tiny
class with a single float field — `RateLimiter` can be instantiated
per-loop without ceremony.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


# Default retry budget. 3 attempts = 7 seconds worst-case (1 + 2 + 4)
# which sits well inside any single producer step's allotment in
# matchday-intel-slow.yml (each step has 300s+).
_DEFAULT_RETRIES = 3
_DEFAULT_TIMEOUT = 15


def http_get_json(
    url: str,
    headers: dict,
    timeout: int = _DEFAULT_TIMEOUT,
    retries: int = _DEFAULT_RETRIES,
) -> dict:
    """HTTP GET → JSON with exponential backoff on 5xx + transient errors.

    Behavior:
    - 2xx → parse JSON, return dict
    - 4xx → raise `urllib.error.HTTPError` immediately (no retry — client
      errors won't go away on retry; auth/usage problems should surface)
    - 5xx / `URLError` / `TimeoutError` / `ConnectionError` → retry with
      backoff `2 ** attempt` seconds (1, 2, 4 by default)
    - Exhausted retries → raise the LAST captured exception so callers
      can pattern-match `except urllib.error.HTTPError` / `except Exception`
      the same way they did pre-this-helper.

    This is a direct port of `scripts/live/fetch_results.py:171-189` —
    pinned identical via `tests/live/test_http_client.py`.
    """
    last_err: Exception | None = None
    # R11 C1: cap honored Retry-After at 60s so a misbehaving provider
    # can't make a single producer block beyond the slow-cron step budget
    # (~300s). 429s with absurdly large Retry-After fall back to the
    # exponential backoff and let the next tick try again.
    _retry_after_cap = 60.0
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500 and e.code != 429:
                raise  # client error — don't retry (429 IS retried below)
            last_err = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
        # Backoff between attempts. Skip the sleep on the final attempt
        # (no further retry would benefit from it).
        if attempt < retries - 1:
            # R11 C1: honor Retry-After (RFC 7231 §7.1.3). Some providers
            # send 429 with a number-of-seconds-string ("60"); others use
            # an HTTP-date format. We accept the seconds form here — the
            # HTTP-date form is rare in API contexts and falls through to
            # the default exponential backoff. Pre-R11 a 429 with a
            # Retry-After:60 would just trigger 2^attempt = 1-2-4s
            # backoff, then we'd hammer again and get rate-limited again.
            sleep_seconds = _backoff_seconds(attempt, last_err, _retry_after_cap)
            time.sleep(sleep_seconds)
    # All retries exhausted — re-raise the last captured failure.
    raise last_err if last_err else RuntimeError(
        f"http_get_json failed: {url}"
    )


def _backoff_seconds(attempt: int, last_err: Exception | None,
                     retry_after_cap: float) -> float:
    """Compute backoff: honor Retry-After if present + numeric, else
    fall back to exponential 2 ** attempt. Cap honored Retry-After at
    retry_after_cap to keep a single producer step under its time budget.
    """
    if isinstance(last_err, urllib.error.HTTPError):
        try:
            ra = last_err.headers.get("Retry-After") if last_err.headers else None
            if ra is not None and str(ra).strip().isdigit():
                return min(float(ra), retry_after_cap)
        except Exception:
            pass
    return float(2 ** attempt)


class RateLimiter:
    """Minimal per-call throttle: every `acquire()` ensures at least
    `min_interval_seconds` has elapsed since the previous `acquire()`.

    Usage:
        rl = RateLimiter(0.15)
        for item in items:
            rl.acquire()
            do_request(item)

    The first acquire() returns immediately; subsequent ones sleep just
    enough to satisfy the interval. Thread-unsafe by design — each
    producer runs single-threaded.
    """

    def __init__(self, min_interval_seconds: float = 0.15) -> None:
        if min_interval_seconds < 0:
            raise ValueError(
                f"min_interval_seconds must be >= 0, got {min_interval_seconds}"
            )
        self.min_interval = float(min_interval_seconds)
        self._last_call: float | None = None

    def acquire(self) -> None:
        """Block until at least `min_interval_seconds` has passed since
        the previous acquire(). First call returns immediately.
        """
        if self.min_interval <= 0:
            return
        now = time.monotonic()
        if self._last_call is not None:
            elapsed = now - self._last_call
            wait = self.min_interval - elapsed
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
        self._last_call = now
