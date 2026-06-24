"""R11 D4 regression — apply_matchday_adjustments MUST call
`_check_freshness` on all 7 input feeds, not just the 3 from R9 P5 B1.

Pre-R11 only `referee_2026.json`, `suspensions_2026.json`, and
`player_stats_2026.json` had freshness guards. A multi-day-stale
weather / lineups / injuries / match_stats snapshot was silently
ingested with no `subsystem_stale` warning. The fast-path's
`matchday_subsystem_stale` lift only fires when the warning is
EMBEDDED — without per-feed freshness checks the operator never sees
it.

R11 D4 adds the missing 4 calls mirroring the existing pattern
(`_check_freshness(input, results, STALENESS_MAX_AGE_HOURS, subsystem,
warnings_acc)`) at the top of each of the 4 _load_*_components
functions.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))


def _load_amd():
    spec = importlib.util.spec_from_file_location(
        "amd_r11_d4", ROOT / "scripts" / "live" / "apply_matchday_adjustments.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestR11D4AllSevenLoadersHaveFreshnessCheck(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / "scripts" / "live"
                    / "apply_matchday_adjustments.py").read_text()

    def test_weather_loader_has_check_freshness(self):
        # Find _load_weather_components body and assert _check_freshness
        # appears inside it.
        i = self.src.find("def _load_weather_components(")
        j = self.src.find("\ndef ", i + 1)
        body = self.src[i:j]
        self.assertIn("_check_freshness(", body,
            "R11 D4: _load_weather_components must call _check_freshness")
        self.assertIn('"weather"', body)

    def test_lineup_loader_has_check_freshness(self):
        i = self.src.find("def _load_lineup_components(")
        j = self.src.find("\ndef ", i + 1)
        body = self.src[i:j]
        self.assertIn("_check_freshness(", body,
            "R11 D4: _load_lineup_components must call _check_freshness")
        self.assertIn('"lineup"', body)

    def test_injury_loader_has_check_freshness(self):
        i = self.src.find("def _load_injury_components(")
        j = self.src.find("\ndef ", i + 1)
        body = self.src[i:j]
        self.assertIn("_check_freshness(", body,
            "R11 D4: _load_injury_components must call _check_freshness")
        self.assertIn('"injury"', body)

    def test_stats_loader_has_check_freshness(self):
        i = self.src.find("def _load_stats_components(")
        j = self.src.find("\ndef ", i + 1)
        body = self.src[i:j]
        self.assertIn("_check_freshness(", body,
            "R11 D4: _load_stats_components must call _check_freshness")
        self.assertIn('"stats_proxy"', body)


class TestR11A4CorruptJSONFallbackEmitsWarning(unittest.TestCase):
    """R11 A4 (R10 deferred): a corrupt JSON file must emit a distinct
    CorruptJSON warning instead of silently masquerading as fresh via
    mtime. Pre-R11 the except (OSError, json.JSONDecodeError): pass
    catch silently fell back to mtime, so a corrupt file showed up as
    fresh content and downstream _read_json(default={}) zeroed the
    subsystem with no operator signal."""

    def setUp(self):
        self.amd = _load_amd()
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="r11_a4_"))

    def tearDown(self):
        for f in self.tmp_dir.iterdir():
            try:
                f.unlink()
            except OSError:
                pass
        self.tmp_dir.rmdir()

    def test_corrupt_json_returns_corrupt_fallback_mtime_source(self):
        path = self.tmp_dir / "broken.json"
        path.write_text("{not valid json")
        ts, src = self.amd._freshness_timestamp_seconds(path)
        self.assertIsNotNone(ts)
        self.assertEqual(src, "corrupt_fallback_mtime",
            "R11 A4: corrupt JSON must surface as a distinct source label")

    def test_corrupt_json_emits_corrupt_json_warning(self):
        input_path = self.tmp_dir / "feed.json"
        ref_path = self.tmp_dir / "ref.json"
        input_path.write_text("{not valid json")
        ref_path.write_text(json.dumps({
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }))
        os.utime(input_path, (time.time(), time.time()))
        warnings_acc: list = []
        self.amd._check_freshness(
            input_path=input_path,
            reference_path=ref_path,
            max_age_hours=self.amd.STALENESS_MAX_AGE_HOURS,
            subsystem="r11_a4_test",
            warnings_acc=warnings_acc,
        )
        corrupt = [w for w in warnings_acc
                   if w.get("exception_class") == "CorruptJSON"]
        self.assertEqual(len(corrupt), 1,
            f"R11 A4: corrupt JSON must emit CorruptJSON warning; "
            f"got {warnings_acc!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
