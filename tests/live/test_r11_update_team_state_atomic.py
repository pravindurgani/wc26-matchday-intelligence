"""R11 D5 regression — update_team_state.py MUST write atomically
(tempfile + os.replace) AND include a `last_updated` ISO-8601 field.

Pre-R11 the file was written with bare `.write_text(json.dumps(...))`:
  - SIGKILL/OOM/disk-full mid-write leaves a partial JSON on disk
  - The simulator parses it with bare `json.loads` at 03_simulate.py:698
  - Partial JSON → JSONDecodeError → simulator crash mid-load
  - `last_updated` was missing → compute_input_hash always sees ""
  - A stalled writer that re-emits identical deltas is invisible to the
    hash gate

R11 D5 adds atomic write + last_updated timestamp.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))


def _load_uts():
    spec = importlib.util.spec_from_file_location(
        "uts_module", ROOT / "scripts" / "live" / "update_team_state.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestR11D5UpdateTeamStateAtomicAndLastUpdated(unittest.TestCase):
    def setUp(self):
        self.uts = _load_uts()
        self.src = (ROOT / "scripts" / "live"
                    / "update_team_state.py").read_text()

    def test_atomic_write_helper_present(self):
        """Static pin: _atomic_write_json must be defined using tempfile
        + os.replace, mirroring the run_live_update.py pattern."""
        self.assertIn("def _atomic_write_json", self.src,
            "R11 D5: _atomic_write_json helper must exist")
        self.assertIn("tempfile.NamedTemporaryFile", self.src,
            "R11 D5: atomic helper must use tempfile.NamedTemporaryFile")
        self.assertIn("os.replace", self.src,
            "R11 D5: atomic helper must use os.replace")

    def test_bare_write_text_removed(self):
        """The pre-R11 bare write_text pattern must be gone."""
        # The bare pattern was `.write_text(json.dumps(out, ...))`
        self.assertNotIn(
            '(LIVE / "live_team_state.json").write_text(json.dumps(',
            self.src,
            "R11 D5: bare write_text on live_team_state.json must be "
            "replaced with _atomic_write_json"
        )

    def test_atomic_writer_called_for_live_team_state(self):
        """Both write paths (empty + populated) must use the atomic helper."""
        # Count occurrences of _atomic_write_json(LIVE / "live_team_state.json"
        n = self.src.count("_atomic_write_json(LIVE / \"live_team_state.json\"")
        self.assertEqual(n, 2,
            f"R11 D5: expected exactly 2 atomic writes (empty + populated "
            f"branch); got {n}")

    def test_last_updated_field_emitted(self):
        """compute_input_hash reads last_updated from this file. Pre-R11
        the field was missing → empty string in the hash → stalled
        writers invisible. Both write branches must emit the field."""
        # Both `out = {...}` blocks should include "last_updated".
        n = self.src.count('"last_updated":')
        self.assertGreaterEqual(n, 2,
            f"R11 D5: both output dicts must include last_updated; "
            f"got {n} occurrence(s)")

    def test_empty_completed_writes_last_updated(self):
        """Functional test: when results_2026.json has no completed
        matches, the empty branch still writes a payload with
        last_updated set to a parseable ISO-8601 string."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            live = tdp / "data" / "live"
            raw = tdp / "data" / "raw"
            proc = tdp / "data" / "processed"
            for p in (live, raw, proc):
                p.mkdir(parents=True)
            (live / "results_2026.json").write_text(json.dumps(
                {"completed_matches": []}))
            (proc / "elo_ratings.json").write_text(json.dumps({}))
            (raw / "wc2026_config.json").write_text(json.dumps({
                "group_stage_schedule": []}))
            with patch.object(self.uts, "LIVE", live), \
                 patch.object(self.uts, "RAW", raw), \
                 patch.object(self.uts, "PROC", proc):
                self.uts.main()
            blob = json.loads((live / "live_team_state.json").read_text())
            self.assertIn("last_updated", blob,
                "R11 D5: empty-completed branch must still emit last_updated")
            # Must parse as ISO-8601.
            from datetime import datetime
            datetime.fromisoformat(blob["last_updated"].replace("Z", "+00:00"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
