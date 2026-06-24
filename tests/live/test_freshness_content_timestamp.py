"""
R9 P5 B1 regression — _check_freshness must use the producer-written
`generated_at` / `updated_at` JSON content timestamp as the primary
freshness source, NOT filesystem mtime.

Pre-R9, both `_check_freshness` (at apply_matchday_adjustments.py:177-178)
and `get_matchday_freshness_warnings` (at the same file's ~line 287) used
`path.stat().st_mtime`. In CI under `actions/checkout@v6`, every checked-
out file's mtime is reset to the checkout time (within microseconds), so
`age_delta_seconds ≈ 0` always passes the 6h threshold. A subsystem
producer (`data/live/injuries_2026.json` etc.) could be stale for DAYS
without firing `subsystem_stale` — defeating the Wave-2 S1 freshness
defense entirely on the actual production runner.

R9 P5 B1 introduces `_freshness_timestamp_seconds(path)` which reads
the JSON content's `generated_at` (or `updated_at`, `last_updated_utc`,
`last_updated`) field and parses it as ISO. mtime falls back only when
the file has no content timestamp — preserves bootstrap-replay paths.

This test simulates the CI scenario:
  - write a JSON file with a 10-day-old `generated_at`
  - set its mtime to "now" (mimicking actions/checkout)
  - assert the freshness guard correctly flags it as stale based on
    content, NOT on mtime
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


class TestFreshnessContentTimestamp(unittest.TestCase):
    def setUp(self):
        self.amd = _load_amd()
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="r9_p5_"))

    def tearDown(self):
        for f in self.tmp_dir.iterdir():
            try:
                f.unlink()
            except OSError:
                pass
        self.tmp_dir.rmdir()

    def _write_with_mtime(self, path: Path, content: dict, mtime_epoch: float):
        path.write_text(json.dumps(content))
        os.utime(path, (mtime_epoch, mtime_epoch))

    def test_helper_prefers_content_generated_at_over_mtime(self):
        """The primary contract: content `generated_at` wins over mtime."""
        path = self.tmp_dir / "input.json"
        # File generated 10 days ago (per content) but with mtime = "now"
        # (mimicking actions/checkout in CI).
        ten_days_ago = datetime.now(timezone.utc) - timedelta(days=10)
        self._write_with_mtime(
            path,
            {"generated_at": ten_days_ago.isoformat()},
            mtime_epoch=time.time(),  # fresh mtime
        )
        ts, source = self.amd._freshness_timestamp_seconds(path)
        self.assertIsNotNone(ts)
        self.assertEqual(source, "content",
            "R9 P5 B1: helper must prefer content timestamp over mtime")
        # The returned timestamp must be ≈ 10 days ago, NOT ≈ now.
        age_seconds = time.time() - ts
        self.assertGreater(age_seconds, 9 * 86400,
            "R9 P5 B1: helper returned mtime-fresh value instead of "
            "content-stale value — CI freshness guard would still be a no-op")

    def test_helper_falls_back_to_mtime_when_no_content_timestamp(self):
        """A file with no generated_at/updated_at field must fall back to
        mtime so bootstrap paths and replays still produce a usable value."""
        path = self.tmp_dir / "no_ts.json"
        old_mtime = time.time() - 3 * 86400
        self._write_with_mtime(path, {"some_other_field": 42}, mtime_epoch=old_mtime)
        ts, source = self.amd._freshness_timestamp_seconds(path)
        self.assertEqual(source, "mtime")
        self.assertAlmostEqual(ts, old_mtime, delta=1.0)

    def test_helper_handles_updated_at_for_results_file(self):
        """results_2026.json uses `updated_at` (not `generated_at`).
        The helper must accept both keys."""
        path = self.tmp_dir / "results.json"
        ts_iso = "2026-06-15T12:00:00+00:00"
        self._write_with_mtime(path, {"updated_at": ts_iso}, mtime_epoch=time.time())
        ts, source = self.amd._freshness_timestamp_seconds(path)
        self.assertEqual(source, "content")
        expected = datetime.fromisoformat(ts_iso).timestamp()
        self.assertEqual(ts, expected)

    def test_helper_handles_z_suffixed_iso(self):
        """ISO timestamps with 'Z' (Zulu) suffix must parse."""
        path = self.tmp_dir / "z_ts.json"
        self._write_with_mtime(
            path, {"generated_at": "2026-06-15T12:00:00Z"},
            mtime_epoch=time.time(),
        )
        ts, source = self.amd._freshness_timestamp_seconds(path)
        self.assertEqual(source, "content")
        expected = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(ts, expected)

    def test_check_freshness_uses_content_ts_in_ci_scenario(self):
        """End-to-end CI scenario: input file is 10 days stale (per content)
        but checkout-fresh (per mtime). Reference file is current (per content).
        _check_freshness must flag input as stale."""
        input_path = self.tmp_dir / "stale_input.json"
        ref_path = self.tmp_dir / "fresh_ref.json"
        now_iso = datetime.now(timezone.utc).isoformat()
        ten_days_ago_iso = (
            datetime.now(timezone.utc) - timedelta(days=10)
        ).isoformat()
        # Both files: mtime = NOW (simulating actions/checkout flatten).
        flat_mtime = time.time()
        self._write_with_mtime(
            input_path, {"generated_at": ten_days_ago_iso}, flat_mtime,
        )
        self._write_with_mtime(
            ref_path, {"updated_at": now_iso}, flat_mtime,
        )
        warnings_acc: list = []
        ok = self.amd._check_freshness(
            input_path=input_path,
            reference_path=ref_path,
            max_age_hours=self.amd.STALENESS_MAX_AGE_HOURS,
            subsystem="r9_p5_test",
            warnings_acc=warnings_acc,
        )
        self.assertFalse(ok,
            "R9 P5 B1: a 10-day-old input (per content) MUST be flagged "
            "stale even when mtime is checkout-fresh — otherwise the CI "
            "freshness guard is a no-op and Wave-2 S1 defense is bypassed")
        self.assertEqual(len(warnings_acc), 1)
        self.assertEqual(warnings_acc[0]["scope"], "freshness")
        self.assertEqual(warnings_acc[0]["exception_class"], "Stale")

    def test_check_freshness_still_passes_for_genuinely_fresh_input(self):
        """Negative case: same setup but input has a NOW generated_at —
        guard must NOT false-alarm (otherwise we lose all defense-in-depth
        in legitimate fresh-tick scenarios)."""
        input_path = self.tmp_dir / "fresh_input.json"
        ref_path = self.tmp_dir / "fresh_ref.json"
        now_iso = datetime.now(timezone.utc).isoformat()
        flat_mtime = time.time()
        self._write_with_mtime(
            input_path, {"generated_at": now_iso}, flat_mtime,
        )
        self._write_with_mtime(
            ref_path, {"updated_at": now_iso}, flat_mtime,
        )
        warnings_acc: list = []
        ok = self.amd._check_freshness(
            input_path=input_path,
            reference_path=ref_path,
            max_age_hours=self.amd.STALENESS_MAX_AGE_HOURS,
            subsystem="r9_p5_test",
            warnings_acc=warnings_acc,
        )
        self.assertTrue(ok)
        self.assertEqual(warnings_acc, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
