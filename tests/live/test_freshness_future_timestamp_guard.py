"""
R10 Q4 regression — _check_freshness must flag a future-dated input
content timestamp as a `FutureTimestamp` warning (not silently treat it
as "fresh" forever).

R9 P5 B1 introduced content-timestamp-preferred freshness reads. But
it didn't guard against the case where the input's `generated_at` is
clock-skewed INTO THE FUTURE — Docker host with bad NTP, replay against
a hard-coded future date, manual edit, etc. Pre-R10 Q4:
    age_delta_seconds = ref_ts - input_ts  # negative on future input
    if age_delta_seconds <= max_age_hours * 3600: return True  # always
A future-stamped input would pass the freshness gate forever — the
subsystem could be stale indefinitely without firing the warning.

R10 Q4 (apply_matchday_adjustments.py:_check_freshness) adds an
explicit `FutureTimestamp` exception_class warning when the input is
more than `max_age_hours` in the future, so operators see the broken-
clock signal instead of silent acceptance.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))


def _load_amd():
    spec = importlib.util.spec_from_file_location(
        "amd_module", ROOT / "scripts" / "live" / "apply_matchday_adjustments.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestFreshnessFutureTimestampGuard(unittest.TestCase):
    def setUp(self):
        self.amd = _load_amd()
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="r10_q4_"))

    def tearDown(self):
        for f in self.tmp_dir.iterdir():
            try:
                f.unlink()
            except OSError:
                pass
        self.tmp_dir.rmdir()

    def _write(self, path: Path, ts_field: str, ts_value: str):
        path.write_text(json.dumps({ts_field: ts_value}))
        # Set mtime to now so the mtime fallback doesn't dominate.
        os.utime(path, (time.time(), time.time()))

    def test_future_input_triggers_FutureTimestamp_warning(self):
        """An input with generated_at 24h in the future relative to the
        reference's updated_at must fire the FutureTimestamp warning,
        not be treated as fresh."""
        input_path = self.tmp_dir / "future_input.json"
        ref_path = self.tmp_dir / "now_ref.json"
        now_dt = datetime.now(timezone.utc)
        future_dt = now_dt + timedelta(hours=24)
        self._write(input_path, "generated_at", future_dt.isoformat())
        self._write(ref_path, "updated_at", now_dt.isoformat())
        warnings_acc: list = []
        ok = self.amd._check_freshness(
            input_path=input_path,
            reference_path=ref_path,
            max_age_hours=self.amd.STALENESS_MAX_AGE_HOURS,
            subsystem="r10_q4_test",
            warnings_acc=warnings_acc,
        )
        self.assertFalse(ok,
            "R10 Q4: 24h-future input must NOT pass the freshness gate")
        self.assertEqual(len(warnings_acc), 1)
        self.assertEqual(warnings_acc[0]["exception_class"], "FutureTimestamp",
            "R10 Q4: future-dated input must emit a distinct "
            "FutureTimestamp signal so operators see the broken-clock "
            "scenario, not a generic Stale")
        self.assertIn("IN THE FUTURE", warnings_acc[0]["message"])

    def test_small_forward_skew_within_threshold_tolerated(self):
        """Sub-threshold forward skew (e.g. 1h ahead with 6h threshold)
        is NOT flagged — typical clock-jitter between two writes that
        complete within a few seconds of each other but parse as
        slightly-future. False-positives here would defeat the freshness
        signal."""
        input_path = self.tmp_dir / "slight_future.json"
        ref_path = self.tmp_dir / "now_ref.json"
        now_dt = datetime.now(timezone.utc)
        slight_future_dt = now_dt + timedelta(hours=1)  # < 6h threshold
        self._write(input_path, "generated_at", slight_future_dt.isoformat())
        self._write(ref_path, "updated_at", now_dt.isoformat())
        warnings_acc: list = []
        ok = self.amd._check_freshness(
            input_path=input_path,
            reference_path=ref_path,
            max_age_hours=self.amd.STALENESS_MAX_AGE_HOURS,
            subsystem="r10_q4_test",
            warnings_acc=warnings_acc,
        )
        self.assertTrue(ok,
            "R10 Q4: small forward skew (1h within 6h threshold) must "
            "stay tolerated — clock-jitter between sequential writes is "
            "normal and shouldn't cry-wolf")
        self.assertEqual(warnings_acc, [])

    def test_normal_stale_still_emits_stale_not_future(self):
        """Negative case: a genuinely stale input must still emit the
        Stale exception_class (R9 P5 B1 behavior preserved)."""
        input_path = self.tmp_dir / "stale_input.json"
        ref_path = self.tmp_dir / "now_ref.json"
        now_dt = datetime.now(timezone.utc)
        stale_dt = now_dt - timedelta(hours=24)
        self._write(input_path, "generated_at", stale_dt.isoformat())
        self._write(ref_path, "updated_at", now_dt.isoformat())
        warnings_acc: list = []
        ok = self.amd._check_freshness(
            input_path=input_path,
            reference_path=ref_path,
            max_age_hours=self.amd.STALENESS_MAX_AGE_HOURS,
            subsystem="r10_q4_test",
            warnings_acc=warnings_acc,
        )
        self.assertFalse(ok)
        self.assertEqual(warnings_acc[0]["exception_class"], "Stale",
            "R10 Q4: backward-stale must still emit Stale (not "
            "FutureTimestamp). R10 Q4 only adds the future-side guard.")

    def test_normal_fresh_still_passes(self):
        """Sanity: a fresh input still passes (no false-positive future
        flag when timestamps are roughly equal)."""
        input_path = self.tmp_dir / "fresh.json"
        ref_path = self.tmp_dir / "ref.json"
        now_iso = datetime.now(timezone.utc).isoformat()
        self._write(input_path, "generated_at", now_iso)
        self._write(ref_path, "updated_at", now_iso)
        warnings_acc: list = []
        ok = self.amd._check_freshness(
            input_path=input_path,
            reference_path=ref_path,
            max_age_hours=self.amd.STALENESS_MAX_AGE_HOURS,
            subsystem="r10_q4_test",
            warnings_acc=warnings_acc,
        )
        self.assertTrue(ok)
        self.assertEqual(warnings_acc, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
