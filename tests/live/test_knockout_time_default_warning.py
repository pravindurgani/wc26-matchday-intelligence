"""
R9 P4 A1 regression — _knockout.load_knockout_fixtures must emit a
single summary stderr warning naming the affected match_nums when KO
entries default to "20:00" because their `time` field is null/missing
in data/raw/knockout_bracket_2026.json.

Pre-R9 the `s.get("time", "20:00")` default fired silently for every
KO match (verified: 32/32 entries have time=None). Downstream:
  - fetch_weather computes Open-Meteo forecast hour from `time` →
    wrong UTC hour requested → wrong forecast applied.
  - fetch_lineups computes a 4h pre-kickoff window from `time` →
    real KO kickoff misses the window → KO lineup intel zero.

Sourcing FIFA's official KO kickoff times into the bracket file is
the proper fix; this warning surfaces the silent gap to operators
so they know which match_nums need data correction.

The warning must dedup per-process so repeated calls (every 10 min
during a tick) don't spam logs.
"""
from __future__ import annotations

import importlib
import io
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))


class TestKnockoutTimeDefaultWarning(unittest.TestCase):
    def setUp(self):
        # Re-import to reset the process-local _KO_DEFAULT_TIME_WARNED set
        # between tests so the dedup logic doesn't bleed across cases.
        if "_knockout" in sys.modules:
            del sys.modules["_knockout"]
        import _knockout  # noqa: E402
        self._knockout = _knockout
        # Make sure the dedup set is empty regardless.
        _knockout._KO_DEFAULT_TIME_WARNED.clear()

    def test_warning_emitted_when_ko_times_are_missing(self):
        """The shipped data/raw/knockout_bracket_2026.json has time=None on
        all 32 KO entries. The loader must emit exactly one summary
        warning naming the affected match_nums."""
        buf = io.StringIO()
        with redirect_stderr(buf):
            rows = self._knockout.load_knockout_fixtures()
        self.assertEqual(len(rows), 32)
        stderr = buf.getvalue()
        # The warning must surface the count, the action, and the deadline.
        self.assertIn("KO matches lack `time`", stderr,
            "R9 P4 A1: missing time warning must name the gap explicitly")
        self.assertIn("knockout_bracket_2026.json", stderr,
            "R9 P4 A1: warning must name the file an operator must fix")
        self.assertIn("R32", stderr,
            "R9 P4 A1: warning must name the deadline (R32 kickoff)")
        # The list of affected match_nums must appear (e.g. [73, 74, ..., 104]).
        for m in (73, 88, 104):
            self.assertIn(str(m), stderr,
                f"R9 P4 A1: warning must enumerate affected m={m}")

    def test_warning_dedups_across_repeated_calls(self):
        """Calling load_knockout_fixtures() N times in a process must emit
        the summary warning at most ONCE per match_num — otherwise a 10-min
        fast-tick that reloads the bracket bloats every operator's terminal
        and the dashboard log buffer."""
        # First call: warning fires once.
        buf1 = io.StringIO()
        with redirect_stderr(buf1):
            self._knockout.load_knockout_fixtures()
        first_warn_lines = [l for l in buf1.getvalue().splitlines()
                            if "KO matches lack `time`" in l]
        self.assertEqual(len(first_warn_lines), 1)
        # Second call: dedup must suppress (no new match_nums).
        buf2 = io.StringIO()
        with redirect_stderr(buf2):
            self._knockout.load_knockout_fixtures()
        second_warn_lines = [l for l in buf2.getvalue().splitlines()
                             if "KO matches lack `time`" in l]
        self.assertEqual(len(second_warn_lines), 0,
            "R9 P4 A1: dedup set must suppress repeated summary warnings "
            "for already-flagged match_nums")

    def test_warning_does_NOT_fire_when_times_present(self):
        """Negative case: if a hypothetical bracket has all times populated,
        no warning fires. This guards against false-positive log noise."""
        # Patch the bracket reader to return a synthetic all-times-present
        # bracket. Use monkeypatching via swapping path content.
        import tempfile
        synthetic = {
            "r32_slots": [
                {"match_num": 73, "date": "2026-06-28", "time": "19:00",
                 "venue": "SoFi", "slot_a": "1A", "slot_b": "3DEFI"},
            ],
            "r16_bracket": [],
            "qf_bracket": [],
            "sf_bracket": [],
            "final_and_third_place": {},
        }
        import json
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
            json.dump(synthetic, tmp)
            tmp_path = Path(tmp.name)
        try:
            buf = io.StringIO()
            with redirect_stderr(buf):
                rows = self._knockout.load_knockout_fixtures(tmp_path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["time"], "19:00")
            self.assertNotIn("KO matches lack `time`", buf.getvalue(),
                "R9 P4 A1: warning must NOT fire when all KO entries have "
                "a `time` field populated")
        finally:
            tmp_path.unlink()


if __name__ == "__main__":
    unittest.main(verbosity=2)
