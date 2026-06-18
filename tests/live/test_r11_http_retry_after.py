"""R11 C1 + C3 regression — _http_client.py honors Retry-After on 429
+ retries 429 + bumped events fetch to retries=3.

Pre-R11:
  - C1: `time.sleep(2 ** attempt)` ignored Retry-After. 429 with
        Retry-After:60 just slept 1-2-4 seconds, then hammered the
        provider into another 429.
  - C1: 429 was treated as a non-retried 4xx — raised on first attempt.
        Cron operator had to wait for the next tick to re-try.
  - C3: `fetch_apifootball_events_for_fixture` passed retries=2 (vs
        the default 3). R32 burst of 8 KO matches hammered
        /fixtures/events with effectively single-retry.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))


def _load_hc():
    spec = importlib.util.spec_from_file_location(
        "hc_r11", ROOT / "scripts" / "live" / "_http_client.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_http_error(code: int, retry_after: str | None = None):
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        "http://test", code, "boom", headers, BytesIO(b"err"))


class TestR11C1RetryAfter(unittest.TestCase):
    def setUp(self):
        self.hc = _load_hc()

    def test_retry_after_seconds_honored_on_429(self):
        """429 with Retry-After: 5 must sleep ~5s on first retry instead
        of the default 2^attempt = 1s exponential backoff."""
        sleep_calls = []

        def fake_urlopen(*_args, **_kwargs):
            raise _make_http_error(429, retry_after="5")

        with patch.object(self.hc.urllib.request, "urlopen", fake_urlopen), \
             patch.object(self.hc.time, "sleep",
                          lambda x: sleep_calls.append(x)):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.hc.http_get_json("http://test", {}, retries=2)
            self.assertEqual(ctx.exception.code, 429)
        # One sleep between two attempts; the value must reflect the
        # Retry-After header, not 2^0 = 1.
        self.assertEqual(len(sleep_calls), 1)
        self.assertEqual(sleep_calls[0], 5.0,
            f"R11 C1: 429 with Retry-After:5 must sleep 5s; got {sleep_calls!r}")

    def test_retry_after_capped_at_60_seconds(self):
        """A misbehaving provider sending Retry-After: 9999 must not
        block a producer past the slow-cron step budget. The cap is
        60s (see _backoff_seconds docstring)."""
        sleep_calls = []

        def fake_urlopen(*_args, **_kwargs):
            raise _make_http_error(429, retry_after="9999")

        with patch.object(self.hc.urllib.request, "urlopen", fake_urlopen), \
             patch.object(self.hc.time, "sleep",
                          lambda x: sleep_calls.append(x)):
            with self.assertRaises(urllib.error.HTTPError):
                self.hc.http_get_json("http://test", {}, retries=2)
        self.assertEqual(sleep_calls[0], 60.0,
            f"R11 C1: Retry-After:9999 must be capped at 60s; "
            f"got {sleep_calls!r}")

    def test_429_retries_three_times(self):
        """Pre-R11 429 was treated as no-retry 4xx. R11 retries 429
        like 5xx — exhausts the budget and re-raises."""
        calls = {"n": 0}

        def fake_urlopen(*_args, **_kwargs):
            calls["n"] += 1
            raise _make_http_error(429)

        with patch.object(self.hc.urllib.request, "urlopen", fake_urlopen), \
             patch.object(self.hc.time, "sleep", lambda x: None):
            with self.assertRaises(urllib.error.HTTPError):
                self.hc.http_get_json("http://test", {}, retries=3)
        self.assertEqual(calls["n"], 3,
            f"R11 C1: 429 must retry 3 times; got {calls['n']}")

    def test_other_4xx_still_no_retry(self):
        """Non-429 4xx (auth / not-found / unprocessable) must STILL
        raise immediately — R11 C1 didn't permissivize them."""
        for code in (400, 401, 403, 404, 422):
            calls = {"n": 0}

            def fake_urlopen(*_args, **_kwargs):
                calls["n"] += 1
                raise _make_http_error(code)

            with patch.object(self.hc.urllib.request, "urlopen", fake_urlopen), \
                 patch.object(self.hc.time, "sleep", lambda x: None):
                with self.assertRaises(urllib.error.HTTPError):
                    self.hc.http_get_json("http://test", {}, retries=3)
            self.assertEqual(calls["n"], 1,
                f"R11 C1: {code} must still raise immediately; "
                f"got {calls['n']} calls")


class TestR11C3EventsFetchRetries(unittest.TestCase):
    """Static pin: fetch_apifootball_events_for_fixture must pass
    retries=3 (was retries=2 pre-R11)."""

    def test_events_fetch_retries_eq_3(self):
        src = (ROOT / "scripts" / "live" / "fetch_results.py").read_text()
        # Locate the events fetch call and assert retries=3 explicit.
        i = src.find("def fetch_apifootball_events_for_fixture(")
        j = src.find("\ndef ", i + 1)
        body = src[i:j]
        # Strip docstring + comments before checking — comments may still
        # reference "retries=2" historically.
        self.assertIn("retries=3", body,
            "R11 C3: events fetch must explicitly pass retries=3 to "
            "absorb R32 burst of 8 KO matches in 24h")
        # The pre-R11 retries=2 must be gone from the actual call
        # (http_get_json(... retries=2)). Allow the mention in comments
        # so the historical context stays documented.
        import re
        executable_call = re.search(
            r"http_get_json\([^)]*retries=2[^)]*\)", body)
        self.assertIsNone(executable_call,
            "R11 C3: http_get_json must NOT still be called with retries=2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
