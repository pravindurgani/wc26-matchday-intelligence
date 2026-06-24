"""R12 B2 regression — fetch_results.py MUST route through
_http_client.http_get_json so the R11 C1 Retry-After honoring + 429
retry actually applies to the /fixtures/events call path.

Pre-R12 fetch_results.py defined its OWN local http_get_json that:
  - ignored Retry-After header on 429
  - raised on 429 without retry
  - sleeps on every attempt including the final (wasted 1s per fail)

The R11 C3 comment at fetch_apifootball_events_for_fixture claimed
Retry-After benefit via "_http_client.http_get_json" — but the actual
call site invoked the LOCAL function.

R12 B2 deletes the local def and imports from _http_client. The R32
burst (8 KO matches × /fixtures/events on 2026-06-28) now actually
backs off correctly when API-Football issues 429 with Retry-After.
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class TestR12B2EventsRetryAfter(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / "scripts" / "live" / "fetch_results.py").read_text()

    def test_local_http_get_json_deleted(self):
        """The local def http_get_json must be gone — only the import remains."""
        # Search for `def http_get_json(` — must NOT appear (only the import).
        self.assertNotRegex(
            self.src,
            r"^def http_get_json\(",
            "R12 B2: local http_get_json definition must be deleted"
        )
        # Multi-line guard to be safe.
        import re
        m = re.search(r"^def http_get_json\(", self.src, re.M)
        self.assertIsNone(m,
            "R12 B2: local http_get_json definition must be deleted "
            "(use shared _http_client.http_get_json instead)")

    def test_imports_from_http_client(self):
        self.assertIn("from scripts.live._http_client import http_get_json",
                      self.src,
            "R12 B2: fetch_results must import http_get_json from _http_client")

    def test_no_local_retry_loop_pattern(self):
        """The local fn had `time.sleep(2 ** attempt)` after the urlopen
        call. With local fn deleted, that pattern must be gone from
        fetch_results too."""
        # Allow the comment / docstring to mention 2**attempt for context,
        # but the executable loop is gone.
        import re
        # The pattern was `time.sleep(2 ** attempt)` in a retry loop.
        # If still present, the local fn might be back.
        execs = re.findall(r"^\s*time\.sleep\(2 \*\* attempt\)", self.src, re.M)
        self.assertEqual(execs, [],
            "R12 B2: executable `time.sleep(2 ** attempt)` retry pattern "
            "must be gone — it now lives in _http_client only")

    def test_events_call_site_still_passes_retries_3(self):
        """The R11 C3 retries=3 bump must still be in place."""
        import re
        events_fn = re.search(
            r"def fetch_apifootball_events_for_fixture[\s\S]*?(?=\ndef |\Z)",
            self.src,
        )
        self.assertIsNotNone(events_fn)
        self.assertIn("retries=3", events_fn.group(0),
            "R12 B2: events fetch must still pass retries=3 (R11 C3)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
