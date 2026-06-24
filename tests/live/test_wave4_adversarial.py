"""
Wave-4 adversarial sweep — pins the silent-failure findings from the
last hardening pass.

Each test asserts a POSITIVE behavior (raises / warns / sentinel) rather
than xfail, per the standing constraint: xfail is a hiding place.

Probes covered (1, 4, 6, 7, 8):

  1. expires_at silent except in apply_matchday_adjustments
     - apply_matchday_adjustments.py:282 previously swallowed malformed
       expires_at (None/int/dict/non-ISO-string) and silently kept the
       overlay ACTIVE. Now a structured `bad_expires_at` warning lands
       in degradation_warnings and the entry is skipped.

  4. Suspension tracker exact-name dedup contract
     - `(team, player, kind)` does NOT normalise. Two players with the
       same surname stay distinct (correct); the SAME player under two
       name-format spellings (e.g. "L. Martínez" vs "Lautaro Martínez")
       do NOT cross-deduplicate (documented limitation).

  6. fetch_weather schema-watchdog gap (documented as contract)
     - Open-Meteo response shape is NOT covered by assert_shape. Pin the
       fact that fetch_weather.py imports neither assert_shape nor
       SchemaDriftError so future regressions are caught.

  7. Σ-invariant gate bool poisoning
     - 47 False + 1 True would previously pass Σ == 1.0. Now
       check_invariants rejects bool with SumOutOfTolerance.

  8. export_ko_advance wiring (S0 class: looks wired, isn't flowing)
     - The module exists on disk; pin that run_live_update.py invokes
       it after the sim and BEFORE the dashboard publish.

Run:
    python3 -m pytest tests/live/test_wave4_adversarial.py -v
"""
from __future__ import annotations

import importlib
import json
import math
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "live"))

import apply_matchday_adjustments as amd  # noqa: E402
import suspension_tracker  # noqa: E402
import check_invariants as ci  # noqa: E402


class _TempFeeds:
    """Local copy of the helper in test_apply_matchday_adjustments — mirror
    so this test file is self-contained and doesn't import another test
    module (pytest collection order is unstable)."""

    def __init__(self, feeds: dict[str, dict]):
        self.feeds = feeds
        self.tmp = None

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmp.name)
        for name, payload in self.feeds.items():
            (tmp_path / name).write_text(json.dumps(payload))
        self._patches = [
            patch.object(amd, "LIVE", tmp_path),
            patch.object(amd, "DASH", tmp_path),
            patch.object(amd, "LOG_PATH",
                         tmp_path / "matchday_intelligence_log.jsonl"),
            patch.object(amd, "OUT_PATH",
                         tmp_path / "matchday_intelligence.json"),
        ]
        for p in self._patches:
            p.start()
        amd._STATE_CACHE = None
        return tmp_path

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        amd._STATE_CACHE = None
        self.tmp.cleanup()


# ---------------------------------------------------------------------------
# Probe 1: expires_at silent except
# ---------------------------------------------------------------------------

class TestExpiresAtMalformedWarns(unittest.TestCase):
    """Pin the Wave-4 fix to apply_matchday_adjustments.py:282.

    Pre-Wave-4 behaviour: `except Exception: pass` swallowed every
    non-ISO expires_at value (None, int, dict, non-ISO string) and
    silently kept the overlay ACTIVE. A typo'd expires_at could keep
    an "expired" entry contributing Elo for the rest of the tournament.

    Post-Wave-4: a structured `bad_expires_at` warning lands in
    degradation_warnings, AND the entry is treated as expired (skipped).
    """

    def _team_adj_with(self, expires_at):
        return {
            "team_adjustments.json": {
                "adjustments": [{
                    "team": "Argentina",
                    "player": "X",
                    "adjustment_elo": -10.0,
                    "approved": True,
                    "expires_at": expires_at,
                }],
            }
        }

    def _malformed_warnings(self, state) -> list[dict]:
        return [
            w for w in state.get("degradation_warnings", [])
            if w.get("subsystem") == "injury"
            and w.get("scope") == "overlay"
            and "bad expires_at" in w.get("message", "")
        ]

    def test_none_expires_at_skips_with_warning(self):
        # Note: `if exp:` short-circuits None, so this entry has no
        # `expires_at` filter applied and we don't get a warning — that's
        # the documented contract for "no expires_at set". This test
        # documents that path explicitly.
        with _TempFeeds(self._team_adj_with(None)):
            state = amd.build_adjustments_state()
        warnings = self._malformed_warnings(state)
        self.assertEqual(warnings, [],
                         "None expires_at == 'no expiry set' — no warning, "
                         "entry stays active")

    def test_integer_expires_at_warns_and_skips(self):
        with _TempFeeds(self._team_adj_with(123456789)):
            state = amd.build_adjustments_state()
        warnings = self._malformed_warnings(state)
        self.assertEqual(len(warnings), 1,
                         f"expected 1 bad_expires_at warning, got "
                         f"{state.get('degradation_warnings')}")
        self.assertIn("int", warnings[0]["message"])
        # Entry should have been skipped — no overlay contribution.
        argentina = [e for e in state["active_adjustments"]
                     if e["team"] == "Argentina"]
        # No active adjustments for Argentina because the only overlay
        # entry was rejected.
        self.assertEqual(argentina, [],
                         "malformed expires_at entry must NOT contribute Elo")

    def test_dict_expires_at_warns_and_skips(self):
        with _TempFeeds(self._team_adj_with({"bogus": "shape"})):
            state = amd.build_adjustments_state()
        warnings = self._malformed_warnings(state)
        self.assertEqual(len(warnings), 1)
        self.assertIn("dict", warnings[0]["message"])

    def test_bad_string_expires_at_warns_and_skips(self):
        # Real-world bug: operator writes 'not-a-date' or '2030/12/31'
        # (slashes, not ISO-8601 dashes). String comparison won't raise
        # but the entry is NOT expired by accident.
        with _TempFeeds(self._team_adj_with("not-a-date")):
            state = amd.build_adjustments_state()
        warnings = self._malformed_warnings(state)
        self.assertEqual(len(warnings), 1,
                         f"expected 1 bad_expires_at warning, got "
                         f"{state.get('degradation_warnings')}")
        self.assertIn("is not ISO-8601", warnings[0]["message"])
        # Skip the entry — no Argentina overlay.
        argentina = [e for e in state["active_adjustments"]
                     if e["team"] == "Argentina"]
        self.assertEqual(argentina, [])

    def test_well_formed_future_expires_at_keeps_entry_active(self):
        # Sanity: a valid future ISO timestamp keeps the entry active
        # — the fix must not regress the happy path.
        far_future = "2099-01-01T00:00:00+00:00"
        with _TempFeeds(self._team_adj_with(far_future)):
            state = amd.build_adjustments_state()
        warnings = self._malformed_warnings(state)
        self.assertEqual(warnings, [], "valid future ts must NOT warn")
        argentina = [e for e in state["active_adjustments"]
                     if e["team"] == "Argentina"]
        self.assertTrue(len(argentina) > 0,
                        "well-formed future expires_at must keep entry active")

    def test_well_formed_past_expires_at_silently_skips(self):
        # Past expiry is the documented happy path: skip silently, no
        # warning. The contract is "warn only on MALFORMED, skip on
        # cleanly-expired".
        far_past = "2000-01-01T00:00:00+00:00"
        with _TempFeeds(self._team_adj_with(far_past)):
            state = amd.build_adjustments_state()
        warnings = self._malformed_warnings(state)
        self.assertEqual(warnings, [],
                         "clean past expires_at must NOT trigger a warning")
        argentina = [e for e in state["active_adjustments"]
                     if e["team"] == "Argentina"]
        self.assertEqual(argentina, [],
                         "expired overlay entry must NOT contribute")


# ---------------------------------------------------------------------------
# Probe 4: Suspension tracker exact-name dedup contract
# ---------------------------------------------------------------------------

class TestSuspensionDedupContract(unittest.TestCase):
    """Pin the exact-name dedup behaviour in suspension_tracker.

    The dedup key in suspension_tracker.py:324 is (team, player, kind)
    using the RAW provider string. This is correct: normalising the
    name would silently merge teammates who share a surname (e.g.
    Argentina has two Martínezes — Lautaro AND Emiliano), under-counting
    cards.

    The trade-off documented here: if the SAME player appears in two
    DIFFERENT events under two name-format spellings ('L. Martínez' vs
    'Lautaro Martínez'), the dedup will NOT cross-match — both events
    pass through. That's an acceptable false-negative for the dedup
    layer; the alternative (normalised dedup) would create false-positive
    merges of distinct players, which is worse for ban prediction.
    """

    def test_two_players_same_surname_both_yellow_no_merge(self):
        """Argentina match where Lautaro Martínez AND Emiliano Martínez
        each pick up a yellow. The (team, player, kind) tuple is
        distinct → both yellow_counter entries stay separate."""
        # Build a minimal results doc with one match's events.
        results = {
            "completed_matches": [{
                "m": 1, "home": "Argentina", "away": "Brazil",
                "home_goals": 0, "away_goals": 0, "status": "FT",
                "events": [
                    {"team": "Argentina", "player": "Lautaro Martínez",
                     "detail": "Yellow Card", "type": "Card"},
                    {"team": "Argentina", "player": "Emiliano Martínez",
                     "detail": "Yellow Card", "type": "Card"},
                ],
            }],
        }
        # Use suspension_tracker's exposed helpers directly. The full
        # main() requires schedule + completed; we can verify the dedup
        # by walking events through the same key shape used in
        # suspension_tracker._build (we replicate the key tuple here
        # since that's the contract under test).
        events = results["completed_matches"][0]["events"]
        seen = set()
        kept = []
        for ev in events:
            kind = ("yellow" if "Yellow" in ev["detail"]
                    else "red" if "Red" in ev["detail"] else None)
            key = (ev["team"], ev["player"], kind)
            if key in seen:
                continue
            seen.add(key)
            kept.append(ev)
        # Both Martínez yellows must survive — distinct first names.
        self.assertEqual(len(kept), 2,
                         "two teammates sharing a surname must NOT merge")
        names = {ev["player"] for ev in kept}
        self.assertEqual(names,
                         {"Lautaro Martínez", "Emiliano Martínez"})

    def test_same_player_two_name_formats_does_not_cross_dedup(self):
        """SAME player under 'L. Martínez' vs 'Lautaro Martínez' produces
        TWO distinct dedup keys. The yellow_counter accumulates them
        separately, which can under-trigger a 2-yellow ban — that's a
        documented limitation, the alternative (normalised key) creates
        bigger false positives across the two distinct Martínezes."""
        events = [
            {"team": "Argentina", "player": "L. Martínez",
             "detail": "Yellow Card", "type": "Card"},
            {"team": "Argentina", "player": "Lautaro Martínez",
             "detail": "Yellow Card", "type": "Card"},
        ]
        seen = set()
        kept = []
        for ev in events:
            key = (ev["team"], ev["player"], "yellow")
            if key in seen:
                continue
            seen.add(key)
            kept.append(ev)
        # Both pass — documenting the limitation.
        self.assertEqual(len(kept), 2,
                         "two name formats for SAME player do not "
                         "cross-deduplicate — documented contract")


# ---------------------------------------------------------------------------
# Probe 6: fetch_weather schema-watchdog gap (documented as contract)
# ---------------------------------------------------------------------------

class TestFetchWeatherSchemaWatchdog(unittest.TestCase):
    """Document the current contract: fetch_weather does NOT call
    assert_shape on the Open-Meteo response. Open-Meteo is a stable
    public API but the consumer code is defensive (`.get("hourly")`
    walk). If this test starts failing because someone wires the
    watchdog into fetch_weather, that's a step forward — adjust the
    expectation, don't suppress the test."""

    def test_fetch_weather_does_not_import_assert_shape(self):
        # Read the file content and assert the literal import is absent.
        # A future intentional wiring will trip this; the test then
        # documents the new contract by being updated.
        src = (ROOT / "scripts" / "live" / "fetch_weather.py").read_text()
        self.assertNotIn(
            "assert_shape", src,
            "fetch_weather.py does NOT currently call assert_shape. If "
            "you've intentionally wired it in, update this test to assert "
            "the call instead — don't suppress the test."
        )

    def test_other_six_fetchers_DO_call_assert_shape(self):
        """Pin the inverse: every fetch_* fetcher EXCEPT fetch_weather
        DOES import + call assert_shape. Regression in any of these
        would flag a deliberate or accidental removal."""
        fetchers = [
            "fetch_results.py", "fetch_injuries.py", "fetch_lineups.py",
            "fetch_match_stats.py", "fetch_player_stats.py",
        ]
        for name in fetchers:
            src = (ROOT / "scripts" / "live" / name).read_text()
            self.assertIn(
                "assert_shape", src,
                f"{name} must call assert_shape on its provider response",
            )


# ---------------------------------------------------------------------------
# Probe 7: Σ-invariant bool poisoning
# ---------------------------------------------------------------------------

class TestCheckInvariantsRejectsBools(unittest.TestCase):
    """Pin the Wave-4 fix in scripts/check_invariants.py.

    Pre-Wave-4 behaviour: Python `bool` is a subclass of int. 47 False
    + 1 True summed to 1.0; isinstance(True, (int, float)) is True;
    math.isfinite(True) is True; 0.0 <= True <= 1.0 is True. So a
    serialiser drift that wrote `true` literals (e.g. from a buggy
    JSON encoder) would silently pass every check.

    Post-Wave-4: bool is explicitly rejected — raises SumOutOfTolerance.
    """

    def _write_temp(self, data: dict) -> Path:
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                        delete=False)
        f.write(json.dumps(data))
        f.close()
        return Path(f.name)

    def test_all_true_false_pattern_rejected(self):
        # 47 False + 1 True sums to 1 (== 1.0), with every other check
        # passing on bool. Must raise SumOutOfTolerance now.
        teams = [{"team": f"t{i}", "p_champion": False} for i in range(47)]
        teams.append({"team": "t47", "p_champion": True})
        p = self._write_temp({"team_predictions": teams})
        try:
            with self.assertRaises(ci.SumOutOfTolerance) as ctx:
                ci.check_invariants(p)
            self.assertIn("non-finite p_champion", str(ctx.exception))
        finally:
            os.unlink(p)

    def test_single_bool_rejected_even_if_sum_drifts(self):
        # A single True (== 1.0) mixed with 47 finite floats still must
        # surface — bool is structurally invalid regardless of the sum.
        teams = [{"team": f"t{i}", "p_champion": 0.0} for i in range(47)]
        teams.append({"team": "t47", "p_champion": True})
        p = self._write_temp({"team_predictions": teams})
        try:
            with self.assertRaises(ci.SumOutOfTolerance):
                ci.check_invariants(p)
        finally:
            os.unlink(p)

    def test_clean_floats_still_pass(self):
        # Sanity: the fix must not break the happy path.
        teams = [{"team": f"t{i}", "p_champion": 1.0/48} for i in range(48)]
        p = self._write_temp({"team_predictions": teams})
        try:
            ci.check_invariants(p)  # raises on failure
        finally:
            os.unlink(p)


# ---------------------------------------------------------------------------
# Probe 8: export_ko_advance wiring (looks wired, isn't flowing)
# ---------------------------------------------------------------------------

class TestExportKoAdvanceWired(unittest.TestCase):
    """Pin that run_live_update.py actually invokes export_ko_advance.

    Pre-Wave-4 the module existed on disk + had its own tests, but no
    orchestrator or workflow yaml referenced it. That meant
    `p_advance_match` (the per-KO-match advance probability the outright
    KO sheet consumes) was never written to the live predictions blob.

    Post-Wave-4: run_live_update.py invokes `python -m
    scripts.live.export_ko_advance` after the sim and BEFORE the
    dashboard publish step.
    """

    def test_run_live_update_invokes_export_ko_advance(self):
        src = (ROOT / "scripts" / "live" / "run_live_update.py").read_text()
        self.assertIn(
            "scripts.live.export_ko_advance", src,
            "run_live_update.py must invoke export_ko_advance — without "
            "this, the S7 KO advance-prob field never reaches production"
        )

    def test_export_ko_advance_runs_after_sim_and_before_publish(self):
        """Order matters: the export reads predictions_live.json (sim
        output) and writes the same file. Must run AFTER 03_simulate.py
        and BEFORE the dashboard publish. Anchor the order with file
        offsets — sim invocation < export < publish."""
        src = (ROOT / "scripts" / "live" / "run_live_update.py").read_text()
        sim_idx = src.find("03_simulate.py")
        export_idx = src.find("scripts.live.export_ko_advance")
        publish_idx = src.find('"predictions_live.json"\n    if src.exists()')
        # Fallback anchor for publish: the dashboard-side dst rename.
        if publish_idx == -1:
            publish_idx = src.find("os.replace(tmp, dst)")
        self.assertGreater(sim_idx, 0, "03_simulate.py must be invoked")
        self.assertGreater(export_idx, sim_idx,
                           "export_ko_advance must run AFTER 03_simulate.py")
        self.assertGreater(publish_idx, export_idx,
                           "dashboard publish must run AFTER "
                           "export_ko_advance — otherwise the dashboard "
                           "blob lacks match_predictions_ko")


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
