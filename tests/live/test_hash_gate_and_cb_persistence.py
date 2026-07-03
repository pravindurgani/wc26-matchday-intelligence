"""
2026-07-03 knockout-window fixes — pins + functional coverage.

Fix 1 (CB persistence): on a sim failure (orchestrator rc=1) the step used
to `exit "$rc"`, the commit step never ran, and the circuit-breaker
increment (data/live/circuit_breaker_state.json) plus the sim_failure
warning in dashboard/live_state.json were lost — the next tick checked
out CB=0 from origin, so CB_THRESHOLD=3 could never trip via CI. Now the
orchestrator records rc as a step output and exits 0, the commit step
runs under `always()`, and a final step re-raises the failure so the run
still shows red.

Fix 2 (dead hash gate): the early-exit gate compared compute_input_hash()
against the input_hash stored in data/processed/predictions_live.json,
but (a) that file was never committed (last landed 29 Jun), and (b) the
hashed payload included per-row `updated_at` stamps that fetch_results
rewrites on EVERY fetch — so the gate could never skip and every 10-min
tick burned a full 25k sim. Now the canonical file is in the commit
allow-list, the hash excludes the volatile per-row timestamp, and
run_live_update re-stamps the hash itself post-validation so compare and
stamp always use the same function.

Fix 4 (window edges): live-matchday.yml's date gate runs through
2026-07-21 00:00 UTC and the CF worker defaults widen to 0-23 UTC /
end 2026-07-20 (late-running matches + the 19 Jul final going long).

Run:
    python3 -m pytest tests/live/test_hash_gate_and_cb_persistence.py -q
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[2]
WF = ROOT / ".github" / "workflows" / "live-matchday.yml"

sys.path.insert(0, str(ROOT / "scripts" / "live"))

import run_live_update as rlu  # noqa: E402


def _live_steps() -> list[dict]:
    wf = yaml.safe_load(WF.read_text())
    return wf["jobs"]["live"]["steps"]


def _step_by_name(steps: list[dict], needle: str) -> dict:
    for s in steps:
        if needle in (s.get("name") or ""):
            return s
    raise AssertionError(f"no step whose name contains {needle!r}")


# ── Fix 1: circuit-breaker persistence across a failed sim tick ─────────
class TestOrchestratorFailureStillCommits(unittest.TestCase):
    def setUp(self):
        self.steps = _live_steps()
        self.yml = WF.read_text()

    def test_orchestrator_step_records_rc_and_never_fails(self):
        step = _step_by_name(self.steps, "Run live update orchestrator")
        self.assertEqual(step.get("id"), "orchestrator",
                         "commit + final steps address outputs via "
                         "steps.orchestrator — the id is load-bearing")
        run = step.get("run") or ""
        self.assertIn('echo "rc=$rc" >> "$GITHUB_OUTPUT"', run,
                      "rc must flow to the final fail step via step output")
        # Comment lines quote the old `exit "$rc"` pattern for context —
        # only executable lines count.
        code_lines = [ln.strip() for ln in run.splitlines()
                      if ln.strip() and not ln.strip().startswith("#")]
        self.assertNotIn('exit "$rc"', code_lines,
                         "the step must NOT re-raise rc directly — that "
                         "kills the commit step and loses the CB increment")
        self.assertTrue(run.rstrip().endswith("exit 0"),
                        "orchestrator step must always exit 0 so the "
                        "commit step ships circuit_breaker_state.json")

    def test_commit_step_runs_under_always_with_original_guards(self):
        step = _step_by_name(self.steps, "Commit updated JSON")
        cond = str(step.get("if") or "")
        self.assertIn("always()", cond)
        self.assertIn("steps.gate.outputs.skip != 'true'", cond,
                      "skip-gate semantics must be preserved exactly")
        self.assertIn("github.event.inputs.dry_run != 'true'", cond,
                      "dry-run guard must be preserved exactly")

    def test_final_step_fails_job_when_orchestrator_failed(self):
        step = _step_by_name(self.steps, "Surface orchestrator failure")
        cond = str(step.get("if") or "")
        self.assertIn("steps.orchestrator.outputs.rc != '0'", cond)
        self.assertIn("steps.orchestrator.outputs.rc != ''", cond,
                      "must not fire when the orchestrator was skipped")
        self.assertIn("exit 1", step.get("run") or "",
                      "the run must still go red after the commit")

    def test_commit_allow_list_includes_canonical_predictions_live(self):
        """Fix 2, workflow half: the gate reads input_hash from the
        canonical copy — it must reach origin or CI ticks always re-sim."""
        commit = _step_by_name(self.steps, "Commit updated JSON")
        self.assertIn("data/processed/predictions_live.json",
                      commit.get("run") or "",
                      "hash-gate fix: canonical predictions_live.json "
                      "must be in the git-add allow-list")

    def test_date_gate_end_extended_to_july_21(self):
        """Fix 4: the 19 Jul final + pens can finish past midnight UTC;
        the gate must stay open through the whole of 20 Jul."""
        self.assertIn("2026-07-21 00:00:00", self.yml)
        self.assertNotIn("2026-07-20 00:00:00", self.yml,
                         "stale END boundary would drop the final's "
                         "post-midnight FT lock")


# ── Fix 4: CF worker defaults ────────────────────────────────────────────
class TestWorkerWindowDefaults(unittest.TestCase):
    def test_worker_js_defaults_cover_full_day_through_july_20(self):
        src = (ROOT / "cf-worker" / "worker.js").read_text()
        self.assertIn('env.WINDOW_END_UTC_DATE   || "2026-07-20"', src)
        self.assertIn('env.WINDOW_HOUR_FROM ?? "0"', src)
        self.assertNotIn('?? "4"', src,
                         "hour window must default to the full 0-23 day — "
                         "04-23 UTC missed 00:00-03:00 UTC kickoffs")

    def test_wrangler_vars_match_worker_defaults(self):
        toml = (ROOT / "cf-worker" / "wrangler.toml").read_text()
        self.assertIn('WINDOW_END_UTC_DATE = "2026-07-20"', toml)
        self.assertIn('WINDOW_HOUR_FROM = "0"', toml)


# ── Fix 2: functional hash-gate behavior ────────────────────────────────
@pytest.fixture
def gate_dirs(tmp_path, monkeypatch):
    """Point run_live_update's module paths at a tmp twin of the repo."""
    live = tmp_path / "live"
    proc = tmp_path / "processed"
    dash = tmp_path / "dashboard"
    for d in (live, proc, dash):
        d.mkdir()
    monkeypatch.setattr(rlu, "LIVE", live)
    monkeypatch.setattr(rlu, "PROC", proc)
    monkeypatch.setattr(rlu, "DASH", dash)
    return live, proc, dash


def _results_payload(updated_at: str, home_score: int = 2) -> dict:
    return {
        "completed_matches": [
            {"m": 1, "home": "Mexico", "away": "South Africa",
             "home_score": home_score, "away_score": 0, "winner": "home",
             "status": "FT", "updated_at": updated_at},
        ],
        "in_play": [],
        "warnings": [],
        "updated_at": updated_at,
    }


def test_hash_ignores_per_row_updated_at_churn(gate_dirs) -> None:
    """fetch_results re-stamps updated_at on every row every tick — the
    hash must not move when ONLY that timestamp changed, or the early
    exit can never fire and every tick re-sims (~9 min)."""
    live, _, _ = gate_dirs
    p = live / "results_2026.json"
    p.write_text(json.dumps(_results_payload("2026-07-03T10:00:00+00:00")))
    h1 = rlu.compute_input_hash()
    p.write_text(json.dumps(_results_payload("2026-07-03T10:10:00+00:00")))
    h2 = rlu.compute_input_hash()
    assert h1 == h2, (
        "hash churned on a pure updated_at re-stamp — the input-hash "
        "early-exit gate is dead again"
    )


def test_hash_still_moves_on_real_input_changes(gate_dirs) -> None:
    """Excluding the volatile timestamp must NOT blind the gate to real
    changes: scores (corrections) and new rows still bump the hash."""
    live, _, _ = gate_dirs
    p = live / "results_2026.json"
    p.write_text(json.dumps(_results_payload("2026-07-03T10:00:00+00:00")))
    h1 = rlu.compute_input_hash()
    # Score correction, same timestamp → must change.
    p.write_text(json.dumps(
        _results_payload("2026-07-03T10:00:00+00:00", home_score=3)))
    h2 = rlu.compute_input_hash()
    assert h1 != h2, "score correction must invalidate the gate"


def test_stamp_input_hash_round_trips_through_gate(gate_dirs) -> None:
    """stamp_input_hash writes with the SAME function the gate compares
    with — after a stamp, an unchanged-input tick reads back an equal
    hash (this is the skip condition in main())."""
    live, proc, _ = gate_dirs
    (live / "results_2026.json").write_text(
        json.dumps(_results_payload("2026-07-03T10:00:00+00:00")))
    (proc / "predictions_live.json").write_text(json.dumps(
        {"team_predictions": [], "input_hash": "stale_sim_stamped_hash"}))
    assert rlu.stamp_input_hash() is True
    # Simulate the next tick: fetcher re-stamps updated_at only.
    (live / "results_2026.json").write_text(
        json.dumps(_results_payload("2026-07-03T10:10:00+00:00")))
    assert rlu.read_last_input_hash() == rlu.compute_input_hash(), (
        "stamp/compare drifted apart — the gate would re-sim forever"
    )


def test_stamp_is_nonfatal_when_predictions_missing(gate_dirs) -> None:
    """A missing canonical file degrades to 'no stamp' (re-sim next
    tick) — never an exception into the orchestrator tick."""
    assert rlu.stamp_input_hash() is False


if __name__ == "__main__":
    unittest.main(verbosity=2)
