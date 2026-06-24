"""Round 6 — schema-drift watchdog wiring in fetch_results.py.

Two production HTTP responses are now passed through assert_shape() before the
fetch continues:

  - GET /fixtures            (in fetch_api_football)                  -> apifootball_fixtures.shape.json
  - GET /fixtures/events     (in fetch_apifootball_events_for_fixture) -> apifootball_fixtures_events.shape.json

These tests pin the soft-mode contract:

  - A payload whose shape matches the captured baseline -> assert_shape()
    returns True silently, the fetch proceeds normally, NO warning is logged.

  - A payload whose shape DRIFTS (e.g. a renamed key) -> assert_shape() logs
    a `[schema_watchdog] SHAPE DRIFT` WARNING, returns False, BUT the fetch
    STILL returns its normalised payload. The tick MUST NOT crash on drift.

No real network calls: `http_get_json` is mocked.
"""
from __future__ import annotations

import copy
import json
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "live"))

import fetch_results  # noqa: E402
from fetch_results import (  # noqa: E402
    fetch_api_football,
    fetch_apifootball_events_for_fixture,
)

SAMPLES = ROOT / "tests" / "live" / "provider_samples"
BASELINES = ROOT / "data" / "live" / "_provider_schemas"


def _load_sample(name: str) -> dict:
    return json.loads((SAMPLES / name).read_text())


def _drift_renamed_key(payload: dict) -> dict:
    """Return a deep copy with a single key renamed inside response[0]."""
    out = copy.deepcopy(payload)
    if out.get("response") and isinstance(out["response"], list) and out["response"]:
        first = out["response"][0]
        if isinstance(first, dict):
            # Rename a known top-level key to simulate a provider rename.
            if "fixture" in first:
                first["match"] = first.pop("fixture")
            elif "type" in first:
                first["event_type"] = first.pop("type")
            else:
                first["new_drift_key"] = first.pop(next(iter(first)))
    return out


# ---------------------------------------------------------------------------
# /fixtures  ->  fetch_api_football
# ---------------------------------------------------------------------------

class TestFixturesHookSoftMode(unittest.TestCase):
    """Cover the assert_shape() hook on the /fixtures endpoint."""

    def setUp(self):
        self.api_key = "x" * 32
        # Real captured shape that the watchdog baseline was hashed from.
        self.payload_match = _load_sample("api_football_fixture_response.json")

    def test_matching_shape_emits_no_drift_warning(self):
        """Baseline-shaped payload -> no SHAPE DRIFT warning, fetch proceeds."""
        with mock.patch.object(fetch_results, "http_get_json",
                               return_value=self.payload_match) as mocked:
            with self.assertLogs("schema_watchdog", level="WARNING") as cm:
                # Emit a placeholder so assertLogs has something to capture
                # even if the watchdog stays silent (the actual assertion is
                # below: the captured text must NOT contain SHAPE DRIFT).
                logging.getLogger("schema_watchdog").warning(
                    "test_placeholder_no_drift_expected")
                out = fetch_api_football(self.api_key, dry_run=True)
        self.assertTrue(mocked.called, "http_get_json must have been invoked")
        joined = "\n".join(cm.output)
        self.assertNotIn("SHAPE DRIFT", joined,
                         "matching payload must not trigger a drift warning")
        # The fetch path itself must return normally — it's a list.
        self.assertIsInstance(out, list)

    def test_drifted_shape_logs_warning_but_fetch_returns(self):
        """Renamed key -> WARNING is logged, fetch STILL returns (no crash)."""
        drifted = _drift_renamed_key(self.payload_match)
        # Sanity: drift is real — the shape must actually differ now.
        from scripts.live._schema_watchdog import compute_shape_hash
        self.assertNotEqual(compute_shape_hash(drifted),
                            compute_shape_hash(self.payload_match))

        with mock.patch.object(fetch_results, "http_get_json",
                               return_value=drifted):
            with self.assertLogs("schema_watchdog", level="WARNING") as cm:
                # Call must NOT raise — soft mode is the entire point.
                out = fetch_api_football(self.api_key, dry_run=True)

        joined = "\n".join(cm.output)
        self.assertIn("SHAPE DRIFT", joined,
                      "drifted payload must trigger a SHAPE DRIFT warning")
        self.assertIn("apifootball_fixtures.shape.json", joined,
                      "warning must name the baseline file")
        # Fetch returned a list (possibly empty after the rename strips the
        # 'fixture' block — but it MUST return, not crash).
        self.assertIsInstance(out, list)


# ---------------------------------------------------------------------------
# /fixtures/events -> fetch_apifootball_events_for_fixture
# ---------------------------------------------------------------------------

class TestEventsHookSoftMode(unittest.TestCase):
    """Cover the assert_shape() hook on the /fixtures/events endpoint."""

    def setUp(self):
        self.api_key = "x" * 32
        self.fixture_id = "1000001"
        self.payload_match = _load_sample("apifootball_events_sample.json")

    def test_matching_shape_emits_no_drift_warning(self):
        with mock.patch.object(fetch_results, "http_get_json",
                               return_value=self.payload_match):
            with self.assertLogs("schema_watchdog", level="WARNING") as cm:
                logging.getLogger("schema_watchdog").warning(
                    "test_placeholder_no_drift_expected")
                events, warn = fetch_apifootball_events_for_fixture(
                    self.api_key, self.fixture_id)
        joined = "\n".join(cm.output)
        self.assertNotIn("SHAPE DRIFT", joined)
        self.assertIsNone(warn, "matching payload must not produce a warning dict")
        # The events list is normalised — must be non-empty for our sample.
        self.assertIsInstance(events, list)
        self.assertGreater(len(events), 0)

    def test_drifted_shape_logs_warning_but_events_return(self):
        drifted = _drift_renamed_key(self.payload_match)
        from scripts.live._schema_watchdog import compute_shape_hash
        self.assertNotEqual(compute_shape_hash(drifted),
                            compute_shape_hash(self.payload_match))

        with mock.patch.object(fetch_results, "http_get_json",
                               return_value=drifted):
            with self.assertLogs("schema_watchdog", level="WARNING") as cm:
                # Must NOT raise.
                events, warn = fetch_apifootball_events_for_fixture(
                    self.api_key, self.fixture_id)

        joined = "\n".join(cm.output)
        self.assertIn("SHAPE DRIFT", joined)
        self.assertIn("apifootball_fixtures_events.shape.json", joined,
                      "warning must name the events baseline file")
        # Returned tuple shape is preserved — soft mode does not corrupt the
        # contract. The normalised events list may be empty (the rename
        # demolished `type`), but the call returned cleanly.
        self.assertIsInstance(events, list)
        self.assertIsNone(warn,
                          "soft-mode drift must NOT populate the warning dict")


# ---------------------------------------------------------------------------
# Module-level wiring sanity checks
# ---------------------------------------------------------------------------

class TestWiringIsPresent(unittest.TestCase):
    """Guard against accidental un-wiring during future refactors."""

    def test_module_imports_assert_shape(self):
        self.assertTrue(hasattr(fetch_results, "assert_shape"),
                        "fetch_results must import assert_shape at module top")

    def test_baseline_directory_constant_resolves(self):
        self.assertTrue(fetch_results._SCHEMA_BASELINE_DIR.is_dir(),
                        "_SCHEMA_BASELINE_DIR must point at an existing dir")
        # Both wired baselines must exist on disk.
        for fn in ("apifootball_fixtures.shape.json",
                   "apifootball_fixtures_events.shape.json"):
            self.assertTrue(
                (fetch_results._SCHEMA_BASELINE_DIR / fn).exists(),
                f"required baseline missing: {fn}",
            )


if __name__ == "__main__":
    unittest.main()
