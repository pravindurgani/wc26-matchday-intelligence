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
        the R9 P4 A1 stderr warning when the bracket it loads has KO
        entries with time=None. Pre-R10 this was silent.

        2026-07-03 update: the SHIPPED bracket now carries real kickoff
        times (the data fix the warning demanded), so the warning surface
        is exercised against a synthetic missing-time bracket routed
        through the real `_knockout.load_knockout_fixtures` — the wiring
        under regression test (fetch_weather → shared loader → warning)
        is unchanged."""
        import json
        import tempfile
        from unittest import mock

        import _knockout
        import fetch_weather  # type: ignore[import-not-found]

        synthetic = {
            "r32_slots": [
                {"match_num": m, "date": "2026-06-28", "time": None,
                 "venue": "SoFi", "slot_a": "1A", "slot_b": "2B"}
                for m in range(73, 89)
            ],
            "r16_bracket": [
                {"match_num": m, "date": "2026-07-04", "time": None,
                 "venue": "AT&T", "slot_a": f"W{m-15}", "slot_b": f"W{m-14}"}
                for m in range(89, 97)
            ],
            "qf_bracket": [
                {"match_num": m, "date": "2026-07-09", "time": None,
                 "venue": "Gillette", "slot_a": f"W{m-8}", "slot_b": f"W{m-7}"}
                for m in range(97, 101)
            ],
            "sf_bracket": [
                {"match_num": m, "date": "2026-07-14", "time": None,
                 "venue": "MetLife", "slot_a": f"W{m-4}", "slot_b": f"W{m-3}"}
                for m in range(101, 103)
            ],
            "final_and_third_place": {
                "third_place": {"match_num": 103, "date": "2026-07-18",
                                "time": None, "venue": "Hard Rock",
                                "slot_a": "L101", "slot_b": "L102"},
                "final": {"match_num": 104, "date": "2026-07-19",
                          "time": None, "venue": "MetLife",
                          "slot_a": "W101", "slot_b": "W102"},
            },
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                         delete=False) as tmp:
            json.dump(synthetic, tmp)
            tmp_path = Path(tmp.name)
        try:
            # fetch_weather binds the loader name into its own namespace
            # (`from _knockout import load_knockout_fixtures`), so patch
            # THAT binding to route through the real loader against the
            # synthetic bracket — the warning still comes from the real
            # _knockout code path.
            with mock.patch.object(
                fetch_weather, "load_knockout_fixtures",
                lambda: _knockout.load_knockout_fixtures(tmp_path),
            ):
                buf = io.StringIO()
                with redirect_stderr(buf):
                    schedule, _, _ = fetch_weather._load_config()
            stderr = buf.getvalue()
            # Schedule must include the 32 KO entries.
            ko_entries = [s for s in schedule if s.get("phase") == "knockout"]
            self.assertEqual(len(ko_entries), 32,
                "R10 Q2 (D1): _load_config must surface all 32 KO entries via "
                "load_knockout_fixtures()")
            # And the R9 P4 A1 warning must have fired for the synthetic
            # missing-time bracket.
            self.assertIn("KO matches lack `time`", stderr,
                "R10 Q2 (D1): the R9 P4 A1 missing-time warning must surface "
                "from fetch_weather._load_config() now that it uses the "
                "shared loader; pre-R10 the inline parsing skipped it")
        finally:
            tmp_path.unlink()

    def test_load_config_is_warning_free_on_shipped_fully_timed_bracket(self):
        """2026-07-03: shipped bracket times are sourced — _load_config on
        repo data must surface 32 KO entries with NO missing-time warning
        (false-positive log noise would train operators to ignore it)."""
        import fetch_weather  # type: ignore[import-not-found]
        buf = io.StringIO()
        with redirect_stderr(buf):
            schedule, _, _ = fetch_weather._load_config()
        ko_entries = [s for s in schedule if s.get("phase") == "knockout"]
        self.assertEqual(len(ko_entries), 32)
        self.assertNotIn("KO matches lack `time`", buf.getvalue(),
            "shipped bracket is fully timed as of 2026-07-03 — a warning "
            "here means either the data regressed or the loader broke")


if __name__ == "__main__":
    unittest.main(verbosity=2)
