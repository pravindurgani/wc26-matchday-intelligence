"""
Schema check for .github/workflows/matchday-intel-slow.yml.

Wave-2 S1 (origin/main had never seen the Phase 2/4/6 producer outputs
because the slow workflow never ran them). This test pins the workflow's
step ordering AND its git-add allow-list so a future YAML edit can't
silently drop a producer or its output without tripping a test failure.

Asserts:
  1. The three producers (fetch_player_stats, referee_adjustments,
     suspension_tracker) appear in the steps list.
  2. They run AFTER fetch_results and BEFORE apply_matchday_adjustments
     (suspension_tracker reads results+events; ordering is load-bearing).
  3. The git-add allow-list includes the three output filenames.

Yaml parsing: PyYAML is a transitive dep already in the dev env, but
absent from production requirements.txt — gate with importorskip so the
test can run in either context.

Run:
    python3 -m pytest tests/live/test_workflow_yaml.py -q
"""
from __future__ import annotations

import unittest
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "matchday-intel-slow.yml"

EXPECTED_PRODUCER_SCRIPTS = (
    "fetch_player_stats",
    "referee_adjustments",
    "suspension_tracker",
)

# Outputs that MUST land in the git add allow-list so the auto-commit
# step pushes them to origin. The orchestrator's freshness guard fires a
# `subsystem_stale` warning if they're missing on the CI host.
EXPECTED_OUTPUT_FILENAMES = (
    "player_stats_2026.json",
    "referee_2026.json",
    "suspensions_2026.json",
)


def _load_workflow() -> dict:
    """Parse the workflow YAML. Caches the parsed dict on the function so
    repeated tests don't re-parse."""
    with WORKFLOW_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _matchday_job_steps() -> list[dict]:
    wf = _load_workflow()
    job = wf["jobs"]["matchday-intel"]
    steps = job["steps"]
    assert isinstance(steps, list) and steps, "matchday-intel.steps empty"
    return steps


def _step_command(step: dict) -> str:
    """Return the `run:` block text for a step, or '' if it's a uses-step."""
    return step.get("run", "") or ""


def _find_step_index_for_script(steps: list[dict], script_name: str) -> int:
    """First step index whose `run:` mentions `scripts/live/<script_name>`.
    Returns -1 if not found."""
    needle = f"scripts/live/{script_name}"
    for i, st in enumerate(steps):
        if needle in _step_command(st):
            return i
    return -1


class TestMatchdayIntelSlowWorkflow(unittest.TestCase):
    """The schedule must invoke the 3 producers in dependency order and
    commit their outputs."""

    def test_workflow_parses(self):
        wf = _load_workflow()
        self.assertIn("jobs", wf)
        self.assertIn("matchday-intel", wf["jobs"])

    def test_all_three_producers_present(self):
        steps = _matchday_job_steps()
        for script in EXPECTED_PRODUCER_SCRIPTS:
            with self.subTest(script=script):
                idx = _find_step_index_for_script(steps, script + ".py")
                self.assertGreaterEqual(
                    idx, 0,
                    msg=(
                        f"missing workflow step for scripts/live/{script}.py "
                        "— Wave-2 S1 producer never runs, orchestrator's "
                        "freshness guard will emit subsystem_stale every tick"
                    ),
                )

    def test_producers_run_after_fetch_results(self):
        """fetch_results --with-events must run BEFORE referee_adjustments
        and suspension_tracker. suspension_tracker walks the per-match
        `events` lists; if results refresh comes second the events are
        stale relative to that tick's referee assignments."""
        steps = _matchday_job_steps()
        results_idx = _find_step_index_for_script(steps, "fetch_results.py")
        self.assertGreaterEqual(
            results_idx, 0,
            msg="fetch_results step missing — suspension_tracker would have no events",
        )
        for downstream in ("referee_adjustments", "suspension_tracker"):
            with self.subTest(downstream=downstream):
                idx = _find_step_index_for_script(steps, downstream + ".py")
                self.assertGreater(
                    idx, results_idx,
                    msg=(
                        f"{downstream} runs at index {idx} but fetch_results "
                        f"runs at {results_idx} — events stale for this tick"
                    ),
                )

    def test_producers_run_before_apply_matchday_adjustments(self):
        """The consolidator must run LAST — otherwise it reads stale
        snapshots from the previous tick instead of this tick's writes."""
        steps = _matchday_job_steps()
        apply_idx = _find_step_index_for_script(
            steps, "apply_matchday_adjustments.py")
        self.assertGreaterEqual(apply_idx, 0)
        for producer in EXPECTED_PRODUCER_SCRIPTS:
            with self.subTest(producer=producer):
                idx = _find_step_index_for_script(steps, producer + ".py")
                self.assertLess(
                    idx, apply_idx,
                    msg=(
                        f"{producer} runs at {idx} >= apply_matchday at "
                        f"{apply_idx} — consolidator reads stale snapshot"
                    ),
                )

    def test_fetch_results_runs_with_events_flag(self):
        """suspension_tracker reads `events` off completed_matches; the
        --with-events flag is the only way to populate that field. Without
        it the workflow can call fetch_results AND suspension_tracker in
        order but the latter still produces empty + `no_events_in_snapshot`."""
        steps = _matchday_job_steps()
        idx = _find_step_index_for_script(steps, "fetch_results.py")
        self.assertGreaterEqual(idx, 0)
        cmd = _step_command(steps[idx])
        self.assertIn(
            "--with-events", cmd,
            msg=(
                "fetch_results invocation in the slow workflow MUST pass "
                "--with-events so suspension_tracker has card events to walk"
            ),
        )

    def test_fetch_results_step_sets_football_provider(self):
        """2026-07-03 fix: the 'Fetch results with events' step set
        API_FOOTBALL_KEY but NOT FOOTBALL_PROVIDER, so
        fetch_results.get_provider_name() (FOOTBALL_PROVIDER →
        WC_RESULTS_SOURCE → 'mock') resolved to 'mock' and the 3h
        event-enrichment fetch was a silent no-op — suspensions built
        from an eventless snapshot every tick. The key alone never
        selects a provider; the step env must carry the provider var
        (plus its back-compat alias) exactly like the fast workflow."""
        steps = _matchday_job_steps()
        idx = _find_step_index_for_script(steps, "fetch_results.py")
        self.assertGreaterEqual(idx, 0)
        env = steps[idx].get("env") or {}
        self.assertIn(
            "FOOTBALL_PROVIDER", env,
            msg=(
                "fetch_results step env missing FOOTBALL_PROVIDER — "
                "get_provider_name() falls back to 'mock' and the "
                "--with-events enrichment fetches nothing"
            ),
        )
        self.assertIn(
            "WC_RESULTS_SOURCE", env,
            msg="back-compat provider alias missing from step env",
        )
        self.assertIn("vars.FOOTBALL_PROVIDER", str(env["FOOTBALL_PROVIDER"]),
                      msg="provider must come from the repo variable, "
                          "mirroring live-matchday.yml's resolution order")

    def test_git_add_includes_three_new_outputs(self):
        """The commit step's git add allow-list must include the three
        producer outputs. Without this the workflow runs the producers
        but never publishes the snapshots back to origin/main, leaving
        the consolidator on the next tick reading stale-or-missing files."""
        steps = _matchday_job_steps()
        # Find the commit step by searching for `git add` in any step.
        commit_step_cmds = [_step_command(s) for s in steps if "git add" in _step_command(s)]
        self.assertEqual(
            len(commit_step_cmds), 1,
            msg=f"expected exactly one git-add step, found {len(commit_step_cmds)}",
        )
        cmd = commit_step_cmds[0]
        for fname in EXPECTED_OUTPUT_FILENAMES:
            with self.subTest(fname=fname):
                self.assertIn(
                    f"data/live/{fname}", cmd,
                    msg=(
                        f"git add allow-list missing data/live/{fname} — "
                        "Wave-2 S1: producer would run but its output never "
                        "lands on origin, freshness guard never green"
                    ),
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
