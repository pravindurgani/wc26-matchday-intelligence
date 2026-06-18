"""
R10 Q2 regression — scripts/live/fetch_weather.py must use the shared
_knockout.load_knockout_fixtures() loader so the R9 P4 A1 missing-time
warning fires once per fetch_weather process invocation.

Pre-R10 fetch_weather.py inlined its own bracket parsing at _load_config
(lines 107-121) and silently hardcoded `time="20:00"` for every KO entry.
The R9 P4 A1 warning at scripts/live/_knockout.py:71-141 only fires when
something calls load_knockout_fixtures() — fetch_weather didn't, so
operators got NO signal about wrong UTC forecast hours for KO matches.
Net pre-R10: every Open-Meteo KO forecast was aimed at the wrong UTC
hour (M73 SoFi Stadium fetched at 03:00 UTC next day instead of 19:00
UTC real kickoff = wrong day's hourly forecast).

R10 Q2 imports load_knockout_fixtures and uses it inside _load_config.
This is structural — the R9 P4 A1 warning is now also a fetch_weather
signal.
"""
from __future__ import annotations

import importlib
import importlib.util
import io
import os
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))


class TestFetchWeatherUsesKnockoutLoader(unittest.TestCase):
    def setUp(self):
        # Fresh import so the R9 P4 A1 dedup set starts empty per test.
        for mod_name in ("_knockout", "fetch_weather", "weather_adjustments"):
            sys.modules.pop(mod_name, None)

    def test_fetch_weather_imports_load_knockout_fixtures(self):
        """Static pin: fetch_weather.py must import load_knockout_fixtures
        from _knockout — otherwise the R9 P4 A1 warning surface skips
        the weather process and KO forecast hours stay silently wrong."""
        src = (ROOT / "scripts" / "live" / "fetch_weather.py").read_text()
        self.assertIn("from _knockout import load_knockout_fixtures", src,
            "R10 Q2 (D1): fetch_weather.py must import load_knockout_fixtures "
            "so the R9 P4 A1 missing-time warning fires for weather too")

    def test_fetch_weather_no_longer_inlines_bracket_parsing(self):
        """The pre-R10 inline bracket loop (which silently hardcoded
        time='20:00') must be gone. Detect via the hardcoded literal
        pattern that was unique to the old _load_config path."""
        src = (ROOT / "scripts" / "live" / "fetch_weather.py").read_text()
        # The pre-R10 pattern: a dict literal with `"time": "20:00"` AND
        # `"phase": "knockout"` on adjacent lines inside _load_config.
        # If this combination reappears, the inline duplication is back.
        self.assertNotIn('"time": "20:00",\n                    "venue":', src,
            "R10 Q2 (D1): fetch_weather._load_config must NOT re-inline "
            "bracket parsing with hardcoded time='20:00' — that path "
            "bypasses the R9 P4 A1 warning surface")

    def test_load_config_emits_R9_P4_A1_warning_for_missing_KO_times(self):
        """End-to-end: invoking fetch_weather._load_config() must trigger
        the R9 P4 A1 stderr warning (because the loader sees all 32 KO
        entries with time=None in the current bracket file). Pre-R10
        this was silent."""
        import fetch_weather  # type: ignore[import-not-found]
        buf = io.StringIO()
        with redirect_stderr(buf):
            schedule, _, _ = fetch_weather._load_config()
        stderr = buf.getvalue()
        # Schedule must include the 32 KO entries.
        ko_entries = [s for s in schedule if s.get("phase") == "knockout"]
        self.assertEqual(len(ko_entries), 32,
            "R10 Q2 (D1): _load_config must surface all 32 KO entries via "
            "load_knockout_fixtures()")
        # And the R9 P4 A1 warning must have fired (it does because the
        # shipped bracket has time=None on all 32).
        self.assertIn("KO matches lack `time`", stderr,
            "R10 Q2 (D1): the R9 P4 A1 missing-time warning must surface "
            "from fetch_weather._load_config() now that it uses the "
            "shared loader; pre-R10 the inline parsing skipped it")


if __name__ == "__main__":
    unittest.main(verbosity=2)
